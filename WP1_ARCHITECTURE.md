# WP1 – Kimi Core Architecture

Stand: 2026-03-24

## Kernentscheidung

**Kimi Core führt. Alles andere unterstützt.**

---

## Verantwortlichkeiten

| Schicht | Rolle | Führungsrecht |
|---|---|---|
| **Kimi Core** (`kimi_core.py`) | Orchestrierung, Routing, Antwort | **Ja — führend** |
| Memory (ChromaDB) | Retrieval, Verdichtung | Nein |
| Workspace | Dokumente halten | Nein |
| Tools (websearch, introspect) | begrenzte Funktionalität | Nein |
| Worker (Coding Agent) | spezialisierte Ausführung | Nein — liefert an Core zurück |
| ORBIT | technische Kompatibilitätsschicht | **Nein** (WP0/WP1 entmachtet) |

---

## Hauptpipeline (WP1)

```
Nutzereingang (WhatsApp)
    ↓
webhook() in app.py
    ↓
_process_chat()
    ↓
KimiCoreRequest erstellen
    ↓
kimi_core.process()          ← Kimi Core führt ab hier
    ↓
ollama_chat() → Memory aktiv
    ↓
Tool-Delegation? (websearch / introspect)
    ↓
Output-Interpretation (proposals, todos)
    ↓
KimiCoreResult
    ↓
save_message() + send_message()
```

---

## Routing-Modi

| Route | Bedeutung |
|---|---|
| `direct` | Kimi antwortet direkt |
| `memory` | Memory-Kontext aktiv (Standard) |
| `tool` | Tool-Delegation (websearch, introspect) |
| `workspace` | Workspace-Kontext nötig (später) |
| `worker` | Worker-Delegation (Coding Agent, später) |
| `orbit_compat` | temporary_compat: ORBIT-Pfad |

---

## Delete Candidates (Führungslogik)

| Pfad | Label | Begründung |
|---|---|---|
| Direkter `ollama_chat()` in `_process_chat` | `delete_candidate` | ersetzt durch `kimi_core.process()` |
| `_handle_web_search()` direkt in `_process_chat` | `temporary_compat` | jetzt über Core delegiert |
| `_handle_introspect()` direkt in `_process_chat` | `temporary_compat` | jetzt über Core delegiert |
| ORBIT als primäre Arbeitslogik | `delete_candidate` | WP0 entmachtet |
| `_orbit_trigger` in webhook | `temporary_compat` | /task Befehl, später über Core |

---

## Akzeptanzcheck WP1

| Kriterium | Status |
|---|---|
| Kimi Core als explizite Schicht | ✓ `kimi_core.py` |
| Nutzereingaben über Kimi Core | ✓ `_process_chat` → `kimi_core.process()` |
| Core entscheidet über Delegation | ✓ Routing-Logik in Core |
| Keine konkurrierende Führungsinstanz | ✓ ORBIT WP0 entmachtet |
| ORBIT nur Kompatibilitätsschicht | ✓ |
| Verantwortlichkeiten dokumentiert | ✓ diese Datei |
| Alte Führungslogik markiert | ✓ temporary_compat / delete_candidate |
