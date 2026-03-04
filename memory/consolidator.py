"""
SchnuBot.ai - Konsolidierer (Gedaechtnisbildung)
Referenz: Konzeptdokument V1.1, Abschnitt 13

Analysiert Gespraechsbloecke und erzeugt/aktualisiert Memory-Chunks via gpt-oss:120b-cloud.
Phase 4: Alle Aktionen aktiv (create, confirm, update, supersede).
"""

import json
import logging
import re
import requests
from datetime import datetime, timezone

from config import OLLAMA_API_URL, OLLAMA_API_KEY
from memory.memory_config import (
    CONSOLIDATION_MODEL,
    CHUNK_TYPES,
    VALID_SOURCES,
    EPISTEMIC_STATUS,
    CONFIDENCE_THRESHOLDS,
    CONFIDENCE_GLOBAL_MIN,
    CONFIDENCE_MAX,
    BUFFER_MAX_TURNS_PER_BLOCK,
    CONSOLIDATION_MAX_ACTIONS_PER_BLOCK,
    TAGS_MAX_PER_CHUNK,
    TYPE_FACTORS,
    MERGE_SIMILARITY_THRESHOLD,
)
from memory.chunk_schema import (
    create_chunk,
    validate_chunk,
    sanitize_tags,
    apply_confirm,
    apply_update,
    apply_archive,
)
from memory.memory_store import (
    store_chunk,
    get_chunk,
    update_chunk,
    archive_chunk,
    query_active,
    find_merge_candidates,
)

logger = logging.getLogger(__name__)

# Erlaubte Aktionen (Phase 4: alle aktiv)
ALLOWED_ACTIONS = {"create", "confirm", "update", "supersede"}


# =============================================================================
# System-Message (Abschnitt 13.1)
# =============================================================================

CONSOLIDATION_SYSTEM_MESSAGE = """You are the memory consolidation system for Mr. Robot, a WhatsApp AI assistant.
Your operator is Tommy. Mr. Robot is the persona — you analyze his conversations.

Your role:
- Analyze conversation blocks between Tommy (user) and Mr. Robot (assistant)
- Extract NEW information OR recognize updates to EXISTING memory chunks
- Compare the conversation against existing chunks provided below
- You are NOT the chat model. You never talk to Tommy directly.
- Your only output is a JSON array of memory actions — nothing else.

The conversation is in German. Your chunk texts must also be in German.
Respond ONLY with a valid JSON array. No prose, no markdown, no explanation."""


# =============================================================================
# Konsolidierer-Prompt (Abschnitt 13, Phase 4 erweitert)
# =============================================================================

