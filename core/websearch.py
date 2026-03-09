"""
core/websearch.py — Web Search via Tavily

Zentrale Schnittstelle fuer alle Web-Suchen in SchnuBot.
Kimi ruft search() auf, bekommt aufbereiteten Text zurueck.

Tavily gibt LLM-optimierte Antworten zurueck — kein Link-Parsing noetig.
"""

import logging
import os
import time

import requests

logger = logging.getLogger(__name__)

TAVILY_API_URL = "https://api.tavily.com/search"
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "")

# Standardeinstellungen
DEFAULT_MAX_RESULTS = 5       # Anzahl Quellen die Tavily auswertet
DEFAULT_SEARCH_DEPTH = "basic"  # "basic" (schnell) oder "advanced" (gruendlicher, mehr Credits)
MAX_ANSWER_CHARS = 2000       # Maximale Zeichenlaenge der aufbereiteten Antwort


def search(query: str, max_results: int = DEFAULT_MAX_RESULTS, depth: str = DEFAULT_SEARCH_DEPTH) -> dict:
    """
    Fuehrt eine Web-Suche via Tavily durch.

    Args:
        query:       Suchanfrage als natuerlichsprachiger String.
        max_results: Anzahl Quellen (1–10).
        depth:       "basic" oder "advanced".

    Returns:
        dict mit:
            success (bool)
            answer  (str)  — aufbereiteter Text fuer Kimi
            sources (list) — Liste von {"title", "url"} fuer Transparenz
            query   (str)  — die tatsaechlich gesendete Anfrage
            error   (str)  — nur bei success=False
    """
    if not TAVILY_API_KEY:
        logger.error("TAVILY_API_KEY nicht gesetzt — Web Search nicht verfuegbar")
        return _error("Web Search ist nicht konfiguriert (kein API-Key).")

    if not query or not query.strip():
        return _error("Leere Suchanfrage.")

    query = query.strip()
    logger.info(f"Web Search: '{query}' (depth={depth}, max_results={max_results})")

    t_start = time.time()
    try:
        response = requests.post(
            TAVILY_API_URL,
            json={
                "api_key": TAVILY_API_KEY,
                "query": query,
                "search_depth": depth,
                "max_results": max_results,
                "include_answer": True,       # Tavily-Zusammenfassung
                "include_raw_content": False,
                "include_images": False,
            },
            timeout=20,
        )
        response.raise_for_status()
        data = response.json()

    except requests.Timeout:
        logger.warning(f"Web Search Timeout fuer '{query}'")
        return _error("Die Suche hat zu lange gedauert (Timeout). Versuch es nochmal.")
    except requests.HTTPError as e:
        logger.error(f"Tavily HTTP-Fehler: {e}")
        return _error(f"Suche fehlgeschlagen (HTTP {response.status_code}).")
    except Exception as e:
        logger.error(f"Web Search unerwarteter Fehler: {e}")
        return _error("Unbekannter Fehler bei der Web-Suche.")

    elapsed = time.time() - t_start
    logger.info(f"Web Search abgeschlossen in {elapsed:.2f}s")

    # Antwort aufbereiten
    answer = _build_answer(data, query)
    sources = _extract_sources(data)

    return {
        "success": True,
        "answer": answer,
        "sources": sources,
        "query": query,
    }


def format_for_kimi(result: dict) -> str:
    """
    Formatiert ein search()-Ergebnis als Kontext-String fuer den Kimi-Prompt.
    Kimi bekommt die Antwort + Quellen als lesbaren Block.
    """
    if not result.get("success"):
        return f"[Web Search fehlgeschlagen: {result.get('error', 'Unbekannter Fehler')}]"

    lines = [
        f"[Web Search: \"{result['query']}\"]",
        "",
        result["answer"],
    ]

    if result.get("sources"):
        lines.append("")
        lines.append("Quellen:")
        for s in result["sources"][:3]:  # max 3 Quellen im Prompt
            lines.append(f"- {s['title']}: {s['url']}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Interne Hilfsfunktionen
# ---------------------------------------------------------------------------

def _build_answer(data: dict, query: str) -> str:
    """Baut aus der Tavily-Antwort einen sauberen Text fuer Kimi."""
    parts = []

    # Tavily-Direktantwort (beste Quelle)
    tavily_answer = data.get("answer", "").strip()
    if tavily_answer:
        parts.append(tavily_answer)

    # Ergaenzend: relevante Snippets aus den Ergebnissen
    results = data.get("results", [])
    for r in results[:3]:
        content = r.get("content", "").strip()
        if content and content not in tavily_answer:
            parts.append(content)

    answer = "\n\n".join(parts)

    # Kuerzen falls zu lang
    if len(answer) > MAX_ANSWER_CHARS:
        answer = answer[:MAX_ANSWER_CHARS] + "…"

    return answer if answer else "Keine verwertbaren Ergebnisse gefunden."


def _extract_sources(data: dict) -> list:
    """Extrahiert Quellen-Metadaten aus der Tavily-Antwort."""
    sources = []
    for r in data.get("results", []):
        title = r.get("title", "").strip()
        url = r.get("url", "").strip()
        if url:
            sources.append({"title": title or url, "url": url})
    return sources


def _error(message: str) -> dict:
    return {"success": False, "answer": "", "sources": [], "query": "", "error": message}
