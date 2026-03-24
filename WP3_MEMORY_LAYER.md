# WP3 – Memory Layer

Stand: 2026-03-24

## Leitprinzip

**Memory unterstützt Kimi Core — führt nicht.**

Priorität der Kontextquellen:
```
Active Working Context (AWC)
    ↓
Fast-Track
    ↓
Typed Memory / ChromaDB
    ↓
Diary (private Selbstspur)
    ↓
breites Retrieval / Rest
```

---

## Memory-Bestandteile in V2

| Baustein | Rolle | Grenzen |
|---|---|---|
| **Fast-Track** | kurzfristige Relevanz, aktueller Fokus | ersetzt nicht AWC, dient Jetzt |
| **Konsolidierer** | Langzeitverdichtung, Muster | startet keine Tasks, schreibt nicht in Workspace |
| **Diary** | private Selbstspur, Identität | nicht operativ steuernd, privat |
| **Typed Memory (ChromaDB)** | strukturierte Erinnerung | nachgeordnet, nicht primärer Lageersatz |

---

## WP3 Memory-Verbote

Memory darf **nicht**:
- Prioritäten setzen (nur Kimi Core entscheidet)
- den Active Working Context überschreiben
- Tasks selbst starten
- Workspace-Dokumente autonom anlegen
- operative Führung aus Ähnlichkeit ableiten

---

## Technische Umsetzung

`build_system_prompt()` in `ollama_client.py`:
1. AWC → via `doc_context` aus `kimi_core.process()` (WP2)
2. Fast-Track → `get_fast_track_chunks()` (neu in Schritt 7b)
3. Typed Memory → `prefetched_chunks` (wie bisher)
4. Diary → Teil von Typed Memory (`self_reflection source=robot`), nur als Stil-Spur
5. Globale Regeln → am Ende (unverändert)

---

## Kritische Altpfade (temporary_compat / delete_candidate)

| Pfad | Label | Begründung |
|---|---|---|
| `load_cognition_echo()` in build_system_prompt | `temporary_compat` | Kognitions-Echo aus altem System |
| `score_and_select()` direkt in `chat()` | `temporary_compat` | ersetzt durch kimi_core-gesteuertes Retrieval |
| Diary als impliziter Kognitions-Trigger | `delete_candidate` | WP0 deaktiviert |
| `proposed_pattern` im Memory-Prompt | `temporary_compat` | prüfen ob noch nötig |

---

## Akzeptanzcheck WP3

| Kriterium | Status |
|---|---|
| AWC → Fast-Track → Typed Memory → Diary → Rest | ✓ in build_system_prompt sichtbar |
| Fast-Track als explizite Schicht | ✓ Schritt 7b |
| Diary privat und nicht steuernd | ✓ nur als Stil-Spur in self_reflection |
| Konsolidierer verdichtend, nicht führend | ✓ kein direkter Steuerungspfad |
| Typed Memory nachgeordnet | ✓ nach AWC + Fast-Track |
| Memory-Verbote dokumentiert | ✓ in chat() + diese Datei |
| Kritische Altpfade markiert | ✓ temporary_compat / delete_candidate |
