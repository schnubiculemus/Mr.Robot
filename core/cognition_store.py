"""
core/cognition_store.py — Raw Cognition Store (WP9 Hygiene)

Drei Ebenen:
  A. Raw Cognition → SQLite (cognition_entries) — vorläufig, flüchtig, intern
  B. Promoted Cognition → Chroma/memory_active (self_reflection, proposed_pattern) — verdichtet
  C. Operative Folge → wp10_proposals — formaler Vorschlag

Prinzip: memory_active ist verdichtetes Gedächtnis, nicht Kimis ungebremstes inneres Tagebuch.

Status-Modell für cognition_entries:
  raw       — frisch gespeichert, noch nicht bewertet
  promoted  — in Chroma / WP10 aufgestiegen
  discarded — verworfen
"""

import logging
import uuid
from core.database import get_connection
from core.datetime_utils import to_iso

logger = logging.getLogger(__name__)

# =============================================================================
# Limits (Phase 1: Blutung stoppen)
# =============================================================================

DAILY_LIMITS = {
    "cognition_note":  40,   # max rohe Denkformen pro Tag gesamt
    "proposal_seed":   8,    # max proposal_seeds pro Tag
}

PER_RUN_LIMITS = {
    "light":            2,
    "medium":           3,
    "deep":             3,
    "post_interaction": 2,
}

SIMILARITY_THRESHOLD = 0.85  # Textsimilarität für Dedupe (einfach, Zeichenebene)


# =============================================================================
# DB-Tabelle
# =============================================================================

def init_cognition_entries_table(conn=None) -> None:
    """Erstellt cognition_entries Tabelle. Idempotent."""
    _own = conn is None
    if _own:
        conn = get_connection()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS cognition_entries (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            text TEXT NOT NULL,
            confidence REAL DEFAULT 0.7,
            reflection_level TEXT NOT NULL,
            related_line TEXT DEFAULT '',
            proposal_candidate INTEGER DEFAULT 0,
            source_context TEXT DEFAULT '',
            status TEXT NOT NULL DEFAULT 'raw',
            novelty_score REAL,
            promoted_chunk_id TEXT DEFAULT '',
            promoted_target TEXT DEFAULT '',
            run_id TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            promoted_at TEXT DEFAULT ''
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_cognition_entries_user_ts "
        "ON cognition_entries(user_id, created_at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_cognition_entries_status "
        "ON cognition_entries(status, user_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_cognition_entries_kind "
        "ON cognition_entries(kind, user_id, created_at DESC)"
    )
    if _own:
        conn.commit()
        conn.close()


# =============================================================================
# Limits prüfen
# =============================================================================

def daily_cognition_count(user_id: str, kind: str = None) -> int:
    """Zählt gespeicherte Einträge der letzten 24h (UTC-Cutoff statt Datums-LIKE)."""
    try:
        from datetime import datetime, timezone, timedelta
        # UTC-basiertes 24h-Fenster statt Datumsgrenze — keine Berlin/UTC-Schieflagen
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        conn = get_connection()
        try:
            if kind:
                row = conn.execute(
                    "SELECT COUNT(*) FROM cognition_entries "
                    "WHERE user_id=? AND kind=? AND created_at > ?",
                    (user_id, kind, cutoff)
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT COUNT(*) FROM cognition_entries "
                    "WHERE user_id=? AND created_at > ?",
                    (user_id, cutoff)
                ).fetchone()
            return row[0] if row else 0
        finally:
            conn.close()
    except Exception as e:
        logger.debug(f"daily_cognition_count fehlgeschlagen: {e}")
        return 0


def _is_similar_to_recent(user_id: str, text: str, hours: int = 24) -> bool:
    """Einfache Textsimilarität gegen letzte N Stunden — verhindert Fast-Duplikate."""
    try:
        from datetime import datetime, timezone, timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        conn = get_connection()
        try:
            rows = conn.execute(
                "SELECT text FROM cognition_entries "
                "WHERE user_id=? AND created_at > ? "
                "ORDER BY created_at DESC LIMIT 50",
                (user_id, cutoff)
            ).fetchall()
        finally:
            conn.close()

        text_lower = text.lower()
        text_words = set(text_lower.split())
        for row in rows:
            existing = row[0].lower()
            existing_words = set(existing.split())
            if not existing_words:
                continue
            overlap = len(text_words & existing_words) / max(len(text_words), 1)
            if overlap >= SIMILARITY_THRESHOLD:
                return True
        return False
    except Exception as e:
        logger.debug(f"_is_similar_to_recent fehlgeschlagen: {e}")
        return False


