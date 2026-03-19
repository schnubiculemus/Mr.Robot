"""
SchnuBot.ai — Kognitions-Feedback-Schleife (cognition_feedback.py)

Kimi prueft ihre eigenen frueheren Reflexionen auf Bewahrheitung.

Ablauf:
1. Holt self_reflection Chunks die 14-60 Tage alt sind (noch kein feedback-done Tag)
2. Holt Konversationen + neue Chunks der letzten 2 Wochen als Kontext
3. Kimi bewertet: bewahrheitet | widerlegt | offen
4. Schreibt Feedback-Chunk mit replies_to auf den alten
5. Markiert alten Chunk mit Tag 'feedback-done'

Ergebnis:
- BEWAHRHEITET: neuer Chunk epistemic_status=confirmed, confidence +0.1
- WIDERLEGT:    neuer Chunk epistemic_status=outdated, alter Chunk archiviert
- OFFEN:        neuer Chunk epistemic_status=speculative, bleibt im Pool

Laeuft in orbit_cognition.py nach autonomer Reflexion.
Cooldown: MIN_INTERVAL_DAYS zwischen zwei Feedback-Laeufen.
"""

import logging
from datetime import datetime, timezone, timedelta

from config import BOT_NAME
from memory.memory_store import store_chunk
from memory.chunk_schema import create_chunk

logger = logging.getLogger(__name__)

MIN_INTERVAL_DAYS = 7
MIN_CHUNK_AGE_DAYS = 14
MAX_CHUNK_AGE_DAYS = 60
MAX_CHUNKS_PER_RUN = 3


def _get_feedback_candidates() -> list:
    """Holt self_reflection Chunks die alt genug sind und noch kein Feedback haben."""
    try:
        from memory.memory_store import get_active_collection
        col = get_active_collection()
        result = col.get(
            where={"$and": [
                {"source": "robot"},
                {"chunk_type": "self_reflection"},
                {"status": "active"},
            ]},
            include=["documents", "metadatas"],
        )
        if not result["ids"]:
            return []

        now = datetime.now(timezone.utc)
        candidates = []

        for i, chunk_id in enumerate(result["ids"]):
            meta = result["metadatas"][i]
            text = result["documents"][i]

            tags_raw = meta.get("tags", "")
            if isinstance(tags_raw, str):
                tags = [t.strip() for t in tags_raw.split(",") if t.strip()]
            else:
                tags = list(tags_raw) if tags_raw else []

            if "feedback-done" in tags:
                continue

            created_str = meta.get("created_at", "")
            if not created_str:
                continue

            try:
                from core.datetime_utils import safe_parse_dt
                created_dt = safe_parse_dt(created_str)
                if not created_dt:
                    continue
                age_days = (now - created_dt).days
            except Exception:
                continue

            if age_days < MIN_CHUNK_AGE_DAYS or age_days > MAX_CHUNK_AGE_DAYS:
                continue

            candidates.append({
                "id": chunk_id,
                "text": text,
                "created_at": created_str,
                "age_days": age_days,
                "tags": tags,
                "confidence": float(meta.get("confidence", 0.5)),
                "epistemic_status": meta.get("epistemic_status", "inferred"),
                "replies_to": meta.get("replies_to", ""),
            })

        candidates.sort(key=lambda c: c["created_at"])
        return candidates[:MAX_CHUNKS_PER_RUN]

    except Exception as e:
        logger.warning(f"CognitionFeedback: Kandidaten laden fehlgeschlagen: {e}")
        return []


def _get_recent_context(user_id: str, days: int = 14) -> str:
    """Holt Kontext der letzten N Tage als kompakten Text."""
    lines = []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    try:
        from memory.memory_store import get_active_collection
        col = get_active_collection()
        result = col.get(
            where={"$and": [
                {"source": "robot"},
                {"chunk_type": "self_reflection"},
                {"status": "active"},
            ]},
            include=["documents", "metadatas"],
        )
        recent = []
        for i, cid in enumerate(result["ids"]):
            meta = result["metadatas"][i]
            if meta.get("created_at", "") > cutoff:
                recent.append(result["documents"][i][:150])
        if recent:
            lines.append("Meine neueren Reflexionen:")
            for r in recent[:5]:
                lines.append("- " + r)
    except Exception as e:
        logger.debug(f"CognitionFeedback: Reflexionen laden: {e}")

    try:
        from core.database import get_connection
        conn = get_connection()
        try:
            rows = conn.execute(
                "SELECT role, content FROM messages"
                " WHERE phone_number = ? AND timestamp > ?"
                " ORDER BY timestamp DESC LIMIT 20",
                (user_id, cutoff)
            ).fetchall()
        finally:
            conn.close()
        if rows:
            lines.append("\nLetzte Gespraeche (Auszug):")
            for row in reversed(rows[:10]):
                role = "Tommy" if row["role"] == "user" else "Kimi"
                lines.append(role + ": " + row["content"][:100])
    except Exception as e:
        logger.debug(f"CognitionFeedback: Turns laden: {e}")

    return "\n".join(lines) if lines else "(kein Kontext verfuegbar)"


