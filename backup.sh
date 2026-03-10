#!/bin/bash
# SchnuBot.ai — Backup
# Täglicher Cronjob: vollständiger Tarball + Rotation
# Restore: bash restore.sh <backup-datei>

BACKUP_DIR="/opt/whatsapp-bot/backups"
PROJECT_DIR="/opt/whatsapp-bot"
MAX_BACKUPS=7
DATE=$(date +%Y-%m-%d_%H%M)
BACKUP_FILE="$BACKUP_DIR/schnubot_backup_${DATE}.tar.gz"
LOG="$PROJECT_DIR/logs/backup.log"

mkdir -p "$BACKUP_DIR"

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
    exit 1
fi

# Rotation: nur die letzten MAX_BACKUPS behalten
BACKUP_COUNT=$(ls -1 "$BACKUP_DIR"/schnubot_backup_*.tar.gz 2>/dev/null | wc -l)
if [ "$BACKUP_COUNT" -gt "$MAX_BACKUPS" ]; then
    REMOVE_COUNT=$((BACKUP_COUNT - MAX_BACKUPS))
    ls -1t "$BACKUP_DIR"/schnubot_backup_*.tar.gz | tail -n "$REMOVE_COUNT" | xargs rm -f
    echo "$(date): $REMOVE_COUNT alte Backups entfernt" | tee -a "$LOG"
fi

echo "$(date): Verfügbare Backups: $(ls -1 $BACKUP_DIR/schnubot_backup_*.tar.gz 2>/dev/null | wc -l)" | tee -a "$LOG"
