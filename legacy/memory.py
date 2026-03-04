import os
import json
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

MEMORY_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "memory")


def ensure_memory_dir():
    os.makedirs(MEMORY_DIR, exist_ok=True)


def get_memory_path(user_id):
    safe_id = user_id.replace("@", "_").replace(".", "_")
    return os.path.join(MEMORY_DIR, f"{safe_id}.json")


def load_memory(user_id):
    ensure_memory_dir()
    path = get_memory_path(user_id)

    if not os.path.exists(path):
        return {"facts": [], "updated_at": None, "merged_facts": []}

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if "merged_facts" not in data:
            data["merged_facts"] = []
        return data
    except (json.JSONDecodeError, IOError) as e:
        logger.error(f"Fehler beim Laden der Memory für {user_id}: {e}")
        return {"facts": [], "updated_at": None, "merged_facts": []}


def save_memory(user_id, memory_data):
    ensure_memory_dir()
    path = get_memory_path(user_id)
    memory_data["updated_at"] = datetime.now().isoformat()

    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(memory_data, f, ensure_ascii=False, indent=2)
    except IOError as e:
        logger.error(f"Fehler beim Speichern der Memory für {user_id}: {e}")


def _fact_text(fact):
    """Extrahiert den Fakt-Text, egal ob String oder Dict."""
    if isinstance(fact, dict):
        return fact.get("fakt", "")
    return str(fact)


def _fact_key(fact):
    """Normalisierter Key zum Vergleichen."""
    return _fact_text(fact).lower().strip()


def update_memory(user_id, new_facts):
    """Fügt neue Fakten hinzu. Akzeptiert sowohl Strings als auch Dicts."""
    if not new_facts:
        return

    memory = load_memory(user_id)
    existing_facts = memory.get("facts", [])
    existing_keys = [_fact_key(f) for f in existing_facts]

    for new_fact in new_facts:
        # Normalisieren: String → Dict
        if isinstance(new_fact, str):
            new_fact = new_fact.strip()
            if not new_fact:
                continue
            new_fact = {"fakt": new_fact, "kategorie": "persoenlich"}
        
        if not isinstance(new_fact, dict) or not new_fact.get("fakt"):
            continue

        # stil_global Fakten werden NICHT gespeichert
        if new_fact.get("kategorie") == "stil_global":
            logger.info(f"🧠 Stilanweisung ignoriert (gehört in soul.md): '{new_fact['fakt']}'")
            continue

        new_key = _fact_key(new_fact)
        new_prefix = " ".join(new_key.split()[:3])

        replaced = False
        for i, existing in enumerate(existing_facts):
            existing_prefix = " ".join(_fact_key(existing).split()[:3])
            if new_prefix == existing_prefix and new_key != _fact_key(existing):
                existing_facts[i] = new_fact
                logger.info(f"🧠 Aktualisiert: '{_fact_text(existing)}' → '{new_fact['fakt']}'")
                replaced = True
                break

        if not replaced:
            if new_key not in existing_keys:
                existing_facts.append(new_fact)
                existing_keys.append(new_key)
                logger.info(f"🧠 Neuer Fakt [{new_fact.get('kategorie', '?')}]: '{new_fact['fakt']}'")

    memory["facts"] = existing_facts
    save_memory(user_id, memory)


def get_unmerged_facts(user_id):
    """Gibt Fakten zurück die noch nicht in die Context-Datei gemergt wurden."""
    memory = load_memory(user_id)
    merged = set(f.lower() for f in memory.get("merged_facts", []))
    return [f for f in memory.get("facts", []) if _fact_key(f) not in merged]


def get_unmerged_by_category(user_id):
    """Gibt ungemergte Fakten nach Kategorie gruppiert zurück."""
    unmerged = get_unmerged_facts(user_id)
    
    grouped = {
        "persoenlich": [],
        "kommunikation": [],
        "knowledge": [],
    }
    
    for fact in unmerged:
        if isinstance(fact, dict):
            cat = fact.get("kategorie", "persoenlich")
            grouped.setdefault(cat, []).append(fact)
        else:
            # Legacy: einfache Strings → persönlich
            grouped["persoenlich"].append({"fakt": fact, "kategorie": "persoenlich"})
    
    return grouped


