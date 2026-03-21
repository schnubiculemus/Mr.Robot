"""
core/proposals.py — Kimis Proposals-System

Proposals leben in SQLite (kimi_proposals Tabelle) — nicht in ChromaDB.
ChromaDB ist für Memory-Chunks. SQLite ist für operative Objekte.

Kimi reicht Proposals ein via:
[PROPOSAL: {"title": "...", "description": "...", "effort": "klein|mittel|groß", "reason": "..."}]

Tommy sieht sie im Dashboard unter /proposals.
Approve → Todo anlegen. Reject → abgelehnt. Defer → zurückgestellt.
"""

import re
import json
import logging
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

PROPOSAL_PATTERN = re.compile(r'\[PROPOSAL:\s*(\{.*?\})\s*\]', re.DOTALL)
VALID_EFFORTS = ("klein", "mittel", "groß", "gross", "gros")


def extract_proposals(text: str) -> tuple[str, list[dict]]:
    """Sucht [PROPOSAL: {...}] in Kimis Output. Gibt (cleaned_text, proposals) zurück."""
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
            logger.warning(f"Proposal JSON parse error: {e}")

    return cleaned, proposals


def save_proposal(proposal: dict, source: str = "chat", user_id: str = "") -> int | None:
    """
    Speichert einen Proposal in SQLite.
    Returns: proposal.id (int) oder None bei Fehler.
    """
    try:
        from core.database import get_connection, init_kimi_proposals_table
        from core.datetime_utils import to_iso

        init_kimi_proposals_table()

        title = proposal.get("title", "Unbenannter Vorschlag")[:200]
        description = proposal.get("description", "")[:1000]
        reason = proposal.get("reason", "")[:500]
        effort = proposal.get("effort", "mittel").lower()
        if effort not in VALID_EFFORTS:
            effort = "mittel"

        conn = get_connection()
        try:
            cur = conn.execute(
                """INSERT INTO kimi_proposals
                   (user_id, title, description, reason, effort, status, source_module, created_at)
                   VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)""",
                (user_id, title, description, reason, effort, source, to_iso())
            )
            conn.commit()
            proposal_id = cur.lastrowid
            logger.info(f"Proposal gespeichert: #{proposal_id} — '{title[:50]}'")
            return proposal_id
        finally:
            conn.close()

    except Exception as e:
        logger.warning(f"save_proposal fehlgeschlagen: {e}")
        return None


def get_proposals(status: str = "pending", user_id: str = None) -> list[dict]:
    """Lädt Proposals aus SQLite."""
    try:
        from core.database import get_connection, init_kimi_proposals_table
        init_kimi_proposals_table()
        conn = get_connection()
        try:
            if status and status != "all":
                rows = conn.execute(
                    "SELECT * FROM kimi_proposals WHERE status=? ORDER BY created_at DESC",
                    (status,)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM kimi_proposals ORDER BY created_at DESC"
                ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"get_proposals fehlgeschlagen: {e}")
        return []


def approve_proposal(proposal_id: int, user_id: str) -> bool:
    """Genehmigt einen Proposal und legt automatisch ein kimi-Todo an."""
    try:
        from core.database import get_connection
        from core.datetime_utils import to_iso
        from core.todos import create_todo

        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT * FROM kimi_proposals WHERE id=?", (proposal_id,)
            ).fetchone()
            if not row:
                return False
            proposal = dict(row)

            # kimi-Todo anlegen
            due = (datetime.now(timezone.utc) + timedelta(days=3)).strftime("%Y-%m-%d")
            todo = create_todo(
                user_id=user_id,
                title=f"[Approved] {proposal['title']}",
                description=f"Genehmigter Proposal. Aufwand: {proposal['effort']}. {proposal.get('description','')[:200]}",
                priority="mittel",
                project="kimi",
                due_date=due,
            )

            # Proposal updaten
            conn.execute(
                "UPDATE kimi_proposals SET status='approved', approved_at=?, approved_todo_id=? WHERE id=?",
                (to_iso(), todo["id"] if todo else None, proposal_id)
            )
            conn.commit()
            logger.info(f"Proposal #{proposal_id} approved → Todo #{todo['id'] if todo else '?'}")
            return True
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"approve_proposal fehlgeschlagen: {e}")
        return False


def reject_proposal(proposal_id: int) -> bool:
    """Lehnt einen Proposal ab."""
    try:
        from core.database import get_connection
        from core.datetime_utils import to_iso
        conn = get_connection()
        try:
            conn.execute(
                "UPDATE kimi_proposals SET status='rejected', rejected_at=? WHERE id=?",
                (to_iso(), proposal_id)
            )
            conn.commit()
            logger.info(f"Proposal #{proposal_id} rejected")
            return True
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"reject_proposal fehlgeschlagen: {e}")
        return False


def defer_proposal(proposal_id: int) -> bool:
    """Stellt einen Proposal zurück."""
    try:
        from core.database import get_connection
        conn = get_connection()
        try:
            conn.execute(
                "UPDATE kimi_proposals SET status='deferred' WHERE id=?",
                (proposal_id,)
            )
            conn.commit()
            logger.info(f"Proposal #{proposal_id} deferred")
            return True
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"defer_proposal fehlgeschlagen: {e}")
        return False
