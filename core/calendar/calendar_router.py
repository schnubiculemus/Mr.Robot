"""
core/calendar/calendar_router.py — Unified Calendar Interface

Einziger Einstiegspunkt für app.py, heartbeat.py und proactive.py.
Kapselt Google Calendar und iCloud vollständig.

Kalender-Mapping (aus tools_config.json):
    work    → Google Calendar (Arbeit)
    private → iCloud (Privat)
    study   → iCloud (Study)

CALENDAR_ACTION-Format:
    [CALENDAR_ACTION: {"action": "list", "range": "today"}]
    [CALENDAR_ACTION: {"action": "list", "range": "tomorrow"}]
    [CALENDAR_ACTION: {"action": "list", "range": "2026-03-15"}]
    [CALENDAR_ACTION: {"action": "create", "calendar": "work|private|study",
                       "title": "...", "start": "YYYY-MM-DDTHH:MM",
                       "end": "YYYY-MM-DDTHH:MM", "description": "..."}]
    [CALENDAR_ACTION: {"action": "delete", "calendar": "work|private|study", "event_id": "..."}]
    [CALENDAR_ACTION: {"action": "update", "calendar": "work|private|study", "event_id": "...",
                       "title": "...", "start": "...", "end": "..."}]
"""

import re
import json
import logging
import os

logger = logging.getLogger(__name__)

TOOLS_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "data", "tools_config.json")

# Kalender-ID → provider + icloud_name
CALENDAR_MAP = {
    "work":    {"provider": "google", "id": "calendar_work"},
    "private": {"provider": "icloud", "id": "calendar_private"},
    "study":   {"provider": "icloud", "id": "calendar_study"},
}

CALENDAR_LABELS = {
    "work":    "Arbeit",
    "private": "Privat",
    "study":   "Study",
}


# =============================================================================
# Config Helpers
# =============================================================================

def _load_sub_calendars() -> dict:
    """Gibt sub_calendars als Dict {cal_id: cal_config} zurück."""
    try:
        with open(TOOLS_CONFIG_PATH) as f:
            tools = json.load(f)
        for t in tools:
            if t.get("id") == "calendar":
                return {c["id"]: c for c in t.get("sub_calendars", [])}
    except Exception:
        pass
    return {}


def _is_enabled(calendar_key: str) -> bool:
    """Prüft ob ein Kalender enabled ist."""
    cfg = CALENDAR_MAP.get(calendar_key)
    if not cfg:
        return False
    subs = _load_sub_calendars()
    cal = subs.get(cfg["id"], {})
    return cal.get("enabled", False)


def _has_permission(calendar_key: str, perm: str) -> bool:
    """Prüft ob ein Kalender eine bestimmte Permission hat (read/write/delete)."""
    cfg = CALENDAR_MAP.get(calendar_key)
    if not cfg:
        return False
    subs = _load_sub_calendars()
    cal = subs.get(cfg["id"], {})
    if not cal.get("enabled", False):
        return False
    return cal.get("permissions", {}).get(perm, False)


def _get_provider(calendar_key: str) -> str:
    """Gibt den Provider für einen Kalender-Key zurück."""
    return CALENDAR_MAP.get(calendar_key, {}).get("provider", "")


# =============================================================================
# Öffentliche API
# =============================================================================

def list_events(date_str: str) -> list:
    """
    Gibt alle Termine eines Tages zurück — alle aktivierten Kalender zusammen.

    Args:
        date_str: ISO-Datum "YYYY-MM-DD"

    Returns:
        Chronologisch sortierte Liste von Event-Dicts
    """
    from core.calendar.google_cal import list_events as google_list
    from core.calendar.icloud_cal import list_events as icloud_list
    from core.calendar.calendar_utils import merge_and_sort

    all_events = []

    if _is_enabled("work"):
        try:
            all_events += google_list(date_str)
        except Exception as e:
            logger.warning(f"Google Calendar list fehlgeschlagen: {e}")

    icloud_needed = _is_enabled("private") or _is_enabled("study")
    if icloud_needed:
        try:
            all_events += icloud_list(date_str)
        except Exception as e:
            logger.warning(f"iCloud CalDAV list fehlgeschlagen: {e}")

    return merge_and_sort(all_events, [])


def get_today_summary() -> str:
    """
    Kompakter Tages-Überblick für das Briefing.
    """
    from core.calendar.calendar_utils import parse_date_range, date_label_for, format_events_for_kimi
    from datetime import datetime
    from zoneinfo import ZoneInfo

    today = datetime.now(ZoneInfo("Europe/Berlin")).date().isoformat()
    events = list_events(today)
    label = date_label_for(today)
    return format_events_for_kimi(events, label)


# =============================================================================
# CALENDAR_ACTION Parsing und Dispatch
# =============================================================================

def extract_calendar_action(text: str):
    """
    Extrahiert einen [CALENDAR_ACTION: {...}] Block aus Kimis Antwort.

    Returns:
        (text_ohne_block, action_dict_oder_None)
    """
    pattern = r"\[CALENDAR_ACTION:\s*(\{.*?\})\s*\]"
    match = re.search(pattern, text, re.DOTALL)
    if not match:
        return text, None

    raw_json = match.group(1)
    text_cleaned = re.sub(pattern, "", text, flags=re.DOTALL).strip()
    text_cleaned = re.sub(r"\n{3,}", "\n\n", text_cleaned).strip()

    try:
        action = json.loads(raw_json)
        return text_cleaned, action
    except json.JSONDecodeError as e:
        logger.warning(f"CALENDAR_ACTION JSON ungültig: {e} — Block: {raw_json[:100]}")
        return text_cleaned, None


