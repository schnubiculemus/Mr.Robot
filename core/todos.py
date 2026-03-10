"""
core/todos.py — Todo-System für SchnuBot.ai

Todos leben in SQLite (bot.db), Tabelle: todos.
Kimi kann Todos via [TODO_ACTION: {...}] anlegen, abschließen und auflisten.
"""

import json
import logging
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

PRIORITIES = ("keine", "hoch", "mittel", "niedrig")
VALID_ACTIONS = ("create", "complete", "delete", "list", "update")


# =============================================================================
# DB-Init (wird von database.init_db() aufgerufen)
# =============================================================================

def init_todos_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS todos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            title TEXT NOT NULL,
            description TEXT,
            priority TEXT NOT NULL DEFAULT 'mittel',
            project TEXT,
            due_date TEXT,
            status TEXT NOT NULL DEFAULT 'open',
            created_at TEXT NOT NULL,
            completed_at TEXT,
            reminded_at TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_todos_user ON todos(user_id, status)")


# =============================================================================
# CRUD
# =============================================================================

def create_todo(user_id: str, title: str, description: str = None,
                priority: str = "keine", project: str = None,
                due_date: str = None) -> dict:
    from core.database import get_connection
    now = datetime.now(timezone.utc).isoformat()
    priority = priority if priority in PRIORITIES else "keine"
    conn = get_connection()
    cur = conn.execute("""
        INSERT INTO todos (user_id, title, description, priority, project, due_date, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, 'open', ?)
    """, (user_id, title.strip(), description, priority, project, due_date, now))
    conn.commit()
    todo_id = cur.lastrowid
    logger.info(f"Todo erstellt: #{todo_id} '{title}' (Prio: {priority})")
    return get_todo(todo_id)


def complete_todo(todo_id: int) -> dict | None:
    from core.database import get_connection
    now = datetime.now(timezone.utc).isoformat()
    conn = get_connection()
    conn.execute(
        "UPDATE todos SET status='done', completed_at=? WHERE id=?",
        (now, todo_id)
    )
    conn.commit()
    return get_todo(todo_id)


def delete_todo(todo_id: int) -> bool:
    from core.database import get_connection
    conn = get_connection()
    conn.execute("DELETE FROM todos WHERE id=?", (todo_id,))
    conn.commit()
    return True


def get_todo(todo_id: int) -> dict | None:
    from core.database import get_connection
    conn = get_connection()
    row = conn.execute("SELECT * FROM todos WHERE id=?", (todo_id,)).fetchone()
    return dict(row) if row else None


def get_open_todos(user_id: str) -> list:
    from core.database import get_connection
    conn = get_connection()
    rows = conn.execute("""
        SELECT * FROM todos
        WHERE user_id=? AND status='open'
        ORDER BY
            CASE priority WHEN 'hoch' THEN 0 WHEN 'mittel' THEN 1 ELSE 2 END,
            due_date ASC NULLS LAST,
            created_at ASC
    """, (user_id,)).fetchall()
    return [dict(r) for r in rows]


def get_all_todos(user_id: str, limit: int = 50) -> list:
    from core.database import get_connection
    conn = get_connection()
    rows = conn.execute("""
        SELECT * FROM todos
        WHERE user_id=?
        ORDER BY created_at DESC
        LIMIT ?
    """, (user_id, limit)).fetchall()
    return [dict(r) for r in rows]


def get_overdue_todos(user_id: str) -> list:
    """Todos mit überschrittenem Fälligkeitsdatum."""
    from core.database import get_connection
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    conn = get_connection()
    rows = conn.execute("""
        SELECT * FROM todos
        WHERE user_id=? AND status='open' AND due_date IS NOT NULL AND due_date < ?
        ORDER BY due_date ASC
    """, (user_id, today)).fetchall()
    return [dict(r) for r in rows]


def get_due_today(user_id: str) -> list:
    """Todos die heute fällig sind."""
    from core.database import get_connection
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    conn = get_connection()
    rows = conn.execute("""
        SELECT * FROM todos
        WHERE user_id=? AND status='open' AND due_date=?
        ORDER BY CASE priority WHEN 'hoch' THEN 0 WHEN 'mittel' THEN 1 ELSE 2 END
    """, (user_id, today)).fetchall()
    return [dict(r) for r in rows]


def mark_reminded(todo_id: int):
    from core.database import get_connection
    now = datetime.now(timezone.utc).isoformat()
    conn = get_connection()
    conn.execute("UPDATE todos SET reminded_at=? WHERE id=?", (now, todo_id))
    conn.commit()


# =============================================================================
# Todo-Formatting für WhatsApp
# =============================================================================

PRIO_EMOJI = {"hoch": "🔴", "mittel": "🟡", "niedrig": "🟢"}


