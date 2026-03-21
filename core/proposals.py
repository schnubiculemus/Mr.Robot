"""
core/proposals.py — Kimis Proposals-System

Kimi kann Vorschläge für Programmierprojekte und Verbesserungen einreichen.
Format im Chat oder in der Kognition:

[PROPOSAL: {"title": "...", "description": "...", "effort": "klein|mittel|groß", "reason": "..."}]

Vorschläge landen als 'proposal'-Chunks in ChromaDB mit Status 'pending'.
Tommy kann im Dashboard approve/reject/defer.
Approve → automatisch als kimi-Todo angelegt.
"""

import re
import json
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

PROPOSAL_PATTERN = re.compile(r'\[PROPOSAL:\s*(\{.*?\})\s*\]', re.DOTALL)

VALID_EFFORTS = ("klein", "mittel", "groß", "gross")


def extract_proposals(text: str) -> tuple[str, list[dict]]:
    """
    Sucht [PROPOSAL: {...}] in Kimis Output.
    Gibt (cleaned_text, [proposal_dict, ...]) zurück.
    """
    matches = list(PROPOSAL_PATTERN.finditer(text))
    if not matches:
        return text, []

    cleaned = PROPOSAL_PATTERN.sub("", text).strip()
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned).strip()

    proposals = []
    for match in matches:
        try:
            raw = match.group(1).replace('\n', ' ').replace('\r', '')
            proposal = json.loads(raw)
            proposals.append(proposal)
        except json.JSONDecodeError as e:
            logger.warning(f"Proposal JSON parse error: {e} — raw: {match.group(1)[:100]}")

    return cleaned, proposals


def save_proposal(proposal: dict, source: str = "chat") -> str | None:
    """
    Speichert einen Vorschlag als Chunk in ChromaDB.
    Returns: chunk_id oder None bei Fehler.
    """
    try:
        from memory.memory_store import store_chunk
        from memory.chunk_schema import create_chunk

        title = proposal.get("title", "Unbenannter Vorschlag")[:100]
        description = proposal.get("description", "")[:500]
        effort = proposal.get("effort", "mittel").lower()
        if effort not in VALID_EFFORTS:
            effort = "mittel"
        reason = proposal.get("reason", "")[:300]

        text = f"PROPOSAL: {title}"
        if description:
            text += f"\n\nBeschreibung: {description}"
        if reason:
            text += f"\n\nBegründung: {reason}"
        text += f"\n\nAufwand: {effort}"

        chunk = create_chunk(
            text=text,
            chunk_type="decision",
            source="robot",
            confidence=0.8,
            epistemic_status="stated",
            tags=["proposal", "pending", f"effort:{effort}", f"source:{source}"],
        )
        # Extra-Metadaten für Dashboard
        chunk["metadata"]["proposal_status"] = "pending"
        chunk["metadata"]["proposal_title"] = title
        chunk["metadata"]["proposal_effort"] = effort

        store_chunk(chunk)
        logger.info(f"Proposal gespeichert: {chunk['id'][:8]} — '{title[:50]}'")
        return chunk["id"]

    except Exception as e:
        logger.warning(f"save_proposal fehlgeschlagen: {e}")
        return None


def get_proposals(status: str = "pending") -> list[dict]:
    """Lädt alle Proposals mit einem bestimmten Status aus ChromaDB."""
    try:
        from memory.memory_store import get_active_collection
        col = get_active_collection()
        results = col.get(
            where={"$and": [
                {"source": "robot"},
                {"chunk_type": "decision"},
            ]},
            include=["documents", "metadatas", "ids"],
        )
        proposals = []
        for i, doc in enumerate(results.get("documents") or []):
            meta = (results.get("metadatas") or [{}])[i]
            tags = str(meta.get("tags", ""))
            if "proposal" not in tags:
                continue
            if status and f"pending" not in tags and status == "pending":
                continue
            if status and status not in tags and status != "pending":
                continue
            proposals.append({
                "id": (results.get("ids") or [""])[i],
                "text": doc,
                "title": meta.get("proposal_title", ""),
                "effort": meta.get("proposal_effort", "mittel"),
                "status": meta.get("proposal_status", "pending"),
                "created_at": meta.get("created_at", ""),
                "tags": tags,
            })
        proposals.sort(key=lambda p: p["created_at"], reverse=True)
        return proposals
    except Exception as e:
        logger.warning(f"get_proposals fehlgeschlagen: {e}")
        return []


def approve_proposal(chunk_id: str, user_id: str) -> bool:
    """Genehmigt einen Vorschlag — legt automatisch ein kimi-Todo an."""
    try:
        from memory.memory_store import get_active_collection
        from core.todos import create_todo
        from core.datetime_utils import now_berlin
        from datetime import timedelta

        col = get_active_collection()
        result = col.get(ids=[chunk_id], include=["documents", "metadatas"])
        if not result["ids"]:
            return False

        meta = result["metadatas"][0]
        title = meta.get("proposal_title", "Proposal")
        effort = meta.get("proposal_effort", "mittel")

        # Status updaten
        col.update(
            ids=[chunk_id],
            metadatas=[{**meta, "proposal_status": "approved", "tags": str(meta.get("tags", "")).replace("pending", "approved")}]
        )

        # kimi-Todo anlegen
        due = (now_berlin() + timedelta(days=3)).strftime("%Y-%m-%d")
        create_todo(
            user_id=user_id,
            title=f"[Approved] {title}",
            description=f"Genehmigter Proposal. Aufwand: {effort}",
            priority="mittel",
            project="kimi",
            due_date=due,
        )
        logger.info(f"Proposal approved: {chunk_id[:8]} — '{title[:50]}'")
        return True
    except Exception as e:
        logger.warning(f"approve_proposal fehlgeschlagen: {e}")
        return False


def reject_proposal(chunk_id: str) -> bool:
    """Lehnt einen Vorschlag ab."""
    try:
        from memory.memory_store import get_active_collection
        col = get_active_collection()
        result = col.get(ids=[chunk_id], include=["metadatas"])
        if not result["ids"]:
            return False
        meta = result["metadatas"][0]
        col.update(
            ids=[chunk_id],
            metadatas=[{**meta, "proposal_status": "rejected", "tags": str(meta.get("tags", "")).replace("pending", "rejected")}]
        )
        logger.info(f"Proposal rejected: {chunk_id[:8]}")
        return True
    except Exception as e:
        logger.warning(f"reject_proposal fehlgeschlagen: {e}")
        return False


def defer_proposal(chunk_id: str) -> bool:
    """Stellt einen Vorschlag zurück."""
    try:
        from memory.memory_store import get_active_collection
        col = get_active_collection()
        result = col.get(ids=[chunk_id], include=["metadatas"])
        if not result["ids"]:
            return False
        meta = result["metadatas"][0]
        col.update(
            ids=[chunk_id],
            metadatas=[{**meta, "proposal_status": "deferred", "tags": str(meta.get("tags", "")).replace("pending", "deferred")}]
        )
        logger.info(f"Proposal deferred: {chunk_id[:8]}")
        return True
    except Exception as e:
        logger.warning(f"defer_proposal fehlgeschlagen: {e}")
        return False
