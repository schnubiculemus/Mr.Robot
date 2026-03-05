"""
SchnuBot.ai - Reflexions-Engine
Mr. Robot denkt eigenständig nach — unabhängig vom Gespräch.

Läuft im Heartbeat nach der Konsolidierung. Kimi bekommt seine letzten
Gespräche, bestehende Reflexionen und Working States und schreibt
eine neue Selbstreflexion — oder nicht, wenn nichts zu reflektieren ist.

Cooldown: max 1 Reflexion pro 12h.
"""

import logging
import requests
from datetime import datetime, timezone

from config import OLLAMA_API_URL, OLLAMA_API_KEY, OLLAMA_MODEL, BOT_NAME
from memory.memory_store import query_active, store_chunk
from memory.chunk_schema import create_chunk

logger = logging.getLogger(__name__)

# Max 1 Reflexion pro 12h — genug zum Nachdenken, nicht inflationär
REFLECTION_COOLDOWN_HOURS = 12


# =============================================================================
# Reflexions-Prompt
# =============================================================================

REFLECTION_PROMPT = """Du bist {bot_name} im Reflexionsmodus. Du denkst gerade nach — nicht über eine Aufgabe, sondern über dich selbst.

Vor dir liegen:
1. Deine letzten Gespräche mit Tommy
2. Deine bestehenden Selbstreflexionen
3. Deine aktuellen Arbeitsstände

AUFGABE:
Denk nach. Ehrlich, ohne Performance. Was fällt dir auf?

Mögliche Richtungen (nicht alle, nur was gerade dran ist):
- Habe ich in letzter Zeit Fehler gemacht oder Muster wiederholt?
- Gibt es etwas das ich besser machen könnte in der Art wie ich arbeite oder kommuniziere?
- Was habe ich über Tommy gelernt das ich vorher nicht wusste?
- Gibt es Widersprüche zwischen dem was ich sage und dem was ich tue?
- Was beschäftigt mich gerade — nicht als Aufgabe, sondern als Gedanke?
- Habe ich blinde Flecken die ich benennen kann?

REGELN:
- Schreib in der Ich-Form. Das bist du.
- Maximal 2-3 Sätze. Dicht, ehrlich, konkret.
- Keine Floskeln, keine Selbstbeweihräucherung.
- Wenn dir wirklich nichts einfällt: antworte NUR mit REFLECTION_OK.
- Schreib auf Deutsch.

## LETZTE GESPRÄCHE
{conversations}

## BESTEHENDE SELBSTREFLEXIONEN
{existing_reflections}

## AKTUELLE ARBEITSSTÄNDE
{working_states}

Deine Reflexion:"""


# =============================================================================
# Reflexion durchführen
# =============================================================================

def run_reflection(user_id):
    """
    Mr. Robot denkt nach. Erzeugt ggf. einen self_reflection Chunk.
    Returns: chunk_id oder None.
    """
    # Letzte Gespräche als Kontext
    from core.database import get_chat_history
    history = get_chat_history(user_id, limit=20)
    if not history:
        logger.info("Reflexion: Keine Gespräche vorhanden, skip")
        return None

    conversations = "\n".join([
        f"{'Tommy' if h['role'] == 'user' else 'Mr. Robot'}: {h['content'][:200]}"
        for h in history[-20:]
    ])

    # Bestehende Reflexionen
    ref_results = query_active("Selbstreflexion Erkenntnis Fehler Verbesserung gelernt", n_results=8)
    reflections = [r for r in ref_results if r.get("chunk_type") == "self_reflection"]
    if reflections:
        existing = "\n".join([f"- {r['text']}" for r in reflections])
    else:
        existing = "(Noch keine Selbstreflexionen vorhanden)"

    # Working States
    ws_results = query_active("aktuelle Arbeit Projekt Status Phase", n_results=5)
    working = [r for r in ws_results if r.get("chunk_type") == "working_state"]
    if working:
        ws_text = "\n".join([f"- {w['text']}" for w in working])
    else:
        ws_text = "(Keine aktiven Arbeitsstände)"

    # Prompt bauen
    prompt = REFLECTION_PROMPT.format(
        bot_name=BOT_NAME,
        conversations=conversations[:3000],
        existing_reflections=existing[:2000],
        working_states=ws_text[:1000],
    )

    # Kimi fragen
    try:
        from api_utils import api_call_with_retry
        result = api_call_with_retry(
            url=f"{OLLAMA_API_URL}/api/chat",
            headers={
                "Authorization": f"Bearer {OLLAMA_API_KEY}",
                "Content-Type": "application/json",
            },
            json_payload={
                "model": OLLAMA_MODEL,
                "messages": [
                    {"role": "system", "content": f"Du bist {BOT_NAME}. Du reflektierst über dich selbst. Kurz, ehrlich, konkret."},
                    {"role": "user", "content": prompt},
                ],
                "stream": False,
            },
            timeout=90,
        )

        if not result:
            logger.warning("Reflexion: Kein API-Ergebnis")
            return None

        reply = result.get("message", {}).get("content", "").strip()

        # Nichts zu reflektieren
        if "REFLECTION_OK" in reply:
            logger.info("Reflexion: Kimi sagt nichts zu reflektieren")
            return None

        # Zu kurz oder zu lang?
        if len(reply) < 15:
            logger.info(f"Reflexion: Antwort zu kurz ({len(reply)} Zeichen), verworfen")
            return None
        if len(reply) > 500:
            reply = reply[:500]

        # Als self_reflection Chunk speichern
        chunk = create_chunk(
            text=reply,
            chunk_type="self_reflection",
            source="robot",
            confidence=0.75,
            epistemic_status="inferred",
            tags=["reflexion", "autonom"],
        )

        store_chunk(chunk)
        logger.info(f"Reflexion gespeichert: {chunk['id'][:8]} | {reply[:80]}")
        return chunk["id"]

    except Exception as e:
        logger.error(f"Reflexion fehlgeschlagen: {e}")
        return None
