"""
SchnuBot.ai - Ollama Client (Post-Cutover)
Referenz: Konzeptdokument V1.1

System-Prompt Aufbau:
1. Datum/Uhrzeit
2. soul.md (Identität)
3. style.md (Sprache & Ton) — nur chat-Modus
4. tools.md (Tool-Übersicht) — nur chat-Modus
5. architecture.md (Selbstwissen)
6. Memory-Chunks aus ChromaDB (dynamisch, kontextabhängig)
6.5 Kognitions-Echo (letzte 24h Kognitions-Outputs) — nur chat-Modus
7. Globale Regeln (Preferences/Decisions)
8. Web Search Hinweis — nur chat-Modus
9. Markdown-Verbot — nur chat-Modus

Legacy-Systeme entfernt:
- Kein format_memory_for_prompt (altes Memory)
- Kein load_context (tommy.facts) — jetzt in ChromaDB
- Kein load_knowledge (knowledge/*.md) — jetzt in ChromaDB
- Kein bim.facts — jetzt in ChromaDB
- Kein user.md — in soul.md aufgegangen
- Kein extract_memories — ersetzt durch Konsolidierer + Fast-Track
- Kein rules.md — ersetzt durch style.md
"""

import os
import logging
import time
from config import OLLAMA_API_URL, OLLAMA_API_KEY, OLLAMA_MODEL, BOT_NAME
from memory.retrieval import score_and_select
from memory.prompt_builder import build_memory_prompt, build_global_rules_prompt
from memory.memory_store import query_active

logger = logging.getLogger(__name__)

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(PROJECT_DIR)
SOUL_PATH = os.path.join(ROOT_DIR, "soul.md")
STYLE_PATH = os.path.join(ROOT_DIR, "style.md")
TOOLS_PATH = os.path.join(ROOT_DIR, "tools.md")
ARCHITECTURE_PATH = os.path.join(ROOT_DIR, "architecture.md")


