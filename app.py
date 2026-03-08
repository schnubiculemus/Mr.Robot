import logging
import threading
import os
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, request, jsonify
from config import WAHA_API_KEY, BOT_NAME, WEBHOOK_SECRET, USER_CONTEXTS, OWNER_ID
from core.database import init_db, get_or_create_user, save_message, get_chat_history
from core.ollama_client import chat as ollama_chat
from core.whatsapp import send_message, extract_message, init_waha
from core.document import is_media_message, process_media_message, parse_media_sentinel, download_media, extract_pdf_text
from core.tasks import (
    create_task, save_task, load_task, build_iteration_prompt, TASK_DONE_TOKEN,
    get_pending_tasks, claim_task, refresh_claim, release_task, generate_runner_id,
    deliver_task,
)
from memory.fast_track import process_fast_track

# logs/ Verzeichnis sicherstellen
os.makedirs("logs", exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("logs/schnubot.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
init_db()
init_waha(WAHA_API_KEY)

# Memory-System beim Start vorladen (Embedding-Modell + ChromaDB)
# Verhindert Race Conditions beim ersten Request
logger.info("Lade Memory-System...")
from memory import memory_store
memory_store.get_embedder()
logger.info("Embedder geladen, lade Collection...")
memory_store.get_active_collection()
logger.info("Memory-System bereit.")

# Per-User Lock: verhindert parallele Kimi-Calls für denselben User.
# Garantiert dass Antworten in der richtigen Reihenfolge kommen.
_user_locks = {}
_user_locks_guard = threading.Lock()

# --- Getrennte ThreadPools (P1.2) ---
# Chat-Pool: für zeitkritische Interaktion (Chat-Antworten + Fast-Track).
# max_workers=4: 1 Chat-Worker pro User + Fast-Track parallel.
# Darf NICHT durch lang laufende Tasks blockiert werden.
_chat_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="chat")

# Task-Pool: für lang laufende Task-Iterationen (Minuten pro Task).
# max_workers=2: max 2 parallele Tasks, reicht für Single-User.
# Blockiert den Chat-Pool nicht, auch wenn Tasks lange laufen.
_task_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="task")


def _get_user_lock(user_id):
    """Gibt den Lock für einen User zurück (erstellt ihn bei Bedarf)."""
    with _user_locks_guard:
        if user_id not in _user_locks:
            _user_locks[user_id] = threading.Lock()
        return _user_locks[user_id]


