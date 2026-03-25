"""
core/gate_service.py -- 5.1 Write-Gate und Verifikation

Jede Schreiboperation laeuft durch diesen Service:
1. Risk Matrix -- Klasse und Gate bestimmen
2. Preflight -- Vorbedingungen pruefen
3. Execute -- Write ausfuehren
4. Verify -- Ergebnis verifizieren
5. Audit -- Audit-Eintrag schreiben

Klassen:
  A -- intern, reversibel, low-risk (workspace)
  B -- operativ, kontrollierbar (todos)
  C -- hochriskant / extern (kalender, mail) -- in 5.1 GESPERRT (5.2: Klasse B-Erweiterung + Approval-Flow fuer Klasse C)

Gates:
  none -- direkt ausfuehren
  soft -- Preflight + Verify erzwungen
  hard -- Approval noetig (5.2+)
  blocked -- nicht erlaubt in 5.1
"""
import logging
import os
from core.datetime_utils import to_iso

logger = logging.getLogger(__name__)


def _write_result(ok: bool, result=None, error: str = None,
                  audit_id: int = None, gate: str = "",
                  verified: bool = False, verify_msg: str = "",
                  pending: bool = False, write_request_id: int = None,
                  preview: str = "", message: str = "") -> dict:
    """
    5.6.1: Einheitliches Ergebnisobjekt fuer alle execute_write-Rueckgaben.
    Garantiert dass alle Felder immer vorhanden sind.
    """
    return {
        "ok":               ok,
        "result":           result,
        "error":            error,
        "audit_id":         audit_id,
        "gate":             gate,
        "verified":         verified,
        "verify_msg":       verify_msg,
        "pending":          pending,
        "write_request_id": write_request_id,
        "preview":          preview,
        "message":          message,
    }

# =============================================================================
# Risk Matrix -- 5.1 Policy
# =============================================================================

RISK_MATRIX = {
    # Klasse A -- Workspace (intern, begrenzt)
    "workspace.save":   {"class": "A", "gate": "soft",    "verify": True,  "reversible": True,  "approval": False, "compensation": "Datei erneut schreiben"},
    "workspace.delete": {"class": "A", "gate": "soft",    "verify": True,  "reversible": False, "approval": False, "compensation": "Datei nicht wiederherstellbar -- Backup pruefen"},
    "workspace.list":   {"class": "A", "gate": "none",    "verify": False, "reversible": True,  "approval": False, "compensation": None},
    "workspace.read":   {"class": "A", "gate": "none",    "verify": False, "reversible": True,  "approval": False, "compensation": None},

    # Klasse B -- Todos (operativ, kontrollierbar)
    "todos.create":     {"class": "B", "gate": "soft",    "verify": True,  "reversible": True,  "approval": False, "compensation": "Todo manuell anlegen"},
    "todos.status":     {"class": "B", "gate": "soft",    "verify": True,  "reversible": True,  "approval": False, "compensation": "Status manuell korrigieren"},
    "todos.complete":   {"class": "B", "gate": "soft",    "verify": True,  "reversible": False, "approval": False, "compensation": "Todo manuell neu eroeffnen"},

    # Klasse B-Erweiterung -- Preview + Soft-Approval (5.2)
    "calendar.write":   {"class": "B", "gate": "needs_approval", "verify": True,  "reversible": True,  "approval": True,  "compensation": "Termin manuell anlegen oder Request neu erstellen"},
    "calendar.change":  {"class": "B", "gate": "needs_approval", "verify": True,  "reversible": True,  "approval": True,  "compensation": "Aenderung manuell ausfuehren"},
    "calendar.delete":  {"class": "C", "gate": "blocked",        "verify": False, "reversible": False, "approval": True,  "compensation": None},

    # Klasse B -- Proposal-Statusaenderungen (5.5)
    "proposal.approve": {"class": "B", "gate": "needs_approval", "verify": True,  "reversible": True,  "approval": True,  "compensation": "Proposal manuell genehmigen"},
    "proposal.reject":  {"class": "B", "gate": "needs_approval", "verify": True,  "reversible": True,  "approval": True,  "compensation": "Proposal manuell ablehnen oder Status zuruecksetzen"},
    "proposal.defer":   {"class": "B", "gate": "needs_approval", "verify": True,  "reversible": True,  "approval": True,  "compensation": "Proposal manuell zurueckstellen"},

    # 7.x -- Workspace Artifact Actions (Klasse A)
    "workspace.artifact_create":      {"class": "A", "gate": "soft", "verify": True,  "reversible": True,  "approval": False, "compensation": "Artefakt manuell anlegen"},
    "workspace.artifact_update":      {"class": "A", "gate": "soft", "verify": True,  "reversible": True,  "approval": False, "compensation": "Inhalt manuell korrigieren"},
    "workspace.worklog_append":       {"class": "A", "gate": "soft", "verify": True,  "reversible": False, "approval": False, "compensation": "Worklog-Eintrag manuell ergaenzen"},
    "workspace.materialize_execution":{"class": "A", "gate": "soft", "verify": True,  "reversible": True,  "approval": False, "compensation": "Artefakt manuell materialisieren"},
    "workspace.artifact_delete":      {"class": "B", "gate": "soft", "verify": True,  "reversible": False, "approval": False, "compensation": "Artefakt ist archiviert -- kein Restore"},

    # Klasse C -- weiter gesperrt
    "mail.send":        {"class": "C", "gate": "blocked",        "verify": False, "reversible": False, "approval": True,  "compensation": None},
    "external.write":   {"class": "C", "gate": "blocked",        "verify": False, "reversible": False, "approval": True,  "compensation": None},
}

# Gate-Ausgaenge 5.2
GATE_ALLOW           = "allow"
GATE_DENY            = "deny"
GATE_NEEDS_APPROVAL  = "needs_approval"
GATE_PREVIEW_ONLY    = "preview_only"
GATE_BLOCKED         = "blocked"

# Erlaubter Workspace-Scope
WORKSPACE_DIR = None  # wird lazy gesetzt


def _get_workspace() -> str:
    global WORKSPACE_DIR
    if WORKSPACE_DIR is None:
        import os
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        WORKSPACE_DIR = os.path.join(base, "kimi_workspace")
    return WORKSPACE_DIR


