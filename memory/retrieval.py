"""
SchnuBot.ai - Retrieval Pipeline
Referenz: Konzeptdokument V1.1, Abschnitt 8

Score-basierte Chunk-Auswahl mit 6 Faktoren, Type Caps, Global Cap,
Fallback und Logging.
"""

import logging
import json
from datetime import datetime, timezone

from memory.memory_config import (
    RETRIEVAL_WEIGHTS,
    GLOBAL_MAX_CHUNKS,
    MIN_CHUNKS_IF_AVAILABLE,
    TYPE_CAPS,
    TYPE_FACTORS,
    TYPE_DECAY,
    EPISTEMIC_STATUS,
    RECENCY_HORIZON_DAYS,
    RECENCY_MINIMUM,
    WEIGHT_MAX,
    CONFIDENCE_MAX,
    FALLBACK_THRESHOLD_REDUCTION,
)
from memory.memory_store import query_active

logger = logging.getLogger(__name__)

# Separater Logger fuer Retrieval-Log (Abschnitt 15.1)
retrieval_logger = logging.getLogger("retrieval")
if not retrieval_logger.handlers:
    handler = logging.FileHandler("logs/retrieval.log")
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    retrieval_logger.addHandler(handler)
    retrieval_logger.setLevel(logging.INFO)


# =============================================================================
# Score-Berechnung (Abschnitt 8.2)
# =============================================================================

def compute_recency(created_at):
    """
    Linearer Recency-Faktor (Abschnitt 8.3).
    Gestern = 1.0, nach RECENCY_HORIZON_DAYS = RECENCY_MINIMUM.
    """
    created = datetime.fromisoformat(created_at)
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)

    age_days = (datetime.now(timezone.utc) - created).total_seconds() / 86400

    if age_days <= 0:
        return 1.0
    if age_days >= RECENCY_HORIZON_DAYS:
        return RECENCY_MINIMUM

    return 1.0 - (1.0 - RECENCY_MINIMUM) * (age_days / RECENCY_HORIZON_DAYS)


def compute_type_decay(chunk_type, created_at):
    """
    Typspezifische Alterung (Abschnitt 8.5).
    Nur fuer working_state, self_reflection, preference.
    Andere Typen geben 1.0 zurueck.
    """
    if chunk_type not in TYPE_DECAY:
        return 1.0

    decay_config = TYPE_DECAY[chunk_type]
    horizon = decay_config["horizon_days"]
    minimum = decay_config["minimum"]

    created = datetime.fromisoformat(created_at)
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)

    age_days = (datetime.now(timezone.utc) - created).total_seconds() / 86400

    if age_days <= 0:
        return 1.0
    if age_days >= horizon:
        return minimum

    return 1.0 - (1.0 - minimum) * (age_days / horizon)


def compute_score(chunk):
    """
    Berechnet den gewichteten Retrieval-Score fuer einen Chunk (Abschnitt 8.2).

    Erwartet chunk mit _semantic_similarity (aus memory_store.query_active).
    """
    w = RETRIEVAL_WEIGHTS

    # 1. Semantische Naehe (bereits normalisiert 0-1)
    semantic = chunk.get("_semantic_similarity", 0.0)

    # 2. Epistemic Status
    epistemic = EPISTEMIC_STATUS.get(chunk.get("epistemic_status", "stated"), 0.80)

    # 3. Weight (normalisiert auf 0-1 Bereich via WEIGHT_MAX)
    weight_raw = chunk.get("weight", 1.0)
    weight_norm = min(weight_raw / WEIGHT_MAX, 1.0)

    # 4. Recency
    recency = compute_recency(chunk.get("created_at", datetime.now(timezone.utc).isoformat()))

    # 5. Confidence (bereits 0-0.99)
    confidence = chunk.get("confidence", 0.5)

    # 6. Type Factor
    type_factor = TYPE_FACTORS.get(chunk.get("chunk_type", "hard_fact"), 0.80)

    # Type Decay anwenden (multiplikativ auf Recency)
    decay = compute_type_decay(chunk.get("chunk_type"), chunk.get("created_at", ""))
    recency_with_decay = recency * decay

    # Gewichteter Score
    score = (
        w["semantic"]    * semantic
        + w["epistemic"]   * epistemic
        + w["weight"]      * weight_norm
        + w["recency"]     * recency_with_decay
        + w["confidence"]  * confidence
        + w["type_factor"] * type_factor
    )

    return score, {
        "semantic": round(semantic, 3),
        "epistemic": round(epistemic, 3),
        "weight": round(weight_norm, 3),
        "recency": round(recency_with_decay, 3),
        "confidence": round(confidence, 3),
        "type_factor": round(type_factor, 3),
        "decay": round(decay, 3),
    }


