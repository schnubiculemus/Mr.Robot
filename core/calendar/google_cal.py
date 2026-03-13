"""
core/calendar/google_cal.py — Google Calendar Wrapper

Liest und schreibt Termine über die Google Calendar API v3.
Authentifizierung: OAuth2 User-Credentials (token.json lokal generiert,
dann auf den Server kopiert).

Setup einmalig lokal:
    python3 setup_google_oauth.py
    scp token.json root@46.225.163.247:/opt/whatsapp-bot/data/

Credentials in .env:
    GOOGLE_CREDENTIALS_PATH=/opt/whatsapp-bot/data/credentials.json
    GOOGLE_TOKEN_PATH=/opt/whatsapp-bot/data/token.json
    GOOGLE_CALENDAR_ID=primary   (oder spezifische Kalender-ID)
"""

import os
import logging
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/calendar"]


def _get_service():
    """Gibt einen authentifizierten Google Calendar Service zurück."""
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build

    credentials_path = os.getenv("GOOGLE_CREDENTIALS_PATH", "data/credentials.json")
    token_path = os.getenv("GOOGLE_TOKEN_PATH", "data/token.json")

    creds = None

    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)

    # Token abgelaufen → automatisch refreshen (kein User-Eingriff nötig)
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            # Refreshtes Token speichern
            with open(token_path, "w") as f:
                f.write(creds.to_json())
            logger.info("Google Token refreshed")
        except Exception as e:
            logger.error(f"Google Token Refresh fehlgeschlagen: {e}")
            return None

    if not creds or not creds.valid:
        logger.error("Google Calendar: Keine gültigen Credentials. token.json fehlt oder ungültig.")
        return None

    return build("calendar", "v3", credentials=creds)


def list_events(date_str: str) -> list:
    """
    Gibt alle Termine eines Tages zurück.

    Args:
        date_str: ISO-Datum "YYYY-MM-DD"

    Returns:
        Liste von Event-Dicts mit: title, start, end, all_day, recurring,
        recurrence_label, event_id, source="work"
    """
    try:
        service = _get_service()
        if not service:
            return []

        calendar_id = os.getenv("GOOGLE_CALENDAR_ID", "primary")

        # Tages-Grenzen in UTC
        day = datetime.fromisoformat(date_str)
        time_min = day.replace(hour=0, minute=0, second=0, tzinfo=timezone.utc).isoformat()
        time_max = day.replace(hour=23, minute=59, second=59, tzinfo=timezone.utc).isoformat()

        result = service.events().list(
            calendarId=calendar_id,
            timeMin=time_min,
            timeMax=time_max,
            singleEvents=True,         # Recurring Events expandieren
            orderBy="startTime",
        ).execute()

        events = []
        for item in result.get("items", []):
            events.append(_parse_event(item))

        return events

    except Exception as e:
        logger.error(f"Google Calendar list_events fehlgeschlagen: {e}")
        return []


def create_event(title: str, start: str, end: str, description: str = "") -> dict:
    """
    Erstellt einen neuen Termin.

    Args:
        title: Titel des Termins
        start: ISO-Datetime "YYYY-MM-DDTHH:MM" (Berliner Zeit)
        end: ISO-Datetime "YYYY-MM-DDTHH:MM" (Berliner Zeit)
        description: Optionale Beschreibung

    Returns:
        Dict mit event_id und title, oder {"error": "..."} bei Fehler
    """
    try:
        service = _get_service()
        if not service:
            return {"error": "Google Calendar nicht verfügbar"}

        calendar_id = os.getenv("GOOGLE_CALENDAR_ID", "primary")
        tz = "Europe/Berlin"

        event_body = {
            "summary": title,
            "description": description,
            "start": {"dateTime": _ensure_tz(start), "timeZone": tz},
            "end": {"dateTime": _ensure_tz(end), "timeZone": tz},
        }

        created = service.events().insert(
            calendarId=calendar_id,
            body=event_body,
        ).execute()

        logger.info(f"Google Event erstellt: {title} ({created['id']})")
        return {"event_id": created["id"], "title": title}

    except Exception as e:
        logger.error(f"Google Calendar create_event fehlgeschlagen: {e}")
        return {"error": str(e)}


