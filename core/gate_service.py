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

# =============================================================================
# Risk Matrix -- 5.1 Policy
# =============================================================================

RISK_MATRIX = {
    # Klasse A -- Workspace (intern, begrenzt)
    "workspace.save":   {"class": "A", "gate": "soft",    "verify": True,  "reversible": True,  "approval": False},
    "workspace.delete": {"class": "A", "gate": "soft",    "verify": True,  "reversible": False, "approval": False},
    "workspace.list":   {"class": "A", "gate": "none",    "verify": False, "reversible": True,  "approval": False},
    "workspace.read":   {"class": "A", "gate": "none",    "verify": False, "reversible": True,  "approval": False},

    # Klasse B -- Todos (operativ, kontrollierbar)
    "todos.create":     {"class": "B", "gate": "soft",    "verify": True,  "reversible": True,  "approval": False},
    "todos.status":     {"class": "B", "gate": "soft",    "verify": True,  "reversible": True,  "approval": False},
    "todos.complete":   {"class": "B", "gate": "soft",    "verify": True,  "reversible": False, "approval": False},

    # Klasse B-Erweiterung -- Preview + Soft-Approval (5.2)
    "calendar.write":   {"class": "B", "gate": "needs_approval", "verify": True,  "reversible": True,  "approval": True},
    "calendar.change":  {"class": "B", "gate": "needs_approval", "verify": True,  "reversible": True,  "approval": True},
    "calendar.delete":  {"class": "C", "gate": "blocked",        "verify": False, "reversible": False, "approval": True},

    # Klasse C -- weiter gesperrt
    "mail.send":        {"class": "C", "gate": "blocked",        "verify": False, "reversible": False, "approval": True},
    "external.write":   {"class": "C", "gate": "blocked",        "verify": False, "reversible": False, "approval": True},
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
                          expires_hours: int = 24) -> dict | None:
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
            cur = conn.execute(
                """INSERT INTO write_requests
                   (owner_id, task_id, step_id, action_key, tool_ref, risk_class,
                    target_ref, target_scope, preview_payload, preview_text,
                    reason, approval_status, approval_required,
                    created_at, expires_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,'pending',?,?,?)""",
                (owner_id, task_id, step_id, action_key, tool_ref,
                 policy["class"], target_ref, target_scope,
                 json.dumps(params), preview_text[:1000],
                 reason[:300], 1 if policy.get("approval") else 0,
                 now, expires_at)
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
    def _execute_fn(p):
        if tool_ref == "calendar":
            return _execute_calendar_write(action_key, p)
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

    # Task aus waiting_user_decision loesen
    if req.get("task_id") and result["ok"]:
        try:
            import orbit as _orbit
            _orbit.update_task(req["task_id"], status="active")
            logger.info(f"approve_write_request: Task {req['task_id'][:8]} -> active")
        except Exception as e:
            logger.debug(f"approve_write_request: Task-Status-Update fehlgeschlagen: {e}")

    return result


def reject_write_request(req_id: int, reason: str = "", rejected_by: str = "user") -> bool:
    """Lehnt einen Write-Request ab."""
    try:
        from core.database import get_connection
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
            return True
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"reject_write_request fehlgeschlagen: {e}")
        return False


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
    # Kalender-Verify ist komplex (externe API) -- vorerst soft
    return True, "Kalender-Verify: best-effort (extern)"


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
        return {"ok": False, "error": f"{action_key} ist gesperrt (Klasse C)",
                "audit_id": audit_id, "gate": GATE_BLOCKED, "verified": False}

    # Gate: needs_approval -> Write-Request anlegen statt sofort ausfuehren (5.2)
    if gate == "needs_approval":
        preview_text = ""
        if tool_ref == "calendar":
            preview_text = build_calendar_preview(action, params)
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
            return {"ok": False, "pending": True,
                    "error": None,
                    "write_request_id": req["id"],
                    "preview": preview_text,
                    "gate": GATE_NEEDS_APPROVAL,
                    "verified": False,
                    "message": f"Write-Request #{req['id']} angelegt -- wartet auf Freigabe"}
        else:
            return {"ok": False, "error": "Write-Request konnte nicht angelegt werden",
                    "gate": GATE_NEEDS_APPROVAL, "verified": False}

    # Preflight (bei soft gate erzwungen)
    preflight_ok = True
    preflight_msg = "skipped"
    if gate in ("soft", "hard"):
        if tool_ref == "workspace":
            preflight_ok, preflight_msg = preflight_workspace(action, params)
        elif tool_ref == "todos":
            preflight_ok, preflight_msg = preflight_todo(action, params, owner_id)
        else:
            preflight_msg = f"kein Preflight fuer {tool_ref}"

        if not preflight_ok:
            audit_id = write_audit(
                owner_id=owner_id, action_type=action, tool_ref=tool_ref,
                risk_class=risk_class, gate_result="preflight_failed",
                preflight_result=preflight_msg, error=preflight_msg,
                success=False, task_id=task_id, step_id=step_id
            )
            return {"ok": False, "error": f"Preflight fehlgeschlagen: {preflight_msg}",
                    "audit_id": audit_id, "gate": gate, "verified": False}

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
        return {"ok": False, "error": err, "audit_id": audit_id,
                "gate": gate, "verified": False}

    if not write_result.get("success", False):
        err = write_result.get("error", "Write fehlgeschlagen")
        audit_id = write_audit(
            owner_id=owner_id, action_type=action, tool_ref=tool_ref,
            risk_class=risk_class, gate_result=gate,
            preflight_result=preflight_msg,
            write_result=str(write_result.get("result",""))[:200],
            error=err, success=False, task_id=task_id, step_id=step_id
        )
        return {"ok": False, "error": err, "audit_id": audit_id,
                "gate": gate, "verified": False}

    # Post-Write-Verifikation
    verify_ok = True
    verify_msg = "skipped"
    if policy.get("verify"):
        if tool_ref == "workspace":
            verify_ok, verify_msg = verify_workspace(action, params)
        elif tool_ref == "todos":
            verify_ok, verify_msg = verify_todo(action, params, write_result)
        elif tool_ref == "calendar":
            verify_ok, verify_msg = verify_calendar(action, params)

        if not verify_ok:
            audit_id = write_audit(
                owner_id=owner_id, action_type=action, tool_ref=tool_ref,
                risk_class=risk_class, gate_result=gate,
                preflight_result=preflight_msg,
                write_result=str(write_result.get("result",""))[:200],
                verify_result=verify_msg, error=f"Verifikation fehlgeschlagen: {verify_msg}",
                success=False, task_id=task_id, step_id=step_id
            )
            return {"ok": False, "error": f"Write ausgefuehrt aber Verifikation fehlgeschlagen: {verify_msg}",
                    "audit_id": audit_id, "gate": gate, "verified": False}

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

    return {
        "ok": True,
        "result": write_result.get("result"),
        "error": None,
        "audit_id": audit_id,
        "gate": gate,
        "verified": verify_ok,
        "verify_msg": verify_msg,
    }
