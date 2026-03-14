#!/bin/bash
DATE=$(date +%Y-%m-%d_%H%M)
DEST="/opt/whatsapp-bot/data/backups/chromadb_${DATE}.tar.gz"
mkdir -p /opt/whatsapp-bot/data/backups
sqlite3 /opt/whatsapp-bot/data/chromadb/chroma.sqlite3 "PRAGMA wal_checkpoint(FULL);" 2>/dev/null
tar -czf "$DEST" -C /opt/whatsapp-bot data/chromadb
ls -t /opt/whatsapp-bot/data/backups/chromadb_*.tar.gz | tail -n +9 | xargs rm -f 2>/dev/null
echo "Backup done: $DEST"
