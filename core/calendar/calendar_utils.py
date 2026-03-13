"""
core/calendar/calendar_utils.py — Formatierung und Datum-Parsing

Gemeinsame Hilfsfunktionen für beide Provider:
- Termine für Kimi formatieren (Fließtext, kein Markdown)
- Datum-Strings normalisieren
- Events chronologisch sortieren und mergen
"""

import logging
from datetime import datetime, timezone, date
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
BERLIN = ZoneInfo("Europe/Berlin")


def merge_and_sort(google_events: list, icloud_events: list) -> list:
    """
    Merged Events beider Provider und sortiert chronologisch nach Startzeit.
    Ganztägige Termine kommen zuerst.
    """
    all_events = google_events + icloud_events
    return sorted(all_events, key=_sort_key)


def _sort_key(event: dict):
    """Sortier-Key: Ganztägige zuerst, dann chronologisch."""
    if event.get("all_day"):
        return (0, "")
    start = event.get("start", "")
    return (1, start)


def format_events_for_kimi(events: list, date_label: str = "Heute") -> str:
    """
    Formatiert eine Event-Liste als lesbaren Fließtext für Kimi.
    Kein Markdown, keine Sternchen — WhatsApp-kompatibel.

    Beispiel-Output:
        Heute, Mittwoch 15. März:

        Ganztägig: Schulferien Sachsen [Arbeit]

        09:00–10:00 · Jour Fixe BIM-Team [Arbeit]
        12:00–13:00 · Mittagessen Julia [Privat] (wöchentlich)
        Keine weiteren Termine heute.
    """
    if not events:
        return f"{date_label}: Keine Termine."

    lines = [f"{date_label}:\n"]

    all_day = [e for e in events if e.get("all_day")]
    timed = [e for e in events if not e.get("all_day")]

    for event in all_day:
        source_label = _source_label(event.get("source", ""))
        recurrence = f" ({event['recurrence_label']})" if event.get("recurrence_label") else ""
        lines.append(f"Ganztägig: {event['title']} [{source_label}]{recurrence}")

    if all_day and timed:
        lines.append("")

    for event in timed:
        start_str = _format_time(event.get("start", ""))
        end_str = _format_time(event.get("end", ""))
        source_label = _source_label(event.get("source", ""))
        recurrence = f" ({event['recurrence_label']})" if event.get("recurrence_label") else ""
        location = f" · {event['location']}" if event.get("location") else ""
        lines.append(f"{start_str}–{end_str} · {event['title']} [{source_label}]{recurrence}{location}")

    return "\n".join(lines)


def format_single_event(event: dict) -> str:
    """Kompakte Einzeiler-Darstellung eines einzelnen Events."""
    source_label = _source_label(event.get("source", ""))
    if event.get("all_day"):
        return f"Ganztägig: {event['title']} [{source_label}]"
    start_str = _format_time(event.get("start", ""))
    end_str = _format_time(event.get("end", ""))
    return f"{start_str}–{end_str} · {event['title']} [{source_label}]"


def _format_time(dt_str: str) -> str:
    """Extrahiert HH:MM aus einem ISO-Datetime-String, konvertiert in Berliner Zeit."""
    if not dt_str:
        return "?"
    try:
        # Verschiedene Formate abfangen
        for fmt in ["%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M%z",
                    "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"]:
            try:
                dt = datetime.strptime(dt_str[:25], fmt)
                if dt.tzinfo:
                    dt = dt.astimezone(BERLIN)
                return dt.strftime("%H:%M")
            except ValueError:
                continue
        return dt_str[11:16]  # Fallback: roher Substring
    except Exception:
        return dt_str[:5]


def _source_label(source: str) -> str:
    """Übersetzt source-Schlüssel in menschenlesbaren Label."""
    return {"work": "Arbeit", "private": "Privat", "study": "Study"}.get(source, source)


def parse_date_range(range_str: str) -> str:
    """
    Konvertiert einen Range-String in ein ISO-Datum.

    Unterstützte Werte:
        "today"        → heutiges Datum
        "tomorrow"     → morgiges Datum
        "YYYY-MM-DD"   → direkt zurückgeben

    Returns:
        ISO-Datum "YYYY-MM-DD"
    """
    from datetime import timedelta
    today = datetime.now(BERLIN).date()

    if range_str == "today":
        return today.isoformat()
    if range_str == "tomorrow":
        return (today + timedelta(days=1)).isoformat()
    if range_str in ("this_week", "week"):
        # Montag dieser Woche
        monday = today - timedelta(days=today.weekday())
        return f"week:{monday.isoformat()}"
    if range_str in ("next_week",):
        monday = today - timedelta(days=today.weekday()) + timedelta(weeks=1)
        return f"week:{monday.isoformat()}"
    if range_str in ("this_month", "month"):
        return f"month:{today.year}-{today.month:02d}"
    # Direktes Datum
    try:
        date.fromisoformat(range_str)
        return range_str
    except ValueError:
        logger.warning(f"calendar_utils: unbekannter range_str '{range_str}' → heute")
        return today.isoformat()


def date_label_for(date_str: str) -> str:
    """Gibt einen menschenlesbaren Datumslabel zurück (Heute / Morgen / Wochentag Datum)."""
    from datetime import timedelta
    today = datetime.now(BERLIN).date()
    target = date.fromisoformat(date_str)

    if target == today:
        return f"Heute, {_format_date_long(target)}"
    if target == today + timedelta(days=1):
        return f"Morgen, {_format_date_long(target)}"
    return _format_date_long(target)


def _format_date_long(d: date) -> str:
    """Formatiert ein Datum als 'Mittwoch, 15. März 2026'."""
    weekdays = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]
    months = ["", "Januar", "Februar", "März", "April", "Mai", "Juni",
              "Juli", "August", "September", "Oktober", "November", "Dezember"]
    return f"{weekdays[d.weekday()]}, {d.day}. {months[d.month]} {d.year}"