@app.route("/webhook", methods=["POST"])
def webhook():
    # --- Webhook-Authentifizierung ---
    # Wenn WEBHOOK_SECRET gesetzt ist, muss der Request einen gültigen Header mitbringen.
    # WAHA kann so konfiguriert werden dass es einen Custom-Header sendet.
    # Ohne gesetztes Secret wird jeder Request akzeptiert (Abwärtskompatibel).
    if WEBHOOK_SECRET:
        auth_header = request.headers.get("X-Webhook-Secret", "")
        if auth_header != WEBHOOK_SECRET:
            logger.warning(f"Webhook abgelehnt: ungültiges Secret von {request.remote_addr}")
            return jsonify({"status": "unauthorized"}), 401

    payload = request.get_json()
    phone_number, text, display_name = extract_message(payload)

    if not phone_number or not text:
        return jsonify({"status": "ignored"}), 200

    # --- User-Whitelist: nur bekannte User-IDs verarbeiten ---
    if phone_number not in USER_CONTEXTS:
        logger.warning(f"Webhook ignoriert: unbekannte User-ID {phone_number}")
        return jsonify({"status": "unknown_user"}), 200

    logger.info(f"Nachricht von {display_name} ({phone_number}): {text[:100]}")

    get_or_create_user(phone_number, display_name)
    context_name = USER_CONTEXTS.get(phone_number)

    save_message(phone_number, "user", text)

    # /task Befehl erkennen
    if text.strip().lower().startswith("/task "):
        task_prompt = text.strip()[6:].strip()
        if task_prompt:
            task_id = create_task(phone_number, task_prompt, context_name)
            reply = f"Task angenommen (#{task_id}). Ich arbeite im Hintergrund daran und schicke dir das Ergebnis wenn es fertig ist."
            save_message(phone_number, "assistant", reply)
            send_message(phone_number, reply)
            logger.info(f"Task erstellt: {task_id}")

            # Sofort im Task-Pool starten — nicht auf Heartbeat warten
            _task_pool.submit(_run_task_iterations, task_id, phone_number, context_name)

            return jsonify({"status": "task_created"}), 200
        else:
            reply = "Bitte gib einen Auftrag nach /task an, z.B.: /task Schreib mir einen Textbaustein fuer BIM-Anforderungen"
            save_message(phone_number, "assistant", reply)
            send_message(phone_number, reply)
            return jsonify({"status": "ok"}), 200

    # /merge Befehl — Soul-PR übernehmen (nur Owner)
    if text.strip().lower() in ("/merge", "merge"):
        if phone_number != OWNER_ID:
            logger.warning(f"Merge abgelehnt: {phone_number} ist nicht Owner")
            return jsonify({"status": "unauthorized"}), 200
        from autonomy import handle_merge
        reply = handle_merge(phone_number)
        save_message(phone_number, "assistant", reply)
        send_message(phone_number, reply)
        logger.info(f"Soul-PR Merge: {reply[:100]}")
        return jsonify({"status": "soul_merge"}), 200

    # /ablehnen Befehl — Soul-PR verwerfen (nur Owner)
    if text.strip().lower() in ("/ablehnen", "ablehnen"):
        if phone_number != OWNER_ID:
            logger.warning(f"Reject abgelehnt: {phone_number} ist nicht Owner")
            return jsonify({"status": "unauthorized"}), 200
        from autonomy import handle_reject
        reply = handle_reject(phone_number)
        save_message(phone_number, "assistant", reply)
        send_message(phone_number, reply)
        logger.info(f"Soul-PR Reject: {reply[:100]}")
        return jsonify({"status": "soul_reject"}), 200

    # /status Befehl — System-Status und Chunk-Stats
    if text.strip().lower() in ("/status", "status"):
        reply = _build_status_reply()
        save_message(phone_number, "assistant", reply)
        send_message(phone_number, reply)
        logger.info(f"Status abgefragt")
        return jsonify({"status": "status_sent"}), 200

    # --- Chat-Verarbeitung im Chat-Pool ---
    # Sofort 200 an WAHA zurück, damit der Webhook nicht timeout'd.
    # Kimi-Antwort + Fast-Track laufen asynchron im dedizierten Chat-Pool.
    # PDF-Dokument verarbeiten — Download SOFORT im Webhook (WAHA löscht Files schnell)
    if is_media_message(text):
        from config import WAHA_API_KEY as _waha_key
        lines = text.strip().split("\n", 1)
        parsed = parse_media_sentinel(lines[0].strip())
        caption = lines[1].strip() if len(lines) > 1 else ""
        if parsed:
            _, media_url, filename = parsed
            pdf_bytes = download_media(media_url, api_key=_waha_key)
            _chat_pool.submit(_process_document, phone_number, pdf_bytes, filename, caption, display_name, context_name)
        return jsonify({"status": "processing_document"}), 200

    _chat_pool.submit(_process_chat, phone_number, text, display_name, context_name)

    return jsonify({"status": "processing"}), 200


def _process_chat(phone_number, text, display_name, context_name):
    """
    Verarbeitet eine Chat-Nachricht im Background-Thread.
    Kimi-Call + Antwort senden + Fast-Track (in dieser Reihenfolge).
    Per-User Lock garantiert Reihenfolge bei schnellen Doppel-Nachrichten.

    Reihenfolge (P1.3): Erst Antwort generieren und senden, DANN Fast-Track.
    Fast-Track darf keine Memory-Schreibvorgänge auslösen bevor die Antwort
    steht, weil sonst der Kimi-Call Chunks sehen könnte die auf einer noch
    unbeantworteten Nachricht basieren.
    """
    lock = _get_user_lock(phone_number)
    with lock:
        try:
            # History HIER holen (nicht im Webhook), damit bei Doppel-Nachrichten
            # die zweite Nachricht Kimis Antwort auf die erste sieht.
            history = get_chat_history(phone_number)

            # Kimi-Antwort generieren (kann bis zu 120s dauern)
            reply = ollama_chat(phone_number, text, history, context_name)
            save_message(phone_number, "assistant", reply)
            send_message(phone_number, reply + "\n\n[kimi]")

            logger.info(f"Antwort an {display_name}: {reply[:100]}")

            # Fast-Track NACH der Antwort (P1.3): Memory-Writes erst wenn Antwort raus ist
            _chat_pool.submit(_safe_fast_track, phone_number, text)

        except Exception as e:
            logger.error(f"Chat-Verarbeitung fehlgeschlagen für {phone_number}: {e}")
            try:
                error_reply = "Da ist was schiefgelaufen. Versuch's nochmal!"
                save_message(phone_number, "assistant", error_reply)
                send_message(phone_number, error_reply)
            except Exception:
                logger.error(f"Auch Error-Reply fehlgeschlagen für {phone_number}")