def format_todo_list(todos: list, title: str = "Offene Todos") -> str:
    if not todos:
        return "Keine offenen Todos."

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lines = [f"*{title}* ({len(todos)})\n"]

    current_project = None
    for t in todos:
        proj = t.get("project") or "Allgemein"
        if proj != current_project:
            if current_project is not None:
                lines.append("")
            lines.append(f"_{proj}_")
            current_project = proj

        prio = t.get("priority", "mittel")
        emoji = PRIO_EMOJI.get(prio, "⚪")
        due = t.get("due_date", "")
        due_str = ""
        if due:
            if due < today:
                due_str = f" ⚠ überfällig ({due})"
            elif due == today:
                due_str = " ← heute"
            else:
                due_str = f" ({due})"

        lines.append(f"{emoji} #{t['id']} {t['title']}{due_str}")
        if t.get("description"):
            lines.append(f"   {t['description']}")

    return "\n".join(lines)


def format_single_todo(t: dict, verb: str = "Erstellt") -> str:
    prio = t.get("priority", "mittel")
    emoji = PRIO_EMOJI.get(prio, "⚪")
    parts = [f"{verb}: {emoji} *{t['title']}*"]
    if t.get("description"):
        parts.append(t["description"])
    extras = []
    if t.get("project"):
        extras.append(f"Kategorie: {t['project']}")
    if t.get("due_date"):
        extras.append(f"Fällig: {t['due_date']}")
    extras.append(f"Priorität: {prio}")
    extras.append(f"#{t['id']}")
    parts.append(" · ".join(extras))
    return "\n".join(parts)


# =============================================================================
# Kimi-Antwort parsen: [TODO_ACTION: {...}]
# =============================================================================

import re

TODO_PATTERN = re.compile(r'\[TODO_ACTION:\s*(\{.*?\})\s*\]', re.DOTALL)


def extract_todo_action(reply: str) -> tuple[str | None, dict | None]:
    """
    Sucht [TODO_ACTION: {...}] in Kimis Antwort.
    Gibt (cleaned_reply, action_dict) zurück oder (reply, None) wenn nichts gefunden.
    """
    match = TODO_PATTERN.search(reply)
    if not match:
        return reply, None

    cleaned = TODO_PATTERN.sub("", reply).strip()
    # Mehrfache Leerzeilen die durch Entfernen entstehen bereinigen
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned).strip()
    try:
        # Kimi schreibt manchmal Zeilenumbrüche im JSON — normalisieren
        raw_json = match.group(1).replace('\n', ' ').replace('\r', '')
        action = json.loads(raw_json)
        return cleaned, action
    except json.JSONDecodeError as e:
        logger.warning(f"Todo-JSON parse error: {e} — raw: {match.group(1)[:100]}")
        return cleaned, None


def execute_todo_action(user_id: str, action: dict) -> str | None:
    """
    Führt eine Todo-Aktion aus und gibt einen kurzen Status-Text zurück.
    Wird als Zusatz an Kimis Antwort gehängt.
    """
    act = action.get("action", "").lower()

    if act == "create":
        todo = create_todo(
            user_id=user_id,
            title=action.get("title", "Unbenanntes Todo"),
            description=action.get("description"),
            priority=action.get("priority", "mittel"),
            project=action.get("category") or action.get("project"),
            due_date=action.get("due_date"),
        )
        return format_single_todo(todo, verb="✓ Todo gespeichert")

    elif act == "complete":
        todo_id = action.get("id")
        if not todo_id:
            return "⚠ Kein Todo-ID für 'complete' angegeben."
        todo = complete_todo(int(todo_id))
        if todo:
            return f"✓ Todo #{todo_id} abgehakt: *{todo['title']}*"
        return f"⚠ Todo #{todo_id} nicht gefunden."

    elif act == "delete":
        todo_id = action.get("id")
        if not todo_id:
            return "⚠ Kein Todo-ID für 'delete' angegeben."
        delete_todo(int(todo_id))
        return f"🗑 Todo #{todo_id} gelöscht."

    elif act == "list":
        todos = get_open_todos(user_id)
        return format_todo_list(todos)

    return None


# =============================================================================
# Proaktive Erinnerungen (wird vom Heartbeat aufgerufen)
# =============================================================================

def get_reminder_message(user_id: str) -> str | None:
    """
    Gibt eine Erinnerungsnachricht zurück wenn relevante Todos anstehen.
    Wird vom Proaktiv-System aufgerufen.
    Cooldown: 1x pro Tag pro Todo.
    """
    overdue = get_overdue_todos(user_id)
    due_today = get_due_today(user_id)

    # Überfällige Todos — immer erinnern wenn noch nicht heute erinnert
    remind_overdue = []
    for t in overdue:
        last = t.get("reminded_at", "")
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if not last or last[:10] < today_str:
            remind_overdue.append(t)
            mark_reminded(t["id"])

    remind_today = []
    for t in due_today:
        last = t.get("reminded_at", "")
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if not last or last[:10] < today_str:
            remind_today.append(t)
            mark_reminded(t["id"])

    if not remind_overdue and not remind_today:
        return None

    parts = []
    if remind_overdue:
        items = "\n".join(f"⚠ #{t['id']} {t['title']} (fällig war {t['due_date']})" for t in remind_overdue[:3])
        parts.append(f"*Überfällige Todos:*\n{items}")
    if remind_today:
        items = "\n".join(f"📌 #{t['id']} {t['title']}" for t in remind_today[:3])
        parts.append(f"*Heute fällig:*\n{items}")

    return "\n\n".join(parts)
