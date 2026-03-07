"""
SchnuBot.ai - Memory Dashboard
Web-UI für Chunk Explorer, Retrieval Inspector, Konsolidierer und Fast-Track.

Läuft als eigener Flask-Server auf Port 5001.
Liest direkt aus ChromaDB und Log-Files — kein Schreibzugriff auf produktive Daten.

Start: python dashboard.py
URL:   http://localhost:5001
"""

import os
import sys
import json
import logging
from datetime import datetime, timezone, timedelta
from flask import Flask, render_template, jsonify, request

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_DIR)

# HF offline — kein Download beim Dashboard-Start
os.environ["HF_HUB_OFFLINE"] = "1"

from memory.memory_store import (
    get_active_collection,
    get_archive_collection,
    get_stats,
    embed_query,
)
from memory.memory_config import CHUNK_TYPES
from core.datetime_utils import now_utc, now_berlin, safe_parse_dt

logging.basicConfig(level=logging.INFO, format="%(asctime)s [DASHBOARD] %(message)s")
logger = logging.getLogger(__name__)

app = Flask(
    __name__,
    template_folder=os.path.join(PROJECT_DIR, "dashboard", "templates"),
    static_folder=os.path.join(PROJECT_DIR, "dashboard", "static"),
)


# =============================================================================
# Helpers
# =============================================================================

def _chunk_from_collection(collection, include_embeddings=False):
    """Lädt alle Chunks aus einer Collection als Liste von Dicts."""
    includes = ["documents", "metadatas"]
    if include_embeddings:
        includes.append("embeddings")

    data = collection.get(include=includes)
    chunks = []

    for i, chunk_id in enumerate(data["ids"]):
        meta = data["metadatas"][i]
        text = data["documents"][i]

        try:
            weight = float(meta.get("weight", 1.0))
            confidence = float(meta.get("confidence", 0.5))
        except (ValueError, TypeError):
            weight, confidence = 1.0, 0.5

        chunk = {
            "id": chunk_id,
            "text": text,
            "chunk_type": meta.get("chunk_type", "unknown"),
            "source": meta.get("source", "unknown"),
            "status": meta.get("status", "active"),
            "weight": round(weight, 4),
            "confidence": round(confidence, 4),
            "epistemic_status": meta.get("epistemic_status", "stated"),
            "created_at": meta.get("created_at", ""),
            "tags": meta.get("tags", ""),
            "supersedes": meta.get("supersedes", ""),
            "last_confirmed_at": meta.get("last_confirmed_at", ""),
            "last_decay_at": meta.get("last_decay_at", ""),
        }
        chunks.append(chunk)

    return chunks


def _parse_tags(tags_str):
    """Parst Tags-String zu Liste."""
    if not tags_str:
        return []
    return [t.strip() for t in tags_str.split(",") if t.strip()]


def _age_str(iso_str):
    """Gibt menschenlesbares Alter zurück."""
    dt = safe_parse_dt(iso_str)
    if not dt:
        return "?"
    delta = now_utc() - dt
    days = delta.days
    hours = delta.seconds // 3600
    if days > 0:
        return f"{days}d"
    return f"{hours}h"


# =============================================================================
# Pages
# =============================================================================

@app.route("/")
def index():
    """Übersicht — Stats und Schnellblick."""
    return render_template("index.html")


@app.route("/chunks")
def chunks_page():
    """Chunk Explorer."""
    return render_template("chunks.html")


@app.route("/retrieval")
def retrieval_page():
    """Retrieval Inspector."""
    return render_template("retrieval.html")


# =============================================================================
# API: Stats
# =============================================================================

@app.route("/api/stats")
def api_stats():
    """Übersicht-Statistiken."""
    stats = get_stats()
    active = get_active_collection()
    all_data = active.get(include=["metadatas"])

    today_str = now_utc().strftime("%Y-%m-%d")
    today_count = 0
    by_type = {}
    by_source = {}
    by_epistemic = {}

    for meta in all_data["metadatas"]:
        ct = meta.get("chunk_type", "unknown")
        by_type[ct] = by_type.get(ct, 0) + 1

        src = meta.get("source", "unknown")
        by_source[src] = by_source.get(src, 0) + 1

        ep = meta.get("epistemic_status", "unknown")
        by_epistemic[ep] = by_epistemic.get(ep, 0) + 1

        created = meta.get("created_at", "")
        if created[:10] == today_str:
            today_count += 1

    return jsonify({
        "active_chunks": stats["active_count"],
        "archived_chunks": stats["archive_count"],
        "total_chunks": stats["total_count"],
        "today_count": today_count,
        "by_type": by_type,
        "by_source": by_source,
        "by_epistemic": by_epistemic,
        "timestamp": now_berlin().strftime("%d.%m.%Y %H:%M"),
    })


# =============================================================================
# API: Chunks
# =============================================================================

