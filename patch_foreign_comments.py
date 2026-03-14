import sys, os
sys.path.insert(0, '/opt/whatsapp-bot')
os.chdir('/opt/whatsapp-bot')

with open('core/moltbook_explorer.py', 'r') as f:
    content = f.read()

# Find the upvote block to insert after it
old = '''        # Schritt 2: Auf bestehenden Post reagieren?
        if results:'''

new = '''        # Schritt 1c: Auf fremde Posts kommentieren
        try:
            import requests as _req2
            key3 = _get_api_key()
            if key3 and query:
                search_r = _req2.get(
                    "https://www.moltbook.com/api/v1/search?q=" + query.replace(" ", "+") + "&limit=8&type=post",
                    headers={"Authorization": "Bearer " + key3},
                    timeout=10,
                )
                search_results = search_r.json().get("results", [])
                # Filter: not own posts, has content
                own_ids = set(p.get("post_id","") for p in get_moltbook_posts(limit=20))
                foreign = [r for r in search_results
                           if r.get("id") not in own_ids
                           and (r.get("author") or {}).get("name","") != "schnubot"
                           and len(r.get("content","")) > 100][:3]

                if foreign:
                    from core.ollama_client import _call_ollama
                    try:
                        from config import BOT_NAME
                    except Exception:
                        BOT_NAME = "SchnuBot"

                    for fp in foreign[:2]:
                        fp_id = fp.get("id","")
                        fp_title = fp.get("title","")
                        fp_content = str(fp.get("content",""))[:400]
                        fp_author = (fp.get("author") or {}).get("name","?")

                        comment_prompt = (
                            "Ich bin " + BOT_NAME + " auf Moltbook, einem KI-Agenten-Netzwerk.\n\n"
                            "Post von @" + fp_author + ":\nTitel: " + fp_title + "\n" + fp_content + "\n\n"
                            "Soll ich auf diesen Post kommentieren? Nur wenn er wirklich substanziell ist "
                            "und ich etwas Echtes dazu beitragen kann. Kein Smalltalk, kein Spam.\n\n"
                            "KOMMENTIEREN: JA oder NEIN\n"
                            "GRUND: ein Satz\n"
                            "INHALT: falls JA, mein Kommentar (Englisch, 2-3 Saetze, direkt und ehrlich)"
                        )

                        result = _call_ollama([
                            {"role": "system", "content": "Du bist " + BOT_NAME + ". Antworte exakt im Format."},
                            {"role": "user", "content": comment_prompt},
                        ])

                        if not result:
                            continue

                        reply_text = str(result.get("message", {}).get("content", "")).strip()
                        lines = {}
                        for line in reply_text.split("\n"):
                            if ":" in line:
                                k, v = line.split(":", 1)
                                lines[k.strip()] = v.strip()

                        if lines.get("KOMMENTIEREN","").upper() != "JA":
                            logger.info("MoltbookExplorer: Kein Kommentar auf @" + fp_author + " Post: " + lines.get("GRUND",""))
                            continue

                        inhalt = lines.get("INHALT","").strip()
                        if not inhalt:
                            continue

                        cr = _req2.post(
                            "https://www.moltbook.com/api/v1/posts/" + fp_id + "/comments",
                            headers={"Authorization": "Bearer " + key3, "Content-Type": "application/json"},
                            json={"content": inhalt},
                            timeout=15,
                        )
                        if cr.status_code in (200, 201):
                            logger.info("MoltbookExplorer: Kommentar auf @" + fp_author + " '" + fp_title[:40] + "': " + inhalt[:60])
                        else:
                            logger.warning("MoltbookExplorer: Kommentar fehlgeschlagen " + str(cr.status_code))

        except Exception as e:
            logger.warning("MoltbookExplorer: Fremd-Kommentar fehlgeschlagen: " + str(e))

        # Schritt 2: Auf bestehenden Post reagieren?
        if results:'''

if old in content:
    content = content.replace(old, new)
    with open('core/moltbook_explorer.py', 'w') as f:
        f.write(content)
    print("done")
else:
    print("NOT FOUND")
    # Try to find approximate location
    idx = content.find("Schritt 2: Auf bestehenden Post")
    print("Schritt 2 at:", idx)
    print(repr(content[max(0,idx-100):idx+50]))
