"""
core/calendar/icloud_cal.py — iCloud CalDAV Wrapper

Liest und schreibt Termine über CalDAV (iCloud).
Authentifizierung: Apple ID + App-spezifisches Passwort.

App-Passwort generieren:
    https://appleid.apple.com → Sicherheit → App-Passwörter → "+" → Name: "SchnuBot"
    → generiertes Passwort in .env eintragen

Credentials in .env:
    ICLOUD_USERNAME=deine@apple.id
    ICLOUD_APP_PASSWORD=xxxx-xxxx-xxxx-xxxx
    ICLOUD_CALENDAR_NAMES=Zuhause,OSMI  (kommagetrennt, leer = alle)
"""

import os
import logging
from datetime import datetime, timezone, timedelta, date
import uuid

logger = logging.getLogger(__name__)

ICLOUD_CALDAV_URL = "https://caldav.icloud.com"


def _get_client():
    """Gibt einen authentifizierten CalDAV Client zurück."""
    import caldav

    username = os.getenv("ICLOUD_USERNAME", "")
    password = os.getenv("ICLOUD_APP_PASSWORD", "")

    if not username or not password:
        logger.error("iCloud CalDAV: ICLOUD_USERNAME oder ICLOUD_APP_PASSWORD nicht gesetzt")
        return None

    try:
        client = caldav.DAVClient(
            url=ICLOUD_CALDAV_URL,
            username=username,
            password=password,
        )
        return client
    except Exception as e:
        logger.error(f"iCloud CalDAV: Verbindung fehlgeschlagen: {e}")
        return None


def _get_calendars(client):
    """Gibt die konfigurierten Kalender zurück (gefiltert nach ICLOUD_CALENDAR_NAMES)."""
    try:
        principal = client.principal()
        all_calendars = principal.calendars()

        filter_names_raw = os.getenv("ICLOUD_CALENDAR_NAMES", "")
        if filter_names_raw.strip():
            filter_names = [n.strip().lower() for n in filter_names_raw.split(",")]
            return [
                cal for cal in all_calendars
                if cal.name and cal.name.lower() in filter_names
            ]

        return all_calendars

    except Exception as e:
        logger.error(f"iCloud CalDAV: Kalender laden fehlgeschlagen: {e}")
        return []


def list_events(date_str: str) -> list:
    """
    Gibt alle Termine eines Tages aus iCloud zurück.

    Args:
        date_str: ISO-Datum "YYYY-MM-DD"

    Returns:
        Liste von Event-Dicts mit: title, start, end, all_day, recurring,
        recurrence_label, event_id, source="private"
    """
    try:
        client = _get_client()
        if not client:
            return []

        calendars = _get_calendars(client)
        if not calendars:
            logger.warning("iCloud CalDAV: Keine Kalender gefunden")
            return []

        day = datetime.fromisoformat(date_str)
        start_dt = day.replace(hour=0, minute=0, second=0, tzinfo=timezone.utc)
        end_dt = day.replace(hour=23, minute=59, second=59, tzinfo=timezone.utc)

        events = []
        for calendar in calendars:
            try:
                results = calendar.date_search(start=start_dt, end=end_dt, expand=True)
                for event in results:
                    cal_name = calendar.get_display_name().lower()
                    parsed = _parse_event(event, cal_name)
                    if parsed:
                        events.append(parsed)
            except Exception as e:
                logger.warning(f"iCloud CalDAV: Fehler bei Kalender '{calendar.name}': {e}")
                continue

        return events

    except Exception as e:
        logger.error(f"iCloud CalDAV list_events fehlgeschlagen: {e}")
        return []


def create_event(title: str, start: str, end: str, description: str = "") -> dict:
    """
    Erstellt einen neuen Termin im ersten konfigurierten iCloud-Kalender.

    Args:
        title: Titel des Termins
        start: ISO-Datetime "YYYY-MM-DDTHH:MM"
        end: ISO-Datetime "YYYY-MM-DDTHH:MM"
        description: Optionale Beschreibung

    Returns:
        Dict mit event_id und title, oder {"error": "..."} bei Fehler
    """
    try:
        client = _get_client()
        if not client:
            return {"error": "iCloud CalDAV nicht verfügbar"}

        calendars = _get_calendars(client)
        if not calendars:
            return {"error": "Kein iCloud-Kalender gefunden"}

        target_calendar = calendars[0]
        event_id = str(uuid.uuid4())

        start_dt = _parse_dt(start)
        end_dt = _parse_dt(end)

        ical = _build_ical(event_id, title, start_dt, end_dt, description)
        target_calendar.add_event(ical)

        logger.info(f"iCloud Event erstellt: {title} ({event_id})")
        return {"event_id": event_id, "title": title}

    except Exception as e:
        logger.error(f"iCloud CalDAV create_event fehlgeschlagen: {e}")
        return {"error": str(e)}


def delete_event(event_id: str) -> dict:
    """
    Löscht einen Termin anhand seiner UID.

    Returns:
        {"ok": True} oder {"error": "..."}
    """
    try:
        client = _get_client()
        if not client:
            return {"error": "iCloud CalDAV nicht verfügbar"}

        calendars = _get_calendars(client)
        for calendar in calendars:
            try:
                event = calendar.event_by_uid(event_id)
                event.delete()
                logger.info(f"iCloud Event gelöscht: {event_id}")
                return {"ok": True}
            except Exception:
                continue

        return {"error": f"Event {event_id} nicht gefunden"}

    except Exception as e:
        logger.error(f"iCloud CalDAV delete_event fehlgeschlagen: {e}")
        return {"error": str(e)}


