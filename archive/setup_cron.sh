#!/bin/bash
# SchnuBot.ai - Cronjob Setup (Phase 6b)
# Richtet Heartbeat und Backup als Cronjobs ein.

CRON_FILE="/tmp/schnubot_cron"

# Bestehende Crontab sichern
crontab -l 2>/dev/null > "$CRON_FILE" || true

# Alte SchnuBot-Einträge entfernen
sed -i '/schnubot/Id' "$CRON_FILE"
sed -i '/whatsapp-bot/Id' "$CRON_FILE"

# Neue Einträge
cat >> "$CRON_FILE" << 'CRON'

# === SchnuBot.ai Cronjobs ===

# Heartbeat: alle 3 Stunden (Konsolidierung + Proaktiv-Engine)
0 */3 * * * cd /opt/whatsapp-bot && /opt/whatsapp-bot/venv/bin/python heartbeat.py >> /opt/whatsapp-bot/logs/heartbeat_cron.log 2>&1

# Backup: täglich um 4:00 Uhr
0 4 * * * /opt/whatsapp-bot/backup.sh >> /opt/whatsapp-bot/logs/backup.log 2>&1

CRON

crontab "$CRON_FILE"
rm "$CRON_FILE"

echo "Cronjobs installiert:"
crontab -l | grep -v "^#" | grep -v "^$"