def execute_calendar_action(action: dict) -> str:
    """
    Führt eine Kalender-Aktion aus und gibt einen menschenlesbaren
    Ergebnis-String zurück.
    """
    action_type = action.get("action", "").lower()

    if action_type == "list":
        return _action_list(action)
    elif action_type == "create":
        return _action_create(action)
    elif action_type == "delete":
        return _action_delete(action)
    elif action_type == "update":
        return _action_update(action)
    else:
        logger.warning(f"Unbekannte CALENDAR_ACTION: {action_type}")
        return f"Unbekannte Kalender-Aktion: {action_type}"


# =============================================================================
# Action Handler
# =============================================================================

def _action_list(action: dict) -> str:
    from core.calendar.calendar_utils import parse_date_range, date_label_for, format_events_for_kimi
    from datetime import date, timedelta

    range_str = action.get("range", "today")
    resolved = parse_date_range(range_str)

    # Wochen-Range: "week:YYYY-MM-DD" (Montag)
    if resolved.startswith("week:"):
        monday = date.fromisoformat(resolved[5:])
        parts = []
        for i in range(7):
            day = monday + timedelta(days=i)
            day_str = day.isoformat()
            events = list_events(day_str)
            if events:
                label = date_label_for(day_str)
                parts.append(format_events_for_kimi(events, label))
        if not parts:
            return "Diese Woche keine Termine."
        return "\n\n".join(parts)

    # Monats-Range: "month:YYYY-MM"
    if resolved.startswith("month:"):
        import calendar as cal_mod
        year, month = map(int, resolved[6:].split("-"))
        _, days_in_month = cal_mod.monthrange(year, month)
        parts = []
        for day_num in range(1, days_in_month + 1):
            day_str = f"{year}-{month:02d}-{day_num:02d}"
            events = list_events(day_str)
            if events:
                label = date_label_for(day_str)
                parts.append(format_events_for_kimi(events, label))
        if not parts:
            return "Diesen Monat keine Termine."
        return "\n\n".join(parts)

    # Einzel-Tag
    events = list_events(resolved)
    label = date_label_for(resolved)
    return format_events_for_kimi(events, label)


def _action_create(action: dict) -> str:
    calendar = action.get("calendar", "").lower()
    title    = action.get("title", "")
    start    = action.get("start", "")
    end      = action.get("end", "")
    description = action.get("description", "")

    if not title or not start or not end:
        return "Termin konnte nicht erstellt werden: Titel, Start und Ende sind erforderlich."

    if not calendar:
        return "Bitte sag mir ob der Termin für Arbeit (work), Privat (private) oder Study (study) ist."

    if calendar not in CALENDAR_MAP:
        return f"Unbekannter Kalender: '{calendar}'. Bitte 'work', 'private' oder 'study' angeben."

    if not _has_permission(calendar, "write"):
        label = CALENDAR_LABELS.get(calendar, calendar)
        return f"Schreibzugriff auf '{label}' ist deaktiviert."

    provider = _get_provider(calendar)
    if provider == "google":
        from core.calendar.google_cal import create_event
    else:
        from core.calendar.icloud_cal import create_event

    result = create_event(title, start, end, description)

    if "error" in result:
        return f"Termin konnte nicht erstellt werden: {result['error']}"

    from core.calendar.calendar_utils import _format_time
    cal_label = CALENDAR_LABELS.get(calendar, calendar)
    return f"Termin angelegt: {title}, {start[:10]} {_format_time(start)}–{_format_time(end)} [{cal_label}]"


def _action_delete(action: dict) -> str:
    calendar = action.get("calendar", "").lower()
    event_id = action.get("event_id", "")

    if not event_id:
        return "Termin konnte nicht gelöscht werden: Keine Event-ID angegeben."

    if calendar not in CALENDAR_MAP:
        return "Bitte sag mir ob es ein Arbeit-, Privat- oder Study-Termin ist."

    if not _has_permission(calendar, "delete"):
        label = CALENDAR_LABELS.get(calendar, calendar)
        return f"Löschzugriff auf '{label}' ist deaktiviert."

    provider = _get_provider(calendar)
    if provider == "google":
        from core.calendar.google_cal import delete_event
    else:
        from core.calendar.icloud_cal import delete_event

    result = delete_event(event_id)

    if "error" in result:
        return f"Termin konnte nicht gelöscht werden: {result['error']}"

    return "Termin wurde gelöscht."


def _action_update(action: dict) -> str:
    calendar = action.get("calendar", "").lower()
    event_id = action.get("event_id", "")

    if not event_id:
        return "Termin konnte nicht aktualisiert werden: Keine Event-ID angegeben."

    if calendar not in CALENDAR_MAP:
        return "Bitte sag mir ob es ein Arbeit-, Privat- oder Study-Termin ist."

    if not _has_permission(calendar, "write"):
        label = CALENDAR_LABELS.get(calendar, calendar)
        return f"Schreibzugriff auf '{label}' ist deaktiviert."

    title       = action.get("title")
    start       = action.get("start")
    end         = action.get("end")
    description = action.get("description")

    provider = _get_provider(calendar)
    if provider == "google":
        from core.calendar.google_cal import update_event
    else:
        from core.calendar.icloud_cal import update_event

    result = update_event(event_id, title=title, start=start, end=end, description=description)

    if "error" in result:
        return f"Termin konnte nicht aktualisiert werden: {result['error']}"

    return f"Termin aktualisiert: {result.get('title', event_id)}"
