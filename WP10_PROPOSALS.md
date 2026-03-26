# WP10 — Proposal-Layer

Stand: 2026-03-26

---

## Leitprinzip

Ein Proposal ist ein Vorschlag, kein Auftrag.
Kimi darf Proposals einreichen, aber nicht selbst daraus Arbeit machen.

---

## Proposal-Typen

| Typ | Bedeutung |
|---|---|
| `self_constitution_change` | Änderungen an soul.md / Kernprinzipien / Selbstverständnis |
| `behavior_adjustment` | Antwortstil, Verhaltenstendenzen, Kommunikationsmuster |
| `workflow_improvement` | Abläufe, Arbeitslogik, Bedienfluss |
| `architecture_improvement` | Systemstruktur, Modulschnittstellen, Zuständigkeiten |
| `memory_improvement` | Gedächtnislogik, Konsolidierung, Retrieval |
| `other` | Restkategorie |

---

## Status-Modell

| Status | Bedeutung |
|---|---|
| `open` | Eingereicht, wartet auf Entscheidung |
| `accepted` | Angenommen (Umsetzung liegt beim Menschen) |
| `rejected` | Abgelehnt |
| `withdrawn` | Von Kimi zurückgezogen |

---

## Speicherort

SQLite — `wp10_proposals` Tabelle.

Nicht ChromaDB (ChromaDB bleibt für `proposal_seed` aus WP9).
Proposals sind entscheidbare Objekte mit Status — das ist objektlogisch, nicht semantisch.

---

## Marker-Format (für Kimi)

```
[WP10_PROPOSAL: {
  "type": "self_constitution_change",
  "title": "...",
  "summary": "...",
  "reason": "...",
  "suggested_change": "...",
  "risk_note": "..."
}]
```

---

## Übergang WP9 → WP10

```
WP9 erzeugt proposal_seed (ChromaDB, intern, roh)
    ↓ expliziter Einreichungsakt
WP10 Proposal (SQLite, formal, sichtbar, entscheidbar)
```

Kein automatischer Übergang — `create_from_seed()` muss explizit aufgerufen werden.
Der Seed bleibt in ChromaDB bestehen.

---

## Sperrrregeln (absolut)

- Kein Proposal → Todo (automatisch)
- Kein Proposal → Task (automatisch)
- Kein Proposal → ORBIT (automatisch)
- Kein Proposal → Workspace-Aktion (automatisch)
- Kein Proposal → automatische Umsetzung

Erst nach expliziter Entscheidung durch Tommy kann ein angenommenes Proposal
manuell in Arbeit überführt werden.

---

## soul.md / style.md Pfad

Explizit abgedeckt durch `self_constitution_change` und `behavior_adjustment`.
Kimi darf Unstimmigkeiten in soul.md formal vorschlagen — aber nicht selbst ändern.

---

## Geänderte Dateien

| Datei | Änderung |
|---|---|
| `core/proposal_service_wp10.py` | Neu: Proposal-CRUD, Typen, Status, Seed-Übergang |
| `core/database.py` | `init_wp10_proposals_table` in `init_db()` |
| `core/kimi_output.py` | `[WP10_PROPOSAL:]` Marker + `_run_wp10_proposal()` |
| `tools.md` | WP10_PROPOSAL Instruktion für Kimi |
| `WP10_PROPOSALS.md` | Neu: dieses Dokument |
