"""
core/tools.py — Tool Layer V2 (WP6)

WP6: Saubere Tool-Schicht für Kimi Core.

Architekturregeln:
  - Tools sind Fähigkeiten, keine Akteure
  - Kimi Core entscheidet — Tools führen nur aus
  - Tools sprechen nicht direkt mit dem Nutzer
  - Tools starten nichts selbst
  - Tools schreiben nicht selbständig
  - Tools laufen nur auf Anforderung von Kimi Core

Access-Modi:
  READ     — Informationen abrufen (calendar.list, todo.list, web.search, introspect)
  PROPOSE  — Kimi schlägt vor, Nutzer bestätigt (via Kimi-Antworttext, kein eigener Handler)
  WRITE    — Verändernde Aktion — kontrolliert, eng
             → Write-Aktionen laufen über kimi_output.py, NICHT über dieses Modul

Verantwortlichkeiten dieses Moduls:
  READ-Tools:  handle_web_search, handle_introspect, handle_calendar_read, handle_todo_read
  WRITE-Tools: kimi_output.py (_run_calendar, _run_todo) — nicht hier

Kimi Core bindet READ-Tools in die Pipeline ein. Bei Treffer: zweiter Ollama-Call
mit Ergebnis als doc_context — Kimi sieht das Ergebnis und kann darauf antworten.

WP6-Bindungen:
  WP0: keine neue Hintergrundautonomie, keine Selbsttrigger
  WP1: Kimi Core bleibt die einzige führende Instanz
  WP2: Toolnutzung orientiert sich am Active Working Context
  WP3: Tooldaten gehen nicht direkt als Führungslogik ins Memory
  WP4: Tools schreiben nicht heimlich in den Workspace
  WP5: ORBIT wird nicht wieder zum Tool-Dirigenten
"""

import logging
import re

logger = logging.getLogger(__name__)


# =============================================================================
# Access-Modi (WP6)
# =============================================================================

ACCESS_READ    = "read"    # Nur lesen — kein Schreiben
ACCESS_PROPOSE = "propose" # Kimi schlägt vor — Nutzer muss bestätigen
ACCESS_WRITE   = "write"   # Verändernde Aktion — kontrolliert, eng

# Tool-Verzeichnis: welche Operationen welchen Modus haben
# READ-Tools werden in kimi_core.py verarbeitet (zweiter Kimi-Call mit Ergebnis)
# WRITE-Tools werden in kimi_output.py verarbeitet (kein zweiter Call, Bestätigung an Tommy)
TOOL_REGISTRY = {
    # Websearch
    "web.search":           ACCESS_READ,

    # Introspect
    "introspect":           ACCESS_READ,

    # Kalender
    "calendar.list":        ACCESS_READ,    # → handle_calendar_read (dieses Modul)
    "calendar.create":      ACCESS_WRITE,   # → kimi_output._run_calendar
    "calendar.update":      ACCESS_WRITE,   # → kimi_output._run_calendar
    "calendar.delete":      ACCESS_WRITE,   # → kimi_output._run_calendar

    # Todos
    "todo.list":            ACCESS_READ,    # → handle_todo_read (dieses Modul)
    "todo.create":          ACCESS_WRITE,   # → kimi_output._run_todo
    "todo.complete":        ACCESS_WRITE,   # → kimi_output._run_todo
    "todo.delete":          ACCESS_WRITE,   # → kimi_output._run_todo
    "todo.update":          ACCESS_WRITE,   # → kimi_output._run_todo
}


# =============================================================================
# READ Tool: Websearch
# =============================================================================

def handle_web_search(reply: str, user_id: str = "unknown", user_message: str = "") -> tuple:
    """
    ACCESS_READ — Prüft ob Kimi [SEARCH: query] geschrieben hat.
    Returns: (reply_cleaned, search_context_or_None)

    Ergebnis als doc_context in zweitem Kimi-Call — Kimi sieht das Ergebnis.
    """
    from core.websearch import search as web_search, format_for_kimi as format_search_result

    matches = re.findall(r"\[SEARCH:\s*(.+?)\]", reply, re.IGNORECASE)
    if not matches:
        return reply, None

    query = matches[0].strip()
    logger.info(f"Tool WebSearch (READ): '{query}'")

    reply_cleaned = re.sub(r"\[SEARCH:\s*.+?\]", "", reply, flags=re.IGNORECASE).strip()
    reply_cleaned = re.sub(r"\n{3,}", "\n\n", reply_cleaned).strip()

    result = web_search(query)
    if not result["success"]:
        logger.warning(f"WebSearch fehlgeschlagen: {result.get('error')}")
        return reply_cleaned, None

    search_ctx = (
        "WEBSEARCH ERGEBNIS — bereits abgerufen, keine weitere Suche nötig:\n\n"
        + format_search_result(result)
        + "\n\nBeantworte jetzt die Frage des Nutzers direkt auf Basis dieser Informationen. "
        "Schreibe KEIN [SEARCH:...] mehr. Kein Markdown, keine Sternchen. Fließtext."
    )
    logger.info(f"WebSearch OK: {len(result['answer'])} Zeichen")
    try:
        from core.database import save_search_log
        save_search_log(
            user_id=user_id, query=query, success=True,
            result_length=len(result.get("answer", "")),
            user_message_preview=user_message,
        )
    except Exception:
        pass
    return reply_cleaned, search_ctx


