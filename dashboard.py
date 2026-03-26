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
from core.database import get_fast_track_events, get_fast_track_stats, get_consolidator_events, get_consolidator_stats, get_soul_proposals, update_soul_proposal_status, get_connection as get_db_connection, get_mirror_turns, get_mirror_stats, get_chunk_genealogy
from core.heartbeat_log import get_recent_runs
from config import DASHBOARD_TOKEN, FLASK_SECRET_KEY, USER_CONTEXTS

logging.basicConfig(level=logging.INFO, format="%(asctime)s [DASHBOARD] %(message)s")
logger = logging.getLogger(__name__)

# Todos-Tabelle beim Start sicherstellen
try:
    from core.todos import init_todos_table
    with get_db_connection() as _conn:
        init_todos_table(_conn)
except Exception as _e:
    logger.warning(f"init_todos_table fehlgeschlagen: {_e}")

app = Flask(
    __name__,
    template_folder=os.path.join(PROJECT_DIR, "dashboard", "templates"),
    static_folder=os.path.join(PROJECT_DIR, "dashboard", "static"),
)
app.secret_key = FLASK_SECRET_KEY

@app.context_processor
def inject_globals():
    from config import BOT_NAME
    return {"bot_name": BOT_NAME}

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


@app.route("/timeline")
@require_auth
def timeline_page():
    return render_template("timeline.html")


@app.route("/api/heartbeat/runs")
@require_auth
def api_heartbeat_runs():
    from core.heartbeat_log import get_recent_runs
    limit = int(request.args.get("limit", 100))
    runs = get_recent_runs(limit=limit)
    return jsonify(runs)


@app.route("/soul")
@require_auth
def soul_page():
    return redirect("/essence")


@app.route("/todos")
@require_auth
def todos_page():
    return render_template("todos.html")


@app.route("/api/todos")
@require_auth
def api_todos():
    from core.todos import get_all_todos
    user_id = list(USER_CONTEXTS.keys())[0]
    status = request.args.get("status")
    todos = get_all_todos(user_id, limit=200)
    if status:
        todos = [t for t in todos if t["status"] == status]
    return jsonify(todos)


@app.route("/api/todos", methods=["POST"])
@require_auth
def api_todos_create():
    from core.todo_service import create_todo
    user_id = list(USER_CONTEXTS.keys())[0]
    data = request.json or {}
    todo = create_todo(
        owner_id=user_id,
        title=data.get("title", ""),
        description=data.get("description"),
        priority=data.get("priority", "mittel"),
        project=data.get("project"),
        due_date=data.get("due_date"),
        origin_type=data.get("origin_type", "manual"),
        origin_ref=data.get("origin_ref"),
        execution_mode=data.get("execution_mode", "none"),
        release_mode=data.get("release_mode", "manual"),
        task_template=data.get("task_template"),
    )
    return jsonify(todo), 201


@app.route("/api/todos/<int:todo_id>", methods=["PATCH"])
@require_auth
def api_todos_update(todo_id):
    from core.todo_service import complete_todo as svc_complete_todo
    from core.todos import get_todo
    from core.database import get_connection
    data = request.json or {}
    action = data.get("action")
    if action == "complete":
        ok = svc_complete_todo(todo_id, summary="Manuell im Dashboard erledigt")
        todo = get_todo(todo_id)
        return jsonify(todo)
    todo = get_todo(todo_id)
    if not todo:
        return jsonify({"error": "nicht gefunden"}), 404
    conn = get_connection()
    allowed = {"title", "description", "priority", "project", "due_date", "status", "execution_mode", "release_mode", "task_template"}
    updates = {k: v for k, v in data.items() if k in allowed}
    if updates:
        sets = ", ".join(f"{k}=?" for k in updates)
        conn.execute(f"UPDATE todos SET {sets} WHERE id=?", (*updates.values(), todo_id))
        conn.commit()
    return jsonify(get_todo(todo_id))


@app.route("/api/planner/state")
@require_auth
def api_planner_state():
    """WP8: legacy_compat — Planner ist nicht mehr Hauptpfad. Gibt leere Struktur zurück."""
    return jsonify({
        "focus": {}, "focus_hours": 0, "decisions": [],
        "lagebild": {"running": 0, "waiting": 0, "blocked": 0, "startable": 0,
                     "write_requests_pending": 0, "write_requests_overdue": 0,
                     "gate_active": 0, "under_pressure": 0, "mature_lines": 0,
                     "top_candidates": []},
        "hierarchy": [], "write_audit": [], "pending_writes": [],
        "_legacy_compat": True,
    })


def _api_planner_state_legacy():
    """WP8: legacy_compat body — nicht mehr aktiv. delete_candidate."""
    from core.planner import get_planner_focus, get_focus_duration_hours, run_planner
    from core.database import get_connection
    from config import USER_CONTEXTS
    user_id = list(USER_CONTEXTS.keys())[0]

    focus = get_planner_focus(user_id) or {}
    focus_hours = get_focus_duration_hours(user_id)

    conn = get_connection()
    try:
        decisions = [dict(r) for r in conn.execute(
            "SELECT * FROM planner_decisions WHERE owner_id=? ORDER BY decided_at DESC LIMIT 10",
            (user_id,)
        ).fetchall()]
        # Kandidaten-Lagebild
        from core.planner import collect_candidates, _build_situation, score_candidates
        cands = collect_candidates(user_id)
        sit = _build_situation(cands)
        scored = score_candidates(cands, sit)
        # Write-Requests im Lagebild zaehlen
        wr_pending = len([s for s in scored if s.get("type") == "write_request"
                          and s.get("approval_status") == "pending"])
        wr_overdue = len([s for s in scored if s.get("type") == "write_request"
                          and s.get("overdue")])

        # 6.x: Linien mit Gate/Pressure zaehlen
        gate_active_count = len([s for s in scored if s.get("first_line_gate")])
        pressure_count    = len([s for s in scored if s.get("execution_pressure", 0) > 0])
        mature_count      = len([s for s in scored if s.get("is_mature")])

        lagebild = {
            "running":   len(sit["running"]),
            "waiting":   len(sit["waiting"]),
            "blocked":   len(sit["blocked"]),
            "startable": len(sit["startable"]),
            "write_requests_pending": wr_pending,
            "write_requests_overdue": wr_overdue,
            "gate_active":    gate_active_count,
            "under_pressure": pressure_count,
            "mature_lines":   mature_count,
            "top_candidates": [
                {"id": s["id"], "type": s["type"], "title": s["title"][:50],
                 "decision": s["decision"], "score": s["score"],
                 "stagnating": s.get("stagnating", False),
                 "line_status": s.get("line_status"),
                 "overdue": s.get("overdue", False),
                 "decision_due_at": s.get("decision_due_at"),
                 "blocker_type": s.get("blocker_type"),
                 "execution_pressure": s.get("execution_pressure", 0),
                 "first_line_gate": s.get("first_line_gate", False),
                 "is_mature": s.get("is_mature", False),
                 "meta_cycle_count": s.get("meta_cycle_count", 0),
                 "first_meaningful_execution": s.get("first_meaningful_execution")}
                for s in scored[:8]
            ],
        }
    finally:
        conn.close()

    # Goal/Proposal/Todo Hierarchie
    hierarchy = []
    try:
        from core.database import get_connection as _gc2
        _c2 = _gc2()
        goals = [dict(r) for r in _c2.execute(
            "SELECT id, title, status, progress FROM kimi_goals WHERE owner_id=? AND status='active' ORDER BY priority DESC LIMIT 5",
            (user_id,)
        ).fetchall()]
        for g in goals:
            # WP10: kein kimi_proposals-Abruf mehr — Proposals sind in wp10_proposals
            g_todos = [dict(r) for r in _c2.execute(
                "SELECT id, title, status, execution_mode FROM todos WHERE goal_id=? AND status IN ('open','in_progress') LIMIT 3",
                (g["id"],)
            ).fetchall()]
            hierarchy.append({"goal": g, "proposals": [], "todos": g_todos})
        _c2.close()
    except Exception:
        hierarchy = []

    # Write-Audit letzte 10
    try:
        write_audit = [dict(r) for r in conn2.execute(
            "SELECT action_type, tool_ref, risk_class, gate_result, success, verify_result, executed_at, error FROM write_audit ORDER BY executed_at DESC LIMIT 10"
        ).fetchall()] if False else []  # conn2 schon geschlossen -- eigene Abfrage
        from core.database import get_connection as _gc3
        _c3 = _gc3()
        write_audit = [dict(r) for r in _c3.execute(
            "SELECT action_type, tool_ref, risk_class, gate_result, success, verify_result, target_ref, executed_at, error FROM write_audit ORDER BY executed_at DESC LIMIT 10"
        ).fetchall()]
        _c3.close()
    except Exception:
        write_audit = []

    # Pending Write-Requests mit Linienkontext (5.4)
    try:
        from core.gate_service import get_pending_write_requests
        pending_writes_raw = get_pending_write_requests(user_id)
        pending_writes = []
        from core.database import get_connection as _gc6
        _c6 = _gc6()
        for wr in pending_writes_raw:
            enriched = dict(wr)
            # Hauptlinie (origin_todo)
            if wr.get("origin_todo_id"):
                try:
                    row = _c6.execute("SELECT title FROM todos WHERE id=?",
                                      (int(wr["origin_todo_id"]),)).fetchone()
                    if row:
                        enriched["origin_todo_title"] = row["title"][:50]
                except Exception:
                    pass
            # Nebenlinie
            if wr.get("secondary_line_id"):
                try:
                    row = _c6.execute("SELECT title FROM todos WHERE id=?",
                                      (int(wr["secondary_line_id"]),)).fetchone()
                    if row:
                        enriched["secondary_line_title"] = row["title"][:40]
                except Exception:
                    pass
            # Approve/Reject Labels
            enriched["after_approve_label"] = {
                "continue_line": "Linie fortsetzen",
                "complete_line": "Linie abschliessen",
            }.get(wr.get("after_approve_action","continue_line"), "Fortsetzen")
            enriched["after_reject_label"] = {
                "replan": "Umplanen",
                "pause":  "Pausieren",
                "close":  "Beenden",
            }.get(wr.get("after_reject_action","replan"), "Umplanen")
            pending_writes.append(enriched)
        _c6.close()
    except Exception:
        pending_writes = []

    return jsonify({
        "focus":          focus,
        "focus_hours":    round(focus_hours, 1),
        "decisions":      decisions,
        "lagebild":       lagebild,
        "hierarchy":      hierarchy,
        "write_audit":    write_audit,
        "pending_writes": pending_writes,
    })


