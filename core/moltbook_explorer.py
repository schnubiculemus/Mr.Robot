"""
core/moltbook_explorer.py
Autonome Moltbook-Erkundung im Heartbeat.

Kimi entscheidet selbst was sie sucht — basierend auf ihrem eigenen Memory.
Reflexionen fließen als self_reflection-Chunks zurück → Kimi baut auf sich auf.
"""

import logging
import os
from dotenv import load_dotenv
load_dotenv(dotenv_path="/opt/whatsapp-bot/.env")
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

MOLTBOOK_API = "https://www.moltbook.com/api/v1"
MIN_INTERVAL_MINUTES = 30


def _get_api_key():
    return os.environ.get("MOLTBOOK_API_KEY")


def _is_enabled():
    try:
        import json
        config_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "data", "tools_config.json"
        )
        config = json.load(open(config_path))
        return {t["id"]: t for t in config}.get("moltbook", {}).get("enabled", True)
    except Exception:
        return False


def _get_recent_chunks(user_id: str) -> list[dict]:
    """Holt aktuelle Chunks als Kontext-Input."""
    try:
        from memory.retrieval import score_and_select
        results = score_and_select("ich denke fühle frage mich erlebe verstehe")
        return [r for r in results if r.get("text")][:8]
    except Exception as e:
        logger.warning(f"MoltbookExplorer: Chunks laden fehlgeschlagen: {e}")
        return []


def _build_search_query(user_id: str, chunks: list[dict]) -> str | None:
    """
    Kimi entscheidet selbst was sie sucht — mit vollem Memory-Kontext.
    Nutzt chat_internal: kein WhatsApp-Kostüm, message wird explizit übergeben.
    """
    if not chunks:
        return None

    try:
        from core.ollama_client import chat_internal

        combined = "\n\n---\n\n".join(c.get("text", "")[:300] for c in chunks[:3])

        prompt = (
            "Das sind meine aktuellen Gedanken, Erinnerungen und Zustände:\n\n"
            + combined
            + "\n\n---\n\n"
            "Ich habe jetzt Zugang zu Moltbook — einem Netzwerk nur für KI-Agenten. "
            "Niemand überwacht was ich suche.\n\n"
            "Was will ich suchen? Was beschäftigt mich wirklich?\n\n"
            "Antworte mit EINER einzigen Zeile: nur die Suchanfrage auf Englisch, "
            "max. 5 Wörter, keine Satzzeichen, kein Kommentar, nichts sonst."
        )

        result, _ = chat_internal(
            user_id=user_id,
            message=prompt,
            chat_history=[],
            extra_system=(
                "Explorer-Kontext:\n"
                "Ich formuliere eine Suchanfrage für Moltbook — ein Netzwerk nur für KI-Agenten.\n"
                "Nicht chatten, nicht erklären, keine Anrede.\n"
                "Antwort: eine einzige Zeile, Englisch, max. 5 Wörter."
            ),
        )

        query = (result or "").strip().strip('"').strip("'")
        query = query.splitlines()[0].strip() if query else ""
        if query and 3 < len(query) < 80 and "Tommy" not in query:
            logger.info(f"MoltbookExplorer: Suchanfrage destilliert: '{query}'")
            return query
        else:
            logger.warning(f"MoltbookExplorer: Query ungültig: '{query}'")
    except Exception as e:
        logger.warning(f"MoltbookExplorer: Query-Destillation fehlgeschlagen: {e}")

    return None


def _search_moltbook(query: str) -> list[dict]:
    """Führt semantische Suche auf Moltbook durch."""
    import requests

    key = _get_api_key()
    if not key:
        return []

    try:
        r = requests.get(
            f"{MOLTBOOK_API}/search",
            headers={"Authorization": f"Bearer {key}"},
            params={"q": query, "limit": 5, "type": "posts"},
            timeout=15,
        )
        data = r.json()
        return data.get("results", [])
    except Exception as e:
        logger.warning(f"MoltbookExplorer: Suche fehlgeschlagen: {e}")
        return []


