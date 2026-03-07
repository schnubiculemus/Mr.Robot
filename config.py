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
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")  # Optional: Auth für eingehende Webhooks

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
# Token selbst setzen: python3 -c "import secrets; print(secrets.token_hex(32))"
DASHBOARD_TOKEN = os.getenv("DASHBOARD_TOKEN", "97af1c5b59637b0372b9bee663720bb3a0c715749e09c6c9320f970e217fa0e9")