def update_event(event_id: str, title: str = None, start: str = None,
                 end: str = None, description: str = None) -> dict:
    """
    Aktualisiert einen bestehenden iCloud-Termin.

    Returns:
        {"ok": True, "title": ...} oder {"error": "..."}
    """
    try:
        client = _get_client()
        if not client:
            return {"error": "iCloud CalDAV nicht verfügbar"}

        calendars = _get_calendars(client)
        for calendar in calendars:
            try:
                event = calendar.event_by_uid(event_id)
                # iCal-Objekt parsen und patchen
                from icalendar import Calendar, Event
                cal_obj = Calendar.from_ical(event.data)

                for component in cal_obj.walk():
                    if component.name == "VEVENT":
                        if title:
                            component["SUMMARY"] = title
                        if start:
                            component["DTSTART"].dt = _parse_dt(start)
                        if end:
                            component["DTEND"].dt = _parse_dt(end)
                        if description is not None:
                            component["DESCRIPTION"] = description

                event.data = cal_obj.to_ical()
                event.save()
                logger.info(f"iCloud Event aktualisiert: {event_id}")
                return {"ok": True, "title": title or "(unverändert)"}
            except Exception:
                continue

        return {"error": f"Event {event_id} nicht gefunden"}

    except Exception as e:
        logger.error(f"iCloud CalDAV update_event fehlgeschlagen: {e}")
        return {"error": str(e)}


def list_calendar_names() -> list:
    """
    Utility: Gibt alle verfügbaren Kalender-Namen zurück.
    Einmalig aufrufen um ICLOUD_CALENDAR_NAMES korrekt zu setzen.
    """
    try:
        client = _get_client()
        if not client:
            return []
        principal = client.principal()
        return [cal.name for cal in principal.calendars() if cal.name]
    except Exception as e:
        logger.error(f"iCloud CalDAV list_calendar_names fehlgeschlagen: {e}")
        return []


# =============================================================================
# Interne Helpers
# =============================================================================

def _parse_event(event, calendar_name: str = "private") -> dict | None:
    """Normalisiert ein caldav Event-Objekt."""
    try:
        from icalendar import Calendar
        cal = Calendar.from_ical(event.data)

        for component in cal.walk():
            if component.name != "VEVENT":
                continue

            title = str(component.get("SUMMARY", "(Kein Titel)"))
            description = str(component.get("DESCRIPTION", ""))
            location = str(component.get("LOCATION", ""))
            uid = str(component.get("UID", ""))

            dtstart = component.get("DTSTART")
            dtend = component.get("DTEND")

            all_day = isinstance(dtstart.dt, date) and not isinstance(dtstart.dt, datetime)

            start = dtstart.dt.isoformat() if dtstart else ""
            end = dtend.dt.isoformat() if dtend else ""

            # Recurring Events
            rrule = component.get("RRULE")
            recurring = rrule is not None
            recurrence_label = _recurrence_label(rrule) if recurring else None

            return {
                "event_id":         uid,
                "title":            title,
                "start":            start,
                "end":              end,
                "all_day":          all_day,
                "recurring":        recurring,
                "recurrence_label": recurrence_label,
                "description":      description,
                "location":         location,
                "source":           "study" if "study" in calendar_name else "private",
            }
    except Exception as e:
        logger.warning(f"iCloud Event parsen fehlgeschlagen: {e}")
        return None


def _recurrence_label(rrule) -> str:
    """Leitet aus RRULE einen menschenlesbaren Label ab."""
    if not rrule:
        return "wiederkehrend"
    freq = rrule.get("FREQ", [""])[0].upper()
    mapping = {
        "DAILY":   "täglich",
        "WEEKLY":  "wöchentlich",
        "MONTHLY": "monatlich",
        "YEARLY":  "jährlich",
    }
    return mapping.get(freq, "wiederkehrend")


def _parse_dt(dt_str: str) -> datetime:
    """Parst einen ISO-Datetime-String mit Berliner Zeitzone."""
    from zoneinfo import ZoneInfo
    berlin = ZoneInfo("Europe/Berlin")
    # Format: YYYY-MM-DDTHH:MM oder YYYY-MM-DDTHH:MM:SS
    dt_str = dt_str[:16]  # auf HH:MM kürzen
    dt = datetime.fromisoformat(dt_str)
    return dt.replace(tzinfo=berlin)


def _build_ical(uid: str, title: str, start: datetime, end: datetime, description: str) -> str:
    """Baut einen minimalen iCal-String für einen neuen Termin."""
    now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    start_str = start.strftime("%Y%m%dT%H%M%S")
    end_str = end.strftime("%Y%m%dT%H%M%S")
    tz = "Europe/Berlin"

    return (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "PRODID:-//SchnuBot//Calendar//DE\r\n"
        "BEGIN:VEVENT\r\n"
        f"UID:{uid}\r\n"
        f"DTSTAMP:{now}\r\n"
        f"DTSTART;TZID={tz}:{start_str}\r\n"
        f"DTEND;TZID={tz}:{end_str}\r\n"
        f"SUMMARY:{title}\r\n"
        f"DESCRIPTION:{description}\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )
