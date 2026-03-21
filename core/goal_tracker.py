"""
goal_tracker.py — Kimis aktive Zielverfolgung

Goals leben in SQLite (kimi_goals) — nicht mehr in ChromaDB.
Dieser Tracker prüft regelmäßig:
  - Welche aktiven Goals gibt es?
  - Welche haben noch kein offenes Todo?
  - Was ist der nächste konkrete Schritt?

Wird aufgerufen aus autonomous_reflection.py nach jedem Reflexions-Run.
"""

import logging
from datetime import timedelta

logger = logging.getLogger(__name__)

STALE_DAYS = 3
MAX_GOALS_PER_RUN = 2


def _get_kimi_goals(user_id: str) -> list[dict]:
    """Lädt alle aktiven Goals aus SQLite via goal_service."""
    try:
        from core.goal_service import get_active_goals
        return get_active_goals(user_id)
    except Exception as e:
        logger.debug(f"goal_tracker: Goals laden fehlgeschlagen: {e}")
        return []


def _get_todos_for_goal(goal_id: int, user_id: str) -> list[dict]:
    """Prüft ob offene Todos für dieses Goal existieren."""
    try:
        from core.database import get_connection
        conn = get_connection()
        try:
            rows = conn.execute(
                "SELECT * FROM todos WHERE goal_id=? AND status NOT IN ('done','cancelled') AND (user_id=? OR user_id IS NULL)",
                (goal_id, user_id)
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()
    except Exception as e:
        logger.debug(f"goal_tracker: Todo-Check fehlgeschlagen: {e}")
        return []


def _has_recent_workspace_progress(goal_title: str) -> bool:
    """Prüft ob im Workspace eine Datei zum Ziel existiert."""
    try:
        from core.code_exec import _list_files_raw
        files = _list_files_raw()
        goal_words = [w.lower() for w in goal_title.split() if len(w) > 4]
        return any(any(w in f.lower() for w in goal_words) for f in files)
    except Exception:
        return False


def run_goal_tracker(user_id: str) -> int:
    """
    Hauptfunktion: Prüft Kimis Goals und handelt.
    Returns: Anzahl reaktivierter Goals.
    """
    goals = _get_kimi_goals(user_id)
    if not goals:
        logger.debug("goal_tracker: Keine aktiven Goals gefunden")
        return 0

    logger.info(f"goal_tracker: {len(goals)} aktives Goal(e) gefunden")

    try:
        from core.todo_service import create_todo
        from core.datetime_utils import now_berlin
        import re
    except Exception as e:
        logger.warning(f"goal_tracker: Import fehlgeschlagen: {e}")
        return 0

    activated = 0
    tomorrow = (now_berlin() + timedelta(days=1)).strftime("%Y-%m-%d")

    for goal in goals:
        if activated >= MAX_GOALS_PER_RUN:
            break

        goal_id = goal["id"]
        goal_title = goal.get("title", "")

        # Schon offene Todos für dieses Goal?
        existing = _get_todos_for_goal(goal_id, user_id)
        if existing:
            logger.debug(f"goal_tracker: Goal #{goal_id} hat bereits {len(existing)} Todo(s)")
            continue

        # Workspace-Fortschritt prüfen
        has_progress = _has_recent_workspace_progress(goal_title)

        code_keywords = ["skript", "script", "detektor", "detector", "bauen", "code",
                        "analyse", "werkzeug", "tool", "implementier"]
        is_code_goal = any(w in goal_title.lower() for w in code_keywords)

        if is_code_goal and not has_progress:
            try:
                from core.code_exec import save_file
                slug = re.sub(r"[^a-z0-9]", "_", goal_title.lower()[:40]).strip("_")
                ts = now_berlin().strftime("%Y%m%d_%H%M")
                filename = f"goal_{ts}_{slug[:20]}.py"
                stub = f"# Ziel: {goal_title[:200]}\n# Angelegt: {now_berlin().strftime('%Y-%m-%d %H:%M')}\n\n# TODO: implementieren\n"
                save_file(filename, stub)
                logger.info(f"goal_tracker: Stub angelegt: {filename}")
            except Exception as _se:
                logger.debug(f"goal_tracker: Stub fehlgeschlagen: {_se}")

        # Todo via todo_service mit goal_id Verknüpfung
        try:
            if is_code_goal:
                title = f"{'Weitermachen' if has_progress else 'Anfangen'}: {goal_title[:50]}"
            else:
                title = f"Nächster Schritt: {goal_title[:50]}"

            todo = create_todo(
                owner_id=user_id,
                title=title,
                description=f"Aus Zielverfolgung — Goal #{goal_id}: {goal_title[:200]}",
                priority="mittel",
                project="kimi",
                due_date=tomorrow,
                origin_type="goal",
                origin_ref=str(goal_id),
                goal_id=goal_id,
            )
            if todo:
                logger.info(f"goal_tracker: Todo #{todo['id']} für Goal #{goal_id}: '{title[:50]}'")
                activated += 1

                # ORBIT-Trigger für Code-Goals
                if is_code_goal:
                    try:
                        import orbit as _orbit
                        _orbit.create_trigger(
                            trigger_type="cognition_output",
                            source="goal_tracker",
                            payload={
                                "user_id": user_id,
                                "source": "goal_tracker",
                                "topic_core": f"Kimi hat ein Code-Ziel: {goal_title[:60]}",
                                "relevance": "weak",
                            },
                        )
                    except Exception as _ot:
                        logger.debug(f"goal_tracker: ORBIT-Trigger fehlgeschlagen: {_ot}")

        except Exception as _te:
            logger.debug(f"goal_tracker: Todo-Anlage fehlgeschlagen: {_te}")

    logger.info(f"goal_tracker: {activated} Goal(e) reaktiviert")
    return activated
