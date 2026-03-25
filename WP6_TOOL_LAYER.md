# WP6 — Tool Layer

Stand: 2026-03-25
Nach: WP0–WP5 + Post-WP5-Cleanup

---

## Ziel

Klare, schlanke Tool-Schicht für Kimi Core.
Kimi nutzt Werkzeuge — die Werkzeuge nutzen nicht Kimi.

---

## Leitprinzip

**Kimi Core entscheidet. Tools führen nur aus.**

Tools sind Fähigkeiten, keine Akteure:
- sprechen nicht direkt mit dem Nutzer
- priorisieren nichts
- starten nichts selbst
- schreiben nicht selbständig
- laufen nur auf Anforderung von Kimi Core

---

## Access-Modi

Drei Zugriffsmodi für alle Tool-Operationen:

| Modus | Bedeutung | Wo verarbeitet |
|---|---|---|
| **READ** | Informationen abrufen, kein Schreiben | `core/tools.py` → zweiter Kimi-Call |
| **PROPOSE** | Kimi schlägt vor, Nutzer bestätigt | via Kimi-Antworttext (kein eigener Handler) |
| **WRITE** | Verändernde Aktion — kontrolliert, eng | `core/kimi_output.py` |

**WRITE ist nur erlaubt wenn:**
- A. explizite Nutzeranweisung
- B. Kimi schlägt vor → Nutzer bestätigt
- C. zukünftiger bewusst erweiterter Modus

---

## Tool-Verzeichnis (TOOL_REGISTRY)

```python
# Websearch
"web.search"        → READ

# Introspect
"introspect"        → READ

# Kalender
"calendar.list"     → READ    ← handle_calendar_read (core/tools.py)
"calendar.create"   → WRITE   ← kimi_output._run_calendar
"calendar.update"   → WRITE   ← kimi_output._run_calendar
"calendar.delete"   → WRITE   ← kimi_output._run_calendar

# Todos
"todo.list"         → READ    ← handle_todo_read (core/tools.py)
"todo.create"       → WRITE   ← kimi_output._run_todo
"todo.complete"     → WRITE   ← kimi_output._run_todo
"todo.delete"       → WRITE   ← kimi_output._run_todo
"todo.update"       → WRITE   ← kimi_output._run_todo
```

---

## Verarbeitungspipeline in kimi_core.py

```
Schritt 1:  Erster Kimi-Call (Memory aktiv, AWC als extra_system)
Schritt 2:  handle_web_search       → READ → zweiter Call mit search_ctx
Schritt 3:  handle_introspect       → READ → zweiter Call mit introspect_ctx
Schritt 4:  handle_calendar_read    → READ → zweiter Call mit cal_ctx    [WP6]
Schritt 4b: handle_todo_read        → READ → zweiter Call mit todo_ctx   [WP6]
Schritt 5:  process_kimi_output     → WRITE-Aktionen (todos, calendar, proposals)
Schritt 6:  Workspace-Routing
Schritt 7:  AWC aktualisieren
```

READ-Tools kommen VOR `kimi_output.py` — Kimi sieht das Ergebnis und kann darauf
antworten. WRITE-Tools werden NACH dem letzten Kimi-Call ausgeführt.

---

## Tool-Marker-Format

Kimi signalisiert Tool-Nutzung durch Marker in ihrer Antwort:

```
[SEARCH: query]
[INTROSPECT]
[CALENDAR_ACTION: {"action": "list", "range": "today"}]
[CALENDAR_ACTION: {"action": "create", "calendar": "work", "title": "...", "start": "...", "end": "..."}]
[TODO_ACTION: {"action": "list", "project": "kimi"}]
[TODO_ACTION: {"action": "create", "title": "...", "priority": "hoch"}]
```

READ-Marker werden von `core/tools.py` verarbeitet — der Block wird aus dem Reply
entfernt, das Ergebnis als doc_context in einen zweiten Call gegeben.

WRITE-Marker verbleiben im Reply bis `kimi_output.py` sie verarbeitet.

---

## Rollenmodell

| Schicht | Rolle | Führungsrecht |
|---|---|---|
| **Kimi Core** | Orchestrierung, Entscheidung | **Ja** |
| Tool Layer (core/tools.py) | READ-Ausführung | Nein |
| kimi_output.py | WRITE-Ausführung | Nein |
| calendar_router.py | Kalender-Dispatch | Nein |
| todo_service.py | Todo-CRUD | Nein |

---

## Geänderte Dateien

| Datei | Änderung |
|---|---|
| `core/tools.py` | Komplett neu: ACCESS_READ/PROPOSE/WRITE, TOOL_REGISTRY, handle_calendar_read, handle_todo_read |
| `kimi_core.py` | Schritt 4 + 4b eingefügt: CalendarRead + TodoRead in Pipeline |

Unverändert (bewusst):
- `core/kimi_output.py` — WRITE-Verarbeitung bleibt dort
- `core/calendar/calendar_router.py` — Dispatch-Logik bleibt dort
- `core/todo_service.py` / `core/todos.py` — CRUD bleibt dort
- `orbit.py` — nicht angefasst

---

## Akzeptanzcheck

✓ Tool Layer als eigene Schicht klar definiert (core/tools.py)
✓ Kimi Core kann Tools gezielt aufrufen
✓ READ / PROPOSE / WRITE sauber unterschieden
✓ Listen, Kalender und Websearch klar eingeordnet
✓ Tools haben keine Agentenrolle, keine Priorisierung, keine Eigenautonomie
✓ WP0–WP5 nicht unterlaufen

**WP6 ist grün: Kimi nutzt Werkzeuge — die Werkzeuge nutzen nicht Kimi.**

---

## WP6-Bindungen

- **WP0:** keine neue Hintergrundautonomie, keine neuen Trigger
- **WP1:** Kimi Core bleibt einzige führende Instanz
- **WP2:** Tools orientieren sich am Active Working Context, setzen ihn nicht
- **WP3:** Tool-Ergebnisse gehen nicht direkt als Führungslogik ins Memory
- **WP4:** Tools schreiben nicht heimlich in den Workspace
- **WP5:** ORBIT wurde nicht wieder aufgeladen

---

## Nicht im Scope (bewusst)

- Coding Agent
- neue Triggerlogik
- neue autonome Hintergrundarbeit
- große Policy-Engine
- Tool-Nebenprozesse
- Miro Kanban Sync (→ späteres WP)
- /code-Command (→ späteres WP)