# =============================================================================
# Speichern
# =============================================================================

def save_cognition_entries(
    forms: list[dict],
    user_id: str,
    reflection_level: str,
    source_context: str = "",
    run_id: str = "",
) -> int:
    """
    Speichert rohe Denkformen in SQLite (cognition_entries).
    NICHT in Chroma. Limits werden enforced.

    Returns: Anzahl tatsächlich gespeicherter Einträge.
    """
    if not forms:
        return 0

    # Self-bootstrapping
    try:
        init_cognition_entries_table()
    except Exception:
        pass

    # Per-Run Limit
    run_limit = PER_RUN_LIMITS.get(reflection_level, 3)

    # Tages-Limits prüfen
    total_today = daily_cognition_count(user_id)
    seeds_today = daily_cognition_count(user_id, kind="proposal_seed")

    stored = 0
    run_count = 0
    now = to_iso()

    try:
        conn = get_connection()
        try:
            for form in forms:
                if run_count >= run_limit:
                    logger.debug(f"save_cognition_entries: Run-Limit {run_limit} erreicht")
                    break

                kind = form.get("kind", "observation")
                text = form.get("text", "").strip()
                if not text:
                    continue

                # Tages-Limit prüfen
                if kind == "proposal_seed":
                    if seeds_today + stored >= DAILY_LIMITS["proposal_seed"]:
                        logger.debug("save_cognition_entries: proposal_seed Tageslimit erreicht")
                        continue
                else:
                    if total_today + stored >= DAILY_LIMITS["cognition_note"]:
                        logger.debug("save_cognition_entries: cognition_note Tageslimit erreicht")
                        break

                # Duplikat-Check
                if _is_similar_to_recent(user_id, text, hours=24):
                    logger.debug(f"save_cognition_entries: ähnlicher Eintrag vorhanden, übersprungen")
                    continue

                entry_id = f"cog_{uuid.uuid4().hex[:12]}"
                conn.execute(
                    """INSERT INTO cognition_entries
                       (id, user_id, kind, text, confidence, reflection_level,
                        related_line, proposal_candidate, source_context,
                        status, run_id, created_at)
                       VALUES (?,?,?,?,?,?,?,?,?,'raw',?,?)""",
                    (entry_id, user_id, kind, text,
                     float(form.get("confidence", 0.7)),
                     reflection_level,
                     form.get("related_line", ""),
                     1 if form.get("proposal_candidate") else 0,
                     source_context[:200] if source_context else "",
                     run_id, now)
                )
                stored += 1
                run_count += 1
                logger.info(f"Cognition [{kind}] [{reflection_level}] → SQLite: {text[:60]}...")

            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.error(f"save_cognition_entries fehlgeschlagen: {e}")

    return stored


# =============================================================================
# Lesen (für cognition_echo)
# =============================================================================

