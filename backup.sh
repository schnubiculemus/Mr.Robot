#!/bin/bash
# SchnuBot.ai — Backup (W11: konsistent, Dienste gestoppt)
# Täglicher Cronjob: vollständiger Tarball + Rotation
# Restore: bash restore.sh <backup-datei>

BACKUP_DIR="/opt/whatsapp-bot/backups"
PROJECT_DIR="/opt/whatsapp-bot"
MAX_BACKUPS=7
DATE=$(date +%Y-%m-%d_%H%M)
BACKUP_FILE="$BACKUP_DIR/schnubot_backup_${DATE}.tar.gz"
LOG="$PROJECT_DIR/logs/backup.log"

mkdir -p "$BACKUP_DIR"

# W11: Dienste stoppen vor Backup — kein paralleler Chroma-Zugriff
echo "$(date): Stoppe Services für konsistentes Backup..." | tee -a "$LOG"
systemctl stop schnubot schnubot-cognition schnubot-dashboard 2>/dev/null
sleep 3

# Dateien die gesichert werden
TARGETS=(
    "data/bot.db"
    "data/chromadb"
    "data/token_usage.json"
    "data/tools_config.json"
    "soul.md"
    "rules.md"
    "tools.md"
    "architecture.md"
    "heartbeat_state.json"
    "arch_update_state.json"
    "soul_pr_pending.json"
    "diary"
)

# Nur vorhandene Pfade sichern
EXISTING=()
for target in "${TARGETS[@]}"; do
    if [ -e "$PROJECT_DIR/$target" ]; then
        EXISTING+=("$target")
    fi
done

tar -czf "$BACKUP_FILE" -C "$PROJECT_DIR" "${EXISTING[@]}" 2>/dev/null

if [ $? -eq 0 ]; then
    SIZE=$(du -h "$BACKUP_FILE" | cut -f1)
    echo "$(date): Backup OK: $BACKUP_FILE ($SIZE)" | tee -a "$LOG"
else
    echo "$(date): FEHLER beim Tarball!" | tee -a "$LOG" >&2
    # Dienste wieder starten auch bei Fehler
    systemctl start schnubot schnubot-dashboard schnubot-cognition 2>/dev/null
    exit 1
fi

# W11: Chroma-Healthcheck nach Backup (Backup auf Lesbarkeit prüfen)
echo "$(date): Prüfe Backup-Integrität..." | tee -a "$LOG"
HEALTH_OK=true

# Temporäres Entpacken für Healthcheck
TMPDIR=$(mktemp -d)
tar -xzf "$BACKUP_FILE" -C "$TMPDIR" "data/chromadb" 2>/dev/null
if [ $? -eq 0 ]; then
    cd "$PROJECT_DIR" && source venv/bin/activate 2>/dev/null
    CHROMA_TEST_PATH="$TMPDIR/data/chromadb" python3 scripts/chroma_healthcheck.py >> "$LOG" 2>&1
    HEALTH_EXIT_CODE=$?
    if [ "$HEALTH_EXIT_CODE" -eq 0 ]; then
        echo "$(date): Healthcheck OK (exit=0)" | tee -a "$LOG"
    else
        echo "$(date): Healthcheck WARN (exit=$HEALTH_EXIT_CODE)" | tee -a "$LOG"
        HEALTH_OK=false
    fi
else
    echo "$(date): Healthcheck SKIP — chromadb nicht im Backup gefunden" | tee -a "$LOG"
fi
rm -rf "$TMPDIR"

# Rotation: nur die letzten MAX_BACKUPS behalten
BACKUP_COUNT=$(ls -1 "$BACKUP_DIR"/schnubot_backup_*.tar.gz 2>/dev/null | wc -l)
if [ "$BACKUP_COUNT" -gt "$MAX_BACKUPS" ]; then
    REMOVE_COUNT=$((BACKUP_COUNT - MAX_BACKUPS))
    ls -1t "$BACKUP_DIR"/schnubot_backup_*.tar.gz | tail -n "$REMOVE_COUNT" | xargs rm -f
    echo "$(date): $REMOVE_COUNT alte Backups entfernt" | tee -a "$LOG"
fi

echo "$(date): Verfügbare Backups: $(ls -1 $BACKUP_DIR/schnubot_backup_*.tar.gz 2>/dev/null | wc -l)" | tee -a "$LOG"

# W11: Dienste gestuft wieder starten (wie restore.sh)
echo "$(date): Starte Services gestuft..." | tee -a "$LOG"
systemctl start schnubot 2>/dev/null
sleep 5
systemctl is-active schnubot --quiet && echo "$(date): schnubot: aktiv" | tee -a "$LOG" || echo "$(date): schnubot: WARN nicht aktiv" | tee -a "$LOG"

systemctl start schnubot-dashboard 2>/dev/null
sleep 5
systemctl is-active schnubot-dashboard --quiet && echo "$(date): schnubot-dashboard: aktiv" | tee -a "$LOG" || echo "$(date): schnubot-dashboard: WARN nicht aktiv" | tee -a "$LOG"

systemctl start schnubot-cognition 2>/dev/null
sleep 5
systemctl is-active schnubot-cognition --quiet && echo "$(date): schnubot-cognition: aktiv" | tee -a "$LOG" || echo "$(date): schnubot-cognition: WARN nicht aktiv" | tee -a "$LOG"

if $HEALTH_OK; then
    echo "$(date): Backup abgeschlossen — Healthcheck grün" | tee -a "$LOG"
    exit 0
else
    echo "$(date): Backup WARN — Backup erstellt, aber Chroma-Healthcheck nicht bestanden" | tee -a "$LOG"
    echo "$(date): Backup-Datei: $BACKUP_FILE (vorhanden, aber nicht als Gold-Backup freigegeben)" | tee -a "$LOG"
    exit 2
fi
