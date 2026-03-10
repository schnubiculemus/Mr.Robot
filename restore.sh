#!/bin/bash
# SchnuBot.ai — Restore
# Verwendung: bash restore.sh <backup-datei>
# Beispiel:   bash restore.sh backups/schnubot_backup_2026-03-10_0300.tar.gz

PROJECT_DIR="/opt/whatsapp-bot"
LOG="$PROJECT_DIR/logs/backup.log"

if [ -z "$1" ]; then
    echo "Verwendung: bash restore.sh <backup-datei>"
    echo ""
    echo "Verfügbare Backups:"
    ls -1t "$PROJECT_DIR/backups"/schnubot_backup_*.tar.gz 2>/dev/null || echo "  (keine gefunden)"
    exit 1
fi

BACKUP_FILE="$1"

# Relativen Pfad auflösen
if [[ "$BACKUP_FILE" != /* ]]; then
    BACKUP_FILE="$PROJECT_DIR/$BACKUP_FILE"
fi

if [ ! -f "$BACKUP_FILE" ]; then
    echo "FEHLER: Backup-Datei nicht gefunden: $BACKUP_FILE"
    exit 1
fi

echo "Backup-Datei: $BACKUP_FILE"
echo "Inhalt:"
tar -tzf "$BACKUP_FILE"
echo ""

read -p "Restore durchführen? Laufende Prozesse werden gestoppt. [j/N] " confirm
if [ "$confirm" != "j" ] && [ "$confirm" != "J" ]; then
    echo "Abgebrochen."
    exit 0
fi

# Services stoppen
echo "Stoppe Services..."
systemctl stop schnubot 2>/dev/null
pkill -f dashboard.py 2>/dev/null
sleep 2

# Sicherheitskopie des aktuellen Zustands
SAFETY_BACKUP="$PROJECT_DIR/backups/pre_restore_$(date +%Y-%m-%d_%H%M).tar.gz"
echo "Erstelle Sicherheitskopie des aktuellen Zustands: $SAFETY_BACKUP"
tar -czf "$SAFETY_BACKUP" -C "$PROJECT_DIR" \
    data/bot.db data/chromadb data/token_usage.json data/tools_config.json \
    soul.md rules.md tools.md architecture.md \
    heartbeat_state.json arch_update_state.json soul_pr_pending.json \
    diary 2>/dev/null

# Restore
echo "Starte Restore..."
tar -xzf "$BACKUP_FILE" -C "$PROJECT_DIR" 2>/dev/null

if [ $? -eq 0 ]; then
    echo "$(date): Restore OK aus $BACKUP_FILE" | tee -a "$LOG"
    echo ""
    echo "Restore abgeschlossen. Services manuell starten:"
    echo "  systemctl start schnubot"
    echo "  cd /opt/whatsapp-bot && source venv/bin/activate && nohup python dashboard.py > logs/dashboard.log 2>&1 &"
else
    echo "$(date): FEHLER beim Restore!" | tee -a "$LOG" >&2
    echo "Sicherheitskopie liegt unter: $SAFETY_BACKUP"
    exit 1
fi
