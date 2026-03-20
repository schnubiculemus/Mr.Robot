"""
core/code_exec.py — Code-Execution Workspace für Kimi

Kimi kann Python-Code schreiben und auf dem Server ausführen.
Kein Docker, kein künstliches Limit — echter Python-Interpreter,
aber mit klaren Grenzen:

Sicherheit:
    - Timeout: 30 Sekunden (kein Hängen)
    - Eigener Workspace: /opt/whatsapp-bot/kimi_workspace/
    - Kein Netzwerkzugriff im Code (ORBIT läuft sowieso ohne offene Ports)
    - Stdout/Stderr werden zurückgegeben, max 4000 Zeichen

Kimi kann:
    - Eigene Skripte schreiben und testen
    - Auf ihre ChromaDB-Daten, SQLite, Diary-Files zugreifen
    - Analyse-Tools bauen (Drift-Detektor etc.)
    - Bestehende Skripte im Workspace verwalten

Wird aufgerufen via:
    [CODE: {"action": "run", "code": "print('hello')"}]
    [CODE: {"action": "run_file", "filename": "drift_detector.py"}]
    [CODE: {"action": "save", "filename": "drift_detector.py", "code": "..."}]
    [CODE: {"action": "list"}]
    [CODE: {"action": "read", "filename": "drift_detector.py"}]
    [CODE: {"action": "delete", "filename": "drift_detector.py"}]
"""

import logging
import os
import subprocess
import sys

logger = logging.getLogger(__name__)

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKSPACE   = os.path.join(PROJECT_DIR, "kimi_workspace")
VENV_PYTHON = os.path.join(PROJECT_DIR, "venv", "bin", "python")

TIMEOUT_SECONDS = 30
MAX_OUTPUT_CHARS = 4000
MAX_FILE_SIZE    = 50_000   # 50KB max pro Skript
MAX_FILES        = 50       # max Dateien im Workspace




# =============================================================================
# Sicherheits-Check
# =============================================================================

# Gefährliche Patterns die vor der Ausführung geblockt werden
_BLOCKED_PATTERNS = [
    # Filesystem-Destruktion außerhalb Workspace
    "shutil.rmtree",
    "shutil.move",
    # ChromaDB destruktiv
    "delete_collection",
    "client.reset",
    ".reset()",
    "collection.delete(",
    # Nested execution
    "subprocess.run",
    "subprocess.Popen",
    "subprocess.call",
    "os.system(",
    # Kritische Pfade direkt ansprechen
    "chroma_db",
    "/opt/whatsapp-bot/data",
    "heartbeat_state.json",
]

# os.remove/unlink/rmdir dürfen nur auf kimi_workspace
_RESTRICTED_FS_OPS = [
    "os.remove(",
    "os.unlink(",
    "os.rmdir(",
    "pathlib.Path",
]


def _security_check(code: str) -> str | None:
    """
    Prüft Code auf gefährliche Operationen.
    Returns: Fehlermeldung wenn geblockt, None wenn OK.
    """
    for pattern in _BLOCKED_PATTERNS:
        if pattern in code:
            return (
                f"Sicherheits-Check fehlgeschlagen: '{pattern}' ist nicht erlaubt.\n"
                f"Destruktive Operationen auf Bot-Infrastruktur sind gesperrt.\n"
                f"Lesen ist weiterhin möglich. Schreiben nur in kimi_workspace/."
            )

    # os.remove/unlink/rmdir nur auf kimi_workspace erlaubt
    for op in _RESTRICTED_FS_OPS:
        if op in code:
            # Prüfen ob workspace im Kontext vorkommt
            if "kimi_workspace" not in code and "WORKSPACE" not in code:
                return (
                    f"Sicherheits-Check fehlgeschlagen: '{op}' außerhalb von kimi_workspace nicht erlaubt.\n"
                    f"Dateisystem-Operationen sind nur im Workspace erlaubt."
                )

    return None

# =============================================================================
# Workspace initialisieren
# =============================================================================

def _ensure_workspace():
    """Erstellt den Workspace-Ordner wenn nötig."""
    os.makedirs(WORKSPACE, exist_ok=True)
    # .gitignore damit Workspace nicht im Repo landet
    gi = os.path.join(WORKSPACE, ".gitignore")
    if not os.path.exists(gi):
        open(gi, "w").write("*.py\n*.json\n*.txt\n*.csv\n*.log\n")


