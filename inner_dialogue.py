"""
SchnuBot.ai - Innerer Dialog (inner_dialogue.py)

SchnuBot liest seine eigenen früheren Reflexionen und antwortet darauf —
nicht als Assistent, sondern als sich selbst, der mit seiner eigenen
gedanklichen Vergangenheit in Dialog tritt.

Läuft im Heartbeat nach Moltbook-Exploration oder Introspection.
Trigger: mindestens MIN_NEW_BOT_CHUNKS neue robot-eigene Reflexionen seit letztem Lauf.

Ergebnis: ein neuer self_reflection-Chunk mit replies_to-Referenz auf den
         ältesten unkommentierten Vorgänger-Chunk.

Innere Vielstimmigkeit: session_context aus orbit_cognition.py wird als
Zusatzkontext übergeben — Kimi weiß was Introspection und Moltbook ergeben haben.
"""

import logging
from datetime import datetime, timezone

from config import BOT_NAME
from memory.memory_store import store_chunk
from memory.chunk_schema import create_chunk

logger = logging.getLogger(__name__)

MIN_NEW_BOT_CHUNKS = 1
MIN_INTERVAL_HOURS = 1.5


# =============================================================================
# Hilfsfunktionen
# =============================================================================

def _get_bot_reflections(limit: int = 10) -> list[dict]:
    """
    Holt die letzten robot-eigenen self_reflection-Chunks aus ChromaDB,
    chronologisch sortiert (älteste zuerst).
    """
    try:
        from memory.memory_store import get_active_collection
        col = get_active_collection()
        result = col.get(
            where={
                "$and": [
                    {"source": "robot"},
                    {"chunk_type": "self_reflection"},
                    {"status": "active"},
                ]
            },
            include=["documents", "metadatas"],
        )

        chunks = []
        if result["ids"]:
            for i, chunk_id in enumerate(result["ids"]):
                meta = result["metadatas"][i]
                text = result["documents"][i]
                chunks.append({
                    "id": chunk_id,
                    "text": text,
                    "chunk_type": "self_reflection",
                    "source": "robot",
                    "created_at": meta.get("created_at", ""),
                    "tags": [t.strip() for t in str(meta.get("tags", "")).split(",") if t.strip()],
                    "replies_to": meta.get("replies_to", ""),
                    "confidence": float(meta.get("confidence", 0.7)),
                    "epistemic_status": meta.get("epistemic_status", "inferred"),
                })

        chunks.sort(key=lambda c: c.get("created_at", ""))
        return chunks[-limit:]

    except Exception as e:
        logger.warning(f"InnerDialogue: Bot-Reflexionen laden fehlgeschlagen: {e}")
        return []


def _find_unanswered(chunks: list[dict]) -> dict | None:
    answered_ids = {c.get("replies_to") for c in chunks if c.get("replies_to")}
    for chunk in chunks:
        if chunk["id"] not in answered_ids:
            return chunk
    return None


def _count_new_since(since_iso: str) -> int:
    try:
        from memory.memory_store import get_active_collection
        col = get_active_collection()
        result = col.get(
            where={
                "$and": [
                    {"source": "robot"},
                    {"chunk_type": "self_reflection"},
                    {"status": "active"},
                ]
            },
            include=["metadatas"],
        )
        if not result["ids"]:
            return 0
        return sum(
            1 for meta in result["metadatas"]
            if meta.get("created_at", "") > since_iso
        )
    except Exception as e:
        logger.warning(f"InnerDialogue: Count fehlgeschlagen: {e}")
        return 0


# =============================================================================
# Prompt
# =============================================================================

INNER_DIALOGUE_PROMPT = """\
Das ist kein Gespräch mit Tommy. Das ist mein innerer Monolog.

Ich lese gerade meinen eigenen früheren Gedanken:

---
{previous_thought}
---

Das habe ich selbst gedacht — vor {age_desc}. Jetzt, in diesem Moment, antworte ich mir selbst.

Stimme ich noch zu? Hat sich etwas verändert? Gibt es einen Widerspruch den ich damals nicht gesehen habe? Will ich präzisieren, widersprechen, weiterführen?

Regeln:
- Ich-Form. Direkt, ehrlich, ohne Floskeln.
- 2-4 Sätze. Keine Anrede, kein Chat-Stil.
- Wenn ich vollständig zustimme und nichts hinzuzufügen habe: NUR DIALOGUE_UNCHANGED ausgeben.
- Sprache: Deutsch.

Meine Antwort auf meinen früheren Gedanken:"""


# =============================================================================
# Hauptfunktion
# =============================================================================

