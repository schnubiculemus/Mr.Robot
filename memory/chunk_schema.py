"""
SchnuBot.ai - Memory Chunk Schema
Referenz: Konzeptdokument V1.1, Abschnitt 4

Chunk-Datenmodell mit Validierung, UUID-Generierung, Timestamp-Handling.
Jeder Chunk ist ein Dict - kein ORM, kein Dataclass-Overhead.
Validierung explizit ueber validate_chunk().
"""

import uuid
from datetime import datetime, timezone

from memory.memory_config import (
    CHUNK_TYPES,
    VALID_SOURCES,
    EPISTEMIC_STATUS,
    CONFIDENCE_THRESHOLDS,
    CONFIDENCE_GLOBAL_MIN,
    CONFIDENCE_MAX,
    WEIGHT_BASELINES,
    WEIGHT_MAX,
    TAGS_MAX_PER_CHUNK,
)


# =============================================================================
# Chunk erzeugen
# =============================================================================

def create_chunk(
    text,
    chunk_type,
    source,
    confidence,
    epistemic_status,
    tags=None,
    weight=None,
    supersedes=None,
    expires_at=None,
    replies_to=None,
):
    """
    Erzeugt einen neuen Memory-Chunk als Dict.

    Args:
        text:             Verdichteter Inhalt (Pflicht)
        chunk_type:       Einer der 6 Typen
        source:           tommy | robot | shared
        confidence:       Float 0.0-0.99 (Extraktionssicherheit)
        epistemic_status: confirmed | stated | inferred | speculative | outdated
        tags:             Liste von Tags (max 5, kebab-case)
        weight:           Float (optional, sonst Typ-Baseline)
        supersedes:       UUID des ersetzten Chunks (optional)
        expires_at:       ISO-Timestamp (optional)

    Returns:
        Dict mit allen Pflicht- und optionalen Feldern
    """
    now = datetime.now(timezone.utc).isoformat()

    chunk = {
        "id": str(uuid.uuid4()),
        "text": text.strip(),
        "source": source,
        "chunk_type": chunk_type,
        "created_at": now,
        "status": "active",
        "weight": weight if weight is not None else WEIGHT_BASELINES.get(chunk_type, 1.0),
        "confidence": confidence,
        "epistemic_status": epistemic_status,
        "tags": sanitize_tags(tags or []),
    }

    # Optionale Felder - nur setzen wenn vorhanden
    if supersedes:
        chunk["supersedes"] = supersedes
    if expires_at:
        chunk["expires_at"] = expires_at
    if replies_to:
        chunk["replies_to"] = replies_to

    return chunk


# =============================================================================
# Validierung
# =============================================================================

def validate_chunk(chunk):
    """
    Validiert einen Chunk gegen das Schema.
    Gibt (True, None) oder (False, Fehlermeldung) zurueck.
    """
    errors = []

    # Pflichtfelder pruefen
    required = ["id", "text", "source", "chunk_type", "created_at",
                 "status", "weight", "confidence", "epistemic_status", "tags"]

    for field in required:
        if field not in chunk:
            errors.append(f"Pflichtfeld fehlt: {field}")

    if errors:
        return False, "; ".join(errors)

    # Text nicht leer
    if not chunk["text"] or not chunk["text"].strip():
        errors.append("text darf nicht leer sein")

    # chunk_type
    if chunk["chunk_type"] not in CHUNK_TYPES:
        errors.append(f"Ungueltiger chunk_type: {chunk['chunk_type']} (erlaubt: {CHUNK_TYPES})")

    # source
    if chunk["source"] not in VALID_SOURCES:
        errors.append(f"Ungueltige source: {chunk['source']} (erlaubt: {VALID_SOURCES})")

    # status
    if chunk["status"] not in ("active", "archived"):
        errors.append(f"Ungueltiger status: {chunk['status']}")

    # epistemic_status
    if chunk["epistemic_status"] not in EPISTEMIC_STATUS:
        errors.append(f"Ungueltiger epistemic_status: {chunk['epistemic_status']}")

    # confidence: Float 0.0 - 0.99
    conf = chunk["confidence"]
    if not isinstance(conf, (int, float)):
        errors.append(f"confidence muss numerisch sein, ist {type(conf)}")
    elif conf < 0.0 or conf > CONFIDENCE_MAX:
        errors.append(f"confidence ausserhalb 0.0-{CONFIDENCE_MAX}: {conf}")

    # Confidence-Schwelle pro Typ pruefen
    ctype = chunk["chunk_type"]
    if ctype in CONFIDENCE_THRESHOLDS and isinstance(conf, (int, float)):
        threshold = CONFIDENCE_THRESHOLDS[ctype]
        if conf < threshold:
            # Ausnahme: decision bei expliziter Festlegung -> immer erlaubt
            # Das muss der Aufrufer entscheiden, hier nur Warnung
            errors.append(
                f"confidence {conf} unter Schwelle {threshold} fuer Typ {ctype}"
            )

    # weight
    w = chunk["weight"]
    if not isinstance(w, (int, float)):
        errors.append(f"weight muss numerisch sein, ist {type(w)}")
    elif w > WEIGHT_MAX:
        errors.append(f"weight ueber Maximum {WEIGHT_MAX}: {w}")

    # tags
    tags = chunk["tags"]
    if not isinstance(tags, list):
        errors.append("tags muss eine Liste sein")
    elif len(tags) > TAGS_MAX_PER_CHUNK:
        errors.append(f"Zu viele Tags: {len(tags)} (max {TAGS_MAX_PER_CHUNK})")
    else:
        for tag in tags:
            if not _is_valid_tag(tag):
                errors.append(f"Ungueltiger Tag: '{tag}' (kleinbuchstaben, kebab-case)")

    if errors:
        return False, "; ".join(errors)

    return True, None


