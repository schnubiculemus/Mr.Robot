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
  C -- hochriskant / extern (kalender, mail) -- in 5.1 GESPERRT

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

    # Klasse C -- GESPERRT in 5.1
    "calendar.write":   {"class": "C", "gate": "blocked", "verify": False, "reversible": False, "approval": True},
    "calendar.delete":  {"class": "C", "gate": "blocked", "verify": False, "reversible": False, "approval": True},
    "mail.send":        {"class": "C", "gate": "blocked", "verify": False, "reversible": False, "approval": True},
    "external.write":   {"class": "C", "gate": "blocked", "verify": False, "reversible": False, "approval": True},
}

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
            error=f"{action_key} ist in 5.1 gesperrt",
            success=False, task_id=task_id, step_id=step_id
        )
        return {"ok": False, "error": f"{action_key} ist gesperrt (Klasse C -- nicht erlaubt in 5.1)",
                "audit_id": audit_id, "gate": "blocked", "verified": False}

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
