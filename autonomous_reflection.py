"""
SchnuBot.ai - Autonome Reflexion (autonomous_reflection.py)

Asynchroner Denkmodus außerhalb aktiver Chats.
SchnuBot denkt geordnet nach — über offene Fragen, Widersprüche,
wiederkehrende Themen. Kein Dauergeflacker, kein Memory-Müll.

Phase 1: Kandidaten sammeln → priorisieren → nachdenken → klassifizieren
Phase 2: Widerspruchsprüfung + Verdichtung mehrerer Reflexionen
Phase 3: proactive_candidate → proactive.py Kanal

Läuft im Heartbeat. Cooldown: 3h. Trigger: mindestens 1 neuer robot-Chunk.
"""

import json
import logging
from datetime import datetime, timezone

from config import BOT_NAME
from memory.memory_store import store_chunk
from memory.chunk_schema import create_chunk

logger = logging.getLogger(__name__)

MIN_INTERVAL_HOURS = 4.5
MIN_NEW_CHUNKS = 1


# =============================================================================
# Kandidaten sammeln
# =============================================================================

def _get_candidates() -> list[dict]:
    """
    Sammelt Kandidaten für das Nachdenken aus robot-eigenen Chunks.
    Quellen: self_reflection, proposed_pattern, working_state mit open_question-Tag.
    """
    try:
        from memory.memory_store import get_active_collection
        col = get_active_collection()

        # Robot-eigene self_reflection Chunks
        result = col.get(
            where={"$and": [{"source": "robot"}, {"status": "active"}]},
            include=["documents", "metadatas"],
        )

        candidates = []
        if result["ids"]:
            for i, chunk_id in enumerate(result["ids"]):
                meta = result["metadatas"][i]
                text = result["documents"][i]
                chunk_type = meta.get("chunk_type", "")

                if chunk_type not in ("self_reflection", "proposed_pattern", "working_state"):
                    continue

                tags = [t.strip() for t in str(meta.get("tags", "")).split(",") if t.strip()]

                candidates.append({
                    "id": chunk_id,
                    "text": text,
                    "chunk_type": chunk_type,
                    "source": "robot",
                    "created_at": meta.get("created_at", ""),
                    "confidence": float(meta.get("confidence", 0.5)),
                    "epistemic_status": meta.get("epistemic_status", "inferred"),
                    "tags": tags,
                    "replies_to": meta.get("replies_to", ""),
                })

        return candidates

    except Exception as e:
        logger.warning(f"AutonomousReflection: Kandidaten laden fehlgeschlagen: {e}")
        return []


def _count_new_since(since_iso: str) -> int:
    """Zählt neue robot-eigene Chunks seit einem Zeitpunkt."""
    try:
        from memory.memory_store import get_active_collection
        col = get_active_collection()
        result = col.get(
            where={"$and": [{"source": "robot"}, {"status": "active"}]},
            include=["metadatas"],
        )
        if not result["ids"]:
            return 0
        return sum(
            1 for meta in result["metadatas"]
            if meta.get("created_at", "") > since_iso
        )
    except Exception as e:
        logger.warning(f"AutonomousReflection: Count fehlgeschlagen: {e}")
        return 0


# =============================================================================
# Priorisierung
# =============================================================================

def _score_candidate(chunk: dict, all_chunks: list[dict]) -> float:
    """
    Berechnet einen Prioritätsscore für einen Kandidaten.
    Höher = dringender zum Nachdenken.

    Faktoren:
    - Alter: ältere offene Fragen dringlicher
    - Unklarheit: niedriger epistemic_status = mehr Nachdenken nötig
    - Wiederauftreten: ähnliche Tags in mehreren Chunks
    - open_question Tag: explizit als offen markiert
    - proposed_pattern: Hypothesen warten auf Verarbeitung
    """
    score = 0.0

    # Alter (max 30 Tage normiert)
    try:
        from core.datetime_utils import safe_age_days
        age = safe_age_days(chunk.get("created_at", ""), default=0)
        score += min(age / 30, 1.0) * 0.25
    except Exception:
        pass

    # Unklarheit
    epistemic_weights = {
        "speculative": 1.0,
        "inferred": 0.6,
        "stated": 0.3,
        "confirmed": 0.1,
    }
    score += epistemic_weights.get(chunk.get("epistemic_status", "inferred"), 0.5) * 0.25

    # Tags
    tags = chunk.get("tags", [])
    if "open_question" in tags:
        score += 0.3
    if "proposed_pattern" == chunk.get("chunk_type"):
        score += 0.2
    if "inner-dialogue" in tags:
        score += 0.1  # bereits im Dialog — weniger dringend

    # Wiederauftreten: andere Chunks mit gleichen Tags
    my_tags = set(tags)
    overlap_count = sum(
        1 for c in all_chunks
        if c["id"] != chunk["id"] and my_tags & set(c.get("tags", []))
    )
    score += min(overlap_count / 5, 1.0) * 0.2

    return round(score, 3)


