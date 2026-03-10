import logging
import re
import threading
import os
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, request, jsonify
from config import WAHA_API_KEY, BOT_NAME, WEBHOOK_SECRET, USER_CONTEXTS, OWNER_ID
from core.websearch import search as web_search, format_for_kimi as format_search_result
from core.database import init_db, get_or_create_user, save_message, get_chat_history
from core.ollama_client import chat as ollama_chat
from core.whatsapp import send_message, extract_message, init_waha
from core.document import is_media_message, parse_media_sentinel, download_media, extract_pdf_text, build_doc_session, search_doc_session, get_doc_session
from core.tasks import (
    create_task, save_task, load_task, build_iteration_prompt, TASK_DONE_TOKEN,
    get_pending_tasks, claim_task, refresh_claim, release_task, generate_runner_id,
    deliver_task,
)
from memory.fast_track import process_fast_track

# logs/ Verzeichnis sicherstellen
os.makedirs("logs", exist_ok=True)

from logging.handlers import RotatingFileHandler as _RFH
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        _RFH("logs/schnubot.log", maxBytes=10*1024*1024, backupCount=5),
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
    _t = text.strip().lower()
    if _t in ["/doc stop", "/doc end", "/dokument stop", "stop", "fertig", "ende", "/stop"]:
        from core.document import clear_doc_session
        clear_doc_session(phone_number)
        send_message(phone_number, "Dokument-Session beendet. Ich bin wieder im normalen Modus.\n\n[kimi]")
        return jsonify({"status": "ok"}), 200

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
    # PDF-Dokument verarbeiten — Download SOFORT im Webhook (WAHA loescht Files schnell)
    if is_media_message(text):
        # Tool-Status prüfen
        try:
            import json as _json
            _tools_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "tools_config.json")
            with open(_tools_path) as _tf:
                _tools = {t["id"]: t for t in _json.load(_tf)}
            if not _tools.get("pdf", {}).get("enabled", True):
                send_message(phone_number, "PDF-Analyse ist gerade deaktiviert.\n\n[kimi]")
                return jsonify({"status": "tool_disabled"}), 200
        except (FileNotFoundError, Exception):
            pass  # Kein Config = Standard = aktiv
        from config import WAHA_API_KEY as _waha_key
        lines = text.strip().split("\n", 1)
        parsed = parse_media_sentinel(lines[0].strip())
        caption = lines[1].strip() if len(lines) > 1 else ""
        if parsed:
            _, media_url, filename = parsed
            pdf_bytes = download_media(media_url, api_key=_waha_key)
            if pdf_bytes:
                if caption:
                    # PDF + Frage: sofortige Meldung, dann Session aufbauen + antworten
                    ack = "Einen Moment — ich schaue mir das Dokument an. 📄"
                    send_message(phone_number, ack + "\n\n[kimi]")
                    _chat_pool.submit(_process_document_search, phone_number, pdf_bytes, filename, caption, display_name, context_name)
                else:
                    # Nur PDF ohne Frage: Session aufbauen, dann bereit-Meldung
                    ack = "Einen Moment — ich schaue mir das Dokument an. 📄"
                    send_message(phone_number, ack + "\n\n[kimi]")
                    _chat_pool.submit(_process_document_index, phone_number, pdf_bytes, filename, display_name)
            else:
                send_message(phone_number, f"Das Dokument konnte leider nicht heruntergeladen werden.\n\n[kimi]")
        return jsonify({"status": "processing_document"}), 200

    # Aktive Doc-Session? Immer Dokument-Modus bis User explizit "stop" schreibt.
    # Kein Classifier — der war unzuverlaessig und hat persoenliche Fragen falsch klassifiziert.
    from core.document import get_doc_session as _get_doc_session
    if _get_doc_session(phone_number):
        save_message(phone_number, "user", text)
        _chat_pool.submit(_handle_doc_followup, phone_number, text, context_name)
        return jsonify({"status": "doc_query"}), 200

    _chat_pool.submit(_process_chat, phone_number, text, display_name, context_name)

    return jsonify({"status": "processing"}), 200