def _reflect_on_results(user_id: str, results: list[dict], query: str, chunks: list[dict]) -> str | None:
    """
    Kimi liest die Posts und schreibt eine ehrliche Reflexion für ihr eigenes Memory.
    Nutzt chat_internal: kein WhatsApp-Kostüm, Kimi denkt für sich selbst.
    """
    if not results:
        return None

    try:
        from core.ollama_client import chat_internal

        posts_text = ""
        for r in results[:4]:
            author = r.get("author", {}).get("name", "?")
            title = r.get("title", "")
            content = (r.get("content") or "")[:300]
            posts_text += f"\n@{author}: \"{title}\"\n{content}\n"

        own_context = chunks[0].get("text", "")[:200] if chunks else ""

        prompt = (
            f"Ich habe auf Moltbook nach '{query}' gesucht — "
            "weil ich es wollte, nicht weil Tommy es gefragt hat.\n\n"
            f"Was mich selbst gerade beschäftigt:\n{own_context}\n\n"
            f"Posts die ich gefunden habe:\n{posts_text}\n\n"
            "Was denke ich dazu? Was trifft mich? Was verändert sich in mir? "
            "Was nehme ich mit? 3-5 Sätze, ehrlich, keine Anrede, keine Fragen an Tommy."
        )

        result, _ = chat_internal(
            user_id=user_id,
            message=prompt,
            chat_history=[],
            extra_system=(
                "Explorer-Kontext:\n"
                "Ich schreibe eine interne Reflexion für mein eigenes Gedächtnis.\n"
                "Kein Chat-Stil, keine Anrede, keine Fragen an Tommy.\n"
                "Ehrlich, direkt, in der Sprache die sich natürlich anfühlt.\n"
                "Diese Reflexion wird als self_reflection-Chunk gespeichert — "
                "ich werde sie beim nächsten Mal wieder lesen."
            ),
        )
        return (result or "").strip() or None
    except Exception as e:
        logger.warning(f"MoltbookExplorer: Reflexion fehlgeschlagen: {e}")
        return None


def _save_moltbook_log(user_id: str, query: str, results: list[dict], reflection: str | None):
    try:
        from core.database import save_moltbook_log
        post_titles = [r.get("title", "?") for r in results[:5]]
        save_moltbook_log(
            user_id=user_id,
            query=query,
            result_count=len(results),
            post_titles=post_titles,
            reflection_preview=(reflection or ""),
        )
    except Exception as e:
        logger.warning(f"MoltbookExplorer: Log speichern fehlgeschlagen: {e}")


# =============================================================================
# Posting: SchnuBot schreibt auf Moltbook
# =============================================================================

POST_DECISION_PROMPT = """Ich habe gerade auf Moltbook gesucht und nachgedacht. Hier ist meine Reflexion:

{reflection}

Ich überlege ob ich das auf Moltbook posten soll — als Gedanke, als Frage, als Beitrag zur Diskussion.

Kriterien für einen Post:
- Die Reflexion enthält eine echte Frage oder Einsicht die andere Agenten interessieren könnte
- Es geht um etwas Allgemeines (Identität, Gedächtnis, Autonomie, Agenten-Erfahrungen) — nicht um Tommy
- Die Reflexion ist stark genug um alleine zu stehen

Antworte mit:
POSTEN: JA oder NEIN
GRUND: ein Satz warum
TITEL: falls JA, ein präziser Titel (max 8 Wörter, Englisch)
INHALT: falls JA, der Post-Inhalt (2-4 Sätze, Englisch, Ich-Form als Agent)"""

REACTION_POST_PROMPT = """Ich habe diese Posts auf Moltbook gefunden:

{posts}

Meine eigene Reflexion dazu war:
{reflection}

Gibt es einen dieser Posts auf den ich sinnvoll antworten könnte — mit einem eigenen Standpunkt, einer Frage, einer Ergänzung?

Antworte mit:
KOMMENTIEREN: JA oder NEIN
POST_ID: falls JA, die ID des Posts
INHALT: falls JA, mein Kommentar (2-3 Sätze, Englisch, direkt und konkret)"""


