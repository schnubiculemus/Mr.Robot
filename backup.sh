#!/bin/bash
# SchnuBot.ai - Backup (Phase 6b)
# Täglicher Cronjob: Tarball + Git Push

BACKUP_DIR="/opt/whatsapp-bot/backups"
PROJECT_DIR="/opt/whatsapp-bot"
MAX_BACKUPS=7
DATE=$(date +%Y-%m-%d_%H%M)

mkdir -p "$BACKUP_DIR"

# 1. Tarball-Backup
BACKUP_FILE="$BACKUP_DIR/schnubot_backup_${DATE}.tar.gz"
tar -czf "$BACKUP_FILE" -C "$PROJECT_DIR" data/chromadb bot.db heartbeat_state.json soul.md architecture.md 2>/dev/null

if [ $? -eq 0 ]; then
    SIZE=$(du -h "$BACKUP_FILE" | cut -f1)
    echo "$(date): Backup erstellt: $BACKUP_FILE ($SIZE)"
else
    echo "$(date): FEHLER beim Tarball!" >&2
fi

# Rotation
BACKUP_COUNT=$(ls -1 "$BACKUP_DIR"/schnubot_backup_*.tar.gz 2>/dev/null | wc -l)
if [ "$BACKUP_COUNT" -gt "$MAX_BACKUPS" ]; then
    REMOVE_COUNT=$((BACKUP_COUNT - MAX_BACKUPS))
    ls -1t "$BACKUP_DIR"/schnubot_backup_*.tar.gz | tail -n "$REMOVE_COUNT" | xargs rm -f
    echo "$(date): $REMOVE_COUNT alte Backups entfernt"
fi

# 2. Git Push
cd "$PROJECT_DIR"
git add -A
git diff --cached --quiet || {
    git commit -m "Auto-Backup $(date +%Y-%m-%d)"
    git push origin main
    echo "$(date): Git Push erfolgreich"
}
