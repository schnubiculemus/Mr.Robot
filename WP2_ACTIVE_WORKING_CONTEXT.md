# WP2 – Active Working Context

Stand: 2026-03-24

## Ziel

Verbindlicher Primäranker für laufende Arbeit.
Kimi Core liest AWC **vor** Memory-Zugriff.

---

## Datenmodell

Tabelle: `active_working_context` (genau ein Eintrag, id=1 CHECK-Constraint)

| Feld | Typ | Bedeutung |
|---|---|---|
| `active_line` | TEXT | Aktuelles Thema / Vorhaben / Linie |
| `active_goal` | TEXT | Ziel innerhalb der Linie |
| `active_document` | TEXT | Führendes Workspace-Dokument |
| `last_clean_state` | TEXT | Letzter verlässlicher Stand |
| `last_decision` | TEXT | Letzte gültige Entscheidung |
| `next_open_question` | TEXT | Nächste offene Stelle |
| `proposed_switch_to` | TEXT | Vorgeschlagener neuer Kontext (noch nicht bestätigt) |
| `proposed_switch_reason` | TEXT | Grund für Vorschlag |
| `proposed_switch_confirmed` | INT | 0=vorgeschlagen, 1=bestätigt |
| `updated_at` | TEXT | Letztes Update |

## Regel: genau ein aktiver Kontext

- `id=1` mit `CHECK (id = 1)` — technisch nur ein Eintrag möglich
- kein Mehrfach-Fokus

## Kontextwechsel-Regel

- Kimi **darf vorschlagen** (`propose_context_switch()`)
- Kimi **vollzieht nicht selbst**
- Nutzer oder Core bestätigen (`confirm_context_switch()`)
- `proposed_switch_confirmed=0` = noch offen

---

## API

| Funktion | Beschreibung |
|---|---|
| `get_active_context(owner_id)` | Kontext lesen |
| `set_active_context(owner_id, **fields)` | Kontext vollständig setzen |
| `update_active_context(owner_id, **fields)` | Einzelfelder aktualisieren |
| `clear_active_context(owner_id)` | Kontext löschen |
| `propose_context_switch(owner_id, new_line, reason)` | Wechsel vorschlagen |
| `confirm_context_switch(owner_id)` | Wechsel bestätigen |
| `format_for_prompt(ctx)` | Für System-Prompt formatieren |

---

## Kimi-Core-Integration

In `kimi_core.process()`:
1. `get_active_context()` lesen
2. `format_for_prompt()` → `doc_context` für `ollama_chat()`
3. AWC ist **vor** Memory aktiv

---

## Akzeptanzcheck WP2

| Kriterium | Status |
|---|---|
| Expliziter AWC | ✓ `active_working_context.py` |
| 6 Pflichtfelder | ✓ alle drin |
| Genau ein aktiver Kontext | ✓ `id=1 CHECK` |
| Kimi Core liest AWC | ✓ vor Memory in `process()` |
| Kein stiller Kontextwechsel | ✓ `propose_context_switch()` + Bestätigung |
| AWC als Primäranker vorbereitet | ✓ |
