with open('core/database.py', 'r') as f:
    content = f.read()

# Add direction column to moltbook_inbox
old = '''    cursor.execute("""
        CREATE TABLE IF NOT EXISTS moltbook_inbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            received_at TEXT NOT NULL,
            post_id TEXT NOT NULL,
            post_title TEXT,
            author TEXT NOT NULL,
            content TEXT NOT NULL,
            comment_id TEXT,
            processed INTEGER DEFAULT 0
        )
    """)'''

new = '''    cursor.execute("""
        CREATE TABLE IF NOT EXISTS moltbook_inbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            received_at TEXT NOT NULL,
            post_id TEXT NOT NULL,
            post_title TEXT,
            author TEXT NOT NULL,
            content TEXT NOT NULL,
            comment_id TEXT,
            processed INTEGER DEFAULT 0,
            direction TEXT DEFAULT 'in'
        )
    """)
    # Migration: add direction column if missing
    try:
        conn.execute("ALTER TABLE moltbook_inbox ADD COLUMN direction TEXT DEFAULT 'in'")
        conn.commit()
    except Exception:
        pass'''

if old in content:
    content = content.replace(old, new)
    print("table updated")
else:
    print("table NOT FOUND")

# Update save_moltbook_inbox to accept direction
old2 = '''def save_moltbook_inbox(post_id, post_title, author, content_text, comment_id=""):
    conn = get_connection()
    try:
        conn.execute("INSERT OR IGNORE INTO moltbook_inbox (received_at, post_id, post_title, author, content, comment_id) VALUES (?, ?, ?, ?, ?, ?)",
            (to_iso(), post_id, post_title, author, content_text, comment_id))
        conn.commit()
    except Exception as e:
        logger.warning(f"save_moltbook_inbox: {e}")
    finally:
        conn.close()'''

new2 = '''def save_moltbook_inbox(post_id, post_title, author, content_text, comment_id="", direction="in"):
    conn = get_connection()
    try:
        conn.execute("INSERT INTO moltbook_inbox (received_at, post_id, post_title, author, content, comment_id, direction) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (to_iso(), post_id, post_title, author, content_text, comment_id, direction))
        conn.commit()
    except Exception as e:
        logger.warning(f"save_moltbook_inbox: {e}")
    finally:
        conn.close()'''

if old2 in content:
    content = content.replace(old2, new2)
    print("save function updated")
else:
    print("save NOT FOUND")

with open('core/database.py', 'w') as f:
    f.write(content)
print("done")
