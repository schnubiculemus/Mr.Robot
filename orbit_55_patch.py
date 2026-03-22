"""
orbit_55_patch.py -- Patch orbit.py fuer 5.5 Proposal-Write-Familie
Ausfuehren auf dem Server: venv/bin/python3 orbit_55_patch.py
"""
import re

ORBIT_PATH = "/opt/whatsapp-bot/orbit.py"

with open(ORBIT_PATH, "r") as f:
    content = f.read()

# 1. _WRITE_TOOLS erweitern
old1 = '_WRITE_TOOLS = {"workspace", "todos_write", "calendar_write"}'
new1 = '_WRITE_TOOLS = {"workspace", "todos_write", "calendar_write", "proposal_write"}'
if old1 in content:
    content = content.replace(old1, new1)
    print("_WRITE_TOOLS: Done")
else:
    print("_WRITE_TOOLS: NOT FOUND")

# 2. Tool-Registry ergaenzen
old2 = '    "calendar_write":     {"criticality": "kritisch",        "usage": ["write"],          "type": "extern",        "write_indirect": False},'
new2 = '''    "calendar_write":     {"criticality": "kritisch",        "usage": ["write"],          "type": "extern",        "write_indirect": False},
    "proposal_write":     {"criticality": "kritisch",        "usage": ["write"],          "type": "intern",        "write_indirect": False},'''
if old2 in content:
    content = content.replace(old2, new2)
    print("Tool registry: Done")
else:
    print("Tool registry: NOT FOUND")

# 3. Dispatch: proposal_write durch Gate routen
# Finde den Todos-Dispatch-Block und fuege Proposal danach ein
proposal_dispatch = '''
    # Proposals -- 5.5: proposal_write via Gate
    if tool_ref == "proposal_write":
        action_type = params.get("action", action)
        if action_type not in ("approve", "reject", "defer"):
            return {"success": False, "error": f"Unbekannte proposal_write action: {action_type}"}

        from core.gate_service import execute_write, build_proposal_preview
        action_key = f"proposal.{action_type}"
        write_params = dict(params)
        write_params["owner_id"] = _owner_id

        def _do_proposal(p):
            from core.gate_service import execute_proposal_action
            return execute_proposal_action(action_type, p)

        gresult = execute_write(action_key, write_params, _owner_id, _do_proposal,
                                task_id=task_id, step_id=step_id)

        if gresult.get("pending"):
            if task_id:
                update_task(task_id, status="waiting_user_decision")
            return {"success": False, "pending": True,
                    "result": gresult.get("message", "Proposal-Write-Request angelegt"),
                    "write_request_id": gresult.get("write_request_id"),
                    "preview": gresult.get("preview")}

        return {"success": gresult["ok"],
                "result": gresult.get("result") or gresult.get("error"),
                "error": gresult.get("error"),
                "audit_id": gresult.get("audit_id")}

'''

# Einfuegen nach dem Todos-Dispatch
marker = "    # Web Search\n    if tool_ref == \"websearch\":"
if marker in content:
    content = content.replace(marker, proposal_dispatch + "    # Web Search\n    if tool_ref == \"websearch\":")
    print("Proposal dispatch: Done")
else:
    print("Proposal dispatch: NOT FOUND marker")

# 4. _e_append_next_step: proposal_write erkennen
old4 = '''        elif any(w in hint_lower for w in ["blockiert", "block", "feststeckt", "gesperrt"]):'''
new4 = '''        elif any(w in hint_lower for w in ["proposal", "vorschlag", "genehmigen", "ablehnen"]):
            # 5.5: Proposal-Statusaenderung via Gate
            import json as _j55
            tool_ref = "proposal_write"
            action = "approve"
            if any(w in hint_lower for w in ["ablehnen", "reject"]):
                action = "reject"
            elif any(w in hint_lower for w in ["verschieben", "defer", "spaeter"]):
                action = "defer"
            description = _j55.dumps({"action": action})
        elif any(w in hint_lower for w in ["blockiert", "block", "feststeckt", "gesperrt"]):'''
if old4 in content:
    content = content.replace(old4, new4)
    print("_e_append proposal: Done")
else:
    print("_e_append: NOT FOUND")

with open(ORBIT_PATH, "w") as f:
    f.write(content)

print("\\nPatch abgeschlossen.")
