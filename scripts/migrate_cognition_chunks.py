#!/usr/bin/env python3
"""
scripts/migrate_cognition_chunks.py — Phase 2: Bestehende Rohkognition bereinigen

Entfernt cognition_note und proposal_seed aus memory_active (Chroma).
Überführt sie optional in cognition_entries (SQLite).
self_reflection bleibt in Chroma — das ist eine andere Ebene.

Verwendung:
    python3 scripts/migrate_cognition_chunks.py --dry-run    # nur anzeigen
    python3 scripts/migrate_cognition_chunks.py --migrate    # SQLite übernehmen + Chroma bereinigen
    python3 scripts/migrate_cognition_chunks.py --purge-only # nur aus Chroma löschen
"""

import sys
import os
import argparse

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIR)
os.chdir(PROJECT_DIR)

from memory.memory_store import get_active_collection
from core.cognition_store import init_cognition_entries_table
from core.database import get_connection
from core.datetime_utils import to_iso


def main():
    parser = argparse.ArgumentParser(description="Cognition-Chunks aus Chroma bereinigen")
    parser.add_argument("--dry-run", action="store_true", help="Nur anzeigen, nichts tun")
    parser.add_argument("--migrate", action="store_true", help="In SQLite übernehmen + aus Chroma löschen")
    parser.add_argument("--purge-only", action="store_true", help="Nur aus Chroma löschen")
    args = parser.parse_args()

    if not (args.dry_run or args.migrate or args.purge_only):
        parser.print_help()
        sys.exit(1)

    col = get_active_collection()
    total = col.count()
    print(f"memory_active: {total} Chunks gesamt")

    # Alle cognition_note + proposal_seed finden
    result = col.get(
        where={"chunk_type": {"$in": ["cognition_note", "proposal_seed"]}},
        include=["documents", "metadatas"],
        limit=10000
    )
    ids = result["ids"]
    docs = result["documents"]
    metas = result["metadatas"]

    print(f"Gefunden: {len(ids)} cognition_note/proposal_seed Chunks")

    # self_reflection zur Info zählen
    sr_result = col.get(
        where={"chunk_type": "self_reflection"},
        include=["documents"],
        limit=10000
    )
    print(f"self_reflection: {len(sr_result['ids'])} Chunks — bleiben in Chroma")

    if args.dry_run:
        print("\n--- DRY RUN --- (nichts wird geändert)")
        for i, (cid, doc, meta) in enumerate(zip(ids, docs, metas)):
            print(f"  [{meta.get('chunk_type')}] {cid[:12]} | {doc[:80]}")
        print(f"\nWürden entfernt werden: {len(ids)} Chunks")
        return

    # SQLite-Tabelle sicherstellen
    init_cognition_entries_table()

    migrated = 0
    purged = 0

    if args.migrate or args.purge_only:
        conn = get_connection() if args.migrate else None

        for cid, doc, meta in zip(ids, docs, metas):
            if args.migrate and conn:
                # In SQLite übernehmen (als discarded — war Rohdenkoutput)
                try:
                    conn.execute(
                        """INSERT OR IGNORE INTO cognition_entries
                           (id, user_id, kind, text, confidence, reflection_level,
                            source_context, status, created_at)
                           VALUES (?,?,?,?,?,?,'migrated_from_chroma','discarded',?)""",
                        (
                            cid,
                            meta.get("source", "").replace("cognition:", ""),
                            meta.get("chunk_type", "cognition_note"),
                            doc,
                            float(meta.get("confidence", 0.7)),
                            meta.get("reflection_level", "unknown"),
                            meta.get("created_at", to_iso()),
                        )
                    )
                    migrated += 1
                except Exception as e:
                    print(f"  WARN SQLite-Insert {cid[:12]}: {e}")

        if conn:
            conn.commit()
            conn.close()

        # Aus Chroma entfernen (in Batches)
        batch_size = 100
        for i in range(0, len(ids), batch_size):
            batch = ids[i:i+batch_size]
            try:
                col.delete(ids=batch)
                purged += len(batch)
                print(f"  Gelöscht aus Chroma: {purged}/{len(ids)}")
            except Exception as e:
                print(f"  FEHLER beim Löschen: {e}")

    remaining = col.count()
    print(f"\nFertig:")
    print(f"  Migriert nach SQLite: {migrated}")
    print(f"  Aus Chroma entfernt: {purged}")
    print(f"  Verbleibend in Chroma: {remaining}")
    print(f"  (self_reflection bleibt erhalten)")


if __name__ == "__main__":
    main()
