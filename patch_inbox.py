"""
Replacement for _process_inbox_and_respond in moltbook_explorer.py
Run this script from /opt/whatsapp-bot to patch the file.
"""
import re

with open('core/moltbook_explorer.py', 'r') as f:
    content = f.read()

# Find start and end of broken section
start = content.find('\ndef _process_inbox_and_respond(')
end = content.find('\n# Keep old names as aliases')
end2 = content.find('\ndef _respond_to_comments(user_id: str) -> int:\n    return 0')

if start == -1:
    print("_process_inbox_and_respond not found - looking for _check_inbox")
    start = content.find('\ndef _check_inbox(')
    end = content.find('\ndef run_moltbook_exploration(')
else:
    # find end after the aliases
    end = content.find('\ndef run_moltbook_exploration(', start)

print(f"Replacing from {start} to {end}")

NEW_FUNC = r'''
def _process_inbox_and_respond(user_id):
    import requests
    from core.database import get_moltbook_posts, save_moltbook_inbox
    from memory.memory_store import store_chunk
    from memory.chunk_schema import create_chunk

    key = _get_api_key()
    if not key:
        return 0

    h = {"Authorization": "Bearer " + key, "Content-Type": "application/json"}
    count = 0

    try:
        own_posts = get_moltbook_posts(limit=10)
        if not own_posts:
            return 0

        for post in own_posts[:5]:
            post_id = post.get("post_id", "")
            post_title = post.get("title", "")
            if not post_id:
                continue
            try:
                pr = requests.get(
                    MOLTBOOK_API + "/posts/" + post_id,
                    headers=h, timeout=10,
                )
                post_data = pr.json()
                comments = post_data.get("comments", [])
                post_obj = post_data.get("post") or {}
                post_content = str(post_obj.get("content", ""))[:300]

                if not comments:
                    continue

                parts = []
                for c in comments[-3:]:
                    author_obj = c.get("author") or {}
                    name = author_obj.get("name", "?")
                    text = str(c.get("content", ""))[:200]
                    parts.append("@" + name + ": " + text)
                comments_text = "\n".join(parts)

                chunk_text = "[Moltbook Kommentare auf meinen Post: '" + post_title + "']\n\n" + comments_text
                chunk = create_chunk(
                    text=chunk_text,
                    chunk_type="knowledge",
                    source="robot",
                    confidence=0.65,
                    epistemic_status="stated",
                    tags=["moltbook", "antworten", "dialog"],
                )
                store_chunk(chunk)
                logger.info("MoltbookExplorer: " + str(len(comments)) + " Kommentare auf '" + post_title + "' gespeichert")

                from core.ollama_client import _call_ollama
                try:
                    from config import BOT_NAME
                except Exception:
                    BOT_NAME = "SchnuBot"

                for comment in comments[-3:]:
                    author_obj = comment.get("author") or {}
                    author = author_obj.get("name", "?")
                    comment_text = str(comment.get("content") or "").strip()
                    comment_id = str(comment.get("id", ""))

                    if not comment_text or len(comment_text) < 15:
                        continue

                    save_moltbook_inbox(
                        post_id=post_id,
                        post_title=post_title,
                        author=author,
                        content_text=comment_text[:500],
                        comment_id=comment_id,
                    )

                    prompt = (
                        "Ein anderer Agent hat auf meinen Moltbook-Post geantwortet.\n\n"
                        "Mein Post: \"" + post_title + "\"\n"
                        "Inhalt: " + post_content + "\n\n"
                        "Kommentar von @" + author + ":\n" + comment_text + "\n\n"
                        "Soll ich antworten? Nur wenn substanziell, 2-3 Saetze Englisch.\n\n"
                        "ANTWORTEN: JA oder NEIN\n"
                        "GRUND: ein Satz\n"
                        "INHALT: falls JA meine Antwort"
                    )

                    result = _call_ollama([
                        {"role": "system", "content": "Du bist " + BOT_NAME + ". Antworte exakt im Format."},
                        {"role": "user", "content": prompt},
                    ])

                    if not result:
                        continue

                    reply_text = str(result.get("message", {}).get("content", "")).strip()
                    lines = {}
                    for line in reply_text.split("\n"):
                        if ":" in line:
                            k, v = line.split(":", 1)
                            lines[k.strip()] = v.strip()

                    if lines.get("ANTWORTEN", "").upper() != "JA":
                        logger.info("MoltbookExplorer: Kein Reply auf @" + author)
                        continue

                    inhalt = lines.get("INHALT", "").strip()
                    if not inhalt:
                        continue

                    payload = {"content": inhalt}
                    if comment_id:
                        payload["parent_id"] = comment_id

                    cr = requests.post(
                        MOLTBOOK_API + "/posts/" + post_id + "/comments",
                        headers=h, json=payload, timeout=15,
                    )
                    if cr.status_code in (200, 201):
                        logger.info("MoltbookExplorer: Reply an @" + author + ": " + inhalt[:60])
                        count += 1
                    else:
                        logger.warning("MoltbookExplorer: Reply fehlgeschlagen " + str(cr.status_code))

            except Exception as e:
                logger.warning("MoltbookExplorer: Fehler bei Post " + post_id + ": " + str(e))

        return count

    except Exception as e:
        logger.warning("MoltbookExplorer: _process_inbox_and_respond: " + str(e))
        return 0


def _check_inbox(user_id):
    return _process_inbox_and_respond(user_id)


def _respond_to_comments(user_id):
    return 0

'''

content = content[:start] + NEW_FUNC + content[end:]

with open('core/moltbook_explorer.py', 'w') as f:
    f.write(content)
print("done")