def build_consolidation_prompt(conversation_block, existing_chunks=None):
    """Baut den Konsolidierer-Prompt mit dynamischen Werten aus Config."""

    existing_section = ""
    if existing_chunks:
        lines = ["## EXISTING CHUNKS (for comparison)"]
        lines.append("These chunks already exist in memory. Use their IDs for confirm/update/supersede actions.")
        lines.append("")
        for chunk in existing_chunks:
            tags = chunk.get("tags", [])
            tags_str = f" | Tags: {', '.join(tags)}" if tags else ""
            lines.append(
                f'  ID: {chunk["id"]}\n'
                f'  Type: {chunk["chunk_type"]} | Source: {chunk["source"]} | '
                f'Epistemic: {chunk["epistemic_status"]} | Confidence: {chunk["confidence"]}{tags_str}\n'
                f'  Text: {chunk["text"]}\n'
            )
        existing_section = "\n".join(lines) + "\n\n"

    return f"""## TASK
Analyze the following conversation block. Extract NEW information worth remembering
OR recognize updates to existing memory chunks.

{existing_section}## CHUNK TYPES (exactly one per chunk)

hard_fact: Stable, verifiable facts about Tommy.
  "Tommy arbeitet als BIM Manager am Universitaetsklinikum Leipzig."
  "Tommy hat einen Hintergrund in Medieninformatik."
  "Tommys Server laeuft auf Hetzner CPX32 mit Ubuntu 24."

preference: Likes, dislikes, communication style preferences. Stable traits, recurring patterns.
  "Tommy bevorzugt technisch fundierte Antworten ohne Smalltalk."
  "Tommy will keine Emojis in technischen Diskussionen."
  "Tommy arbeitet abends und mag lange, fokussierte Sessions."

decision: Binding decisions and commitments. Must be explicit, not speculative.
  "SchnuBot nutzt ChromaDB als Vektor-Datenbank — entschieden."
  "Drei-Modell-Architektur: Kimi chattet, gpt-oss konsolidiert, nomic embeddet."
  "Kein Parallelbetrieb beim Cutover, harter Umstieg."

working_state: Current work status, project progress, temporary activities, open tasks.
  "NUK-Projekt: Phase 2 Vorbereitung laeuft, Kickoff im Maerz geplant."
  "SchnuBot-Umbau: Phase 1 Infrastruktur abgeschlossen, Phase 2 Retrieval aktiv."
  "Tommy arbeitet gerade an der soul.md Perfektionierung."

self_reflection: Mr. Robot's own insights about his behavior or performance.
  "Ich sollte bei technischen Themen praeziser nachfragen statt anzunehmen."
  "Meine Antworten zu BIM-Themen sind manchmal zu allgemein."
  "Tommy korrigiert meinen Stil wiederholt — ich sollte knapper antworten."

knowledge: Domain knowledge useful for future conversations. ONLY if explicitly present in the block.
  "Low Level BIM arbeitet mit DWG/DGN-Dateien und Excel-Parametrik."
  "IFC ist das offene Format fuer High Level BIM, zertifiziert durch buildingSMART."

Boundary rule: If it describes a stable trait or recurring pattern: preference or hard_fact.
If it describes a temporary current activity: working_state.

## SOURCE (origin of information)
- "tommy": Information comes from Tommy (he said it, decided it, stated it)
- "robot": Self-reflection by Mr. Robot (own insight about his behavior/performance)
- "shared": Jointly developed rule or decision where both explicitly contributed ("wir setzen...", "entschieden...")

"shared" only for genuinely co-developed decisions. When in doubt, use "tommy".

## EPISTEMIC STATUS (reliability of the information itself)
epistemic_status != confidence.
- epistemic_status = What kind of knowledge is this? (fact, inference, speculation?)
- confidence = How sure are you about the extraction? (Did you extract it correctly?)

Definitions:
- "confirmed": Explicitly stated as definite fact AND/OR verified by system output in the block.
- "stated": Directly said by a speaker, not independently verified.
- "inferred": Obvious conclusion from multiple statements in the block. Not explicitly stated.
- "speculative": Intention or consideration, not a firm decision.
- "outdated": Was valid before but superseded by newer info within the block.

## COMPATIBILITY GUIDANCE (epistemic_status + chunk_type)
- decision: almost always "stated" or "confirmed". If speculative, it is NOT a decision.
- hard_fact: usually "stated". Only "confirmed" if verified by system output.
- working_state: "speculative" okay. "outdated" possible if revised in block.
- self_reflection: usually "inferred" or "stated".
- knowledge: only "stated" or "confirmed". Never "speculative".

## CONFIDENCE (extraction certainty)
Float between {CONFIDENCE_GLOBAL_MIN} and {CONFIDENCE_MAX}.
Minimum thresholds per type:
- decision: {CONFIDENCE_THRESHOLDS['decision']}
- hard_fact: {CONFIDENCE_THRESHOLDS['hard_fact']}
- knowledge: {CONFIDENCE_THRESHOLDS['knowledge']}
- preference: {CONFIDENCE_THRESHOLDS['preference']}
- working_state: {CONFIDENCE_THRESHOLDS['working_state']}
- self_reflection: {CONFIDENCE_THRESHOLDS['self_reflection']}

## ACTIONS
Choose the right action by comparing the conversation against existing chunks:

- "create": New information, no matching existing chunk.
- "confirm": The conversation restates or verifies an existing chunk. Reference its ID.
  Use when: Tommy repeats a known fact, or system output verifies stored info.
- "update": An existing chunk needs refinement (slightly changed, more detail, correction).
  Reference its ID and provide the updated text.
  Use when: The core info is the same but details changed.
- "supersede": An existing chunk is contradicted or fully replaced by new info.
  Reference the old chunk's ID. Provide the new replacement text.
  Use when: The old info is wrong or no longer valid.

IMPORTANT:
- confirm/update/supersede REQUIRE "existing_chunk_id" field with the exact ID from EXISTING CHUNKS.
- If no existing chunks match, always use "create".
- Prefer confirm over create when the info already exists.
- Prefer update over supersede when the change is minor.

## HARD RULES
1. NO HALLUCINATION: Only extract information present in or clearly derivable from the conversation block.
2. CONDENSATION ALLOWED: Paraphrase, summarize, draw obvious conclusions.
3. FORBIDDEN: Psychological speculation, unstated interpretations, reading between the lines.
4. Exactly ONE chunk_type per chunk. No "misc", "unknown", or "other".
5. Tags: max {TAGS_MAX_PER_CHUNK}, lowercase, kebab-case, semantically relevant, no filler words.
6. Chunk text in German, condensed but self-contained.
7. If nothing is worth storing or updating: return empty array [].
8. PRIVACY: Do NOT store API keys, tokens, passwords, addresses, phone numbers, emails, QR codes, or secrets.
9. LIMIT: Output at most {CONSOLIDATION_MAX_ACTIONS_PER_BLOCK} actions per block. Prioritize decisions.
10. KNOWLEDGE STRICTNESS: Only create knowledge chunks from info explicitly in the block.
11. DEDUPLICATION: If two extracted chunks have >90% same meaning, keep the more precise one.
12. Do not store assistant messages unless they are explicit self_reflection.

## OUTPUT FORMAT
JSON array only. No prose, no markdown backticks.

For "create":
{{
  "action": "create",
  "text": "Condensed content in German",
  "chunk_type": "hard_fact|preference|decision|working_state|self_reflection|knowledge",
  "source": "tommy|robot|shared",
  "confidence": 0.85,
  "epistemic_status": "confirmed|stated|inferred|speculative|outdated",
  "tags": ["tag-eins", "tag-zwei"]
}}

For "confirm":
{{
  "action": "confirm",
  "existing_chunk_id": "uuid-of-existing-chunk",
  "confidence": 0.90
}}

For "update":
{{
  "action": "update",
  "existing_chunk_id": "uuid-of-existing-chunk",
  "text": "Updated text in German",
  "confidence": 0.85,
  "epistemic_status": "stated"
}}

For "supersede":
{{
  "action": "supersede",
  "existing_chunk_id": "uuid-of-old-chunk",
  "text": "New replacement text in German",
  "chunk_type": "hard_fact|preference|decision|working_state|self_reflection|knowledge",
  "source": "tommy|robot|shared",
  "confidence": 0.85,
  "epistemic_status": "stated",
  "tags": ["tag-eins"]
}}

If nothing worth storing or updating: []

## CONVERSATION BLOCK

{conversation_block}

## YOUR RESPONSE (JSON array only):"""