def get_policy(action_key: str) -> dict:
    """Gibt Policy fuer eine Aktion zurueck. Unbekannte Aktionen -> blocked."""
    return RISK_MATRIX.get(action_key, {"class": "C", "gate": "blocked",
                                         "verify": False, "reversible": False, "approval": True})


# =============================================================================
# Preflight-Checks
# =============================================================================

def preflight_workspace(action: str, params: dict) -> tuple[bool, str]:
    """
    Preflight fuer Workspace-Writes.
    Prueft: Scope, Pfad, Dateiname.
    """
    workspace = _get_workspace()
    fname = params.get("filename", params.get("content", ""))

    if not fname and action != "list":
        return False, "Kein Dateiname angegeben"

    if fname:
        # Path-Traversal verhindern
        basename = os.path.basename(fname)
        if basename != fname and ".." in fname:
            return False, f"Pfad-Traversal nicht erlaubt: {fname}"
        if basename.startswith(".") and basename not in (".gitignore",):
            return False, f"Versteckte Dateien nicht erlaubt: {basename}"

        # Zielordner muss im Workspace liegen
        target = os.path.join(workspace, basename)
        if not target.startswith(workspace):
            return False, f"Ziel ausserhalb des Workspace: {target}"

        # Bei Delete: Datei muss existieren
        if action == "delete":
            if not os.path.exists(target):
                return False, f"Datei nicht gefunden: {basename}"

    return True, "ok"


def preflight_todo(action: str, params: dict, owner_id: str) -> tuple[bool, str]:
    """
    Preflight fuer Todo-Writes.
    Prueft: Todo existiert, Statuswechsel zulaessig, Nutzerbezug.
    """
    from core.database import get_connection
    todo_id = params.get("id") or params.get("todo_id")

    if action in ("status", "complete") and not todo_id:
        return False, "Keine Todo-ID angegeben"

    if todo_id:
        conn = get_connection()
        try:
            row = conn.execute("SELECT * FROM todos WHERE id=?", (int(todo_id),)).fetchone()
            if not row:
                return False, f"Todo #{todo_id} nicht gefunden"
            todo = dict(row)

            # Statuswechsel-Validierung
            valid_transitions = {
                "open":        {"in_progress", "blocked", "done"},
                "in_progress": {"blocked", "done", "open"},
                "blocked":     {"open", "in_progress"},
                "done":        set(),
            }
            new_status = params.get("status")
            if new_status:
                current = todo.get("status", "open")
                allowed = valid_transitions.get(current, set())
                if new_status not in allowed:
                    return False, f"Statuswechsel {current} -> {new_status} nicht erlaubt"
        finally:
            conn.close()

    if action == "create":
        if not params.get("title"):
            return False, "Kein Titel fuer Todo angegeben"

    return True, "ok"


# =============================================================================
# Post-Write-Verifikation
# =============================================================================

def verify_workspace(action: str, params: dict) -> tuple[bool, str]:
    """Verifiziert Workspace-Write."""
    workspace = _get_workspace()
    fname = params.get("filename", "")
    if not fname:
        return True, "ok (kein Dateiname -- kein Verify)"

    basename = os.path.basename(fname)
    target = os.path.join(workspace, basename)

    if action == "save":
        if not os.path.exists(target):
            return False, f"Datei nach Write nicht gefunden: {basename}"
        size = os.path.getsize(target)
        content_len = len(params.get("code", params.get("content", "")))
        if content_len > 0 and size == 0:
            return False, f"Datei ist leer nach Write: {basename}"
        return True, f"Datei verifiziert: {basename} ({size} Bytes)"

    elif action == "delete":
        if os.path.exists(target):
            return False, f"Datei noch vorhanden nach Delete: {basename}"
        return True, f"Datei geloescht: {basename}"

    return True, "ok"


def verify_todo(action: str, params: dict, expected_result: dict) -> tuple[bool, str]:
    """Verifiziert Todo-Write."""
    from core.database import get_connection
    todo_id = params.get("id") or params.get("todo_id") or (expected_result or {}).get("id")

    if not todo_id:
        return True, "ok (kein Todo-ID -- kein Verify)"

    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM todos WHERE id=?", (int(todo_id),)).fetchone()
        if not row:
            return False, f"Todo #{todo_id} nicht in DB nach Write"
        todo = dict(row)

        if action == "status":
            expected_status = params.get("status")
            if expected_status and todo.get("status") != expected_status:
                return False, f"Status nicht geaendert: erwartet {expected_status}, ist {todo.get('status')}"

        return True, f"Todo #{todo_id} verifiziert (status: {todo.get('status')})"
    finally:
        conn.close()


# =============================================================================
# Audit
# =============================================================================

