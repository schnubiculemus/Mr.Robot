"""
active_working_context.py — WP2: Active Working Context

Der verbindliche Primäranker für laufende Arbeit.
Kimi Core liest diesen Kontext VOR breiterem Memory-Zugriff.

Regel: genau ein aktiver Kontext gleichzeitig (id=1, CHECK-Constraint).
Kontextwechsel: Kimi darf vorschlagen, nicht selbst vollziehen.
"""

import logging
from core.datetime_utils import to_iso

logger = logging.getLogger(__name__)

# Pflichtfelder
AWC_FIELDS = [
    "active_line",
    "active_goal",
    "active_document",
    "last_clean_state",
    "last_decision",
    "next_open_question",
]


def get_active_context(owner_id: str) -> dict | None:
    """
    Liest den aktiven Arbeitskontext.
    Gibt None zurück wenn kein Kontext gesetzt ist.
    """
    try:
        from core.database import get_connection
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT * FROM active_working_context WHERE owner_id=? LIMIT 1",
                (owner_id,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"get_active_context fehlgeschlagen: {e}")
        return None


def set_active_context(owner_id: str, **fields) -> bool:
    """
    Setzt den aktiven Arbeitskontext vollständig (ersetzt alten).
    Genau ein Kontext gleichzeitig (id=1).
    """
    try:
        from core.database import get_connection
        conn = get_connection()
        now = to_iso()
        try:
            conn.execute(
                """INSERT INTO active_working_context
                   (id, owner_id, active_line, active_goal, active_document,
                    last_clean_state, last_decision, next_open_question,
                    proposed_switch_to, proposed_switch_reason, proposed_switch_confirmed,
                    updated_at, created_at)
                   VALUES (1, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, 0, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                     owner_id=excluded.owner_id,
                     active_line=excluded.active_line,
                     active_goal=excluded.active_goal,
                     active_document=excluded.active_document,
                     last_clean_state=excluded.last_clean_state,
                     last_decision=excluded.last_decision,
                     next_open_question=excluded.next_open_question,
                     proposed_switch_to=NULL,
                     proposed_switch_reason=NULL,
                     proposed_switch_confirmed=0,
                     updated_at=excluded.updated_at""",
                (owner_id,
                 fields.get("active_line"),
                 fields.get("active_goal"),
                 fields.get("active_document"),
                 fields.get("last_clean_state"),
                 fields.get("last_decision"),
                 fields.get("next_open_question"),
                 now, now)
            )
            conn.commit()
            logger.info(f"AWC gesetzt: line={fields.get('active_line','?')[:60]}")
            return True
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"set_active_context fehlgeschlagen: {e}")
        return False


def update_active_context(owner_id: str, **fields) -> bool:
    """
    Aktualisiert einzelne Felder des aktiven Kontexts.
    Legt neuen an falls noch keiner existiert.
    """
    existing = get_active_context(owner_id)
    if not existing:
        return set_active_context(owner_id, **fields)
    try:
        from core.database import get_connection
        conn = get_connection()
        now = to_iso()
        try:
            # Nur übergebene Felder aktualisieren
            allowed = AWC_FIELDS + ["proposed_switch_to", "proposed_switch_reason",
                                     "proposed_switch_confirmed"]
            updates = {k: v for k, v in fields.items() if k in allowed}
            if not updates:
                return True
            set_clause = ", ".join(f"{k}=?" for k in updates)
            values = list(updates.values()) + [now, owner_id]
            conn.execute(
                f"UPDATE active_working_context SET {set_clause}, updated_at=? WHERE owner_id=?",
                values
            )
            conn.commit()
            logger.debug(f"AWC aktualisiert: {list(updates.keys())}")
            return True
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"update_active_context fehlgeschlagen: {e}")
        return False


def clear_active_context(owner_id: str) -> bool:
    """Löscht den aktiven Kontext."""
    try:
        from core.database import get_connection
        conn = get_connection()
        try:
            conn.execute("DELETE FROM active_working_context WHERE owner_id=?", (owner_id,))
            conn.commit()
            logger.info(f"AWC gelöscht für {owner_id[:20]}")
            return True
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"clear_active_context fehlgeschlagen: {e}")
        return False


def propose_context_switch(owner_id: str, new_line: str, reason: str) -> bool:
    """
    Kimi schlägt einen Kontextwechsel vor.
    Wechsel wird NICHT vollzogen -- nur vorgemerkt.
    Nutzer oder Core müssen explizit bestätigen.
    """
    return update_active_context(
        owner_id,
        proposed_switch_to=new_line,
        proposed_switch_reason=reason,
        proposed_switch_confirmed=0,
    )


def confirm_context_switch(owner_id: str) -> bool:
    """
    Bestätigt einen vorgeschlagenen Kontextwechsel.
    Setzt proposed_switch_to als neue active_line.
    """
    ctx = get_active_context(owner_id)
    if not ctx or not ctx.get("proposed_switch_to"):
        return False
    new_line = ctx["proposed_switch_to"]
    reason = ctx.get("proposed_switch_reason", "")
    return set_active_context(
        owner_id,
        active_line=new_line,
        active_goal="",
        active_document="",
        last_clean_state="",
        last_decision=f"Kontextwechsel bestätigt: {reason[:100]}",
        next_open_question="",
    )


def format_for_prompt(ctx: dict) -> str:
    """
    Formatiert den Active Working Context für den System-Prompt.
    Kimi Core injiziert das vor Memory-Kontext.
    """
    if not ctx:
        return ""
    lines = ["## Aktiver Arbeitskontext"]
    if ctx.get("active_line"):
        lines.append(f"**Linie:** {ctx['active_line']}")
    if ctx.get("active_goal"):
        lines.append(f"**Ziel:** {ctx['active_goal']}")
    if ctx.get("active_document"):
        lines.append(f"**Dokument:** {ctx['active_document']}")
    if ctx.get("last_clean_state"):
        lines.append(f"**Letzter Stand:** {ctx['last_clean_state']}")
    if ctx.get("last_decision"):
        lines.append(f"**Letzte Entscheidung:** {ctx['last_decision']}")
    if ctx.get("next_open_question"):
        lines.append(f"**Offene Frage:** {ctx['next_open_question']}")
    if ctx.get("proposed_switch_to") and not ctx.get("proposed_switch_confirmed"):
        lines.append(f"\n⚠️ Vorgeschlagener Wechsel zu: {ctx['proposed_switch_to']}")
        if ctx.get("proposed_switch_reason"):
            lines.append(f"   Grund: {ctx['proposed_switch_reason']}")
        lines.append("   Noch nicht bestätigt.")
    return "\n".join(lines)