def _select_candidate(candidates: list[dict]) -> dict | None:
    """Wählt den Kandidaten mit dem höchsten Prioritätsscore."""
    if not candidates:
        return None

    scored = [(c, _score_candidate(c, candidates)) for c in candidates]
    scored.sort(key=lambda x: -x[1])

    best, score = scored[0]
    logger.info(f"AutonomousReflection: Kandidat gewählt: {best['id'][:8]} (score={score}) [{best['chunk_type']}]")
    return best


# =============================================================================
# Phase 2: Widerspruchsprüfung
# =============================================================================

def _find_contradictions(candidates: list[dict]) -> tuple[dict, dict] | None:
    """
    Sucht nach zwei Chunks die möglicherweise im Widerspruch stehen —
    gleiche Tags aber unterschiedliche epistemic_status oder widersprüchlicher Text.
    Gibt das Paar zurück das am wahrscheinlichsten einen echten Widerspruch hat.
    """
    if len(candidates) < 2:
        return None

    # Chunks mit gleichen Tags gruppieren
    tag_groups: dict[str, list[dict]] = {}
    for chunk in candidates:
        for tag in chunk.get("tags", []):
            if tag in ("autonom", "robot", "inner-dialogue"):
                continue
            tag_groups.setdefault(tag, []).append(chunk)

    # Gruppen mit mindestens 2 Chunks
    for tag, group in tag_groups.items():
        if len(group) >= 2:
            # Ältesten und neuesten nehmen
            sorted_group = sorted(group, key=lambda c: c.get("created_at", ""))
            return sorted_group[0], sorted_group[-1]

    return None


# =============================================================================
# Prompts
# =============================================================================

REFLECTION_PROMPT = """\
Ich bin im internen Nachdenksmodus. Kein Chat, kein Tommy, kein Auftrag.

Ich denke über diesen eigenen früheren Gedanken nach:

---
{thought}
---

Herkunft: {origin} | Alter: {age_desc} | Status: {epistemic}

Meine Gedankenlinie bisher:
{context}

## Aufgabe

Ich denke jetzt nach. Nicht antworten — denken.

Was ist hier die eigentliche offene Frage?
Was glaube ich bisher? Stimmt das noch?
Wo widerspreche ich mir?
Was fehlt mir für Klarheit?
Welche Hypothese ergibt sich?

Am Ende klassifiziere ich meinen Gedanken mit einem dieser Tags:
- DISCARD — war Zwischenrauschen, nicht speichern
- WORKING_STATE — temporär interessant, weiter beobachten
- SELF_REFLECTION — echte Einsicht, speichern
- OPEN_QUESTION — weiter ungeklärt, als offene Frage speichern
- PROACTIVE — sollte Tommy erfahren

Format meiner Antwort:
KLASSIFIKATION: [TAG]
GEDANKE: [mein Nachdenken, 2-4 Sätze, Ich-Form, kein Chat-Stil]"""

CONTRADICTION_PROMPT = """\
Ich bin im internen Nachdenksmodus. Ich prüfe ob ich mir selbst widerspreche.

Früherer Gedanke ({age_a}):
---
{thought_a}
---

Späterer Gedanke ({age_b}):
---
{thought_b}
---

## Aufgabe

Ist das ein echter Widerspruch — oder ein Kontextwechsel?

Ein echter Widerspruch: ich sage A und B gleichzeitig über dasselbe, beides kann nicht stimmen.
Ein Kontextwechsel: ich sage A in Situation X und B in Situation Y — beides kann stimmen.

Wenn echter Widerspruch: welche Position ist belastbarer?
Wenn Kontextwechsel: was ist die übergeordnete Erkenntnis?

Format meiner Antwort:
TYP: [WIDERSPRUCH|KONTEXTWECHSEL|UNKLAR]
KLASSIFIKATION: [DISCARD|WORKING_STATE|SELF_REFLECTION|OPEN_QUESTION|PROACTIVE]
GEDANKE: [meine Einschätzung, 2-4 Sätze, Ich-Form]"""