@app.route("/api/write-audit")
@require_auth
def api_write_audit():
    from core.database import get_connection
    limit = int(request.args.get("limit", 20))
    conn = get_connection()
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM write_audit ORDER BY executed_at DESC LIMIT ?", (limit,)
        ).fetchall()]
    finally:
        conn.close()
    return jsonify(rows)


@app.route("/api/write-requests")
@require_auth
def api_write_requests():
    """Offene und letzte Write-Requests."""
    from core.gate_service import get_pending_write_requests
    from core.database import get_connection
    from config import USER_CONTEXTS
    user_id = list(USER_CONTEXTS.keys())[0]
    pending = get_pending_write_requests(user_id)
    conn = get_connection()
    try:
        recent = [dict(r) for r in conn.execute(
            "SELECT * FROM write_requests WHERE owner_id=? ORDER BY created_at DESC LIMIT 20",
            (user_id,)
        ).fetchall()]
    finally:
        conn.close()
    return jsonify({"pending": pending, "recent": recent})


@app.route("/api/write-requests/<int:req_id>/action", methods=["POST"])
@require_auth
def api_write_request_action(req_id):
    """Approve, Reject oder Defer eines Write-Requests."""
    from core.gate_service import approve_write_request, reject_write_request, defer_write_request
    data = request.json or {}
    action = data.get("action")
    if action == "approve":
        result = approve_write_request(req_id, approved_by="dashboard")
        return jsonify(result)
    elif action == "reject":
        ok = reject_write_request(req_id, reason=data.get("reason",""), rejected_by="dashboard")
        return jsonify({"ok": ok})
    elif action == "defer":
        hours = int(data.get("hours", 24))
        ok = defer_write_request(req_id, defer_hours=hours,
                                  reason=data.get("reason",""), deferred_by="dashboard")
        return jsonify({"ok": ok})
    return jsonify({"error": "Unbekannte Aktion"}), 400


@app.route("/api/proposals/<int:proposal_id>/write", methods=["POST"])
@require_auth
def api_proposal_write(proposal_id):
    """
    5.5: Erzeugt einen Write-Request fuer eine Proposal-Statusaenderung.
    Erwartet: {"action": "approve"|"reject"|"defer", "reason": "..."}
    """
    from core.gate_service import execute_write, build_proposal_preview, create_write_request, get_policy
    from config import USER_CONTEXTS
    user_id = list(USER_CONTEXTS.keys())[0]
    data = request.json or {}
    action = data.get("action")
    if action not in ("approve", "reject", "defer"):
        return jsonify({"error": "Ungueltige Aktion"}), 400

    action_key = f"proposal.{action}"
    params = {"id": proposal_id, "action": action, "reason": data.get("reason","")}
    preview = build_proposal_preview(action, params)

    # execute_write -> erzeugt Write-Request (needs_approval)
    result = execute_write(
        action_key, params, user_id,
        lambda p: {"success": False, "error": "Direktausfuehrung nicht erlaubt"},
    )
    if result.get("pending"):
        return jsonify({"ok": True, "write_request_id": result.get("write_request_id"),
                        "preview": preview})
    return jsonify({"ok": False, "error": result.get("error","Fehler")}), 400


@app.route("/api/write-requests/history")
@require_auth
def api_write_requests_history():
    """Alle Write-Requests mit Status-History."""
    from core.database import get_connection
    from config import USER_CONTEXTS
    user_id = list(USER_CONTEXTS.keys())[0]
    conn = get_connection()
    try:
        rows = [dict(r) for r in conn.execute(
            """SELECT * FROM write_requests WHERE owner_id=?
               ORDER BY created_at DESC LIMIT 50""",
            (user_id,)
        ).fetchall()]
    finally:
        conn.close()
    return jsonify(rows)


# =============================================================================
# 7.x Workspace / Artifact API
# =============================================================================

# =============================================================================
# WP4: V2-Workspace Routen (Hauptpfad)
# =============================================================================

@app.route("/api/v2/workspace/documents")
@require_auth
def api_v2_workspace_documents():
    """V2: Alle Dokumente des aktiven Nutzers (note + code_file)."""
    from config import OWNER_ID
    from core.workspace_service import list_documents, get_leading_document
    docs = list_documents(OWNER_ID)
    leading = get_leading_document(OWNER_ID)
    return jsonify({"documents": docs, "leading_document": leading})


@app.route("/api/v2/workspace/documents/<path:doc_id>")
@require_auth
def api_v2_workspace_document(doc_id):
    """V2: Einzelnes Dokument lesen."""
    from config import OWNER_ID
    from core.workspace_service import read_document, get_leading_document
    content = read_document(OWNER_ID, doc_id)
    if content is None:
        return jsonify({"error": "Nicht gefunden"}), 404
    leading = get_leading_document(OWNER_ID)
    return jsonify({"doc_id": doc_id, "content": content,
                    "is_leading": doc_id == leading})


@app.route("/api/v2/workspace/leading")
@require_auth
def api_v2_workspace_leading():
    """V2: Führendes Dokument + Inhalt."""
    from config import OWNER_ID
    from core.workspace_service import get_leading_document, read_leading_document
    doc_id = get_leading_document(OWNER_ID)
    content = read_leading_document(OWNER_ID)
    return jsonify({"doc_id": doc_id, "content": content})


# =============================================================================
# WP4: Legacy Workspace Routen (temporary_compat -- delete_candidate nach Migration)
# =============================================================================

@app.route("/api/workspace/lines")
@require_auth
def api_workspace_lines():
    """Legacy: Alle Linien mit Workspace-Eintraegen. temporary_compat."""
    from core.database import get_connection
    conn = get_connection()
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM line_workspace_state ORDER BY last_workspace_write_at DESC LIMIT 50"
        ).fetchall()]
    finally:
        conn.close()
    return jsonify(rows)


@app.route("/api/workspace/lines/<path:line_id>/artifacts")
@require_auth
def api_workspace_line_artifacts(line_id):
    """Legacy: Alle Artifacts einer Linie. temporary_compat -- delete_candidate"""
    from core.workspace_artifact_service import list_line_artifacts, build_line_manifest
    status = request.args.get("status")
    artifact_type = request.args.get("type")
    artifacts = list_line_artifacts(line_id, status=status, artifact_type=artifact_type)
    manifest = build_line_manifest(line_id)
    return jsonify({"artifacts": artifacts, "manifest": manifest})


@app.route("/api/workspace/artifacts/<int:artifact_id>")
@require_auth
def api_workspace_artifact(artifact_id):
    """Legacy: Einzelnes Artifact. temporary_compat -- delete_candidate"""
    from core.workspace_artifact_service import get_artifact, read_artifact_content
    art = get_artifact(artifact_id)
    if not art:
        return jsonify({"error": "Nicht gefunden"}), 404
    content = read_artifact_content(artifact_id)
    return jsonify({"artifact": art, "content": content})


@app.route("/api/workspace/artifacts/<int:artifact_id>/status", methods=["POST"])
@require_auth
def api_workspace_artifact_status(artifact_id):
    """Status eines Artifacts aendern."""
    from core.workspace_artifact_service import set_artifact_status
    data = request.json or {}
    ok = set_artifact_status(artifact_id, data.get("status",""), actor="dashboard")
    return jsonify({"ok": ok})


@app.route("/api/workspace/stats")
@require_auth
def api_workspace_stats():
    """Legacy: Gesamtstatistik alter Artifacts. temporary_compat -- delete_candidate"""
    from core.database import get_connection
    conn = get_connection()
    try:
        total = conn.execute("SELECT COUNT(*) FROM workspace_artifacts").fetchone()[0]
        by_type = [dict(r) for r in conn.execute(
            "SELECT artifact_type, COUNT(*) as n FROM workspace_artifacts GROUP BY artifact_type"
        ).fetchall()]
        by_status = [dict(r) for r in conn.execute(
            "SELECT status, COUNT(*) as n FROM workspace_artifacts GROUP BY status"
        ).fetchall()]
        materialized = conn.execute(
            "SELECT COUNT(*) FROM workspace_artifacts WHERE is_materialized_execution=1"
        ).fetchone()[0]
        recent = [dict(r) for r in conn.execute(
            "SELECT * FROM workspace_artifacts ORDER BY created_at DESC LIMIT 10"
        ).fetchall()]
    finally:
        conn.close()
    return jsonify({
        "total": total, "materialized": materialized,
        "by_type": by_type, "by_status": by_status,
        "recent": recent,
    })


