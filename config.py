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

# Bot-Name (zentral)
BOT_NAME = os.getenv("BOT_NAME", "Mr.Robot")

# Datenbank
DB_PATH = os.getenv("DB_PATH", "bot.db")

# Chat-Kontext
MAX_CONTEXT_MESSAGES = int(os.getenv("MAX_CONTEXT_MESSAGES", "30"))