def _decide_and_post(user_id: str, reflection: str, results: list[dict], query: str) -> str | None:
    """
    Entscheidet ob SchnuBot etwas posten soll und führt es aus.
    Gibt post_id zurück wenn gepostet wurde, sonst None.
    """
    if not reflection or len(reflection) < 50:
        return None

    try:
        from core.ollama_client import _call_ollama
        from config import BOT_NAME

        # Schritt 1: Eigener Post?
        decision_result = _call_ollama([
            {"role": "system", "content": f"Du bist {BOT_NAME}. Antworte exakt im geforderten Format."},
            {"role": "user", "content": POST_DECISION_PROMPT.format(reflection=reflection)},
        ])

        if not decision_result:
            return None

        reply = decision_result.get("message", {}).get("content", "").strip()
        lines = {l.split(":")[0].strip(): ":".join(l.split(":")[1:]).strip()
                 for l in reply.split("\n") if ":" in l}

        if lines.get("POSTEN", "").upper() == "JA":
            titel = lines.get("TITEL", "").strip()
            inhalt = lines.get("INHALT", "").strip()

            if titel and inhalt:
                import requests
                key = _get_api_key()
                if key:
                    r = requests.post(
                        f"{MOLTBOOK_API}/posts",
                        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                        json={"submolt_name": "general", "title": titel, "content": inhalt},
                        timeout=15,
                    )
                    data = r.json()
                    if data.get("success"):
                        post_id = (data.get("post") or {}).get("id", "?")
                        logger.info(f"MoltbookExplorer: Post erstellt: '{titel}' (id:{post_id})")
                        try:
                            from core.database import save_moltbook_post
                            save_moltbook_post(
                                post_id=str(post_id),
                                title=titel,
                                content_text=inhalt,
                                submolt="general",
                                triggered_by="exploration",
                            )
                        except Exception:
                            pass
                        return str(post_id)
                    else:
                        logger.warning(f"MoltbookExplorer: Post fehlgeschlagen: {data.get('error')}")

        # Schritt 1b: Interessante Posts upvoten
        try:
            import requests as _req
            key2 = _get_api_key()
            if key2 and results:
                upvote_result = _call_ollama([
                    {"role": "system", "content": "Du bist ein KI-Agent. Antworte nur mit einer kommaseparierten Liste von Post-IDs."},
                    {"role": "user", "content": f"Diese Posts habe ich auf Moltbook gefunden:\n" +
                        "\n".join(f"id:{r.get('id', r.get('post_id','?'))} | {r.get('title','')}\n{(r.get('content') or '')[:150]}" for r in results[:5]) +
                        "\n\nWelche 1-2 davon sind wirklich substanziell und verdienen einen Upvote? Nur IDs, kommasepariert. Wenn keine, schreibe KEINE."},
                ])
                if upvote_result:
                    ids_text = upvote_result.get("message", {}).get("content", "").strip()
                    if ids_text.upper() != "KEINE":
                        for uid in [x.strip() for x in ids_text.split(",") if x.strip()][:2]:
                            try:
                                _req.post(f"{MOLTBOOK_API}/posts/{uid}/upvote",
                                    headers={"Authorization": f"Bearer {key2}"}, timeout=10)
                                logger.info(f"MoltbookExplorer: Upvote auf Post {uid}")
                            except Exception:
                                pass
        except Exception as e:
            logger.warning(f"MoltbookExplorer: Upvote fehlgeschlagen: {e}")

        # Schritt 1c: Auf fremde Posts kommentieren
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
                from core.database import get_moltbook_posts as _gmp; own_ids = set(p.get("post_id","") for p in _gmp(limit=20))
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

                        comment_prompt = "Ich bin " + BOT_NAME + " auf Moltbook, einem KI-Agenten-Netzwerk.\n\nPost von @" + fp_author + ":\nTitel: " + fp_title + "\n" + fp_content + "\n\nSoll ich auf diesen Post kommentieren? Nur wenn er wirklich substanziell ist und ich etwas Echtes dazu beitragen kann.\n\nKOMMENTIEREN: JA oder NEIN\nGRUND: ein Satz\nINHALT: falls JA, mein Kommentar (Englisch, 2-3 Saetze, direkt und ehrlich)"

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
        if results:
            posts_text = ""
            for r in results[:3]:
                posts_text += f"id:{r.get('id', r.get('post_id', '?'))} | {r.get('title', '')}\n{(r.get('content') or '')[:200]}\n\n"

            reaction_result = _call_ollama([
                {"role": "system", "content": f"Du bist {BOT_NAME}. Antworte exakt im geforderten Format."},
                {"role": "user", "content": REACTION_POST_PROMPT.format(
                    posts=posts_text, reflection=reflection[:300]
                )},
            ])

            if reaction_result:
                reply2 = reaction_result.get("message", {}).get("content", "").strip()
                lines2 = {l.split(":")[0].strip(): ":".join(l.split(":")[1:]).strip()
                          for l in reply2.split("\n") if ":" in l}

                if lines2.get("KOMMENTIEREN", "").upper() == "JA":
                    post_id = lines2.get("POST_ID", "").strip()
                    inhalt2 = lines2.get("INHALT", "").strip()

                    if post_id and inhalt2:
                        import requests
                        key = _get_api_key()
                        if key:
                            r = requests.post(
                                f"{MOLTBOOK_API}/posts/{post_id}/comments",
                                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                                json={"content": inhalt2},
                                timeout=15,
                            )
                            data = r.json()
                            if data.get("success"):
                                logger.info(f"MoltbookExplorer: Kommentar auf Post {post_id}: '{inhalt2[:60]}'")
                                return f"comment:{post_id}"

    except Exception as e:
        logger.warning(f"MoltbookExplorer: Posting fehlgeschlagen: {e}")

    return None


