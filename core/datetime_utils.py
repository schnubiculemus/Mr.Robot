"""
SchnuBot.ai - Datetime Utilities (Phase 9.4)

Zentrale, sichere Datetime-Funktionen für das gesamte Projekt.

Konventionen:
- Alle Timestamps intern als UTC-aware datetime
- Speicherung als ISO-8601 String (immer mit Timezone-Info)
- Display für Tommy: Europe/Berlin
- Parsing: tolerant gegenüber naiven Strings, "Z"-Suffix, kaputten Werten

Warum: Ein einziger kaputter Timestamp konnte bisher die gesamte
Retrieval-Pipeline oder den Heartbeat aushebeln. Diese Funktionen
stellen sicher, dass kaputte Einzelwerte übersprungen werden,
ohne das Gesamtsystem zu gefährden.
"""

import logging
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

# Berlin Timezone (UTC+1 / UTC+2 bei Sommerzeit)
# Vereinfacht ohne pytz/zoneinfo — reicht für Display-Zwecke.
# Falls verfügbar, wird zoneinfo genutzt.
try:
    from zoneinfo import ZoneInfo
    TZ_BERLIN = ZoneInfo("Europe/Berlin")
except ImportError:
    # Fallback: feste CET-Offset (ignoriert Sommerzeit)
    TZ_BERLIN = timezone(timedelta(hours=1))
    logger.warning("zoneinfo nicht verfügbar, Sommerzeit wird ignoriert")


def now_utc():
    """Gibt die aktuelle Zeit als UTC-aware datetime zurück."""
    return datetime.now(timezone.utc)


def now_berlin():
    """Gibt die aktuelle Zeit in Europe/Berlin zurück (für Display an Tommy)."""
    return datetime.now(TZ_BERLIN)


def safe_parse_dt(value, default=None):
    """
    Parst einen Datetime-String sicher zu einem UTC-aware datetime.

    Toleriert:
    - ISO-8601 mit und ohne Timezone
    - "Z"-Suffix (Zulu = UTC)
    - Naive Strings (werden als UTC interpretiert)
    - None, leere Strings, kaputte Werte → gibt default zurück

    Args:
        value:   String, datetime, oder None
        default: Rückgabewert bei Fehler (default: None)

    Returns:
        UTC-aware datetime oder default
    """
    if value is None:
        return default

    # Bereits ein datetime → nur Timezone sicherstellen
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    # String-Parsing
    if not isinstance(value, str) or not value.strip():
        return default

    text = value.strip()

    # "Z" durch "+00:00" ersetzen (fromisoformat versteht "Z" erst ab Python 3.11)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"

    try:
        dt = datetime.fromisoformat(text)
        # Naive → UTC annehmen
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        logger.warning(f"Datetime-Parse fehlgeschlagen: '{value[:50]}' — verwende Default")
        return default


def safe_age_days(value, default=0):
    """
    Berechnet das Alter in Tagen sicher.

    Args:
        value:   Timestamp-String oder datetime
        default: Rückgabewert bei Parse-Fehler

    Returns:
        Alter in Tagen (int) oder default
    """
    dt = safe_parse_dt(value)
    if dt is None:
        return default
    delta = now_utc() - dt
    return max(0, delta.days)


def to_iso(dt=None):
    """
    Konvertiert einen datetime zu ISO-8601 String.
    Ohne Argument: aktuelle UTC-Zeit.
    """
    if dt is None:
        dt = now_utc()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def format_berlin(dt=None, fmt="%A, %d. %B %Y, %H:%M Uhr"):
    """
    Formatiert einen datetime für Display an Tommy (Europe/Berlin).
    Ohne Argument: aktuelle Zeit.
    """
    if dt is None:
        dt = now_utc()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    berlin_dt = dt.astimezone(TZ_BERLIN)
    return berlin_dt.strftime(fmt)
