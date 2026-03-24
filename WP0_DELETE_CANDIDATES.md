# WP0 – Delete Candidates

Stand: 2026-03-24

## Legende
- `delete_candidate` — kann nach WP1-Stabilisierung physisch entfernt werden
- `temporary_compat` — bleibt vorerst wegen Abhängigkeiten, später löschen
- `keep` — bleibt dauerhaft

---

## orbit.py

| Funktion/Bereich | Label | Begründung |
|---|---|---|
| `_handle_idle_pulse()` | `delete_candidate` | WP0 abgeschaltet, V2 hat kein idle_pulse |
| `_handle_cognition_run()` | `delete_candidate` | autonome Kognition WP0 deaktiviert |
| `_handle_cognition_output()` | `delete_candidate` | Folge-Handler von cognition_run |
| `_auto_trigger_brief()` | `delete_candidate` | Auto-Artefakte WP0 deaktiviert |
| `_auto_trigger_result()` | `delete_candidate` | Auto-Artefakte WP0 deaktiviert |
| `_auto_trigger_report()` | `delete_candidate` | Auto-Artefakte WP0 deaktiviert |
| `_auto_trigger_analysis()` | `delete_candidate` | Auto-Artefakte WP0 deaktiviert |
| `_auto_trigger_plan()` | `delete_candidate` | Auto-Artefakte WP0 deaktiviert |
| `_auto_trigger_worklog_stagnation()` | `temporary_compat` | nur bei explizitem Vollzug sinnvoll, prüfen |
| `_should_trigger_analysis()` | `delete_candidate` | Zwischenstand-Analyse-Trigger, WP0 deaktiviert |
| `TRIGGER_HANDLERS["idle_pulse"]` | `delete_candidate` | nach Handler-Löschung entfernen |
| `TRIGGER_HANDLERS["cognition_run"]` | `delete_candidate` | nach Handler-Löschung entfernen |
| `TRIGGER_HANDLERS["cognition_output"]` | `delete_candidate` | nach Handler-Löschung entfernen |
| Planner-Aufrufe in idle_pulse | `delete_candidate` | Planner nur noch explizit |
| `_is_recovery_transition()` | `keep` | bleibt als Guard |

## orbit_cognition.py

| Funktion/Bereich | Label | Begründung |
|---|---|---|
| `run_kognition()` gesamter Body | `delete_candidate` | WP0 deaktiviert, V2 hat eigenen Kern |
| InnerDialogue-Integration | `delete_candidate` | Teil der alten Kognition |
| AutonomousReflection-Integration | `delete_candidate` | Teil der alten Kognition |
| MoltbookExplorer-Integration | `temporary_compat` | Moltbook bleibt, Explorer prüfen |
| Diary-Integration in Kognition | `temporary_compat` | Diary bleibt, aber nicht als Auto-Kognition |

## core/planner.py

| Funktion/Bereich | Label | Begründung |
|---|---|---|
| `maybe_start_task()` Bootstrap-Auto-Steps | `temporary_compat` | WP0 deaktiviert durch ENABLE_AUTO_ARTIFACTS |
| `run_planner()` Auto-Start-Logik | `temporary_compat` | nur noch explizit aufrufen |

## Trigger-Typen in orbit_triggers (DB)

| Trigger-Typ | Label | Begründung |
|---|---|---|
| `idle_pulse` | `delete_candidate` | WP0 deaktiviert |
| `cognition_run` | `delete_candidate` | WP0 deaktiviert |
| `cognition_output` | `delete_candidate` | WP0 deaktiviert |
| `tool_result` | `keep` | bleibt für Task-Execution |
| `recovery_result` | `temporary_compat` | technisch noch nötig |

---

## WP0 Akzeptanzcheck

| Kriterium | Status |
|---|---|
| Idle Pulse deaktiviert | ✓ `ENABLE_IDLE_PULSE = False` |
| Autonome Kognition deaktiviert | ✓ `ENABLE_AUTONOMOUS_COGNITION = False` |
| Auto-Dokumente deaktiviert | ✓ `ENABLE_AUTO_ARTIFACTS = False` |
| Recovery kein sichtbarer Workspace | ✓ `_is_recovery_transition()` Guard |
| Max 1 heißer Task | ✓ `MAX_HOT_TASKS = 1` |
| ORBIT keine autonome Exekutivrolle | ✓ kein idle_pulse/cognition mehr |
| Safe Mode verankert | ✓ `SAFE_MODE = True` + Feature-Gates |
| Delete-Candidate-Liste | ✓ diese Datei |