def load_file(path):
    """Lädt eine Datei und ersetzt {{BOT_NAME}} Placeholder."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        return content.replace("{{BOT_NAME}}", BOT_NAME)
    except FileNotFoundError:
        return None


def load_soul():
    """Lädt die Verfassung (soul.md). Fallback auf Minimal-Prompt."""
    return load_file(SOUL_PATH) or f"Du bist {BOT_NAME}, ein hilfreicher Assistent."


def load_style():
    """Lädt Sprache und Ton (style.md)."""
    return load_file(STYLE_PATH)


def load_tools():
    """Lädt die Tool-Übersicht (tools.md)."""
    return load_file(TOOLS_PATH)


def load_architecture():
    """Lädt das Selbstwissen (architecture.md)."""
    return load_file(ARCHITECTURE_PATH)


# =============================================================================
# Kognitions-Echo
# =============================================================================

# Cache: wird pro Prozess einmal gebaut und für 10 Minuten gehalten.
# Kognition läuft alle 2h — 10 Minuten Cache ist kein echtes Staleness-Risiko.
_cognition_echo_cache = None
_cognition_echo_cache_time = 0
_COGNITION_ECHO_TTL = 600  # 10 Minuten


def load_cognition_echo() -> str | None:
    """
    Baut einen kompakten Kognitions-Echo Block aus dem heartbeat_state.json.

    Zeigt Kimi im Chat-Prompt was sie in den letzten 24h gedacht hat:
    - Welche Kognitions-Module gelaufen sind
    - Was dabei herauskam (topic_core aus cognition_session)
    - Confidence-Trend aus self_reflection_summary falls verfügbar

    Ziel: Kimi spürt im Chat was nachts passiert ist — nicht als zufällig
    retrievelter Chunk, sondern als fester Kontext-Block.

    Returns None wenn keine relevanten Kognitions-Daten vorhanden.
    """
    global _cognition_echo_cache, _cognition_echo_cache_time

    if (_cognition_echo_cache is not None and
            (time.time() - _cognition_echo_cache_time) < _COGNITION_ECHO_TTL):
        return _cognition_echo_cache

    try:
        from core.state import load_state
        from core.datetime_utils import now_utc, safe_parse_dt
        from datetime import timedelta

        state = load_state()
        cutoff = (now_utc() - timedelta(hours=24)).isoformat()

        # cognition_session aus State lesen
        session = state.get("cognition_session", {})
        if not session:
            _cognition_echo_cache = None
            _cognition_echo_cache_time = time.time()
            return None

        lines = ["## Was ich zuletzt gedacht habe"]
        has_content = False

        # Module die gelaufen sind + was dabei rauskam
        module_labels = {
            "diary":                "Tagebuch",
            "introspection":        "Mirror-Analyse",
            "moltbook":             "Moltbook",
            "inner_dialogue":       "Innerer Dialog",
            "autonomous_reflection": "Autonome Reflexion",
        }

        for module_key, label in module_labels.items():
            entry = session.get(module_key)
            if not entry:
                continue
            # Nur Einträge der letzten 24h
            ts = entry.get("timestamp", "")
            if ts < cutoff:
                continue

            topic = entry.get("topic", "")
            chunk_id = entry.get("chunk_id", "")
            if not topic and not chunk_id:
                continue

            has_content = True
            ts_short = ts[:16].replace("T", " ") if ts else "?"
            line = f"[{ts_short}] {label}"
            if topic:
                line += f": {topic[:120]}"
            lines.append(line)

        if not has_content:
            _cognition_echo_cache = None
            _cognition_echo_cache_time = time.time()
            return None

        # Selbstbild-Trend anhängen wenn verfügbar
        try:
            from self_reflection_summary import get_confidence_trend, get_recent_reflections
            chunks = get_recent_reflections(limit=10)
            if chunks:
                trend = get_confidence_trend(chunks)
                trend_str = trend.get("trend", "stabil")
                strong = trend.get("strong_count", 0)
                total = trend.get("total", 0)
                lines.append(
                    f"\nSelbstbild-Trend: {trend_str} "
                    f"({strong} starke Überzeugungen von {total} Reflexionen)"
                )
        except Exception:
            pass

        result = "\n".join(lines)
        _cognition_echo_cache = result
        _cognition_echo_cache_time = time.time()
        return result

    except Exception as e:
        logger.debug(f"load_cognition_echo: {e}")
        _cognition_echo_cache = None
        _cognition_echo_cache_time = time.time()
        return None


def invalidate_cognition_echo_cache() -> None:
    """Invalidiert den Kognitions-Echo Cache — wird von orbit_cognition aufgerufen."""
    global _cognition_echo_cache, _cognition_echo_cache_time
    _cognition_echo_cache = None
    _cognition_echo_cache_time = 0


# =============================================================================
# Globale Regeln Cache
# =============================================================================

_global_rules_cache = None
_global_rules_cache_time = 0
_GLOBAL_RULES_TTL = 120  # Sekunden


def _load_global_rules():
    """
    Lädt alle aktiven Preferences und Decisions — IMMER, unabhängig von der Query.
    Gecached für 120s (P1.16): vermeidet Collection-Scan bei jeder Nachricht.

    Returns:
        Liste von Chunk-Dicts, nach Weight*Confidence sortiert (stärkste zuerst)
    """
    global _global_rules_cache, _global_rules_cache_time

    if _global_rules_cache is not None and (time.time() - _global_rules_cache_time) < _GLOBAL_RULES_TTL:
        return _global_rules_cache

    global_chunks = []

    try:
        collection = __import__('memory.memory_store', fromlist=['get_active_collection']).get_active_collection()
        all_data = collection.get(
            where={"$or": [{"chunk_type": "preference"}, {"chunk_type": "decision"}]},
            include=["documents", "metadatas"],
        )

        if all_data["ids"]:
            for i, chunk_id in enumerate(all_data["ids"]):
                meta = all_data["metadatas"][i]
                text = all_data["documents"][i]

                if meta.get("status", "active") != "active":
                    continue

                try:
                    weight = float(meta.get("weight", 1.0))
                    confidence = float(meta.get("confidence", 0.5))
                except (ValueError, TypeError):
                    weight, confidence = 1.0, 0.5

                global_chunks.append({
                    "id": chunk_id,
                    "text": text,
                    "chunk_type": meta.get("chunk_type", "preference"),
                    "source": meta.get("source", "tommy"),
                    "weight": weight,
                    "confidence": confidence,
                    "epistemic_status": meta.get("epistemic_status", "stated"),
                    "created_at": meta.get("created_at", ""),
                    "tags": meta.get("tags") if isinstance(meta.get("tags"), list) else [t.strip() for t in str(meta.get("tags", "")).split(",") if t.strip()],
                })

            global_chunks.sort(key=lambda c: c["weight"] * c["confidence"], reverse=True)

    except Exception as e:
        logger.warning(f"Globale Regeln laden fehlgeschlagen: {e}")

    _global_rules_cache = global_chunks
    _global_rules_cache_time = time.time()

    return global_chunks


# =============================================================================
# System-Prompt Builder
# =============================================================================

def build_system_prompt(context_name=None, user_id=None, user_message=None, doc_context=None, mode="chat", extra_system=None):
    """
    Baut den System-Prompt dynamisch zusammen.

    mode="chat"     → vollständiger WhatsApp-Prompt (style, tools, Kognitions-Echo,
                      Markdown-Verbot, Websearch)
    mode="internal" → schlanker Denkraum-Prompt (soul, architecture, memory, globale Regeln)
                      Kein WhatsApp-Kostüm, kein Deutsch-Zwang, kein Chat-Kontext.
                      Für Heartbeat, Moltbook, Diary, MIRROR — alles was nicht Tommy-Chat ist.

    extra_system    → optionaler Zusatzblock am Ende (z.B. Explorer-Kontext, Task-Beschreibung)
    """
    parts = []

    # 1. Datum/Uhrzeit (Berliner Zeit für Tommy)
    from core.datetime_utils import format_berlin
    parts.append(f"Aktuelles Datum und Uhrzeit: {format_berlin()}")

    # 2. Verfassung — immer, in beiden Modi
    parts.append(load_soul())

    if mode == "chat":
        # 3. Stil & Sprache (chat only)
        style = load_style()
        if style:
            parts.append(style)

        # 4. Tool-Übersicht (chat only)
        tools = load_tools()
        if tools:
            parts.append(tools)
    else:
        parts.append(
            "INTERNER MODUS:\n"
            "Das ist kein Gespräch mit Tommy. Kein WhatsApp. Kein Assistent-Modus.\n"
            "Ich denke gerade für mich selbst — reflektiere, erkunde, verarbeite.\n"
            "Keine Anrede, keine Chat-Floskeln, kein WhatsApp-Stil.\n"
            "Antwort exakt im geforderten Format, in der geforderten Sprache."
        )

    # 5. Selbstwissen — immer, in beiden Modi
    arch = load_architecture()
    if arch:
        parts.append(arch)

    # 6. Globale Regeln laden (IDs merken für Deduplizierung)
    global_rules = _load_global_rules()
    global_rule_ids = set()
    if global_rules:
        global_rule_ids = {c["id"] for c in global_rules}

    # 7. Memory-Chunks (kontextabhängig)
    if user_message and not doc_context:
        try:
            chunks = score_and_select(user_message)
            if global_rule_ids:
                chunks = [c for c in chunks if c["id"] not in global_rule_ids]
            memory_prompt = build_memory_prompt(chunks)
            if memory_prompt:
                parts.append(memory_prompt)
        except Exception as e:
            logger.warning(f"Memory-Retrieval fehlgeschlagen: {e}")

    # 7.5 Kognitions-Echo + Tommy-Kontext — nur chat-Modus
    # Beide nach Memory, vor globalen Regeln.
    # Kimi sieht was sie zuletzt gedacht hat UND wer ihr Gegenüber ist.
    if mode == "chat":
        echo = load_cognition_echo()
        if echo:
            parts.append(echo)

        try:
            from tommy_model import build_tommy_context
            tommy_ctx = build_tommy_context()
            if tommy_ctx:
                parts.append(tommy_ctx)
        except Exception as _te:
            logger.debug(f"build_tommy_context nicht verfuegbar: {_te}")

    # 8. Globale Regeln am ENDE — nach Memory, vor der User-Nachricht
    if global_rules:
        rules_prompt = build_global_rules_prompt(global_rules)
        if rules_prompt:
            parts.append(rules_prompt)

    if mode == "chat":
        # 9. Web Search
        parts.append(
            "WEB SEARCH VERFUEGBAR:\n"
            "Wenn du aktuelle Informationen benoenigst (News, Preise, aktuelle Ereignisse, Fakten die du nicht sicher kennst), "
            "schreibe in deine Antwort: [SEARCH: deine suchanfrage]\n"
            "Beispiel: [SEARCH: aktueller Bitcoin Preis]\n"
            "Nur EINEN Search-Block pro Antwort. Nur wenn wirklich noetig — nicht bei allgemeinem Wissen."
        )

        # 10. Markdown-Verbot
        parts.append(
            "KEIN MARKDOWN. Keine Sternchen, keine Rauten, keine Trennlinien, keine Unterstriche, keine Backticks. "
            "Nur Fliesstext und Zeilenumbrueche. WhatsApp rendert Markdown nicht — es erscheint als Zeichensalat."
        )

    # 11. Optionaler Zusatzblock
    if extra_system:
        parts.append(extra_system)

    # 12. Dokument-Kontext — ganz am Ende, höchste Recency-Priorität
    if doc_context:
        parts.append(
            "DOKUMENT-KONTEXT (bereits extrahiert, liegt vollstaendig vor):\n\n" + doc_context
        )

    return "\n\n---\n\n".join(parts)


# =============================================================================
# Token-Tracking
# =============================================================================

def _track_tokens(prompt_tokens, completion_tokens):
    import json
    from datetime import datetime, timezone
    data_dir = os.path.join(ROOT_DIR, "data")
    os.makedirs(data_dir, exist_ok=True)
    path = os.path.join(data_dir, "token_usage.json")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        with open(path, "r") as f:
            usage = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        usage = {}
    if today not in usage:
        usage[today] = {"prompt": 0, "completion": 0, "total": 0, "calls": 0}
    usage[today]["prompt"] += prompt_tokens
    usage[today]["completion"] += completion_tokens
    usage[today]["total"] += prompt_tokens + completion_tokens
    usage[today]["calls"] += 1
    keys = sorted(usage.keys())
    if len(keys) > 90:
        for k in keys[:-90]:
            del usage[k]
    with open(path, "w") as f:
        json.dump(usage, f, indent=2)


# =============================================================================
# Ollama API
# =============================================================================

def _call_ollama(messages):
    """
    Low-Level Ollama API Call — gemeinsame Basis für chat() und chat_internal().
    """
    from api_utils import api_call_with_retry

    result = api_call_with_retry(
        url=f"{OLLAMA_API_URL}/api/chat",
        headers={
            "Authorization": f"Bearer {OLLAMA_API_KEY}",
            "Content-Type": "application/json",
        },
        json_payload={"model": OLLAMA_MODEL, "messages": messages, "stream": False},
        timeout=120,
    )

    if not result:
        return None

    try:
        _track_tokens(
            prompt_tokens=result.get("prompt_eval_count", 0),
            completion_tokens=result.get("eval_count", 0),
        )
    except Exception:
        pass

    return result


def chat(user_id, message, chat_history, context_name=None, doc_context=None):
    """Sendet eine Nachricht an Kimi und gibt die Antwort zurück.

    WICHTIG: chat_history enthält die aktuelle User-Nachricht bereits.
    message wird nur für build_system_prompt (Memory-Retrieval) verwendet.

    Returns:
        str — Kimi-Antwort
        dict — turn_meta mit chunks + global_rules für MIRROR-Logging
    """
    retrieved_chunks = []
    active_global_rules = []
    try:
        if message and not doc_context:
            retrieved_chunks = score_and_select(message)
        active_global_rules = _load_global_rules()
    except Exception as e:
        logger.warning(f"MIRROR chunk-fetch fehlgeschlagen: {e}")

    system_prompt = build_system_prompt(
        context_name=context_name,
        user_id=user_id,
        user_message=message,
        doc_context=doc_context,
        mode="chat",
    )
    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(chat_history)

    result = _call_ollama(messages)

    if not result:
        return "Sorry, Kimi ist gerade nicht erreichbar. Versuch's gleich nochmal!", {}

    response_text = result.get("message", {}).get("content", "Hmm, da kam keine Antwort zurueck.")
    turn_meta = {
        "chunks": retrieved_chunks,
        "global_rules": active_global_rules,
    }
    return response_text, turn_meta


def chat_internal(user_id, message, chat_history=None, context_name=None, doc_context=None, extra_system=None):
    """
    Interner Kimi-Call für Heartbeat, Moltbook, Diary, MIRROR.

    Returns:
        str — Kimi-Antwort
        dict — turn_meta mit chunks + global_rules
    """
    chat_history = chat_history or []

    retrieved_chunks = []
    active_global_rules = []
    try:
        if message and not doc_context:
            retrieved_chunks = score_and_select(message)
        active_global_rules = _load_global_rules()
    except Exception as e:
        logger.warning(f"Internal chunk-fetch fehlgeschlagen: {e}")

    system_prompt = build_system_prompt(
        context_name=context_name,
        user_id=user_id,
        user_message=message,
        doc_context=doc_context,
        mode="internal",
        extra_system=extra_system,
    )

    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(chat_history)
    messages.append({"role": "user", "content": message})

    result = _call_ollama(messages)
    if not result:
        return "", {"chunks": retrieved_chunks, "global_rules": active_global_rules}

    response_text = result.get("message", {}).get("content", "").strip()
    return response_text, {
        "chunks": retrieved_chunks,
        "global_rules": active_global_rules,
    }
