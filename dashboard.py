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
from functools import wraps
from flask import Flask, render_template, jsonify, request, redirect, url_for, make_response

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
from core.database import get_fast_track_events, get_fast_track_stats, get_consolidator_events, get_consolidator_stats, get_soul_proposals, update_soul_proposal_status
from config import DASHBOARD_TOKEN, FLASK_SECRET_KEY

logging.basicConfig(level=logging.INFO, format="%(asctime)s [DASHBOARD] %(message)s")
logger = logging.getLogger(__name__)

app = Flask(
    __name__,
    template_folder=os.path.join(PROJECT_DIR, "dashboard", "templates"),
    static_folder=os.path.join(PROJECT_DIR, "dashboard", "static"),
)
app.secret_key = FLASK_SECRET_KEY

COOKIE_NAME = "dashboard_session"
COOKIE_DAYS = 30


def _is_authenticated():
    """Prüft ob das Session-Cookie gültig ist."""
    if not DASHBOARD_TOKEN:
        return True  # Kein Token gesetzt → Auth deaktiviert
    return request.cookies.get(COOKIE_NAME) == DASHBOARD_TOKEN


def require_auth(f):
    """Decorator: schützt eine Route mit Token-Auth."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not _is_authenticated():
            return redirect(url_for("login_page", next=request.path))
        return f(*args, **kwargs)
    return decorated


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
            "retrieved_count": int(meta.get("retrieved_count", 0)),
            "last_retrieved_at": meta.get("last_retrieved_at", ""),
        }
        chunks.append(chunk)

    return chunks


def _parse_tags(tags_str):
    """Parst Tags-String oder Liste zu Liste."""
    if not tags_str:
        return []
    if isinstance(tags_str, list):
        return [t.strip() for t in tags_str if str(t).strip()]
    return [t.strip() for t in str(tags_str).split(",") if t.strip()]


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
# Auth
# =============================================================================

@app.route("/login", methods=["GET", "POST"])
def login_page():
    error = None
    if request.method == "POST":
        token = request.form.get("token", "").strip()
        if token == DASHBOARD_TOKEN:
            resp = make_response(redirect(request.args.get("next", "/")))
            resp.set_cookie(
                COOKIE_NAME, token,
                max_age=60 * 60 * 24 * COOKIE_DAYS,
                httponly=True, samesite="Lax",
            )
            return resp
        error = "Falsches Token."
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    resp = make_response(redirect(url_for("login_page")))
    resp.delete_cookie(COOKIE_NAME)
    return resp


# =============================================================================
# Pages
# =============================================================================

@app.route("/")
@require_auth
def index():
    return render_template("index.html")


@app.route("/chunks")
@require_auth
def chunks_page():
    return render_template("chunks.html")


@app.route("/retrieval")
@require_auth
def retrieval_page():
    return render_template("retrieval.html")


@app.route("/fasttrack")
@require_auth
def fasttrack_page():
    return render_template("fasttrack.html")


@app.route("/consolidator")
@require_auth
def consolidator_page():
    return render_template("consolidator.html")


@app.route("/diary")
@require_auth
def diary_page():
    return render_template("diary.html")


@app.route("/soul")
@require_auth
def soul_page():
    return render_template("soul.html")


# =============================================================================
# API: Stats
# =============================================================================

@app.route("/api/stats")
@require_auth
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

    # Letzter Heartbeat aus heartbeat_state.json
    heartbeat_last_run = None
    heartbeat_age_min = None
    try:
        state_path = os.path.join(PROJECT_DIR, "heartbeat_state.json")
        with open(state_path, "r") as f:
            state = json.load(f)
        # Suche nach *_last_run Key
        for key, val in state.items():
            if key.endswith("_last_run") and val:
                dt = safe_parse_dt(val)
                if dt:
                    from core.datetime_utils import TZ_BERLIN
                    heartbeat_last_run = dt.astimezone(TZ_BERLIN).strftime("%d.%m.%Y %H:%M")
                    delta = now_utc() - dt
                    heartbeat_age_min = int(delta.total_seconds() / 60)
                break
    except Exception:
        pass

    return jsonify({
        "active_chunks": stats["active_count"],
        "archived_chunks": stats["archive_count"],
        "total_chunks": stats["total_count"],
        "today_count": today_count,
        "by_type": by_type,
        "by_source": by_source,
        "by_epistemic": by_epistemic,
        "timestamp": now_berlin().strftime("%d.%m.%Y %H:%M"),
        "heartbeat_last_run": heartbeat_last_run,
        "heartbeat_age_min": heartbeat_age_min,
        "system": (lambda p=__import__('psutil'): {
            "cpu": round(p.cpu_percent(interval=0.5), 1),
            "ram_used_gb": round(p.virtual_memory().used / 1024**3, 1),
            "ram_total_gb": round(p.virtual_memory().total / 1024**3, 1),
            "ram_percent": p.virtual_memory().percent,
            "disk_used_gb": round(p.disk_usage('/').used / 1024**3, 1),
            "disk_total_gb": round(p.disk_usage('/').total / 1024**3, 1),
            "disk_percent": p.disk_usage('/').percent,
        })(),
    })


# =============================================================================
# Tools
# =============================================================================

TOOLS_CONFIG_PATH = os.path.join(PROJECT_DIR, "data", "tools_config.json")

DEFAULT_TOOLS = [
    {"id": "pdf",        "name": "PDF-Analyse",      "icon": "📄", "description": "PDFs per WhatsApp hochladen und durchsuchen", "enabled": True,  "available": True},
    {"id": "websearch",  "name": "Web Search",        "icon": "🌐", "description": "Aktuelle Informationen aus dem Web abrufen",  "enabled": False, "available": False},
    {"id": "calendar",   "name": "Kalender",          "icon": "📅", "description": "Termine lesen und erstellen",                "enabled": False, "available": False},
    {"id": "email",      "name": "E-Mail",            "icon": "✉️",  "description": "E-Mails lesen und senden",                  "enabled": False, "available": False},
    {"id": "voice",      "name": "Sprachnachrichten", "icon": "🎙️", "description": "Sprachnachrichten transkribieren (Whisper)", "enabled": False, "available": False},
    {"id": "tasks",      "name": "Aufgaben",          "icon": "✅", "description": "Aufgaben erstellen und verwalten",           "enabled": False, "available": False},
    {"id": "images",     "name": "Bildanalyse",       "icon": "🖼️", "description": "Bilder beschreiben und analysieren",        "enabled": False, "available": False},
    {"id": "contacts",   "name": "Kontakte",          "icon": "👤", "description": "WhatsApp-Kontakte als Kontext nutzen",      "enabled": False, "available": False},
]

def load_tools_config():
    try:
        with open(TOOLS_CONFIG_PATH, "r") as f:
            saved = {t["id"]: t for t in json.load(f)}
        tools = []
        for t in DEFAULT_TOOLS:
            merged = dict(t)
            if t["id"] in saved:
                merged["enabled"] = saved[t["id"]].get("enabled", t["enabled"])
            tools.append(merged)
        return tools
    except (FileNotFoundError, json.JSONDecodeError):
        return DEFAULT_TOOLS

def save_tools_config(tools):
    os.makedirs(os.path.dirname(TOOLS_CONFIG_PATH), exist_ok=True)
    with open(TOOLS_CONFIG_PATH, "w") as f:
        json.dump(tools, f, indent=2)


@app.route("/tools")
@require_auth
def tools_page():
    return render_template("tools.html")


@app.route("/api/tools")
@require_auth
def api_tools_get():
    return jsonify(load_tools_config())


@app.route("/api/tools/<tool_id>", methods=["PATCH"])
@require_auth
def api_tools_patch(tool_id):
    data = request.get_json()
    tools = load_tools_config()
    for t in tools:
        if t["id"] == tool_id:
            if not t["available"] and data.get("enabled"):
                return jsonify({"error": "Tool noch nicht verfügbar"}), 400
            t["enabled"] = bool(data.get("enabled", t["enabled"]))
            break
    else:
        return jsonify({"error": "Tool nicht gefunden"}), 404
    save_tools_config(tools)
    return jsonify({"ok": True})


# =============================================================================
# API: Token-Tracking
# =============================================================================

@app.route("/api/tokens")
@require_auth
def api_tokens():
    import json
    from datetime import datetime, timezone, timedelta
    path = os.path.join(PROJECT_DIR, "data", "token_usage.json")
    try:
        with open(path, "r") as f:
            usage = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        usage = {}

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")

    # Letzte 7 Tage
    week_days = [(datetime.now(timezone.utc) - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7)]
    last_week_days = [(datetime.now(timezone.utc) - timedelta(days=i+7)).strftime("%Y-%m-%d") for i in range(7)]

    today_data = usage.get(today, {"prompt": 0, "completion": 0, "total": 0, "calls": 0})
    yesterday_data = usage.get(yesterday, {"prompt": 0, "completion": 0, "total": 0, "calls": 0})

    week_total = sum(usage.get(d, {}).get("total", 0) for d in week_days)
    last_week_total = sum(usage.get(d, {}).get("total", 0) for d in last_week_days)

    def pct_change(current, previous):
        if previous == 0:
            return None
        return round((current - previous) / previous * 100, 1)

    # Letzte 14 Tage fuer Chart
    chart_days = sorted([(datetime.now(timezone.utc) - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(13, -1, -1)])
    chart_data = [{"date": d, "total": usage.get(d, {}).get("total", 0), "calls": usage.get(d, {}).get("calls", 0)} for d in chart_days]

    return jsonify({
        "today": today_data,
        "yesterday": yesterday_data,
        "today_vs_yesterday": pct_change(today_data["total"], yesterday_data["total"]),
        "week_total": week_total,
        "last_week_total": last_week_total,
        "week_vs_last_week": pct_change(week_total, last_week_total),
        "chart": chart_data,
    })


# =============================================================================
# API: Chunks
# =============================================================================

@app.route("/api/chunks")
@require_auth
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
@require_auth
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
# API: Chunk Edit + Archive
# =============================================================================

@app.route("/api/chunks/<chunk_id>", methods=["PATCH"])
@require_auth
def api_chunk_update(chunk_id):
    """Aktualisiert weight, confidence und/oder tags eines Chunks."""
    data = request.get_json(silent=True) or {}
    collection = get_active_collection()
    try:
        result = collection.get(ids=[chunk_id], include=["documents", "metadatas"])
        if not result["ids"]:
            return jsonify({"error": "Chunk nicht gefunden"}), 404
        meta = dict(result["metadatas"][0])
        if "weight" in data:
            meta["weight"] = float(data["weight"])
        if "confidence" in data:
            meta["confidence"] = float(data["confidence"])
        if "tags" in data:
            meta["tags"] = str(data["tags"])
        collection.update(ids=[chunk_id], metadatas=[meta])
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/chunks/<chunk_id>/archive", methods=["POST"])
@require_auth
def api_chunk_archive(chunk_id):
    """Verschiebt einen Chunk in die Archive-Collection."""
    active = get_active_collection()
    archive = get_archive_collection()
    try:
        result = active.get(ids=[chunk_id], include=["documents", "metadatas", "embeddings"])
        if not result["ids"]:
            return jsonify({"error": "Chunk nicht gefunden"}), 404
        meta = dict(result["metadatas"][0])
        meta["status"] = "archived"
        from core.datetime_utils import now_utc
        meta["archived_at"] = now_utc().isoformat()
        archive.add(
            ids=[chunk_id],
            documents=result["documents"],
            metadatas=[meta],
            embeddings=result["embeddings"] if result.get("embeddings") else None,
        )
        active.delete(ids=[chunk_id])
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/chunks/bulk-archive", methods=["POST"])
@require_auth
def api_chunks_bulk_archive():
    """Archiviert mehrere Chunks auf einmal."""
    data = request.get_json(silent=True) or {}
    ids = data.get("ids", [])
    if not ids:
        return jsonify({"error": "Keine IDs übergeben"}), 400
    active = get_active_collection()
    archive = get_archive_collection()
    done = []
    errors = []
    for chunk_id in ids:
        try:
            result = active.get(ids=[chunk_id], include=["documents", "metadatas", "embeddings"])
            if not result["ids"]:
                errors.append(chunk_id)
                continue
            meta = dict(result["metadatas"][0])
            meta["status"] = "archived"
            from core.datetime_utils import now_utc
            meta["archived_at"] = now_utc().isoformat()
            archive.add(
                ids=[chunk_id],
                documents=result["documents"],
                metadatas=[meta],
                embeddings=result["embeddings"] if result.get("embeddings") else None,
            )
            active.delete(ids=[chunk_id])
            done.append(chunk_id)
        except Exception as e:
            errors.append(chunk_id)
    return jsonify({"archived": len(done), "errors": errors})


# =============================================================================
# API: Retrieval Simulation
# =============================================================================

@app.route("/api/retrieval/simulate")
@require_auth
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

    # Finale Auswahl mit Ablehnungsgründen
    from memory.retrieval import apply_caps
    for chunk in candidates:
        score, details = compute_score(chunk)
        chunk["_retrieval_score"] = round(score, 4)
        chunk["_score_details"] = details

    selected_chunks, rejected_chunks = apply_caps(candidates)
    selected_ids = {c["id"] for c in selected_chunks}
    rejection_reasons = {c["id"]: reason for c, reason in rejected_chunks}

    for s in scored:
        s["selected"] = s["id"] in selected_ids
        s["rejection_reason"] = rejection_reasons.get(s["id"], None)

    # Prompt-Vorschau bauen
    from memory.prompt_builder import build_memory_prompt
    prompt_block = build_memory_prompt(selected_chunks) or ""
    prompt_chars = len(prompt_block)
    prompt_tokens_est = round(prompt_chars / 4)  # ~4 Zeichen pro Token

    # Chunk-Reihenfolge im finalen Prompt (nach Typ-Reihenfolge)
    from memory.memory_config import PROMPT_TYPE_ORDER
    def sort_key(c):
        try:
            return PROMPT_TYPE_ORDER.index(c.get("chunk_type", "hard_fact"))
        except ValueError:
            return 99
    prompt_order = sorted(selected_chunks, key=sort_key)
    prompt_order_ids = [c["id"] for c in prompt_order]

    # Typen-Mix im finalen Prompt
    type_mix = {}
    for c in selected_chunks:
        t = c.get("chunk_type", "?")
        type_mix[t] = type_mix.get(t, 0) + 1

    # Verdrängte Top-Kandidaten: guter Score aber rausgeflogen
    displaced = [
        s for s in scored
        if not s["selected"]
        and s["retrieval_score"] >= 0.65
    ]
    displaced.sort(key=lambda x: x["retrieval_score"], reverse=True)

    return jsonify({
        "query": query,
        "candidates_count": len(scored),
        "selected_count": len(selected_chunks),
        "candidates": scored,
        "prompt_preview": prompt_block,
        "prompt_chars": prompt_chars,
        "prompt_tokens_est": prompt_tokens_est,
        "prompt_order": prompt_order_ids,
        "type_mix": type_mix,
        "displaced_top": displaced[:5],
    })


# =============================================================================
# API: Retrieval Log (letzte Einträge)
# =============================================================================

@app.route("/api/retrieval/log")
@require_auth
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
# API: Fast-Track Monitor
# =============================================================================

@app.route("/api/fasttrack/stats")
@require_auth
def api_fasttrack_stats():
    """Aggregierte Fast-Track-Statistiken."""
    try:
        stats = get_fast_track_stats()
        stats["timestamp"] = now_berlin().strftime("%d.%m.%Y %H:%M")
        return jsonify(stats)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/fasttrack/events")
@require_auth
def api_fasttrack_events():
    """Fast-Track Events mit optionalem Filter."""
    limit = int(request.args.get("limit", 50))
    user_id = request.args.get("user_id", None)
    stored_filter = request.args.get("stored", "")  # "1", "0", oder ""

    try:
        events = get_fast_track_events(limit=limit * 2, user_id=user_id)

        if stored_filter == "1":
            events = [e for e in events if e["stored"] == 1]
        elif stored_filter == "0":
            events = [e for e in events if e["stored"] == 0]

        events = events[:limit]

        # Aufbereiten für Frontend
        for ev in events:
            ev["tags_list"] = [t.strip() for t in (ev.get("tags") or "").split(",") if t.strip()]
            ev["timestamp_short"] = ev["timestamp"][:16].replace("T", " ") if ev["timestamp"] else "?"
            ev["message_short"] = (ev.get("message_preview") or "")[:100]
            ev["chunk_text_short"] = (ev.get("chunk_text") or "")[:120]

        return jsonify({"events": events, "total": len(events)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# =============================================================================
# API: Konsolidierer Inspector
# =============================================================================

@app.route("/api/consolidator/stats")
@require_auth
def api_consolidator_stats():
    """Aggregierte Konsolidierer-Statistiken."""
    try:
        stats = get_consolidator_stats()
        stats["timestamp"] = now_berlin().strftime("%d.%m.%Y %H:%M")
        return jsonify(stats)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/consolidator/events")
@require_auth
def api_consolidator_events():
    """Konsolidierer Events."""
    limit = int(request.args.get("limit", 30))
    try:
        events = get_consolidator_events(limit=limit)
        for ev in events:
            ev["timestamp_short"] = ev["timestamp"][:16].replace("T", " ") if ev["timestamp"] else "?"
            try:
                ev["actions"] = json.loads(ev.get("actions_json") or "[]")
            except Exception:
                ev["actions"] = []

            # Zusammenfassung
            summary = {}
            for a in ev["actions"]:
                k = a.get("action", "?")
                summary[k] = summary.get(k, 0) + 1
            ev["actions_summary"] = summary
            ev["actions_count"] = len(ev["actions"])

            # Funnel-Daten
            turns = ev.get("turns_count", 0)
            # Kandidaten = block_size (Turns die an LLM gingen) minus dropped
            candidates_extracted = ev.get("block_size", 0)
            dropped = ev.get("dropped_count", 0)
            actions_count = len(ev["actions"])
            ev["funnel"] = {
                "turns": turns,
                "candidates_extracted": candidates_extracted,
                "dropped": dropped,
                "actions": actions_count,
            }

            # Netto-Effekt
            net = {"new": 0, "superseded": 0, "archived": 0}
            for a in ev["actions"]:
                action = a.get("action", "")
                if action == "store_new":
                    net["new"] += 1
                elif action == "supersede":
                    net["superseded"] += 1
                elif action == "archive":
                    net["archived"] += 1
            ev["net_effect"] = net

            # No-Op Erklärung
            if ev.get("null_result"):
                if ev.get("error"):
                    ev["noop_reason"] = "LLM-Fehler: " + ev["error"][:80]
                elif turns == 0:
                    ev["noop_reason"] = "Block war leer"
                elif candidates_extracted == 0:
                    ev["noop_reason"] = "Keine Kandidaten extrahiert"
                elif actions_count == 0:
                    ev["noop_reason"] = "LLM hat keine Aktionen erzeugt"
                else:
                    ev["noop_reason"] = "Alle Aktionen verworfen"
            else:
                ev["noop_reason"] = None

        return jsonify({"events": events, "total": len(events)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500



@app.route("/api/consolidator/diff/<chunk_id>")
@require_auth
def api_consolidator_diff(chunk_id):
    """Gibt alten und neuen Text für einen supersede-Chunk zurück."""
    try:
        # Neuer Chunk (active)
        active = get_active_collection()
        result = active.get(ids=[chunk_id], include=["documents", "metadatas"])
        if result["ids"]:
            new_text = result["documents"][0]
            supersedes = result["metadatas"][0].get("supersedes", "")
        else:
            # Im Archiv suchen
            archive = get_archive_collection()
            result = archive.get(ids=[chunk_id], include=["documents", "metadatas"])
            if not result["ids"]:
                return jsonify({"error": "Chunk nicht gefunden"}), 404
            new_text = result["documents"][0]
            supersedes = result["metadatas"][0].get("supersedes", "")

        old_text = None
        if supersedes:
            # Alten Chunk im Archiv suchen
            archive = get_archive_collection()
            old_result = archive.get(ids=[supersedes], include=["documents"])
            if old_result["ids"]:
                old_text = old_result["documents"][0]
            else:
                # Evtl. noch aktiv
                old_result = active.get(ids=[supersedes], include=["documents"])
                if old_result["ids"]:
                    old_text = old_result["documents"][0]

        return jsonify({
            "chunk_id": chunk_id,
            "new_text": new_text,
            "old_text": old_text,
            "supersedes": supersedes,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# =============================================================================
# API: Soul Editor
# =============================================================================

SOUL_MD_PATH = os.path.join(PROJECT_DIR, "soul.md")


def _parse_soul_sections(text):
    """Zerlegt soul.md in Sektionen. Gibt Liste von {title, content, index} zurück."""
    lines = text.split("\n")
    sections = []
    current_title = "__preamble__"
    current_lines = []
    idx = 0

    for line in lines:
        if line.startswith("## "):
            if current_lines or current_title == "__preamble__":
                sections.append({
                    "index": idx,
                    "title": current_title,
                    "content": "\n".join(current_lines).strip(),
                })
                idx += 1
            current_title = line[3:].strip()
            current_lines = []
        else:
            current_lines.append(line)

    # Letzte Sektion
    if current_lines or current_title != "__preamble__":
        sections.append({
            "index": idx,
            "title": current_title,
            "content": "\n".join(current_lines).strip(),
        })

    return sections


def _rebuild_soul_md(sections):
    """Baut soul.md aus Sektionen-Liste wieder zusammen."""
    parts = []
    for s in sections:
        if s["title"] == "__preamble__":
            parts.append(s["content"])
        else:
            parts.append(f"## {s['title']}\n\n{s['content']}")
    return "\n\n---\n\n".join(parts) + "\n"


@app.route("/api/soul/md")
@require_auth
def api_soul_md_get():
    """Gibt soul.md als Sektionen zurück."""
    try:
        with open(SOUL_MD_PATH, "r") as f:
            text = f.read()
        sections = _parse_soul_sections(text)
        return jsonify({"sections": sections, "raw": text})
    except FileNotFoundError:
        return jsonify({"error": "soul.md nicht gefunden"}), 404


@app.route("/api/soul/md", methods=["POST"])
@require_auth
def api_soul_md_save():
    """Speichert soul.md — entweder als Rohtext oder als Sektionen-Liste."""
    data = request.get_json()

    # Backup anlegen
    import shutil
    backup_path = SOUL_MD_PATH + ".bak"
    if os.path.exists(SOUL_MD_PATH):
        shutil.copy2(SOUL_MD_PATH, backup_path)

    if "raw" in data:
        new_text = data["raw"]
    elif "sections" in data:
        new_text = _rebuild_soul_md(data["sections"])
    else:
        return jsonify({"error": "raw oder sections erforderlich"}), 400

    with open(SOUL_MD_PATH, "w") as f:
        f.write(new_text)

    return jsonify({"ok": True, "chars": len(new_text)})


# =============================================================================
# API: Diary
# =============================================================================

@app.route("/api/diary/list")
@require_auth
def api_diary_list():
    """Liste aller Tagebucheinträge, neueste zuerst."""
    diary_dir = os.path.join(PROJECT_DIR, "diary")
    entries = []

    if not os.path.isdir(diary_dir):
        return jsonify({"entries": []})

    for fname in sorted(os.listdir(diary_dir), reverse=True):
        if not fname.endswith(".md"):
            continue
        path = os.path.join(diary_dir, fname)
        date = None
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.lower().startswith("datum:"):
                        date = line.split(":", 1)[1].strip()
                        break
        except IOError:
            pass
        entries.append({"filename": fname[:-3], "date": date or fname[:-3]})

    return jsonify({"entries": entries, "total": len(entries)})


@app.route("/api/diary/entry")
@require_auth
def api_diary_entry():
    """Inhalt eines einzelnen Tagebucheintrags."""
    filename = request.args.get("file", "")
    if not filename or ".." in filename or "/" in filename:
        return jsonify({"error": "Ungültiger Dateiname"}), 400

    path = os.path.join(PROJECT_DIR, "diary", filename + ".md")
    if not os.path.isfile(path):
        return jsonify({"error": "Datei nicht gefunden"}), 404

    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()
    except IOError as e:
        return jsonify({"error": str(e)}), 500

    # Header parsen (Datum, Autor, Titel)
    title, date, author, body = filename, None, None, raw
    lines = raw.split("\n")
    header_end = 0

    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith("# "):
            title = s[2:]
        elif s.lower().startswith("datum:"):
            date = s.split(":", 1)[1].strip()
        elif s.lower().startswith("autor:"):
            author = s.split(":", 1)[1].strip()
        elif s == "---" and i > 0:
            header_end = i + 1
            break

    body = "\n".join(lines[header_end:]).strip()

    return jsonify({
        "filename": filename,
        "title": title,
        "date": date,
        "author": author,
        "body": body,
        "raw": raw,
    })


# =============================================================================
# Main
# =============================================================================

@app.route("/api/soul/proposals")
@require_auth
def api_soul_proposals():
    status = request.args.get("status")
    proposals = get_soul_proposals(limit=50, status=status or None)
    all_proposals = get_soul_proposals(limit=200)
    stats = {
        "open": sum(1 for p in all_proposals if p["status"] == "open"),
        "adopted": sum(1 for p in all_proposals if p["status"] == "adopted"),
        "rejected": sum(1 for p in all_proposals if p["status"] == "rejected"),
        "total": len(all_proposals),
    }
    return jsonify({"proposals": proposals, "stats": stats})


@app.route("/api/soul/proposals/<int:proposal_id>/status", methods=["POST"])
@require_auth
def api_soul_proposal_status(proposal_id):
    data = request.get_json()
    status = data.get("status")
    if status not in ("open", "adopted", "rejected"):
        return jsonify({"error": "Invalid status"}), 400
    update_soul_proposal_status(proposal_id, status)
    return jsonify({"ok": True})


if __name__ == "__main__":
    logger.info("Dashboard startet auf Port 5001...")
    logger.info("URL: http://localhost:5001")
    app.run(host="0.0.0.0", port=5001, debug=False)