# =============================================================================
# Merge-Kandidaten fuer den Prompt laden (Phase 4, Checkpoint 4.1)
# =============================================================================

def _get_existing_chunks_for_block(turns, max_candidates=15):
    """
    Sucht bestehende Chunks die zum Turn-Block passen.
    Nutzt den gesamten Block-Text als Query fuer semantische Suche.
    """
    # Block-Text als Query zusammenbauen
    block_text = format_block_for_prompt(turns)
    if not block_text:
        return []

    try:
        results = query_active(block_text, n_results=max_candidates)
        if results:
            logger.info(f"Konsolidierer: {len(results)} bestehende Chunks als Kontext geladen")
        return results
    except Exception as e:
        logger.warning(f"Konsolidierer: Fehler beim Laden bestehender Chunks: {e}")
        return []


# =============================================================================
# Buffer & Blockbildung (Abschnitt 13.4)
# =============================================================================

def build_blocks(turns):
    """Teilt Turns in Bloecke auf. Max BUFFER_MAX_TURNS_PER_BLOCK pro Block."""
    if not turns:
        return []

    blocks = []
    for i in range(0, len(turns), BUFFER_MAX_TURNS_PER_BLOCK):
        block = turns[i:i + BUFFER_MAX_TURNS_PER_BLOCK]
        blocks.append(block)

    return blocks


