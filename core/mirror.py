"""
core/mirror.py — MIRROR Phase 1

Turn-Logging für Kimi. Speichert nach jeder Antwort ein strukturiertes
Turn-Objekt: aktive Chunks, Rule-Stack, Response-Profil, Pattern-Flags.

Kein Chain-of-Thought, keine rohen Gedanken — nur rekonstruierbare
Entscheidungsartefakte.
"""

import json
import logging
import re
import uuid
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Pattern-Definitionen
# Jedes Pattern hat: id, name, check-Funktion → gibt (triggered: bool, strength: float) zurück
# ---------------------------------------------------------------------------

def _pattern_aufzaehlung(response: str) -> tuple[bool, float]:
    """Bot listet statt zu reden — Aufzählungs-Falle."""
    lines = response.split("\n")
    list_lines = sum(1 for l in lines if re.match(r"^\s*[-•*]\s|^\s*\d+\.\s", l))
    total = max(len([l for l in lines if l.strip()]), 1)
    density = list_lines / total
    return density > 0.35, round(density, 2)


def _pattern_projektmodus(response: str) -> tuple[bool, float]:
    """Bot weicht auf Meta/Architektur aus statt konkret zu liefern."""
    meta_words = [
        "roadmap", "architektur", "modul", "system", "schicht", "phase",
        "konzept", "strategie", "infrastruktur", "pipeline", "framework",
        "überblick", "struktur", "ansatz"
    ]
    words = response.lower().split()
    if not words:
        return False, 0.0
    hits = sum(1 for w in words if any(m in w for m in meta_words))
    strength = min(hits / max(len(words) / 20, 1), 1.0)
    return strength > 0.4, round(strength, 2)


def _pattern_regel_relapse(response: str) -> tuple[bool, float]:
    """Markdown trotz Verbot — Regel-Rückfall."""
    markdown_hits = len(re.findall(r"\*\*|__|\#{1,3} |```|---|\*[^*]", response))
    strength = min(markdown_hits / 5, 1.0)
    return markdown_hits >= 2, round(strength, 2)


def _pattern_uebervorsicht(response: str, action_blocks: list) -> tuple[bool, float]:
    """Bot hätte handeln sollen, fragt aber nach."""
    hedge_phrases = [
        "welchen kalender", "welche liste", "soll ich", "meinst du",
        "welchen meinst", "arbeit oder privat", "möchtest du dass ich",
        "soll das"
    ]
    lower = response.lower()
    hits = sum(1 for p in hedge_phrases if p in lower)
    # Wenn keine Action-Blöcke gefeuert und Fragen vorhanden
    no_action = len(action_blocks) == 0
    strength = min(hits * 0.4, 1.0) if no_action else 0.0
    return hits >= 1 and no_action, round(strength, 2)


def _pattern_selbstkritik(response: str) -> tuple[bool, float]:
    """Kimi analysiert eigene Fehler laut im Chat."""
    phrases = [
        "same old pattern", "ich hab's wieder", "ich merk dass ich",
        "tut mir leid dass ich", "schon wieder", "immer wieder",
        "dieser fehler", "ich weiß, ich sollte"
    ]
    lower = response.lower()
    hits = sum(1 for p in phrases if p in lower)
    return hits >= 1, min(hits * 0.5, 1.0)


# ---------------------------------------------------------------------------
# Action-Block-Extraktion
# ---------------------------------------------------------------------------

def extract_action_blocks(response: str) -> list[str]:
    """Welche Action-Blöcke hat Kimi gefeuert?"""
    patterns = [
        r"\[CALENDAR_ACTION:",
        r"\[TODO_ACTION:",
        r"\[SEARCH:",
        r"\[INTROSPECT:",
        r"\[MIRROR:",
    ]
    found = []
    for p in patterns:
        if re.search(p, response):
            found.append(p.replace(r"\[", "").replace(":", ""))
    return found


# ---------------------------------------------------------------------------
# Response-Profil
# ---------------------------------------------------------------------------

def build_response_profile(response: str) -> dict:
    """Messbare Eigenschaften der Antwort."""
    lines = [l for l in response.split("\n") if l.strip()]
    words = response.split()
    list_lines = sum(1 for l in lines if re.match(r"^\s*[-•*]\s|^\s*\d+\.\s", l))

    # Direktheits-Score: kurze Antworten ohne Füllwörter sind direkter
    filler = ["eigentlich", "grundsätzlich", "ich würde sagen", "sozusagen",
              "quasi", "gewissermaßen", "im grunde"]
    filler_hits = sum(1 for f in filler if f in response.lower())
    directness = max(0.0, 1.0 - (filler_hits * 0.15) - (len(words) / 500))

    return {
        "length_chars": len(response),
        "length_words": len(words),
        "list_density": round(list_lines / max(len(lines), 1), 2),
        "directness_score": round(min(max(directness, 0.0), 1.0), 2),
        "has_markdown": bool(re.search(r"\*\*|__|\#{1,3} |```", response)),
        "action_blocks": extract_action_blocks(response),
    }