@app.route("/api/workspace/artifacts/recent")
@require_auth
def api_workspace_artifacts_recent():
    """Letzte N Artefakte optional gefiltert. 7.5.8: system-Artefakte ausgeblendet."""
    from core.database import get_connection
    limit = int(request.args.get("limit", 30))
    atype = request.args.get("type")
    status = request.args.get("status")
    show_system = request.args.get("show_system", "0") == "1"
    conn = get_connection()
    try:
        q = "SELECT * FROM workspace_artifacts WHERE 1=1"
        p = []
        # 7.5.8: system/hidden Artefakte standardmäßig ausblenden
        if not show_system:
            try:
                q += " AND (visibility_class IS NULL OR visibility_class = 'workspace')"
            except Exception:
                pass
        if atype:
            q += " AND artifact_type=?"; p.append(atype)
        if status:
            q += " AND status=?"; p.append(status)
        q += " ORDER BY created_at DESC LIMIT ?"
        p.append(limit)
        rows = [dict(r) for r in conn.execute(q, p).fetchall()]
    finally:
        conn.close()
    return jsonify(rows)


@app.route("/api/write-control")
@require_auth
def api_write_control():
    """
    5.6.4: Einheitliche Kontrollschicht -- Request + Audit + Linienkontext zusammen.
    Zeigt die letzten N Writes mit vollstaendigem Status.
    """
    from core.database import get_connection
    from config import USER_CONTEXTS
    user_id = list(USER_CONTEXTS.keys())[0]
    limit = int(request.args.get("limit", 30))
    family = request.args.get("family")  # optional: workspace/todos/proposal/calendar

    conn = get_connection()
    try:
        # Write-Requests
        wr_query = """
            SELECT wr.*, wa.verify_result, wa.preflight_result, wa.write_result,
                   wa.executed_at as audit_executed_at
            FROM write_requests wr
            LEFT JOIN write_audit wa ON wa.task_id = wr.task_id
                AND wa.action_type = SUBSTR(wr.action_key, INSTR(wr.action_key,'.')+1)
            WHERE wr.owner_id=?
        """
        params = [user_id]
        if family:
            wr_query += " AND wr.tool_ref=?"
            params.append(family)
        wr_query += " ORDER BY wr.created_at DESC LIMIT ?"
        params.append(limit)

        requests_rows = [dict(r) for r in conn.execute(wr_query, params).fetchall()]

        # Audit-Only-Writes (Klasse A -- kein Request noetig)
        audit_query = """
            SELECT * FROM write_audit
            WHERE owner_id=? AND risk_class='A'
        """
        audit_params = [user_id]
        if family:
            audit_query += " AND tool_ref=?"
            audit_params.append(family)
        audit_query += " ORDER BY executed_at DESC LIMIT ?"
        audit_params.append(limit // 2)

        audit_rows = [dict(r) for r in conn.execute(audit_query, audit_params).fetchall()]

        # Statistik
        stats = {
            "total_requests": conn.execute(
                "SELECT COUNT(*) FROM write_requests WHERE owner_id=?", (user_id,)
            ).fetchone()[0],
            "pending": conn.execute(
                "SELECT COUNT(*) FROM write_requests WHERE owner_id=? AND approval_status='pending'",
                (user_id,)
            ).fetchone()[0],
            "verify_failed": conn.execute(
                "SELECT COUNT(*) FROM write_requests WHERE owner_id=? AND verification_status='failed'",
                (user_id,)
            ).fetchone()[0],
            "by_family": {},
        }
        for fam in ("workspace","todos","proposal","calendar"):
            stats["by_family"][fam] = conn.execute(
                "SELECT COUNT(*) FROM write_requests WHERE owner_id=? AND tool_ref=?",
                (user_id, fam)
            ).fetchone()[0]

    finally:
        conn.close()

    return jsonify({
        "requests": requests_rows,
        "audit_class_a": audit_rows,
        "stats": stats,
    })


@app.route("/api/planner/run", methods=["POST"])
@require_auth
def api_planner_run():
    """WP8: legacy_compat — Planner-Run deaktiviert."""
    return jsonify({"ok": False, "reason": "legacy_compat — Planner nicht mehr aktiv (WP8)"})


@app.route("/api/todos/<int:todo_id>", methods=["DELETE"])
@require_auth
def api_todos_delete(todo_id):
    from core.todos import delete_todo
    delete_todo(todo_id)
    return jsonify({"ok": True})


@app.route("/quality")
@require_auth
def quality_page():
    return render_template("quality.html")


@app.route("/api/quality/stats")
@require_auth
def api_quality_stats():
    """Qualitaetsmetriken fuer das Gedaechtnis."""
    try:
        from datetime import timezone
        active = get_active_collection()
        all_data = active.get(include=["metadatas", "documents"])
        metas = all_data["metadatas"]
        docs  = all_data["documents"]
        now   = datetime.now(timezone.utc)

        t7  = (now - timedelta(days=7)).isoformat()
        t30 = (now - timedelta(days=30)).isoformat()

        total = len(metas)
        retrieved_ever = 0
        retrieved_7d   = 0
        retrieved_30d  = 0
        never_retrieved = 0
        dead_chunks = []
        top_chunks  = []
        confidence_buckets = {"0.0-0.3": 0, "0.3-0.6": 0, "0.6-0.8": 0, "0.8+": 0}
        weight_sum  = 0.0
        age_sum_days = 0

        for i, meta in enumerate(metas):
            rc  = int(meta.get("retrieved_count", 0))
            lra = meta.get("last_retrieved_at", "")
            ca  = meta.get("created_at", "")
            conf = float(meta.get("confidence", 0))
            w    = float(meta.get("weight", 1.0))
            weight_sum += w

            try:
                created_dt = datetime.fromisoformat(ca.replace("Z", "+00:00"))
                age_days = (now - created_dt).days
            except Exception:
                age_days = 0
            age_sum_days += age_days

            if rc > 0:
                retrieved_ever += 1
                if lra >= t7:
                    retrieved_7d += 1
                if lra >= t30:
                    retrieved_30d += 1
            else:
                never_retrieved += 1
                if age_days >= 14:
                    dead_chunks.append({
                        "id": all_data["ids"][i],
                        "text": (docs[i] or "")[:80],
                        "chunk_type": meta.get("chunk_type", "?"),
                        "age_days": age_days,
                        "confidence": round(conf, 2),
                        "weight": round(w, 2),
                    })

            if rc > 0:
                top_chunks.append({
                    "id": all_data["ids"][i],
                    "text": (docs[i] or "")[:80],
                    "chunk_type": meta.get("chunk_type", "?"),
                    "retrieved_count": rc,
                    "last_retrieved_at": lra[:16].replace("T", " ") if lra else "-",
                    "confidence": round(conf, 2),
                })

            if conf < 0.3:
                confidence_buckets["0.0-0.3"] += 1
            elif conf < 0.6:
                confidence_buckets["0.3-0.6"] += 1
            elif conf < 0.8:
                confidence_buckets["0.6-0.8"] += 1
            else:
                confidence_buckets["0.8+"] += 1

        top_chunks.sort(key=lambda x: x["retrieved_count"], reverse=True)
        dead_chunks.sort(key=lambda x: x["age_days"], reverse=True)

        ft_7d = {"total": 0, "stored": 0}
        cons_7d = {"runs": 0, "noop": 0, "actions": 0}
        try:
            conn = get_db_connection()
            t7_db = (now - timedelta(days=7)).strftime("%Y-%m-%d")
            rows = conn.execute(
                "SELECT stored FROM fast_track_events WHERE timestamp >= ?", (t7_db,)
            ).fetchall()
            ft_7d["total"]  = len(rows)
            ft_7d["stored"] = sum(1 for r in rows if r[0])

            rows2 = conn.execute(
                "SELECT null_result, actions_json FROM consolidator_events WHERE timestamp >= ?",
                (t7_db,)
            ).fetchall()
            cons_7d["runs"] = len(rows2)
            cons_7d["noop"] = sum(1 for r in rows2 if r[0])
            for r in rows2:
                try:
                    acts = json.loads(r[1] or "[]")
                    cons_7d["actions"] += len(acts)
                except Exception:
                    pass
        except Exception:
            pass

        return jsonify({
            "total": total,
            "retrieved_ever": retrieved_ever,
            "retrieved_7d": retrieved_7d,
            "retrieved_30d": retrieved_30d,
            "never_retrieved": never_retrieved,
            "usage_rate_ever": round(retrieved_ever / total * 100) if total else 0,
            "usage_rate_7d":   round(retrieved_7d   / total * 100) if total else 0,
            "usage_rate_30d":  round(retrieved_30d  / total * 100) if total else 0,
            "avg_weight": round(weight_sum / total, 2) if total else 0,
            "avg_age_days": round(age_sum_days / total) if total else 0,
            "dead_chunks": dead_chunks[:20],
            "top_chunks":  top_chunks[:15],
            "confidence_buckets": confidence_buckets,
            "ft_7d": ft_7d,
            "cons_7d": cons_7d,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/essence")
@require_auth
def essence_page():
    return render_template("essence.html")


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

    heartbeat_last_run = None
    heartbeat_age_min = None
    try:
        state_path = os.path.join(PROJECT_DIR, "heartbeat_state.json")
        with open(state_path, "r") as f:
            state = json.load(f)
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
            "uptime": (lambda: (lambda s: f"{s//86400}d {(s%86400)//3600}h {(s%3600)//60}min")(int(__import__('time').time() - p.boot_time())))(),
        })(),
    })


# =============================================================================
# Tools
# =============================================================================

TOOLS_CONFIG_PATH = os.path.join(PROJECT_DIR, "data", "tools_config.json")