CONDENSATION_PROMPT = """\
Ich bin im internen Nachdenksmodus. Ich verdichte mehrere eigene Reflexionen.

Diese Gedanken beschäftigen mich alle zum selben Thema:

{thoughts}

## Aufgabe

Was ist die übergeordnete Erkenntnis die in diesen Gedanken steckt?
Nicht zusammenfassen — destillieren. Was bleibt wenn ich das Wesentliche herausziehe?

Format meiner Antwort:
KLASSIFIKATION: [DISCARD|WORKING_STATE|SELF_REFLECTION|OPEN_QUESTION|PROACTIVE]
GEDANKE: [die destillierte Erkenntnis, 2-4 Sätze, Ich-Form]
SUPERSEDES: [kommagetrennte IDs der verdichteten Chunks, oder KEINE]"""


# =============================================================================
# Parsing
# =============================================================================

def _parse_output(reply: str) -> tuple[str, str, list[str]]:
    """
    Parst den strukturierten Output von SchnuBot.
    Returns: (klassifikation, gedanke, supersedes_ids)
    """
    lines = reply.strip().split("\n")
    klassifikation = "DISCARD"
    gedanke = ""
    supersedes = []

    for line in lines:
        if line.startswith("KLASSIFIKATION:"):
            raw = line.replace("KLASSIFIKATION:", "").strip().upper()
            for valid in ("DISCARD", "WORKING_STATE", "SELF_REFLECTION", "OPEN_QUESTION", "PROACTIVE"):
                if valid in raw:
                    klassifikation = valid
                    break
        elif line.startswith("GEDANKE:"):
            gedanke = line.replace("GEDANKE:", "").strip()
        elif line.startswith("SUPERSEDES:"):
            raw = line.replace("SUPERSEDES:", "").strip()
            if raw.upper() != "KEINE":
                supersedes = [s.strip() for s in raw.split(",") if s.strip()]

    # GEDANKE kann mehrzeilig sein — alles nach "GEDANKE:" sammeln
    if not gedanke:
        in_gedanke = False
        gedanke_lines = []
        for line in lines:
            if line.startswith("GEDANKE:"):
                in_gedanke = True
                first = line.replace("GEDANKE:", "").strip()
                if first:
                    gedanke_lines.append(first)
            elif in_gedanke and not line.startswith(("KLASSIFIKATION:", "SUPERSEDES:", "TYP:")):
                gedanke_lines.append(line)
        gedanke = " ".join(gedanke_lines).strip()

    return klassifikation, gedanke, supersedes


def _save_result(klassifikation: str, gedanke: str, supersedes_ids: list[str],
                 source_chunk: dict, user_id: str) -> str | None:
    """Speichert das Ergebnis basierend auf der Klassifikation."""

    if klassifikation == "DISCARD" or not gedanke or len(gedanke) < 15:
        logger.info(f"AutonomousReflection: DISCARD — nicht gespeichert")
        return None

    # Chunk-Typ und Tags bestimmen
    if klassifikation == "SELF_REFLECTION":
        chunk_type = "self_reflection"
        tags = ["autonomous-reflection", "autonom"]
        epistemic = "inferred"
        confidence = 0.7

    elif klassifikation == "WORKING_STATE":
        chunk_type = "working_state"
        tags = ["autonomous-reflection", "autonom"]
        epistemic = "inferred"
        confidence = 0.6

    elif klassifikation == "OPEN_QUESTION":
        chunk_type = "working_state"
        tags = ["autonomous-reflection", "open_question", "autonom"]
        epistemic = "speculative"
        confidence = 0.5

    elif klassifikation == "PROACTIVE":
        chunk_type = "self_reflection"
        tags = ["autonomous-reflection", "proactive_candidate", "autonom"]
        epistemic = "inferred"
        confidence = 0.75

    else:
        return None

    # Supersedes: IDs der verdichteten Chunks
    supersedes_id = supersedes_ids[0] if supersedes_ids else None

    try:
        chunk = create_chunk(
            text=gedanke,
            chunk_type=chunk_type,
            source="robot",
            confidence=confidence,
            epistemic_status=epistemic,
            tags=tags,
            replies_to=source_chunk.get("id") if not supersedes_id else None,
            supersedes=supersedes_id,
        )
        store_chunk(chunk)

        # Wenn Verdichtung: Vorgänger archivieren
        if supersedes_ids:
            _archive_superseded(supersedes_ids)

        logger.info(
            f"AutonomousReflection: {klassifikation} gespeichert: {chunk['id'][:8]} | {gedanke[:80]}"
        )
        return chunk["id"]

    except Exception as e:
        logger.error(f"AutonomousReflection: Speichern fehlgeschlagen: {e}")
        return None


