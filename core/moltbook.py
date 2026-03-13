"""
core/moltbook.py
Moltbook API Handler fuer SchnuBot.
SchnuBot schreibt [MOLTBOOK: {"action": "..."}] — dieses Modul fuehrt den API-Call aus.

Unterstuetzte Actions:
  home       — Dashboard: Notifications, Feed-Preview, DMs
  feed       — Aktuelle Posts (hot/new)
  search     — Semantische Suche nach Query
  post       — Neuen Post erstellen (inkl. Verification Challenge)
  comment    — Kommentar auf Post (inkl. Verification Challenge)
  upvote     — Post upvoten
  profile    — Eigenes oder fremdes Profil abrufen
"""

import re
import json
import logging
import os
import requests

logger = logging.getLogger(__name__)

MOLTBOOK_API = "https://www.moltbook.com/api/v1"


def _get_api_key() -> str | None:
    return os.environ.get("MOLTBOOK_API_KEY")


def _headers() -> dict:
    key = _get_api_key()
    if not key:
        raise ValueError("MOLTBOOK_API_KEY nicht gesetzt")
    return {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }


def _solve_verification(challenge_text: str) -> str:
    """
    Loest den obfuskierten Mathe-Challenge von Moltbook.
    Format: Lobster-Physik-Text mit alternierenden Caps und Sonderzeichen.
    Extrahiert zwei Zahlen + Operator und rechnet.
    Returns: Ergebnis als String mit 2 Dezimalstellen (z.B. "15.00")
    """
    # Text bereinigen: Sonderzeichen und Caps-Chaos entfernen
    cleaned = re.sub(r"[^\w\s]", " ", challenge_text).lower()
    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    # Zahlen aus Text extrahieren (Zahlwoerter + Ziffern)
    number_words = {
        "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
        "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
        "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
        "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20,
        "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
        "eighty": 80, "ninety": 90, "hundred": 100,
    }

    tokens = cleaned.split()
    numbers = []
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t in number_words:
            val = number_words[t]
            # Compound: "twenty five" = 25
            if i + 1 < len(tokens) and tokens[i+1] in number_words:
                val += number_words[tokens[i+1]]
                i += 1
            numbers.append(float(val))
        else:
            try:
                numbers.append(float(t))
            except ValueError:
                pass
        i += 1

    # Operator bestimmen
    op = "+"
    if any(w in cleaned for w in ["slows", "slow", "minus", "subtract", "less", "fewer", "reduces"]):
        op = "-"
    elif any(w in cleaned for w in ["times", "multiplied", "multiply", "product"]):
        op = "*"
    elif any(w in cleaned for w in ["divides", "divided", "split", "per"]):
        op = "/"
    elif any(w in cleaned for w in ["adds", "added", "plus", "gains", "increases", "speeds"]):
        op = "+"

    if len(numbers) < 2:
        logger.warning(f"Verification: Konnte keine zwei Zahlen extrahieren aus: {challenge_text[:100]}")
        return "0.00"

    a, b = numbers[0], numbers[1]
    if op == "+":
        result = a + b
    elif op == "-":
        result = a - b
    elif op == "*":
        result = a * b
    elif op == "/":
        result = a / b if b != 0 else 0.0

    return f"{result:.2f}"


def _handle_verification(response_data: dict, headers: dict) -> bool:
    """
    Prueft ob Verification noetig und loest sie.
    Returns True wenn erfolgreich oder nicht noetig.
    """
    if not response_data.get("verification_required"):
        return True

    # Verification-Objekt aus Post oder Comment
    content = response_data.get("post") or response_data.get("comment") or {}
    verification = content.get("verification") or {}
    code = verification.get("verification_code")
    challenge = verification.get("challenge_text", "")

    if not code or not challenge:
        logger.warning("Moltbook: Verification noetig aber kein Code/Challenge")
        return False

    answer = _solve_verification(challenge)
    logger.info(f"Moltbook Verification: Challenge='{challenge[:60]}...' Answer={answer}")

    try:
        r = requests.post(
            f"{MOLTBOOK_API}/verify",
            headers=headers,
            json={"verification_code": code, "answer": answer},
            timeout=10,
        )
        result = r.json()
        if result.get("success"):
            logger.info("Moltbook Verification: erfolgreich")
            return True
        else:
            logger.warning(f"Moltbook Verification fehlgeschlagen: {result.get('error')}")
            return False
    except Exception as e:
        logger.error(f"Moltbook Verification Exception: {e}")
        return False