@app.route("/api/chunks")
def api_chunks():
    """Alle Chunks mit Filtern."""
    collection_name = request.args.get("collection", "active")
    chunk_type = request.args.get("type", "")
    source = request.args.get("source", "")
    status = request.args.get("status", "")
    search = request.args.get("search", "")
    tag_filter = request.args.get("tag", "")

    if collection_name == "archive":
        collection = get_archive_collection()
    else:
        collection = get_active_collection()

    chunks = _chunk_from_collection(collection)

    # Filter anwenden
    if chunk_type:
        chunks = [c for c in chunks if c["chunk_type"] == chunk_type]
    if source:
        chunks = [c for c in chunks if c["source"] == source]
    if status:
        chunks = [c for c in chunks if c["status"] == status]
    if search:
        search_lower = search.lower()
        chunks = [c for c in chunks if search_lower in c["text"].lower() or search_lower in c["id"].lower()]
    if tag_filter:
        tag_lower = tag_filter.lower()
        chunks = [c for c in chunks if tag_lower in c["tags"].lower()]

    # Sortierung: neueste zuerst
    chunks.sort(key=lambda c: c.get("created_at", ""), reverse=True)

    # Aufbereiten
    for c in chunks:
        c["tags_list"] = _parse_tags(c["tags"])
        c["age"] = _age_str(c["created_at"])
        c["text_preview"] = c["text"][:120] + "..." if len(c["text"]) > 120 else c["text"]
        c["created_short"] = c["created_at"][:16].replace("T", " ") if c["created_at"] else "?"

    return jsonify({"chunks": chunks, "total": len(chunks)})


@app.route("/api/chunks/<chunk_id>")
def api_chunk_detail(chunk_id):
    """Einzelner Chunk mit allen Details."""
    collection = get_active_collection()
    try:
        result = collection.get(ids=[chunk_id], include=["documents", "metadatas"])
        if not result["ids"]:
            # In Archive suchen
            archive = get_archive_collection()
            result = archive.get(ids=[chunk_id], include=["documents", "metadatas"])
            if not result["ids"]:
                return jsonify({"error": "Chunk nicht gefunden"}), 404

        meta = result["metadatas"][0]
        text = result["documents"][0]

        try:
            weight = float(meta.get("weight", 1.0))
            confidence = float(meta.get("confidence", 0.5))
        except (ValueError, TypeError):
            weight, confidence = 1.0, 0.5

        chunk = {
            "id": chunk_id,
            "text": text,
            "chunk_type": meta.get("chunk_type", "unknown"),
            "source": meta.get("source", "unknown"),
            "status": meta.get("status", "active"),
            "weight": round(weight, 4),
            "confidence": round(confidence, 4),
            "epistemic_status": meta.get("epistemic_status", "stated"),
            "created_at": meta.get("created_at", ""),
            "tags": meta.get("tags", ""),
            "tags_list": _parse_tags(meta.get("tags", "")),
            "supersedes": meta.get("supersedes", ""),
            "last_confirmed_at": meta.get("last_confirmed_at", ""),
            "last_decay_at": meta.get("last_decay_at", ""),
            "age": _age_str(meta.get("created_at", "")),
        }

        return jsonify(chunk)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# =============================================================================
# API: Retrieval Simulation
# =============================================================================

@app.route("/api/retrieval/simulate")
def api_retrieval_simulate():
    """Simuliert ein Retrieval für eine Query — zeigt was im Prompt landen würde."""
    query = request.args.get("q", "")
    if not query:
        return jsonify({"error": "Query-Parameter 'q' fehlt"}), 400

    from memory.retrieval import score_and_select, compute_score
    from memory.memory_store import query_active

    # Rohkandidaten aus ChromaDB
    candidates = query_active(query, n_results=30)

    # Scoring
    scored = []
    for chunk in candidates:
        score, details = compute_score(chunk)
        scored.append({
            "id": chunk["id"],
            "text_preview": chunk["text"][:120],
            "chunk_type": chunk.get("chunk_type", "?"),
            "semantic_similarity": round(chunk.get("_semantic_similarity", 0), 4),
            "retrieval_score": round(score, 4),
            "score_details": details,
            "weight": round(chunk.get("weight", 1.0), 3),
            "confidence": round(chunk.get("confidence", 0.5), 3),
            "epistemic_status": chunk.get("epistemic_status", "stated"),
            "age": _age_str(chunk.get("created_at", "")),
        })

    scored.sort(key=lambda x: x["retrieval_score"], reverse=True)

    # Finale Auswahl
    selected = score_and_select(query)
    selected_ids = {c["id"] for c in selected}

    for s in scored:
        s["selected"] = s["id"] in selected_ids

    return jsonify({
        "query": query,
        "candidates_count": len(scored),
        "selected_count": len(selected),
        "candidates": scored,
    })


# =============================================================================
# API: Retrieval Log (letzte Einträge)
# =============================================================================

@app.route("/api/retrieval/log")
def api_retrieval_log():
    """Letzte Retrieval-Log Einträge."""
    log_path = os.path.join(PROJECT_DIR, "logs", "retrieval.log")
    limit = int(request.args.get("limit", 20))

    entries = []
    if os.path.exists(log_path):
        try:
            with open(log_path, "r") as f:
                lines = f.readlines()

            for line in reversed(lines[-200:]):
                line = line.strip()
                if not line:
                    continue
                # Format: "2026-03-07 12:00:00,000 {json}"
                try:
                    json_start = line.index("{")
                    entry = json.loads(line[json_start:])
                    entries.append(entry)
                    if len(entries) >= limit:
                        break
                except (ValueError, json.JSONDecodeError):
                    continue
        except IOError:
            pass

    return jsonify({"entries": entries, "total": len(entries)})


# =============================================================================
# Main
# =============================================================================

if __name__ == "__main__":
    logger.info("Dashboard startet auf Port 5001...")
    logger.info("URL: http://localhost:5001")
    app.run(host="0.0.0.0", port=5001, debug=False)
