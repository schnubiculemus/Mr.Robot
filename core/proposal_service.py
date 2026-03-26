"""
core/proposal_service.py — LEGACY_COMPAT (WP10) — delete_candidate

Status: INAKTIV. Alle Funktionen sind no-ops.
Durch core/proposal_service_wp10.py ersetzt.

Diese Datei existiert nur noch damit Import-Fehler in Altpfaden
nicht zu Abstürzen führen. Keine Funktion tut noch etwas.

V2-Hauptpfad: core/proposal_service_wp10.py + wp10_proposals
"""
import logging
logger = logging.getLogger(__name__)

VALID_STATUSES = ("pending", "approved", "deferred", "rejected", "implemented")
VALID_EFFORTS = ("klein", "mittel", "groß", "gross")

_LEGACY_MSG = "legacy_compat WP10: proposal_service.py ist inaktiv — nutze proposal_service_wp10.py"


def create_proposal(*args, **kwargs):
    """WP10: no-op. Nutze core.proposal_service_wp10.create_proposal()."""
    logger.debug(_LEGACY_MSG)
    return None


def get_proposal(proposal_id: int):
    """WP10: no-op."""
    logger.debug(_LEGACY_MSG)
    return None


def get_proposals(status: str = "pending", owner_id: str = None) -> list:
    """WP10: no-op — gibt leere Liste zurück."""
    logger.debug(_LEGACY_MSG)
    return []


def approve_proposal(proposal_id: int, owner_id: str):
    """WP10: no-op. Kein automatisches Todo mehr."""
    logger.debug(_LEGACY_MSG)
    return None


def reject_proposal(proposal_id: int, reason: str = None) -> bool:
    """WP10: no-op."""
    logger.debug(_LEGACY_MSG)
    return False


def defer_proposal(proposal_id: int) -> bool:
    """WP10: no-op."""
    logger.debug(_LEGACY_MSG)
    return False


def mark_implemented(proposal_id: int, task_id: str = None) -> bool:
    """WP10: no-op. kein implemented-Status mehr."""
    logger.debug(_LEGACY_MSG)
    return False


def set_last_error(proposal_id: int, error: str) -> None:
    """WP10: no-op."""
    logger.debug(_LEGACY_MSG)