def list_recent_cognition_entries(
    user_id: str,
    limit: int = 10,
    hours: int = 72,
    status: str = "raw",
    kind: str = None,
) -> list[dict]:
    """
    Liest rohe Cognition-Einträge aus SQLite.
    Ersetzt den bisherigen Chroma-Abruf für cognition_echo.
    """
    try:
        init_cognition_entries_table()
    except Exception:
        pass

    try:
        from datetime import datetime, timezone, timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        conn = get_connection()
        try:
            where = ["user_id=?", "created_at > ?"]
            params = [user_id, cutoff]
            if status:
                where.append("status=?")
                params.append(status)
            if kind:
                where.append("kind=?")
                params.append(kind)
            params.append(limit)
            rows = conn.execute(
                f"SELECT * FROM cognition_entries WHERE {' AND '.join(where)} "
                f"ORDER BY created_at DESC LIMIT ?",
                params
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()
    except Exception as e:
        logger.debug(f"list_recent_cognition_entries fehlgeschlagen: {e}")
        return []


def find_similar_recent_entries(
    user_id: str, text: str, hours: int = 48
) -> list[dict]:
    """Findet ähnliche Einträge der letzten N Stunden."""
    recent = list_recent_cognition_entries(user_id, limit=50, hours=hours)
    text_words = set(text.lower().split())
    similar = []
    for e in recent:
        e_words = set(e["text"].lower().split())
        if not e_words:
            continue
        overlap = len(text_words & e_words) / max(len(text_words), 1)
        if overlap >= 0.6:
            similar.append(e)
    return similar


# =============================================================================
# Status-Updates (Promotion / Discard)
# =============================================================================

def mark_promoted(
    entry_id: str,
    promoted_chunk_id: str = "",
    promoted_target: str = "chroma",
) -> bool:
    """Markiert einen Eintrag als promoviert (→ Chroma oder WP10)."""
    try:
        conn = get_connection()
        try:
            conn.execute(
                """UPDATE cognition_entries
                   SET status='promoted', promoted_chunk_id=?,
                       promoted_target=?, promoted_at=?
                   WHERE id=?""",
                (promoted_chunk_id, promoted_target, to_iso(), entry_id)
            )
            conn.commit()
            return True
        finally:
            conn.close()
    except Exception as e:
        logger.debug(f"mark_promoted fehlgeschlagen: {e}")
        return False


def mark_discarded(entry_id: str) -> bool:
    """Verwirft einen Eintrag."""
    try:
        conn = get_connection()
        try:
            conn.execute(
                "UPDATE cognition_entries SET status='discarded' WHERE id=?",
                (entry_id,)
            )
            conn.commit()
            return True
        finally:
            conn.close()
    except Exception as e:
        logger.debug(f"mark_discarded fehlgeschlagen: {e}")
        return False


# =============================================================================
# Phase 3: Promotion nach Chroma (self_reflection)
# =============================================================================

def promote_to_chroma(entry: dict, user_id: str) -> str | None:
    """
    Promoviert einen cognition_entry nach Chroma als self_reflection.
    Nur bei hinreichender Confidence und semantischer Neuheit.

    Returns: chunk_id wenn erfolgreich, None sonst.
    """
    # Mindest-Confidence für Promotion
    if float(entry.get("confidence", 0)) < 0.75:
        logger.debug(f"promote_to_chroma: confidence zu niedrig ({entry.get('confidence')})")
        return None

    # Nur insights und self_corrections sind promotionswürdig
    if entry.get("kind") not in ("insight", "self_correction"):
        return None

    try:
        from memory.memory_store import store_chunk, query_active
        from memory.chunk_schema import create_chunk

        # Semantischer Duplikat-Check in Chroma
        existing = query_active(
            entry["text"],
            n_results=3,
            where_filter={"chunk_type": "self_reflection"}
        )
        for e in existing:
            if e.get("_semantic_similarity", 0) > 0.92:
                logger.debug("promote_to_chroma: semantisches Duplikat in Chroma")
                return None

        chunk = create_chunk(
            text=entry["text"],
            chunk_type="self_reflection",
            source="cognition:promoted",
            confidence=float(entry.get("confidence", 0.75)),
            epistemic_status="stated",
            tags=["cognition:promoted", f"level:{entry.get('reflection_level', '')}"],
        )
        store_chunk(chunk)
        mark_promoted(entry["id"], promoted_chunk_id=chunk["id"], promoted_target="chroma")
        logger.info(f"Promoted → Chroma self_reflection: {entry['text'][:60]}")
        return chunk["id"]
    except Exception as e:
        logger.warning(f"promote_to_chroma fehlgeschlagen: {e}")
        return None


def promote_to_wp10(entry: dict, user_id: str) -> bool:
    """
    Promoviert einen proposal_seed nach WP10.
    Nur wenn konkret genug und proposal_candidate=True.
    """
    if not entry.get("proposal_candidate"):
        return False
    if entry.get("kind") != "proposal_seed":
        return False

    try:
        from core.proposal_service_wp10 import create_from_seed
        proposal = create_from_seed(
            seed_chunk={
                "id": entry["id"],
                "text": entry["text"],
                "related_line": entry.get("related_line", ""),
            },
            owner_id=user_id,
            proposal_type="other",
        )
        if proposal:
            mark_promoted(entry["id"], promoted_target="wp10")
            logger.info(f"Promoted → WP10: {entry['text'][:60]}")
            return True
    except Exception as e:
        logger.warning(f"promote_to_wp10 fehlgeschlagen: {e}")
    return False