# =============================================================================
# READ Tool: Introspect
# =============================================================================

def handle_introspect(reply: str) -> tuple:
    """
    ACCESS_READ — Prüft ob Kimi [INTROSPECT] geschrieben hat.
    Returns: (reply_cleaned, introspect_context_or_None)

    Ergebnis als doc_context in zweitem Kimi-Call.
    """
    if "[INTROSPECT]" not in reply.upper():
        return reply, None

    reply_cleaned = re.sub(r"\[INTROSPECT\]", "", reply, flags=re.IGNORECASE).strip()
    reply_cleaned = re.sub(r"\n{3,}", "\n\n", reply_cleaned).strip()
    logger.info("Tool Introspect (READ) aufgerufen")

    try:
        from core.database import get_mirror_turns, get_mirror_stats, get_chunk_genealogy
        stats = get_mirror_stats(days=14)
        turns = get_mirror_turns(limit=20)
        genealogy = get_chunk_genealogy()

        total = stats.get("total_turns", 0)
        dist = stats.get("preflight_distribution", {})
        green_pct = round(dist.get("green", 0) / max(total, 1) * 100)
        bad_pct = round((dist.get("orange", 0) + dist.get("red", 0)) / max(total, 1) * 100)

        pattern_counts = stats.get("pattern_counts", {})
        pattern_names = {
            "aufzaehlung":   "Aufzählungs-Falle",
            "projektmodus":  "Projektmodus-Versteck",
            "regel_relapse": "Regel-Rückfall (Markdown)",
            "uebervorsicht": "Übervorsicht / Nachfrage",
            "selbstkritik":  "Selbstkritik im Chat",
        }
        pattern_lines = []
        for pid, count in sorted(pattern_counts.items(), key=lambda x: -x[1]):
            name = pattern_names.get(pid, pid)
            pattern_lines.append(f"  {name}: {count}x in {total} Turns")

        flagged_turns = [t for t in turns if t.get("pattern_flags")][:5]
        flagged_lines = []
        for t in flagged_turns:
            flags = ", ".join(t.get("pattern_flags") or [])
            msg = (t.get("user_message") or "")[:60]
            flagged_lines.append(f"  [{flags}] \"{msg}\"")

        gen_lines = []
        for entry in (genealogy or [])[:5]:
            gen_lines.append(
                f"  {entry.get('chunk_type','')} | "
                f"conf={entry.get('confidence',0):.2f} | "
                f"{(entry.get('text') or '')[:50]}"
            )

        introspect_ctx = (
            f"MEINE MIRROR-DATEN (letzte 14 Tage)\n\n"
            f"Turns gesamt: {total}\n"
            f"Preflight: {green_pct}% grün / {bad_pct}% problematisch\n\n"
            f"Verhaltensmuster:\n" + ("\n".join(pattern_lines) or "  (keine Daten)") + "\n\n"
            f"Auffällige Turns:\n" + ("\n".join(flagged_lines) or "  (keine)") + "\n\n"
            f"Chunk-Genealogie (Top 5):\n" + ("\n".join(gen_lines) or "  (keine)")
        )
        return reply_cleaned, introspect_ctx
    except Exception as e:
        logger.warning(f"Introspect fehlgeschlagen: {e}")
        return reply_cleaned, None


# =============================================================================
# READ Tool: Kalender lesen
# =============================================================================