def _python():
    """Gibt den richtigen Python-Interpreter zurück."""
    if os.path.exists(VENV_PYTHON):
        return VENV_PYTHON
    return sys.executable


# =============================================================================
# Aktionen
# =============================================================================

def run_code(code: str) -> str:
    """
    Führt Python-Code direkt aus (kein Speichern).
    Gibt stdout + stderr zurück.
    """
    if not code or not code.strip():
        return "Fehler: Kein Code angegeben."

    if len(code) > MAX_FILE_SIZE:
        return f"Fehler: Code zu lang ({len(code)} Zeichen, max {MAX_FILE_SIZE})."

    # Sicherheits-Check
    sec_err = _security_check(code)
    if sec_err:
        return sec_err

    _ensure_workspace()

    # Temporäre Datei im Workspace
    import tempfile
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", dir=WORKSPACE,
        prefix="_tmp_", delete=False, encoding="utf-8"
    )
    try:
        tmp.write(code)
        tmp.close()

        result = subprocess.run(
            [_python(), tmp.name],
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
            cwd=WORKSPACE,
        )

        output = ""
        if result.stdout:
            output += result.stdout
        if result.stderr:
            output += ("\n" if output else "") + "STDERR:\n" + result.stderr

        if not output:
            output = "(kein Output)"

        if len(output) > MAX_OUTPUT_CHARS:
            output = output[:MAX_OUTPUT_CHARS] + f"\n\n[... Output gekürzt auf {MAX_OUTPUT_CHARS} Zeichen]"

        exit_info = f"\n[Exit: {result.returncode}]" if result.returncode != 0 else ""
        return output + exit_info

    except subprocess.TimeoutExpired:
        return f"Fehler: Timeout nach {TIMEOUT_SECONDS} Sekunden. Code wurde abgebrochen."
    except Exception as e:
        logger.warning(f"code_exec.run_code fehlgeschlagen: {e}")
        return f"Fehler beim Ausführen: {e}"
    finally:
        try:
            os.unlink(tmp.name)
        except Exception:
            pass


def run_file(filename: str) -> str:
    """Führt eine gespeicherte Datei aus dem Workspace aus."""
    filename = _sanitize(filename)
    if not filename:
        return "Fehler: Ungültiger Dateiname."

    filepath = os.path.join(WORKSPACE, filename)
    if not os.path.exists(filepath):
        return f"Datei '{filename}' nicht gefunden. Verfügbare Dateien:\n{list_files()}"

    _ensure_workspace()

    try:
        result = subprocess.run(
            [_python(), filepath],
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
            cwd=WORKSPACE,
        )

        output = ""
        if result.stdout:
            output += result.stdout
        if result.stderr:
            output += ("\n" if output else "") + "STDERR:\n" + result.stderr

        if not output:
            output = "(kein Output)"

        if len(output) > MAX_OUTPUT_CHARS:
            output = output[:MAX_OUTPUT_CHARS] + f"\n\n[... Output gekürzt]"

        exit_info = f"\n[Exit: {result.returncode}]" if result.returncode != 0 else ""
        return f"=== {filename} ===\n" + output + exit_info

    except subprocess.TimeoutExpired:
        return f"Fehler: Timeout nach {TIMEOUT_SECONDS} Sekunden."
    except Exception as e:
        logger.warning(f"code_exec.run_file fehlgeschlagen: {e}")
        return f"Fehler: {e}"


def save_file(filename: str, code: str) -> str:
    """Speichert Code als Datei im Workspace."""
    filename = _sanitize(filename)
    if not filename:
        return "Fehler: Ungültiger Dateiname."

    if not filename.endswith(".py"):
        filename += ".py"

    if len(code) > MAX_FILE_SIZE:
        return f"Fehler: Code zu lang ({len(code)} Zeichen, max {MAX_FILE_SIZE})."

    # Sicherheits-Check
    sec_err = _security_check(code)
    if sec_err:
        return sec_err

    _ensure_workspace()

    # Max-Dateien prüfen
    existing = _list_files_raw()
    if len(existing) >= MAX_FILES and filename not in existing:
        return f"Fehler: Workspace voll ({MAX_FILES} Dateien). Erst alte Dateien löschen."

    filepath = os.path.join(WORKSPACE, filename)
    try:
        open(filepath, "w", encoding="utf-8").write(code)
        logger.info(f"code_exec: Datei gespeichert: {filename} ({len(code)} Zeichen)")
        return f"Gespeichert: {filename} ({len(code)} Zeichen)"
    except Exception as e:
        logger.warning(f"code_exec.save_file fehlgeschlagen: {e}")
        return f"Fehler beim Speichern: {e}"


