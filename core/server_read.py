"""
core/server_read.py — Server-Lesezugriff für Kimi

Kimi kann damit Logs, Systemstatus und eigene Dateien lesen.
Nur lesend — kein Schreiben, kein Ausführen (das kommt mit Code-Execution).

Erlaubte Aktionen:
    read_log   — Letzte N Zeilen einer Log-Datei
    read_file  — Inhalt einer Datei im Bot-Verzeichnis (Whitelist)
    status     — Systemstatus (RAM, Disk, Service-Status)

Wird via ORBIT execute_tool aufgerufen:
    tool_ref="server_read", action="read_log", params={"log": "orbit", "lines": 50}
    tool_ref="server_read", action="read_file", params={"path": "diary/2026-03-19.md"}
    tool_ref="server_read", action="status"

Auch direkt aufrufbar aus dem Chat via [SERVER_READ: {...}] Syntax (in tools.md definiert).
"""

import logging
import os
import subprocess

logger = logging.getLogger(__name__)

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_DIR     = os.path.join(PROJECT_DIR, "logs")

# Erlaubte Log-Dateien
LOG_FILES = {
    "orbit":     "orbit.log",
    "cognition": "cognition.log",
    "app":       "app.log",
    "heartbeat": "heartbeat_cron.log",
    "backup":    "backup.log",
}

# Erlaubte Dateipfade (relativ zu PROJECT_DIR) — Whitelist
READABLE_PATHS = {
    # Identity
    "soul.md", "rules.md", "tools.md", "style.md", "architecture.md",
    # Diary
    "diary/",   # Verzeichnis — alle .md drunter erlaubt
    # Data
    "data/tools_config.json",
    "data/token_usage.json",
    # State (read-only Snapshot)
    "heartbeat_state.json",
}

MAX_LOG_LINES   = 200
MAX_FILE_CHARS  = 8000


# =============================================================================
# Aktionen
# =============================================================================

def read_log(log_name: str, lines: int = 50) -> str:
    """Liest die letzten N Zeilen einer Log-Datei."""
    log_file = LOG_FILES.get(log_name.lower())
    if not log_file:
        available = ", ".join(LOG_FILES.keys())
        return f"Unbekannte Log-Datei '{log_name}'. Verfügbar: {available}"

    filepath = os.path.join(LOG_DIR, log_file)
    if not os.path.exists(filepath):
        return f"Log-Datei '{log_file}' existiert nicht."

    lines = min(max(1, lines), MAX_LOG_LINES)

    try:
        result = subprocess.run(
            ["tail", f"-{lines}", filepath],
            capture_output=True, text=True, timeout=5
        )
        content = result.stdout.strip()
        if not content:
            return f"Log '{log_name}' ist leer."
        return f"=== {log_file} (letzte {lines} Zeilen) ===\n{content}"
    except Exception as e:
        logger.warning(f"server_read.read_log fehlgeschlagen: {e}")
        return f"Fehler beim Lesen von '{log_file}': {e}"


def read_file(path: str) -> str:
    """Liest eine Datei aus dem Bot-Verzeichnis — nur Whitelist."""
    # Normalisieren und Traversal verhindern
    path = path.lstrip("/").replace("..", "")
    abs_path = os.path.normpath(os.path.join(PROJECT_DIR, path))

    # Muss im PROJECT_DIR bleiben
    if not abs_path.startswith(PROJECT_DIR):
        return "Zugriff verweigert: Pfad außerhalb des Bot-Verzeichnisses."

    # Whitelist prüfen
    rel_path = os.path.relpath(abs_path, PROJECT_DIR)
    allowed = False
    for allowed_path in READABLE_PATHS:
        if allowed_path.endswith("/"):
            # Verzeichnis — alle Dateien drunter erlaubt
            if rel_path.startswith(allowed_path) or rel_path.startswith(allowed_path.rstrip("/")):
                allowed = True
                break
        else:
            if rel_path == allowed_path:
                allowed = True
                break

    if not allowed:
        readable = ", ".join(sorted(READABLE_PATHS))
        return f"Zugriff verweigert: '{rel_path}' ist nicht in der Whitelist.\nErlaubt: {readable}"

    if not os.path.exists(abs_path):
        return f"Datei '{rel_path}' nicht gefunden."

    if not os.path.isfile(abs_path):
        # Verzeichnis — Listing
        try:
            entries = sorted(os.listdir(abs_path))
            return f"=== {rel_path}/ ===\n" + "\n".join(entries[:50])
        except Exception as e:
            return f"Fehler beim Lesen von '{rel_path}': {e}"

    try:
        content = open(abs_path, encoding="utf-8", errors="replace").read()
        if len(content) > MAX_FILE_CHARS:
            content = content[:MAX_FILE_CHARS] + f"\n\n[... gekürzt auf {MAX_FILE_CHARS} Zeichen]"
        return f"=== {rel_path} ===\n{content}"
    except Exception as e:
        logger.warning(f"server_read.read_file fehlgeschlagen: {e}")
        return f"Fehler beim Lesen von '{rel_path}': {e}"


def read_status() -> str:
    """Liest Systemstatus: RAM, Disk, Service-Status."""
    lines = []

    # RAM
    try:
        result = subprocess.run(
            ["free", "-h"], capture_output=True, text=True, timeout=3
        )
        lines.append("=== RAM ===")
        lines.append(result.stdout.strip())
    except Exception:
        lines.append("RAM: nicht verfügbar")

    # Disk
    try:
        result = subprocess.run(
            ["df", "-h", PROJECT_DIR], capture_output=True, text=True, timeout=3
        )
        lines.append("\n=== Disk ===")
        lines.append(result.stdout.strip())
    except Exception:
        lines.append("Disk: nicht verfügbar")

    # Service-Status
    for service in ["schnubot", "schnubot-orbit", "schnubot-dashboard"]:
        try:
            result = subprocess.run(
                ["systemctl", "is-active", service],
                capture_output=True, text=True, timeout=3
            )
            status = result.stdout.strip()
            lines.append(f"\n{service}: {status}")
        except Exception:
            lines.append(f"\n{service}: unbekannt")

    # Uptime
    try:
        result = subprocess.run(
            ["uptime", "-p"], capture_output=True, text=True, timeout=3
        )
        lines.append(f"\nUptime: {result.stdout.strip()}")
    except Exception:
        pass

    return "\n".join(lines)


# =============================================================================
# Haupt-Dispatcher
# =============================================================================

def execute_server_read(action: str, params: dict) -> str:
    """
    Haupt-Einstiegspunkt für das server_read Tool.

    Actions:
        read_log   — params: {"log": "orbit", "lines": 50}
        read_file  — params: {"path": "soul.md"}
        status     — params: {}
    """
    action = action.lower().strip()

    if action == "read_log":
        log_name = params.get("log", "orbit")
        lines    = int(params.get("lines", 50))
        return read_log(log_name, lines)

    elif action == "read_file":
        path = params.get("path", "")
        if not path:
            return "Fehler: 'path' fehlt."
        return read_file(path)

    elif action in ("status", "system_status"):
        return read_status()

    else:
        return (
            f"Unbekannte Aktion '{action}'. "
            f"Verfügbar: read_log, read_file, status"
        )
