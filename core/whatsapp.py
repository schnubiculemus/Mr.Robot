import requests

WAHA_API_URL = "http://localhost:3000"
WAHA_API_KEY = None
WAHA_SESSION = "default"


def init_waha(api_key):
    """Setzt den API-Key."""
    global WAHA_API_KEY
    WAHA_API_KEY = api_key


def get_headers():
    """Standard-Headers für WAHA API Calls."""
    headers = {"Content-Type": "application/json"}
    if WAHA_API_KEY:
        headers["X-Api-Key"] = WAHA_API_KEY
    return headers


def send_message(to, text):
    """Sendet eine WhatsApp-Nachricht über WAHA."""
    url = f"{WAHA_API_URL}/api/sendText"

    # WhatsApp hat ein Limit von ~4096 Zeichen pro Nachricht
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

        # Nur Nachrichten von anderen (nicht eigene)
        if message.get("fromMe", False):
            return None, None, None

        # Absender-ID (LID-Format: 221152228159675@lid)
        from_id = message.get("from", "")

        # Display-Name des Absenders
        notify_name = message.get("_data", {}).get("notifyName", "Unbekannt")

        # Text extrahieren
        text = message.get("body", "")
        if not text:
            text = message.get("_data", {}).get("body", "")

        # Medien erkennen (PDF via hasMedia) — VOR Text-Return prüfen
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
            # Audio/Sprachnachrichten (ogg, mp3, m4a, ptt)
            if any(t in mimetype.lower() for t in ("audio", "ogg", "mpeg")) and media_url:
                sentinel = f"[MEDIA:audio:{media_url}:{filename}]"
                return from_id, sentinel, notify_name

        if text:
            return from_id, text, notify_name

        return None, None, None

    except (KeyError, IndexError, TypeError):
        return None, None, None
