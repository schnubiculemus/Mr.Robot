"""
core/proposals.py — LEGACY_COMPAT (WP10) — delete_candidate

Status: INAKTIV. Alle Funktionen sind no-ops.
Durch core/proposal_service_wp10.py + core/kimi_output.py [WP10_PROPOSAL:] ersetzt.

[PROPOSAL: {...}] wird nicht mehr aktiv verarbeitet.
Nutze [WP10_PROPOSAL: {...}] im Chat.
"""
import logging
import re

logger = logging.getLogger(__name__)

# Pattern bleibt definiert damit Import nicht bricht — aber nie aktiv genutzt
PROPOSAL_PATTERN = re.compile(r'\[PROPOSAL:\s*(\{.*?\})\s*\]', re.DOTALL)

_LEGACY_MSG = "legacy_compat WP10: proposals.py ist inaktiv — nutze [WP10_PROPOSAL:]"


def extract_proposals(text: str) -> tuple:
    """WP10: no-op. Entfernt alten Marker still, erzeugt keine Actions."""
    cleaned = PROPOSAL_PATTERN.sub("", text).strip()
    return cleaned, []


def save_proposal(proposal: dict, source: str = "chat", user_id: str = "") -> None:
    """WP10: no-op."""
    logger.debug(_LEGACY_MSG)
    return None


def get_proposals(status: str = "pending", user_id: str = None) -> list:
    """WP10: no-op."""
    logger.debug(_LEGACY_MSG)
    return []


def approve_proposal(proposal_id: int, owner_id: str) -> bool:
    """WP10: no-op."""
    logger.debug(_LEGACY_MSG)
    return False


def reject_proposal(proposal_id: int) -> bool:
    """WP10: no-op."""
    logger.debug(_LEGACY_MSG)
    return False


def defer_proposal(proposal_id: int) -> bool:
    """WP10: no-op."""
    logger.debug(_LEGACY_MSG)
    return False