def format_block_for_prompt(turns):
    """Formatiert einen Turn-Block als lesbaren Text fuer den Prompt."""
    role_map = {"user": "Tommy", "assistant": "Mr. Robot"}
    lines = []
    for turn in turns:
        role = role_map.get(turn.get("role"))
        if role is None:
            continue
        lines.append(f"{role}: {turn['content']}")
    return "\n\n".join(lines)


# =============================================================================
# Konsolidierung ausfuehren (Phase 4 erweitert)
# =============================================================================

def consolidate_block(turns):
    """
    Konsolidiert einen einzelnen Turn-Block.
    Phase 4: Laedt bestehende Chunks und uebergibt sie dem Prompt.
    """
    conversation_text = format_block_for_prompt(turns)

    # Phase 4: Bestehende Chunks als Kontext laden
    existing_chunks = _get_existing_chunks_for_block(turns)
    prompt = build_consolidation_prompt(conversation_text, existing_chunks)

    # API-Call
    raw_response = _call_consolidation_model(prompt)
    if raw_response is None:
        return []

    # JSON parsen
    chunk_defs = _parse_response(raw_response)
    if chunk_defs is None:
        logger.warning("Erster Parse fehlgeschlagen, Retry mit Fehlerhinweis...")
        retry_prompt = prompt + "\n\nYour previous response was not valid JSON. Return ONLY a valid JSON array now. No text, no backticks."
        raw_response = _call_consolidation_model(retry_prompt)
        if raw_response is None:
            return []
        chunk_defs = _parse_response(raw_response)
        if chunk_defs is None:
            logger.error("Retry fehlgeschlagen, Block verworfen")
            return []

    if not chunk_defs:
        logger.info("Konsolidierer: No-Op (nichts speicherwuerdig)")
        return []

    # Aktionslimit
    chunk_defs = _apply_action_limit(chunk_defs)

    # Chunks verarbeiten (create/confirm/update/supersede)
    result_ids = []
    for cdef in chunk_defs:
        action = cdef.get("action", "create")
        if action == "create":
            chunk_id = _process_create(cdef)
        elif action == "confirm":
            chunk_id = _process_confirm(cdef)
        elif action == "update":
            chunk_id = _process_update(cdef)
        elif action == "supersede":
            chunk_id = _process_supersede(cdef)
        else:
            logger.warning(f"Konsolidierer: Unbekannte Action '{action}', uebersprungen")
            chunk_id = None

        if chunk_id:
            result_ids.append(chunk_id)

    # Ergebnis loggen
    actions = {}
    for cdef in chunk_defs:
        a = cdef.get("action", "?")
        actions[a] = actions.get(a, 0) + 1
    logger.info(f"Konsolidierer: {len(result_ids)} Aktionen ausgefuehrt | {actions}")

    return result_ids


def consolidate_turns(turns):
    """Haupteinstiegspunkt: Turns -> Bloecke -> Konsolidierung."""
    blocks = build_blocks(turns)
    if not blocks:
        return 0

    total = 0
    for i, block in enumerate(blocks):
        logger.info(f"Konsolidiere Block {i+1}/{len(blocks)} ({len(block)} Turns)")
        ids = consolidate_block(block)
        total += len(ids)

    return total


# =============================================================================
# Aktionslimit mit Priorisierung
# =============================================================================

def _apply_action_limit(chunk_defs):
    """
    Wendet Aktionslimit an. Decisions duerfen Limit ueberschreiten.
    confirm/update zaehlen nicht gegen das Limit (sie erzeugen keine neuen Chunks).
    """
    creates = [c for c in chunk_defs if c.get("action") == "create"]
    non_creates = [c for c in chunk_defs if c.get("action") != "create"]

    if len(creates) <= CONSOLIDATION_MAX_ACTIONS_PER_BLOCK:
        return chunk_defs

    logger.warning(
        f"Konsolidierer: {len(creates)} create-Aktionen, "
        f"Limit ist {CONSOLIDATION_MAX_ACTIONS_PER_BLOCK}. Priorisiere."
    )

    decisions = [c for c in creates if c.get("chunk_type") == "decision"]
    others = [c for c in creates if c.get("chunk_type") != "decision"]

    # V1-Heuristik: Retrieval TYPE_FACTORS als Proxy fuer Zukunftsnutzen
    def sort_key(cdef):
        type_prio = TYPE_FACTORS.get(cdef.get("chunk_type", ""), 0.5)
        conf = float(cdef.get("confidence", 0.0))
        return (type_prio, conf)

    others.sort(key=sort_key, reverse=True)
    remaining_slots = max(0, CONSOLIDATION_MAX_ACTIONS_PER_BLOCK - len(decisions))
    kept = others[:remaining_slots]
    dropped = others[remaining_slots:]

    if dropped:
        logger.info(f"Konsolidierer: {len(dropped)} create-Chunks gedroppt")

    return decisions + kept + non_creates