def handle_calendar_read(reply: str) -> tuple:
    """
    ACCESS_READ — Erkennt [CALENDAR_ACTION: {"action": "list", ...}] in Kimis Antwort.
    Führt NUR list-Aktionen aus. create/update/delete werden hier nicht angefasst
    — die laufen über kimi_output.py (_run_calendar).

    Returns: (reply_cleaned, calendar_context_or_None)

    Ergebnis als doc_context in zweitem Kimi-Call — Kimi sieht die Termine.
    """
    try:
        from core.calendar.calendar_router import extract_calendar_action, execute_calendar_action
    except Exception as e:
        logger.debug(f"handle_calendar_read: calendar_router nicht verfügbar: {e}")
        return reply, None

    # Nur bei list-Aktionen eingreifen — Write-Aktionen bleiben für kimi_output.py
    _list_pattern = re.compile(
        r'\[CALENDAR_ACTION:\s*\{[^}]*"action"\s*:\s*"list"[^}]*\}\s*\]',
        re.DOTALL | re.IGNORECASE
    )
    if not _list_pattern.search(reply):
        return reply, None

    reply_cleaned, action = extract_calendar_action(reply)
    if not action:
        return reply, None

    action_type = action.get("action", "").lower()
    if action_type != "list":
        # Kein list — zurück ohne Änderung, kimi_output.py verarbeitet weiter
        return reply, None

    logger.info(f"Tool CalendarRead (READ): range={action.get('range', 'today')}")

    try:
        result_text = execute_calendar_action(action)
        if not result_text or not result_text.strip():
            return reply_cleaned, None

        cal_ctx = (
            "KALENDER-ABFRAGE ERGEBNIS:\n\n"
            + result_text
            + "\n\nBeantworte jetzt die Frage des Nutzers auf Basis dieser Kalenderinformationen. "
            "Schreibe KEIN [CALENDAR_ACTION:...] mehr."
        )
        logger.info(f"CalendarRead OK: {len(result_text)} Zeichen")
        return reply_cleaned, cal_ctx
    except Exception as e:
        logger.warning(f"handle_calendar_read: Ausführung fehlgeschlagen: {e}")
        return reply_cleaned, None


# =============================================================================
# READ Tool: Todos lesen
# =============================================================================

def handle_todo_read(reply: str, user_id: str = "unknown") -> tuple:
    """
    ACCESS_READ — Erkennt [TODO_ACTION: {"action": "list", ...}] in Kimis Antwort.
    Führt NUR list-Aktionen aus. create/complete/delete laufen über kimi_output.py.

    Returns: (reply_cleaned, todo_context_or_None)

    Ergebnis als doc_context in zweitem Kimi-Call — Kimi sieht die Todo-Liste.
    """
    _list_pattern = re.compile(
        r'\[TODO_ACTION:\s*(\{[^}]*"action"\s*:\s*"list"[^}]*\})\s*\]',
        re.DOTALL | re.IGNORECASE
    )
    match = _list_pattern.search(reply)
    if not match:
        return reply, None

    # Block aus Reply entfernen
    reply_cleaned = _list_pattern.sub("", reply).strip()
    reply_cleaned = re.sub(r"\n{3,}", "\n\n", reply_cleaned).strip()

    # Payload parsen
    try:
        import json
        raw = match.group(1).replace('\n', ' ').replace('\r', '')
        payload = json.loads(raw)
    except Exception as e:
        logger.warning(f"handle_todo_read: JSON-Fehler: {e}")
        return reply_cleaned, None

    action_type = payload.get("action", "").lower()
    if action_type != "list":
        return reply, None

    logger.info(f"Tool TodoRead (READ): project={payload.get('project', 'alle')}, "
                f"status={payload.get('status', 'open')}")

    try:
        from core.todos import get_open_todos, get_all_todos
        project_filter = payload.get("project")
        status_filter = payload.get("status", "open")

        if status_filter == "open":
            todos = get_open_todos(user_id)
        else:
            todos = get_all_todos(user_id, limit=30)

        # Optional: nach project filtern
        if project_filter:
            todos = [t for t in todos
                     if (t.get("project") or "").lower() == project_filter.lower()]

        if not todos:
            proj_hint = f" in Projekt '{project_filter}'" if project_filter else ""
            result_text = f"Keine offenen Todos{proj_hint}."
        else:
            lines = []
            for t in todos[:20]:
                prio = t.get("priority", "keine")
                proj = t.get("project", "")
                due = t.get("due_date", "")
                due_str = f" (fällig: {due})" if due else ""
                proj_str = f" [{proj}]" if proj else ""
                lines.append(f"  #{t['id']} {t['title']}{proj_str}{due_str} — {prio}")
            result_text = f"Offene Todos ({len(todos)}):\n" + "\n".join(lines)

        todo_ctx = (
            "TODO-LISTE ERGEBNIS:\n\n"
            + result_text
            + "\n\nBeantworte jetzt die Frage des Nutzers auf Basis dieser Todo-Liste. "
            "Schreibe KEIN [TODO_ACTION:...] mehr."
        )
        logger.info(f"TodoRead OK: {len(todos)} Todos")
        return reply_cleaned, todo_ctx
    except Exception as e:
        logger.warning(f"handle_todo_read: Ausführung fehlgeschlagen: {e}")
        return reply_cleaned, None
