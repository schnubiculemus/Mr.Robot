"""
core/proposals.py — Proposals (Thin Wrapper über proposal_service)

Parsing-Logik bleibt hier.
Alle Writes gehen über proposal_service.py.
"""
import re
import json
import logging

logger = logging.getLogger(__name__)

PROPOSAL_PATTERN = re.compile(r'\[PROPOSAL:\s*(\{.*?\})\s*\]', re.DOTALL)


def extract_proposals(text: str) -> tuple[str, list[dict]]:
    matches = list(PROPOSAL_PATTERN.finditer(text))
    if not matches:
        return text, []

    cleaned = PROPOSAL_PATTERN.sub("", text).strip()
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned).strip()

    proposals = []
    for match in matches:
        try:
            raw = match.group(1).replace('\n', ' ').replace('\r', '')
            proposals.append(json.loads(raw))
        except json.JSONDecodeError as e:
            logger.warning(f"Proposal JSON parse error: {e}")

    return cleaned, proposals


def save_proposal(proposal: dict, source: str = "chat", user_id: str = "") -> int | None:
    """Speichert Proposal via proposal_service. Gibt ID zurück oder None."""
    from core.proposal_service import create_proposal
    result = create_proposal(
        owner_id=user_id,
        title=proposal.get("title", "Unbenannter Vorschlag"),
        description=proposal.get("description"),
        reason=proposal.get("reason"),
        effort=proposal.get("effort", "mittel"),
        source_type=source,
        confidence=proposal.get("confidence", 1.0),
    )
    return result["id"] if result else None


def get_proposals(status: str = "pending", user_id: str = None) -> list:
    from core.proposal_service import get_proposals as _get
    return _get(status=status)


def approve_proposal(proposal_id: int, owner_id: str) -> bool:
    from core.proposal_service import approve_proposal as _approve
    result = _approve(proposal_id, owner_id)
    return result is not None


def reject_proposal(proposal_id: int) -> bool:
    from core.proposal_service import reject_proposal as _reject
    return _reject(proposal_id)


def defer_proposal(proposal_id: int) -> bool:
    from core.proposal_service import defer_proposal as _defer
    return _defer(proposal_id)