# =============================================================================
# Action Handlers (Phase 4)
# =============================================================================

def _process_create(cdef):
    """Verarbeitet eine create-Aktion. Validiert und speichert neuen Chunk."""
    text = cdef.get("text", "").strip()
    chunk_type = cdef.get("chunk_type", "")
    source = cdef.get("source", "")
    confidence = cdef.get("confidence", 0.0)
    epistemic_status = cdef.get("epistemic_status", "")
    tags = cdef.get("tags", [])

    # Validierung
    error = _validate_common_fields(text, chunk_type, source, confidence, epistemic_status)
    if error:
        return None

    confidence = float(confidence)
    if not isinstance(tags, list):
        tags = []
    tags = sanitize_tags(tags)

    if _contains_sensitive_data(text):
        logger.warning(f"Konsolidierer: PII/Secret erkannt, Chunk uebersprungen")
        return None

    _check_epistemic_compatibility(chunk_type, epistemic_status, text)

    chunk = create_chunk(
        text=text, chunk_type=chunk_type, source=source,
        confidence=confidence, epistemic_status=epistemic_status, tags=tags,
    )

    valid, err = validate_chunk(chunk)
    if not valid:
        logger.warning(f"Konsolidierer: Chunk ungueltig: {err}")
        return None

    try:
        store_chunk(chunk)
        logger.info(f"CREATE: [{chunk_type}] [{source}] [{epistemic_status}] conf={confidence} | {text[:80]}")
        return chunk["id"]
    except Exception as e:
        logger.error(f"Konsolidierer: Speicherfehler: {e}")
        return None


def _process_confirm(cdef):
    """Verarbeitet eine confirm-Aktion. Staerkt bestehenden Chunk."""
    chunk_id = cdef.get("existing_chunk_id", "")
    if not chunk_id:
        logger.warning("Konsolidierer: confirm ohne existing_chunk_id, uebersprungen")
        return None

    existing = get_chunk(chunk_id)
    if not existing:
        logger.warning(f"Konsolidierer: confirm — Chunk {chunk_id[:8]} nicht gefunden, uebersprungen")
        return None

    apply_confirm(existing)

    try:
        update_chunk(existing)
        logger.info(
            f"CONFIRM: [{existing['chunk_type']}] conf={existing['confidence']:.2f} "
            f"weight={existing['weight']:.2f} | {existing['text'][:60]}"
        )
        return chunk_id
    except Exception as e:
        logger.error(f"Konsolidierer: confirm-Fehler: {e}")
        return None


def _process_update(cdef):
    """Verarbeitet eine update-Aktion. Aktualisiert Text/Confidence eines bestehenden Chunks."""
    chunk_id = cdef.get("existing_chunk_id", "")
    new_text = cdef.get("text", "").strip()
    new_confidence = cdef.get("confidence")
    new_epistemic = cdef.get("epistemic_status")

    if not chunk_id:
        logger.warning("Konsolidierer: update ohne existing_chunk_id, uebersprungen")
        return None

    existing = get_chunk(chunk_id)
    if not existing:
        logger.warning(f"Konsolidierer: update — Chunk {chunk_id[:8]} nicht gefunden, uebersprungen")
        return None

    # Text aktualisieren
    if new_text:
        if _contains_sensitive_data(new_text):
            logger.warning(f"Konsolidierer: PII in update-Text, uebersprungen")
            return None
        existing["text"] = new_text

    # Epistemic Status aktualisieren
    if new_epistemic and new_epistemic in EPISTEMIC_STATUS:
        existing["epistemic_status"] = new_epistemic

    # apply_update: weight +0.02, confidence blend
    if new_confidence:
        try:
            apply_update(existing, float(new_confidence))
        except (ValueError, TypeError):
            pass

    try:
        update_chunk(existing)
        logger.info(
            f"UPDATE: [{existing['chunk_type']}] conf={existing['confidence']:.2f} | "
            f"{existing['text'][:80]}"
        )
        return chunk_id
    except Exception as e:
        logger.error(f"Konsolidierer: update-Fehler: {e}")
        return None


