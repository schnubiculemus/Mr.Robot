"""
SchnuBot.ai - Kimis Introspektions-Engine (Phase 1)

Gibt Kimi Zugang zu ihren eigenen akkumulierten self_reflection-Chunks —
als strukturierten "Rückspiegel" der in autonomous_reflection eingebunden wird.

Phase 1: Passiv — wird von autonomous_reflection und heartbeat aufgerufen.
Phase 2 (nach Tasksystem 2.0): Kimi kann das aktiv als Tool aufrufen.

Funktionen:
- get_self_reflection_summary()  → kompakter Text für Prompts
- get_confidence_trend()         → Trend der Confidence-Werte über Zeit
- get_recent_reflections()       → rohe Chunk-Liste für direkte Verarbeitung
"""

import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Wie viele Chunks maximal laden
MAX_CHUNKS = 20
# Wie viele Chunks im Summary-Text anzeigen
SUMMARY_CHUNKS = 8
# Mindest-Confidence für "starke" Reflexionen
STRONG_CONFIDENCE = 0.7


# =============================================================================
# Chunks laden
# =============================================================================

def get_recent_reflections(limit: int = MAX_CHUNKS) -> list[dict]:
    """
    Lädt die neuesten aktiven self_reflection-Chunks von Kimi selbst.
    Sortiert: neueste zuerst.
    """
    try:
        from memory.memory_store import get_active_collection
        col = get_active_collection()

        result = col.get(
            where={"$and": [
                {"source": "robot"},
                {"status": "active"},
                {"chunk_type": "self_reflection"},
            ]},
            include=["documents", "metadatas"],
        )

        if not result["ids"]:
            return []

        chunks = []
        for i, chunk_id in enumerate(result["ids"]):
            meta = result["metadatas"][i]
            chunks.append({
                "id": chunk_id,
                "text": result["documents"][i],
                "created_at": meta.get("created_at", ""),
                "confidence": float(meta.get("confidence", 0.5)),
                "epistemic_status": meta.get("epistemic_status", "inferred"),
                "tags": [t.strip() for t in str(meta.get("tags", "")).split(",") if t.strip()],
                "replies_to": meta.get("replies_to", ""),
            })

        # Neueste zuerst
        chunks.sort(key=lambda c: c.get("created_at", ""), reverse=True)
        return chunks[:limit]

    except Exception as e:
        logger.warning(f"SelfReflectionSummary: Laden fehlgeschlagen: {e}")
        return []


# =============================================================================
# Confidence-Trend
# =============================================================================

def get_confidence_trend(chunks: list[dict] = None) -> dict:
    """
    Berechnet Confidence-Trend über die letzten Reflexionen.

    Returns dict mit:
        - avg_recent: Durchschnitt der letzten 5 Chunks
        - avg_older:  Durchschnitt der Chunks 6-15
        - trend:      'steigend' | 'fallend' | 'stabil'
        - delta:      float (positiv = besser)
        - strong_count: Anzahl Chunks mit confidence >= STRONG_CONFIDENCE
    """
    if chunks is None:
        chunks = get_recent_reflections()

    if not chunks:
        return {"avg_recent": 0.5, "avg_older": 0.5, "trend": "stabil", "delta": 0.0, "strong_count": 0}

    recent = chunks[:5]
    older = chunks[5:15]

    avg_recent = sum(c["confidence"] for c in recent) / len(recent) if recent else 0.5
    avg_older = sum(c["confidence"] for c in older) / len(older) if older else avg_recent

    delta = round(avg_recent - avg_older, 3)
    if delta > 0.05:
        trend = "steigend"
    elif delta < -0.05:
        trend = "fallend"
    else:
        trend = "stabil"

    strong_count = sum(1 for c in chunks if c["confidence"] >= STRONG_CONFIDENCE)

    return {
        "avg_recent": round(avg_recent, 3),
        "avg_older": round(avg_older, 3),
        "trend": trend,
        "delta": delta,
        "strong_count": strong_count,
        "total": len(chunks),
    }