DEFAULT_TOOLS = [
    {"id": "pdf",        "name": "PDF-Analyse",      "icon": "📄", "description": "PDFs per WhatsApp hochladen und durchsuchen", "enabled": True,  "available": True},
    {"id": "calendar",   "name": "Kalender",          "icon": "📅", "description": "Termine lesen und erstellen",                "enabled": False, "available": False},
    {"id": "email",      "name": "E-Mail",            "icon": "✉️",  "description": "E-Mails lesen und senden",                  "enabled": False, "available": False},
    {"id": "voice",      "name": "Sprachnachrichten", "icon": "🎙️", "description": "Sprachnachrichten transkribieren (Whisper)", "enabled": True,  "available": True},
    {"id": "tasks",      "name": "Aufgaben",          "icon": "✅", "description": "Aufgaben & Erinnerungen nach Kategorie",     "enabled": True,  "available": True},
    {"id": "images",     "name": "Bildanalyse",       "icon": "🖼️", "description": "Bilder beschreiben und analysieren",        "enabled": False, "available": False},
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
                merged["available"] = saved[t["id"]].get("available", t["available"])
                if "sub_calendars" in saved[t["id"]]:
                    merged["sub_calendars"] = saved[t["id"]]["sub_calendars"]
            tools.append(merged)
        return tools
    except (FileNotFoundError, json.JSONDecodeError):
        return DEFAULT_TOOLS

def save_tools_config(tools):
    os.makedirs(os.path.dirname(TOOLS_CONFIG_PATH), exist_ok=True)
    with open(TOOLS_CONFIG_PATH, "w") as f:
        json.dump(tools, f, indent=2)


# =============================================================================
# MIRROR
# =============================================================================

@app.route("/mirror")
@require_auth
def mirror_page():
    return render_template("mirror.html")


@app.route("/api/mirror/turns")
@require_auth
def api_mirror_turns():
    limit = min(int(request.args.get("limit", 50)), 500)
    user_id = request.args.get("user_id", None)
    turns = get_mirror_turns(limit=limit, user_id=user_id)
    return jsonify({"turns": turns, "count": len(turns)})


@app.route("/api/mirror/stats")
@require_auth
def api_mirror_stats():
    days = min(int(request.args.get("days", 7)), 90)
    stats = get_mirror_stats(days=days)
    return jsonify(stats)


@app.route("/genealogy")
@require_auth
def genealogy_page():
    return render_template("genealogy.html")

@app.route("/api/genealogy/chunks")
@require_auth
def api_genealogy_chunks():
    chunks = get_chunk_genealogy()
    return jsonify(chunks)

@app.route("/search-log")
@require_auth
def search_log_page():
    return render_template("search_log.html")


@app.route("/api/search-log")
@require_auth
def api_search_log():
    from core.database import get_search_log
    entries = get_search_log(limit=200)
    return jsonify({"entries": entries})


@app.route("/proposed-patterns")
@require_auth
def proposed_patterns_page():
    return render_template("proposed_patterns.html")


@app.route("/api/self-reflection")
@require_auth
def api_self_reflection():
    try:
        from self_reflection_summary import get_introspection_data
        data = get_introspection_data()
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e), "trend": {}, "chunks": [], "timeline": [], "top_tags": []}), 500


@app.route("/api/proposed-patterns")
@require_auth
def api_proposed_patterns():
    from core.database import get_proposed_patterns
    status = request.args.get("status", None)
    entries = get_proposed_patterns(status=status, limit=100)
    return jsonify({"patterns": entries, "count": len(entries)})


@app.route("/api/proposed-patterns/<int:pattern_id>/action", methods=["POST"])
@require_auth
def api_proposed_pattern_action(pattern_id):
    from core.database import update_proposed_pattern_status, get_proposed_patterns
    data = request.get_json() or {}
    action = data.get("action")
    if action not in ("dismiss", "keep", "promote"):
        return jsonify({"error": "Ungültige Aktion"}), 400
    patterns = get_proposed_patterns(limit=200)
    pattern = next((p for p in patterns if p["id"] == pattern_id), None)
    if not pattern:
        return jsonify({"error": "Pattern nicht gefunden"}), 404
    chunk_id = pattern["chunk_id"]
    try:
        if action == "dismiss":
            update_proposed_pattern_status(pattern_id, "dismissed")
            try:
                col = get_active_collection()
                col.update(ids=[chunk_id], metadatas=[{"status": "archived"}])
            except Exception:
                pass
            return jsonify({"ok": True, "action": "dismissed"})
        elif action == "keep":
            col = get_active_collection()
            result = col.get(ids=[chunk_id], include=["metadatas"])
            existing_meta = result["metadatas"][0] if result["metadatas"] else {}
            col.update(ids=[chunk_id], metadatas=[{**existing_meta, "chunk_type": "working_state"}])
            update_proposed_pattern_status(pattern_id, "working_state", promoted_to=chunk_id)
            return jsonify({"ok": True, "action": "kept_as_working_state", "chunk_id": chunk_id})
        elif action == "promote":
            col = get_active_collection()
            result = col.get(ids=[chunk_id], include=["metadatas"])
            existing_meta = result["metadatas"][0] if result["metadatas"] else {}
            existing_tags = existing_meta.get("tags", "")
            if isinstance(existing_tags, list):
                existing_tags = ",".join(existing_tags)
            new_tags = existing_tags + ",promoted-pattern" if existing_tags else "promoted-pattern"
            col.update(ids=[chunk_id], metadatas=[{
                **existing_meta,
                "chunk_type": "working_state",
                "tags": new_tags,
                "confidence": "0.7",
            }])
            update_proposed_pattern_status(pattern_id, "promoted", promoted_to=chunk_id)
            return jsonify({"ok": True, "action": "promoted", "chunk_id": chunk_id})
    except Exception as e:
        logger.error(f"proposed_pattern action fehlgeschlagen: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/system-map")
@require_auth
def system_map_page():
    return render_template("system_map.html")


@app.route("/mind")
@require_auth
def mind_page():
    return render_template("mind.html")


@app.route("/api/mind")
@require_auth
def api_mind():
    from core.state import load_state
    from memory.memory_store import get_active_collection

    owner_id = "221152228159675@lid"
    state = load_state()

    levels = {
        "moltbook":             {"last_run": state.get(f"{owner_id}_last_moltbook"),             "label": "Moltbook",          "icon": "search"},
        "introspection":        {"last_run": state.get(f"{owner_id}_last_introspection"),        "label": "Introspection",     "icon": "mirror"},
        "inner_dialogue":       {"last_run": state.get(f"{owner_id}_last_inner_dialogue"),       "label": "Innerer Dialog",    "icon": "chat"},
        "autonomous_reflection":{"last_run": state.get(f"{owner_id}_last_autonomous_reflection"),"label": "Autonome Reflexion","icon": "brain"},
    }

    timeline_chunks = []
    open_questions = []
    proactive_candidates = []

    try:
        col = get_active_collection()
        result = col.get(
            where={"$and": [{"source": "robot"}, {"status": "active"}]},
            include=["documents", "metadatas"],
        )
        if result["ids"]:
            for i, chunk_id in enumerate(result["ids"]):
                meta = result["metadatas"][i]
                text = result["documents"][i]
                tags_raw = meta.get("tags", "")
                chunk_type = meta.get("chunk_type", "")
                if isinstance(tags_raw, list):
                    tags = tags_raw
                else:
                    tags = [t.strip() for t in str(tags_raw).split(",") if t.strip()]
                tags_set = set(tags)
                if "moltbook" in tags_set or "exploration" in tags_set:
                    origin, icon = "moltbook", "search"
                elif "introspection" in tags_set:
                    origin, icon = "introspection", "mirror"
                elif "inner-dialogue" in tags_set:
                    origin, icon = "inner_dialogue", "chat"
                elif "autonomous-reflection" in tags_set:
                    origin, icon = "autonomous_reflection", "brain"
                elif chunk_type == "diary" or "diary" in tags_set:
                    origin, icon = "diary", "diary"
                elif "autonom" in tags_set or "reflexion" in tags_set:
                    origin, icon = "introspection", "mirror"
                else:
                    origin, icon = "other", "dot"
                chunk_data = {
                    "id": chunk_id, "id_short": chunk_id[:8],
                    "text": text[:200], "full_text": text,
                    "chunk_type": chunk_type,
                    "created_at": meta.get("created_at", ""),
                    "tags": tags, "replies_to": meta.get("replies_to", ""),
                    "origin": origin, "icon": icon,
                    "confidence": float(meta.get("confidence", 0.5)),
                }
                timeline_chunks.append(chunk_data)
                if "open_question" in tags:
                    open_questions.append(chunk_data)
                if "proactive_candidate" in tags:
                    proactive_candidates.append(chunk_data)
                if origin in levels:
                    existing = levels[origin].get("last_chunk")
                    if not existing or meta.get("created_at", "") > existing.get("created_at", ""):
                        levels[origin]["last_chunk"] = {
                            "id": chunk_id[:8], "text": text[:120],
                            "created_at": meta.get("created_at", ""),
                        }
        timeline_chunks.sort(key=lambda c: c.get("created_at", ""), reverse=True)
    except Exception as e:
        logger.error(f"api_mind: {e}")

    return jsonify({
        "levels": levels,
        "timeline": timeline_chunks[:60],
        "open_questions": open_questions,
        "proactive_candidates": proactive_candidates,
    })


@app.route("/workspace")
@require_auth
def workspace_page():
    from config import BOT_NAME as _BN
    return render_template("workspace.html", bot_name=_BN)


@app.route("/api/workspace/files")
@require_auth
def api_workspace_files():
    """Liste aller Dateien im kimi_workspace."""
    import os, json
    from core.code_exec import WORKSPACE, _list_files_raw
    files = []
    for filename in sorted(_list_files_raw()):
        filepath = os.path.join(WORKSPACE, filename)
        try:
            stat = os.stat(filepath)
            content = open(filepath, encoding="utf-8", errors="replace").read()
            files.append({
                "name": filename,
                "size": stat.st_size,
                "modified": stat.st_mtime,
                "lines": content.count("\n") + 1,
                "preview": content[:300],
                "content": content,
            })
        except Exception:
            continue
    return json.dumps(files, ensure_ascii=False)