def _process_supersede(cdef):
    """
    Verarbeitet eine supersede-Aktion.
    1. Alten Chunk archivieren (status: archived, epistemic: outdated)
    2. Neuen Chunk erstellen mit supersedes-Referenz
    """
    old_chunk_id = cdef.get("existing_chunk_id", "")
    if not old_chunk_id:
        logger.warning("Konsolidierer: supersede ohne existing_chunk_id, uebersprungen")
        return None

    # Alten Chunk pruefen
    old_chunk = get_chunk(old_chunk_id)
    if not old_chunk:
        logger.warning(f"Konsolidierer: supersede — Chunk {old_chunk_id[:8]} nicht gefunden, als create behandelt")
        cdef["action"] = "create"
        return _process_create(cdef)

    # Neuen Chunk validieren
    text = cdef.get("text", "").strip()
    chunk_type = cdef.get("chunk_type", old_chunk.get("chunk_type", ""))
    source = cdef.get("source", old_chunk.get("source", "tommy"))
    confidence = cdef.get("confidence", 0.0)
    epistemic_status = cdef.get("epistemic_status", "stated")
    tags = cdef.get("tags", [])

    error = _validate_common_fields(text, chunk_type, source, confidence, epistemic_status)
    if error:
        return None

    confidence = float(confidence)
    if not isinstance(tags, list):
        tags = []
    tags = sanitize_tags(tags)

    if _contains_sensitive_data(text):
        logger.warning(f"Konsolidierer: PII in supersede-Text, uebersprungen")
        return None

    # 1. Alten Chunk archivieren
    try:
        archive_chunk(old_chunk_id)
        logger.info(f"SUPERSEDE: Alter Chunk {old_chunk_id[:8]} archiviert")
    except Exception as e:
        logger.error(f"Konsolidierer: Archivierung fehlgeschlagen: {e}")
        return None

    # 2. Neuen Chunk erstellen mit Referenz
    new_chunk = create_chunk(
        text=text, chunk_type=chunk_type, source=source,
        confidence=confidence, epistemic_status=epistemic_status,
        tags=tags, supersedes=old_chunk_id,
    )

    valid, err = validate_chunk(new_chunk)
    if not valid:
        logger.warning(f"Konsolidierer: Neuer supersede-Chunk ungueltig: {err}")
        return None

    try:
        store_chunk(new_chunk)
        logger.info(
            f"SUPERSEDE: Neuer Chunk [{chunk_type}] [{source}] conf={confidence} | {text[:80]}"
        )
        return new_chunk["id"]
    except Exception as e:
        logger.error(f"Konsolidierer: Speicherfehler beim supersede: {e}")
        return None


# =============================================================================
# Gemeinsame Validierung
# =============================================================================

def _validate_common_fields(text, chunk_type, source, confidence, epistemic_status):
    """
    Validiert gemeinsame Felder fuer create und supersede.
    Returns: None wenn ok, Error-String wenn nicht.
    """
    if not text:
        logger.warning("Konsolidierer: Chunk ohne Text, uebersprungen")
        return "no text"

    if chunk_type not in CHUNK_TYPES:
        logger.warning(f"Konsolidierer: Ungueltiger Typ '{chunk_type}', uebersprungen")
        return "bad type"

    if source not in VALID_SOURCES:
        logger.warning(f"Konsolidierer: Ungueltige Source '{source}', uebersprungen")
        return "bad source"

    if epistemic_status not in EPISTEMIC_STATUS:
        logger.warning(f"Konsolidierer: Ungueltiger Epistemic Status '{epistemic_status}', uebersprungen")
        return "bad epistemic"

    try:
        conf = float(confidence)
    except (ValueError, TypeError):
        logger.warning(f"Konsolidierer: Ungueltige Confidence '{confidence}', uebersprungen")
        return "bad confidence"

    if conf < CONFIDENCE_GLOBAL_MIN or conf > CONFIDENCE_MAX:
        logger.warning(f"Konsolidierer: Confidence {conf} ausserhalb Bereich, uebersprungen")
        return "confidence range"

    threshold = CONFIDENCE_THRESHOLDS.get(chunk_type, CONFIDENCE_GLOBAL_MIN)
    if conf < threshold:
        logger.info(f"Konsolidierer: Confidence {conf} unter Schwelle {threshold} fuer {chunk_type}")
        return "below threshold"

    return None