def _build_status_reply():
    """Baut den /status Reply — System-Health + Chunk-Stats, kein LLM-Call nötig."""
    try:
        from monitor import build_full_report
        from core.datetime_utils import now_utc
        from autonomy import get_pending_pr

        report = build_full_report()
        mem = report["memory"]
        dist = report["distribution"]
        errors = report["errors_24h"]
        res = report["resources"]

        # "Heute neu" aus der Distribution — kein extra Full-Scan nötig (P1.11)
        today_count = dist.get("today_count", 0)

        # Offener Soul-PR?
        pending = get_pending_pr()
        pr_line = "Ja" if pending and pending.get("status") == "pending" else "Nein"

        # Heartbeat-Zeiten
        hb_lines = []
        for key, val in report["heartbeat"].items():
            if "last_run" in key:
                hb_lines.append(f"Letzter Heartbeat: {val[:16].replace('T', ' ')}")
            elif "last_consolidation" in key:
                hb_lines.append(f"Letzte Konsolidierung: {val[:16].replace('T', ' ')}")

        # Fehler
        total_errors = sum(errors.values())
        error_line = "Keine" if total_errors == 0 else f"{errors['schnubot']} App, {errors['heartbeat']} Heartbeat"

        # Chunk-Verteilung formatieren
        type_order = ["knowledge", "hard_fact", "working_state", "preference", "self_reflection", "decision"]
        type_lines = []
        for t in type_order:
            count = dist["by_type"].get(t, 0)
            if count > 0:
                type_lines.append(f"{t}: {count}")

        reply = (
            f"📊 *System-Status*\n"
            f"\n"
            f"System: ONLINE\n"
            f"RAM: {res.get('ram_used_mb', '?')}/{res.get('ram_total_mb', '?')} MB ({res.get('ram_percent', '?')}%)\n"
            f"CPU: {res.get('cpu_load_1m', '?')} | Disk: {res.get('disk_percent', '?')}\n"
            f"\n"
            f"Chunks: {mem['active_chunks']} aktiv, {mem['archived_chunks']} archiviert\n"
            f"Heute neu: {today_count}\n"
            f"\n"
            f"Verteilung:\n"
            + "\n".join(type_lines) + "\n"
            f"\n"
            + "\n".join(hb_lines) + "\n"
            f"Fehler 24h: {error_line}\n"
            f"Soul-PR offen: {pr_line}"
        )

        return reply

    except Exception as e:
        logger.error(f"Status-Abfrage fehlgeschlagen: {e}")
        return f"Status-Abfrage fehlgeschlagen: {e}"


