# Post-WP5 Cleanup

Stand: 2026-03-25

## Klassifikationsliste

### orbit.py

| Bereich | Klassifikation |
|---|---|
| `execute_tool()` / `_dispatch_tool()` | **keep** |
| `tool_result` / `recovery_result` / `manual_override` / `wiedervorlage` Handler | **keep** |
| `run_recovery()` | **temporary_compat** (technische Reparatur) |
| `_run_maintenance()` | **keep** |
| `tick()` | **keep** (Infrastrukturpuls) |
| `_handle_user_input()` | **temporary_compat** → Safe Mode no-op |
| `_handle_heartbeat()` | **temporary_compat** → Safe Mode no-op |
| `_handle_time_window()` | **temporary_compat** (Wiedervorlagen bleiben) |
| `_handle_cognition_output()` | **temporary_compat** → Safe Mode no-op |
| `_handle_cognition_run()` | **delete_candidate** (WP0 deaktiviert) |
| `_handle_idle_pulse()` | **delete_candidate** (WP0 deaktiviert) |
| `create_thread()` / `update_thread()` | **temporary_compat** → **delete_candidate** |
| `_maybe_autonomous_task()` | **delete_candidate** (stillgelegt) |
| `_auto_trigger_*()` alle | **delete_candidate** (WP0 deaktiviert) |
| `activate_policy()` / `bootstrap_policies()` | **temporary_compat** → **delete_candidate** |
| `activate_routine()` / `execute_routine()` | **temporary_compat** → **delete_candidate** |
| `check_proactive()` | **temporary_compat** (Safe Mode blockt) |

### core/planner.py

| Bereich | Klassifikation |
|---|---|
| `maybe_start_task()` | **temporary_compat** → **delete_candidate** |
| `run_planner()` | **temporary_compat** |
| `score_candidates()` | **temporary_compat** |
| `choose_worklines()` | **temporary_compat** |
| `should_replan()` | **temporary_compat** |

### core/workspace_artifact_service.py

| Bereich | Klassifikation |
|---|---|
| Lesen (`get_artifact`, `read_artifact_content`, `list_line_artifacts`) | **temporary_compat** |
| Schreiben (`create_artifact`, `update_artifact_content`) | **delete_candidate** (Safe Mode blockiert) |
| `materialize_execution_artifact()` | **delete_candidate** (Safe Mode blockiert) |
| `append_worklog_entry()` | **delete_candidate** (Safe Mode blockiert) |

---

## /task Entscheidung

**Funktionsfähig** — `/task` routet jetzt direkt über `_process_chat` → Kimi Core.
Kein ORBIT-Umweg mehr. Kein stiller Defekt.

---

## Bewusst stehen geblieben

- `run_recovery()` bleibt temporär für technische Reparatur
- `_handle_time_window()` bleibt für Wiedervorlagen (echter Nutzwert)
- Thread-DB-Tabelle bleibt für Altbestand (kein Schreiben in V2-Nutzpfad)
- Legacy-Dashboard-Routen bleiben als `temporary_compat` (V2-Routen sind Hauptpfad)
