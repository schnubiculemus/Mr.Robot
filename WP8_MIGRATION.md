# WP8 — Migration / Übergangsabschluss

Stand: 2026-03-25
Nach: WP0–WP7

---

## Leitprinzip

V2 ist jetzt das System. Legacy ist nur noch Rest.

---

## Was in WP8 physisch entfernt wurde

### orbit.py — Delete-Candidates entfernt (~890 Zeilen)

| Funktion | Begründung |
|---|---|
| `create_thread` / `get_thread` / `get_threads` / `update_thread` | Thread-System ohne Nutzwert in V2 |
| `thread_transition` / `assess_thread_relevance` / `convert_thread_to_task` / `discard_thread` / `merge_threads` | Thread-Logik-Sektion |
| `_auto_trigger_brief` / `_auto_trigger_result` / `_auto_trigger_report` | Auto-Artifact-Erzeugung, WP0 deaktiviert |
| `_auto_trigger_plan` / `_auto_trigger_analysis` / `_auto_trigger_worklog_stagnation` | Auto-Artifact-Erzeugung, WP0 deaktiviert |
| `_should_trigger_analysis` | Abhängigkeit der gelöschten _auto_trigger_analysis |
| `_auto_create_steps` | Step-Bootstrapping aus Altarchitektur |
| `_handle_user_input` | Safe Mode no-op, nutzt gelöschtes Thread-System |
| `_handle_heartbeat` | Safe Mode no-op, Thread-Stale-Markierung |
| `_maybe_autonomous_task` | Autonome Task-Erzeugung aus Thread — V2-fremd |
| `_handle_cognition_run` / `_handle_idle_pulse` | WP0 deaktiviert, keine V2-Rolle |
| `_maybe_run_planner` | temporary_compat → entfernt |
| `_is_recovery_transition` / `TRIGGER_MATRIX` | Abhängigkeit gelöschter Funktionen |
| `ENABLE_IDLE_PULSE` / `ENABLE_AUTONOMOUS_COGNITION` / `ENABLE_AUTO_ARTIFACTS` | Schalter ohne Funktion |
| idle_pulse in `tick()` | Trigger-Erzeugung für nicht mehr existierenden Handler |

### TRIGGER_HANDLERS (orbit.py process())
Entfernt: `user_input`, `heartbeat`, `cognition_run`, `idle_pulse`
Behalten: `tool_result`, `recovery_result`, `manual_override`, `wiedervorlage`, `time_window`, `mirror_signal`, `review_result`, `cognition_output`

---

## Legacy-/Compat-Übersicht nach WP8

### orbit.py
| Teil | Status | Begründung |
|---|---|---|
| Task-CRUD (`create_task`, `get_task`, `update_task`) | **keep** | Wird von kimi_output.py für Todo-ORBIT gebraucht |
| Step-CRUD + `execute_step` | **keep** | Aktiver Worker-Pfad |
| `tick()` / `run_scheduler()` | **keep** | Technischer Kern |
| `recovery` / `run_recovery()` | **keep** | Technische Reparaturfunktion |
| `check_proactive()` | **temporary_compat** | Safe Mode gibt early return |
| `_handle_cognition_output` | **temporary_compat** | WP5 Safe Mode gated |
| `_handle_mirror_signal` / `_handle_review_result` | **temporary_compat** | Selten aktiv |
| `_handle_time_window` | **keep** | Wiedervorlagen — echter Nutzwert |
| Policy/Routine-System | **temporary_compat** | Nicht aktiv, aber noch referenziert |
| `bootstrap_policies()` | **temporary_compat** | Startup-Logik, kein V2-Pfad |

### core/workspace_artifact_service.py
| Status | **legacy_compat** |
|---|---|
| Nutzung | orbit.py, gate_service.py, dashboard.py |
| V2-Ersatz | `core/workspace_service.py` |
| Delete-Kandidaten | `materialize_execution_artifact()`, `append_worklog_entry()`, alle Legacy-ARTIFACT_TYPES |
| Wann löschen | Wenn orbit.py + gate_service.py + dashboard.py auf V2-Workspace umgestellt sind |

### core/planner.py
| Status | **temporary_compat** |
|---|---|
| Nutzung | orbit.py (indirekt), dashboard.py, orbit_cognition.py |
| V2-Ersatz | Kimi Core + AWC |
| Delete-Kandidaten | `score_candidates()`, `choose_worklines()`, `should_replan()`, `maybe_start_task()` |
| Wann löschen | Wenn orbit_cognition.py und dashboard.py planner-unabhängig sind |

---

## V2 — Hauptsystem nach WP8

```
Nutzereingang (WhatsApp /code /task oder normal)
    ↓
app.py webhook
    ↓
_process_chat()
    ↓
kimi_core.process()              ← FÜHRT (einzige Führungsinstanz)
    ↓
AWC lesen (extra_system)         ← Primäranker laufender Arbeit
    ↓
ollama_chat() → Memory aktiv     ← AWC → Fast-Track → Typed Memory → Diary
    ↓
READ-Tools (websearch/introspect/calendar.list/todo.list)  ← core/tools.py
    ↓
Coding Agent (minimax-m2.7)      ← coding_agent.py (Worker, nur bei /code oder [CODE_AGENT:])
    ↓
Output-Interpretation (todos/calendar write, proposals)    ← core/kimi_output.py
    ↓
Workspace-Routing                ← core/workspace_service.py (V2)
    ↓
AWC aktualisieren
    ↓
Antwort an Tommy
```

### Schichtenregeln V2 (nach WP8)
| Schicht | Rolle | Führungsrecht |
|---|---|---|
| **Kimi Core** | Orchestrierung | **Ja** |
| AWC | Primäranker laufender Arbeit | Nein |
| Memory (ChromaDB) | Retrieval | Nein |
| Tool Layer (core/tools.py) | READ-Operationen | Nein |
| Coding Agent | Worker für Code | Nein |
| kimi_output.py | WRITE-Operationen | Nein |
| Workspace V2 | Dokumente/Code | Nein |
| ORBIT | Executor (wenn delegiert) | **Nein** |

---

## Was WP9 vorfindet

WP9 kann auf dieser Basis aufsetzen:
- Kein alter Planner mehr als aktiver Hauptpfad
- Kein Thread-System mehr
- Keine idle_pulse-Infrastruktur mehr
- ORBIT ist klar Executor, nicht Orchestrator
- Legacy-Dateien sind klar markiert

WP9-Themen (noch nicht abgestimmt):
- Kimis durchgehendes Bewusstsein (idle_pulse V2 — bewusstes Neudesign)
- soul.md Rewrite
- Kalender/Miro Tool-Ausbau
- ChromaDB-Lock-Problem lösen

---

## Dateien geändert in WP8

| Datei | Änderung |
|---|---|
| `orbit.py` | ~890 Zeilen delete_candidates physisch entfernt |
| `core/workspace_artifact_service.py` | Header als legacy_compat markiert |
| `core/planner.py` | Header als temporary_compat markiert |
| `WP8_MIGRATION.md` | Neu: dieses Dokument |
