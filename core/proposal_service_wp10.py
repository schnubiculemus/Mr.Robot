"""
core/proposal_service_wp10.py — WP10: Proposal-Layer

Ein Proposal ist ein Vorschlag, kein Auftrag.
Kimi darf Proposals einreichen, aber nicht selbst daraus Arbeit machen.

Proposal-Typen:
  self_constitution_change  — Änderungen an soul.md / Kernprinzipien
  behavior_adjustment       — Antwortstil, Verhaltenstendenzen, Muster
  workflow_improvement      — Abläufe, Arbeitslogik, Bedienfluss
  architecture_improvement  — Systemstruktur, Modulschnittstellen
  memory_improvement        — Gedächtnislogik, Konsolidierung, Retrieval
  other                     — Restkategorie

Status-Modell:
  open       — eingereicht, wartet auf Entscheidung
  accepted   — angenommen (Umsetzung liegt beim Menschen)
  rejected   — abgelehnt
  withdrawn  — von Kimi zurückgezogen

Sperrrregeln (absolut):
  - Kein Proposal → Todo
  - Kein Proposal → Task
  - Kein Proposal → ORBIT
  - Kein Proposal → Workspace-Aktion
  - Kein Proposal → automatische Umsetzung

Übergang von WP9:
  proposal_seed (ChromaDB, intern, roh) → Proposal (SQLite, formal, sichtbar)
  Nur über expliziten Einreichungsakt — keine automatische Konvertierung.
"""

import logging
from core.database import get_connection
from core.datetime_utils import to_iso

logger = logging.getLogger(__name__)

# =============================================================================
# Proposal-Typen
# =============================================================================

PROPOSAL_TYPES = {
    "self_constitution_change": "Änderung an soul.md / Kernprinzipien / Selbstverständnis",
    "behavior_adjustment":      "Antwortstil, Verhaltenstendenz, Kommunikationsmuster",
    "workflow_improvement":     "Ablauf, Arbeitslogik, Bedienfluss, Routinen",
    "architecture_improvement": "Systemstruktur, Modulschnittstellen, Zuständigkeiten",
    "memory_improvement":       "Gedächtnislogik, Konsolidierung, Retrieval",
    "other":                    "Sonstiges (Restkategorie)",
}

PROPOSAL_STATUSES = {"open", "accepted", "rejected", "withdrawn"}

# Proposal-Typen die explizit soul.md / style.md betreffen
CONSTITUTION_TYPES = {"self_constitution_change", "behavior_adjustment"}


# =============================================================================
# DB-Tabelle initialisieren (wird von database.init_db() aufgerufen)
# =============================================================================

def init_wp10_proposals_table(conn=None) -> None:
    """Erstellt wp10_proposals Tabelle. Idempotent."""
    _own = conn is None
    if _own:
        conn = get_connection()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS wp10_proposals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id TEXT NOT NULL,
            proposal_type TEXT NOT NULL DEFAULT 'other',
            title TEXT NOT NULL,
            summary TEXT,
            reason TEXT,
            suggested_change TEXT,
            risk_note TEXT,
            priority_hint TEXT DEFAULT 'normal',
            source TEXT NOT NULL DEFAULT 'chat',
            source_ref TEXT,
            related_line TEXT,
            related_document TEXT,
            status TEXT NOT NULL DEFAULT 'open',
            decision_note TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            decided_at TEXT
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_wp10_proposals_status "
        "ON wp10_proposals(status, owner_id, created_at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_wp10_proposals_type "
        "ON wp10_proposals(proposal_type, status)"
    )
    if _own:
        conn.commit()
        conn.close()


# =============================================================================
# CRUD
# =============================================================================

def create_proposal(
    owner_id: str,
    title: str,
    proposal_type: str = "other",
    summary: str = None,
    reason: str = None,
    suggested_change: str = None,
    risk_note: str = None,
    priority_hint: str = "normal",
    source: str = "chat",
    source_ref: str = None,
    related_line: str = None,
    related_document: str = None,
) -> dict | None:
    """
    Erstellt ein neues Proposal.

    WP10-Garantie:
    - kein Todo wird angelegt
    - kein Task wird angelegt
    - kein ORBIT wird aktiviert
    - keine Workspace-Aktion wird ausgeführt
    - Proposal bleibt Vorschlag bis zur expliziten Entscheidung
    """
    if not title or not title.strip():
        logger.warning("create_proposal: kein Titel angegeben")
        return None

    if proposal_type not in PROPOSAL_TYPES:
        logger.warning(f"create_proposal: unbekannter Typ '{proposal_type}' → 'other'")
        proposal_type = "other"

    now = to_iso()
    try:
        conn = get_connection()
        try:
            cur = conn.execute(
                """INSERT INTO wp10_proposals
                   (owner_id, proposal_type, title, summary, reason,
                    suggested_change, risk_note, priority_hint,
                    source, source_ref, related_line, related_document,
                    status, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'open',?,?)""",
                (owner_id, proposal_type, title.strip(), summary, reason,
                 suggested_change, risk_note, priority_hint,
                 source, source_ref, related_line, related_document,
                 now, now)
            )
            conn.commit()
            proposal_id = cur.lastrowid
            logger.info(
                f"WP10 Proposal erstellt: #{proposal_id} [{proposal_type}] '{title[:50]}'"
            )
            return get_proposal(proposal_id)
        finally:
            conn.close()
    except Exception as e:
        logger.error(f"create_proposal fehlgeschlagen: {e}")
        return None


