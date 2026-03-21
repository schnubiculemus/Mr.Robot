"""
core/todo_service.py — Todos Service

Einzige Stelle die Todos mit vollständiger Verknüpfung anlegt.
Ergänzt core/todos.py — ersetzt es nicht sofort (Rückwärtskompatibilität).
"""
import logging
from core.datetime_utils import to_iso
from core.database import get_connection

logger = logging.getLogger(__name__)


def create_todo(owner_id: str, title: str, description: str = None,
                priority: str = "keine", project: str = None,
                due_date: str = None, origin_type: str = None,
                origin_ref: str = None, proposal_id: int = None,
                goal_id: int = None) -> dict | None:
    """
    Legt ein Todo an mit vollständigen Verknüpfungsfeldern.
    """
    try:
        from core.todos import create_todo as _legacy_create
        # Legacy-Funktion für Basis-Felder
        todo = _legacy_create(
            user_id=owner_id,
            title=title,
            description=description,
            priority=priority,
            project=project,
            due_date=due_date,
        )
        if not todo:
            return None

        # Verknüpfungsfelder nachträglich setzen
        now = to_iso()
        conn = get_connection()
        try:
            conn.execute(
                """UPDATE todos SET
                   origin_type=?, origin_ref=?, proposal_id=?, goal_id=?, status_updated_at=?
                   WHERE id=?""",
                (origin_type, origin_ref, proposal_id, goal_id, now, todo["id"])
            )
            conn.commit()
        finally:
            conn.close()

        # Frisches Todo mit allen Feldern zurückgeben
        from core.todos import get_todo
        return get_todo(todo["id"])

    except Exception as e:
        logger.warning(f"todo_service.create_todo fehlgeschlagen: {e}")
        return None


def start_todo(todo_id: int, task_id: str) -> bool:
    """Setzt Todo auf in_progress und verknüpft den Task."""
    try:
        now = to_iso()
        conn = get_connection()
        try:
            conn.execute(
                "UPDATE todos SET status='in_progress', linked_task_id=?, status_updated_at=? WHERE id=?",
                (task_id, now, todo_id)
            )
            conn.commit()
            logger.info(f"Todo #{todo_id} → in_progress (Task {task_id[:8]})")
            return True
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"start_todo fehlgeschlagen: {e}")
        return False


def complete_todo(todo_id: int, summary: str = None) -> bool:
    """
    Schließt Todo ab und erzeugt Completion-Event.
    Wenn Todo aus Proposal stammt → Proposal auf implemented.
    """
    try:
        now = to_iso()
        conn = get_connection()
        try:
            row = conn.execute("SELECT * FROM todos WHERE id=?", (todo_id,)).fetchone()
            if not row:
                return False
            todo = dict(row)

            conn.execute(
                "UPDATE todos SET status='done', completed_at=?, status_updated_at=? WHERE id=?",
                (now, now, todo_id)
            )

            # Completion-Event
            conn.execute(
                """INSERT INTO kimi_completions
                   (owner_id, for_object_type, for_object_id, reason, summary, created_at)
                   VALUES (?,?,?,?,?,?)""",
                (todo.get("user_id",""), "todo", str(todo_id), "done", summary or todo.get("title",""), now)
            )
            conn.commit()

            # Proposal auf implemented wenn verknüpft
            if todo.get("proposal_id"):
                from core.proposal_service import mark_implemented
                mark_implemented(todo["proposal_id"])

            # Goal-Fortschritt
            if todo.get("goal_id"):
                from core.goal_service import update_goal_progress, get_goal
                goal = get_goal(todo["goal_id"])
                if goal:
                    new_progress = min(100, goal.get("progress", 0) + 10)
                    update_goal_progress(todo["goal_id"], new_progress, f"Todo #{todo_id} erledigt")

            logger.info(f"Todo #{todo_id} abgeschlossen")
            return True
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"complete_todo fehlgeschlagen: {e}")
        return False


def block_todo(todo_id: int, reason: str) -> bool:
    """Setzt Todo auf blocked."""
    try:
        now = to_iso()
        conn = get_connection()
        try:
            conn.execute(
                "UPDATE todos SET status='blocked', status_updated_at=? WHERE id=?",
                (now, todo_id)
            )
            conn.commit()
            return True
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"block_todo fehlgeschlagen: {e}")
        return False


def record_observation(owner_id: str, content: str, obs_type: str = "tool_result",
                       goal_id: int = None, proposal_id: int = None,
                       todo_id: int = None, task_id: str = None,
                       step_id: str = None, payload: dict = None) -> int | None:
    """Speichert eine Observation — Ergebnis eines Steps oder einer Wahrnehmung."""
    try:
        import json
        conn = get_connection()
        try:
            cur = conn.execute(
                """INSERT INTO kimi_observations
                   (owner_id, goal_id, proposal_id, todo_id, task_id, step_id,
                    type, content, payload_json, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (owner_id, goal_id, proposal_id, todo_id, task_id, step_id,
                 obs_type, content[:1000],
                 json.dumps(payload) if payload else None,
                 to_iso())
            )
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"record_observation fehlgeschlagen: {e}")
        return None
