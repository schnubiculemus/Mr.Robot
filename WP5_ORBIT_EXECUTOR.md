# WP5 – ORBIT als Executor

Stand: 2026-03-25

## Neue Rolle

**ORBIT ist technischer Executor, nicht Führungsinstanz.**

Kimi Core führt. ORBIT dient.

---

## Was ORBIT noch darf

| Funktion | Status |
|---|---|
| `execute_tool()` / `_dispatch_tool()` | **behalten** — explizite Ausführung |
| `create_task()` / `create_step()` / `update_step()` | **behalten** — Task-Runner |
| `run_recovery()` | **temporary_compat** — technische Reparatur |
| `_run_maintenance()` | **behalten** — Datenbankpflege |
| `tick()` | **behalten** — Infrastrukturpuls (nur Restläufe) |
| `quality_gate()` / `pre_execution_check()` | **behalten** — Ausführungssicherheit |
| `get_artifact()` / `read_artifact_content()` | **temporary_compat** — Lesen Legacy |

## Was ORBIT nicht mehr darf (WP5 entmachtet)

| Funktion | Status | Grund |
|---|---|---|
| `_auto_trigger_brief/result/report/analysis/plan()` | `delete_candidate` | autonome Dokumentproduktion (WP0 deaktiviert) |
| `_auto_trigger_worklog_stagnation()` | `delete_candidate` | Legacy-Worklog (WP4 blockiert) |
| `_auto_create_steps()` | `delete_candidate` | autonome Step-Erzeugung |
| `_should_trigger_analysis()` | `delete_candidate` | Analyse-Trigger-Entscheid |
| `_handle_idle_pulse()` | `delete_candidate` | autonomes Hintergrunddenken (WP0) |
| `_handle_cognition_run()` | `delete_candidate` | autonome Kognition (WP0) |
| `create_thread()` / `update_thread()` | `temporary_compat` → `delete_candidate` | Linien-Container der alten Autonomie |
| `activate_policy()` / `bootstrap_policies()` | `temporary_compat` → `delete_candidate` | ORBIT-Führungslogik |
| `activate_routine()` / `execute_routine()` | `temporary_compat` → `delete_candidate` | ORBIT-Führungslogik |
| `check_proactive()` | `temporary_compat` (Safe Mode blockiert) | proaktive Altlogik |

---

## Task-Definition V2

Ein Task ist eine kleine, explizite Ausführungseinheit für einen konkreten Arbeitsschritt.

- kein Linien-Container
- keine Planungsmaschine
- wird von Kimi Core delegiert, nicht von ORBIT selbst gestartet

---

## Akzeptanzcheck WP5

| Kriterium | Status |
|---|---|
| Modulkopf: "Executor, nicht Führungsinstanz" | ✓ |
| Auto-Trigger: delete_candidate | ✓ |
| Threads: temporary_compat | ✓ |
| Policies/Routines: temporary_compat | ✓ |
| tick(): nur Infrastruktur | ✓ |
| WP0–WP4 nicht unterlaufen | ✓ |
| Kimi Core weiterhin führend | ✓ |
