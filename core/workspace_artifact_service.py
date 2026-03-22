"""
core/workspace_artifact_service.py -- 7.x Workspace-Artefaktsystem

Kanonische Schicht fuer alle Workspace-Writes.
Artefakte gehoeren zu einer Linie, haben Typ, Zweck, Format, Status.
"""
import os
import logging
import re
import json

from core.datetime_utils import to_iso

logger = logging.getLogger(__name__)

# =============================================================================
# Whitelists
# =============================================================================

ARTIFACT_TYPES = {
    "brief", "analysis", "plan", "implementation",
    "result", "report", "worklog", "patch", "test", "review"
}

ARTIFACT_PURPOSES = {
    "line_bootstrap", "working_state", "handover",
    "execution_result", "review_input", "review_output",
    "implementation_base", "verification_output",
}

ALLOWED_FORMATS = {
    "md", "json", "txt", "py", "js", "ts",
    "html", "css", "sql", "yaml"
}

ARTIFACT_STATUSES = {"draft", "active", "final", "superseded", "archived"}

# =============================================================================
# Pfad-Helfer
# =============================================================================

def get_workspace_root() -> str:
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "kimi_workspace")


def get_lines_root() -> str:
    return os.path.join(get_workspace_root(), "lines")


def normalize_line_id(line_id: str) -> str:
    """
    Normalisiert line_id fuer sichere Verzeichnisnamen.
    z.B. "todo:42" -> "todo_42"
    """
    return re.sub(r'[^a-zA-Z0-9_\-]', '_', str(line_id))


def get_line_root(line_id: str) -> str:
    return os.path.join(get_lines_root(), normalize_line_id(line_id))


def ensure_line_structure(line_id: str) -> str:
    """Stellt Verzeichnisstruktur fuer eine Linie sicher. Gibt line_root zurueck."""
    line_root = get_line_root(line_id)
    os.makedirs(os.path.join(line_root, "artifacts"), exist_ok=True)
    os.makedirs(os.path.join(line_root, "meta"), exist_ok=True)
    os.makedirs(os.path.join(line_root, "worklog"), exist_ok=True)
    return line_root


def _build_filename(artifact_id: int, artifact_type: str, fmt: str, version: int) -> str:
    """Systemischer Dateiname -- nicht frei vom Modell bestimmt."""
    return f"{artifact_id:06d}__{artifact_type}_v{version}.{fmt}"


def _build_relative_path(line_id: str, filename: str) -> str:
    return os.path.join("lines", normalize_line_id(line_id), "artifacts", filename)


def _full_path(relative_path: str) -> str:
    return os.path.join(get_workspace_root(), relative_path)


# =============================================================================
# Artifact-Lebenszyklus
# =============================================================================

