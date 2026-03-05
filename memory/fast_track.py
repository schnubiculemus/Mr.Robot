"""
SchnuBot.ai - Fast-Track Konsolidierung (Sofortspeicherung)
Referenz: Konzeptdokument V1.1, Abschnitt 13.3.1

Erkennt explizite Decisions und Hard Facts waehrend des Gespraechs
und speichert sie sofort als Chunks, ohne auf den Heartbeat zu warten.
Heartbeat kann spaeter nachkorrigieren.
"""

import re
import logging
from datetime import datetime, timezone

from memory.memory_config import (
    FAST_TRACK_MAX_PER_CHAT,
    FAST_TRACK_CONFIDENCE_PENALTY,
    WEIGHT_BASELINES,
    CONFIDENCE_THRESHOLDS,
)
from memory.chunk_schema import create_chunk, validate_chunk, sanitize_tags
from memory.memory_store import store_chunk

logger = logging.getLogger(__name__)

# =============================================================================
# Erkennungsmuster (Abschnitt 13.3.1)
# =============================================================================

# Decision-Signale: nur explizite, unmissverständliche Festlegungen
_DECISION_PATTERNS = [
    re.compile(r"\bab jetzt\b", re.I),
    re.compile(r"\bab sofort\b", re.I),
    re.compile(r"\bwir machen das so\b", re.I),
    re.compile(r"\bist entschieden\b", re.I),
    re.compile(r"\bsteht fest\b", re.I),
    re.compile(r"\bich habe entschieden\b", re.I),
]

# Hard-Fact-Signale: nur direkte Aufforderungen zur Speicherung
_HARD_FACT_PATTERNS = [
    re.compile(r"\bmerk dir\b", re.I),
    re.compile(r"\bspeicher dir\b", re.I),
    re.compile(r"\bich hei(ss|ß)e\b", re.I),
    re.compile(r"\bich arbeite bei\b", re.I),
    re.compile(r"\bich wohne in\b", re.I),
]


# =============================================================================
# Session-Tracker (zaehlt Fast-Tracks pro Chat)
# =============================================================================

# Einfacher In-Memory-Counter, wird bei Bot-Neustart zurueckgesetzt.
# Das ist okay — das Limit ist pro Chat-Session, nicht persistent.
_session_counts = {}


def _get_session_key(user_id):
    """Erzeugt einen tagesbasierten Session-Key."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return f"{user_id}_{today}"


def _get_count(user_id):
    """Gibt aktuelle Fast-Track-Anzahl fuer diesen User heute zurueck."""
    key = _get_session_key(user_id)
    return _session_counts.get(key, 0)


def _increment_count(user_id):
    """Erhoeht den Fast-Track-Zaehler."""
    key = _get_session_key(user_id)
    _session_counts[key] = _session_counts.get(key, 0) + 1


# =============================================================================
# Erkennung
# =============================================================================

def detect_fast_track(user_message):
    """
    Prueft ob eine User-Nachricht ein Fast-Track-Kandidat ist.

    Returns:
        ("decision", matched_pattern) oder ("hard_fact", matched_pattern) oder None
    """
    for pattern in _DECISION_PATTERNS:
        match = pattern.search(user_message)
        if match:
            return "decision", match.group()

    for pattern in _HARD_FACT_PATTERNS:
        match = pattern.search(user_message)
        if match:
            return "hard_fact", match.group()

    return None


# =============================================================================
# Sofortspeicherung
# =============================================================================

def process_fast_track(user_id, user_message):
    """
    Prueft und verarbeitet eine Nachricht fuer Fast-Track.

    Args:
        user_id: User-ID
        user_message: Die Nachricht des Users

    Returns:
        chunk_id wenn gespeichert, sonst None
    """
    # Limit pruefen
    if _get_count(user_id) >= FAST_TRACK_MAX_PER_CHAT:
        return None

    # Erkennung
    result = detect_fast_track(user_message)
    if result is None:
        return None

    chunk_type, matched = result
    logger.info(f"Fast-Track erkannt: [{chunk_type}] Trigger: '{matched}'")

    # Confidence: konservativ (Baseline - Penalty)
    # TODO Roadmap: max(base, base + 0.05) ist sinnlos, base + 0.05 ist immer größer.
    # Vermutlich gemeint: base + 0.05 als kleiner Boost, dann minus Penalty.
    # Fix: confidence = base_confidence + 0.05 - FAST_TRACK_CONFIDENCE_PENALTY
    base_confidence = CONFIDENCE_THRESHOLDS.get(chunk_type, 0.75)
    confidence = max(base_confidence, base_confidence + 0.05) - FAST_TRACK_CONFIDENCE_PENALTY

    # Chunk erzeugen
    # Text ist die ganze Nachricht, verdichtet — bei Fast-Track
    # nehmen wir die User-Nachricht direkt, der Heartbeat kann spaeter verfeinern.
    chunk = create_chunk(
        text=user_message.strip(),
        chunk_type=chunk_type,
        source="tommy",
        confidence=confidence,
        epistemic_status="stated",
        tags=["fast-track"],
    )

    # Validieren
    valid, error = validate_chunk(chunk)
    if not valid:
        logger.warning(f"Fast-Track: Chunk ungueltig: {error}")
        return None

    # PII-Check (gleiche Logik wie Konsolidierer)
    try:
        from memory.consolidator import _contains_sensitive_data
        if _contains_sensitive_data(user_message):
            logger.warning(f"Fast-Track: PII erkannt, uebersprungen")
            return None
    except ImportError:
        pass

    # Speichern
    try:
        store_chunk(chunk)
        _increment_count(user_id)
        logger.info(
            f"Fast-Track gespeichert: [{chunk_type}] conf={confidence} "
            f"({_get_count(user_id)}/{FAST_TRACK_MAX_PER_CHAT}) | {user_message[:80]}"
        )
        return chunk["id"]
    except Exception as e:
        logger.error(f"Fast-Track Speicherfehler: {e}")
        return None
