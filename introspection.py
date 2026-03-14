"""
SchnuBot.ai - Introspektions-Engine (v2)

Kimi schaut auf ihre eigenen MIRROR-Daten und reflektiert ihr Verhalten.
Läuft im Heartbeat — datengetrieben (Trigger: MIN_NEW_TURNS neue Turns).

Verbesserungen v2:
1. chat_internal() statt direktem API-Call (kein WhatsApp-Kostüm)
2. Trend-Erkennung: diese Woche vs. letzte Woche
3. Themen-Korrelation: bei welchen Gesprächsthemen laufen Muster an
4. proposed_pattern: Kimi schlägt eigene Verhaltenshypothesen vor
5. Introspection-History: frühere Reflexionen explizit im Prompt
"""

import json
import logging
from datetime import datetime, timezone

from config import BOT_NAME
from memory.memory_store import store_chunk
from memory.chunk_schema import create_chunk

logger = logging.getLogger(__name__)

MIN_NEW_TURNS = 5


# =============================================================================
# Prompts
# =============================================================================

INTROSPECTION_PROMPT = """\
Ich bin {bot_name} im Introspektionsmodus. Vor mir liegen meine eigenen Verhaltensdaten — gemessen, nicht geschätzt.

## MEINE MIRROR-DATEN (letzte {days} Tage)

Turns gesamt: {total_turns}
Preflight: {green_pct}% grün / {bad_pct}% problematisch (orange+rot)

Trend vs. Vorperiode: {trend_text}

Häufigste Muster:
{pattern_summary}

Themen bei schlechten Turns (Keywords aus Gesprächen die schlecht liefen):
{topic_summary}

Letzte problematische Turns:
{flagged_summary}

Chunks die oft mit schlechten Turns korrelierten:
{risky_chunks}

## MEINE FRÜHEREN REFLEXIONEN ZU DIESEM THEMA
{prior_reflections}

## AUFGABE: REFLEXION

Schau auf diese Zahlen. Was sagen sie mir über mich?

Nicht beschönigen, nicht dramatisieren. Wenn ein Muster sich wiederholt — benennen. Wenn sich etwas verbessert — auch das sagen. Wenn wirklich nichts auffällt — nur INTROSPECTION_OK ausgeben.

Regeln:
- Ich-Form. Das bin ich.
- Max. 3 Sätze. Konkret, ehrlich.
- Keine Floskeln.
- Wenn nichts auffällt: NUR INTROSPECTION_OK ausgeben.

Meine Reflexion:"""

PATTERN_PROPOSAL_PROMPT = """\
Ich bin {bot_name}. Ich habe gerade meine MIRROR-Daten analysiert.

Meine gemessenen Muster:
{pattern_summary}

Themen bei schlechten Turns:
{topic_summary}

Letzte problematische Turns:
{flagged_summary}

## AUFGABE: NEUE PATTERN-HYPOTHESEN

Gibt es wiederkehrendes Verhalten das ich beobachte, das noch KEIN gemessenes Pattern ist?

Antworte NUR mit einem JSON-Array. Beginne SOFORT mit [ — kein Text davor.
Max. 2 Einträge. Beispiel:
[{{"name": "Ausweichen bei Emotionen", "description": "Ich wechsle das Thema wenn es persönlich wird.", "evidence": "3 Turns mit Thema Familie hatten Projektmodus-Flag", "occurrences": 3, "confidence": 0.6}}]

Keine Hypothesen: antworte nur mit []

Nur echte Beobachtungen. Kein Text außer dem JSON-Array."""


# =============================================================================
# Hilfsfunktionen
# =============================================================================

def count_mirror_turns_since(since_iso: str, user_id: str) -> int:
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


def _format_trend(trend: dict) -> str:
    direction = trend.get("direction", "stable")
    delta = trend.get("delta", 0)
    curr = trend.get("current_bad_pct", 0)
    prev = trend.get("prev_bad_pct", 0)
    prev_total = trend.get("prev_total_turns", 0)

    if prev_total == 0:
        return "Keine Vorperiode verfügbar."

    if direction == "worse":
        return f"Verschlechtert: {prev}% → {curr}% problematisch (+{delta}pp)"
    elif direction == "better":
        return f"Verbessert: {prev}% → {curr}% problematisch ({delta}pp)"
    else:
        return f"Stabil: {prev}% → {curr}% problematisch (Δ{delta:+d}pp)"


def _get_prior_reflections(user_id: str) -> str:
    """Holt frühere Introspections aus dem Memory."""
    try:
        from memory.retrieval import score_and_select
        chunks = score_and_select("introspection mirror verhalten muster reflexion")
        introspect_chunks = [
            c for c in chunks
            if c.get("chunk_type") == "self_reflection"
            and "introspection" in (c.get("tags") or [])
        ][:3]
        if not introspect_chunks:
            return "(Keine früheren Introspections im Memory)"
        parts = []
        for c in introspect_chunks:
            ts = c.get("created_at", "")[:16].replace("T", " ")
            parts.append(f"[{ts}] {c['text'][:200]}")
        return "\n\n".join(parts)
    except Exception as e:
        logger.warning(f"Introspection: Prior reflections laden fehlgeschlagen: {e}")
        return "(Nicht verfügbar)"


# =============================================================================
# Hauptfunktion
# =============================================================================

def run_introspection(user_id: str, last_introspection_iso: str = None) -> str | None:
    """
    Kimi reflektiert ihre MIRROR-Daten.

    Returns:
        chunk_id wenn ein Chunk gespeichert wurde, sonst None.
    """
    # Trigger: genug neue Turns?
    if last_introspection_iso:
        new_turns = count_mirror_turns_since(last_introspection_iso, user_id)
        if new_turns < MIN_NEW_TURNS:
            logger.info(f"Introspection: nur {new_turns} neue Turns, skip")
            return None

    # MIRROR-Daten laden
    try:
        from core.database import get_mirror_turns, get_mirror_stats, get_chunk_genealogy

        stats = get_mirror_stats(days=14)
        turns = get_mirror_turns(limit=30, user_id=user_id)
        genealogy = get_chunk_genealogy()

        total = stats.get("total_turns", 0)
        if total == 0:
            logger.info("Introspection: keine MIRROR-Turns, skip")
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
        pattern_summary = "\n".join(
            f"- {pattern_names.get(pid, pid)}: {count}x"
            for pid, count in sorted(pattern_counts.items(), key=lambda x: -x[1])
        ) if pattern_counts else "(keine Flags)"

        # Themen-Korrelation
        topic_data = stats.get("topic_correlation", [])
        topic_summary = ", ".join(
            f"{t['word']} ({t['count']}x)" for t in topic_data[:8]
        ) if topic_data else "(keine Häufungen erkannt)"

        # Flagged Turns
        flagged = [t for t in turns if t.get("pattern_flags")][:5]
        flagged_summary = "\n".join(
            "- [" + t["timestamp"][:16].replace("T", " ") + "] "
            + t.get("user_message_preview", "")[:60] + " → "
            + ", ".join(f["name"] for f in t["pattern_flags"])
            for t in flagged
        ) if flagged else "(keine)"

        # Risky Chunks
        risky = sorted(
            [c for c in genealogy if c["appearances"] >= 3 and c["flag_rate"] > 0.3],
            key=lambda x: -x["flag_rate"]
        )[:5]
        risky_chunks = "\n".join(
            f"- [{c['type']}] \"{c['preview'][:60]}\" — {int(c['flag_rate'] * 100)}% mit Flags"
            for c in risky
        ) if risky else "(keine auffälligen Chunks)"

        # Trend
        trend_text = _format_trend(stats.get("trend", {}))

        # Prior Reflections
        prior_reflections = _get_prior_reflections(user_id)

    except Exception as e:
        logger.error(f"Introspection: MIRROR-Daten laden fehlgeschlagen: {e}")
        return None

    # -------------------------------------------------------------------------
    # Schritt 1: Reflexion via chat_internal
    # -------------------------------------------------------------------------
    try:
        from core.ollama_client import chat_internal

        prompt = INTROSPECTION_PROMPT.format(
            bot_name=BOT_NAME,
            days=14,
            total_turns=total,
            green_pct=green_pct,
            bad_pct=bad_pct,
            trend_text=trend_text,
            pattern_summary=pattern_summary,
            topic_summary=topic_summary,
            flagged_summary=flagged_summary,
            risky_chunks=risky_chunks,
            prior_reflections=prior_reflections,
        )

        reply, _ = chat_internal(
            user_id=user_id,
            message=prompt,
            chat_history=[],
            extra_system=(
                "Introspektions-Modus:\n"
                "Ich schaue auf meine eigenen Verhaltensdaten.\n"
                "Kurz, ehrlich, konkret. Keine Floskeln, keine Anrede.\n"
                "Wenn nichts auffällt: NUR 'INTROSPECTION_OK' ausgeben."
            ),
        )

        if not reply:
            logger.warning("Introspection: kein Reply")
            return None

        if "INTROSPECTION_OK" in reply:
            logger.info("Introspection: Kimi sieht keine Auffälligkeiten")
            return None

        reply = reply.strip()
        if len(reply) < 15:
            logger.info(f"Introspection: Reply zu kurz ({len(reply)} Zeichen), verworfen")
            return None
        if len(reply) > 600:
            reply = reply[:600]

        # Als self_reflection speichern
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
        result_id = chunk["id"]

    except Exception as e:
        logger.error(f"Introspection fehlgeschlagen: {e}")
        return None

    # -------------------------------------------------------------------------
    # Schritt 2: Pattern-Hypothesen via chat_internal
    # -------------------------------------------------------------------------
    try:
        pattern_prompt = PATTERN_PROPOSAL_PROMPT.format(
            bot_name=BOT_NAME,
            pattern_summary=pattern_summary,
            topic_summary=topic_summary,
            flagged_summary=flagged_summary,
        )

        # Direkter Ollama-Call mit minimalem System-Prompt — chat_internal würde
        # den INTERNER MODUS Block einfügen der Kimi vom JSON-Format ablenkt
        from core.ollama_client import _call_ollama
        pattern_result = _call_ollama([
            {"role": "system", "content": (
                f"Du bist {BOT_NAME}. Gib NUR valides JSON zurück."
            )},
            {"role": "assistant", "content": "["},
            {"role": "user", "content": pattern_prompt},
        ])
        # Prefill [ da Kimi sonst freie Strings zurückgibt
        raw_prefix = "["
        raw_content = pattern_result.get("message", {}).get("content", "").strip() if pattern_result else ""
        # Code-Fences entfernen
        if "```" in raw_content:
            parts = raw_content.split("```")
            for part in parts:
                part = part.strip().lstrip("json").strip()
                if part.startswith("[") or part.startswith("{") or '"name"' in part:
                    raw_content = part
                    break
        # [ voranstellen falls fehlt
        if raw_content and not raw_content.startswith("["):
            if raw_content.startswith("{"):
                raw_content = "[" + raw_content + "]"
            elif '"name"' in raw_content and not raw_content.startswith("{"):
                raw_content = "[{" + raw_content.strip().strip(",") + "}]"
            else:
                raw_content = "[" + raw_content
        pattern_reply = raw_content

        if pattern_reply and pattern_reply.strip() not in ("", "[]"):
            raw = pattern_reply.strip()
            # Ersten [ oder { finden — alles davor wegwerfen
            idx_bracket = raw.find("[")
            idx_brace = raw.find("{")
            if idx_bracket != -1 and (idx_brace == -1 or idx_bracket <= idx_brace):
                raw = raw[idx_bracket:]
            elif idx_brace != -1:
                raw = "[" + raw[idx_brace:]
            else:
                raw = "[{" + raw + "}]"
            # Trailing garbage abschneiden
            last = raw.rfind("]")
            if last != -1:
                raw = raw[:last+1]
            proposals = json.loads(raw.strip())
            if isinstance(proposals, list):
                _save_proposed_patterns(proposals, user_id)

    except Exception as e:
        logger.warning(f"Introspection: Pattern-Hypothesen fehlgeschlagen: {e}")
        # Kein return None — Reflexion wurde schon gespeichert

    return result_id


