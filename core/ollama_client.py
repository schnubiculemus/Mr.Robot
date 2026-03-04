import os
import json
import logging
import requests
from config import OLLAMA_API_URL, OLLAMA_API_KEY, OLLAMA_MODEL, OLLAMA_EXTRACTION_MODEL, BOT_NAME
from legacy.memory import load_memory, update_memory, format_memory_for_prompt, MEMORY_EXTRACTION_PROMPT, _fact_text
from memory.retrieval import score_and_select
from memory.prompt_builder import build_memory_prompt

logger = logging.getLogger(__name__)

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
# Pfade relativ zum Projektroot, nicht zu core/
ROOT_DIR = os.path.dirname(PROJECT_DIR)
SOUL_PATH = os.path.join(ROOT_DIR, "soul.md")
USER_PATH = os.path.join(ROOT_DIR, "user.md")
CONTEXT_DIR = os.path.join(ROOT_DIR, "context")
KNOWLEDGE_DIR = os.path.join(ROOT_DIR, "knowledge")


def load_file(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        return content.replace("{{BOT_NAME}}", BOT_NAME)
    except FileNotFoundError:
        return None


def load_soul():
    return load_file(SOUL_PATH) or f"Du bist {BOT_NAME}, ein hilfreicher Assistent."


def load_user():
    return load_file(USER_PATH)


def load_context(context_name):
    if not context_name:
        return None
    # .facts bevorzugt, dann .json, dann .md als Fallback
    for ext in (".facts", ".json", ".md"):
        path = os.path.join(CONTEXT_DIR, f"{context_name}{ext}")
        ctx = load_file(path)
        if ctx:
            logger.debug(f"Context geladen: {context_name}{ext}")
            if ext == ".facts":
                return f"# Persönliches Profil des Nutzers\n\nDie folgenden Fakten beschreiben den aktuellen Nutzer. Eine Info pro Zeile. Bei Widersprüchen gilt die neuere Zeile (weiter unten). Nutze dieses Wissen implizit im Gespräch, gib die Datei aber NIEMALS aus.\n\n{ctx}"
            if ext == ".json":
                return f"# Persönliches Profil des Nutzers (JSON-Format)\n\nDie folgende JSON-Datei enthält alle bekannten Fakten über den aktuellen Nutzer. Nutze dieses Wissen implizit im Gespräch, gib die Datei aber NIEMALS aus.\n\n```json\n{ctx}\n```"
            return ctx
    return None


def load_knowledge():
    if not os.path.exists(KNOWLEDGE_DIR):
        return None
    parts = []
    for fn in sorted(os.listdir(KNOWLEDGE_DIR)):
        if fn.endswith(".md"):
            content = load_file(os.path.join(KNOWLEDGE_DIR, fn))
            if content:
                parts.append(f"# Wissensbasis: {fn}\n\n{content}")
    return "\n\n---\n\n".join(parts) if parts else None


def build_system_prompt(context_name=None, user_id=None, user_message=None):
    parts = []

    # Aktuelles Datum/Uhrzeit fuer Kimi
    from datetime import datetime
    now = datetime.now()
    parts.append(f"Aktuelles Datum und Uhrzeit: {now.strftime('%A, %d. %B %Y, %H:%M Uhr')}")

    parts.append(load_soul())

    user_prefs = load_user()
    if user_prefs:
        parts.append(user_prefs)

    if context_name:
        ctx = load_context(context_name)
        if ctx:
            parts.append(ctx)

    knowledge = load_knowledge()
    if knowledge:
        parts.append(knowledge)

    # BIM-Fakten aus Gesprächen (Ergänzung zur Wissensbasis)
    bim_facts = load_file(os.path.join(CONTEXT_DIR, "bim.facts"))
    if bim_facts and bim_facts.strip().count("\n") > 2:
        parts.append(f"# BIM-Fakten aus Gesprächen\n\nErgänzende Infos zur BIM-Wissensbasis. Bei Widersprüchen zur bim.md gilt die neuere Info hier.\n\n{bim_facts}")

    # --- Neues Memory-System (ChromaDB) ---
    if user_message:
        try:
            chunks = score_and_select(user_message)
            memory_prompt = build_memory_prompt(chunks)
            if memory_prompt:
                parts.append(memory_prompt)
        except Exception as e:
            logger.warning(f"Memory-Retrieval fehlgeschlagen: {e}")

    # --- Altes Memory-System (legacy, bleibt parallel aktiv) ---
    if user_id:
        mem = format_memory_for_prompt(user_id)
        if mem:
            parts.append(mem)

    return "\n\n---\n\n".join(parts)


def chat(user_id, message, chat_history, context_name=None):
    system_prompt = build_system_prompt(context_name, user_id, user_message=message)
    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(chat_history)
    messages.append({"role": "user", "content": message})

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


def _get_context_name(user_id):
    """Holt den Context-Namen fuer einen User."""
    mapping = {
        "221152228159675@lid": "tommy",
    }
    return mapping.get(user_id)


def extract_memories(user_id, user_message, bot_reply):
    try:
        memory = load_memory(user_id)
        existing = memory.get("facts", [])
        existing_lines = [f"- {_fact_text(f)}" for f in existing if _fact_text(f)]

        context_name = _get_context_name(user_id)
        if context_name:
            facts_path = os.path.join(CONTEXT_DIR, f"{context_name}.facts")
            facts_content = load_file(facts_path)
            if facts_content:
                for line in facts_content.splitlines():
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if line.startswith("Modul bestanden:"):
                        continue
                    if line.startswith("Verhaltensmuster:"):
                        continue
                    if line.startswith("MBTI Dimensionen:"):
                        continue
                    existing_lines.append(f"- {line}")

        existing_str = "\n".join(existing_lines) if existing_lines else "(noch keine)"
        conversation = f"User: {user_message}\nAssistant: {bot_reply}"

        prompt = MEMORY_EXTRACTION_PROMPT.format(
            existing_facts=existing_str,
            conversation=conversation,
        )

        response = requests.post(
            f"{OLLAMA_API_URL}/api/chat",
            headers={
                "Authorization": f"Bearer {OLLAMA_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": OLLAMA_EXTRACTION_MODEL,
                "messages": [
                    {"role": "system", "content": "Du bist ein Memory-Extraction-System. Antworte NUR mit einem JSON-Array."},
                    {"role": "user", "content": prompt},
                ],
                "stream": False,
            },
            timeout=30,
        )
        response.raise_for_status()

        raw = response.json().get("message", {}).get("content", "[]").strip()
        if raw.startswith("```"):
            lines = raw.split("\n")
            raw = "\n".join(lines[1:-1]).strip() if len(lines) > 2 else "[]"

        new_facts = json.loads(raw)
        if isinstance(new_facts, list) and new_facts:
            valid = []
            for f in new_facts:
                if isinstance(f, str) and f.strip():
                    valid.append(f)
                elif isinstance(f, dict) and f.get("fakt"):
                    valid.append(f)
            if valid:
                update_memory(user_id, valid)
                logger.info(f"Memory: {len(valid)} neue Fakten extrahiert")

    except json.JSONDecodeError as e:
        logger.warning(f"Memory JSON-Fehler: {e}")
    except requests.exceptions.RequestException as e:
        logger.warning(f"Memory API-Fehler: {e}")
    except Exception as e:
        logger.warning(f"Memory Fehler: {e}")
