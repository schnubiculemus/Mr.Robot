"""
workspace_service.py — WP4: Kooperativer Workspace V2

Der Workspace ist eine gemeinsame Werkbank für Notizen und Code-Dateien.

Regeln (WP4):
- Nur zwei echte Typen: note und code_file
- Schreiben nur kooperativ: explizit oder durch Kimi Core im aktiven Kontext
- Kein autonomes Schreiben aus Triggern, Recovery oder Memory-Impuls
- Pro aktivem Kontext: ein führendes Dokument + Hilfsdokumente

Kimi Core führt -- Workspace dient.
"""

import logging
import os

logger = logging.getLogger(__name__)

# V2 Dokumenttypen
DOC_TYPE_NOTE      = "note"
DOC_TYPE_CODE      = "code_file"
DOC_TYPES_V2       = {DOC_TYPE_NOTE, DOC_TYPE_CODE}

# Schreibregel
WRITE_REASON_EXPLICIT  = "explicit"   # Nutzer hat es klar angefordert
WRITE_REASON_IMPLICIT  = "implicit"   # Kimi Core erkennt klare Schreibabsicht


def get_workspace_root() -> str:
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "kimi_workspace", "v2")


def _doc_path(owner_id: str, doc_id: str) -> str:
    root = get_workspace_root()
    safe_owner = owner_id.replace("@", "_").replace(":", "_")[:20]
    return os.path.join(root, safe_owner, f"{doc_id}.md")


# =============================================================================
# Lesen
# =============================================================================

def read_document(owner_id: str, doc_id: str) -> str | None:
    """Liest ein Dokument. Gibt None zurück wenn nicht vorhanden."""
    path = _doc_path(owner_id, doc_id)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        logger.warning(f"read_document fehlgeschlagen: {e}")
        return None


def list_documents(owner_id: str) -> list[dict]:
    """Listet alle Dokumente eines Nutzers."""
    root = get_workspace_root()
    safe_owner = owner_id.replace("@", "_").replace(":", "_")[:20]
    owner_dir = os.path.join(root, safe_owner)
    if not os.path.exists(owner_dir):
        return []
    docs = []
    for fname in sorted(os.listdir(owner_dir)):
        if fname.endswith(".md"):
            doc_id = fname[:-3]
            fpath = os.path.join(owner_dir, fname)
            size = os.path.getsize(fpath)
            mtime = os.path.getmtime(fpath)
            docs.append({"doc_id": doc_id, "size": size, "modified": mtime})
    return docs


# =============================================================================
# Schreiben (kooperativ -- nur über Kimi Core)
# =============================================================================

def write_document(owner_id: str, doc_id: str, content: str,
                   doc_type: str = DOC_TYPE_NOTE,
                   write_reason: str = WRITE_REASON_EXPLICIT) -> bool:
    """
    Schreibt ein Dokument.

    WP4 Schreibregel:
    - write_reason muss angegeben werden
    - EXPLICIT: Nutzer hat es klar angefordert
    - IMPLICIT: Kimi Core hat klare Schreibabsicht erkannt
    - Kein anderer Pfad erlaubt
    """
    if doc_type not in DOC_TYPES_V2:
        logger.warning(f"write_document: Typ '{doc_type}' ist kein V2-Typ -- abgelehnt")
        return False

    if write_reason not in (WRITE_REASON_EXPLICIT, WRITE_REASON_IMPLICIT):
        logger.warning(f"write_document: ungültiger write_reason '{write_reason}'")
        return False

    path = _doc_path(owner_id, doc_id)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, path)
        logger.info(f"Workspace: {doc_type} '{doc_id}' geschrieben ({write_reason})")
        return True
    except Exception as e:
        logger.error(f"write_document fehlgeschlagen: {e}")
        return False


def append_to_document(owner_id: str, doc_id: str, addition: str,
                        write_reason: str = WRITE_REASON_EXPLICIT) -> bool:
    """Ergänzt ein bestehendes Dokument."""
    existing = read_document(owner_id, doc_id) or ""
    new_content = existing + "\n\n" + addition if existing else addition
    return write_document(owner_id, doc_id, new_content,
                          write_reason=write_reason)


def delete_document(owner_id: str, doc_id: str) -> bool:
    """Löscht ein Dokument (archiviert es)."""
    path = _doc_path(owner_id, doc_id)
    if not os.path.exists(path):
        return False
    archive_path = path.replace(".md", "_archived.md")
    try:
        os.rename(path, archive_path)
        logger.info(f"Workspace: '{doc_id}' archiviert")
        return True
    except Exception as e:
        logger.warning(f"delete_document fehlgeschlagen: {e}")
        return False


# =============================================================================
# Führendes Dokument + Hilfsdokumente (WP4 + AWC-Integration)
# =============================================================================

def set_leading_document(owner_id: str, doc_id: str) -> bool:
    """
    Setzt das führende Dokument im Active Working Context.
    Kimi Core ruft das auf wenn eine klare Schreibabsicht erkannt wird.
    """
    try:
        from active_working_context import update_active_context
        return update_active_context(owner_id, active_document=doc_id)
    except Exception as e:
        logger.warning(f"set_leading_document fehlgeschlagen: {e}")
        return False


def get_leading_document(owner_id: str) -> str | None:
    """Gibt das führende Dokument aus dem AWC zurück."""
    try:
        from active_working_context import get_active_context
        awc = get_active_context(owner_id)
        return awc.get("active_document") if awc else None
    except Exception:
        return None


def read_leading_document(owner_id: str) -> str | None:
    """Liest das führende Dokument direkt."""
    doc_id = get_leading_document(owner_id)
    if not doc_id:
        return None
    return read_document(owner_id, doc_id)


# =============================================================================
# Workspace-Hygiene
# =============================================================================
# WP4: kein Recovery-Output, kein System-Output als normale Dokumente
# Systemspuren bleiben intern (orbit logs, DB) -- nicht im V2-Workspace

def is_cooperative_write_allowed(write_reason: str) -> bool:
    """
    Prüft ob ein Schreibvorgang erlaubt ist.
    Nur EXPLICIT und IMPLICIT sind erlaubt.
    """
    return write_reason in (WRITE_REASON_EXPLICIT, WRITE_REASON_IMPLICIT)