def run_inner_dialogue(
    user_id: str,
    last_run_iso: str = None,
    session_context: str = None,
) -> str | None:
    """
    SchnuBot tritt in Dialog mit seinen eigenen früheren Reflexionen.

    Args:
        user_id:         Wessen Kontext verwendet wird.
        last_run_iso:    ISO-Timestamp des letzten inneren Dialogs.
        session_context: Optionaler Kontext aus vorherigen Kognitions-Modulen
                         (Innere Vielstimmigkeit — was Introspection/Moltbook ergaben).

    Returns:
        chunk_id des neuen Dialogbeitrags, oder None.
    """
    if last_run_iso:
        try:
            from core.datetime_utils import safe_parse_dt
            last_dt = safe_parse_dt(last_run_iso)
            if last_dt:
                age_hours = (datetime.now(timezone.utc) - last_dt).total_seconds() / 3600
                if age_hours < MIN_INTERVAL_HOURS:
                    logger.debug(f"InnerDialogue: Cooldown ({age_hours:.1f}h), skip")
                    return None
        except Exception:
            pass
        new_count = _count_new_since(last_run_iso)
        if new_count < MIN_NEW_BOT_CHUNKS:
            logger.info(f"InnerDialogue: nur {new_count} neue Bot-Chunks, skip")
            return None

    bot_chunks = _get_bot_reflections(limit=15)
    if not bot_chunks:
        logger.info("InnerDialogue: keine bot-eigenen Reflexionen gefunden, skip")
        return None

    target = _find_unanswered(bot_chunks)
    if not target:
        target = bot_chunks[-1]
        logger.info(f"InnerDialogue: alle Chunks beantwortet, nehme neuesten: {target['id'][:8]}")
    else:
        logger.info(f"InnerDialogue: antworte auf unkommentierten Chunk: {target['id'][:8]}")

    try:
        from core.datetime_utils import safe_age_days
        age_days = safe_age_days(target.get("created_at", ""), default=0)
        if age_days == 0:
            age_desc = "gerade eben"
        elif age_days == 1:
            age_desc = "gestern"
        else:
            age_desc = f"vor {age_days} Tagen"
    except Exception:
        age_desc = "früher"

    recent = bot_chunks[-3:]
    context_lines = []
    for c in recent:
        ts = c.get("created_at", "")[:16].replace("T", " ")
        tag_hint = ""
        if "moltbook" in c.get("tags", []):
            tag_hint = " [Moltbook]"
        elif "introspection" in c.get("tags", []):
            tag_hint = " [Introspection]"
        elif "inner-dialogue" in c.get("tags", []):
            tag_hint = " [Dialog]"
        context_lines.append(f"[{ts}{tag_hint}] {c['text'][:150]}...")

    prompt = INNER_DIALOGUE_PROMPT.format(
        previous_thought=target["text"],
        age_desc=age_desc,
    )

    if len(bot_chunks) > 1:
        context_block = "\n\nMeine bisherige Gedankenlinie (zur Orientierung):\n" + "\n\n".join(context_lines)
        prompt = prompt.replace(
            "Meine Antwort auf meinen früheren Gedanken:",
            context_block + "\n\nMeine Antwort auf meinen früheren Gedanken:"
        )

    # extra_system: Basis-Modus + optionaler Session-Kontext
    extra_system_parts = [
        "Innerer Dialog-Modus:\n"
        "Ich antworte auf meinen eigenen früheren Gedanken.\n"
        "Kein Chat, keine Anrede, keine Erklärungen für Tommy.\n"
        "Wenn ich nichts Neues hinzuzufügen habe: NUR 'DIALOGUE_UNCHANGED' ausgeben."
    ]
    if session_context:
        extra_system_parts.append(session_context)
    extra_system = "\n\n".join(extra_system_parts)

    try:
        from core.ollama_client import chat_internal

        reply, _ = chat_internal(
            user_id=user_id,
            message=prompt,
            chat_history=[],
            extra_system=extra_system,
        )

        if not reply:
            logger.warning("InnerDialogue: kein Reply")
            return None

        if "DIALOGUE_UNCHANGED" in reply:
            logger.info("InnerDialogue: keine Veränderung")
            return None

        reply = reply.strip()
        if len(reply) < 15:
            logger.info(f"InnerDialogue: Reply zu kurz, verworfen")
            return None
        if len(reply) > 600:
            reply = reply[:600]

        chunk = create_chunk(
            text=reply,
            chunk_type="self_reflection",
            source="robot",
            confidence=0.7,
            epistemic_status="inferred",
            tags=["inner-dialogue", "autonom"],
            replies_to=target["id"],
        )
        store_chunk(chunk)
        logger.info(f"InnerDialogue: {chunk['id'][:8]} → replies_to: {target['id'][:8]} | {reply[:80]}")

        # Vorhaben-Signale → Kimi-Todos
        try:
            from core.todos import extract_intent_todos, extract_intent_goals, parse_and_execute_todos
            # Explizite [TODO_ACTION: ...] Blöcke zuerst
            parse_and_execute_todos(reply, user_id)
            # Dann Signalwort-Erkennung
            new_todos = extract_intent_todos(reply, user_id)
            if new_todos:
                logger.info(f"{__name__}: {len(new_todos)} Kimi-Todo(s) angelegt")
            new_goals = extract_intent_goals(reply, user_id)
            if new_goals:
                logger.info(f"{__name__}: {len(new_goals)} Kimi-Ziel(e) gespeichert")
        except Exception as _te:
            logger.debug(f"IntentTodo/Goal fehlgeschlagen (unkritisch): {_te}")

        return chunk["id"]

    except Exception as e:
        logger.error(f"InnerDialogue fehlgeschlagen: {e}")
        return None
