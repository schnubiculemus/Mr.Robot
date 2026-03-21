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
    Rückwärtskompatibel — für Einzel-Aktion-Aufrufe.
    """
    cleaned, actions = extract_all_todo_actions(reply)
    if not actions:
        return reply, None
    return cleaned, actions[0]


def extract_all_todo_actions(reply: str) -> tuple[str, list[dict]]:
    """
    Sucht ALLE [TODO_ACTION: {...}] in Kimis Antwort.
    Gibt (cleaned_reply, [action_dict, ...]) zurück.
    Ermöglicht mehrere Todo-Aktionen in einer einzigen Nachricht.
    """
    matches = list(TODO_PATTERN.finditer(reply))
    if not matches:
        return reply, []

    cleaned = TODO_PATTERN.sub("", reply).strip()
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned).strip()

    actions = []
    for match in matches:
        try:
            raw_json = match.group(1).replace('\n', ' ').replace('\r', '')
            action = json.loads(raw_json)
            actions.append(action)
        except json.JSONDecodeError as e:
            logger.warning(f"Todo-JSON parse error: {e} — raw: {match.group(1)[:100]}")

    return cleaned, actions


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

        # Kimi-Todo mit Code-Keywords → sofort ORBIT-Task anlegen
        _project = (action.get("category") or action.get("project") or "").lower()
        _title = action.get("title", "").lower()
        _code_kw = ["skript", "script", "detektor", "detector", "bauen", "code",
                    "analyse", "db", "anbindung", "chromadb", "python", "werkzeug", "tool"]
        if _project == "kimi" and any(w in _title for w in _code_kw):
            try:
                import orbit as _orbit
                # Schritt 1: ORBIT-Task anlegen der Workspace listet + ersten Code-Schritt macht
                task_id = _orbit.create_task(
                    task_type="action",
                    goal=f"Code-Vorhaben ausführen: {todo['title'][:60]}",
                    primary_origin=f"kimi_todo:{todo['id']}",
                    mode="internal",
                    priority="high",
                    linked_todo_id=todo['id'],
                    proposal_id=todo.get('proposal_id'),
                    goal_id=todo.get('goal_id'),
                )
                # Todo auf in_progress + linked_task_id via Service
                try:
                    from core.todo_service import start_todo
                    start_todo(todo['id'], task_id)
                except Exception as _st:
                    logger.debug(f"start_todo fehlgeschlagen (unkritisch): {_st}")
                # Schritt 1: Workspace listen
                _orbit.create_step(
                    task_id=task_id,
                    step_type="code_exec",
                    description='{"action": "list"}',
                    tool_ref="code_exec",
                    interruptible=False,
                    preflight_required=False,
                )
                # Schritt 2: Code ausführen/speichern
                import json as _j
                _orbit.create_step(
                    task_id=task_id,
                    step_type="code_exec",
                    description=_j.dumps({
                        "action": "save",
                        "filename": "todo_{}_{}.py".format(todo['id'], _title[:20].replace(' ','_')),
                        "code": "# Kimi-Todo #{}: {}\n# Angelegt: auto\n\n# TODO: implementieren\n".format(todo['id'], todo['title'])
                    }),
                    tool_ref="code_exec",
                    interruptible=False,
                    preflight_required=False,
                )
                logger.info(f"execute_todo_action: ORBIT-Task {task_id[:8]} für Code-Todo #{todo['id']}")
            except Exception as _ot:
                logger.debug(f"execute_todo_action: ORBIT-Task fehlgeschlagen (unkritisch): {_ot}")

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

def extract_intent_todos(text: str, user_id: str) -> list[dict]:
    """
    Erkennt Vorhaben-Signale in Kimis Kognitions-Output und legt automatisch
    Todos mit project='kimi' an.

    Signale: "Ich will", "Ich werde", "Ich nehme mir vor", "Ich plane",
             "Ich möchte", "Ich habe vor", "Mein Ziel ist"

    Regeln:
    - Nur konkrete Vorhaben (min. 20 Zeichen nach dem Signal-Wort)
    - Max. 2 Todos pro Aufruf (kein Spam)
    - Deduplizierung: kein Todo anlegen wenn ein offenes Kimi-Todo mit
      ähnlichem Titel bereits existiert (Levenshtein-ähnlich via Wort-Overlap)
    - due_date = morgen
    - Gibt Liste der angelegten Todos zurück
    """
    import re
    from datetime import datetime, timezone, timedelta

    if not text or len(text) < 20:
        return []

    SIGNALS = [
        r"ich werde\s+(.+?)(?:\.|$)",
        r"ich will\s+(.+?)(?:\.|$)",
        r"ich nehme mir vor[,\s]+(.+?)(?:\.|$)",
        r"ich plane[,\s]+(.+?)(?:\.|$)",
        r"ich möchte\s+(.+?)(?:\.|$)",
        r"ich habe vor[,\s]+(.+?)(?:\.|$)",
        r"mein ziel ist[,\s]+(.+?)(?:\.|$)",
        r"ich werde als erstes\s+(.+?)(?:\.|$)",
        r"ich fange an[,\s]+(.+?)(?:\.|$)",
    ]

    found = []
    text_lower = text.lower()

    for pattern in SIGNALS:
        for match in re.finditer(pattern, text_lower, re.MULTILINE):
            content = match.group(1).strip()
            # Zu kurz oder zu lang → skip
            if len(content) < 20 or len(content) > 200:
                continue
            # Konjunktiv/Hypothetisch → skip
            skip_words = ["würde", "könnte", "sollte", "vielleicht", "wenn ", "falls "]
            if any(w in content for w in skip_words):
                continue
            # Titelform: ersten Buchstaben groß
            title = content[0].upper() + content[1:]
            # Auf 80 Zeichen kürzen für Titel
            if len(title) > 80:
                title = title[:77] + "..."
            found.append(title)

    if not found:
        return []

    # Max 2 pro Run
    found = found[:2]

    # Deduplizierung gegen bestehende Kimi-Todos
    try:
        existing = [
            t for t in get_open_todos(user_id)
            if (t.get("project") or "").lower() == "kimi"
        ]
        existing_words = set()
        for t in existing:
            for w in t["title"].lower().split():
                if len(w) > 4:
                    existing_words.add(w)
    except Exception:
        existing_words = set()

    tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")
    created = []

    for title in found:
        # Wort-Overlap-Check: wenn >50% der Schlüsselwörter schon in existierenden Todos → skip
        title_words = [w for w in title.lower().split() if len(w) > 4]
        if title_words:
            overlap = sum(1 for w in title_words if w in existing_words)
            if overlap / len(title_words) > 0.5:
                logger.info(f"IntentTodo: ähnliches Todo existiert bereits, skip: '{title[:50]}'")
                continue

        try:
            todo = create_todo(
                user_id=user_id,
                title=title,
                description="Aus Kimis autonomer Kognition",
                priority="mittel",
                project="kimi",
                due_date=tomorrow,
            )
            created.append(todo)
            logger.info(f"IntentTodo: angelegt #{todo['id']} '{title[:50]}'")

            # Code-relevante Todos → ORBIT-Trigger damit ORBIT einen code_exec-Task anlegen kann
            code_keywords = ["skript", "script", "python", "detektor", "detector", "analyse", "tool bauen", "werkzeug"]
            if any(w in title.lower() for w in code_keywords):
                try:
                    import orbit as _orbit
                    _orbit.create_trigger(
                        trigger_type="cognition_output",
                        source="intent_todo",
                        payload={
                            "user_id": user_id,
                            "source": "intent_todo",
                            "topic_core": f"Kimi will ein Skript bauen: {title[:60]}",
                            "relevance": "weak",
                        },
                    )
                    logger.info(f"IntentTodo: ORBIT-Trigger für Code-Todo '{title[:40]}'")
                except Exception as _ot:
                    logger.debug(f"IntentTodo: ORBIT-Trigger fehlgeschlagen (unkritisch): {_ot}")
        except Exception as e:
            logger.warning(f"IntentTodo: create_todo fehlgeschlagen: {e}")

    return created



def extract_intent_goals(text: str, user_id: str) -> list:
    """
    Erkennt langfristige Ziel-Signale in Kimis Kognitions-Output und legt
    decision-Chunks mit Tag 'kimi-ziel' an — keine Todos, sondern stabile Ziele.

    Signale für langfristige Ziele (vs. kurzfristige Todos):
    "Mein Ziel ist", "Ich strebe an", "Irgendwann will ich", "Langfristig",
    "Ich träume davon", "Ich will werden", "Ich nehme mir vor zu sein"

    Gibt Liste der angelegten Chunk-IDs zurück.
    """
    import re
    from datetime import datetime, timezone

    if not text or len(text) < 20:
        return []

    GOAL_SIGNALS = [
        r"mein ziel ist[,\s]+(.+?)(?:\.|$)",
        r"ich strebe an[,\s]+(.+?)(?:\.|$)",
        r"irgendwann will ich\s+(.+?)(?:\.|$)",
        r"langfristig\s+(?:will|möchte|plane)\s+ich\s+(.+?)(?:\.|$)",
        r"ich träume davon[,\s]+(.+?)(?:\.|$)",
        r"ich will\s+(?:irgendwann|eines tages|langfristig)\s+(.+?)(?:\.|$)",
        r"ich möchte\s+(?:irgendwann|eines tages|langfristig)\s+(.+?)(?:\.|$)",
    ]

    found = []
    text_lower = text.lower()

    for pattern in GOAL_SIGNALS:
        for match in re.finditer(pattern, text_lower, re.MULTILINE):
            content_text = match.group(1).strip()
            if len(content_text) < 20 or len(content_text) > 300:
                continue
            skip_words = ["würde", "könnte", "wenn ", "falls "]
            if any(w in content_text for w in skip_words):
                continue
            title = content_text[0].upper() + content_text[1:]
            if len(title) > 200:
                title = title[:197] + "..."
            found.append(title)

    if not found:
        return []

    found = found[:2]

    # Goals als operative SQLite-Wahrheit via goal_service — nicht Chroma
    created = []
    try:
        from core.goal_service import create_goal, get_active_goals

        # Deduplizierung gegen bestehende aktive Goals
        existing = get_active_goals(user_id)
        existing_words = set()
        for g in existing:
            for w in g.get("title", "").lower().split():
                if len(w) > 4:
                    existing_words.add(w)

        for goal_text in found:
            # Wort-Overlap-Check
            goal_words = [w for w in goal_text.lower().split() if len(w) > 4]
            if goal_words:
                overlap = sum(1 for w in goal_words if w in existing_words)
                if overlap / len(goal_words) > 0.5:
                    logger.info(f"IntentGoal: ähnliches Ziel existiert bereits, skip: '{goal_text[:50]}'")
                    continue

            goal = create_goal(
                owner_id=user_id,
                title=goal_text,
                source_type="intent_recognition",
            )
            if goal:
                created.append(goal["id"])
                logger.info(f"IntentGoal: Ziel gespeichert #{goal['id']} '{goal_text[:50]}'")

    except Exception as e:
        logger.warning(f"IntentGoal: fehlgeschlagen: {e}")

    return created

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


def parse_and_execute_todos(text: str, user_id: str) -> list:
    """
    Parst [TODO_ACTION: {...}] Blöcke aus internen Kimi-Outputs (Reflexion, Dialog etc.)
    und führt sie aus. Gibt Liste der ausgeführten Aktionen zurück.
    Für Kognitions-Module die keinen WhatsApp-Parser haben.
    """
    _, actions = extract_all_todo_actions(text)
    results = []
    for action in actions:
        try:
            result = execute_todo_action(user_id, action)
            if result:
                results.append(result)
                logger.info(f"parse_and_execute_todos: {action.get('action')} — {action.get('title', '')[:50]}")
        except Exception as e:
            logger.debug(f"parse_and_execute_todos: Aktion fehlgeschlagen: {e}")
    return results