FEEDBACK_PROMPT = (
    "Ich bin {bot_name} im Feedback-Modus. Ich ueberpruefe einen eigenen frueheren Gedanken.\n\n"
    "Damaliger Gedanke (vor {age_days} Tagen, {created}):\n"
    "---\n"
    "{thought}\n"
    "---\n"
    "Epistemic Status damals: {epistemic}\n"
    "Confidence damals: {confidence}\n\n"
    "Was seitdem passiert ist:\n"
    "{recent_context}\n\n"
    "## Aufgabe\n\n"
    "Hat sich dieser Gedanke bewahrheitet?\n\n"
    "Drei moegliche Bewertungen:\n"
    "- BEWAHRHEITET: der Gedanke hat sich als zutreffend erwiesen\n"
    "- WIDERLEGT: der Gedanke war falsch oder nicht haltbar\n"
    "- OFFEN: noch zu frueh zu sagen, braucht mehr Zeit\n\n"
    "Regeln:\n"
    "- Ich-Form. Konkret, ehrlich. 2-4 Saetze.\n"
    "- Nur eine Bewertung.\n"
    "- Wenn wirklich unklar: OFFEN.\n\n"
    "Format:\n"
    "BEWERTUNG: [BEWAHRHEITET|WIDERLEGT|OFFEN]\n"
    "FEEDBACK: [meine Einschaetzung]"
)


def _parse_feedback(reply: str) -> tuple:
    """Returns: (bewertung, feedback_text)"""
    bewertung = "OFFEN"
    feedback = ""
    for line in reply.strip().split("\n"):
        if line.startswith("BEWERTUNG:"):
            raw = line.replace("BEWERTUNG:", "").strip().upper()
            for valid in ("BEWAHRHEITET", "WIDERLEGT", "OFFEN"):
                if valid in raw:
                    bewertung = valid
                    break
        elif line.startswith("FEEDBACK:"):
            feedback = line.replace("FEEDBACK:", "").strip()

    if not feedback:
        in_feedback = False
        parts = []
        for line in reply.strip().split("\n"):
            if line.startswith("FEEDBACK:"):
                in_feedback = True
                first = line.replace("FEEDBACK:", "").strip()
                if first:
                    parts.append(first)
            elif in_feedback and not line.startswith("BEWERTUNG:"):
                parts.append(line)
        feedback = " ".join(parts).strip()

    return bewertung, feedback


def _mark_feedback_done(chunk_id: str) -> None:
    """Fuegt 'feedback-done' Tag zum Quell-Chunk hinzu."""
    try:
        from memory.memory_store import get_active_collection
        col = get_active_collection()
        result = col.get(ids=[chunk_id], include=["metadatas"])
        if not result["metadatas"]:
            return
        meta = result["metadatas"][0]
        tags_raw = meta.get("tags", "")
        if isinstance(tags_raw, str):
            tags = [t.strip() for t in tags_raw.split(",") if t.strip()]
        else:
            tags = list(tags_raw) if tags_raw else []
        if "feedback-done" not in tags:
            tags.append("feedback-done")
        col.update(ids=[chunk_id], metadatas=[{**meta, "tags": tags}])
    except Exception as e:
        logger.warning(f"CognitionFeedback: feedback-done setzen fehlgeschlagen: {e}")


def _archive_chunk(chunk_id: str) -> None:
    """Archiviert einen widerlegten Chunk."""
    try:
        from memory.memory_store import get_active_collection
        col = get_active_collection()
        result = col.get(ids=[chunk_id], include=["metadatas"])
        if result["metadatas"]:
            meta = result["metadatas"][0]
            col.update(ids=[chunk_id], metadatas=[{**meta, "status": "archived"}])
            logger.info(f"CognitionFeedback: Chunk archiviert (widerlegt): {chunk_id[:8]}")
    except Exception as e:
        logger.warning(f"CognitionFeedback: Archivierung fehlgeschlagen: {e}")


