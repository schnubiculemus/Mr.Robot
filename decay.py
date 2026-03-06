"""
SchnuBot.ai - Gewichts- und Confidence-Decay
Referenz: Phase 8 Plan, Fix 9.2

Chunks altern über Zeit:
- weight sinkt um 0.02 pro Woche wenn nicht bestätigt/abgerufen
- working_state Chunks werden nach 14 Tagen ohne Bestätigung archiviert
- Confidence sinkt um 0.01 pro Woche bei Chunks ohne Bestätigung

Fix 9.2: Decay ist jetzt INKREMENTELL statt kumulativ.
Vorher: Gesamtalter * Rate → auf aktuellen Wert angewandt → Doppel-Decay
Jetzt:  Nur die Zeit seit dem letzten Decay-Lauf wird berechnet.
        Neues Metadata-Feld 'last_decay_at' trackt den letzten Lauf.
        Bei Chunks ohne last_decay_at (Altbestand) wird ref_date verwendet
        und der erste Decay-Lauf setzt last_decay_at.

Wird vom Heartbeat aufgerufen (nach Deduplizierung).
"""

import logging
from datetime import datetime, timezone

from memory.memory_config import CONFIDENCE_GLOBAL_MIN
from core.datetime_utils import now_utc, safe_parse_dt, to_iso
from memory.memory_store import (
    get_active_collection,
    archive_chunk,
)

logger = logging.getLogger(__name__)

# Decay-Parameter
WEIGHT_MIN = 0.50
DECAY_WEIGHT_PER_WEEK = 0.02
DECAY_CONFIDENCE_PER_WEEK = 0.01
WORKING_STATE_MAX_AGE_DAYS = 14
DECAY_MIN_AGE_DAYS = 7  # Kein Decay in der ersten Woche


def run_decay(dry_run=False):
    """
    Wendet inkrementellen Decay auf alle aktiven Chunks an.

    Inkrementell: Berechnet nur den Decay für die Zeit seit dem letzten
    Decay-Lauf (last_decay_at), nicht vom Gesamtalter.

    confirm/update setzen last_confirmed_at zurück, was das Referenzdatum
    für die Erstberechnung verschiebt — bestätigte Chunks altern langsamer.

    Returns: Dict mit Stats {decayed, archived, skipped}
    """
    collection = get_active_collection()
    all_data = collection.get(include=["documents", "metadatas"])

    if not all_data["ids"]:
        return {"decayed": 0, "archived": 0, "skipped": 0}

    now = now_utc()
    now_iso = to_iso(now)
    stats = {"decayed": 0, "archived": 0, "skipped": 0}

    for i, chunk_id in enumerate(all_data["ids"]):
        meta = all_data["metadatas"][i]
        text = all_data["documents"][i]

        # --- Alter seit Erstellung/Bestätigung (für Archivierung + Min-Age) ---
        created = meta.get("created_at", "")
        last_confirmed = meta.get("last_confirmed_at", "")

        # Referenzdatum für Gesamtalter: last_confirmed_at > created_at
        ref_date_str = last_confirmed if last_confirmed else created
        ref_date = safe_parse_dt(ref_date_str)
        if ref_date is None:
            stats["skipped"] += 1
            continue

        age_days = (now - ref_date).days

        # Kein Decay in der ersten Woche nach Erstellung/Bestätigung
        if age_days < DECAY_MIN_AGE_DAYS:
            continue

        chunk_type = meta.get("chunk_type", "")
        weight = _safe_float(meta.get("weight", 1.0), 1.0)
        confidence = _safe_float(meta.get("confidence", 0.5), 0.5)

        # --- working_state: nach 14 Tagen archivieren ---
        if chunk_type == "working_state" and age_days >= WORKING_STATE_MAX_AGE_DAYS:
            if dry_run:
                logger.info(f"[DRY] ARCHIVIERE working_state ({age_days}d): {text[:60]}")
            else:
                try:
                    archive_chunk(chunk_id)
                    logger.info(f"DECAY-ARCHIV: working_state ({age_days}d) | {text[:60]}")
                except Exception as e:
                    logger.warning(f"Decay-Archivierung fehlgeschlagen: {e}")
                    continue
            stats["archived"] += 1
            continue

        # --- Inkrementeller Decay ---
        # Nur die Zeit seit dem letzten Decay-Lauf berechnen.
        # Bei Chunks ohne last_decay_at: erstes Mal → ab ref_date.
        last_decay_str = meta.get("last_decay_at", "")
        last_decay_dt = safe_parse_dt(last_decay_str) if last_decay_str else ref_date
        if last_decay_dt is None:
            last_decay_dt = ref_date

        delta_days = (now - last_decay_dt).total_seconds() / 86400

        # Mindestens 1 Tag seit letztem Decay, sonst überspringen
        if delta_days < 1.0:
            continue

        delta_weeks = delta_days / 7.0
        weight_decay = DECAY_WEIGHT_PER_WEEK * delta_weeks
        confidence_decay = DECAY_CONFIDENCE_PER_WEEK * delta_weeks

        new_weight = max(WEIGHT_MIN, weight - weight_decay)
        new_confidence = max(CONFIDENCE_GLOBAL_MIN, confidence - confidence_decay)

        # Nur updaten wenn sich etwas geändert hat (Schwelle: 0.005)
        weight_changed = abs(new_weight - weight) > 0.005
        confidence_changed = abs(new_confidence - confidence) > 0.005

        if not weight_changed and not confidence_changed:
            # Trotzdem last_decay_at setzen damit nächster Lauf korrekt rechnet
            if not last_decay_str:
                if not dry_run:
                    try:
                        meta_update = dict(meta)
                        meta_update["last_decay_at"] = now_iso
                        collection.update(ids=[chunk_id], metadatas=[meta_update])
                    except Exception as e:
                        logger.warning(f"Decay last_decay_at init fehlgeschlagen: {e}")
            continue

        if dry_run:
            logger.info(
                f"[DRY] DECAY [{chunk_type}] ({age_days}d, Δ{delta_days:.1f}d): "
                f"w {weight:.3f}→{new_weight:.3f} | c {confidence:.3f}→{new_confidence:.3f} | "
                f"{text[:50]}"
            )
        else:
            try:
                meta_update = dict(meta)
                meta_update["weight"] = round(new_weight, 4)
                meta_update["confidence"] = round(new_confidence, 4)
                meta_update["last_decay_at"] = now_iso

                collection.update(
                    ids=[chunk_id],
                    metadatas=[meta_update],
                )
                logger.info(
                    f"DECAY: [{chunk_type}] ({age_days}d, Δ{delta_days:.1f}d) "
                    f"w {weight:.3f}→{new_weight:.3f} c {confidence:.3f}→{new_confidence:.3f}"
                )
            except Exception as e:
                logger.warning(f"Decay-Update fehlgeschlagen: {e}")
                continue

        stats["decayed"] += 1

    logger.info(
        f"Decay abgeschlossen: {stats['decayed']} angepasst, "
        f"{stats['archived']} archiviert, {stats['skipped']} übersprungen"
    )
    return stats


def _safe_float(value, default):
    """Castet einen Wert sicher zu float. Fängt str-Altlasten ab."""
    try:
        return float(value)
    except (ValueError, TypeError):
        return default