def delete_event(event_id: str) -> dict:
    """
    Löscht einen Termin anhand seiner ID.

    Returns:
        {"ok": True} oder {"error": "..."}
    """
    try:
        service = _get_service()
        if not service:
            return {"error": "Google Calendar nicht verfügbar"}

        calendar_id = os.getenv("GOOGLE_CALENDAR_ID", "primary")
        service.events().delete(calendarId=calendar_id, eventId=event_id).execute()
        logger.info(f"Google Event gelöscht: {event_id}")
        return {"ok": True}

    except Exception as e:
        logger.error(f"Google Calendar delete_event fehlgeschlagen: {e}")
        return {"error": str(e)}


def update_event(event_id: str, title: str = None, start: str = None,
                 end: str = None, description: str = None) -> dict:
    """
    Aktualisiert einen bestehenden Termin (nur übergebene Felder).

    Returns:
        {"ok": True, "title": ...} oder {"error": "..."}
    """
    try:
        service = _get_service()
        if not service:
            return {"error": "Google Calendar nicht verfügbar"}

        calendar_id = os.getenv("GOOGLE_CALENDAR_ID", "primary")
        tz = "Europe/Berlin"

        # Erst holen, dann patchen — nur geänderte Felder überschreiben
        event = service.events().get(calendarId=calendar_id, eventId=event_id).execute()

        if title:
            event["summary"] = title
        if start:
            event["start"] = {"dateTime": _ensure_tz(start), "timeZone": tz}
        if end:
            event["end"] = {"dateTime": _ensure_tz(end), "timeZone": tz}
        if description is not None:
            event["description"] = description

        updated = service.events().update(
            calendarId=calendar_id,
            eventId=event_id,
            body=event,
        ).execute()

        logger.info(f"Google Event aktualisiert: {event_id}")
        return {"ok": True, "title": updated.get("summary", "")}

    except Exception as e:
        logger.error(f"Google Calendar update_event fehlgeschlagen: {e}")
        return {"error": str(e)}


# =============================================================================
# Interne Helpers
# =============================================================================

def _parse_event(item: dict) -> dict:
    """Normalisiert ein Google API Event-Objekt."""
    start_raw = item.get("start", {})
    end_raw = item.get("end", {})

    all_day = "date" in start_raw and "dateTime" not in start_raw

    start = start_raw.get("dateTime", start_raw.get("date", ""))
    end = end_raw.get("dateTime", end_raw.get("date", ""))

    # Recurring Events erkennen
    recurrence = item.get("recurringEventId") is not None or bool(item.get("recurrence"))
    recurrence_label = _recurrence_label(item) if recurrence else None

    return {
        "event_id":        item.get("id", ""),
        "title":           item.get("summary", "(Kein Titel)"),
        "start":           start,
        "end":             end,
        "all_day":         all_day,
        "recurring":       recurrence,
        "recurrence_label": recurrence_label,
        "description":     item.get("description", ""),
        "location":        item.get("location", ""),
        "source":          "work",
    }


def _recurrence_label(item: dict) -> str:
    """Leitet aus RRULE einen menschenlesbaren Label ab."""
    rules = item.get("recurrence", [])
    for rule in rules:
        rule_upper = rule.upper()
        if "FREQ=DAILY" in rule_upper:
            return "täglich"
        if "FREQ=WEEKLY" in rule_upper:
            return "wöchentlich"
        if "FREQ=MONTHLY" in rule_upper:
            return "monatlich"
        if "FREQ=YEARLY" in rule_upper:
            return "jährlich"
    # Recurring Event ohne RRULE (ist eine Instanz einer Serie)
    return "wiederkehrend"


def _ensure_tz(dt_str: str) -> str:
    """Stellt sicher dass ein Datetime-String eine Timezone hat (+02:00 / +01:00)."""
    if "+" in dt_str or dt_str.endswith("Z"):
        return dt_str
    # Keine TZ → Berliner Zeit anhängen (vereinfacht, ohne DST-Check)
    return dt_str + ":00+01:00"