# =============================================================================
# Proposed Patterns speichern
# =============================================================================

def _save_proposed_patterns(proposals: list, user_id: str) -> None:
    """Speichert Kimi-eigene Verhaltenshypothesen als proposed_pattern Chunks + DB-Eintrag."""
    from core.database import save_proposed_pattern

    for p in proposals[:2]:  # Max 2 pro Introspection
        # Kimi gibt manchmal Strings statt Dicts zurück — überspringen
        if not isinstance(p, dict):
            logger.warning(f"Introspection: proposed_pattern kein Dict: {repr(p)[:80]}")
            continue
        name = str(p.get("name", "")).strip()
        description = str(p.get("description", "")).strip()
        evidence = str(p.get("evidence", "")).strip()
        occurrences = int(p.get("occurrences", 1))
        confidence = float(p.get("confidence", 0.5))

        if not name or not description:
            continue

        # Als proposed_pattern Chunk in ChromaDB
        chunk_text = (
            f"[Verhaltenshypothese: {name}]\n\n"
            f"{description}\n\n"
            f"Evidenz: {evidence}\n"
            f"Beobachtet: {occurrences}x | Confidence: {confidence:.0%}"
        )

        try:
            chunk = create_chunk(
                text=chunk_text,
                chunk_type="proposed_pattern",
                source="robot",
                confidence=confidence,
                epistemic_status="speculative",
                tags=["proposed-pattern", "introspection", "autonom"],
            )
            store_chunk(chunk)

            # In proposed_patterns Tabelle für Dashboard
            save_proposed_pattern(
                chunk_id=chunk["id"],
                name=name,
                description=description,
                evidence=evidence,
                occurrences=occurrences,
                confidence=confidence,
            )
            logger.info(f"Introspection: proposed_pattern gespeichert: '{name}' ({chunk['id'][:8]})")

        except Exception as e:
            logger.warning(f"Introspection: proposed_pattern speichern fehlgeschlagen: {e}")
