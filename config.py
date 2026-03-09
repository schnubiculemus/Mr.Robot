import os
from dotenv import load_dotenv

load_dotenv()

# Ollama Cloud API
OLLAMA_API_URL = os.getenv("OLLAMA_API_URL", "https://api.ollama.com")
OLLAMA_API_KEY = os.getenv("OLLAMA_API_KEY", "")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "kimi-k2.5:latest")
OLLAMA_EXTRACTION_MODEL = os.getenv("OLLAMA_EXTRACTION_MODEL", "ministral-3:8b")

# WAHA
WAHA_API_KEY = os.getenv("WAHA_API_KEY", "")

# Web Search (Tavily)
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
if not WEBHOOK_SECRET:
    import logging as _l
    _l.getLogger(__name__).warning(
        "WEBHOOK_SECRET ist nicht gesetzt — eingehende Webhooks werden nicht verifiziert."
    )

# Bot-Name (zentral)
BOT_NAME = os.getenv("BOT_NAME", "Mr.Robot")

# Datenbank
DB_PATH = os.getenv("DB_PATH", "bot.db")

# Chat-Kontext
MAX_CONTEXT_MESSAGES = int(os.getenv("MAX_CONTEXT_MESSAGES", "30"))

# User-Mapping: WAHA-ID -> Context-Name (= Dateiname in context/ ohne .md)
# Zentral definiert, wird von app.py und heartbeat.py importiert (P1.9).
USER_CONTEXTS = {
    "221152228159675@lid": "tommy",
}

# Owner: nur dieser User darf /merge, /ablehnen und andere privilegierte Commands
OWNER_ID = "221152228159675@lid"

# Dashboard Auth
# Token MUSS in .env gesetzt sein: DASHBOARD_TOKEN=<secrets.token_hex(32)>
# Generieren: python3 -c "import secrets; print(secrets.token_hex(32))"
_dashboard_token_raw = os.getenv("DASHBOARD_TOKEN", "")
if not _dashboard_token_raw:
    raise RuntimeError(
        "DASHBOARD_TOKEN ist nicht gesetzt. Bitte in .env eintragen:\n"
        "  python3 -c 'import secrets; print(secrets.token_hex(32))'"
    )
DASHBOARD_TOKEN = _dashboard_token_raw

# Flask Secret Key (separat vom Dashboard-Token)
# Generieren: python3 -c "import secrets; print(secrets.token_hex(32))"
_flask_secret_raw = os.getenv("FLASK_SECRET_KEY", "")
if not _flask_secret_raw:
    raise RuntimeError(
        "FLASK_SECRET_KEY ist nicht gesetzt. Bitte in .env eintragen:\n"
        "  python3 -c 'import secrets; print(secrets.token_hex(32))'"
    )
FLASK_SECRET_KEY = _flask_secret_raw
