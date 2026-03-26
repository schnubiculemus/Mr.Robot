# WP9 — Kognitive autonome Schleife

Stand: 2026-03-26

---

## Grundsatz

Es gibt nur eine Kimi.
Kimi darf autonom denken, aber nicht autonom handeln.
Kognition ja. Exekution nein.

---

## Architektur

```
schnubot-cognition.service (separater Service)
    ↓
cognition_service.py — Haupt-Tick
    ↓
┌─────────────────────────────────────────────┐
│  Queue-Check (alle 30s)                     │
│  cognition_requests → post_interaction      │
├─────────────────────────────────────────────┤
│  Takt-Fenster                               │
│  light:  alle 90 min                        │
│  medium: alle 7h                            │
│  deep:   max 1x/24h                         │
└─────────────────────────────────────────────┘
    ↓
_gather_inputs() — Inputquellen sammeln
    ↓
_build_cognition_prompt() — Prompt bauen
    ↓
_call_cognition_model() — Kimi K2.5 denkt
    ↓
[optional: _run_inner_dialogue() bei Spannungen]
    ↓
_store_cognition_outputs() → ChromaDB
_write_diary_entry() → diary/YYYY-MM-DD.md
```

---

## Inputquellen

| Quelle | Zweck | Level |
|---|---|---|
| Active Working Context | Realer aktueller Arbeitsanker | alle |
| Fast-Track | Kurzfristige Relevanz | alle |
| Typed Memory | Muster, Entscheidungen, Reflexionsspuren | medium/deep |
| Diary | Subjektiver Verlauf, Tonalität | alle |
| Cognition Echo | Bisherige cognition_notes | alle |
| Moltbook | Erkenntnismaterial | medium/deep |

---

## Denkformen (kind)

| Kind | Bedeutung |
|---|---|
| `observation` | Beobachtung über Zustand, Verhalten, Verlauf |
| `tension` | Widerspruch oder Spannungsfeld |
| `question` | Offene innere Frage |
| `insight` | Verdichtung / Erkenntnis |
| `self_correction` | Innerer Korrekturimpuls |
| `proposal_seed` | Rohmaterial für WP10-Proposals |

---

## Outputs

| Ziel | Inhalt | Chunk-Typ |
|---|---|---|
| ChromaDB | observation, tension, question, insight, self_correction | `cognition_note` |
| ChromaDB | proposal_seed | `proposal_seed` |
| diary/ | Diary-Eintrag bei medium/deep oder insights/tensions | Markdown |

---

## Post-Interaction Queue (cognition_requests)

Kimi Core schreibt nach bedeutsamen Turns in `cognition_requests`:
- request_type: `post_interaction`
- priority: `light`
- source_context: route, delegations, text_preview

Cognitive Service pollt alle 30s — sauber entkoppelt, kein direkter Service-Aufruf.

Bedeutsame Turns: coding_agent, calendar_read, todo_read, ROUTE_TOOL, ROUTE_WORKER, coding_mode

---

## Heartbeat-Trennung

| Service | Zuständigkeit |
|---|---|
| `heartbeat.py` (Cron) | Konsolidierung, Deduplizierung, Decay — Memory-Pflege |
| `schnubot-cognition.service` | Kognition, Reflexion, Denkformen, Proposal Seeds |

Gedächtnispflege und Kognition sind sauber getrennt.

---

## Sperrrregeln (absolut)

1. Keine Tool-Aufrufe
2. Keine Workspace-Writes
3. Keine Todo-/Task-Rechte
4. Keine ORBIT-Aktivierung
5. Kein User-Outreach
6. Keine automatische Proposal-Einreichung

---

## Innerer Dialog

Nur bei Spannung, nicht bei Routine.
Kein separater Sprecher — dieselbe Kimi prüft sich selbst.
Format: These → Gegenthese → Einordnung

Springt an bei: KIND_TENSION in medium/deep Läufen

---

## Beziehung zu WP10

WP9 erzeugt `proposal_seed` Chunks in ChromaDB.
WP10 kann daraus formale Proposals machen — nicht automatisch, sondern nach Prüfung.
Ein proposal_seed ist kein Task, kein Todo, keine automatische Weiterverarbeitung.

---

## Geänderte Dateien

| Datei | Änderung |
|---|---|
| `cognition_service.py` | Neu: Kognitiver Service |
| `core/database.py` | `cognition_requests` Tabelle in init_db() |
| `kimi_core.py` | Post-Interaction Request schreiben |
| `schnubot-cognition.service` | Neu: systemd Unit |
| `WP9_COGNITIVE_LOOP.md` | Neu: dieses Dokument |