def write_audit(owner_id: str, action_type: str, tool_ref: str,
                risk_class: str, gate_result: str,
                preflight_result: str = None, write_result: str = None,
                verify_result: str = None, target_ref: str = None,
                target_scope: str = None, success: bool = False,
                error: str = None, task_id: str = None,
                step_id: str = None) -> int | None:
    """Schreibt Audit-Eintrag fuer jeden Write-Versuch."""
    try:
        from core.database import get_connection
        conn = get_connection()
        try:
            cur = conn.execute(
                """INSERT INTO write_audit
                   (owner_id, task_id, step_id, action_type, tool_ref,
                    risk_class, gate_result, preflight_result, write_result,
                    verify_result, target_ref, target_scope, success, error, executed_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (owner_id, task_id, step_id, action_type, tool_ref,
                 risk_class, gate_result, preflight_result, write_result,
                 verify_result, target_ref, target_scope,
                 1 if success else 0, error, to_iso())
            )
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"write_audit fehlgeschlagen: {e}")
        return None


# =============================================================================
# Preview + Approval-Lebenszyklus (5.2)
# =============================================================================

def create_write_request(owner_id: str, action_key: str, params: dict,
                          preview_text: str, reason: str = "",
                          task_id: str = None, step_id: str = None,
                          expires_hours: int = 24,
                          origin_todo_id: int = None,
                          origin_goal_id: int = None,
                          after_approve_action: str = "continue_line",
                          after_reject_action: str = "replan",
                          secondary_line_id: int = None,
                          secondary_line_type: str = None) -> dict | None:
    """
    Legt einen Write-Request an -- Preview-Entitaet fuer Approval-Flow.
    Gibt den angelegten Request zurueck.
    """
    try:
        import json, datetime
        from core.database import get_connection
        policy = get_policy(action_key)
        tool_ref = action_key.split(".")[0]
        target_ref = params.get("filename") or str(params.get("id","")) or params.get("event_id","")
        target_scope = tool_ref
        now = to_iso()
        expires_dt = (datetime.datetime.now(datetime.timezone.utc)
                      + datetime.timedelta(hours=expires_hours))
        expires_at = expires_dt.isoformat()

        conn = get_connection()
        try:
            # origin_todo_id aus task ableiten falls nicht explizit
            _origin_todo_id = origin_todo_id
            if not _origin_todo_id and task_id:
                try:
                    t = conn.execute("SELECT linked_todo_id FROM orbit_tasks WHERE id=?", (task_id,)).fetchone()
                    if t and t["linked_todo_id"]:
                        _origin_todo_id = t["linked_todo_id"]
                except Exception:
                    pass

            cur = conn.execute(
                """INSERT INTO write_requests
                   (owner_id, task_id, step_id, action_key, tool_ref, risk_class,
                    target_ref, target_scope, preview_payload, preview_text,
                    reason, approval_status, approval_required,
                    created_at, expires_at,
                    origin_todo_id, origin_goal_id,
                    after_approve_action, after_reject_action,
                    secondary_line_id, secondary_line_type,
                    line_status)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,'pending',?,?,?,?,?,?,?,?,?,'pending_approval')""",
                (owner_id, task_id, step_id, action_key, tool_ref,
                 policy["class"], target_ref, target_scope,
                 json.dumps(params), preview_text[:1000],
                 reason[:300], 1 if policy.get("approval") else 0,
                 now, expires_at,
                 _origin_todo_id, origin_goal_id,
                 after_approve_action, after_reject_action,
                 secondary_line_id, secondary_line_type)
            )
            conn.commit()
            req_id = cur.lastrowid
            logger.info(f"write_request #{req_id} angelegt: {action_key} | {target_ref[:40]}")
            return get_write_request(req_id)
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"create_write_request fehlgeschlagen: {e}")
        return None


def get_write_request(req_id: int) -> dict | None:
    try:
        from core.database import get_connection
        conn = get_connection()
        try:
            row = conn.execute("SELECT * FROM write_requests WHERE id=?", (req_id,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"get_write_request fehlgeschlagen: {e}")
        return None


def get_pending_write_requests(owner_id: str) -> list:
    """Gibt alle offenen Write-Requests zurueck."""
    try:
        from core.database import get_connection
        conn = get_connection()
        try:
            rows = conn.execute(
                """SELECT * FROM write_requests
                   WHERE owner_id=? AND approval_status='pending'
                   ORDER BY created_at DESC""",
                (owner_id,)
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"get_pending_write_requests fehlgeschlagen: {e}")
        return []


def approve_write_request(req_id: int, approved_by: str = "user") -> dict:
    """
    Genehmigt einen Write-Request und fuehrt den Write aus.
    Gibt Ergebnis-Dict zurueck.
    """
    import json
    req = get_write_request(req_id)
    if not req:
        return {"ok": False, "error": f"Write-Request #{req_id} nicht gefunden"}

    if req["approval_status"] != "pending":
        return {"ok": False, "error": f"Request ist nicht mehr pending: {req['approval_status']}"}

    # Params wiederherstellen
    try:
        params = json.loads(req["preview_payload"] or "{}")
    except Exception:
        params = {}

    action_key = req["action_key"]
    owner_id = req["owner_id"]

    # Execute-Funktion je Tool
    tool_ref = req["tool_ref"]
    action = action_key.split(".", 1)[1] if "." in action_key else action_key
    def _execute_fn(p):
        if tool_ref == "calendar":
            return _execute_calendar_write(action_key, p)
        elif tool_ref == "proposal":
            return execute_proposal_action(action, p)
        return {"success": False, "error": f"Kein Execute-Handler fuer {tool_ref}"}

    # Write ausfuehren
    result = execute_write(
        action_key, params, owner_id, _execute_fn,
        task_id=req.get("task_id"), step_id=req.get("step_id")
    )

    # Request-Status aktualisieren
    new_status = "executed" if result["ok"] else "failed"
    try:
        from core.database import get_connection
        conn = get_connection()
        conn.execute(
            """UPDATE write_requests SET
               approval_status=?, approved_by=?, approved_at=?,
               executed_at=?, verification_status=?, audit_id=?
               WHERE id=?""",
            (new_status, approved_by, to_iso(), to_iso(),
             "verified" if result.get("verified") else "unverified",
             result.get("audit_id"), req_id)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning(f"approve_write_request: Status-Update fehlgeschlagen: {e}")

    # 5.5: Proposal-Approve -> Todo ableiten
    if result["ok"] and tool_ref == "proposal" and action == "approve":
        try:
            import json as _j
            params_raw = _j.loads(req.get("preview_payload") or "{}")
            proposal_id = params_raw.get("id") or params_raw.get("proposal_id")
            if proposal_id:
                from core.database import get_connection as _gc_p
                _cp = _gc_p()
                prop = _cp.execute(
                    "SELECT * FROM kimi_proposals WHERE id=?", (int(proposal_id),)
                ).fetchone()
                _cp.close()
                if prop:
                    prop = dict(prop)
                    # Todo anlegen falls noch keines verknuepft
                    existing = None
                    try:
                        _cp2 = _gc_p()
                        existing = _cp2.execute(
                            "SELECT id FROM todos WHERE proposal_id=?",
                            (proposal_id,)
                        ).fetchone()
                        _cp2.close()
                    except Exception:
                        pass
                    if not existing:
                        from core.todo_service import create_todo
                        from config import OWNER_ID
                        create_todo(
                            owner_id=req.get("owner_id", OWNER_ID),
                            title=f"[Approved] {prop.get('title','')[:80]}",
                            category="kimi",
                            execution_mode="orbit_internal",
                            release_mode="summarize",
                            task_template="implementation",
                            proposal_id=int(proposal_id),
                            goal_id=prop.get("goal_id"),
                        )
                        logger.info(f"approve proposal #{proposal_id}: Todo angelegt")
        except Exception as e:
            logger.debug(f"approve proposal -> Todo-Ableitung fehlgeschlagen: {e}")

    # 5.4: Differenzierte Approve-Folge
    after_approve = req.get("after_approve_action", "continue_line")
    if result["ok"] and req.get("task_id"):
        try:
            import orbit as _orbit
            if after_approve == "complete_line":
                _orbit.task_transition(req["task_id"], "completed", reason="Approve: abgeschlossen")
                logger.info(f"approve: Task {req['task_id'][:8]} -> completed")
            else:  # continue_line (default)
                _orbit.update_task(req["task_id"], status="active")
                logger.info(f"approve: Task {req['task_id'][:8]} -> active (continue_line)")
        except Exception as e:
            logger.debug(f"approve: Task-Folge fehlgeschlagen: {e}")
    elif not result["ok"] and req.get("task_id"):
        # Write fehlgeschlagen nach Approval -- Task reaktivieren, Planner soll umplanen
        try:
            import orbit as _orbit
            _orbit.update_task(req["task_id"], status="active")
        except Exception:
            pass

    # line_status aktualisieren
    try:
        from core.database import get_connection as _gc4
        _c4 = _gc4()
        _c4.execute("UPDATE write_requests SET line_status=? WHERE id=?",
                    ("executed" if result["ok"] else "failed", req_id))
        _c4.commit()
        _c4.close()
    except Exception:
        pass

    # 5.4: Planner-Folgeentscheidung auslosen
    _trigger_planner_consequence(req, "approved" if result["ok"] else "rejected")

    return result


def reject_write_request(req_id: int, reason: str = "", rejected_by: str = "user") -> bool:
    """Lehnt einen Write-Request ab und reaktiviert den Task."""
    try:
        from core.database import get_connection
        req = get_write_request(req_id)
        conn = get_connection()
        try:
            conn.execute(
                """UPDATE write_requests SET
                   approval_status='rejected', rejected_reason=?,
                   approved_by=?, approved_at=?
                   WHERE id=?""",
                (reason[:300], rejected_by, to_iso(), req_id)
            )
            conn.commit()
            logger.info(f"write_request #{req_id} abgelehnt: {reason[:60]}")
        finally:
            conn.close()

        # 5.4: Differenzierte Reject-Folge
        after_reject = req.get("after_reject_action", "replan") if req else "replan"
        if req and req.get("task_id"):
            try:
                import orbit as _orbit
                obs_content = f"Write-Request #{req_id} abgelehnt: {reason[:100] or 'kein Grund'}"
                if after_reject == "pause":
                    _orbit.update_task(req["task_id"], status="waiting")
                    logger.info(f"reject: Task {req['task_id'][:8]} -> waiting (pause)")
                elif after_reject == "close":
                    _orbit.task_transition(req["task_id"], "aborted", reason=f"Reject: {reason[:60]}")
                    logger.info(f"reject: Task {req['task_id'][:8]} -> aborted")
                else:  # replan (default)
                    _orbit.update_task(req["task_id"], status="active")
                    logger.info(f"reject: Task {req['task_id'][:8]} -> active (replan)")
                # Observation schreiben
                try:
                    from core.todo_service import record_observation
                    from config import OWNER_ID
                    record_observation(owner_id=req.get("owner_id", OWNER_ID),
                                       content=obs_content, obs_type="blocker",
                                       task_id=req["task_id"])
                except Exception:
                    pass
                # line_status
                from core.database import get_connection as _gc5
                _c5 = _gc5()
                _c5.execute("UPDATE write_requests SET line_status='rejected' WHERE id=?", (req_id,))
                _c5.commit()
                _c5.close()
            except Exception as e:
                logger.debug(f"reject: Folge fehlgeschlagen: {e}")

        # 5.4: Planner-Folgeentscheidung
        _trigger_planner_consequence(req, "rejected")
        return True
    except Exception as e:
        logger.warning(f"reject_write_request fehlgeschlagen: {e}")
        return False


def defer_write_request(req_id: int, defer_hours: int = 24,
                         reason: str = "", deferred_by: str = "user") -> bool:
    """
    5.3: Verschiebt einen Write-Request -- Entscheidung spaeter.
    Linie bleibt offen aber wird nicht hektisch neu vorgeschlagen.
    """
    try:
        import datetime
        from core.database import get_connection
        req = get_write_request(req_id)
        deferred_until = (datetime.datetime.now(datetime.timezone.utc)
                          + datetime.timedelta(hours=defer_hours)).isoformat()
        conn = get_connection()
        try:
            conn.execute(
                """UPDATE write_requests SET
                   approval_status='deferred', rejected_reason=?,
                   approved_by=?, approved_at=?, expires_at=?
                   WHERE id=?""",
                (f"deferred: {reason}"[:300], deferred_by, to_iso(), deferred_until, req_id)
            )
            conn.commit()
            logger.info(f"write_request #{req_id} deferred bis {deferred_until[:16]}")
        finally:
            conn.close()

        # Task auf waiting setzen (nicht waiting_user_decision -- weniger urgent)
        if req and req.get("task_id"):
            try:
                import orbit as _orbit
                _orbit.update_task(req["task_id"], status="waiting")
                from core.todo_service import record_observation
                from config import OWNER_ID
                record_observation(
                    owner_id=req.get("owner_id", OWNER_ID),
                    content=f"Write-Request #{req_id} verschoben fuer {defer_hours}h: {reason[:80]}",
                    obs_type="state_change",
                    task_id=req["task_id"],
                )
            except Exception as e:
                logger.debug(f"defer: Task-Status fehlgeschlagen: {e}")

        # 5.4: Planner-Folgeentscheidung
        _trigger_planner_consequence(req, "deferred")
        return True
    except Exception as e:
        logger.warning(f"defer_write_request fehlgeschlagen: {e}")
        return False


def expire_stale_write_requests(owner_id: str) -> int:
    """
    5.3: Markiert abgelaufene Write-Requests als expired.
    Gibt Anzahl abgelaufener Requests zurueck.
    """
    try:
        import datetime
        from core.database import get_connection
        now = to_iso()
        conn = get_connection()
        try:
            rows = conn.execute(
                """SELECT id, task_id, owner_id FROM write_requests
                   WHERE owner_id=? AND approval_status='pending'
                   AND expires_at IS NOT NULL AND expires_at < ?""",
                (owner_id, now)
            ).fetchall()
            for r in rows:
                conn.execute(
                    "UPDATE write_requests SET approval_status='expired' WHERE id=?",
                    (r["id"],)
                )
                # Task reaktivieren
                if r.get("task_id"):
                    try:
                        import orbit as _orbit
                        _orbit.update_task(r["task_id"], status="active")
                    except Exception:
                        pass
            conn.commit()
            if rows:
                logger.info(f"expire_stale: {len(rows)} Write-Requests abgelaufen")
                # 5.4: Planner-Folge fuer jeden abgelaufenen Request
                for r in rows:
                    _trigger_planner_consequence(
                        {"owner_id": owner_id, "task_id": r.get("task_id"),
                         "action_key": r.get("action_key",""),
                         "origin_todo_id": r.get("origin_todo_id"),
                         "after_reject_action": "replan"},
                        "expired"
                    )
            return len(rows)
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"expire_stale_write_requests fehlgeschlagen: {e}")
        return 0


def _trigger_planner_consequence(req: dict, outcome: str) -> None:
    """
    5.4: Loest eine echte Planner-Folgeentscheidung aus.
    outcome: 'approved' | 'rejected' | 'deferred' | 'expired'
    """
    try:
        from core.planner import save_planner_focus, pause_focus, get_planner_focus
        from config import OWNER_ID
        owner_id = req.get("owner_id", OWNER_ID)
        task_id = req.get("task_id")
        after_approve = req.get("after_approve_action", "continue_line")
        after_reject = req.get("after_reject_action", "replan")

        focus = get_planner_focus(owner_id)

        if outcome == "approved":
            # Fokus zurueck auf origin_todo wenn vorhanden
            origin_todo = req.get("origin_todo_id")
            if origin_todo and focus:
                save_planner_focus(
                    owner_id=owner_id,
                    primary_type="todo", primary_id=int(origin_todo),
                    reason=f"Freigabe erteilt: {req.get('action_key','')} -- {after_approve}",
                    confidence=0.9,
                )
                logger.info(f"planner_consequence: approve -> Fokus auf Todo #{origin_todo}")

        elif outcome == "rejected":
            if after_reject == "replan":
                # Fokus-Confidence senken -- Planner soll neu bewerten
                if focus:
                    save_planner_focus(
                        owner_id=owner_id,
                        primary_type=focus.get("primary_line_type","todo"),
                        primary_id=focus.get("primary_line_id", 0),
                        reason=f"Reject: {req.get('action_key','')} -- Replanning",
                        confidence=0.3,  # niedrig -> should_replan wird bald True
                    )
            elif after_reject in ("pause", "close"):
                # Fokus pausieren
                pause_focus(owner_id, reason=f"Reject: {req.get('action_key','')}")
            logger.info(f"planner_consequence: reject({after_reject}) -> Fokus angepasst")

        elif outcome == "deferred":
            # Fokus auf Nebenlinie verlagern falls vorhanden
            secondary_id = req.get("secondary_line_id")
            if secondary_id and focus:
                save_planner_focus(
                    owner_id=owner_id,
                    primary_type=focus.get("primary_line_type","todo"),
                    primary_id=focus.get("primary_line_id", 0),
                    secondary_type="todo", secondary_id=int(secondary_id),
                    reason=f"Defer: {req.get('action_key','')} verschoben -- Nebenlinie aktiv",
                    confidence=0.7,
                )
            logger.info(f"planner_consequence: defer -> Nebenlinie #{secondary_id}")

        elif outcome == "expired":
            # Fokus-Confidence sehr niedrig -- zwingt Replan
            if focus:
                save_planner_focus(
                    owner_id=owner_id,
                    primary_type=focus.get("primary_line_type","todo"),
                    primary_id=focus.get("primary_line_id", 0),
                    reason=f"Expired: {req.get('action_key','')} -- Replan noetig",
                    confidence=0.1,
                )
            logger.info(f"planner_consequence: expired -> Replan erzwungen")

    except Exception as e:
        logger.debug(f"_trigger_planner_consequence fehlgeschlagen: {e}")


def build_calendar_preview(action: str, params: dict) -> str:
    """Erzeugt lesbaren Preview-Text fuer Kalender-Writes."""
    if action in ("write", "create"):
        return (f"Kalender-Eintrag anlegen: '{params.get('title','(kein Titel)')}' "
                f"am {params.get('start','?')} in {params.get('calendar','work')}")
    elif action == "change":
        return (f"Kalender-Eintrag aendern: Event #{params.get('event_id','?')} "
                f"-> Titel: {params.get('title','?')}, Zeit: {params.get('start','?')}")
    return f"Kalender-Aktion: {action} auf {params.get('event_id','?')}"


def _execute_calendar_write(action_key: str, params: dict) -> dict:
    """Fuehrt einen genehmigten Kalender-Write aus."""
    try:
        from tools.calendar_tool import create_event, update_event
        action = action_key.split(".", 1)[1] if "." in action_key else action_key
        if action in ("write", "create"):
            result = create_event(
                calendar=params.get("calendar", "work"),
                title=params.get("title", ""),
                start=params.get("start", ""),
                end=params.get("end", ""),
            )
            return {"success": bool(result), "result": str(result)[:200]}
        elif action == "change":
            result = update_event(
                calendar=params.get("calendar", "work"),
                event_id=params.get("event_id", ""),
                title=params.get("title"),
                start=params.get("start"),
                end=params.get("end"),
            )
            return {"success": bool(result), "result": str(result)[:200]}
        return {"success": False, "error": f"Unbekannte Kalender-Aktion: {action}"}
    except Exception as e:
        return {"success": False, "error": str(e)[:200]}


def verify_calendar(action: str, params: dict) -> tuple[bool, str]:
    """Verifiziert Kalender-Write -- best-effort."""
    return True, "Kalender-Verify: best-effort (extern)"


# =============================================================================
# Proposal-Familie (5.5)
# =============================================================================

def preflight_proposal(action: str, params: dict, owner_id: str) -> tuple[bool, str]:
    """Preflight fuer Proposal-Statusaenderungen."""
    from core.database import get_connection
    proposal_id = params.get("id") or params.get("proposal_id")

    if not proposal_id:
        return False, "Keine Proposal-ID angegeben"

    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM kimi_proposals WHERE id=?", (int(proposal_id),)
        ).fetchone()
        if not row:
            return False, f"Proposal #{proposal_id} nicht gefunden"
        proposal = dict(row)

        # Zulässige Statusübergänge
        valid_transitions = {
            "approve": {"pending", "deferred"},
            "reject":  {"pending", "deferred"},
            "defer":   {"pending"},
        }
        current = proposal.get("status", "pending")
        if action not in valid_transitions:
            return False, f"Unbekannte Proposal-Aktion: {action}"
        if current not in valid_transitions[action]:
            return False, f"Proposal ist bereits '{current}' -- {action} nicht erlaubt"

    finally:
        conn.close()

    return True, "ok"


def execute_proposal_action(action: str, params: dict) -> dict:
    """
    5.6.2: Fuehrt eine Proposal-Statusaenderung aus -- ueber proposal_service.
    Strukturierte Rueckgabe mit ok, state, id.
    """
    proposal_id = int(params.get("id") or params.get("proposal_id", 0))
    if not proposal_id:
        return {"success": False, "error": "Keine Proposal-ID", "id": None}

    status_map = {"approve": "approved", "reject": "rejected", "defer": "deferred"}
    new_status = status_map.get(action)
    if not new_status:
        return {"success": False, "error": f"Unbekannte Aktion: {action}", "id": proposal_id}

    try:
        # Service-Layer bevorzugen
        if action == "approve":
            from core.proposal_service import approve_proposal
            from config import OWNER_ID
            result = approve_proposal(proposal_id, OWNER_ID)
            ok = result is not None
        elif action == "reject":
            from core.proposal_service import reject_proposal
            ok = reject_proposal(proposal_id)
        elif action == "defer":
            from core.proposal_service import defer_proposal
            ok = defer_proposal(proposal_id)
        else:
            ok = False

        return {"success": ok,
                "result": f"Proposal #{proposal_id} -> {new_status}" if ok else "Service-Fehler",
                "id": proposal_id, "new_status": new_status if ok else None}
    except ImportError:
        # Fallback: direkter DB-Write wenn proposal_service nicht verfuegbar
        logger.warning("proposal_service nicht gefunden -- Fallback auf direkten DB-Write")
        try:
            from core.database import get_connection
            import datetime
            now = datetime.datetime.now(datetime.timezone.utc).isoformat()
            conn = get_connection()
            conn.execute("UPDATE kimi_proposals SET status=?, updated_at=? WHERE id=?",
                         (new_status, now, proposal_id))
            conn.commit()
            conn.close()
            return {"success": True, "result": f"Proposal #{proposal_id} -> {new_status}",
                    "id": proposal_id, "new_status": new_status}
        except Exception as e2:
            return {"success": False, "error": str(e2)[:200], "id": proposal_id}
    except Exception as e:
        return {"success": False, "error": str(e)[:200], "id": proposal_id}


def verify_proposal(action: str, params: dict, write_result: dict) -> tuple[bool, str]:
    """Verifiziert Proposal-Statusaenderung."""
    from core.database import get_connection
    proposal_id = params.get("id") or params.get("proposal_id") or (write_result or {}).get("id")
    if not proposal_id:
        return True, "ok (kein Verify ohne ID)"

    expected = {"approve": "approved", "reject": "rejected", "defer": "deferred"}.get(action)
    if not expected:
        return True, "ok"

    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT status FROM kimi_proposals WHERE id=?", (int(proposal_id),)
        ).fetchone()
        if not row:
            return False, f"Proposal #{proposal_id} nach Write nicht gefunden"
        actual = row["status"]
        if actual != expected:
            return False, f"Status nicht geaendert: erwartet {expected}, ist {actual}"
        return True, f"Proposal #{proposal_id} verifiziert: status={actual}"
    finally:
        conn.close()


# =============================================================================
# 7.x Artifact Preflight + Verify
# =============================================================================

def preflight_artifact(action: str, params: dict) -> tuple[bool, str]:
    """Preflight fuer Workspace-Artifact-Writes.
    WP4: temporary_compat -- Legacy-Schreibpfad (delete_candidate nach V2-Migration)
    Im Safe Mode: artifact_create / materialize_execution blockiert.
    """
    try:
        from orbit import SAFE_MODE as _SM, ENABLE_AUTO_ARTIFACTS as _EAA
    except Exception:
        _SM, _EAA = False, True
    if _SM and action in ("artifact_create", "materialize_execution") and not _EAA:
        import logging as _lg
        _lg.getLogger(__name__).debug(
            f"WP4: Legacy-Artifact-Write '{action}' im Safe Mode blockiert"
        )
        return False, f"WP4 Safe Mode: Legacy-Artifact-Write '{action}' deaktiviert"
    from core.workspace_artifact_service import (
        ARTIFACT_TYPES, ALLOWED_FORMATS, ARTIFACT_STATUSES, get_artifact
    )
    if action in ("artifact_create", "materialize_execution"):
        atype = params.get("artifact_type", "result" if action == "materialize_execution" else "")
        if atype and atype not in ARTIFACT_TYPES:
            return False, f"Ungueltiger artifact_type: '{atype}'"
        fmt = params.get("format", "md")
        if fmt not in ALLOWED_FORMATS:
            return False, f"Ungueltiges Format: '{fmt}'"
        if not params.get("content", "").strip():
            return False, "Inhalt darf nicht leer sein"
        if not params.get("line_id", ""):
            return False, "line_id fehlt"

    elif action == "artifact_update":
        artifact_id = params.get("artifact_id")
        # 7.5.4: Fallback -- artifact_id optional, line_id + artifact_type reichen
        if not artifact_id:
            line_id = params.get("line_id")
            artifact_type = params.get("artifact_type")
            if not line_id:
                return False, "artifact_update: artifact_id oder line_id erforderlich"
            from core.workspace_artifact_service import get_latest_line_artifact
            _art = get_latest_line_artifact(line_id, artifact_type)
            if not _art:
                return False, f"artifact_update: kein Artefakt fuer Linie '{line_id}' (type={artifact_type}) gefunden"
            # artifact_id in params eintragen damit der Executor sie findet
            params["artifact_id"] = _art["id"]
            artifact_id = _art["id"]
        art = get_artifact(int(artifact_id))
        if not art:
            return False, f"Artifact #{artifact_id} nicht gefunden"
        status = params.get("status")
        if status and status not in ARTIFACT_STATUSES:
            return False, f"Ungueltiger Status: '{status}'"

    elif action == "artifact_delete":
        artifact_id = params.get("artifact_id")
        if not artifact_id:
            return False, "artifact_id fehlt"
        art = get_artifact(int(artifact_id))
        if not art:
            return False, f"Artifact #{artifact_id} nicht gefunden"

    elif action == "worklog_append":
        # WP4: temporary_compat -- delete_candidate (Legacy-Worklog)
        # Im Safe Mode blockiert -- kein Legacy-Write im normalen V2-Betrieb
        try:
            from orbit import SAFE_MODE as _SM_GWL
        except Exception:
            _SM_GWL = False
        if _SM_GWL:
            return False, "WP4 Safe Mode: worklog_append ist Legacy -- im V2-Betrieb nicht erlaubt"
        if not params.get("content", "").strip():
            return False, "Worklog-Inhalt darf nicht leer sein"
        if not params.get("line_id", ""):
            return False, "line_id fehlt"

    return True, "ok"


def verify_artifact_write(action: str, params: dict, write_result: dict) -> tuple[bool, str]:
    """
    Verifiziert Artifact-Write -- je Aktion unterschiedliche Semantik.
    Delete: Datei weg + Status archived.
    Create/Update/Materialize: Datei da + DB-Eintrag korrekt.
    """
    artifact_id = (write_result or {}).get("artifact_id") or params.get("artifact_id")
    if not artifact_id:
        return True, "ok (kein artifact_id -- kein Verify)"
    try:
        from core.workspace_artifact_service import get_artifact, _full_path
        import os

        if action == "artifact_delete":
            # Delete: Datei muss weg sein, Status muss 'archived' sein
            art = get_artifact(int(artifact_id))
            if not art:
                return True, "ok (Artifact aus DB entfernt)"
            if art.get("status") != "archived":
                return False, f"Artifact #{artifact_id} Status nicht 'archived' nach Delete: {art.get('status')}"
            full_path = _full_path(art["relative_path"])
            if os.path.exists(full_path):
                return False, f"Datei noch vorhanden nach Delete: {art['relative_path']}"
            return True, f"Artifact #{artifact_id} korrekt geloescht (archived, Datei weg)"

        else:
            # Create/Update/Materialize: Datei muss da sein
            from core.workspace_artifact_service import verify_artifact
            return verify_artifact(int(artifact_id))

    except Exception as e:
        return False, f"Verify fehlgeschlagen: {e}"


def build_proposal_preview(action: str, params: dict) -> str:
    """Erzeugt lesbaren Preview-Text fuer Proposal-Statusaenderungen."""
    from core.database import get_connection
    proposal_id = params.get("id") or params.get("proposal_id")
    title = params.get("title", f"#{proposal_id}")
    if proposal_id:
        try:
            conn = get_connection()
            row = conn.execute(
                "SELECT title, status FROM kimi_proposals WHERE id=?",
                (int(proposal_id),)
            ).fetchone()
            conn.close()
            if row:
                title = row["title"][:60]
                current = row["status"]
        except Exception:
            current = "pending"
    else:
        current = "pending"

    labels = {
        "approve": f"Proposal '{title}' genehmigen ({current} → approved)",
        "reject":  f"Proposal '{title}' ablehnen ({current} → rejected)",
        "defer":   f"Proposal '{title}' zurueckstellen ({current} → deferred)",
    }
    base = labels.get(action, f"Proposal-Aktion: {action}")
    if action == "approve":
        base += " -- daraus koennte ein Todo entstehen."
    elif action == "reject":
        base += " -- Linie wird umgeplant oder geschlossen."
    elif action == "defer":
        base += " -- Wiedervorlage spaeter."
    return base


# =============================================================================
# Haupteinstieg -- execute_write
# =============================================================================

def execute_write(action_key: str, params: dict, owner_id: str,
                  execute_fn, task_id: str = None,
                  step_id: str = None) -> dict:
    """
    Zentraler Write-Ausfuehrungspfad.

    action_key: z.B. "workspace.save", "todos.status"
    params:     Parameter fuer die Aktion
    execute_fn: callable das die eigentliche Aktion ausfuehrt -> dict mit success/result/error
    task_id/step_id: fuer Audit-Kontext

    Gibt zurueck:
    {
        "ok": bool,
        "result": ...,
        "error": str | None,
        "audit_id": int | None,
        "gate": str,
        "verified": bool,
    }
    """
    policy = get_policy(action_key)
    gate = policy["gate"]
    risk_class = policy["class"]
    tool_ref, action = action_key.split(".", 1) if "." in action_key else (action_key, "")

    logger.debug(f"execute_write: {action_key} | class={risk_class} | gate={gate}")

    # Gate: blocked -> sofort ablehnen
    if gate == "blocked":
        audit_id = write_audit(
            owner_id=owner_id, action_type=action, tool_ref=tool_ref,
            risk_class=risk_class, gate_result="blocked",
            error=f"{action_key} ist gesperrt",
            success=False, task_id=task_id, step_id=step_id
        )
        return _write_result(False, error=f"{action_key} ist gesperrt (Klasse C)",
                               audit_id=audit_id, gate=GATE_BLOCKED)

    # Gate: needs_approval -> Write-Request anlegen statt sofort ausfuehren (5.2)
    if gate == "needs_approval":
        preview_text = ""
        if tool_ref == "calendar":
            preview_text = build_calendar_preview(action, params)
        elif tool_ref == "proposal":
            preview_text = build_proposal_preview(action, params)
        else:
            preview_text = f"{action_key} auf {params.get('event_id') or params.get('id','?')}"

        req = create_write_request(
            owner_id=owner_id,
            action_key=action_key,
            params=params,
            preview_text=preview_text,
            reason=params.get("reason", ""),
            task_id=task_id,
            step_id=step_id,
        )
        if req:
            logger.info(f"execute_write: Write-Request #{req['id']} angelegt fuer {action_key}")
            return _write_result(False, pending=True, gate=GATE_NEEDS_APPROVAL,
                                   write_request_id=req["id"], preview=preview_text,
                                   message=f"Write-Request #{req['id']} angelegt -- wartet auf Freigabe")
        else:
            return _write_result(False, error="Write-Request konnte nicht angelegt werden",
                                   gate=GATE_NEEDS_APPROVAL)

    # Preflight (bei soft gate erzwungen)
    preflight_ok = True
    preflight_msg = "skipped"
    if gate in ("soft", "hard"):
        if tool_ref == "workspace":
            # 7.x: artifact-Aktionen haben eigenen Preflight
            if action.startswith("artifact") or action in ("worklog_append", "materialize_execution"):
                preflight_ok, preflight_msg = preflight_artifact(action, params)
            else:
                preflight_ok, preflight_msg = preflight_workspace(action, params)
        elif tool_ref == "todos":
            preflight_ok, preflight_msg = preflight_todo(action, params, owner_id)
        elif tool_ref == "proposal":
            preflight_ok, preflight_msg = preflight_proposal(action, params, owner_id)
        else:
            preflight_msg = f"kein Preflight fuer {tool_ref}"

        if not preflight_ok:
            audit_id = write_audit(
                owner_id=owner_id, action_type=action, tool_ref=tool_ref,
                risk_class=risk_class, gate_result="preflight_failed",
                preflight_result=preflight_msg, error=preflight_msg,
                success=False, task_id=task_id, step_id=step_id
            )
            return _write_result(False, error=f"Preflight fehlgeschlagen: {preflight_msg}",
                                   audit_id=audit_id, gate=gate)

    # Write ausfuehren
    try:
        write_result = execute_fn(params)
    except Exception as e:
        err = str(e)[:300]
        audit_id = write_audit(
            owner_id=owner_id, action_type=action, tool_ref=tool_ref,
            risk_class=risk_class, gate_result=gate,
            preflight_result=preflight_msg, error=err,
            success=False, task_id=task_id, step_id=step_id
        )
        return _write_result(False, error=err, audit_id=audit_id, gate=gate)

    if not write_result.get("success", False):
        err = write_result.get("error", "Write fehlgeschlagen")
        audit_id = write_audit(
            owner_id=owner_id, action_type=action, tool_ref=tool_ref,
            risk_class=risk_class, gate_result=gate,
            preflight_result=preflight_msg,
            write_result=str(write_result.get("result",""))[:200],
            error=err, success=False, task_id=task_id, step_id=step_id
        )
        return _write_result(False, error=err, audit_id=audit_id, gate=gate)

    # Post-Write-Verifikation
    verify_ok = True
    verify_msg = "skipped"
    if policy.get("verify"):
        if tool_ref == "workspace":
            if action.startswith("artifact") or action in ("worklog_append", "materialize_execution"):
                verify_ok, verify_msg = verify_artifact_write(action, params, write_result)
            else:
                verify_ok, verify_msg = verify_workspace(action, params)
        elif tool_ref == "todos":
            verify_ok, verify_msg = verify_todo(action, params, write_result)
        elif tool_ref == "calendar":
            verify_ok, verify_msg = verify_calendar(action, params)
        elif tool_ref == "proposal":
            verify_ok, verify_msg = verify_proposal(action, params, write_result)

        if not verify_ok:
            audit_id = write_audit(
                owner_id=owner_id, action_type=action, tool_ref=tool_ref,
                risk_class=risk_class, gate_result=gate,
                preflight_result=preflight_msg,
                write_result=str(write_result.get("result",""))[:200],
                verify_result=verify_msg, error=f"Verifikation fehlgeschlagen: {verify_msg}",
                success=False, task_id=task_id, step_id=step_id
            )
            # 5.6.3: Verify-Fail -> Request-Status explizit auf 'verify_failed' + Compensation
            compensation = policy.get("compensation")
            try:
                from core.database import get_connection as _gc56
                _c56 = _gc56()
                _c56.execute(
                    "UPDATE write_requests SET verification_status='failed', line_status='verify_failed' WHERE task_id=? AND approval_status IN ('pending','executed')",
                    (task_id,)
                )
                _c56.commit()
                _c56.close()
            except Exception:
                pass
            err_msg = f"Write ausgefuehrt aber Verifikation fehlgeschlagen: {verify_msg}"
            if compensation:
                err_msg += f" -- Compensation: {compensation}"
            return _write_result(False, error=err_msg, audit_id=audit_id, gate=gate)

    # Audit -- Erfolg
    result_str = str(write_result.get("result",""))[:300]
    audit_id = write_audit(
        owner_id=owner_id, action_type=action, tool_ref=tool_ref,
        risk_class=risk_class, gate_result=gate,
        preflight_result=preflight_msg,
        write_result=result_str,
        verify_result=verify_msg,
        target_ref=params.get("filename") or str(params.get("id","")),
        target_scope="workspace" if tool_ref == "workspace" else "todos",
        success=True, task_id=task_id, step_id=step_id
    )

    logger.info(f"execute_write OK: {action_key} | audit={audit_id} | {result_str[:60]}")

    return _write_result(True,
        result=write_result.get("result"),
        audit_id=audit_id, gate=gate,
        verified=verify_ok, verify_msg=verify_msg)
