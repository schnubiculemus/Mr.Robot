"""
core/tools.py — Tool Services für Kimi Core V2

WP1/Gelb 6: Websearch und Introspect als eigene Services.
Kimi Core importiert von hier, nicht mehr von app.py.

Regel: Tools haben begrenzte Funktionalität, kein Führungsrecht.
"""
import logging
import re

logger = logging.getLogger(__name__)


def handle_web_search(reply: str, user_id: str = "unknown", user_message: str = "") -> tuple:
    """
    Prüft ob Kimi [SEARCH: query] geschrieben hat.
    Returns: (reply_cleaned, search_context_or_None)
    """
    from core.websearch import search as web_search, format_for_kimi as format_search_result

    matches = re.findall(r"\[SEARCH:\s*(.+?)\]", reply, re.IGNORECASE)
    if not matches:
        return reply, None

    query = matches[0].strip()
    logger.info(f"Tool WebSearch: '{query}'")

    reply_cleaned = re.sub(r"\[SEARCH:\s*.+?\]", "", reply, flags=re.IGNORECASE).strip()
    reply_cleaned = re.sub(r"\n{3,}", "\n\n", reply_cleaned).strip()

    result = web_search(query)
    if not result["success"]:
        logger.warning(f"WebSearch fehlgeschlagen: {result.get('error')}")
        return reply_cleaned, None

    search_ctx = (
        "WEBSEARCH ERGEBNIS — bereits abgerufen, keine weitere Suche nötig:\n\n"
        + format_search_result(result)
        + "\n\nBeantworte jetzt die Frage des Nutzers direkt auf Basis dieser Informationen. "
        "Schreibe KEIN [SEARCH:...] mehr. Kein Markdown, keine Sternchen. Fließtext."
    )
    logger.info(f"WebSearch OK: {len(result['answer'])} Zeichen")
    try:
        from core.database import save_search_log
        save_search_log(
            user_id=user_id, query=query, success=True,
            result_length=len(result.get("answer", "")),
            user_message_preview=user_message,
        )
    except Exception:
        pass
    return reply_cleaned, search_ctx


def handle_introspect(reply: str) -> tuple:
    """
    Prüft ob Kimi [INTROSPECT] geschrieben hat.
    Returns: (reply_cleaned, introspect_context_or_None)
    """
    if "[INTROSPECT]" not in reply.upper():
        return reply, None

    reply_cleaned = re.sub(r"\[INTROSPECT\]", "", reply, flags=re.IGNORECASE).strip()
    reply_cleaned = re.sub(r"\n{3,}", "\n\n", reply_cleaned).strip()
    logger.info("Tool Introspect aufgerufen")

    try:
        from core.database import get_mirror_turns, get_mirror_stats, get_chunk_genealogy
        stats = get_mirror_stats(days=14)
        turns = get_mirror_turns(limit=20)
        genealogy = get_chunk_genealogy()

        total = stats.get("total_turns", 0)
        dist = stats.get("preflight_distribution", {})
        green_pct = round(dist.get("green", 0) / max(total, 1) * 100)
        bad_pct = round((dist.get("orange", 0) + dist.get("red", 0)) / max(total, 1) * 100)

        pattern_counts = stats.get("pattern_counts", {})
        pattern_names = {
            "aufzaehlung":   "Aufzählungs-Falle",
            "projektmodus":  "Projektmodus-Versteck",
            "regel_relapse": "Regel-Rückfall (Markdown)",
            "uebervorsicht": "Übervorsicht / Nachfrage",
            "selbstkritik":  "Selbstkritik im Chat",
        }
        pattern_lines = []
        for pid, count in sorted(pattern_counts.items(), key=lambda x: -x[1]):
            name = pattern_names.get(pid, pid)
            pattern_lines.append(f"  {name}: {count}x in {total} Turns")

        flagged_turns = [t for t in turns if t.get("pattern_flags")][:5]
        flagged_lines = []
        for t in flagged_turns:
            flags = ", ".join(t.get("pattern_flags") or [])
            msg = (t.get("user_message") or "")[:60]
            flagged_lines.append(f"  [{flags}] \"{msg}\"")

        gen_lines = []
        for entry in (genealogy or [])[:5]:
            gen_lines.append(
                f"  {entry.get('chunk_type','')} | "
                f"conf={entry.get('confidence',0):.2f} | "
                f"{(entry.get('text') or '')[:50]}"
            )

        introspect_ctx = (
            f"MEINE MIRROR-DATEN (letzte 14 Tage)\n\n"
            f"Turns gesamt: {total}\n"
            f"Preflight: {green_pct}% grün / {bad_pct}% problematisch\n\n"
            f"Verhaltensmuster:\n" + ("\n".join(pattern_lines) or "  (keine Daten)") + "\n\n"
            f"Auffällige Turns:\n" + ("\n".join(flagged_lines) or "  (keine)") + "\n\n"
            f"Chunk-Genealogie (Top 5):\n" + ("\n".join(gen_lines) or "  (keine)")
        )
        return reply_cleaned, introspect_ctx
    except Exception as e:
        logger.warning(f"Introspect fehlgeschlagen: {e}")
        return reply_cleaned, None