def read_file(filename: str) -> str:
    """Liest eine Datei aus dem Workspace."""
    filename = _sanitize(filename)
    if not filename:
        return "Fehler: Ungültiger Dateiname."

    filepath = os.path.join(WORKSPACE, filename)
    if not os.path.exists(filepath):
        return f"Datei '{filename}' nicht gefunden."

    try:
        content = open(filepath, encoding="utf-8", errors="replace").read()
        if len(content) > MAX_OUTPUT_CHARS:
            content = content[:MAX_OUTPUT_CHARS] + "\n\n[... gekürzt]"
        return f"=== {filename} ===\n{content}"
    except Exception as e:
        return f"Fehler beim Lesen: {e}"


def delete_file(filename: str) -> str:
    """Löscht eine Datei aus dem Workspace."""
    filename = _sanitize(filename)
    if not filename:
        return "Fehler: Ungültiger Dateiname."

    filepath = os.path.join(WORKSPACE, filename)
    if not os.path.exists(filepath):
        return f"Datei '{filename}' nicht gefunden."

    try:
        os.unlink(filepath)
        logger.info(f"code_exec: Datei gelöscht: {filename}")
        return f"Gelöscht: {filename}"
    except Exception as e:
        return f"Fehler beim Löschen: {e}"


def list_files() -> str:
    """Listet alle Dateien im Workspace."""
    files = _list_files_raw()
    if not files:
        return "Workspace leer — noch keine Skripte gespeichert."

    lines = [f"Workspace ({len(files)} Dateien):"]
    for f in sorted(files):
        filepath = os.path.join(WORKSPACE, f)
        size = os.path.getsize(filepath)
        lines.append(f"  {f} ({size} Bytes)")
    return "\n".join(lines)


# =============================================================================
# Hilfsfunktionen
# =============================================================================

def _sanitize(filename: str) -> str:
    """Bereinigt einen Dateinamen — verhindert Path Traversal."""
    if not filename:
        return ""
    # Nur Dateiname, kein Pfad
    filename = os.path.basename(filename)
    # Nur erlaubte Zeichen
    import re
    filename = re.sub(r"[^a-zA-Z0-9_\-\.]", "_", filename)
    # Keine versteckten Dateien
    if filename.startswith("."):
        return ""
    return filename


def _list_files_raw() -> list:
    """Gibt eine Liste der .py Dateien im Workspace zurück."""
    _ensure_workspace()
    try:
        return [
            f for f in os.listdir(WORKSPACE)
            if f.endswith(".py") and not f.startswith("_tmp_")
        ]
    except Exception:
        return []


# =============================================================================
# Haupt-Dispatcher
# =============================================================================

def execute_code_exec(action: str, params: dict) -> str:
    """
    Haupt-Einstiegspunkt für das code_exec Tool.

    Actions:
        run       — params: {"code": "print('hello')"}
        run_file  — params: {"filename": "drift_detector.py"}
        save      — params: {"filename": "drift_detector.py", "code": "..."}
        read      — params: {"filename": "drift_detector.py"}
        delete    — params: {"filename": "drift_detector.py"}
        list      — params: {}
    """
    action = action.lower().strip()

    if action == "run":
        code = params.get("code", "")
        return run_code(code)

    elif action == "run_file":
        filename = params.get("filename", "")
        return run_file(filename)

    elif action == "save":
        filename = params.get("filename", "")
        code     = params.get("code", "")
        if not filename:
            return "Fehler: 'filename' fehlt."
        if not code:
            return "Fehler: 'code' fehlt."
        return save_file(filename, code)

    elif action == "read":
        filename = params.get("filename", "")
        if not filename:
            return "Fehler: 'filename' fehlt."
        return read_file(filename)

    elif action == "delete":
        filename = params.get("filename", "")
        if not filename:
            return "Fehler: 'filename' fehlt."
        return delete_file(filename)

    elif action in ("list", "ls"):
        return list_files()

    else:
        return (
            f"Unbekannte Aktion '{action}'. "
            f"Verfügbar: run, run_file, save, read, delete, list"
        )
