#!/bin/bash
# SchnuBot.ai — Restore (W11: kontrolliert, gestuft, mit Healthcheck)
# Verwendung: bash restore.sh <backup-datei>
# Beispiel:   bash restore.sh backups/schnubot_backup_2026-03-17_0400.tar.gz

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

read -p "Restore durchführen? Alle Services werden gestoppt. [j/N] " confirm
if [ "$confirm" != "j" ] && [ "$confirm" != "J" ]; then
    echo "Abgebrochen."
    exit 0
fi

# W11: Alle Services stoppen — kein paralleler Zugriff
echo "$(date): Stoppe ALLE Services..." | tee -a "$LOG"
systemctl stop schnubot schnubot-cognition schnubot-dashboard 2>/dev/null
sleep 3

# Sicherheitskopie des aktuellen Zustands (W11: nur vorhandene Pfade)
SAFETY_BACKUP="$PROJECT_DIR/backups/pre_restore_$(date +%Y-%m-%d_%H%M).tar.gz"
echo "Erstelle Sicherheitskopie: $SAFETY_BACKUP"
SAFETY_TARGETS=(
    "data/bot.db" "data/chromadb" "data/token_usage.json" "data/tools_config.json"
    "soul.md" "rules.md" "tools.md" "architecture.md"
    "heartbeat_state.json" "arch_update_state.json" "soul_pr_pending.json" "diary"
)
SAFETY_EXISTING=()
for t in "${SAFETY_TARGETS[@]}"; do
    [ -e "$PROJECT_DIR/$t" ] && SAFETY_EXISTING+=("$t")
done
tar -czf "$SAFETY_BACKUP" -C "$PROJECT_DIR" "${SAFETY_EXISTING[@]}" 2>/dev/null
if [ $? -eq 0 ]; then
    echo "Sicherheitskopie erstellt: $SAFETY_BACKUP"
else
    echo "WARN: Sicherheitskopie fehlgeschlagen — Restore wird trotzdem fortgesetzt"
fi

# Chroma komplett entfernen vor Restore
echo "Entferne altes chromadb..."
rm -rf "$PROJECT_DIR/data/chromadb"

# Restore
echo "Starte Restore..."
tar -xzf "$BACKUP_FILE" -C "$PROJECT_DIR" 2>/dev/null

if [ $? -ne 0 ]; then
    echo "$(date): FEHLER beim Restore!" | tee -a "$LOG" >&2
    echo "Sicherheitskopie liegt unter: $SAFETY_BACKUP"
    exit 1
fi

echo "$(date): Restore eingespielt: $BACKUP_FILE" | tee -a "$LOG"

# W11: DB-Schema sicherstellen
echo "DB-Schema initialisieren..."
cd "$PROJECT_DIR" && source venv/bin/activate && \
    python3 -c "from core.database import init_db; init_db(); print('DB OK')" 2>&1 | tee -a "$LOG"

# W11: Chroma-Healthcheck OFFLINE (keine Services laufen)
echo ""
echo "=== Chroma-Healthcheck ==="
cd "$PROJECT_DIR" && source venv/bin/activate && \
    python3 scripts/chroma_healthcheck.py 2>&1 | tee -a "$LOG"
HEALTH_EXIT=${PIPESTATUS[0]}

echo ""
if [ "$HEALTH_EXIT" -eq 0 ]; then
    echo "Healthcheck: GRÜN — Backup ist freigabefähig"
    echo ""
    echo "W11 Startsequenz:"
    echo "  1. systemctl start schnubot"
    echo "  2. (warten, prüfen)"
    echo "  3. systemctl start schnubot-dashboard"
    echo "  4. (warten, prüfen)"
    echo "  5. systemctl start schnubot-cognition"
    echo ""
    read -p "Bot jetzt starten? [j/N] " start_confirm
    if [ "$start_confirm" = "j" ] || [ "$start_confirm" = "J" ]; then
        echo "Starte schnubot..."
        systemctl start schnubot
        sleep 5
        systemctl is-active schnubot --quiet && echo "schnubot: aktiv" || echo "schnubot: WARN nicht aktiv"

        echo "Starte schnubot-dashboard..."
        systemctl start schnubot-dashboard
        sleep 5
        systemctl is-active schnubot-dashboard --quiet && echo "schnubot-dashboard: aktiv" || echo "schnubot-dashboard: WARN nicht aktiv"

        echo "Starte schnubot-cognition..."
        systemctl start schnubot-cognition
        sleep 5
        systemctl is-active schnubot-cognition --quiet && echo "schnubot-cognition: aktiv" || echo "schnubot-cognition: WARN nicht aktiv"

        echo "$(date): Gestufter Start abgeschlossen" | tee -a "$LOG"
    else
        echo "Services nicht gestartet — W11 Startsequenz manuell:"
        echo "  systemctl start schnubot && sleep 5 && systemctl is-active schnubot"
        echo "  systemctl start schnubot-dashboard && sleep 5 && systemctl is-active schnubot-dashboard"
        echo "  systemctl start schnubot-cognition"
    fi
else
    echo "Healthcheck: ROT — Backup nicht freigabefähig!"
    echo "Services bleiben gestoppt."
    echo "Sicherheitskopie: $SAFETY_BACKUP"
    echo "$(date): Restore WARN: Healthcheck fehlgeschlagen" | tee -a "$LOG"
    exit 1
fi