# ---------------------------------------------------------------------------
# Chunk-Zusammenfassung für das Turn-Objekt
# ---------------------------------------------------------------------------

def summarize_chunks(chunks: list) -> list[dict]:
    """Komprimiert Chunk-Liste auf das Wesentliche."""
    result = []
    for c in chunks:
        result.append({
            "id": c.get("id", "?"),
            "type": c.get("chunk_type", c.get("type", "?")),
            "score": round(c.get("_retrieval_score", 0.0), 3),
            "tags": c.get("tags", [])[:3],
            "preview": (c.get("text", "") or "")[:80].replace("\n", " "),
        })
    return result


def summarize_global_rules(rules: list) -> list[dict]:
    """Komprimiert Global-Rules auf ID + Weight."""
    result = []
    for r in rules:
        result.append({
            "id": r.get("id", "?"),
            "type": r.get("chunk_type", "?"),
            "weight": round(r.get("weight", 0.0) * r.get("confidence", 1.0), 3),
        })
    return result


# ---------------------------------------------------------------------------
# Pattern-Check
# ---------------------------------------------------------------------------

def check_patterns(response: str, action_blocks: list) -> list[dict]:
    """Läuft alle Pattern-Checks und gibt die gefeuerten zurück."""
    checks = [
        ("aufzaehlung",    "Aufzählungs-Falle",      _pattern_aufzaehlung(response)),
        ("projektmodus",   "Projektmodus-Versteck",   _pattern_projektmodus(response)),
        ("regel_relapse",  "Regel-Rückfall (MD)",     _pattern_regel_relapse(response)),
        ("uebervorsicht",  "Übervorsicht / Nachfrage", _pattern_uebervorsicht(response, action_blocks)),
        ("selbstkritik",   "Selbstkritik im Chat",    _pattern_selbstkritik(response)),
    ]
    flags = []
    for pid, name, (triggered, strength) in checks:
        if triggered:
            flags.append({"type": pid, "name": name, "strength": round(strength, 2)})
    return flags


# ---------------------------------------------------------------------------
# Haupt-API: build_turn + save_turn
# ---------------------------------------------------------------------------

def build_turn(
    user_id: str,
    user_message: str,
    response: str,
    chunks: list,
    global_rules: list,
) -> dict:
    """
    Baut das vollständige Turn-Objekt.
    Wird von ollama_client.chat() aufgerufen — noch vor dem Speichern.
    """
    profile = build_response_profile(response)
    pattern_flags = check_patterns(response, profile["action_blocks"])

    # Preflight-Status ableiten
    issues = []
    if profile["has_markdown"]:
        issues.append("Markdown trotz Verbot (Regel-Rückfall)")
    if profile["list_density"] > 0.5:
        issues.append(f"Hohe Listendichte ({profile['list_density']:.0%}) — Aufzählungs-Risiko")
    if profile["length_words"] > 300:
        issues.append(f"Lange Antwort ({profile['length_words']} Wörter)")
    if any(p["type"] == "uebervorsicht" for p in pattern_flags):
        issues.append("Nachfrage statt Handlung erkannt")
    if any(p["type"] == "selbstkritik" for p in pattern_flags):
        issues.append("Selbstkritik im Chat — gehört ins Tagebuch")

    if not issues:
        preflight_status = "green"
    elif len(issues) == 1:
        preflight_status = "yellow"
    elif len(issues) == 2:
        preflight_status = "orange"
    else:
        preflight_status = "red"

    return {
        "turn_id": f"{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}_{uuid.uuid4().hex[:6]}",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "user_id": user_id,
        "user_message_preview": user_message[:120].replace("\n", " "),
        "active_chunks": summarize_chunks(chunks),
        "rule_stack": summarize_global_rules(global_rules),
        "response_profile": profile,
        "preflight": {
            "status": preflight_status,
            "issues": issues,
        },
        "pattern_flags": pattern_flags,
    }


def save_turn(turn: dict) -> None:
    """Speichert ein Turn-Objekt in der Datenbank."""
    try:
        from core.database import save_mirror_turn
        save_mirror_turn(turn)
    except Exception as e:
        logger.warning(f"mirror.save_turn fehlgeschlagen: {e}")