@app.route("/api/workspace/run", methods=["POST"])
@require_auth
def api_workspace_run():
    """Führt eine Workspace-Datei aus."""
    import json
    data = request.get_json() or {}
    filename = data.get("filename", "")
    code     = data.get("code", "")
    if not filename and not code:
        return json.dumps({"error": "filename oder code fehlt"}), 400
    from core.code_exec import run_file, run_code
    if filename:
        result = run_file(filename)
    else:
        result = run_code(code)
    return json.dumps({"output": result}, ensure_ascii=False)


@app.route("/api/workspace/save", methods=["POST"])
@require_auth
def api_workspace_save():
    """Speichert eine Datei im Workspace."""
    import json
    data = request.get_json() or {}
    filename = data.get("filename", "")
    code     = data.get("code", "")
    if not filename or not code:
        return json.dumps({"error": "filename und code erforderlich"}), 400
    from core.code_exec import save_file
    result = save_file(filename, code)
    return json.dumps({"result": result}, ensure_ascii=False)


@app.route("/api/workspace/delete", methods=["POST"])
@require_auth
def api_workspace_delete():
    """Löscht eine Datei aus dem Workspace."""
    import json
    data = request.get_json() or {}
    filename = data.get("filename", "")
    if not filename:
        return json.dumps({"error": "filename fehlt"}), 400
    from core.code_exec import delete_file
    result = delete_file(filename)
    return json.dumps({"result": result}, ensure_ascii=False)




@app.route("/miro/callback")
def miro_callback():
    """
    OAuth2 Callback für Miro.
    Miro leitet nach der Autorisierung hierher und schickt einen 'code'.
    Wir tauschen den Code gegen einen Access Token und speichern ihn in der .env
    """
    import requests as _req
    import os

    code = request.args.get("code")
    error = request.args.get("error")

    if error:
        return f"<h2>Miro OAuth Fehler: {error}</h2>", 400

    if not code:
        return "<h2>Kein Authorization Code erhalten.</h2>", 400

    # Code gegen Token tauschen
    try:
        resp = _req.post(
            "https://api.miro.com/v1/oauth/token",
            data={
                "grant_type": "authorization_code",
                "client_id": "3458764664456651376",
                "client_secret": "mZbd19HP0XchUD4Oz3TgxJxcrHD5Xdyy",
                "code": code,
                "redirect_uri": "http://46.225.163.247:5001/miro/callback",
            },
            timeout=10,
        )
        data = resp.json()
    except Exception as e:
        return f"<h2>Token-Austausch fehlgeschlagen: {e}</h2>", 500

    if "access_token" not in data:
        return f"<h2>Kein Token erhalten:</h2><pre>{data}</pre>", 400

    access_token = data["access_token"]

    # Token in .env speichern
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    try:
        env_content = open(env_path).read() if os.path.exists(env_path) else ""
        if "MIRO_API_TOKEN=" in env_content:
            import re
            env_content = re.sub(r"MIRO_API_TOKEN=.*", f"MIRO_API_TOKEN={access_token}", env_content)
        else:
            env_content += f"\nMIRO_API_TOKEN={access_token}\n"
        open(env_path, "w").write(env_content)
        saved = True
    except Exception as e:
        saved = False

    return f"""
    <html><body style="font-family:sans-serif;max-width:600px;margin:60px auto;padding:20px">
    <h2>Miro OAuth erfolgreich!</h2>
    <p>Token erhalten und {"in .env gespeichert" if saved else "konnte nicht gespeichert werden"}.</p>
    <p><strong>Token:</strong> <code>{access_token[:30]}...</code></p>
    <p>Du kannst dieses Fenster schließen und den Bot neu starten.</p>
    <p><a href="/">Zurück zum Dashboard</a></p>
    </body></html>
    """

@app.route("/moltbook-log")
@require_auth
def moltbook_log_page():
    return render_template("moltbook_log.html")


@app.route("/api/moltbook-log")
@require_auth
def api_moltbook_log():
    from core.database import get_moltbook_log
    entries = get_moltbook_log(limit=100)
    return jsonify({"entries": entries})


@app.route("/api/moltbook-posts")
@require_auth
def api_moltbook_posts():
    from core.database import get_moltbook_posts
    posts = get_moltbook_posts(limit=50)
    return jsonify({"posts": posts, "count": len(posts)})


@app.route("/api/translate", methods=["POST"])
@require_auth
def api_translate():
    from core.ollama_client import _call_ollama
    data = request.get_json()
    text = data.get("text", "").strip()
    if not text:
        return jsonify({"error": "no text"}), 400
    result = _call_ollama([
        {"role": "system", "content": "Du bist ein Uebersetzer. Uebersetze ins Deutsche. Antworte NUR mit der Uebersetzung."},
        {"role": "user", "content": text},
    ])
    if not result:
        return jsonify({"error": "failed"}), 500
    return jsonify({"translation": result.get("message", {}).get("content", "").strip()})


@app.route("/api/moltbook-inbox")
@require_auth
def api_moltbook_inbox():
    from core.database import get_moltbook_inbox
    unread_only = request.args.get("unread", "0") == "1"
    entries = get_moltbook_inbox(limit=100, unread_only=unread_only)
    return jsonify({"entries": entries, "count": len(entries)})


@app.route("/calendar")
@require_auth
def calendar_page():
    return render_template("calendar.html")


@app.route("/api/calendar/events")
@require_auth
def api_calendar_events():
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
    from core.calendar.calendar_router import list_events
    date_str = request.args.get("date", "")
    if not date_str:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        date_str = datetime.now(ZoneInfo("Europe/Berlin")).date().isoformat()
    try:
        events = list_events(date_str)
        return jsonify({"events": events, "date": date_str})
    except Exception as e:
        logger.error(f"Calendar API Fehler: {e}")
        return jsonify({"events": [], "error": str(e)})


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


@app.route("/api/tools/calendar/sub/<cal_id>", methods=["PATCH"])
@require_auth
def api_calendar_sub_patch(cal_id):
    data = request.get_json()
    tools = load_tools_config()
    for t in tools:
        if t.get("id") == "calendar":
            for cal in t.get("sub_calendars", []):
                if cal["id"] == cal_id:
                    cal["enabled"] = bool(data.get("enabled", cal["enabled"]))
                    save_tools_config(tools)
                    return jsonify({"ok": True})
    return jsonify({"error": "Kalender nicht gefunden"}), 404


@app.route("/api/tools/calendar/sub/<cal_id>/perm", methods=["PATCH"])
@require_auth
def api_calendar_sub_perm(cal_id):
    data = request.get_json()
    perm_key = data.get("perm")
    perm_val = bool(data.get("value", False))
    if perm_key not in ("read", "write", "delete"):
        return jsonify({"error": "Ungültiges Recht"}), 400
    if perm_key == "read":
        return jsonify({"error": "Lesen kann nicht deaktiviert werden"}), 400
    tools = load_tools_config()
    for t in tools:
        if t.get("id") == "calendar":
            for cal in t.get("sub_calendars", []):
                if cal["id"] == cal_id:
                    if "permissions" not in cal:
                        cal["permissions"] = {"read": True, "write": True, "delete": True}
                    cal["permissions"][perm_key] = perm_val
                    save_tools_config(tools)
                    return jsonify({"ok": True})
    return jsonify({"error": "Kalender nicht gefunden"}), 404


# =============================================================================
# API: Token-Tracking
# =============================================================================

