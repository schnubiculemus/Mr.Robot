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
import time
from config import OLLAMA_API_URL, OLLAMA_API_KEY, OLLAMA_MODEL, BOT_NAME
from memory.retrieval import score_and_select
from memory.prompt_builder import build_memory_prompt, build_global_rules_prompt
from memory.memory_store import query_active

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


# Cache für globale Regeln (P1.16): Preferences/Decisions ändern sich selten,
# müssen aber bei jeder Nachricht im Prompt sein. TTL vermeidet wiederholte
# Collection-Scans. Cache wird nach 120s invalidiert oder bei leerem Ergebnis.
_global_rules_cache = None
_global_rules_cache_time = 0
_GLOBAL_RULES_TTL = 120  # Sekunden


def _load_global_rules():
    """
    Lädt alle aktiven Preferences und Decisions — IMMER, unabhängig von der Query.
    Gecached für 120s (P1.16): vermeidet Collection-Scan bei jeder Nachricht.

    Returns:
        Liste von Chunk-Dicts, nach Weight*Confidence sortiert (stärkste zuerst)
    """
    global _global_rules_cache, _global_rules_cache_time

    # Cache prüfen
    if _global_rules_cache is not None and (time.time() - _global_rules_cache_time) < _GLOBAL_RULES_TTL:
        return _global_rules_cache

    global_chunks = []

    try:
        collection = __import__('memory.memory_store', fromlist=['get_active_collection']).get_active_collection()
        all_data = collection.get(
            where={"$or": [{"chunk_type": "preference"}, {"chunk_type": "decision"}]},
            include=["documents", "metadatas"],
        )

        if all_data["ids"]:
            for i, chunk_id in enumerate(all_data["ids"]):
                meta = all_data["metadatas"][i]
                text = all_data["documents"][i]

                if meta.get("status", "active") != "active":
                    continue

                try:
                    weight = float(meta.get("weight", 1.0))
                    confidence = float(meta.get("confidence", 0.5))
                except (ValueError, TypeError):
                    weight, confidence = 1.0, 0.5

                global_chunks.append({
                    "id": chunk_id,
                    "text": text,
                    "chunk_type": meta.get("chunk_type", "preference"),
                    "source": meta.get("source", "tommy"),
                    "weight": weight,
                    "confidence": confidence,
                    "epistemic_status": meta.get("epistemic_status", "stated"),
                    "created_at": meta.get("created_at", ""),
                    "tags": meta.get("tags", "").split(",") if meta.get("tags") else [],
                })

            global_chunks.sort(key=lambda c: c["weight"] * c["confidence"], reverse=True)

    except Exception as e:
        logger.warning(f"Globale Regeln laden fehlgeschlagen: {e}")

    # Cache setzen (auch leere Liste cachen, damit nicht dauernd gescannt wird)
    _global_rules_cache = global_chunks
    _global_rules_cache_time = time.time()

    return global_chunks


def build_system_prompt(context_name=None, user_id=None, user_message=None, doc_context=None):
    """
    Baut den System-Prompt dynamisch zusammen.

    1. Datum/Uhrzeit
    2. soul.md — immer
    3. architecture.md — immer
    4. Memory-Chunks — dynamisch basierend auf user_message
    5. Globale Regeln (Preferences + Decisions) — IMMER, am ENDE
       (Recency Bias: LLMs beachten das Ende des System-Prompts stärker)
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

    # 4. Globale Regeln laden (IDs merken für Deduplizierung)
    global_rules = _load_global_rules()
    global_rule_ids = set()
    if global_rules:
        global_rule_ids = {c["id"] for c in global_rules}

    # 5. Memory-Chunks (kontextabhängig) — bei Dokument-Kontext weglassen (spart Platz fuer doc_context)
    if user_message and not doc_context:
        try:
            chunks = score_and_select(user_message)
            # Deduplizierung: Chunks die schon als globale Regeln geladen sind, rausfiltern
            if global_rule_ids:
                chunks = [c for c in chunks if c["id"] not in global_rule_ids]
            memory_prompt = build_memory_prompt(chunks)
            if memory_prompt:
                parts.append(memory_prompt)
        except Exception as e:
            logger.warning(f"Memory-Retrieval fehlgeschlagen: {e}")

    # 6. Globale Regeln am ENDE — nach Memory, vor der User-Nachricht
    # Recency Bias: das Letzte im System-Prompt bekommt das meiste Gewicht.
    if global_rules:
        rules_prompt = build_global_rules_prompt(global_rules)
        if rules_prompt:
            parts.append(rules_prompt)

    # 7. Dokument-Kontext — ganz am Ende, höchste Recency-Priorität
    if doc_context:
        parts.append(
            "DOKUMENT-KONTEXT (bereits extrahiert, liegt vollstaendig vor):\n\n" + doc_context
        )

    return "\n\n---\n\n".join(parts)


def _track_tokens(prompt_tokens, completion_tokens):
    import json
    from datetime import datetime, timezone
    data_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
    os.makedirs(data_dir, exist_ok=True)
    path = os.path.join(data_dir, "token_usage.json")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        with open(path, "r") as f:
            usage = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        usage = {}
    if today not in usage:
        usage[today] = {"prompt": 0, "completion": 0, "total": 0, "calls": 0}
    usage[today]["prompt"] += prompt_tokens
    usage[today]["completion"] += completion_tokens
    usage[today]["total"] += prompt_tokens + completion_tokens
    usage[today]["calls"] += 1
    keys = sorted(usage.keys())
    if len(keys) > 90:
        for k in keys[:-90]:
            del usage[k]
    with open(path, "w") as f:
        json.dump(usage, f, indent=2)


def chat(user_id, message, chat_history, context_name=None, doc_context=None):
    """Sendet eine Nachricht an Kimi und gibt die Antwort zurück.
    
    WICHTIG: chat_history enthält die aktuelle User-Nachricht bereits
    (wird in app.py vor dem Thread-Start via save_message gespeichert).
    Daher KEIN zusätzlicher append von message — sonst sieht Kimi sie doppelt.
    message wird nur für build_system_prompt (Memory-Retrieval) verwendet.
    """
    from api_utils import api_call_with_retry

    system_prompt = build_system_prompt(context_name, user_id, user_message=message, doc_context=doc_context)
    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(chat_history)

    result = api_call_with_retry(
        url=f"{OLLAMA_API_URL}/api/chat",
        headers={
            "Authorization": f"Bearer {OLLAMA_API_KEY}",
            "Content-Type": "application/json",
        },
        json_payload={"model": OLLAMA_MODEL, "messages": messages, "stream": False},
        timeout=120,
    )

    if not result:
        return "Sorry, Kimi ist gerade nicht erreichbar. Versuch's gleich nochmal!"

    # Token-Tracking
    try:
        _track_tokens(
            prompt_tokens=result.get("prompt_eval_count", 0),
            completion_tokens=result.get("eval_count", 0),
        )
    except Exception:
        pass

    return result.get("message", {}).get("content", "Hmm, da kam keine Antwort zurueck.")
