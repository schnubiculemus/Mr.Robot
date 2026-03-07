"""
SchnuBot.ai - Prompt Builder
Referenz: Konzeptdokument V1.1, Abschnitt 9

Formatiert selektierte Memory-Chunks fuer den System-Prompt.
Typweise gruppiert, feste Reihenfolge, self_reflection separiert.
Globale Regeln (Preferences + Decisions) als eigener Block.
"""

from core.datetime_utils import safe_age_days
from memory.memory_config import (
    PROMPT_TYPE_ORDER,
    PROMPT_MEMORY_HEADER,
    PROMPT_REFLECTION_HEADER,
    PROMPT_REFLECTION_HINT,
)

PROMPT_GLOBAL_RULES_HEADER = "# VERBINDLICHE REGELN & PRÄFERENZEN"
PROMPT_GLOBAL_RULES_HINT = """(WICHTIG: Diese Regeln gelten IMMER — unabhängig vom aktuellen Thema.
Verstoß gegen diese Regeln ist ein Fehler. Halte dich strikt daran.)"""


# =============================================================================
# Chunk-Formatierung
# =============================================================================

def _format_chunk(chunk):
    """Formatiert einen einzelnen Chunk fuer den Prompt (Abschnitt 9)."""
    age_days = safe_age_days(chunk.get("created_at", ""), default=0)

    # Tags
    tags = chunk.get("tags", [])
    tags_str = f" | Tags: {', '.join(tags)}" if tags else ""

    return (
        f"[{chunk['chunk_type']}] "
        f"[{chunk['source']}] "
        f"[{age_days}d] "
        f"[conf:{chunk['confidence']:.2f}] "
        f"[{chunk['epistemic_status']}]"
        f"{tags_str}\n"
        f"{chunk['text']}"
    )


def _format_rule(chunk):
    """Formatiert eine globale Regel als klare Anweisung."""
    tags = chunk.get("tags", [])
    is_style = "response-style" in tags or "global-preference" in tags
    prefix = "STIL-REGEL" if is_style else chunk["chunk_type"].upper()
    return f"→ [{prefix}] {chunk['text']}"


# =============================================================================
# Globale Regeln (Preferences + Decisions) — IMMER im Prompt
# =============================================================================

def build_global_rules_prompt(chunks):
    """
    Baut den Block fuer globale Regeln: Preferences und Decisions.
    Wird IMMER geladen, unabhaengig von der User-Nachricht.

    Args:
        chunks: Liste von preference/decision Chunk-Dicts

    Returns:
        String fuer den System-Prompt, oder None wenn keine Chunks
    """
    if not chunks:
        return None

    lines = [PROMPT_GLOBAL_RULES_HEADER, PROMPT_GLOBAL_RULES_HINT, ""]
    for chunk in chunks:
        lines.append(_format_rule(chunk))
    lines.append("")

    return "\n".join(lines)


# =============================================================================
# Prompt-Aufbau (Abschnitt 9)
# =============================================================================

def build_memory_prompt(chunks):
    """
    Nimmt selektierte Chunks und baut den Memory-Block fuer den System-Prompt.

    Reihenfolge: decision -> knowledge -> working_state -> hard_fact -> preference -> self_reflection
    self_reflection bekommt einen eigenen Abschnitt.

    Args:
        chunks: Liste von Chunk-Dicts (aus retrieval.score_and_select)

    Returns:
        String fuer den System-Prompt, oder None wenn keine Chunks
    """
    if not chunks:
        return None

    # Chunks nach Typ gruppieren
    grouped = {}
    for chunk in chunks:
        ctype = chunk.get("chunk_type", "hard_fact")
        grouped.setdefault(ctype, []).append(chunk)

    # Innerhalb jeder Gruppe nach Score sortieren (hoechster zuerst)
    for ctype in grouped:
        grouped[ctype].sort(
            key=lambda c: c.get("_retrieval_score", 0),
            reverse=True,
        )

    sections = []

    # Hauptabschnitt: alle Typen ausser self_reflection
    main_types = [t for t in PROMPT_TYPE_ORDER if t != "self_reflection"]
    main_chunks = []
    for ctype in main_types:
        if ctype in grouped:
            main_chunks.extend(grouped[ctype])

    if main_chunks:
        lines = [PROMPT_MEMORY_HEADER, ""]
        for chunk in main_chunks:
            lines.append(_format_chunk(chunk))
            lines.append("")
        sections.append("\n".join(lines))

    # Separater Abschnitt: self_reflection
    if "self_reflection" in grouped:
        lines = [PROMPT_REFLECTION_HEADER, PROMPT_REFLECTION_HINT, ""]
        for chunk in grouped["self_reflection"]:
            lines.append(_format_chunk(chunk))
            lines.append("")
        sections.append("\n".join(lines))

    if not sections:
        return None

    return "\n---\n\n".join(sections)
