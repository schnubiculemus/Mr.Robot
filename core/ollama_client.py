"""
SchnuBot.ai - Ollama Client (Post-Cutover)
Referenz: Konzeptdokument V1.1

System-Prompt Aufbau:
1. Datum/Uhrzeit
2. soul.md (Identität)
3. architecture.md (Selbstwissen)
4. Memory-Chunks aus ChromaDB (dynamisch, kontextabhängig)

Legacy-Systeme entfernt:
- Kein format_memory_for_prompt (altes Memory)
- Kein load_context (tommy.facts) — jetzt in ChromaDB
- Kein load_knowledge (knowledge/*.md) — jetzt in ChromaDB
- Kein bim.facts — jetzt in ChromaDB
- Kein user.md — in soul.md aufgegangen
- Kein extract_memories — ersetzt durch Konsolidierer + Fast-Track
"""

import os
import logging
import requests
from config import OLLAMA_API_URL, OLLAMA_API_KEY, OLLAMA_MODEL, BOT_NAME
from memory.retrieval import score_and_select
from memory.prompt_builder import build_memory_prompt

logger = logging.getLogger(__name__)

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(PROJECT_DIR)
SOUL_PATH = os.path.join(ROOT_DIR, "soul.md")
ARCHITECTURE_PATH = os.path.join(ROOT_DIR, "architecture.md")


def load_file(path):
    """Lädt eine Datei und ersetzt {{BOT_NAME}} Placeholder."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        return content.replace("{{BOT_NAME}}", BOT_NAME)
    except FileNotFoundError:
        return None


def load_soul():
    """Lädt die Verfassung (soul.md). Fallback auf Minimal-Prompt."""
    return load_file(SOUL_PATH) or f"Du bist {BOT_NAME}, ein hilfreicher Assistent."


def load_architecture():
    """Lädt das Selbstwissen (architecture.md)."""
    return load_file(ARCHITECTURE_PATH)


def build_system_prompt(context_name=None, user_id=None, user_message=None):
    """
    Baut den System-Prompt dynamisch zusammen.

    1. Datum/Uhrzeit
    2. soul.md — immer
    3. architecture.md — immer
    4. Memory-Chunks — dynamisch basierend auf user_message
    """
    parts = []

    # 1. Datum/Uhrzeit (Berliner Zeit für Tommy)
    from core.datetime_utils import format_berlin
    parts.append(f"Aktuelles Datum und Uhrzeit: {format_berlin()}")

    # 2. Verfassung
    parts.append(load_soul())

    # 3. Selbstwissen
    arch = load_architecture()
    if arch:
        parts.append(arch)

    # 4. Memory-Chunks (kontextabhängig)
    if user_message:
        try:
            chunks = score_and_select(user_message)
            memory_prompt = build_memory_prompt(chunks)
            if memory_prompt:
                parts.append(memory_prompt)
        except Exception as e:
            logger.warning(f"Memory-Retrieval fehlgeschlagen: {e}")

    return "\n\n---\n\n".join(parts)


def chat(user_id, message, chat_history, context_name=None):
    """Sendet eine Nachricht an Kimi und gibt die Antwort zurück.
    
    WICHTIG: chat_history enthält die aktuelle User-Nachricht bereits
    (wird in app.py vor dem Thread-Start via save_message gespeichert).
    Daher KEIN zusätzlicher append von message — sonst sieht Kimi sie doppelt.
    message wird nur für build_system_prompt (Memory-Retrieval) verwendet.
    """
    system_prompt = build_system_prompt(context_name, user_id, user_message=message)
    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(chat_history)

    try:
        response = requests.post(
            f"{OLLAMA_API_URL}/api/chat",
            headers={
                "Authorization": f"Bearer {OLLAMA_API_KEY}",
                "Content-Type": "application/json",
            },
            json={"model": OLLAMA_MODEL, "messages": messages, "stream": False},
            timeout=120,
        )
        response.raise_for_status()
        return response.json().get("message", {}).get("content", "Hmm, da kam keine Antwort zurueck.")

    except requests.exceptions.Timeout:
        return "Sorry, Kimi braucht gerade zu lange. Versuch's nochmal!"
    except requests.exceptions.RequestException as e:
        return f"API-Fehler: {str(e)}"
