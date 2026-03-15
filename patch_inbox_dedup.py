"""
Einmalige Bereinigung: Duplikate in moltbook_inbox entfernen.
Behält jeweils den ältesten Eintrag (kleinste id) pro comment_id.
"""
import sqlite3

DB_PATH = "data/bot.db"

conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row

# Zeige aktuellen Zustand
total = conn.execute("SELECT COUNT(*) FROM moltbook_inbox").fetchone()[0]
dupes = conn.execute("""
    SELECT comment_id, COUNT(*) as cnt
    FROM moltbook_inbox
    WHERE comment_id IS NOT NULL AND comment_id != ''
    GROUP BY comment_id
    HAVING cnt > 1
""").fetchall()

print(f"Gesamt: {total} Einträge")
print(f"Duplikate (comment_id): {len(dupes)} Gruppen")
for d in dupes:
    print(f"  comment_id={d['comment_id']} → {d['cnt']}x")

if not dupes:
    print("Nichts zu bereinigen.")
    conn.close()
    exit()

# Lösche Duplikate — behalte jeweils kleinste id
deleted = conn.execute("""
    DELETE FROM moltbook_inbox
    WHERE id NOT IN (
        SELECT MIN(id)
        FROM moltbook_inbox
        WHERE comment_id IS NOT NULL AND comment_id != ''
        GROUP BY comment_id
    )
    AND comment_id IS NOT NULL AND comment_id != ''
""").rowcount

conn.commit()

total_after = conn.execute("SELECT COUNT(*) FROM moltbook_inbox").fetchone()[0]
print(f"\n{deleted} Duplikate gelöscht. Verbleibend: {total_after}")
conn.close()
