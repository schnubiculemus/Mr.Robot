import time
import threading
import requests

WAHA_API_URL = "http://localhost:3000"
WAHA_API_KEY = None
WAHA_SESSION = "default"
WEBHOOK_URL = "http://172.17.0.1:5000/webhook"


def init_waha(api_key):
    """Setzt den API-Key und startet Webhook-Registrierung im Hintergrund."""
    global WAHA_API_KEY
    WAHA_API_KEY = api_key
    # Webhook-Auto-Register deaktiviert — WAHA verliert Webhook beim PUT
    # Webhook muss manuell im WAHA Dashboard gesetzt werden
    pass


def _register_webhook_with_retry(max_attempts: int = 30, delay: int = 3) -> None:
    """
    Wartet bis WAHA WORKING ist, setzt dann den Webhook.
    Läuft im Hintergrund-Thread — blockiert den Bot-Start nicht.
    """
    import logging
    logger = logging.getLogger(__name__)

    for attempt in range(max_attempts):
        try:
            resp = requests.get(
                f"{WAHA_API_URL}/api/sessions",
                headers=get_headers(),
                timeout=5,
            )
            if resp.status_code == 200:
                sessions = resp.json()
                session = next((s for s in sessions if s.get("name") == WAHA_SESSION), None)
                if session and session.get("status") == "WORKING":
                    # Webhook setzen
                    put_resp = requests.put(
                        f"{WAHA_API_URL}/api/sessions/{WAHA_SESSION}",
                        headers=get_headers(),
                        json={"webhooks": [{"url": WEBHOOK_URL, "events": ["message"]}]},
                        timeout=10,
                    )
                    if put_resp.status_code in (200, 201):
                        logger.info(f"WAHA Webhook registriert: {WEBHOOK_URL}")
                        return
                    else:
                        logger.warning(f"Webhook-Registrierung fehlgeschlagen: {put_resp.status_code} {put_resp.text[:100]}")
                        # Trotzdem weitermachen — WAHA hat den Webhook manchmal schon gesetzt
                        return
        except Exception as e:
            logger.debug(f"Webhook-Register Versuch {attempt+1}: {e}")

        time.sleep(delay)

    import logging
    logging.getLogger(__name__).warning("Webhook-Registrierung: WAHA nicht erreichbar nach max. Versuchen")


def get_headers():
    """Standard-Headers für WAHA API Calls."""
    headers = {"Content-Type": "application/json"}
    if WAHA_API_KEY:
        headers["X-Api-Key"] = WAHA_API_KEY
    return headers


def send_message(to, text):
    """Sendet eine WhatsApp-Nachricht über WAHA."""
    url = f"{WAHA_API_URL}/api/sendText"

    chunks = split_message(text, max_length=4000)

    for chunk in chunks:
        payload = {
            "session": WAHA_SESSION,
            "chatId": to,
            "text": chunk,
        }

        try:
            response = requests.post(url, headers=get_headers(), json=payload, timeout=30)
            response.raise_for_status()
        except requests.exceptions.RequestException as e:
            print(f"Fehler beim Senden an {to}: {e}")
            return False

    return True


def split_message(text, max_length=4000):
    """Teilt lange Nachrichten in Chunks auf."""
    if len(text) <= max_length:
        return [text]

    chunks = []
    while text:
        if len(text) <= max_length:
            chunks.append(text)
            break

        split_pos = text.rfind("\n", 0, max_length)
        if split_pos == -1:
            split_pos = text.rfind(" ", 0, max_length)
        if split_pos == -1:
            split_pos = max_length

        chunks.append(text[:split_pos])
        text = text[split_pos:].lstrip()

    return chunks


def extract_message(payload):
    """Extrahiert die Nachricht und Absender-ID aus dem WAHA Webhook-Payload."""
    try:
        event = payload.get("event")

        if event != "message":
            return None, None, None

        message = payload.get("payload", {})

        if message.get("fromMe", False):
            return None, None, None

        from_id = message.get("from", "")
        notify_name = message.get("_data", {}).get("notifyName", "Unbekannt")
        text = message.get("body", "")
        if not text:
            text = message.get("_data", {}).get("body", "")

        if message.get("hasMedia"):
            media = message.get("media", {})
            mimetype = media.get("mimetype", "")
            filename = media.get("filename", "document")
            media_url = media.get("url", "")
            if "pdf" in mimetype.lower() and media_url:
                caption = text or ""
                sentinel = f"[MEDIA:pdf:{media_url}:{filename}]"
                combined = (sentinel + "\n" + caption).strip()
                return from_id, combined, notify_name
            if any(t in mimetype.lower() for t in ("audio", "ogg", "mpeg")) and media_url:
                sentinel = f"[MEDIA:audio:{media_url}:{filename}]"
                return from_id, sentinel, notify_name

        if text:
            return from_id, text, notify_name

        return None, None, None

    except (KeyError, IndexError, TypeError):
        return None, None, None
