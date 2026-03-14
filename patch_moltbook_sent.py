with open('core/moltbook_explorer.py', 'r') as f:
    content = f.read()

# Save sent replies to inbox with direction='out'
old = '''                    if cr.status_code in (200, 201):
                        logger.info("MoltbookExplorer: Reply an @" + author + ": " + inhalt[:60])
                        count += 1
                    else:
                        logger.warning("MoltbookExplorer: Reply fehlgeschlagen " + str(cr.status_code))'''

new = '''                    if cr.status_code in (200, 201):
                        logger.info("MoltbookExplorer: Reply an @" + author + ": " + inhalt[:60])
                        count += 1
                        # Antwort in Inbox als 'out' speichern
                        try:
                            save_moltbook_inbox(
                                post_id=post_id,
                                post_title=post_title,
                                author="schnubot",
                                content_text=inhalt,
                                comment_id="reply-to-" + comment_id,
                                direction="out",
                            )
                        except Exception:
                            pass
                    else:
                        logger.warning("MoltbookExplorer: Reply fehlgeschlagen " + str(cr.status_code))'''

if old in content:
    content = content.replace(old, new)
    with open('core/moltbook_explorer.py', 'w') as f:
        f.write(content)
    print("fixed")
else:
    print("NOT FOUND")
