"""
Einmal-Reparatur: Decay-String-Bug fixen.
Findet alle Chunks in ChromaDB wo weight/confidence als String statt Float gespeichert sind
und korrigiert sie.

Usage:
  cd /opt/whatsapp-bot
  HF_HUB_OFFLINE=1 python fix_decay_strings.py         # Dry-Run (nur anzeigen)
  HF_HUB_OFFLINE=1 python fix_decay_strings.py --fix    # Tatsächlich reparieren
"""

import sys
import os

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_DIR)
os.chdir(PROJECT_DIR)

from memory.memory_store import get_active_collection, get_archive_collection


def fix_collection(collection, name, dry_run=True):
    """Prüft und repariert eine Collection."""
    all_data = collection.get(include=["metadatas"])

    if not all_data["ids"]:
        print(f"  {name}: Leer, nichts zu tun.")
        return 0

    fixed = 0
    for i, chunk_id in enumerate(all_data["ids"]):
        meta = all_data["metadatas"][i]
        needs_fix = False
        meta_update = dict(meta)

        for field in ("weight", "confidence"):
            val = meta.get(field)
            if isinstance(val, str):
                try:
                    meta_update[field] = float(val)
                    needs_fix = True
                except ValueError:
                    print(f"  WARNUNG: {chunk_id[:8]} hat ungültigen {field}: '{val}'")
                    continue

        if needs_fix:
            chunk_type = meta.get("chunk_type", "?")
            old_w = meta.get("weight")
            old_c = meta.get("confidence")
            new_w = meta_update["weight"]
            new_c = meta_update["confidence"]

            if dry_run:
                print(f"  [DRY] {chunk_id[:8]} [{chunk_type}] w: '{old_w}'→{new_w} c: '{old_c}'→{new_c}")
            else:
                try:
                    collection.update(ids=[chunk_id], metadatas=[meta_update])
                    print(f"  FIXED {chunk_id[:8]} [{chunk_type}] w: {new_w} c: {new_c}")
                except Exception as e:
                    print(f"  FEHLER {chunk_id[:8]}: {e}")
                    continue

            fixed += 1

    return fixed


def main():
    dry_run = "--fix" not in sys.argv

    if dry_run:
        print("=== DRY-RUN Modus (zeigt nur Probleme, ändert nichts) ===")
        print("    Zum Reparieren: python fix_decay_strings.py --fix\n")
    else:
        print("=== FIX Modus (repariert beschädigte Chunks) ===\n")

    print("Prüfe active Collection...")
    active = get_active_collection()
    fixed_active = fix_collection(active, "memory_active", dry_run)

    print(f"\nPrüfe archive Collection...")
    archive = get_archive_collection()
    fixed_archive = fix_collection(archive, "memory_archive", dry_run)

    total = fixed_active + fixed_archive
    if total == 0:
        print("\nKeine beschädigten Chunks gefunden. Alles sauber.")
    else:
        action = "gefunden" if dry_run else "repariert"
        print(f"\n{total} Chunks {action} (active: {fixed_active}, archive: {fixed_archive})")


if __name__ == "__main__":
    main()
