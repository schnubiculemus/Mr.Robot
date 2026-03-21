"""
core/goal_service.py — Goals Service

Langfristige Ziele von Kimi. Einzige Stelle die Goals schreibt.
"""
import logging
from core.datetime_utils import to_iso
from core.database import get_connection, init_kimi_b_schema

logger = logging.getLogger(__name__)


def create_goal(owner_id: str, title: str, description: str = None,
                priority: str = "mittel", source_type: str = None,
                source_ref: str = None) -> dict | None:
    try:
        init_kimi_b_schema()
        now = to_iso()
        conn = get_connection()
        try:
            cur = conn.execute(
                """INSERT INTO kimi_goals
                   (owner_id, title, description, status, priority, progress,
                    source_type, source_ref, created_at, updated_at)
                   VALUES (?,?,?,'active',?,0,?,?,?,?)""",
                (owner_id, title[:200], description, priority,
                 source_type, source_ref, now, now)
            )
            conn.commit()
            goal_id = cur.lastrowid
            logger.info(f"Goal #{goal_id} erstellt: '{title[:50]}'")
            return get_goal(goal_id)
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"create_goal fehlgeschlagen: {e}")
        return None


def get_goal(goal_id: int) -> dict | None:
    try:
        conn = get_connection()
        try:
            row = conn.execute("SELECT * FROM kimi_goals WHERE id=?", (goal_id,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"get_goal fehlgeschlagen: {e}")
        return None


def get_active_goals(owner_id: str) -> list:
    try:
        conn = get_connection()
        try:
            rows = conn.execute(
                "SELECT * FROM kimi_goals WHERE owner_id=? AND status='active' ORDER BY priority DESC, created_at DESC",
                (owner_id,)
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"get_active_goals fehlgeschlagen: {e}")
        return []


def update_goal_progress(goal_id: int, progress: int, reason: str = None) -> bool:
    try:
        now = to_iso()
        progress = min(100, max(0, progress))
        conn = get_connection()
        try:
            conn.execute(
                "UPDATE kimi_goals SET progress=?, last_progress_at=?, updated_at=? WHERE id=?",
                (progress, now, now, goal_id)
            )
            conn.commit()
            logger.info(f"Goal #{goal_id} Fortschritt: {progress}%")

            # Automatisch completed wenn 100%
            if progress >= 100:
                conn.execute(
                    "UPDATE kimi_goals SET status='completed', completed_at=?, updated_at=? WHERE id=? AND status='active'",
                    (now, now, goal_id)
                )
                conn.execute(
                    """INSERT INTO kimi_completions
                       (owner_id, for_object_type, for_object_id, reason, summary, created_at)
                       VALUES ('',?,?,?,?,?)""",
                    ("goal", str(goal_id), "progress_100", reason or "100% erreicht", now)
                )
                conn.commit()
                logger.info(f"Goal #{goal_id} automatisch abgeschlossen (100%)")
            return True
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"update_goal_progress fehlgeschlagen: {e}")
        return False


def complete_goal(goal_id: int, summary: str = None) -> bool:
    try:
        now = to_iso()
        conn = get_connection()
        try:
            conn.execute(
                "UPDATE kimi_goals SET status='completed', progress=100, completed_at=?, updated_at=? WHERE id=?",
                (now, now, goal_id)
            )
            conn.commit()
            _record_completion(conn, "goal", str(goal_id), summary=summary)
            conn.commit()
            logger.info(f"Goal #{goal_id} abgeschlossen")
            return True
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"complete_goal fehlgeschlagen: {e}")
        return False


def _record_completion(conn, obj_type: str, obj_id: str, owner_id: str = "",
                       reason: str = None, summary: str = None):
    conn.execute(
        """INSERT INTO kimi_completions
           (owner_id, for_object_type, for_object_id, reason, summary, created_at)
           VALUES (?,?,?,?,?,?)""",
        (owner_id, obj_type, obj_id, reason, summary, to_iso())
    )
