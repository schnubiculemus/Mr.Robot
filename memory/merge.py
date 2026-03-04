"""
SchnuBot.ai - Merge / Deduplizierung
Referenz: Konzeptdokument V1.1, Abschnitt 12

Erkennt und bereinigt semantische Duplikate in der aktiven Collection.
Kann als Einmal-Job oder periodisch im Heartbeat laufen.
"""

import logging
from datetime import datetime, timezone

from memory.memory_config import (
    MERGE_SIMILARITY_THRESHOLD,
    MERGE_MAX_CANDIDATES,
    WEIGHT_MAX,
)
from memory.memory_store import (
    get_active_collection,
    find_merge_candidates,
    archive_chunk,
    embed_text,
)
from memory.chunk_schema import chunk_to_metadata, metadata_to_tags

logger = logging.getLogger(__name__)


def deduplicate_active(dry_run=False):
    """
    Scannt alle aktiven Chunks auf semantische Duplikate.
    Bei Duplikaten (Similarity >= MERGE_SIMILARITY_THRESHOLD):
    - Behaelt den staerkeren Chunk (hoehere weight * confidence)
    - Archiviert den schwaecheren

    Args:
        dry_run: Wenn True, nur loggen ohne zu archivieren

    Returns:
        Anzahl archivierter Duplikate
    """
    collection = get_active_collection()
    all_data = collection.get(include=["documents", "metadatas"])

    if not all_data["ids"]:
        logger.info("Deduplizierung: Keine Chunks vorhanden")
        return 0

    total = len(all_data["ids"])
    logger.info(f"Deduplizierung: Pruefe {total} Chunks auf Duplikate...")

    # Chunks als Dicts aufbauen
    chunks = []
    for i, chunk_id in enumerate(all_data["ids"]):
        meta = all_data["metadatas"][i]
        chunks.append({
            "id": chunk_id,
            "text": all_data["documents"][i],
            "chunk_type": meta.get("chunk_type", ""),
            "source": meta.get("source", ""),
            "weight": float(meta.get("weight", 1.0)),
            "confidence": float(meta.get("confidence", 0.5)),
            "epistemic_status": meta.get("epistemic_status", "stated"),
            "created_at": meta.get("created_at", ""),
        })

    # Bereits archivierte IDs tracken (nicht doppelt pruefen)
    archived_ids = set()
    archived_count = 0

    for chunk in chunks:
        if chunk["id"] in archived_ids:
            continue

        # Merge-Kandidaten suchen (gleicher Typ, hohe Similarity)
        candidates = find_merge_candidates(
            chunk["text"],
            chunk["chunk_type"],
            exclude_id=chunk["id"],
        )

        for candidate in candidates:
            if candidate["id"] in archived_ids:
                continue

            similarity = candidate.get("_merge_similarity", 0)
            if similarity < MERGE_SIMILARITY_THRESHOLD:
                continue

            # Staerke vergleichen: weight * confidence
            chunk_strength = chunk["weight"] * chunk["confidence"]
            candidate_strength = candidate.get("weight", 1.0) * candidate.get("confidence", 0.5)

            # Schwaecheren archivieren
            if chunk_strength >= candidate_strength:
                loser_id = candidate["id"]
                loser_text = candidate.get("text", "")[:60]
                winner_id = chunk["id"]
            else:
                loser_id = chunk["id"]
                loser_text = chunk["text"][:60]
                winner_id = candidate["id"]

            if dry_run:
                logger.info(
                    f"DUPLIKAT (dry-run): {loser_id[:8]} -> archivieren | "
                    f"Similarity: {similarity:.3f} | Winner: {winner_id[:8]} | "
                    f"{loser_text}"
                )
            else:
                try:
                    archive_chunk(loser_id)
                    logger.info(
                        f"DUPLIKAT archiviert: {loser_id[:8]} | "
                        f"Similarity: {similarity:.3f} | Winner: {winner_id[:8]} | "
                        f"{loser_text}"
                    )
                except Exception as e:
                    logger.error(f"Deduplizierung: Archivierung fehlgeschlagen fuer {loser_id[:8]}: {e}")
                    continue

            archived_ids.add(loser_id)
            archived_count += 1

            # Wenn der aktuelle Chunk der Verlierer war, nicht weiter pruefen
            if loser_id == chunk["id"]:
                break

    logger.info(
        f"Deduplizierung abgeschlossen: {archived_count} Duplikate "
        f"{'gefunden (dry-run)' if dry_run else 'archiviert'} von {total} Chunks"
    )
    return archived_count


def run_dedup_report():
    """
    Dry-Run Deduplizierung — zeigt Duplikate ohne zu aendern.
    Nuetzlich fuer Debugging und Tuning des Thresholds.
    """
    return deduplicate_active(dry_run=True)