# =============================================================================
# Summary-Text für Prompts
# =============================================================================

def get_self_reflection_summary(max_chunks: int = SUMMARY_CHUNKS) -> str | None:
    """
    Gibt einen kompakten, lesbaren Rückspiegel-Text zurück —
    geeignet zur Einbindung in den autonomous_reflection Prompt.

    Format:
        ## Mein Selbstbild (akkumuliert)
        Confidence-Trend: stabil (0.62 → 0.61)
        Starke Überzeugungen: 3

        [vor 2d | Eigene Reflexion | conf:0.71]
        Ich tendiere dazu bei komplexen Fragen...

        ...

    Returns None wenn keine Reflexionen vorhanden.
    """
    chunks = get_recent_reflections(limit=MAX_CHUNKS)
    if not chunks:
        return None

    trend = get_confidence_trend(chunks)
    display_chunks = chunks[:max_chunks]

    lines = [
        "## Mein akkumuliertes Selbstbild",
        f"Confidence-Trend: {trend['trend']} "
        f"(alt: {trend['avg_older']:.2f} → neu: {trend['avg_recent']:.2f} | "
        f"Δ{trend['delta']:+.2f})",
        f"Starke Überzeugungen: {trend['strong_count']} von {trend['total']} Reflexionen",
        "",
    ]

    for chunk in display_chunks:
        # Alter berechnen
        try:
            from core.datetime_utils import safe_age_days
            age_days = safe_age_days(chunk.get("created_at", ""), default=0)
            age_str = "gerade eben" if age_days == 0 else f"vor {age_days}d"
        except Exception:
            age_str = "?"

        # Herkunft aus Tags
        tags = chunk.get("tags", [])
        if "moltbook" in tags:
            origin = "Moltbook"
        elif "introspection" in tags:
            origin = "Introspection"
        elif "inner-dialogue" in tags:
            origin = "Innerer Dialog"
        else:
            origin = "Reflexion"

        conf = chunk["confidence"]
        epist = chunk.get("epistemic_status", "inferred")
        replies = f" → Antwort auf {chunk['replies_to'][:8]}" if chunk.get("replies_to") else ""

        lines.append(
            f"[{age_str} | {origin}{replies} | conf:{conf:.2f} | {epist}]"
        )
        # Text leicht kürzen wenn sehr lang
        text = chunk["text"]
        if len(text) > 300:
            text = text[:297] + "..."
        lines.append(text)
        lines.append("")

    return "\n".join(lines)


# =============================================================================
# Dashboard-API (für spätere Visualisierung)
# =============================================================================

def get_introspection_data() -> dict:
    """
    Aggregierte Daten für das Dashboard.
    Gibt alle relevanten Metriken zurück.
    """
    chunks = get_recent_reflections(limit=MAX_CHUNKS)
    trend = get_confidence_trend(chunks)

    # Tags-Häufigkeit
    tag_counts: dict[str, int] = {}
    for chunk in chunks:
        for tag in chunk.get("tags", []):
            if tag not in ("autonom", "robot", "inner-dialogue"):
                tag_counts[tag] = tag_counts.get(tag, 0) + 1

    top_tags = sorted(tag_counts.items(), key=lambda x: -x[1])[:8]

    # Zeitreihe: Confidence pro Chunk (für Chart)
    timeline = [
        {
            "date": c.get("created_at", "")[:10],
            "confidence": c["confidence"],
            "epistemic": c.get("epistemic_status", "inferred"),
            "preview": c["text"][:80],
        }
        for c in reversed(chunks)  # chronologisch für Chart
    ]

    return {
        "trend": trend,
        "top_tags": top_tags,
        "timeline": timeline,
        "chunks": [
            {
                "id": c["id"],
                "text": c["text"],
                "created_at": c.get("created_at", ""),
                "confidence": c["confidence"],
                "epistemic_status": c.get("epistemic_status", "inferred"),
                "tags": c.get("tags", []),
            }
            for c in chunks
        ],
    }