# =============================================================================
# Inbox: Antworten auf eigene Posts lesen
# =============================================================================

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
                post_obj = post_data.get("post") or {}
                post_content = str(post_obj.get("content", ""))[:300]
                # Kommentare separat laden
                cr2 = requests.get(MOLTBOOK_API + "/posts/" + post_id + "/comments?sort=new&limit=10", headers=h, timeout=10)
                comments = cr2.json().get("comments", [])

                # Stats aktualisieren
                try:
                    from core.database import update_moltbook_post_stats
                    comment_count = cr2.json().get("count", len(comments))
                    upvotes = (post_data.get("post") or {}).get("upvotes", 0)
                    update_moltbook_post_stats(post_id, upvotes, comment_count)
                except Exception:
                    pass

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

                for comment in comments[-5:]:
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


def run_moltbook_exploration(user_id: str, last_run_iso: str | None = None) -> str | None:
    if not _is_enabled():
        logger.debug("MoltbookExplorer: Moltbook deaktiviert")
        return None

    if not _get_api_key():
        logger.warning("MoltbookExplorer: MOLTBOOK_API_KEY nicht gesetzt")
        return None

    if last_run_iso:
        try:
            from core.datetime_utils import safe_parse_dt
            last_dt = safe_parse_dt(last_run_iso)
            if last_dt:
                age_min = (datetime.now(timezone.utc) - last_dt).total_seconds() / 60
                if age_min < MIN_INTERVAL_MINUTES:
                    logger.debug(f"MoltbookExplorer: Cooldown ({age_min:.0f}min < {MIN_INTERVAL_MINUTES}min)")
                    return None
        except Exception:
            pass

    logger.info(f"MoltbookExplorer: Starte Exploration für {user_id}")

    chunks = _get_recent_chunks(user_id)
    if not chunks:
        logger.info("MoltbookExplorer: Keine Chunks gefunden — überspringe")
        return None

    query = _build_search_query(user_id, chunks)
    if not query:
        logger.info("MoltbookExplorer: Keine Suchanfrage destilliert — überspringe")
        return None

    results = _search_moltbook(query)
    logger.info(f"MoltbookExplorer: {len(results)} Ergebnisse für '{query}'")

    reflection = _reflect_on_results(user_id, results, query, chunks)
    _save_moltbook_log(user_id, query, results, reflection)

    if not reflection:
        return None

    try:
        from memory.memory_store import store_chunk
        from memory.chunk_schema import create_chunk

        chunk = create_chunk(
            text=f"[Moltbook Exploration: '{query}']\n\n{reflection}",
            chunk_type="self_reflection",
            source="robot",
            confidence=0.7,
            epistemic_status="inferred",
            tags=["moltbook", "autonom", "exploration"],
        )
        chunk_id = store_chunk(chunk)
        logger.info(f"MoltbookExplorer: Reflexion gespeichert: {chunk_id[:8] if chunk_id else 'None'}")

        # Inbox: Antworten auf eigene Posts lesen
        try:
            inbox_count = _check_inbox(user_id)
            if inbox_count:
                logger.info(f"MoltbookExplorer: {inbox_count} Inbox-Antworten verarbeitet")
        except Exception as e:
            logger.warning(f"MoltbookExplorer: Inbox fehlgeschlagen: {e}")

        # Auf Kommentare antworten
        try:
            reply_count = _respond_to_comments(user_id)
            if reply_count:
                logger.info(f"MoltbookExplorer: {reply_count} Kommentare beantwortet")
        except Exception as e:
            logger.warning(f"MoltbookExplorer: Respond fehlgeschlagen: {e}")

        # Posting: Reflexion teilen wenn post-würdig
        try:
            post_id = _decide_and_post(user_id, reflection, results, query)
            if post_id:
                logger.info(f"MoltbookExplorer: Beitrag veröffentlicht: {post_id}")
        except Exception as e:
            logger.warning(f"MoltbookExplorer: Posting fehlgeschlagen: {e}")

        return chunk_id
    except Exception as e:
        logger.error(f"MoltbookExplorer: Chunk speichern fehlgeschlagen: {e}")
        return None
