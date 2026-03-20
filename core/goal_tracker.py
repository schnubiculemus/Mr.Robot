"""
goal_tracker.py — Kimis aktive Zielverfolgung

Kimi hat Ziele (kimi-ziel Chunks in ChromaDB).
Dieser Tracker prüft regelmäßig:
  - Welche Ziele gibt es?
  - Welche hatten lange keinen Fortschritt?
  - Was ist der nächste konkrete Schritt?

Wird aufgerufen aus autonomous_reflection.py nach jedem Reflexions-Run.
"""

import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# Wie viele Tage ohne Fortschritt bevor ein Ziel reaktiviert wird
STALE_DAYS = 3
# Max Ziele pro Run reaktivieren
MAX_GOALS_PER_RUN = 2


def _get_kimi_goals() -> list[dict]:
    """Lädt alle kimi-ziel Chunks aus ChromaDB."""
    try:
        from memory.memory_store import get_active_collection
        col = get_active_collection()
        results = col.get(
            where={"$and": [
                {"source": {"$eq": "robot"}},
                {"chunk_type": {"$eq": "decision"}},
            ]},
            include=["documents", "metadatas", "ids"],
        )
        goals = []
        for i, doc in enumerate(results.get("documents") or []):
            meta = (results.get("metadatas") or [{}])[i]
            tags = meta.get("tags", "")
            if isinstance(tags, str):
                tags = [t.strip() for t in tags.split(",") if t.strip()]
            if "kimi-ziel" in tags:
                goals.append({
                    "id": (results.get("ids") or [""])[i],
                    "text": doc,
                    "created_at": meta.get("created_at", ""),
                    "tags": tags,
                })
        return goals
    except Exception as e:
        logger.debug(f"goal_tracker: Ziele laden fehlgeschlagen: {e}")
        return []


def _get_kimi_todos_for_goal(goal_text: str, user_id: str) -> list[dict]:
    """Prüft ob es offene kimi-Todos gibt die zu diesem Ziel passen."""
    try:
        from core.todos import get_open_todos
        todos = get_open_todos(user_id)
        kimi_todos = [t for t in todos if (t.get("project") or "").lower() == "kimi"]
        goal_words = set(w.lower() for w in goal_text.split() if len(w) > 4)
        matching = []
        for t in kimi_todos:
            title_words = set(w.lower() for w in t["title"].split() if len(w) > 4)
            if goal_words & title_words:
                matching.append(t)
        return matching
    except Exception as e:
        logger.debug(f"goal_tracker: Todo-Check fehlgeschlagen: {e}")
        return []


def _has_recent_workspace_progress(goal_text: str) -> bool:
    """Prüft ob es im Workspace eine Datei gibt die zu diesem Ziel passt."""
    try:
        from core.code_exec import _list_files_raw
        files = _list_files_raw()
        goal_words = [w.lower() for w in goal_text.split() if len(w) > 4]
        for f in files:
            if any(w in f.lower() for w in goal_words):
                return True
        return False
    except Exception:
        return False


def run_goal_tracker(user_id: str) -> int:
    """
    Hauptfunktion: Prüft Kimis Ziele und handelt.
    Returns: Anzahl reaktivierter Ziele.
    """
    goals = _get_kimi_goals()
    if not goals:
        logger.debug("goal_tracker: Keine kimi-Ziele gefunden")
        return 0

    logger.info(f"goal_tracker: {len(goals)} Ziel(e) gefunden")

    try:
        from core.todos import create_todo, get_open_todos
        from core.datetime_utils import now_berlin
        from core.code_exec import save_file
        import re
    except Exception as e:
        logger.warning(f"goal_tracker: Import fehlgeschlagen: {e}")
        return 0

    activated = 0
    tomorrow = (now_berlin() + timedelta(days=1)).strftime("%Y-%m-%d")

    for goal in goals:
        if activated >= MAX_GOALS_PER_RUN:
            break

        goal_text = goal["text"]

        # Prüfen ob schon ein passendes offenes Todo existiert
        existing = _get_kimi_todos_for_goal(goal_text, user_id)
        if existing:
            logger.debug(f"goal_tracker: Ziel '{goal_text[:40]}' hat bereits {len(existing)} Todo(s)")
            continue

        # Prüfen ob Workspace-Fortschritt existiert
        has_progress = _has_recent_workspace_progress(goal_text)

        # Nächsten Schritt formulieren
        code_keywords = ["skript", "script", "detektor", "detector", "bauen", "code", "analyse", "werkzeug"]
        is_code_goal = any(w in goal_text.lower() for w in code_keywords)

        if is_code_goal and not has_progress:
            # Code-Ziel ohne Workspace-Datei → Stub anlegen + Todo
            try:
                slug = re.sub(r"[^a-z0-9]", "_", goal_text.lower()[:40]).strip("_")
                ts = now_berlin().strftime("%Y%m%d_%H%M")
                filename = f"goal_{ts}_{slug[:20]}.py"
                stub = (
                    f"# Ziel: {goal_text[:200]}\n"
                    f"# Angelegt: {now_berlin().strftime('%Y-%m-%d %H:%M')}\n\n"
                    f"# TODO: implementieren\n"
                )
                save_file(filename, stub)
                logger.info(f"goal_tracker: Stub angelegt: {filename}")
            except Exception as _se:
                logger.debug(f"goal_tracker: Stub fehlgeschlagen: {_se}")

        # kimi-Todo für nächsten Schritt anlegen
        try:
            if is_code_goal:
                if has_progress:
                    title = f"Weitermachen: {goal_text[:50]}"
                else:
                    title = f"Anfangen: {goal_text[:50]}"
            else:
                title = f"Nächster Schritt: {goal_text[:50]}"

            todo = create_todo(
                user_id=user_id,
                title=title,
                description=f"Aus Zielverfolgung — Ziel: {goal_text[:200]}",
                priority="mittel",
                project="kimi",
                due_date=tomorrow,
            )
            logger.info(f"goal_tracker: Todo #{todo['id']} angelegt: '{title[:50]}'")
            activated += 1

            # ORBIT-Trigger wenn Code-Ziel
            if is_code_goal:
                try:
                    import orbit as _orbit
                    _orbit.create_trigger(
                        trigger_type="cognition_output",
                        source="goal_tracker",
                        payload={
                            "user_id": user_id,
                            "source": "goal_tracker",
                            "topic_core": f"Kimi hat ein Code-Ziel: {goal_text[:60]}",
                            "relevance": "weak",
                        },
                    )
                except Exception as _ot:
                    logger.debug(f"goal_tracker: ORBIT-Trigger fehlgeschlagen: {_ot}")

        except Exception as _te:
            logger.debug(f"goal_tracker: Todo-Anlage fehlgeschlagen: {_te}")

    logger.info(f"goal_tracker: {activated} Ziel(e) reaktiviert")
    return activated
