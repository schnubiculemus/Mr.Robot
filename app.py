import logging
import threading
import os
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, request, jsonify
from config import WAHA_API_KEY, BOT_NAME, WEBHOOK_SECRET
from core.database import init_db, get_or_create_user, save_message, get_chat_history
from core.ollama_client import chat as ollama_chat
from core.whatsapp import send_message, extract_message, init_waha
from core.tasks import create_task
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

# User-Mapping: WAHA-ID -> Context-Name (= Dateiname in context/ ohne .md)
USER_CONTEXTS = {
    "221152228159675@lid": "tommy",
}

# Owner: nur dieser User darf /merge, /ablehnen und andere privilegierte Commands
OWNER_ID = "221152228159675@lid"

# Per-User Lock: verhindert parallele Kimi-Calls für denselben User.
# Garantiert dass Antworten in der richtigen Reihenfolge kommen.
_user_locks = {}
_user_locks_guard = threading.Lock()

# ThreadPool: begrenzt die Anzahl gleichzeitiger Background-Threads.
# max_workers=4: 1 Chat-Thread + 1 Fast-Track pro User, mit Puffer.
# Verhindert unkontrolliertes Thread-Wachstum bei Nachrichtenspitzen.
_thread_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="schnubot")


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

    # --- Chat-Verarbeitung im ThreadPool ---
    # Sofort 200 an WAHA zurück, damit der Webhook nicht timeout'd.
    # Kimi-Antwort + Fast-Track laufen asynchron im begrenzten Pool.
    _thread_pool.submit(_process_chat, phone_number, text, display_name, context_name)

    return jsonify({"status": "processing"}), 200


def _process_chat(phone_number, text, display_name, context_name):
    """
    Verarbeitet eine Chat-Nachricht im Background-Thread.
    Kimi-Call + Antwort senden + Fast-Track.
    Per-User Lock garantiert Reihenfolge bei schnellen Doppel-Nachrichten.
    """
    lock = _get_user_lock(phone_number)
    with lock:
        try:
            # Fast-Track im Pool starten (nicht als verschachtelter Thread)
            _thread_pool.submit(_safe_fast_track, phone_number, text)

            # History HIER holen (nicht im Webhook), damit bei Doppel-Nachrichten
            # die zweite Nachricht Kimis Antwort auf die erste sieht.
            history = get_chat_history(phone_number)

            # Kimi-Antwort generieren (kann bis zu 120s dauern)
            reply = ollama_chat(phone_number, text, history, context_name)
            save_message(phone_number, "assistant", reply)
            send_message(phone_number, reply + "\n\n[kimi]")

            logger.info(f"Antwort an {display_name}: {reply[:100]}")

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
        from memory.memory_store import get_active_collection
        from datetime import datetime, timezone
        from autonomy import get_pending_pr

        report = build_full_report()
        mem = report["memory"]
        dist = report["distribution"]
        errors = report["errors_24h"]
        res = report["resources"]

        # Heute neu erstellte Chunks zählen
        collection = get_active_collection()
        all_data = collection.get(include=["metadatas"])
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        today_count = sum(
            1 for meta in all_data["metadatas"]
            if meta.get("created_at", "").startswith(today_str)
        )

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
