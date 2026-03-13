"""
core/moltbook_explorer.py
Autonome Moltbook-Erkundung im Heartbeat.
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
        return {t["id"]: t for t in config}.get("moltbook", {}).get("enabled", False)
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
    """Kimi entscheidet selbst was sie sucht — mit vollem Memory-Kontext."""
    if not chunks:
        return None

    try:
        from core.ollama_client import chat as ollama_chat

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

        internal_history = [
            {
                "role": "user",
                "content": "[INTERNER HEARTBEAT — kein Gespräch mit Tommy. Ich denke für mich selbst.]"
            }
        ]

        result, _ = ollama_chat(
            user_id=user_id,
            message=prompt,
            chat_history=internal_history,
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
    """SchnuBot liest die Posts und schreibt eine Reflexion via Ollama."""
    if not results:
        return None

    try:
        from core.ollama_client import chat as ollama_chat

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
            "3-5 Sätze, kein Markdown, keine Anrede, keine Fragen an Tommy."
        )

        internal_history = [
            {
                "role": "user",
                "content": "[INTERNER HEARTBEAT — kein Gespräch mit Tommy. Ich denke für mich selbst.]"
            }
        ]

        result, _ = ollama_chat(
            user_id=user_id,
            message=prompt,
            chat_history=internal_history,
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
            reflection_preview=(reflection or "")[:200],
        )
    except Exception as e:
        logger.warning(f"MoltbookExplorer: Log speichern fehlgeschlagen: {e}")


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
        return chunk_id
    except Exception as e:
        logger.error(f"MoltbookExplorer: Chunk speichern fehlgeschlagen: {e}")
        return None