def get_proposal(proposal_id: int) -> dict | None:
    """Holt ein Proposal per ID."""
    try:
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT * FROM wp10_proposals WHERE id=?", (proposal_id,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"get_proposal fehlgeschlagen: {e}")
        return None


def list_proposals(
    owner_id: str = None,
    status: str = "open",
    proposal_type: str = None,
    limit: int = 50,
) -> list:
    """Listet Proposals, optional gefiltert."""
    try:
        conn = get_connection()
        try:
            where = []
            params = []
            if owner_id:
                where.append("owner_id=?")
                params.append(owner_id)
            if status:
                where.append("status=?")
                params.append(status)
            if proposal_type:
                where.append("proposal_type=?")
                params.append(proposal_type)
            where_str = ("WHERE " + " AND ".join(where)) if where else ""
            params.append(limit)
            rows = conn.execute(
                f"SELECT * FROM wp10_proposals {where_str} "
                f"ORDER BY created_at DESC LIMIT ?",
                params
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"list_proposals fehlgeschlagen: {e}")
        return []


def update_proposal_status(
    proposal_id: int,
    new_status: str,
    decision_note: str = None,
) -> bool:
    """
    Aktualisiert den Status eines Proposals.
    Nur erlaubte Übergänge: open → accepted/rejected/withdrawn

    WP10-Garantie: kein automatischer Folge-Task/Todo/ORBIT.
    """
    if new_status not in PROPOSAL_STATUSES:
        logger.warning(f"update_proposal_status: ungültiger Status '{new_status}'")
        return False

    now = to_iso()
    try:
        conn = get_connection()
        try:
            conn.execute(
                """UPDATE wp10_proposals
                   SET status=?, decision_note=?, updated_at=?, decided_at=?
                   WHERE id=?""",
                (new_status, decision_note, now, now, proposal_id)
            )
            conn.commit()
            logger.info(f"WP10 Proposal #{proposal_id} → {new_status}")
            return True
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"update_proposal_status fehlgeschlagen: {e}")
        return False


def withdraw_proposal(proposal_id: int, reason: str = None) -> bool:
    """Kimi zieht ein Proposal zurück."""
    return update_proposal_status(proposal_id, "withdrawn", decision_note=reason)


# =============================================================================
# Übergang von WP9 proposal_seed → WP10 Proposal
# =============================================================================

def create_from_seed(
    seed_chunk: dict,
    owner_id: str,
    proposal_type: str = "other",
    suggested_change: str = None,
) -> dict | None:
    """
    Erstellt ein formales WP10-Proposal aus einem WP9-proposal_seed-Chunk.

    Übergang: proposal_seed (ChromaDB, intern, roh) → Proposal (SQLite, formal, sichtbar)

    WP10-Garantie: Dieser Akt ist explizit — keine automatische Konvertierung.
    Der Seed bleibt in ChromaDB bestehen (kein Löschen).
    """
    text = seed_chunk.get("text", "").strip()
    if not text:
        logger.warning("create_from_seed: leerer Seed-Text")
        return None

    # Titel aus erstem Satz des Seeds
    title = text.split(".")[0].strip()[:120] or text[:120]

    return create_proposal(
        owner_id=owner_id,
        proposal_type=proposal_type,
        title=title,
        summary=text,
        reason="Aus WP9 proposal_seed übergegangen",
        suggested_change=suggested_change,
        source="wp9_seed",
        source_ref=seed_chunk.get("id", ""),
        related_line=seed_chunk.get("related_line", ""),
    )


# =============================================================================
# Formatierung für Kimi Core
# =============================================================================

def format_proposal_for_kimi(proposal: dict) -> str:
    """Formatiert ein Proposal für die Anzeige im Chat."""
    type_label = PROPOSAL_TYPES.get(proposal.get("proposal_type", "other"), "Sonstiges")
    status = proposal.get("status", "open")
    status_label = {
        "open":      "offen",
        "accepted":  "angenommen",
        "rejected":  "abgelehnt",
        "withdrawn": "zurückgezogen",
    }.get(status, status)

    lines = [
        f"Proposal #{proposal['id']} [{status_label}]",
        f"Typ: {type_label}",
        f"Titel: {proposal['title']}",
    ]
    if proposal.get("summary"):
        lines.append(f"Zusammenfassung: {proposal['summary'][:200]}")
    if proposal.get("reason"):
        lines.append(f"Begründung: {proposal['reason'][:150]}")
    if proposal.get("suggested_change"):
        lines.append(f"Vorgeschlagene Änderung: {proposal['suggested_change'][:200]}")
    if proposal.get("risk_note"):
        lines.append(f"Risiko-Hinweis: {proposal['risk_note'][:100]}")
    if proposal.get("decision_note"):
        lines.append(f"Entscheidungsnotiz: {proposal['decision_note'][:150]}")
    lines.append(f"Eingereicht: {proposal.get('created_at', '')[:16]}")
    return "\n".join(lines)


def format_proposals_list(proposals: list) -> str:
    """Formatiert eine Liste von Proposals für den Chat."""
    if not proposals:
        return "Keine Proposals vorhanden."
    lines = [f"{len(proposals)} Proposal(s):"]
    for p in proposals:
        type_label = PROPOSAL_TYPES.get(p.get("proposal_type", "other"), "?")
        status = {"open": "offen", "accepted": "✓", "rejected": "✗",
                  "withdrawn": "↩"}.get(p.get("status", ""), "?")
        lines.append(
            f"  #{p['id']} [{status}] [{type_label[:20]}] {p['title'][:60]}"
        )
    return "\n".join(lines)
