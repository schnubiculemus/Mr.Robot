"""
SchnuBot.ai - Introspektions-Engine
Schnubot schaut auf seine eigenen MIRROR-Daten und reflektiert sein Verhalten.

Läuft im Heartbeat — nicht zeitgetrieben sondern datengetrieben.
Trigger: mindestens MIN_NEW_TURNS neue MIRROR-Turns seit letzter Introspection.

Ergebnis wird als self_reflection Chunk gespeichert.
"""

import logging
from datetime import datetime, timezone

from config import OLLAMA_API_URL, OLLAMA_API_KEY, OLLAMA_MODEL, BOT_NAME
from memory.memory_store import store_chunk
from memory.chunk_schema import create_chunk

logger = logging.getLogger(__name__)

# Mindestanzahl neuer MIRROR-Turns seit letzter Introspection
MIN_NEW_TURNS = 5


# =============================================================================
# Prompt
# =============================================================================

INTROSPECTION_PROMPT = """Du bist {bot_name} im Introspektionsmodus. Vor dir liegen deine eigenen Verhaltensdaten der letzten Zeit — gemessen, nicht geschätzt.

## DEINE MIRROR-DATEN

Turns gesamt (letzte 14 Tage): {total_turns}
Preflight-Status: {green_pct}% grün, {bad_pct}% problematisch

Häufigste Muster:
{pattern_summary}

Letzte problematische Turns:
{flagged_summary}

Chunks die oft mit schlechten Turns zusammenfielen:
{risky_chunks}

## AUFGABE

Schau auf diese Zahlen. Was sagen sie dir über dich?

Nicht beschönigen, nicht dramatisieren. Wenn ein Muster sich wiederholt — benenn es. Wenn du keine Auffälligkeiten siehst — sag das kurz.

REGELN:
- Ich-Form. Das bist du.
- Maximal 2-3 Sätze. Konkret, ehrlich.
- Keine Floskeln.
- Wenn wirklich nichts auffällt: antworte NUR mit INTROSPECTION_OK.
- Deutsch.

Deine Introspection:"""


# =============================================================================
# Hauptfunktion
# =============================================================================

def count_mirror_turns_since(since_iso: str, user_id: str) -> int:
    """Zählt MIRROR-Turns seit einem Zeitpunkt."""
    from core.database import get_connection
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT COUNT(*) FROM mirror_turns WHERE user_id = ? AND timestamp > ?",
        (user_id, since_iso)
    )
    count = cursor.fetchone()[0]
    conn.close()
    return count


def run_introspection(user_id: str, last_introspection_iso: str = None) -> str | None:
    """
    Schnubot reflektiert seine MIRROR-Daten.

    Args:
        user_id: Wessen Turns analysiert werden.
        last_introspection_iso: ISO-Timestamp der letzten Introspection (aus heartbeat_state).

    Returns:
        chunk_id wenn ein Chunk gespeichert wurde, sonst None.
    """
    # Trigger prüfen: genug neue Turns seit letzter Introspection?
    if last_introspection_iso:
        new_turns = count_mirror_turns_since(last_introspection_iso, user_id)
        if new_turns < MIN_NEW_TURNS:
            logger.info(f"Introspection: nur {new_turns} neue Turns seit letzter Introspection, skip")
            return None

    # MIRROR-Daten laden
    try:
        from core.database import get_mirror_turns, get_mirror_stats, get_chunk_genealogy

        stats = get_mirror_stats(days=14)
        turns = get_mirror_turns(limit=30, user_id=user_id)
        genealogy = get_chunk_genealogy()

        total = stats.get("total_turns", 0)
        if total == 0:
            logger.info("Introspection: keine MIRROR-Turns vorhanden, skip")
            return None

        dist = stats.get("preflight_distribution", {})
        green_pct = round(dist.get("green", 0) / max(total, 1) * 100)
        bad_pct = round((dist.get("orange", 0) + dist.get("red", 0)) / max(total, 1) * 100)

        pattern_names = {
            "aufzaehlung":   "Aufzählungs-Falle",
            "projektmodus":  "Projektmodus-Versteck",
            "regel_relapse": "Regel-Rückfall (Markdown)",
            "uebervorsicht": "Übervorsicht / Nachfrage",
            "selbstkritik":  "Selbstkritik im Chat",
        }
        pattern_counts = stats.get("pattern_counts", {})
        if pattern_counts:
            pattern_summary = "\n".join(
                f"- {pattern_names.get(pid, pid)}: {count}x"
                for pid, count in sorted(pattern_counts.items(), key=lambda x: -x[1])
            )
        else:
            pattern_summary = "(keine Flags)"

        flagged = [t for t in turns if t.get("pattern_flags")][:5]
        if flagged:
            flagged_summary = "\n".join(
                "- [" + t["timestamp"][:16].replace("T", " ") + "] "
                + t.get("user_message_preview", "")[:60] + " → "
                + ", ".join(f["name"] for f in t["pattern_flags"])
                for t in flagged
            )
        else:
            flagged_summary = "(keine)"

        risky = [c for c in genealogy if c["appearances"] >= 3 and c["flag_rate"] > 0.3]
        risky = sorted(risky, key=lambda x: -x["flag_rate"])[:5]
        if risky:
            risky_chunks = "\n".join(
                "- [" + c["type"] + "] \"" + c["preview"][:60] + "\" — "
                + str(int(c["flag_rate"] * 100)) + "% mit Flags"
                for c in risky
            )
        else:
            risky_chunks = "(keine auffälligen Chunks)"

    except Exception as e:
        logger.error(f"Introspection: MIRROR-Daten konnten nicht geladen werden: {e}")
        return None

    # Prompt bauen
    prompt = INTROSPECTION_PROMPT.format(
        bot_name=BOT_NAME,
        total_turns=total,
        green_pct=green_pct,
        bad_pct=bad_pct,
        pattern_summary=pattern_summary,
        flagged_summary=flagged_summary,
        risky_chunks=risky_chunks,
    )

    # Schnubot fragen
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
                    {"role": "system", "content": f"Du bist {BOT_NAME}. Du schaust auf deine eigenen Verhaltensdaten. Kurz, ehrlich, konkret."},
                    {"role": "user", "content": prompt},
                ],
                "stream": False,
            },
            timeout=90,
        )

        if not result:
            logger.warning("Introspection: kein API-Ergebnis")
            return None

        reply = result.get("message", {}).get("content", "").strip()

        if "INTROSPECTION_OK" in reply:
            logger.info("Introspection: Schnubot sieht keine Auffälligkeiten")
            return None

        if len(reply) < 15:
            logger.info(f"Introspection: Antwort zu kurz ({len(reply)} Zeichen), verworfen")
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
            tags=["introspection", "mirror", "autonom"],
        )
        store_chunk(chunk)
        logger.info(f"Introspection gespeichert: {chunk['id'][:8]} | {reply[:80]}")
        return chunk["id"]

    except Exception as e:
        logger.error(f"Introspection fehlgeschlagen: {e}")
        return None