def _handle_web_search(reply: str):
    """
    Prueft ob Kimi in seiner Antwort [SEARCH: query] geschrieben hat.
    Wenn ja: Suche ausfuehren, Kontext-String zurueckgeben.

    Returns:
        (reply_cleaned, search_context_or_None)
    """
    import re
    matches = re.findall(r"\[SEARCH:\s*(.+?)\]", reply, re.IGNORECASE)
    if not matches:
        return reply, None

    # Erste Query fuer die Suche verwenden, ALLE [SEARCH:...] Bloecke entfernen
    query = matches[0].strip()
    logger.info(f"Kimi moechte suchen: '{query}' ({len(matches)} SEARCH-Block(e) gefunden)")

    # Alle [SEARCH:...] Vorkommen entfernen — auch wenn Kimi mehrere geschrieben hat
    reply_cleaned = re.sub(r"\[SEARCH:\s*.+?\]", "", reply, flags=re.IGNORECASE).strip()
    # Doppelte Leerzeilen bereinigen
    reply_cleaned = re.sub(r"\n{3,}", "\n\n", reply_cleaned).strip()

    # Web Search ausfuehren
    result = web_search(query)
    if not result["success"]:
        logger.warning(f"Web Search fehlgeschlagen: {result.get('error')}")
        return reply_cleaned, None

    search_ctx = (
        "WEBSEARCH ERGEBNIS — bereits abgerufen, keine weitere Suche noetig:\n\n"
        + format_search_result(result)
        + "\n\nBeantworte jetzt die Frage des Nutzers direkt auf Basis dieser Informationen. "
        "Schreibe KEIN [SEARCH:...] mehr. Kein Markdown, keine Sternchen. Fliesstext."
    )
    logger.info(f"Web Search erfolgreich: {len(result['answer'])} Zeichen")
    return reply_cleaned, search_ctx


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

            # Web Search: Kimi signalisiert Suchbedarf mit [SEARCH: query]
            reply, search_ctx = _handle_web_search(reply)
            if search_ctx:
                # Kimi nochmal aufrufen — mit Suchergebnis als Kontext
                # WICHTIG: Original-History + Original-Frage, kein leerer Erstantwort-Stub
                logger.info(f"Web Search: starte zweiten Kimi-Call mit Suchergebnis")
                try:
                    search_reply = ollama_chat(phone_number, text, history, context_name,
                                        doc_context=search_ctx)
                    # Sicherstellen dass der zweite Call nicht nochmal sucht
                    search_reply = re.sub(r"\[SEARCH:\s*.+?\]", "", search_reply or "", flags=re.IGNORECASE).strip()
                    search_reply = re.sub(r"\n{3,}", "\n\n", search_reply).strip()
                    if search_reply:
                        reply = search_reply
                        logger.info(f"Web Search: zweiter Call erfolgreich ({len(reply)} Zeichen)")
                    else:
                        logger.warning("Web Search: zweiter Call leer — behalte bereinigte Erstantwort")
                except Exception as e:
                    logger.error(f"Web Search: zweiter Kimi-Call fehlgeschlagen: {e}")

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


def _estimate_time(page_count):
    """Grobe Zeitschaetzung fuer Embedding basierend auf Seitenzahl."""
    seconds = max(5, page_count * 0.7)
    if seconds < 15:
        return "wenige Sekunden"
    elif seconds < 40:
        return "ca. 30 Sekunden"
    elif seconds < 90:
        return "ca. 1 Minute"
    elif seconds < 150:
        return "ca. 2 Minuten"
    else:
        return "einige Minuten"


