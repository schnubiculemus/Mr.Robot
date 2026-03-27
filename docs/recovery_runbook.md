# Recovery Runbook — SchnuBot.ai (W11)

## Grundprinzip

Ein Zustand ist erst dann echt, wenn er sich sicher sichern, prüfen und wiederherstellen lässt.

**Regel 1:** Vor jedem Backup und jedem Restore alle Services stoppen.
**Regel 2:** `get_stats()` / `count()` sind kein Gesundheitsnachweis für Chroma.
**Regel 3:** Nach Restore immer erst Healthcheck, dann gestufter Start.
**Regel 4:** Heartbeat und Cognition erst starten wenn Chroma als gesund gilt.

---

## Startreihenfolge (normal)

```bash
systemctl start schnubot
# warten bis Bot antwortet
systemctl start schnubot-dashboard
# warten bis Dashboard lädt
systemctl start schnubot-cognition
```

**Nicht erlaubt:** alle gleichzeitig starten.

---

## Backup erstellen

```bash
bash /opt/whatsapp-bot/backup.sh
```

Das Skript stoppt alle Services, erstellt den Tarball, führt einen Healthcheck durch und startet Services wieder.

**Manuell (Notfall):**
```bash
systemctl stop schnubot schnubot-cognition schnubot-dashboard
sleep 3
tar -czf /opt/whatsapp-bot/backups/schnubot_backup_$(date +%Y-%m-%d_%H%M).tar.gz \
    -C /opt/whatsapp-bot \
    data/bot.db data/chromadb soul.md rules.md tools.md heartbeat_state.json diary
systemctl start schnubot schnubot-dashboard schnubot-cognition
```

---

## Restore durchführen

```bash
bash /opt/whatsapp-bot/restore.sh backups/schnubot_backup_DATUM.tar.gz
```

Das Skript:
1. Stoppt alle Services
2. Erstellt Sicherheitskopie des aktuellen Zustands
3. Entfernt altes chromadb
4. Spielt Backup ein
5. Führt DB-Migration aus
6. Führt Chroma-Healthcheck aus
7. Startet Services gestuft (nur wenn Healthcheck grün)

---

## Chroma-Healthcheck

```bash
cd /opt/whatsapp-bot && source venv/bin/activate
python3 scripts/chroma_healthcheck.py
```

**Interpretation:**
- `BACKUP_OK` — alle vier Checks grün, Backup freigabefähig
- `BACKUP_FAIL` — mindestens ein Check fehlgeschlagen, nicht starten

**Vier Pflichtchecks:**
1. `count` — Collections vorhanden, nicht leer
2. `docs_get` — Dokumente und Metadaten lesbar
3. `embeddings_get` — Embedding-Vektoren lesbar
4. `query` — Semantische Suche funktioniert

---

## Bekannte Fehlerbilder

### count() OK, get(embeddings) schlägt fehl
**Diagnose:** Chroma-Backup unvollständig oder HNSW-Index korrupt.
**Lösung:** Älteres Backup testen.

### Segfault beim embed_query()
**Diagnose:** Embedding-Stack (torch/sentence-transformers) Problem, nicht Chroma.
**Lösung:** Chroma ohne Embedder testen (`get(embeddings)` mit gespeichertem Vektor).

### `no such table: active_working_context`
**Diagnose:** DB-Migration nicht gelaufen.
**Lösung:**
```bash
cd /opt/whatsapp-bot && source venv/bin/activate
python3 -c "from core.database import init_db; init_db(); print('OK')"
```

### `Error finding id` bei get()
**Diagnose:** Chroma-Metadaten-Segment korrupt.
**Lösung:** Älteres Backup testen oder ChromaDB komplett neu aufbauen via Heartbeat.

### Dashboard zeigt `Unexpected token '<'`
**Diagnose:** API gibt HTML zurück (Python-Fehler).
**Lösung:**
```bash
journalctl -u schnubot-dashboard -n 20 --no-pager | grep -i error
```

---

## Parallele Zugriffe vermeiden

**Kritische Regel:** Nie Chroma-Operationen starten während zwei Services gleichzeitig laufen.

Besonders problematisch:
- Heartbeat + Cognition gleichzeitig auf Chroma
- Restore während Dashboard läuft
- Backup während Bot aktiv schreibt

---

## Gold-Backup erstellen

Nach erfolgreichem Healthcheck ein Backup als "Gold" markieren:

```bash
cp backups/schnubot_backup_DATUM.tar.gz backups/gold_schnubot_backup_DATUM.tar.gz
```

Gold-Backups werden bei Rotation nicht automatisch gelöscht.

---

## Notfall: ChromaDB komplett neu aufbauen

Wenn kein gesundes Backup verfügbar:

```bash
systemctl stop schnubot schnubot-cognition schnubot-dashboard
rm -rf /opt/whatsapp-bot/data/chromadb/
# Bot starten — Heartbeat baut aus SQLite-Messages neu auf
systemctl start schnubot
python3 /opt/whatsapp-bot/heartbeat.py
# Dann Healthcheck
python3 /opt/whatsapp-bot/scripts/chroma_healthcheck.py
```

Chunks gehen verloren, werden aber aus Messages neu konsolidiert.

---

## Service-Befehle Übersicht

```bash
# Status
systemctl status schnubot schnubot-dashboard schnubot-cognition

# Alle stoppen
systemctl stop schnubot schnubot-cognition schnubot-dashboard

# Gestuft starten
systemctl start schnubot && sleep 5
systemctl start schnubot-dashboard && sleep 5
systemctl start schnubot-cognition

# Logs
journalctl -u schnubot -n 50 --no-pager
journalctl -u schnubot-dashboard -n 50 --no-pager
journalctl -u schnubot-cognition -n 50 --no-pager
```