def create_artifact(owner_id: str, line_id: str, artifact_type: str,
                    format: str, content: str,
                    purpose: str = "working_state",
                    task_id: str = None, step_id: str = None,
                    created_by: str = "kimi",
                    is_materialized_execution: bool = False) -> dict | None:
    """
    7.x: Erzeugt ein neues Artefakt.
    Schreibt Datei + DB-Eintrag + Event.
    Gibt Artefakt-Dict zurueck oder None bei Fehler.
    """
    # Validierung
    if artifact_type not in ARTIFACT_TYPES:
        logger.warning(f"create_artifact: ungueltiger Typ '{artifact_type}'")
        return None
    if format not in ALLOWED_FORMATS:
        logger.warning(f"create_artifact: ungueltiges Format '{format}'")
        return None
    if purpose not in ARTIFACT_PURPOSES:
        purpose = "working_state"

    try:
        from core.database import get_connection
        ensure_line_structure(line_id)
        now = to_iso()

        # Versionsnummer bestimmen
        conn = get_connection()
        try:
            existing = conn.execute(
                "SELECT MAX(version) as v FROM workspace_artifacts WHERE line_id=? AND artifact_type=?",
                (line_id, artifact_type)
            ).fetchone()
            version = (existing["v"] or 0) + 1

            # DB-Eintrag (ohne filename/path -- wird nach INSERT bestimmt)
            cur = conn.execute(
                """INSERT INTO workspace_artifacts
                   (owner_id, line_id, task_id, step_id, artifact_type, purpose,
                    format, status, filename, relative_path, version,
                    is_materialized_execution, created_by, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (owner_id, line_id, task_id, step_id, artifact_type, purpose,
                 format, "draft", "_placeholder_", "_placeholder_",
                 version, 1 if is_materialized_execution else 0,
                 created_by, now, now)
            )
            artifact_id = cur.lastrowid

            # Dateiname und Pfad mit echter ID
            filename = _build_filename(artifact_id, artifact_type, format, version)
            relative_path = _build_relative_path(line_id, filename)

            conn.execute(
                "UPDATE workspace_artifacts SET filename=?, relative_path=? WHERE id=?",
                (filename, relative_path, artifact_id)
            )
            conn.commit()
        finally:
            conn.close()

        # Datei schreiben
        full_path = _full_path(relative_path)
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        with open(full_path, "w", encoding="utf-8") as f:
            f.write(content)

        # Verify
        if not os.path.exists(full_path) or (content and os.path.getsize(full_path) == 0):
            logger.error(f"create_artifact: Datei nach Write nicht gefunden: {full_path}")
            return None

        # Event
        _write_event(artifact_id, "created", created_by,
                     {"line_id": line_id, "type": artifact_type, "version": version})

        # line_workspace_state aktualisieren
        _update_line_state(owner_id, line_id, artifact_id, is_materialized_execution)

        artifact = get_artifact(artifact_id)
        logger.info(f"Artifact #{artifact_id} erstellt: {artifact_type} v{version} fuer Linie {line_id}")
        return artifact

    except Exception as e:
        logger.error(f"create_artifact fehlgeschlagen: {e}")
        return None


def update_artifact_content(artifact_id: int, content: str,
                             status: str = None,
                             actor: str = "kimi") -> bool:
    """Aktualisiert Inhalt und optional Status eines Artefakts."""
    try:
        from core.database import get_connection
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT * FROM workspace_artifacts WHERE id=?", (artifact_id,)
            ).fetchone()
            if not row:
                return False
            art = dict(row)

            new_status = status if status in ARTIFACT_STATUSES else art["status"]
            now = to_iso()
            conn.execute(
                "UPDATE workspace_artifacts SET status=?, updated_at=? WHERE id=?",
                (new_status, now, artifact_id)
            )
            conn.commit()
        finally:
            conn.close()

        # Datei ueberschreiben
        full_path = _full_path(art["relative_path"])
        with open(full_path, "w", encoding="utf-8") as f:
            f.write(content)

        _write_event(artifact_id, "updated", actor,
                     {"status": new_status, "size": len(content)})
        return True
    except Exception as e:
        logger.error(f"update_artifact_content fehlgeschlagen: {e}")
        return False


def set_artifact_status(artifact_id: int, status: str, actor: str = "kimi") -> bool:
    """Setzt Status eines Artefakts."""
    if status not in ARTIFACT_STATUSES:
        return False
    try:
        from core.database import get_connection
        conn = get_connection()
        try:
            conn.execute(
                "UPDATE workspace_artifacts SET status=?, updated_at=? WHERE id=?",
                (status, to_iso(), artifact_id)
            )
            conn.commit()
        finally:
            conn.close()
        _write_event(artifact_id, "status_changed", actor, {"status": status})
        return True
    except Exception as e:
        logger.error(f"set_artifact_status fehlgeschlagen: {e}")
        return False


def mark_artifact_superseded(old_id: int, new_id: int, actor: str = "kimi") -> bool:
    """Markiert altes Artefakt als superseded durch neues."""
    try:
        from core.database import get_connection
        conn = get_connection()
        try:
            conn.execute(
                "UPDATE workspace_artifacts SET status='superseded', updated_at=? WHERE id=?",
                (to_iso(), old_id)
            )
            conn.commit()
        finally:
            conn.close()
        _write_event(old_id, "superseded", actor, {"superseded_by": new_id})
        return True
    except Exception as e:
        logger.error(f"mark_artifact_superseded fehlgeschlagen: {e}")
        return False


def delete_artifact(artifact_id: int, actor: str = "kimi") -> bool:
    """Loescht Artefakt (Datei + DB-Status auf archived)."""
    try:
        from core.database import get_connection
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT * FROM workspace_artifacts WHERE id=?", (artifact_id,)
            ).fetchone()
            if not row:
                return False
            art = dict(row)
            conn.execute(
                "UPDATE workspace_artifacts SET status='archived', updated_at=? WHERE id=?",
                (to_iso(), artifact_id)
            )
            conn.commit()
        finally:
            conn.close()

        full_path = _full_path(art["relative_path"])
        if os.path.exists(full_path):
            os.remove(full_path)

        _write_event(artifact_id, "deleted", actor, {})
        return True
    except Exception as e:
        logger.error(f"delete_artifact fehlgeschlagen: {e}")
        return False


# =============================================================================
# Lesen / Abfragen
# =============================================================================

def get_artifact(artifact_id: int) -> dict | None:
    try:
        from core.database import get_connection
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT * FROM workspace_artifacts WHERE id=?", (artifact_id,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()
    except Exception as e:
        logger.error(f"get_artifact fehlgeschlagen: {e}")
        return None


def list_line_artifacts(line_id: str, status: str = None,
                         artifact_type: str = None) -> list:
    try:
        from core.database import get_connection
        conn = get_connection()
        try:
            q = "SELECT * FROM workspace_artifacts WHERE line_id=?"
            p = [line_id]
            if status:
                q += " AND status=?"
                p.append(status)
            if artifact_type:
                q += " AND artifact_type=?"
                p.append(artifact_type)
            q += " ORDER BY created_at DESC"
            return [dict(r) for r in conn.execute(q, p).fetchall()]
        finally:
            conn.close()
    except Exception as e:
        logger.error(f"list_line_artifacts fehlgeschlagen: {e}")
        return []


def get_latest_line_artifact(line_id: str, artifact_type: str = None) -> dict | None:
    artifacts = list_line_artifacts(
        line_id, status="active",
        artifact_type=artifact_type
    )
    if not artifacts:
        artifacts = list_line_artifacts(line_id, artifact_type=artifact_type)
    return artifacts[0] if artifacts else None


def get_latest_materialized_artifact(line_id: str) -> dict | None:
    try:
        from core.database import get_connection
        conn = get_connection()
        try:
            row = conn.execute(
                """SELECT * FROM workspace_artifacts
                   WHERE line_id=? AND is_materialized_execution=1
                   ORDER BY created_at DESC LIMIT 1""",
                (line_id,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()
    except Exception as e:
        logger.error(f"get_latest_materialized_artifact fehlgeschlagen: {e}")
        return None


def read_artifact_content(artifact_id: int) -> str | None:
    art = get_artifact(artifact_id)
    if not art:
        return None
    full_path = _full_path(art["relative_path"])
    try:
        with open(full_path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        logger.error(f"read_artifact_content fehlgeschlagen: {e}")
        return None


# =============================================================================
# Materialiserung / Worklog
# =============================================================================

def materialize_execution_artifact(owner_id: str, line_id: str,
                                    content: str, format: str = "md",
                                    task_id: str = None, step_id: str = None,
                                    purpose: str = "execution_result") -> dict | None:
    """
    7.x + 6.x-Kopplung: Materialisiert first_meaningful_execution als Artefakt.
    Setzt is_materialized_execution=True.
    """
    art = create_artifact(
        owner_id=owner_id,
        line_id=line_id,
        artifact_type="result",
        format=format,
        content=content,
        purpose=purpose,
        task_id=task_id,
        step_id=step_id,
        is_materialized_execution=True,
    )
    if art:
        set_artifact_status(art["id"], "active")
        logger.info(f"materialize_execution: Artifact #{art['id']} fuer Linie {line_id}")
    return art


def append_worklog_entry(line_id: str, entry_text: str,
                          actor: str = "kimi", task_id: str = None) -> bool:
    """Haengt Eintrag an worklog.md der Linie an."""
    try:
        ensure_line_structure(line_id)
        wl_path = os.path.join(get_line_root(line_id), "worklog", "worklog.md")
        now = to_iso()
        entry = f"\n## {now[:16]}"
        if task_id:
            entry += f" | Task {task_id[:8]}"
        entry += f"\n{entry_text.strip()}\n"
        with open(wl_path, "a", encoding="utf-8") as f:
            f.write(entry)
        return True
    except Exception as e:
        logger.error(f"append_worklog_entry fehlgeschlagen: {e}")
        return False


# =============================================================================
# Manifest / Index
# =============================================================================

def build_line_manifest(line_id: str) -> dict:
    """Baut Manifest-Dict fuer eine Linie."""
    artifacts = list_line_artifacts(line_id)
    state = _get_line_state(line_id)
    return {
        "line_id": line_id,
        "artifact_count": len(artifacts),
        "artifacts": [
            {"id": a["id"], "type": a["artifact_type"], "status": a["status"],
             "format": a["format"], "version": a["version"],
             "is_materialized": bool(a["is_materialized_execution"]),
             "created_at": a["created_at"]}
            for a in artifacts
        ],
        "last_write": state.get("last_workspace_write_at") if state else None,
        "latest_materialized": state.get("latest_materialized_artifact_id") if state else None,
    }


def rebuild_artifact_index(line_id: str) -> bool:
    """Schreibt artifact_index.json in meta/."""
    try:
        ensure_line_structure(line_id)
        manifest = build_line_manifest(line_id)
        idx_path = os.path.join(get_line_root(line_id), "meta", "artifact_index.json")
        with open(idx_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2, ensure_ascii=False)
        return True
    except Exception as e:
        logger.error(f"rebuild_artifact_index fehlgeschlagen: {e}")
        return False


# =============================================================================
# Interne Helfer
# =============================================================================

def _write_event(artifact_id: int, event_type: str, actor: str, payload: dict) -> None:
    try:
        from core.database import get_connection
        conn = get_connection()
        try:
            conn.execute(
                """INSERT INTO workspace_artifact_events
                   (artifact_id, event_type, actor, payload_json, created_at)
                   VALUES (?,?,?,?,?)""",
                (artifact_id, event_type, actor,
                 json.dumps(payload, ensure_ascii=False), to_iso())
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.debug(f"_write_event fehlgeschlagen: {e}")


def _update_line_state(owner_id: str, line_id: str,
                        artifact_id: int, is_materialized: bool) -> None:
    try:
        from core.database import get_connection
        conn = get_connection()
        try:
            now = to_iso()
            existing = conn.execute(
                "SELECT * FROM line_workspace_state WHERE line_id=?", (line_id,)
            ).fetchone()
            if existing:
                updates = {
                    "latest_artifact_id": artifact_id,
                    "last_workspace_write_at": now,
                    "artifact_count": (existing["artifact_count"] or 0) + 1,
                    "updated_at": now,
                }
                if is_materialized:
                    updates["latest_materialized_artifact_id"] = artifact_id
                conn.execute(
                    """UPDATE line_workspace_state SET
                       latest_artifact_id=?, last_workspace_write_at=?,
                       artifact_count=?, updated_at=?
                       """ + (", latest_materialized_artifact_id=?" if is_materialized else "") +
                    " WHERE line_id=?",
                    ([artifact_id, now, updates["artifact_count"], now]
                     + ([artifact_id] if is_materialized else [])
                     + [line_id])
                )
            else:
                conn.execute(
                    """INSERT INTO line_workspace_state
                       (line_id, owner_id, latest_artifact_id,
                        latest_materialized_artifact_id,
                        last_workspace_write_at, artifact_count, updated_at)
                       VALUES (?,?,?,?,?,1,?)""",
                    (line_id, owner_id, artifact_id,
                     artifact_id if is_materialized else None,
                     now, now)
                )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.debug(f"_update_line_state fehlgeschlagen: {e}")


def _get_line_state(line_id: str) -> dict | None:
    try:
        from core.database import get_connection
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT * FROM line_workspace_state WHERE line_id=?", (line_id,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()
    except Exception as e:
        return None


# =============================================================================
# Verify-Helfer (fuer gate_service Integration)
# =============================================================================

def verify_artifact(artifact_id: int) -> tuple[bool, str]:
    """Verifiziert dass Artefakt in DB und Datei existiert."""
    art = get_artifact(artifact_id)
    if not art:
        return False, f"Artifact #{artifact_id} nicht in DB"
    full_path = _full_path(art["relative_path"])
    if not os.path.exists(full_path):
        return False, f"Datei nicht gefunden: {art['relative_path']}"
    if os.path.getsize(full_path) == 0:
        return False, f"Datei ist leer: {art['relative_path']}"
    return True, f"Artifact #{artifact_id} verifiziert ({art['artifact_type']} v{art['version']})"