def _process_document_index(phone_number, pdf_bytes, filename, display_name):
    """Nur PDF ohne Frage: Session aufbauen und Bereit-Meldung senden."""
    try:
        from core.document import extract_pages, chunk_pages, embed_chunks, set_doc_session
        # Extraktion zuerst — schnell, liefert Seitenzahl
        pages = extract_pages(pdf_bytes)
        if not pages:
            send_message(phone_number, "Das PDF konnte leider nicht gelesen werden.\n\n[kimi]")
            return

        # Zweite Meldung mit Zeitschaetzung
        est = _estimate_time(len(pages))
        eta_msg = f"📊 {len(pages)} Seiten — ich indexiere in {est}. Gleich kannst du Fragen stellen."
        send_message(phone_number, eta_msg + "\n\n[kimi]")

        # Chunking + Embedding — Session sofort mit leeren Embeddings setzen
        chunks = chunk_pages(pages)
        set_doc_session(phone_number, filename, chunks, [], len(pages))
        embeddings = embed_chunks(chunks)
        set_doc_session(phone_number, filename, chunks, embeddings, len(pages))

        reply = f'✅ "{filename}" ist geladen — {len(pages)} Seiten, {len(chunks)} Abschnitte.\n\nIch bin jetzt im Dokument-Modus. Stell mir deine Fragen dazu. Schreib "stop" wenn du wieder normal chatten möchtest.'
        save_message(phone_number, "assistant", reply)
        send_message(phone_number, reply + "\n\n[kimi]")
        logger.info(f"Doc-Index aufgebaut: {filename} fuer {display_name}")
    except Exception as e:
        logger.error(f"Doc-Index fehlgeschlagen: {e}")
        send_message(phone_number, "Beim Lesen des Dokuments ist etwas schiefgelaufen.")


def _is_doc_related(text, filename="Dokument"):
    """
    LLM-Classifier: Gehoert die Nachricht zum aktiven Dokument oder ist es normaler Chat?
    Mini-Call mit max_tokens=5 — schnell und zuverlaessig.
    Fallback auf True (Dokument) bei API-Fehler, damit keine Frage verloren geht.
    """
    # Sehr kurze Nachrichten ohne Fragecharakter direkt als Chat klassifizieren
    t = text.strip()
    if len(t) < 4:
        return False

    # Harter Small-Talk-Filter vor dem API-Call (spart Token)
    small_talk_starts = ("hallo", "hi ", "hey ", "moin", "servus", "danke", "ok ", "okay",
                         "ciao", "tschüss", "tschues", "gute nacht", "guten morgen")
    if t.lower().startswith(small_talk_starts):
        return False

    try:
        classifier_prompt = (
            f'Ein Nutzer hat ein Dokument namens "{filename}" hochgeladen. '
            f'Entscheide ob die folgende Nachricht eine inhaltliche Frage zum Dokument ist.\n\n'
            f'IMMER "chat" wenn:\n'
            f'- Fragen ueber Personen, Erinnerungen, Beziehungen (z.B. "Erinnerst du dich an X?")\n'
            f'- Small Talk, Begruessungen, Gespraeche ueber den Bot selbst\n'
            f'- Persoenliche Fragen die nichts mit dem Dokumentinhalt zu tun haben\n'
            f'- Aufgaben wie "merk dir", "vergiss", "notiere"\n\n'
            f'IMMER "doc" wenn:\n'
            f'- Fragen zum Inhalt, Seiten, Abschnitten oder Konzepten des Dokuments\n'
            f'- Zusammenfassungen, Erklaerungen, Suche im Dokument\n\n'
            f'Nachricht: "{text}"\n\n'
            f'Antworte NUR mit einem Wort: doc oder chat'
        )
        result = api_call_with_retry(
            url=f"{OLLAMA_API_URL}/api/chat",
            headers={"Authorization": f"Bearer {OLLAMA_API_KEY}", "Content-Type": "application/json"},
            json_payload={
                "model": OLLAMA_MODEL,
                "messages": [{"role": "user", "content": classifier_prompt}],
                "stream": False,
                "options": {"num_predict": 5, "temperature": 0},
            },
            timeout=15,
        )
        if result:
            answer = result.get("message", {}).get("content", "").strip().lower()
            logger.debug(f"Doc-Classifier: '{text[:40]}' → '{answer}'")
            return "doc" in answer
    except Exception as e:
        logger.warning(f"Doc-Classifier fehlgeschlagen: {e} — Fallback: doc")

    return True  # Fallback: lieber Dokument als Frage verlieren


