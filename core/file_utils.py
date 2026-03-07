"""
SchnuBot.ai - File Utilities (P1.4)

Atomisches Schreiben für JSON- und Textdateien.
Verhindert halb-geschriebene oder leere Dateien bei Crashes,
Kill-Signalen oder vollem Disk.

Prinzip: Erst in Temp-Datei im selben Verzeichnis schreiben,
dann per os.replace() atomar umbenennen. os.replace() ist auf
POSIX-Systemen garantiert atomar (gleicher Mount-Point).
"""

import os
import json
import tempfile
import logging

logger = logging.getLogger(__name__)


def atomic_write_json(path, data, ensure_ascii=False, indent=2):
    """
    Schreibt ein Dict/Liste als JSON atomar auf Disk.

    1. Temp-Datei im selben Verzeichnis erstellen
    2. JSON reinschreiben + flush + fsync
    3. os.replace() (atomar auf POSIX)
    4. Bei Fehler: Temp-Datei aufräumen, Exception weiterwerfen

    Args:
        path: Ziel-Dateipfad
        data: JSON-serialisierbares Objekt
        ensure_ascii: json.dump Parameter (default: False für Umlaute)
        indent: json.dump Parameter (default: 2)
    """
    dir_name = os.path.dirname(path) or "."
    fd = None
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            fd = None  # fdopen übernimmt fd, nicht nochmal schließen
            json.dump(data, f, ensure_ascii=ensure_ascii, indent=indent)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
        tmp_path = None  # replace erfolgreich, nicht aufräumen
    except Exception:
        # Temp-Datei aufräumen falls replace fehlschlug
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        raise


def atomic_write_text(path, content, encoding="utf-8"):
    """
    Schreibt einen Text-String atomar auf Disk.

    Gleiche Strategie wie atomic_write_json: Temp + replace.
    Für soul.md, architecture.md und ähnliche Textdateien.

    Args:
        path: Ziel-Dateipfad
        content: String
        encoding: Encoding (default: utf-8)
    """
    dir_name = os.path.dirname(path) or "."
    fd = None
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
        with os.fdopen(fd, "w", encoding=encoding) as f:
            fd = None
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
        tmp_path = None
    except Exception:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        raise