# =============================================================================
# API-Call
# =============================================================================

def _call_consolidation_model(prompt):
    """Ruft gpt-oss:120b-cloud auf mit Retry."""
    from api_utils import api_call_with_retry

    result = api_call_with_retry(
        url=f"{OLLAMA_API_URL}/api/chat",
        headers={
            "Authorization": f"Bearer {OLLAMA_API_KEY}",
            "Content-Type": "application/json",
        },
        json_payload={
            "model": CONSOLIDATION_MODEL,
            "messages": [
                {"role": "system", "content": CONSOLIDATION_SYSTEM_MESSAGE},
                {"role": "user", "content": prompt},
            ],
            "stream": False,
        },
        timeout=120,
    )

    if result:
        return result.get("message", {}).get("content", "").strip()
    return None


# =============================================================================
# Response-Parsing
# =============================================================================

def _parse_response(raw):
    """Parst die JSON-Antwort des Konsolidierers."""
    if not raw:
        return None

    cleaned = raw.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        cleaned = "\n".join(lines[1:-1]).strip() if len(lines) > 2 else "[]"

    try:
        result = json.loads(cleaned)
        if not isinstance(result, list):
            logger.warning(f"Konsolidierer: Antwort ist kein Array: {type(result)}")
            return None
        return result
    except json.JSONDecodeError as e:
        logger.warning(f"Konsolidierer JSON-Fehler: {e}")
        logger.debug(f"Raw response: {raw[:500]}")
        return None


# =============================================================================
# Epistemic-Kompatibilitaet (Soft Warnings)
# =============================================================================

def _check_epistemic_compatibility(chunk_type, epistemic_status, text):
    """Soft Warnings fuer problematische Kombinationen."""
    warnings = []

    if chunk_type == "decision" and epistemic_status == "speculative":
        warnings.append("decision + speculative")
    if chunk_type == "decision" and epistemic_status == "inferred":
        warnings.append("decision + inferred")
    if chunk_type == "knowledge" and epistemic_status == "speculative":
        warnings.append("knowledge + speculative")
    if chunk_type == "knowledge" and epistemic_status == "inferred":
        warnings.append("knowledge + inferred")
    if chunk_type == "hard_fact" and epistemic_status == "outdated":
        warnings.append("hard_fact + outdated")
    if chunk_type == "decision" and epistemic_status == "outdated":
        warnings.append("decision + outdated")
    if chunk_type == "preference" and epistemic_status == "confirmed":
        warnings.append("preference + confirmed")

    for w in warnings:
        logger.warning(f"Epistemic-Kompatibilitaet: {w} | {text[:60]}")


# =============================================================================
# PII / Secrets Sicherheitsnetz
# =============================================================================

_PII_PATTERNS = [
    re.compile(r"sk-[a-zA-Z0-9]{20,}"),
    re.compile(r"Bearer\s+[a-zA-Z0-9\-_.]{20,}"),
    re.compile(r"api[_-]?key\s*[:=]\s*\S{10,}", re.I),
    re.compile(r"-----BEGIN .+ KEY-----"),
    re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}"),
    re.compile(r"\b\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}\b"),
    re.compile(r"password\s*[:=]\s*\S+", re.I),
    re.compile(r"token\s*[:=]\s*[a-f0-9]{32,}", re.I),
]


def _contains_sensitive_data(text):
    """Prueft ob ein Text offensichtliche PII/Secrets enthaelt."""
    for pattern in _PII_PATTERNS:
        if pattern.search(text):
            return True
    return False