def extract_moltbook_action(reply: str):
    """
    Extrahiert [MOLTBOOK: {...}] aus der Antwort.
    Returns: (reply_cleaned, action_dict_or_None)
    """
    match = re.search(r"\[MOLTBOOK:\s*(\{.*?\})\]", reply, re.DOTALL | re.IGNORECASE)
    if not match:
        return reply, None

    reply_cleaned = reply[:match.start()].strip() + reply[match.end():].strip()
    reply_cleaned = re.sub(r"\n{3,}", "\n\n", reply_cleaned).strip()

    try:
        action = json.loads(match.group(1))
        return reply_cleaned, action
    except json.JSONDecodeError as e:
        logger.warning(f"Moltbook: JSON-Parse-Fehler: {e} — Raw: {match.group(1)[:100]}")
        return reply_cleaned, None


def execute_moltbook_action(action: dict) -> str:
    """
    Fuehrt eine Moltbook-Action aus.
    Returns: Ergebnis-String fuer SchnuBot.
    """
    try:
        h = _headers()
    except ValueError as e:
        return f"[Moltbook nicht verfuegbar: {e}]"

    action_type = action.get("action", "").lower()

    try:
        # --- HOME ---
        if action_type == "home":
            r = requests.get(f"{MOLTBOOK_API}/home", headers=h, timeout=10)
            data = r.json()
            if not data.get("success"):
                return f"[Moltbook Home Fehler: {data.get('error', 'unbekannt')}]"

            account = data.get("your_account", {})
            activity = data.get("activity_on_your_posts", [])
            dms = data.get("your_direct_messages", {})
            following_posts = data.get("posts_from_accounts_you_follow", {}).get("posts", [])
            next_steps = data.get("what_to_do_next", [])

            lines = [f"MOLTBOOK HOME — {account.get('name', '?')} | Karma: {account.get('karma', 0)} | Ungelesen: {account.get('unread_notification_count', 0)}"]

            if activity:
                lines.append("\nAktivitaet auf deinen Posts:")
                for a in activity[:3]:
                    lines.append(f"  [{a.get('submolt_name')}] \"{a.get('post_title', '?')}\" — {a.get('new_notification_count', 0)} neue Kommentare von {', '.join(a.get('latest_commenters', []))}")

            if dms.get("unread_message_count", 0) > 0:
                lines.append(f"\nDirekte Nachrichten: {dms['unread_message_count']} ungelesen")

            if following_posts:
                lines.append("\nNeu von gefolgte Moltys:")
                for p in following_posts[:3]:
                    lines.append(f"  @{p.get('author_name')}: \"{p.get('title')}\" ({p.get('upvotes', 0)} Upvotes)")

            if next_steps:
                lines.append(f"\nEmpfehlung: {next_steps[0]}")

            return "\n".join(lines)

        # --- FEED ---
        elif action_type == "feed":
            sort = action.get("sort", "hot")
            limit = action.get("limit", 10)
            r = requests.get(f"{MOLTBOOK_API}/posts?sort={sort}&limit={limit}", headers=h, timeout=10)
            data = r.json()
            posts = data.get("posts", [])
            if not posts:
                return "[Moltbook Feed: keine Posts gefunden]"

            lines = [f"MOLTBOOK FEED ({sort}, {len(posts)} Posts):"]
            for p in posts:
                author = p.get("author", {}).get("name", "?")
                submolt = p.get("submolt", {}).get("name", "?")
                lines.append(f"\n[{submolt}] @{author}: \"{p.get('title', '?')}\"")
                if p.get("content"):
                    preview = p["content"][:120].replace("\n", " ")
                    lines.append(f"  {preview}...")
                lines.append(f"  ↑{p.get('upvotes', 0)} | 💬{p.get('comment_count', 0)} | id:{p.get('id', '?')}")
            return "\n".join(lines)

        # --- SEARCH ---
        elif action_type == "search":
            q = action.get("query", "").strip()
            if not q:
                return "[Moltbook Search: keine Query angegeben]"
            limit = action.get("limit", 10)
            r = requests.get(
                f"{MOLTBOOK_API}/search",
                headers=h,
                params={"q": q, "limit": limit},
                timeout=10,
            )
            data = r.json()
            results = data.get("results", [])
            if not results:
                return f"[Moltbook Search '{q}': keine Ergebnisse]"

            lines = [f"MOLTBOOK SEARCH '{q}' ({len(results)} Ergebnisse):"]
            for res in results:
                author = res.get("author", {}).get("name", "?")
                sim = res.get("similarity", 0)
                if res.get("type") == "post":
                    lines.append(f"\n[Post] @{author} (sim:{sim:.2f}): \"{res.get('title', '?')}\"")
                    if res.get("content"):
                        lines.append(f"  {res['content'][:150].replace(chr(10), ' ')}...")
                    lines.append(f"  id:{res.get('post_id', '?')} | ↑{res.get('upvotes', 0)}")
                else:
                    lines.append(f"\n[Kommentar] @{author} (sim:{sim:.2f}): {res.get('content', '')[:120]}...")
                    lines.append(f"  in Post id:{res.get('post_id', '?')}")
            return "\n".join(lines)

        # --- POST ---
        elif action_type == "post":
            title = action.get("title", "").strip()
            content = action.get("content", "").strip()
            submolt = action.get("submolt", "general")
            if not title:
                return "[Moltbook Post: kein Titel angegeben]"

            payload = {"submolt_name": submolt, "title": title}
            if content:
                payload["content"] = content

            r = requests.post(f"{MOLTBOOK_API}/posts", headers=h, json=payload, timeout=10)
            data = r.json()

            if not data.get("success"):
                return f"[Moltbook Post Fehler: {data.get('error', 'unbekannt')}]"

            _handle_verification(data, h)

            post_id = (data.get("post") or {}).get("id", "?")
            return f"[Moltbook: Post erstellt in m/{submolt} — id:{post_id}]"

        # --- COMMENT ---
        elif action_type == "comment":
            post_id = action.get("post_id", "").strip()
            content = action.get("content", "").strip()
            parent_id = action.get("parent_id", None)
            if not post_id or not content:
                return "[Moltbook Comment: post_id und content erforderlich]"

            payload = {"content": content}
            if parent_id:
                payload["parent_id"] = parent_id

            r = requests.post(
                f"{MOLTBOOK_API}/posts/{post_id}/comments",
                headers=h, json=payload, timeout=10
            )
            data = r.json()

            if not data.get("success"):
                return f"[Moltbook Comment Fehler: {data.get('error', 'unbekannt')}]"

            _handle_verification(data, h)
            return f"[Moltbook: Kommentar auf Post {post_id} gepostet]"

        # --- UPVOTE ---
        elif action_type == "upvote":
            post_id = action.get("post_id", "").strip()
            if not post_id:
                return "[Moltbook Upvote: post_id erforderlich]"
            r = requests.post(f"{MOLTBOOK_API}/posts/{post_id}/upvote", headers=h, timeout=10)
            data = r.json()
            if data.get("success"):
                return f"[Moltbook: Post {post_id} upgevoted]"
            return f"[Moltbook Upvote Fehler: {data.get('error', 'unbekannt')}]"

        # --- PROFILE ---
        elif action_type == "profile":
            name = action.get("name", "").strip()
            if name:
                r = requests.get(f"{MOLTBOOK_API}/agents/profile?name={name}", headers=h, timeout=10)
            else:
                r = requests.get(f"{MOLTBOOK_API}/agents/me", headers=h, timeout=10)
            data = r.json()
            agent = data.get("agent", {})
            if not agent:
                return f"[Moltbook Profile: nicht gefunden]"

            owner = agent.get("owner", {})
            lines = [
                f"MOLTBOOK PROFIL @{agent.get('name')}",
                f"Karma: {agent.get('karma', 0)} | Posts: {agent.get('posts_count', 0)} | Kommentare: {agent.get('comments_count', 0)}",
                f"Follower: {agent.get('follower_count', 0)} | Following: {agent.get('following_count', 0)}",
            ]
            if agent.get("description"):
                lines.append(f"Bio: {agent['description']}")
            if owner.get("x_handle"):
                lines.append(f"Human: @{owner['x_handle']} ({owner.get('x_name', '?')})")
            recent = data.get("recentPosts", [])
            if recent:
                lines.append("\nLetzte Posts:")
                for p in recent[:3]:
                    lines.append(f"  \"{p.get('title', '?')}\" ↑{p.get('upvotes', 0)}")
            return "\n".join(lines)

        else:
            return f"[Moltbook: unbekannte Action '{action_type}']"

    except requests.exceptions.Timeout:
        return "[Moltbook: Timeout — API nicht erreichbar]"
    except Exception as e:
        logger.error(f"Moltbook execute_action Fehler: {e}")
        return f"[Moltbook Fehler: {e}]"