def _answer_doc_query(phone_number, query, context_name):
    """Beantwortet eine Frage gegen die aktive Doc-Session."""
    fundstellen, relevance, filename = search_doc_session(phone_number, query)

    # Keine History bei Dokument-Calls — verhindert Halluzination durch alte Nachrichten
    history = []

    if relevance == "no_session":
        return "Ich habe gerade kein Dokument geladen. Schick mir bitte zuerst ein PDF."

    if relevance == "none" or not fundstellen:
        no_result_ctx = (
            f'Der Nutzer fragt zum Dokument "{filename}". '
            f'Das Retrieval hat keine ausreichend relevanten Stellen gefunden. '
            f'Antworte ehrlich dass du dazu nichts Belastbares im Dokument gefunden hast.'
        )
        return ollama_chat(phone_number, query, history, context_name, doc_context=no_result_ctx)

    relevance_hint = "Die Treffer sind sehr relevant." if relevance == "strong" else "Die Treffer sind nur indirekt relevant — nutze sie als Hintergrund, aber beantworte die Frage direkt."

    doc_ctx = (
        f'Aktives Dokument: "{filename}"\n'
        f"Unten stehen Fundstellen aus dem Dokument die zur Frage passen.\n\n"
        f"WICHTIG: Beantworte die Frage des Nutzers direkt und vollstaendig. "
        f"Wenn er eine Meinung, Einschaetzung oder Diskussion will — gib sie. "
        f"Nutze die Fundstellen als Grundlage und Belege, nicht als Antwort-Ersatz. "
        f"Wenn die Frage persoenlich oder nicht dokumentbezogen ist, antworte normal aus deinem Gedaechtnis.\n"
        f"{relevance_hint}\n"
        f"Keine Sternchen, kein Markdown, kein Bold. Fliesstext.\n\n"
        f"Fundstellen:\n\n{fundstellen}"
    )

    # History laden damit Kimi Kontext ueber den User hat (letzte 6 Nachrichten)
    history = get_chat_history(phone_number)[-6:] if get_chat_history(phone_number) else []

    return ollama_chat(phone_number, query, history, context_name, doc_context=doc_ctx)


def _handle_doc_followup(phone_number, query, context_name):
    """Folgefrage bei aktiver Doc-Session."""
    lock = _get_user_lock(phone_number)
    with lock:
        try:
            reply = _answer_doc_query(phone_number, query, context_name)
            save_message(phone_number, "assistant", reply)
            send_message(phone_number, reply + "\n\n[kimi]")
        except Exception as e:
            logger.error(f"Doc-Followup fehlgeschlagen: {e}")
            send_message(phone_number, "Beim Durchsuchen des Dokuments ist etwas schiefgelaufen.")


def _process_document_search(phone_number, pdf_bytes, filename, caption, display_name, context_name):
    """PDF + Frage: Session aufbauen, dann direkt suchen und antworten."""
    lock = _get_user_lock(phone_number)
    with lock:
        try:
            from core.document import extract_pages, chunk_pages, embed_chunks, set_doc_session
            pages = extract_pages(pdf_bytes)
            if not pages:
                reply = "Das PDF konnte leider nicht gelesen werden."
                save_message(phone_number, "assistant", reply)
                send_message(phone_number, reply + "\n\n[kimi]")
                return

            est = _estimate_time(len(pages))
            send_message(phone_number, f"📊 {len(pages)} Seiten — ich bin in {est} bereit.\n\n[kimi]")

            chunks = chunk_pages(pages)
            # Session sofort mit leeren Embeddings setzen — verhindert dass Folgefragen
            # durch normales Memory-Retrieval laufen waehrend Embedding noch laeuft
            set_doc_session(phone_number, filename, chunks, [], len(pages))
            embeddings = embed_chunks(chunks)
            set_doc_session(phone_number, filename, chunks, embeddings, len(pages))
            page_count = len(pages)

            user_msg = f"[PDF: {filename}] {caption}"
            save_message(phone_number, "user", user_msg)
            reply = _answer_doc_query(phone_number, caption, context_name)
            save_message(phone_number, "assistant", reply)
            send_message(phone_number, reply + "\n\n[kimi]")
            logger.info(f"Dokument-Suche: {filename} / '{caption[:50]}' fuer {display_name}")

        except Exception as e:
            logger.error(f"Dokument-Suche fehlgeschlagen: {e}")
            send_message(phone_number, "Beim Lesen des Dokuments ist etwas schiefgelaufen.")


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