def mark_facts_as_merged(user_id, merged_facts):
    """Markiert Fakten als in Context übernommen."""
    if not merged_facts:
        return

    memory = load_memory(user_id)
    already_merged = memory.get("merged_facts", [])
    already_lower = set(f.lower() for f in already_merged)

    for fact in merged_facts:
        key = _fact_key(fact)
        if key not in already_lower:
            already_merged.append(key)
            already_lower.add(key)

    memory["merged_facts"] = already_merged
    save_memory(user_id, memory)
    logger.info(f"🧠 {len(merged_facts)} Fakten als gemergt markiert")


def cleanup_memory(user_id):
    """Entfernt gemergte Fakten aus der aktiven Liste."""
    memory = load_memory(user_id)
    merged = set(f.lower() for f in memory.get("merged_facts", []))
    before = len(memory["facts"])
    memory["facts"] = [f for f in memory["facts"] if _fact_key(f) not in merged]
    after = len(memory["facts"])

    if before != after:
        save_memory(user_id, memory)
        logger.info(f"🧠 Aufgeräumt: {before - after} gemergte Fakten entfernt")

    return before - after


def format_memory_for_prompt(user_id):
    """Nur UNGEMERGTE Fakten in den Prompt."""
    unmerged = get_unmerged_facts(user_id)
    if not unmerged:
        return None

    lines = ["# Kürzlich gelernte Fakten (noch nicht ins Profil übernommen)", ""]
    for fact in unmerged:
        text = _fact_text(fact)
        cat = fact.get("kategorie", "?") if isinstance(fact, dict) else "?"
        lines.append(f"- [{cat}] {text}")
    lines.append("")
    lines.append("Nutze dieses Wissen natürlich im Gespräch.")

    return "\n".join(lines)


MEMORY_EXTRACTION_PROMPT = """Analysiere den folgenden Gesprächsauszug und extrahiere NEUE Fakten.

KRITISCH WICHTIG:
- Extrahiere NUR Fakten die der USER sagt (Zeilen nach "User:")
- NIEMALS Fakten aus der Assistant-Antwort extrahieren
- NIEMALS Meinungen, Interpretationen oder Aussagen des Assistants speichern
- Wenn der User eine FRAGE stellt, ist das KEIN Fakt (z.B. "Wie findest du X?" → KEIN Fakt)
- Nur AUSSAGEN des Users sind Fakten (z.B. "Ich mag X" → Fakt)

KATEGORIEN:
- "persoenlich": Persönliche Infos über den Nutzer (Vorlieben, Lebensdaten, Beruf, Gesundheit, Pläne, Gewohnheiten, Beziehungen)
- "kommunikation": Wie der Nutzer vom Bot angesprochen werden will (z.B. "kürzere Antworten", "weniger Emojis", "mehr Humor")
- "knowledge": Fachwissen das für die Arbeit relevant ist (z.B. "Formitas hat neuen BIM-Koordinator", "NUK-Projekt verzögert sich")
- "stil_global": Stilanweisungen die den Bot grundsätzlich verändern sollen (z.B. "schreib nie fett", "keine Bulletpoints") → Diese werden NICHT gespeichert, nur gemeldet.

NICHT speichern:
- Tagesstimmungen ("mir geht's gut heute")
- Triviale Gesprächsfetzen
- Fragen des Users (Fragen sind keine Fakten!)
- ALLES was der Assistant/Bot sagt oder meint
- Dinge die SCHON BEKANNT sind (siehe unten)
- Ableitungen oder Interpretationen
- Allgemeinwissen über den Beruf/die Rolle des Nutzers

BEREITS BEKANNTE FAKTEN (NICHT nochmal extrahieren):
{existing_facts}

Gesprächsauszug:
{conversation}

Antworte NUR mit einem JSON-Array. Jeder Eintrag ist ein Objekt:
{{"fakt": "Beschreibung", "kategorie": "persoenlich|kommunikation|knowledge|stil_global"}}

Wenn es keine WIRKLICH NEUEN Fakten gibt: []

Beispiele:
[
  {{"fakt": "Mag den Film Der Marsianer", "kategorie": "persoenlich"}},
  {{"fakt": "Will kürzere Antworten", "kategorie": "kommunikation"}},
  {{"fakt": "NUK-Projekt startet Phase 2 im März", "kategorie": "knowledge"}}
]

Deine Antwort (NUR das JSON-Array):"""