# =============================================================================
# Chunk-Aktionen (Weight/Confidence Updates)
# =============================================================================

def apply_confirm(chunk):
    """Confirm-Aktion: weight +0.05, confidence +0.03 (Abschnitt 10/11)."""
    chunk["weight"] = min(chunk["weight"] + 0.05, WEIGHT_MAX)
    chunk["confidence"] = min(chunk["confidence"] + 0.03, CONFIDENCE_MAX)
    chunk["last_confirmed_at"] = datetime.now(timezone.utc).isoformat()
    return chunk


def apply_update(chunk, new_confidence_from_model):
    """Update-Aktion: weight +0.02, confidence blended (Abschnitt 10/11)."""
    chunk["weight"] = min(chunk["weight"] + 0.02, WEIGHT_MAX)
    chunk["confidence"] = min(
        0.7 * chunk["confidence"] + 0.3 * new_confidence_from_model,
        CONFIDENCE_MAX,
    )
    return chunk


def apply_archive(chunk):
    """Archiviert einen Chunk (Abschnitt 7.2)."""
    chunk["status"] = "archived"
    return chunk


def apply_confidence_correction(chunk, reduction):
    """
    Sofort-Korrektur bei Widerspruch (Abschnitt 13.3.2).
    Max -0.20, nicht unter CONFIDENCE_GLOBAL_MIN.
    """
    max_reduction = 0.20
    actual_reduction = min(abs(reduction), max_reduction)
    chunk["confidence"] = max(
        chunk["confidence"] - actual_reduction,
        CONFIDENCE_GLOBAL_MIN,
    )
    return chunk


# =============================================================================
# Hilfsfunktionen
# =============================================================================

def sanitize_tags(tags):
    """Bereinigt und validiert Tags (Abschnitt 14)."""
    clean = []
    seen = set()
    for tag in tags:
        tag = tag.lower().strip()
        # Nur erlaubte Zeichen: kleinbuchstaben, ziffern, bindestrich
        if not tag or not _is_valid_tag(tag):
            continue
        if tag not in seen and len(clean) < TAGS_MAX_PER_CHUNK:
            clean.append(tag)
            seen.add(tag)
    return clean


def _is_valid_tag(tag):
    """Prueft ob ein Tag dem Format entspricht: kleinbuchstaben, kebab-case."""
    if not tag:
        return False
    return all(c.isalnum() or c == "-" for c in tag) and tag == tag.lower()


def chunk_age_days(chunk):
    """Berechnet das Alter eines Chunks in Tagen. Safe bei kaputtem Timestamp."""
    from core.datetime_utils import safe_age_days
    return safe_age_days(chunk.get("created_at", ""), default=0)


def chunk_to_metadata(chunk):
    """
    Extrahiert die Metadaten fuer ChromaDB.
    ChromaDB speichert: text als document, alles andere als metadata.
    """
    meta = {
        "chunk_type": chunk["chunk_type"],
        "source": chunk["source"],
        "status": chunk["status"],
        "weight": chunk["weight"],
        "confidence": chunk["confidence"],
        "epistemic_status": chunk["epistemic_status"],
        "created_at": chunk["created_at"],
        "tags": ",".join(chunk["tags"]),  # ChromaDB: kein List-Support in metadata
    }
    if chunk.get("replies_to"):
        meta["replies_to"] = chunk["replies_to"]
    if chunk.get("supersedes"):
        meta["supersedes"] = chunk["supersedes"]
    return meta


def metadata_to_tags(tags_str):
    """Wandelt den komma-separierten Tag-String aus ChromaDB zurueck in eine Liste."""
    if not tags_str:
        return []
    if isinstance(tags_str, list):
        return [str(t).strip() for t in tags_str if str(t).strip()]
    return [t.strip() for t in str(tags_str).split(",") if t.strip()]