# =============================================================================
# Selektion mit Caps (Abschnitt 8.1)
# =============================================================================

def apply_caps(scored_chunks):
    """
    Wendet Type Caps und Global Cap an.
    Gibt (selektierte, verworfene) zurueck.
    """
    # Nach Score absteigend sortieren
    scored_chunks.sort(key=lambda x: x["_retrieval_score"], reverse=True)

    type_counts = {}
    selected = []
    rejected = []

    for chunk in scored_chunks:
        ctype = chunk.get("chunk_type", "hard_fact")

        # Global Cap
        if len(selected) >= GLOBAL_MAX_CHUNKS:
            rejected.append((chunk, "global_cap"))
            continue

        # Type Cap
        cap = TYPE_CAPS.get(ctype, 5)
        current = type_counts.get(ctype, 0)
        if current >= cap:
            rejected.append((chunk, f"type_cap_{ctype}"))
            continue

        selected.append(chunk)
        type_counts[ctype] = current + 1

    return selected, rejected


# =============================================================================
# Hauptfunktion: Score + Select (Checkpoint 2.1 + 2.2 + 2.3)
# =============================================================================

def score_and_select(query, n_candidates=60):
    """
    Vollstaendige Retrieval-Pipeline:
    1. Semantische Suche in ChromaDB
    2. Score-Berechnung (6 Faktoren)
    3. Type Caps + Global Cap
    4. Fallback bei zu wenig Ergebnissen
    5. Logging

    Args:
        query: Suchanfrage (User-Nachricht)
        n_candidates: Wie viele Kandidaten aus ChromaDB holen

    Returns:
        Liste von Chunks mit _retrieval_score, sortiert nach Score
    """
    # 1. Kandidaten aus ChromaDB
    candidates = query_active(query, n_results=n_candidates)

    if not candidates:
        _log_retrieval(query, [], [], [], fallback=False, empty=True)
        return []

    # 2. Score berechnen
    for chunk in candidates:
        score, details = compute_score(chunk)
        chunk["_retrieval_score"] = round(score, 4)
        chunk["_score_details"] = details

    # 3. Caps anwenden
    selected, rejected = apply_caps(candidates)

    # 4. Fallback (Abschnitt 8.6)
    fallback_used = False
    if len(selected) < MIN_CHUNKS_IF_AVAILABLE and len(candidates) > len(selected):
        # Threshold senken: niedrigere Scores akzeptieren
        fallback_used = True
        # Alle Kandidaten nochmal ohne Caps, aber mit Global Cap
        selected = candidates[:GLOBAL_MAX_CHUNKS]
        rejected = [(c, "below_threshold") for c in candidates[GLOBAL_MAX_CHUNKS:]]

    # 5. Logging
    _log_retrieval(query, candidates, selected, rejected, fallback=fallback_used)

    return selected


# =============================================================================
# Retrieval-Log (Abschnitt 15.1)
# =============================================================================

def _log_retrieval(query, candidates, selected, rejected, fallback=False, empty=False):
    """Schreibt einen Retrieval-Log-Eintrag."""
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "query": query[:200],
        "candidates": len(candidates),
        "selected": len(selected),
        "fallback": fallback,
    }

    if empty:
        entry["note"] = "Keine Kandidaten gefunden"
        retrieval_logger.info(json.dumps(entry, ensure_ascii=False))
        return

    # Selektierte Chunks kompakt loggen
    entry["chunks"] = [
        {
            "id": c["id"][:8],
            "type": c.get("chunk_type"),
            "score": c.get("_retrieval_score"),
            "epistemic": c.get("epistemic_status"),
        }
        for c in selected[:10]  # Max 10 im Log
    ]

    # Verworfene Chunks mit Grund
    if rejected:
        entry["rejected_count"] = len(rejected)
        entry["rejected_reasons"] = list(set(r[1] for r in rejected))

    retrieval_logger.info(json.dumps(entry, ensure_ascii=False))