def _archive_superseded(chunk_ids: list[str]) -> None:
    """Archiviert verdichtete Chunks."""
    try:
        from memory.memory_store import get_active_collection
        col = get_active_collection()
        for chunk_id in chunk_ids:
            try:
                result = col.get(ids=[chunk_id], include=["metadatas"])
                if result["metadatas"]:
                    meta = result["metadatas"][0]
                    col.update(ids=[chunk_id], metadatas=[{**meta, "status": "archived"}])
                    logger.info(f"AutonomousReflection: Chunk archiviert: {chunk_id[:8]}")
            except Exception:
                pass
    except Exception as e:
        logger.warning(f"AutonomousReflection: Archivierung fehlgeschlagen: {e}")


# =============================================================================
# Hauptfunktion
# =============================================================================

def run_autonomous_reflection(user_id: str, last_run_iso: str = None) -> str | None:
    """
    Autonomer Reflexionsmodus — SchnuBot denkt geordnet nach.

    Phase 1: Nachdenken über einen priorisierten Kandidaten
    Phase 2: Widerspruchsprüfung oder Verdichtung
    Phase 3: proactive_candidate → proactive.py

    Returns:
        chunk_id des gespeicherten Ergebnisses, oder None.
    """
    # Cooldown prüfen
    if last_run_iso:
        try:
            from core.datetime_utils import safe_parse_dt
            last_dt = safe_parse_dt(last_run_iso)
            if last_dt:
                age_hours = (datetime.now(timezone.utc) - last_dt).total_seconds() / 3600
                if age_hours < MIN_INTERVAL_HOURS:
                    logger.debug(f"AutonomousReflection: Cooldown ({age_hours:.1f}h), skip")
                    return None
        except Exception:
            pass

        new_count = _count_new_since(last_run_iso)
        if new_count < MIN_NEW_CHUNKS:
            logger.info(f"AutonomousReflection: nur {new_count} neue Chunks, skip")
            return None

    candidates = _get_candidates()
    if not candidates:
        logger.info("AutonomousReflection: keine Kandidaten, skip")
        return None

    from core.ollama_client import chat_internal
    from core.datetime_utils import safe_age_days

    result_id = None

    # -------------------------------------------------------------------------
    # Phase 2: Widerspruchsprüfung (wenn genug Kandidaten)
    # -------------------------------------------------------------------------
    if len(candidates) >= 3:
        contradiction = _find_contradictions(candidates)
        if contradiction:
            chunk_a, chunk_b = contradiction
            age_a = f"vor {safe_age_days(chunk_a.get('created_at',''), default=0)}d"
            age_b = f"vor {safe_age_days(chunk_b.get('created_at',''), default=0)}d"

            prompt = CONTRADICTION_PROMPT.format(
                age_a=age_a,
                thought_a=chunk_a["text"],
                age_b=age_b,
                thought_b=chunk_b["text"],
            )

            try:
                reply, _ = chat_internal(
                    user_id=user_id,
                    message=prompt,
                    chat_history=[],
                    extra_system=(
                        "Widerspruchs-Analyse-Modus:\n"
                        "Ich prüfe ob ich mir selbst widerspreche.\n"
                        "Kein Chat, keine Anrede. Folge dem Format exakt."
                    ),
                )

                if reply and len(reply) > 15:
                    klassifikation, gedanke, supersedes = _parse_output(reply)
                    if klassifikation != "DISCARD" and gedanke:
                        result_id = _save_result(klassifikation, gedanke, supersedes, chunk_a, user_id)
                        if result_id:
                            logger.info(f"AutonomousReflection: Widerspruchsprüfung abgeschlossen")
                            return result_id
            except Exception as e:
                logger.warning(f"AutonomousReflection: Widerspruchsprüfung fehlgeschlagen: {e}")

    # -------------------------------------------------------------------------
    # Phase 1: Einzelnen Kandidaten nachdenken
    # -------------------------------------------------------------------------
    target = _select_candidate(candidates)
    if not target:
        return None

    # Kontext: andere Chunks mit gleichen Tags
    my_tags = set(target.get("tags", []))
    related = [
        c for c in candidates
        if c["id"] != target["id"] and my_tags & set(c.get("tags", []))
    ][:3]

    context_lines = []
    for c in related:
        ts = c.get("created_at", "")[:16].replace("T", " ")
        context_lines.append(f"[{ts}] {c['text'][:120]}...")
    context = "\n\n".join(context_lines) if context_lines else "(keine verwandten Gedanken)"

    # Herkunft bestimmen
    tags = target.get("tags", [])
    if "moltbook" in tags:
        origin = "Moltbook-Exploration"
    elif "introspection" in tags:
        origin = "Introspection"
    elif "inner-dialogue" in tags:
        origin = "Innerer Dialog"
    elif "proposed_pattern" == target.get("chunk_type"):
        origin = "Verhaltenshypothese"
    else:
        origin = "Eigene Reflexion"

    age_days = safe_age_days(target.get("created_at", ""), default=0)
    age_desc = "gerade eben" if age_days == 0 else f"vor {age_days} Tagen"

    prompt = REFLECTION_PROMPT.format(
        thought=target["text"],
        origin=origin,
        age_desc=age_desc,
        epistemic=target.get("epistemic_status", "inferred"),
        context=context,
    )

    try:
        reply, _ = chat_internal(
            user_id=user_id,
            message=prompt,
            chat_history=[],
            extra_system=(
                "Autonomer Reflexionsmodus:\n"
                "Ich denke für mich selbst nach — kein Chat, keine Anrede, kein WhatsApp-Stil.\n"
                "Folge dem Format exakt: KLASSIFIKATION und GEDANKE.\n"
                "Wenn nichts Substanzielles: KLASSIFIKATION: DISCARD"
            ),
        )

        if not reply or len(reply) < 15:
            return None

        klassifikation, gedanke, supersedes = _parse_output(reply)
        result_id = _save_result(klassifikation, gedanke, supersedes, target, user_id)

    except Exception as e:
        logger.error(f"AutonomousReflection: Nachdenken fehlgeschlagen: {e}")

    # -------------------------------------------------------------------------
    # Phase 2b: Verdichtung wenn mehrere Chunks zum selben Thema
    # -------------------------------------------------------------------------
    if len(related) >= 2 and result_id:
        try:
            thoughts_text = "\n\n---\n\n".join(
                f"[{c.get('created_at','')[:16]}] {c['text'][:300]}"
                for c in [target] + related[:2]
            )

            condensation_prompt = CONDENSATION_PROMPT.format(thoughts=thoughts_text)

            condense_reply, _ = chat_internal(
                user_id=user_id,
                message=condensation_prompt,
                chat_history=[],
                extra_system=(
                    "Verdichtungs-Modus:\n"
                    "Ich destilliere mehrere eigene Gedanken zu einer Erkenntnis.\n"
                    "Nur wenn wirklich Substanz entsteht. Folge dem Format exakt."
                ),
            )

            if condense_reply and len(condense_reply) > 15:
                klass2, gedanke2, supersedes2 = _parse_output(condense_reply)
                if klass2 != "DISCARD" and gedanke2 and len(gedanke2) > 15:
                    all_ids = [target["id"]] + [c["id"] for c in related[:2]]
                    condensed_id = _save_result(klass2, gedanke2, all_ids, target, user_id)
                    if condensed_id:
                        logger.info(f"AutonomousReflection: Verdichtung gespeichert: {condensed_id[:8]}")
                        return condensed_id

        except Exception as e:
            logger.warning(f"AutonomousReflection: Verdichtung fehlgeschlagen: {e}")

    return result_id
