import logging
import threading
from flask import Flask, request, jsonify
from config import WAHA_API_KEY, BOT_NAME
from core.database import init_db, get_or_create_user, save_message, get_chat_history
from core.ollama_client import chat as ollama_chat
from core.whatsapp import send_message, extract_message, init_waha
from core.tasks import create_task
from memory.fast_track import process_fast_track

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


@app.route("/webhook", methods=["POST"])
def webhook():
    payload = request.get_json()
    phone_number, text, display_name = extract_message(payload)

    if not phone_number or not text:
        return jsonify({"status": "ignored"}), 200

    logger.info(f"Nachricht von {display_name} ({phone_number}): {text[:100]}")

    get_or_create_user(phone_number, display_name)
    context_name = USER_CONTEXTS.get(phone_number)
    history = get_chat_history(phone_number)

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

    # --- Fast-Track: Sofortspeicherung bei expliziten Decisions/Facts ---
    threading.Thread(
        target=_safe_fast_track,
        args=(phone_number, text),
        daemon=True,
    ).start()

    reply = ollama_chat(phone_number, text, history, context_name)
    save_message(phone_number, "assistant", reply)
    send_message(phone_number, reply + "\n\n[kimi]")

    logger.info(f"Antwort an {display_name}: {reply[:100]}")

    return jsonify({"status": "ok"}), 200


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