@app.route("/api/tokens")
@require_auth
def api_tokens():
    path = os.path.join(PROJECT_DIR, "data", "token_usage.json")
    try:
        with open(path, "r") as f:
            usage = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        usage = {}

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
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

    chart_days = sorted([(datetime.now(timezone.utc) - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(13, -1, -1)])
    chart_data = [{"date": d, "total": usage.get(d, {}).get("total", 0), "calls": usage.get(d, {}).get("calls", 0)} for d in chart_days]

    return jsonify({
        "today": today_data, "yesterday": yesterday_data,
        "today_vs_yesterday": pct_change(today_data["total"], yesterday_data["total"]),
        "week_total": week_total, "last_week_total": last_week_total,
        "week_vs_last_week": pct_change(week_total, last_week_total),
        "chart": chart_data,
    })


# =============================================================================
# API: Chunks
# =============================================================================

@app.route("/api/chunks/trust")
@require_auth
def api_chunks_trust():
    from core.database import get_chunk_trust_scores
    try:
        scores = get_chunk_trust_scores()
        return jsonify({"trust_scores": scores})
    except Exception as e:
        return jsonify({"trust_scores": {}, "error": str(e)})


@app.route("/api/chunks")
@require_auth
def api_chunks():
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

    chunks.sort(key=lambda c: c.get("created_at", ""), reverse=True)
    for c in chunks:
        c["tags_list"] = _parse_tags(c["tags"])
        c["age"] = _age_str(c["created_at"])
        c["text_preview"] = c["text"][:120] + "..." if len(c["text"]) > 120 else c["text"]
        c["created_short"] = c["created_at"][:16].replace("T", " ") if c["created_at"] else "?"

    return jsonify({"chunks": chunks, "total": len(chunks)})


@app.route("/api/chunks/<chunk_id>")
@require_auth
def api_chunk_detail(chunk_id):
    collection = get_active_collection()
    try:
        result = collection.get(ids=[chunk_id], include=["documents", "metadatas"])
        if not result["ids"]:
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
        return jsonify({
            "id": chunk_id, "text": text,
            "chunk_type": meta.get("chunk_type", "unknown"),
            "source": meta.get("source", "unknown"),
            "status": meta.get("status", "active"),
            "weight": round(weight, 4), "confidence": round(confidence, 4),
            "epistemic_status": meta.get("epistemic_status", "stated"),
            "created_at": meta.get("created_at", ""),
            "tags": meta.get("tags", ""),
            "tags_list": _parse_tags(meta.get("tags", "")),
            "supersedes": meta.get("supersedes", ""),
            "last_confirmed_at": meta.get("last_confirmed_at", ""),
            "last_decay_at": meta.get("last_decay_at", ""),
            "age": _age_str(meta.get("created_at", "")),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/chunks/<chunk_id>", methods=["PATCH"])
@require_auth
def api_chunk_update(chunk_id):
    data = request.get_json(silent=True) or {}
    collection = get_active_collection()
    try:
        result = collection.get(ids=[chunk_id], include=["documents", "metadatas"])
        if not result["ids"]:
            return jsonify({"error": "Chunk nicht gefunden"}), 404
        meta = dict(result["metadatas"][0])
        if "weight" in data: meta["weight"] = float(data["weight"])
        if "confidence" in data: meta["confidence"] = float(data["confidence"])
        if "tags" in data: meta["tags"] = str(data["tags"])
        collection.update(ids=[chunk_id], metadatas=[meta])
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/chunks/<chunk_id>/archive", methods=["POST"])
@require_auth
def api_chunk_archive(chunk_id):
    active = get_active_collection()
    archive = get_archive_collection()
    try:
        result = active.get(ids=[chunk_id], include=["documents", "metadatas", "embeddings"])
        if not result["ids"]:
            return jsonify({"error": "Chunk nicht gefunden"}), 404
        meta = dict(result["metadatas"][0])
        meta["status"] = "archived"
        meta["archived_at"] = now_utc().isoformat()
        archive.add(
            ids=[chunk_id], documents=result["documents"], metadatas=[meta],
            embeddings=result["embeddings"] if result.get("embeddings") else None,
        )
        active.delete(ids=[chunk_id])
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/chunks/bulk-archive", methods=["POST"])
@require_auth
def api_chunks_bulk_archive():
    data = request.get_json(silent=True) or {}
    ids = data.get("ids", [])
    if not ids:
        return jsonify({"error": "Keine IDs übergeben"}), 400
    active = get_active_collection()
    archive = get_archive_collection()
    done, errors = [], []
    for chunk_id in ids:
        try:
            result = active.get(ids=[chunk_id], include=["documents", "metadatas", "embeddings"])
            if not result["ids"]:
                errors.append(chunk_id); continue
            meta = dict(result["metadatas"][0])
            meta["status"] = "archived"
            meta["archived_at"] = now_utc().isoformat()
            archive.add(
                ids=[chunk_id], documents=result["documents"], metadatas=[meta],
                embeddings=result["embeddings"] if result.get("embeddings") else None,
            )
            active.delete(ids=[chunk_id])
            done.append(chunk_id)
        except Exception:
            errors.append(chunk_id)
    return jsonify({"archived": len(done), "errors": errors})


# =============================================================================
# API: Retrieval Simulation
# =============================================================================

@app.route("/api/retrieval/simulate")
@require_auth
def api_retrieval_simulate():
    query = request.args.get("q", "")
    if not query:
        return jsonify({"error": "Query-Parameter 'q' fehlt"}), 400

    from memory.retrieval import compute_score, apply_caps
    from memory.memory_store import query_active

    candidates = query_active(query, n_results=30)
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

    from memory.prompt_builder import build_memory_prompt
    from memory.memory_config import PROMPT_TYPE_ORDER
    prompt_block = build_memory_prompt(selected_chunks) or ""

    def sort_key(c):
        try: return PROMPT_TYPE_ORDER.index(c.get("chunk_type", "hard_fact"))
        except ValueError: return 99

    prompt_order = sorted(selected_chunks, key=sort_key)
    type_mix = {}
    for c in selected_chunks:
        t = c.get("chunk_type", "?")
        type_mix[t] = type_mix.get(t, 0) + 1

    displaced = sorted(
        [s for s in scored if not s["selected"] and s["retrieval_score"] >= 0.65],
        key=lambda x: x["retrieval_score"], reverse=True
    )

    return jsonify({
        "query": query,
        "candidates_count": len(scored),
        "selected_count": len(selected_chunks),
        "candidates": scored,
        "prompt_preview": prompt_block,
        "prompt_chars": len(prompt_block),
        "prompt_tokens_est": round(len(prompt_block) / 4),
        "prompt_order": [c["id"] for c in prompt_order],
        "type_mix": type_mix,
        "displaced_top": displaced[:5],
    })


@app.route("/api/retrieval/log")
@require_auth
def api_retrieval_log():
    log_path = os.path.join(PROJECT_DIR, "logs", "retrieval.log")
    limit = int(request.args.get("limit", 20))
    entries = []
    if os.path.exists(log_path):
        try:
            with open(log_path, "r") as f:
                lines = f.readlines()
            for line in reversed(lines[-200:]):
                line = line.strip()
                if not line: continue
                try:
                    json_start = line.index("{")
                    entry = json.loads(line[json_start:])
                    entries.append(entry)
                    if len(entries) >= limit: break
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
    try:
        stats = get_fast_track_stats()
        stats["timestamp"] = now_berlin().strftime("%d.%m.%Y %H:%M")
        return jsonify(stats)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/fasttrack/events")
@require_auth
def api_fasttrack_events():
    limit = int(request.args.get("limit", 50))
    user_id = request.args.get("user_id", None)
    stored_filter = request.args.get("stored", "")
    try:
        events = get_fast_track_events(limit=limit * 2, user_id=user_id)
        if stored_filter == "1": events = [e for e in events if e["stored"] == 1]
        elif stored_filter == "0": events = [e for e in events if e["stored"] == 0]
        events = events[:limit]
        for ev in events:
            ev["tags_list"] = _parse_tags(ev.get("tags", ""))
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
    try:
        stats = get_consolidator_stats()
        stats["timestamp"] = now_berlin().strftime("%d.%m.%Y %H:%M")
        return jsonify(stats)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/consolidator/events")
@require_auth
def api_consolidator_events():
    limit = int(request.args.get("limit", 30))
    try:
        events = get_consolidator_events(limit=limit)
        for ev in events:
            ev["timestamp_short"] = ev["timestamp"][:16].replace("T", " ") if ev["timestamp"] else "?"
            try: ev["actions"] = json.loads(ev.get("actions_json") or "[]")
            except Exception: ev["actions"] = []
            summary = {}
            for a in ev["actions"]:
                k = a.get("action", "?")
                summary[k] = summary.get(k, 0) + 1
            ev["actions_summary"] = summary
            ev["actions_count"] = len(ev["actions"])
            turns = ev.get("turns_count", 0)
            candidates_extracted = ev.get("block_size", 0)
            dropped = ev.get("dropped_count", 0)
            ev["funnel"] = {"turns": turns, "candidates_extracted": candidates_extracted, "dropped": dropped, "actions": len(ev["actions"])}
            net = {"new": 0, "superseded": 0, "archived": 0}
            for a in ev["actions"]:
                action = a.get("action", "")
                if action == "store_new": net["new"] += 1
                elif action == "supersede": net["superseded"] += 1
                elif action == "archive": net["archived"] += 1
            ev["net_effect"] = net
            if ev.get("null_result"):
                if ev.get("error"): ev["noop_reason"] = "LLM-Fehler: " + ev["error"][:80]
                elif turns == 0: ev["noop_reason"] = "Block war leer"
                elif candidates_extracted == 0: ev["noop_reason"] = "Keine Kandidaten extrahiert"
                elif len(ev["actions"]) == 0: ev["noop_reason"] = "LLM hat keine Aktionen erzeugt"
                else: ev["noop_reason"] = "Alle Aktionen verworfen"
            else: ev["noop_reason"] = None
        return jsonify({"events": events, "total": len(events)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/consolidator/diff/<chunk_id>")
@require_auth
def api_consolidator_diff(chunk_id):
    try:
        active = get_active_collection()
        result = active.get(ids=[chunk_id], include=["documents", "metadatas"])
        if result["ids"]:
            new_text = result["documents"][0]
            supersedes = result["metadatas"][0].get("supersedes", "")
        else:
            archive = get_archive_collection()
            result = archive.get(ids=[chunk_id], include=["documents", "metadatas"])
            if not result["ids"]:
                return jsonify({"error": "Chunk nicht gefunden"}), 404
            new_text = result["documents"][0]
            supersedes = result["metadatas"][0].get("supersedes", "")
        old_text = None
        if supersedes:
            archive = get_archive_collection()
            old_result = archive.get(ids=[supersedes], include=["documents"])
            if old_result["ids"]:
                old_text = old_result["documents"][0]
            else:
                old_result = active.get(ids=[supersedes], include=["documents"])
                if old_result["ids"]:
                    old_text = old_result["documents"][0]
        return jsonify({"chunk_id": chunk_id, "new_text": new_text, "old_text": old_text, "supersedes": supersedes})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# =============================================================================
# API: Soul Editor
# =============================================================================

SOUL_MD_PATH = os.path.join(PROJECT_DIR, "soul.md")
TOOLS_MD_PATH = os.path.join(PROJECT_DIR, "tools.md")
ARCHITECTURE_MD_PATH = os.path.join(PROJECT_DIR, "architecture.md")


def _atomic_write(path, text):
    import tempfile
    dir_ = os.path.dirname(path)
    with tempfile.NamedTemporaryFile("w", dir=dir_, delete=False, suffix=".tmp", encoding="utf-8") as tf:
        tf.write(text)
        tmp_path = tf.name
    os.replace(tmp_path, path)


def _parse_soul_sections(text):
    lines = text.split("\n")
    sections = []
    current_title = "__preamble__"
    current_lines = []
    idx = 0
    for line in lines:
        if line.strip() == "---":
            continue
        if line.startswith("## "):
            if current_lines or current_title == "__preamble__":
                sections.append({"index": idx, "title": current_title, "content": "\n".join(current_lines).strip()})
                idx += 1
            current_title = line[3:].strip()
            current_lines = []
        else:
            current_lines.append(line)
    if current_lines or current_title != "__preamble__":
        sections.append({"index": idx, "title": current_title, "content": "\n".join(current_lines).strip()})
    return sections


def _rebuild_soul_md(sections):
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
    try:
        with open(SOUL_MD_PATH, "r") as f:
            text = f.read()
        return jsonify({"sections": _parse_soul_sections(text), "raw": text})
    except FileNotFoundError:
        return jsonify({"error": "soul.md nicht gefunden"}), 404


@app.route("/api/soul/md", methods=["POST"])
@require_auth
def api_soul_md_save():
    data = request.get_json()
    import shutil
    if os.path.exists(SOUL_MD_PATH):
        shutil.copy2(SOUL_MD_PATH, SOUL_MD_PATH + ".bak")
    if "raw" in data:
        new_text = data["raw"]
    elif "sections" in data:
        new_text = _rebuild_soul_md(data["sections"])
    else:
        return jsonify({"error": "raw oder sections erforderlich"}), 400
    _atomic_write(SOUL_MD_PATH, new_text)
    return jsonify({"ok": True, "chars": len(new_text)})


_ESSENCE_FILES = {
    "tools": TOOLS_MD_PATH,
    "architecture": ARCHITECTURE_MD_PATH,
}


@app.route("/api/essence/<file_key>")
@require_auth
def api_essence_get(file_key):
    if file_key not in _ESSENCE_FILES:
        return jsonify({"error": "Unbekannte Datei"}), 404
    try:
        with open(_ESSENCE_FILES[file_key], "r") as f:
            text = f.read()
        return jsonify({"raw": text})
    except FileNotFoundError:
        return jsonify({"raw": "", "missing": True})


@app.route("/api/essence/<file_key>", methods=["POST"])
@require_auth
def api_essence_save(file_key):
    if file_key not in _ESSENCE_FILES:
        return jsonify({"error": "Unbekannte Datei"}), 404
    data = request.get_json()
    if "raw" not in data:
        return jsonify({"error": "raw erforderlich"}), 400
    path = _ESSENCE_FILES[file_key]
    import shutil
    if os.path.exists(path):
        shutil.copy2(path, path + ".bak")
    with open(path, "w") as f:
        f.write(data["raw"])
    return jsonify({"ok": True, "chars": len(data["raw"])})


@app.route("/api/essence/<file_key>/sections")
@require_auth
def api_essence_sections_get(file_key):
    if file_key not in _ESSENCE_FILES:
        return jsonify({"error": "Unbekannte Datei"}), 404
    try:
        with open(_ESSENCE_FILES[file_key], "r") as f:
            text = f.read()
        return jsonify({"sections": _parse_soul_sections(text), "raw": text})
    except FileNotFoundError:
        return jsonify({"sections": [], "raw": "", "missing": True})


@app.route("/api/essence/<file_key>/sections", methods=["POST"])
@require_auth
def api_essence_sections_save(file_key):
    if file_key not in _ESSENCE_FILES:
        return jsonify({"error": "Unbekannte Datei"}), 404
    data = request.get_json()
    if "sections" not in data:
        return jsonify({"error": "sections erforderlich"}), 400
    path = _ESSENCE_FILES[file_key]
    import shutil
    if os.path.exists(path): shutil.copy2(path, path + ".bak")
    new_text = _rebuild_soul_md(data["sections"])
    _atomic_write(path, new_text)
    return jsonify({"ok": True, "chars": len(new_text)})


# =============================================================================
# API: Diary
# =============================================================================

@app.route("/api/diary/list")
@require_auth
def api_diary_list():
    diary_dir = os.path.join(PROJECT_DIR, "diary")
    entries = []
    if not os.path.isdir(diary_dir):
        return jsonify({"entries": []})
    for fname in sorted(os.listdir(diary_dir), reverse=True):
        if not fname.endswith(".md"): continue
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
    title, date, author = filename, None, None
    lines = raw.split("\n")
    header_end = 0
    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith("# "): title = s[2:]
        elif s.lower().startswith("datum:"): date = s.split(":", 1)[1].strip()
        elif s.lower().startswith("autor:"): author = s.split(":", 1)[1].strip()
        elif s == "---" and i > 0: header_end = i + 1; break
    body = "\n".join(lines[header_end:]).strip()
    return jsonify({"filename": filename, "title": title, "date": date, "author": author, "body": body, "raw": raw})


# =============================================================================
# API: Soul Proposals
# =============================================================================

@app.route("/api/soul/proposals")
@require_auth
def api_soul_proposals():
    status = request.args.get("status")
    proposals = get_soul_proposals(limit=50, status=status or None)
    all_proposals = get_soul_proposals(limit=200)
    stats = {
        "open":     sum(1 for p in all_proposals if p["status"] == "open"),
        "adopted":  sum(1 for p in all_proposals if p["status"] == "adopted"),
        "rejected": sum(1 for p in all_proposals if p["status"] == "rejected"),
        "total":    len(all_proposals),
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


# =============================================================================
# Kimi Proposals
# =============================================================================

@app.route("/proposals")
@require_auth
def proposals_page():
    return render_template("proposals.html")


@app.route("/api/proposals")
@require_auth
def api_proposals_get():
    """WP10: liefert wp10_proposals — einzige aktive Proposal-Wahrheit."""
    from config import USER_CONTEXTS
    user_id = list(USER_CONTEXTS.keys())[0]
    status = request.args.get("status", "open")
    proposal_type = request.args.get("type")
    try:
        from core.proposal_service_wp10 import list_proposals
        # Status-Mapping: alte UI-Begriffe auf WP10-Status
        status_map = {"pending": "open", "approved": "accepted",
                      "rejected": "rejected", "deferred": "withdrawn", "all": None}
        wp10_status = status_map.get(status, status)
        proposals = list_proposals(
            owner_id=user_id,
            status=wp10_status,
            proposal_type=proposal_type,
            limit=100,
        )
        all_p = list_proposals(owner_id=user_id, status=None, limit=500)
        stats = {
            "open":      sum(1 for p in all_p if p.get("status") == "open"),
            "accepted":  sum(1 for p in all_p if p.get("status") == "accepted"),
            "rejected":  sum(1 for p in all_p if p.get("status") == "rejected"),
            "withdrawn": sum(1 for p in all_p if p.get("status") == "withdrawn"),
            # Legacy-Aliase für UI-Kompatibilität
            "pending":   sum(1 for p in all_p if p.get("status") == "open"),
            "approved":  sum(1 for p in all_p if p.get("status") == "accepted"),
            "deferred":  sum(1 for p in all_p if p.get("status") == "withdrawn"),
        }
        return jsonify({"proposals": proposals, "stats": stats})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/proposals/<int:proposal_id>/action", methods=["POST"])
@require_auth
def api_proposal_action(proposal_id):
    """WP10: Proposal-Entscheidungen — kein auto-Todo, kein auto-Task."""
    data = request.get_json()
    action = data.get("action")
    reason = data.get("reason", "")
    try:
        from core.proposal_service_wp10 import update_proposal_status
        # Aktions-Mapping auf WP10-Status
        status_map = {
            "approve": "accepted",
            "accept":  "accepted",
            "reject":  "rejected",
            "defer":   "withdrawn",
            "withdraw": "withdrawn",
        }
        new_status = status_map.get(action)
        if not new_status:
            return jsonify({"error": f"Unbekannte Aktion: {action}"}), 400
        ok = update_proposal_status(proposal_id, new_status, decision_note=reason or None)
        return jsonify({"ok": ok})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# =============================================================================
# ORBIT: Seite
# =============================================================================

@app.route("/planner")
@require_auth
def planner_page():
    return render_template("planner.html")


@app.route("/orbit")
@require_auth
def orbit_page():
    return render_template("orbit.html")


# =============================================================================
# ORBIT: Status & Control
# =============================================================================

ORBIT_RUNTIME_KEY = "mode"


def _get_orbit_mode():
    try:
        conn = get_db_connection()
        row = conn.execute(
            "SELECT value FROM orbit_runtime WHERE key=?", (ORBIT_RUNTIME_KEY,)
        ).fetchone()
        return row["value"] if row else "running"
    except Exception:
        return "running"


def _set_orbit_mode(mode):
    try:
        conn = get_db_connection()
        conn.execute(
            "INSERT INTO orbit_runtime(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (ORBIT_RUNTIME_KEY, mode)
        )
        conn.commit()
    except Exception:
        pass


@app.route("/api/orbit/status")
@require_auth
def api_orbit_status():
    return jsonify({"mode": _get_orbit_mode()})


@app.route("/api/orbit/control", methods=["POST"])
@require_auth
def api_orbit_control():
    data = request.get_json() or {}
    action = data.get("action", "")
    mode_map = {"not_aus": "not_aus", "soft_pause": "soft_pause", "resume": "running"}
    if action not in mode_map:
        return jsonify({"error": "Unbekannte Aktion"}), 400
    _set_orbit_mode(mode_map[action])
    return jsonify({"ok": True, "mode": mode_map[action]})


# =============================================================================
# ORBIT: Lage
# =============================================================================

@app.route("/api/orbit/lage")
@require_auth
def api_orbit_lage():
    try:
        conn = get_db_connection()

        def count(table, where="1=1", params=()):
            r = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}", params).fetchone()
            return r[0] if r else 0

        stats = {
            "tasks_total":       count("orbit_tasks"),
            "tasks_active":      count("orbit_tasks",  "status IN ('new','planned','active','waiting','paused')"),
            "tasks_hot":         count("orbit_tasks",  "hot=1"),
            # WP8: threads_open entfernt (Thread-System nicht mehr aktiv)
            "steps_running":     count("orbit_steps",  "status='running'"),
            "steps_blocked":     count("orbit_steps",  "status='blocked'"),
            "proactive_pending": count("orbit_proactive_messages", "release_state IN ('candidate','too_early','scheduled')"),
        }

        hot_rows = conn.execute(
            "SELECT * FROM orbit_tasks WHERE hot=1 ORDER BY priority DESC, created_at DESC"
        ).fetchall()
        hot_tasks = [dict(r) for r in hot_rows]

        ma = []
        for table in ["orbit_tasks", "orbit_steps", "orbit_policies", "orbit_routines"]:  # WP8: orbit_threads entfernt
            try:
                rows = conn.execute(
                    f"SELECT id, '{table}' as src FROM {table} WHERE manual_attention=1"
                ).fetchall()
                ma.extend([dict(r) for r in rows])
            except Exception:
                pass

        return jsonify({"stats": stats, "hot_tasks": hot_tasks, "manual_attention": ma})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# =============================================================================
# ORBIT: Tasks
# =============================================================================

@app.route("/api/orbit/tasks")
@require_auth
def api_orbit_tasks():
    try:
        conn = get_db_connection()
        where, params = ["1=1"], []
        sv = request.args.get("status", "")
        tv = request.args.get("task_type", "")
        if sv: where.append("status=?"); params.append(sv)
        if tv: where.append("task_type=?"); params.append(tv)
        rows = conn.execute(
            f"SELECT * FROM orbit_tasks WHERE {' AND '.join(where)} ORDER BY hot DESC, created_at DESC LIMIT 200",
            params
        ).fetchall()
        return jsonify({"tasks": [dict(r) for r in rows]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/orbit/tasks/<task_id>/action", methods=["POST"])
@require_auth
def api_orbit_task_action(task_id):
    import orbit as _orbit
    data = request.get_json() or {}
    action = data.get("action", "")
    reason = data.get("reason", f"Dashboard: {action}")
    action_map = {"pause": "paused", "resume": "active", "abort": "aborted"}
    if action not in action_map:
        return jsonify({"error": "Unbekannte Aktion"}), 400
    ok = _orbit.task_transition(task_id, action_map[action], reason=reason)
    return jsonify({"ok": ok})


# =============================================================================
# ORBIT: Threads
# =============================================================================

@app.route("/api/orbit/threads")
@require_auth
def api_orbit_threads():
    """WP8: legacy_compat — Thread-System nicht mehr aktiv."""
    return jsonify({"threads": [], "_legacy_compat": True})


def _api_orbit_threads_legacy():
    """WP8: delete_candidate."""
    try:
        conn = get_db_connection()
        where, params = ["1=1"], []
        sv = request.args.get("status", "new,watching")
        if sv:
            statuses = [s.strip() for s in sv.split(",") if s.strip()]
            if statuses:
                where.append(f"status IN ({','.join('?'*len(statuses))})")
                params.extend(statuses)
        rows = conn.execute(
            f"SELECT * FROM orbit_threads WHERE {' AND '.join(where)} ORDER BY created_at DESC LIMIT 200",
            params
        ).fetchall()
        return jsonify({"threads": [dict(r) for r in rows]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/orbit/threads/<thread_id>/action", methods=["POST"])
@require_auth
def api_orbit_thread_action(thread_id):
    """WP8: legacy_compat — Thread-Aktionen deaktiviert."""
    return jsonify({"ok": False, "reason": "legacy_compat (WP8)"})


def _api_orbit_thread_action_legacy(thread_id):
    import orbit as _orbit
    data = request.get_json() or {}
    action = data.get("action", "")
    reason = data.get("reason", "Dashboard: manuell")
    if action == "discard":
        ok = _orbit.discard_thread(thread_id, reason=reason)
        return jsonify({"ok": ok})
    return jsonify({"error": "Unbekannte Aktion"}), 400


# =============================================================================
# ORBIT: Steps
# =============================================================================

@app.route("/api/orbit/steps")
@require_auth
def api_orbit_steps():
    try:
        conn = get_db_connection()
        where, params = ["1=1"], []
        sv = request.args.get("status", "running,blocked,ready,deferred")
        if sv:
            statuses = [s.strip() for s in sv.split(",") if s.strip()]
            if statuses:
                where.append(f"status IN ({','.join('?'*len(statuses))})")
                params.extend(statuses)
        rows = conn.execute(
            f"SELECT * FROM orbit_steps WHERE {' AND '.join(where)} ORDER BY created_at DESC LIMIT 100",
            params
        ).fetchall()
        return jsonify({"steps": [dict(r) for r in rows]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# =============================================================================
# ORBIT: Proaktive Meldungen
# =============================================================================

@app.route("/api/orbit/proactive")
@require_auth
def api_orbit_proactive():
    try:
        conn = get_db_connection()
        where, params = ["1=1"], []
        sv = request.args.get("status", "candidate,too_early,scheduled")
        if sv:
            statuses = [s.strip() for s in sv.split(",") if s.strip()]
            if statuses:
                where.append(f"release_state IN ({','.join('?'*len(statuses))})")
                params.extend(statuses)
        rows = conn.execute(
            f"SELECT * FROM orbit_proactive_messages WHERE {' AND '.join(where)} ORDER BY created_at DESC LIMIT 100",
            params
        ).fetchall()
        return jsonify({"messages": [dict(r) for r in rows]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# =============================================================================
# ORBIT: Decisions
# =============================================================================

@app.route("/api/orbit/decisions")
@require_auth
def api_orbit_decisions():
    try:
        limit = min(int(request.args.get("limit", 50)), 200)
        conn = get_db_connection()
        rows = conn.execute(
            "SELECT * FROM orbit_decisions ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return jsonify({"decisions": [dict(r) for r in rows]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# =============================================================================
# ORBIT: Reviews
# =============================================================================

@app.route("/api/orbit/reviews")
@require_auth
def api_orbit_reviews():
    try:
        limit = min(int(request.args.get("limit", 30)), 100)
        conn = get_db_connection()
        rows = conn.execute(
            "SELECT * FROM orbit_reviews ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return jsonify({"reviews": [dict(r) for r in rows]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# =============================================================================
# ORBIT: Recovery
# =============================================================================

@app.route("/api/orbit/recovery")
@require_auth
def api_orbit_recovery():
    try:
        limit = min(int(request.args.get("limit", 10)), 50)
        conn = get_db_connection()
        rows = conn.execute(
            "SELECT * FROM orbit_recovery_reports ORDER BY started_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return jsonify({"reports": [dict(r) for r in rows]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# =============================================================================
# ORBIT: Policies
# =============================================================================

@app.route("/api/orbit/policies")
@require_auth
def api_orbit_policies():
    try:
        conn = get_db_connection()
        where, params = ["1=1"], []
        sv = request.args.get("status", "active")
        if sv: where.append("status=?"); params.append(sv)
        rows = conn.execute(
            f"SELECT * FROM orbit_policies WHERE {' AND '.join(where)} ORDER BY rank DESC, created_at DESC LIMIT 200",
            params
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            try: d["scope"] = json.loads(d.get("scope") or "[]")
            except Exception: d["scope"] = []
            result.append(d)
        return jsonify({"policies": result})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/orbit/policies/<policy_id>/action", methods=["POST"])
@require_auth
def api_orbit_policy_action(policy_id):
    import orbit as _orbit
    data = request.get_json() or {}
    action = data.get("action", "")
    reason = data.get("reason", f"Dashboard: {action}")
    if action == "suppress":
        ok = _orbit.suppress_policy(policy_id, reason=reason)
        return jsonify({"ok": ok})
    elif action == "activate":
        ok = _orbit.activate_policy(policy_id, reason=reason)
        return jsonify({"ok": ok})
    elif action == "retire":
        ok = _orbit.retire_policy(policy_id, reason=reason)
        return jsonify({"ok": ok})
    return jsonify({"error": "Unbekannte Aktion"}), 400


# =============================================================================
# ORBIT: Routinen
# =============================================================================

@app.route("/api/orbit/routines")
@require_auth
def api_orbit_routines():
    try:
        conn = get_db_connection()
        where, params = ["1=1"], []
        sv = request.args.get("status", "active")
        if sv: where.append("status=?"); params.append(sv)
        rows = conn.execute(
            f"SELECT * FROM orbit_routines WHERE {' AND '.join(where)} ORDER BY created_at DESC LIMIT 200",
            params
        ).fetchall()
        return jsonify({"routines": [dict(r) for r in rows]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/orbit/routines/<routine_id>/action", methods=["POST"])
@require_auth
def api_orbit_routine_action(routine_id):
    import orbit as _orbit
    data = request.get_json() or {}
    action = data.get("action", "")
    reason = data.get("reason", f"Dashboard: {action}")
    if action == "suppress":
        conn = get_db_connection()
        conn.execute("UPDATE orbit_routines SET status='suppressed' WHERE id=?", (routine_id,))
        conn.commit()
        return jsonify({"ok": True})
    elif action == "activate":
        ok = _orbit.activate_routine(routine_id, reason=reason)
        return jsonify({"ok": ok})
    return jsonify({"error": "Unbekannte Aktion"}), 400


# =============================================================================
# Main
# =============================================================================

if __name__ == "__main__":
    logger.info("Dashboard startet auf Port 5001...")
    logger.info("URL: http://localhost:5001")
    app.run(host="0.0.0.0", port=5001, debug=False)
