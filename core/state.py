"""
core/state.py — Heartbeat-State Management

Zentraler Key-Value Store fuer den persistenten Heartbeat-State.
Atomar schreiben (via core.file_utils), JSON-basiert.

Ausgelagert aus heartbeat.py um zirkulaere Imports zu vermeiden:
  heartbeat.py → proactive.py hatte vorher einen Rueckimport
  auf heartbeat.load_state / save_state / to_iso.
  Jetzt importieren beide aus core.state.
"""

import os
import json
import logging

logger = logging.getLogger(__name__)

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HEARTBEAT_STATE_PATH = os.path.join(PROJECT_DIR, "heartbeat_state.json")


def load_state() -> dict:
    """Laedt den Heartbeat-State. Gibt leeres Dict zurueck bei fehlendem oder korruptem File."""
    if not os.path.exists(HEARTBEAT_STATE_PATH):
        return {}
    try:
        with open(HEARTBEAT_STATE_PATH, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        corrupt_path = HEARTBEAT_STATE_PATH + ".corrupt"
        logger.error(
            f"heartbeat_state.json kaputt: {e} — "
            f"sichere als {corrupt_path}, starte mit leerem State"
        )
        try:
            os.replace(HEARTBEAT_STATE_PATH, corrupt_path)
        except OSError as rename_err:
            logger.error(f"Umbenennen fehlgeschlagen: {rename_err}")
        return {}


def save_state(state: dict) -> None:
    """Speichert den Heartbeat-State atomar."""
    from core.file_utils import atomic_write_json
    atomic_write_json(HEARTBEAT_STATE_PATH, state)
