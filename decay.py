"""
SchnuBot.ai - Gewichts- und Confidence-Decay
Referenz: Phase 8 Plan

Chunks altern über Zeit:
- weight sinkt um 0.02 pro Woche wenn nicht bestätigt/abgerufen
- working_state Chunks werden nach 14 Tagen ohne Bestätigung archiviert
- Confidence sinkt um 0.01 pro Woche bei Chunks ohne Bestätigung

Wird vom Heartbeat aufgerufen (nach Deduplizierung).
"""

import logging
from datetime import datetime, timezone, timedelta

from memory.memory_config import CONFIDENCE_GLOBAL_MIN
from memory.memory_store import (
    get_active_collection,
    update_chunk,
    archive_chunk,
    get_chunk,
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
    Wendet Decay auf alle aktiven Chunks an.
    
    Returns: Dict mit Stats {decayed, archived, skipped}
    """
    collection = get_active_collection()
    all_data = collection.get(include=["documents", "metadatas"])

    if not all_data["ids"]:
        return {"decayed": 0, "archived": 0, "skipped": 0}

    now = datetime.now(timezone.utc)
    stats = {"decayed": 0, "archived": 0, "skipped": 0}

    for i, chunk_id in enumerate(all_data["ids"]):
        meta = all_data["metadatas"][i]
        text = all_data["documents"][i]

        # Alter berechnen
        created = meta.get("created_at", "")
        last_confirmed = meta.get("last_confirmed_at", "")

        # Referenzdatum: last_confirmed_at wenn vorhanden, sonst created_at
        ref_date_str = last_confirmed if last_confirmed else created
        if not ref_date_str:
            stats["skipped"] += 1
            continue

        try:
            ref_date = datetime.fromisoformat(ref_date_str.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            stats["skipped"] += 1
            continue

        age_days = (now - ref_date).days

        # Kein Decay in der ersten Woche
        if age_days < DECAY_MIN_AGE_DAYS:
            continue

        chunk_type = meta.get("chunk_type", "")
        weight = float(meta.get("weight", 1.0))
        confidence = float(meta.get("confidence", 0.5))

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

        # --- Decay berechnen ---
        weeks = age_days / 7.0
        weight_decay = DECAY_WEIGHT_PER_WEEK * weeks
        confidence_decay = DECAY_CONFIDENCE_PER_WEEK * weeks

        new_weight = max(WEIGHT_MIN, weight - weight_decay)
        new_confidence = max(CONFIDENCE_GLOBAL_MIN, confidence - confidence_decay)

        # Nur updaten wenn sich etwas geändert hat (Schwelle: 0.005)
        weight_changed = abs(new_weight - weight) > 0.005
        confidence_changed = abs(new_confidence - confidence) > 0.005

        if not weight_changed and not confidence_changed:
            continue

        if dry_run:
            logger.info(
                f"[DRY] DECAY [{chunk_type}] ({age_days}d): "
                f"w {weight:.2f}→{new_weight:.2f} | c {confidence:.2f}→{new_confidence:.2f} | "
                f"{text[:50]}"
            )
        else:
            try:
                # Direkt in ChromaDB updaten (ohne Re-Embedding)
                meta_update = dict(meta)
                meta_update["weight"] = str(round(new_weight, 4))
                meta_update["confidence"] = str(round(new_confidence, 4))

                collection.update(
                    ids=[chunk_id],
                    metadatas=[meta_update],
                )
                logger.info(
                    f"DECAY: [{chunk_type}] ({age_days}d) "
                    f"w {weight:.2f}→{new_weight:.2f} c {confidence:.2f}→{new_confidence:.2f}"
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