def _save_feedback(bewertung: str, feedback_text: str, source_chunk: dict, user_id: str):
    """Speichert Feedback-Chunk und markiert Quell-Chunk."""
    if not feedback_text or len(feedback_text) < 15:
        return None

    if bewertung == "BEWAHRHEITET":
        new_epistemic = "confirmed"
        new_confidence = min(source_chunk["confidence"] + 0.1, 0.95)
        tags = ["feedback", "feedback-bewahrheitet", "autonom"]
        chunk_text = "[Feedback: bewahrheitet] " + feedback_text
    elif bewertung == "WIDERLEGT":
        new_epistemic = "outdated"
        new_confidence = max(source_chunk["confidence"] - 0.15, 0.2)
        tags = ["feedback", "feedback-widerlegt", "autonom"]
        chunk_text = "[Feedback: widerlegt] " + feedback_text
    else:
        new_epistemic = "speculative"
        new_confidence = source_chunk["confidence"]
        tags = ["feedback", "feedback-offen", "autonom"]
        chunk_text = "[Feedback: offen] " + feedback_text

    try:
        chunk = create_chunk(
            text=chunk_text,
            chunk_type="self_reflection",
            source="robot",
            confidence=new_confidence,
            epistemic_status=new_epistemic,
            tags=tags,
            replies_to=source_chunk["id"],
        )
        store_chunk(chunk)
        logger.info(
            f"CognitionFeedback: {bewertung} gespeichert: {chunk['id'][:8]}"
            f" -> replies_to {source_chunk['id'][:8]} | {feedback_text[:80]}"
        )
        _mark_feedback_done(source_chunk["id"])
        if bewertung == "WIDERLEGT":
            _archive_chunk(source_chunk["id"])
        return chunk["id"]
    except Exception as e:
        logger.error(f"CognitionFeedback: Speichern fehlgeschlagen: {e}")
        return None


def run_cognition_feedback(user_id: str, last_run_iso: str = None) -> int:
    """
    Kimi ueberprueft fruehe Reflexionen auf Bewahrheitung.

    Args:
        user_id:      Tommy's WAHA LID
        last_run_iso: ISO-Timestamp des letzten Feedback-Laufs

    Returns:
        Anzahl verarbeiteter Chunks (0 = nichts zu tun)
    """
    if last_run_iso:
        try:
            from core.datetime_utils import safe_parse_dt
            last_dt = safe_parse_dt(last_run_iso)
            if last_dt:
                age_days = (datetime.now(timezone.utc) - last_dt).days
                if age_days < MIN_INTERVAL_DAYS:
                    logger.debug(f"CognitionFeedback: Cooldown ({age_days}d), skip")
                    return 0
        except Exception:
            pass

    candidates = _get_feedback_candidates()
    if not candidates:
        logger.info("CognitionFeedback: keine Kandidaten, skip")
        return 0

    logger.info(f"CognitionFeedback: {len(candidates)} Kandidaten gefunden")
    recent_context = _get_recent_context(user_id, days=14)
    processed = 0

    try:
        from core.ollama_client import chat_internal

        for candidate in candidates:
            prompt = FEEDBACK_PROMPT.format(
                bot_name=BOT_NAME,
                age_days=candidate["age_days"],
                created=candidate["created_at"][:10],
                thought=candidate["text"],
                epistemic=candidate["epistemic_status"],
                confidence=str(int(candidate["confidence"] * 100)) + "%",
                recent_context=recent_context,
            )

            try:
                reply, _ = chat_internal(
                    user_id=user_id,
                    message=prompt,
                    chat_history=[],
                    extra_system=(
                        "Feedback-Modus:\n"
                        "Ich bewerte einen eigenen frueheren Gedanken.\n"
                        "Kein Chat, keine Anrede. Format exakt: BEWERTUNG und FEEDBACK.\n"
                        "Ehrlich — auch wenn der Gedanke falsch war."
                    ),
                )
                if not reply or len(reply) < 15:
                    continue
                bewertung, feedback_text = _parse_feedback(reply)
                if not feedback_text:
                    continue
                chunk_id = _save_feedback(bewertung, feedback_text, candidate, user_id)
                if chunk_id:
                    processed += 1
                    logger.info(
                        f"CognitionFeedback: [{bewertung}] {candidate['id'][:8]}"
                        f" (vor {candidate['age_days']}d) -> {chunk_id[:8]}"
                    )
            except Exception as e:
                logger.warning(
                    f"CognitionFeedback: Verarbeitung fehlgeschlagen"
                    f" fuer {candidate['id'][:8]}: {e}"
                )
                continue

    except Exception as e:
        logger.error(f"CognitionFeedback: Hauptschleife fehlgeschlagen: {e}")

    logger.info(f"CognitionFeedback: {processed} Chunks verarbeitet")
    return processed
