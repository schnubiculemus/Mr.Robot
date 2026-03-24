# WP4 – Workspace kooperativ und schlank

Stand: 2026-03-24

## Leitbild

Der Workspace ist eine gemeinsame Werkbank — keine Artefaktfabrik.

---

## Dokumentmodell V2

| Typ | Beschreibung | Status |
|---|---|---|
| `note` | Notiz — freier Text, Arbeitsmemo | **V2-Primärtyp** |
| `code_file` | Code-Datei — jede Programmiersprache | **V2-Primärtyp** |
| `brief`, `analysis`, `plan` etc. | Alte Artefakttypen | `legacy_compat` → `delete_candidate` |

---

## Führendes Dokument

- Jeder aktive Kontext hat **ein führendes Dokument** (`active_document` im AWC)
- Optional: Hilfsdokumente (nachgeordnet, nicht konkurrenzierend)
- `workspace_service.set_leading_document()` setzt es im AWC

---

## Schreibregel

Schreiben erlaubt **nur**:
- `WRITE_REASON_EXPLICIT` — Nutzer hat es klar angefordert
- `WRITE_REASON_IMPLICIT` — Kimi Core erkennt klare Schreibabsicht

Schreiben **verboten** aus:
- Triggern / Recovery / Auto-Artefaktketten
- diffusem Memory-Impuls
- Statuswechseln

---

## Neue Dateien

- `core/workspace_service.py` — kooperativer Schreibservice V2
  - `read_document()`, `write_document()`, `append_to_document()`
  - `set_leading_document()`, `get_leading_document()`, `read_leading_document()`

---

## Altpfad-Liste

| Pfad | Label |
|---|---|
| `materialize_execution_artifact()` | `temporary_compat` → `delete_candidate` |
| `append_worklog_entry()` | `temporary_compat` → `delete_candidate` |
| Alte ARTIFACT_TYPES (brief/analysis/plan etc.) | `legacy_compat` → `delete_candidate` |
| `_auto_trigger_*` in orbit.py | `delete_candidate` (WP0 deaktiviert) |

---

## Akzeptanzcheck WP4

| Kriterium | Status |
|---|---|
| V2-Typen: note + code_file | ✓ `WORKSPACE_DOC_TYPES_V2` |
| Führendes Dokument nutzbar | ✓ via AWC `active_document` |
| Schreiben nur kooperativ | ✓ `write_reason` Pflichtparameter |
| Kein autonomes Schreiben | ✓ kein Auto-Trigger-Pfad in workspace_service |
| Kein Recovery-Output | ✓ workspace_service kennt keine Recovery-Pfade |
| WP0–WP3 nicht untergraben | ✓ SAFE_MODE respektiert, kein neuer Auto-Pfad |
| Altpfade markiert | ✓ temporary_compat / delete_candidate |
