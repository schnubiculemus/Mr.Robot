"""
core/proposal_service.py — Proposals Service

Einzige Stelle die Proposals schreibt und Status-Übergänge durchführt.
Alle Übergänge laufen in Transaktionen.
"""
import logging
from core.datetime_utils import to_iso
from core.database import get_connection, init_kimi_b_schema

logger = logging.getLogger(__name__)

VALID_STATUSES = ("pending", "approved", "deferred", "rejected", "implemented")
VALID_EFFORTS = ("klein", "mittel", "groß", "gross")


def create_proposal(owner_id: str, title: str, description: str = None,
                    reason: str = None, effort: str = "mittel",
                    source_type: str = "chat", source_ref: str = None,
                    goal_id: int = None, confidence: float = 1.0) -> dict | None:
    try:
        init_kimi_b_schema()
        effort = effort.lower() if effort else "mittel"
        if effort not in VALID_EFFORTS:
            effort = "mittel"
        now = to_iso()
        conn = get_connection()
        try:
            cur = conn.execute(
                """INSERT INTO kimi_proposals
                   (owner_id, goal_id, title, description, reason, effort,
                    status, source_type, source_ref, confidence, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,'pending',?,?,?,?,?)""",
                (owner_id, goal_id, title[:200], description, reason, effort,
                 source_type, source_ref, confidence, now, now)
            )
            conn.commit()
            proposal_id = cur.lastrowid
            logger.info(f"Proposal #{proposal_id} erstellt: '{title[:50]}' [{source_type}]")
            return get_proposal(proposal_id)
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"create_proposal fehlgeschlagen: {e}")
        return None


def get_proposal(proposal_id: int) -> dict | None:
    try:
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT * FROM kimi_proposals WHERE id=?", (proposal_id,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"get_proposal fehlgeschlagen: {e}")
        return None


def get_proposals(status: str = "pending", owner_id: str = None) -> list:
    try:
        conn = get_connection()
        try:
            if owner_id and status and status != "all":
                rows = conn.execute(
                    "SELECT * FROM kimi_proposals WHERE status=? AND owner_id=? ORDER BY created_at DESC",
                    (status, owner_id)
                ).fetchall()
            elif owner_id:
                rows = conn.execute(
                    "SELECT * FROM kimi_proposals WHERE owner_id=? ORDER BY created_at DESC",
                    (owner_id,)
                ).fetchall()
            elif status and status != "all":
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


def approve_proposal(proposal_id: int, owner_id: str) -> dict | None:
    """
    Genehmigt einen Proposal — kompensierend abgesichert:
    1. Proposal lesen + Status prüfen (Idempotenz)
    2. Todo anlegen via todo_service
    3. Proposal + Completion-Event in einer DB-Transaktion updaten
    4. Bei Fehler in Schritt 3: Todo wieder löschen (Kompensation)
    """
    try:
        from core.todo_service import create_todo
        from core.todos import delete_todo
        from datetime import datetime, timezone, timedelta
        now = to_iso()
        due = (datetime.now(timezone.utc) + timedelta(days=3)).strftime("%Y-%m-%d")

        # Proposal lesen
        proposal = get_proposal(proposal_id)
        if not proposal:
            logger.warning(f"approve_proposal: Proposal #{proposal_id} nicht gefunden")
            return None

        # Idempotenz: schon approved?
        if proposal.get("approved_todo_id"):
            logger.info(f"approve_proposal: Proposal #{proposal_id} bereits approved")
            return proposal

        # Todo anlegen
        todo = create_todo(
            owner_id=owner_id,
            title=f"[Approved] {proposal['title']}",
            description=f"Aus Proposal #{proposal_id}: {proposal.get('description','')[:300]}",
            priority="mittel",
            project="kimi",
            due_date=due,
            origin_type="proposal",
            origin_ref=str(proposal_id),
            proposal_id=proposal_id,
            goal_id=proposal.get("goal_id"),
        )
        if not todo:
            logger.warning(f"approve_proposal: Todo-Anlage fehlgeschlagen")
            return None

        # Proposal + Completion in einer Transaktion
        try:
            conn = get_connection()
            try:
                conn.execute(
                    """UPDATE kimi_proposals
                       SET status='approved', approved_at=?, approved_todo_id=?, updated_at=?
                       WHERE id=?""",
                    (now, todo["id"], now, proposal_id)
                )
                conn.execute(
                    """INSERT INTO kimi_completions
                       (owner_id, for_object_type, for_object_id, reason, summary, created_at)
                       VALUES (?,?,?,?,?,?)""",
                    (owner_id, "proposal", str(proposal_id),
                     "approved", f"Todo #{todo['id']} angelegt", now)
                )
                conn.commit()
            finally:
                conn.close()
        except Exception as _db_err:
            # Kompensation: Todo wieder löschen da Proposal-Update fehlschlug
            logger.warning(f"approve_proposal: DB-Update fehlgeschlagen, kompensiere: {_db_err}")
            try:
                delete_todo(todo["id"])
            except Exception:
                pass
            return None

        logger.info(f"Proposal #{proposal_id} approved → Todo #{todo['id']}")
        return get_proposal(proposal_id)
    except Exception as e:
        logger.warning(f"approve_proposal fehlgeschlagen: {e}")
        return None


def reject_proposal(proposal_id: int, reason: str = None) -> bool:
    try:
        now = to_iso()
        conn = get_connection()
        try:
            conn.execute(
                "UPDATE kimi_proposals SET status='rejected', rejected_at=?, updated_at=? WHERE id=?",
                (now, now, proposal_id)
            )
            conn.execute(
                """INSERT INTO kimi_completions
                   (owner_id, for_object_type, for_object_id, reason, created_at)
                   VALUES ('',?,?,?,?)""",
                ("proposal", str(proposal_id), reason or "rejected", now)
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
    try:
        now = to_iso()
        conn = get_connection()
        try:
            conn.execute(
                "UPDATE kimi_proposals SET status='deferred', updated_at=? WHERE id=?",
                (now, proposal_id)
            )
            conn.commit()
            return True
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"defer_proposal fehlgeschlagen: {e}")
        return False


def mark_implemented(proposal_id: int, task_id: str = None) -> bool:
    """Wird aufgerufen wenn der zugehörige Task erfolgreich abgeschlossen wurde."""
    try:
        now = to_iso()
        conn = get_connection()
        try:
            updates = {"status": "implemented", "implemented_at": now, "updated_at": now}
            if task_id:
                updates["approved_task_id"] = task_id
            fields = ", ".join(f"{k}=?" for k in updates)
            conn.execute(
                f"UPDATE kimi_proposals SET {fields} WHERE id=?",
                list(updates.values()) + [proposal_id]
            )
            conn.execute(
                """INSERT INTO kimi_completions
                   (owner_id, for_object_type, for_object_id, reason, created_at)
                   VALUES ('',?,?,?,?)""",
                ("proposal", str(proposal_id), "implemented", now)
            )
            conn.commit()
            logger.info(f"Proposal #{proposal_id} implemented")

            # Goal-Fortschritt aktualisieren
            proposal = get_proposal(proposal_id)
            if proposal and proposal.get("goal_id"):
                from core.goal_service import update_goal_progress, get_goal
                goal = get_goal(proposal["goal_id"])
                if goal:
                    new_progress = min(100, goal.get("progress", 0) + 20)
                    update_goal_progress(proposal["goal_id"], new_progress, f"Proposal #{proposal_id} implemented")
            return True
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"mark_implemented fehlgeschlagen: {e}")
        return False