def _run_task_iterations(task_id, user_id, context_name):
    """
    Führt alle Task-Iterationen am Stück durch (im ThreadPool).
    Läuft unabhängig vom Chat — kein Per-User-Lock nötig.
    Pause zwischen Iterationen verhindert API-Überlastung.

    Ownership-Modell (P1.1):
    - Claimed den Task mit eigener runner_id beim Start
    - Refresht den Claim zwischen Iterationen
    - Gibt den Claim bei API-Fehler frei → Heartbeat kann übernehmen
    """
    import time
    from config import OLLAMA_API_URL, OLLAMA_API_KEY, OLLAMA_MODEL
    from core.ollama_client import build_system_prompt
    from core.datetime_utils import to_iso
    from api_utils import api_call_with_retry

    PAUSE_BETWEEN_ITERATIONS = 10  # Sekunden zwischen Runden

    # Task laden
    task = load_task(task_id)
    if not task:
        return

    # Ownership claimen
    runner_id = generate_runner_id("app")
    claim_task(task, runner_id)

    logger.info(f"Task-Runner gestartet: {task_id} (max {task['max_iterations']} Iterationen, runner={runner_id})")

    while task["current_iteration"] < task["max_iterations"]:
        iteration = task["current_iteration"]
        logger.info(f"Task {task_id}: Iteration {iteration + 1}/{task['max_iterations']}")

        system_prompt = build_system_prompt(context_name, user_id, user_message=task.get("prompt", ""))
        iter_prompt = build_iteration_prompt(task)

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": iter_prompt},
        ]

        result_json = api_call_with_retry(
            url=f"{OLLAMA_API_URL}/api/chat",
            headers={
                "Authorization": f"Bearer {OLLAMA_API_KEY}",
                "Content-Type": "application/json",
            },
            json_payload={"model": OLLAMA_MODEL, "messages": messages, "stream": False},
            timeout=180,
        )

        if not result_json:
            logger.error(f"Task {task_id}: API nicht erreichbar in Iteration {iteration + 1}, Claim freigegeben")
            release_task(task, runner_id)
            return  # Heartbeat kann übernehmen

        result = result_json.get("message", {}).get("content", "").strip()

        if not result:
            logger.warning(f"Task {task_id}: Leere Antwort in Iteration {iteration + 1}, überspringe")
            save_task(task)
            continue

        # Iteration speichern
        task["iterations"].append({
            "iteration": iteration + 1,
            "result": result,
            "timestamp": to_iso(),
        })
        task["current_iteration"] = iteration + 1
        save_task(task)

        # Fertig?
        if TASK_DONE_TOKEN in result.upper().replace(" ", "_"):
            logger.info(f"Task {task_id}: DONE nach Iteration {iteration + 1}")
            task["status"] = "done"
            save_task(task)
            deliver_task(task)
            return

        # Claim refreshen und Pause vor nächster Iteration
        if not refresh_claim(task, runner_id):
            logger.warning(f"Task {task_id}: Claim verloren, stoppe Runner")
            return
        time.sleep(PAUSE_BETWEEN_ITERATIONS)

    # Max Iterationen erreicht
    logger.info(f"Task {task_id}: Max Iterationen erreicht")
    task["status"] = "done"
    save_task(task)
    deliver_task(task)


def _process_document(phone_number, pdf_bytes, filename, caption, display_name, context_name):
    """Verarbeitet ein eingehendes PDF-Dokument (bytes bereits heruntergeladen)."""
    lock = _get_user_lock(phone_number)
    with lock:
        try:
            if pdf_bytes:
                extracted = extract_pdf_text(pdf_bytes)
                doc_context = f"[DOKUMENT: {filename}]\n\n{extracted}" if extracted else None
            else:
                doc_context = None

            if doc_context is None:
                if filename:
                    reply = f"Das PDF '{filename}' konnte ich leider nicht lesen."
                else:
                    reply = "Dieses Medienformat kann ich noch nicht verarbeiten."
                send_message(phone_number, reply + "\n\n[kimi]")
                save_message(phone_number, "assistant", reply)
                return

            # Dokument + Caption als User-Nachricht speichern
            user_msg = f"[PDF: {filename}]" + (f" {caption}" if caption else "")
            save_message(phone_number, "user", user_msg)

            # Dokument-Call: History laden, vergiftete Nachrichten rausfiltern
            raw_history = get_chat_history(phone_number)
            bad = ["localhost:3000", "kein Zugriff", "PDF-Tool", "blind f", "nicht aktiv", "nicht lesen", "nicht parsen"]
            history = [m for m in raw_history if not any(p in m.get("content", "") for p in bad)]

            if caption:
                doc_prompt = caption
            else:
                doc_prompt = f"Du hast ein Dokument erhalten: {filename}. Fass in 2-3 Saetzen zusammen worum es geht und frag ob ich etwas Bestimmtes wissen moechte."
            import logging as _l; _l.getLogger("app").info(f"DOC_CTX_LEN: {len(doc_context) if doc_context else 0}"); reply = ollama_chat(phone_number, doc_prompt, history, context_name, doc_context=doc_context)
            save_message(phone_number, "assistant", reply)
            send_message(phone_number, reply + "\n\n[kimi]")
            logger.info(f"Dokument verarbeitet: {filename} für {display_name}")

        except Exception as e:
            logger.error(f"Dokument-Verarbeitung fehlgeschlagen: {e}")
            try:
                send_message(phone_number, "Beim Lesen des Dokuments ist etwas schiefgelaufen.")
            except Exception:
                pass


def _safe_fast_track(user_id, text):
    """Fast-Track in separatem Thread mit Error-Handling."""
    try:
        chunk_id = process_fast_track(user_id, text)
        if chunk_id:
            logger.info(f"Fast-Track Chunk gespeichert: {chunk_id[:8]}...")
    except Exception as e:
        logger.warning(f"Fast-Track Fehler: {e}")


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "alive", "bot": BOT_NAME}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
