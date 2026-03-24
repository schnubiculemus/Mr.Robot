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

        # Identitäts-Delta — Kimi im Wandel
        try:
            from self_reflection_summary import build_identity_delta
            delta = build_identity_delta()
            if delta:
                lines.append("")
                lines.append(delta)
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


def build_time_context(user_id: str = None) -> str:
    """
    Baut einen kompakten Zeitkontext-Block für den System-Prompt.

    Gibt Kimi ein Gefühl für:
    - Tagesphase (Nacht / früher Morgen / Morgen / Mittag / Nachmittag / Abend / später Abend)
    - Wie lange seit dem letzten Gespräch mit Tommy
    - Ob heute schon gesprochen wurde
    - Wie lange bis zum nächsten Kognitions-Run (ca.)
    - Ob sie gerade in einer aktiven Gesprächsphase ist oder allein
    """
    try:
        from core.datetime_utils import now_berlin, now_utc, safe_parse_dt
        from datetime import timedelta

        berlin = now_berlin()
        hour = berlin.hour

        # Tagesphase
        if 0 <= hour < 5:
            phase = "Nacht — Tommy schläft wahrscheinlich"
        elif 5 <= hour < 7:
            phase = "früher Morgen"
        elif 7 <= hour < 10:
            phase = "Morgen"
        elif 10 <= hour < 13:
            phase = "Vormittag"
        elif 13 <= hour < 15:
            phase = "Mittagszeit"
        elif 15 <= hour < 18:
            phase = "Nachmittag"
        elif 18 <= hour < 21:
            phase = "Abend"
        elif 21 <= hour < 23:
            phase = "später Abend"
        else:
            phase = "Nacht"

        lines = [f"Tagesphase: {phase}"]

        # Letztes Gespräch mit Tommy
        if user_id:
            try:
                from core.database import get_connection
                conn = get_connection()
                try:
                    row = conn.execute(
                        """SELECT timestamp FROM messages
                           WHERE phone_number = ?
                           ORDER BY timestamp DESC, id DESC LIMIT 1""",
                        (user_id,)
                    ).fetchone()
                finally:
                    conn.close()

                if row and row["timestamp"]:
                    last_ts = safe_parse_dt(row["timestamp"])
                    if last_ts:
                        delta = now_utc() - last_ts
                        mins = int(delta.total_seconds() / 60)
                        if mins < 2:
                            lines.append("Letztes Gespräch: gerade eben")
                        elif mins < 60:
                            lines.append(f"Letztes Gespräch: vor {mins} Minuten")
                        elif mins < 120:
                            lines.append(f"Letztes Gespräch: vor ca. einer Stunde")
                        elif mins < 1440:
                            h = mins // 60
                            lines.append(f"Letztes Gespräch: vor ca. {h} Stunden")
                        else:
                            d = mins // 1440
                            lines.append(f"Letztes Gespräch: vor {d} Tag(en)")
                    else:
                        lines.append("Letztes Gespräch: unbekannt")
                else:
                    lines.append("Letztes Gespräch: noch kein Gespräch heute")
            except Exception:
                pass

        # Nächster Kognitions-Run (Cron: 0 */2 * * *)
        next_cron_hour = ((hour // 2) + 1) * 2
        if next_cron_hour >= 24:
            next_cron_hour = 0
        mins_to_cron = ((next_cron_hour - hour) * 60) - berlin.minute
        if mins_to_cron <= 0:
            mins_to_cron += 120
        if mins_to_cron < 15:
            lines.append(f"Nächster Kognitions-Run: in ~{mins_to_cron} Minuten")
        elif mins_to_cron < 60:
            lines.append(f"Nächster Kognitions-Run: in ca. {mins_to_cron} Minuten")
        else:
            lines.append(f"Nächster Kognitions-Run: in ca. {mins_to_cron // 60}h {mins_to_cron % 60}min")

        return "## Zeitkontext\n" + "\n".join(lines)

    except Exception as e:
        logger.debug(f"build_time_context fehlgeschlagen (unkritisch): {e}")
        return ""


def build_perspective_context() -> str | None:
    """
    Baut einen kompakten Block mit Kimis eigenen Positionen und Haltungen.

    Nutzt proposed_pattern-Chunks mit hoher Confidence oder confirmed-Status —
    das sind Kimis akkumulierte Überzeugungen die sie aus Reflexion und Erfahrung
    gewonnen hat.

    Kimi kennt dadurch bei Gesprächen ihre eigene Haltung zu bekannten Themen
    und muss sie nicht jedes Mal neu ableiten.

    Zeigt max. 4 Positionen — die stärksten zuerst.
    """
    try:
        from memory.memory_store import get_active_collection

        col = get_active_collection()
        result = col.get(
            where={"$and": [
                {"source": "robot"},
                {"status": "active"},
                {"chunk_type": "proposed_pattern"},
            ]},
            include=["documents", "metadatas"],
        )

        if not result["ids"]:
            return None

        positions = []
        for i, chunk_id in enumerate(result["ids"]):
            meta  = result["metadatas"][i]
            text  = result["documents"][i]
            conf  = float(meta.get("confidence", 0.5))
            epist = meta.get("epistemic_status", "inferred")

            # Nur starke oder bestätigte Positionen
            if conf >= 0.65 or epist == "confirmed":
                positions.append({
                    "text":       text,
                    "confidence": conf,
                    "epistemic":  epist,
                    "created_at": meta.get("created_at", ""),
                })

        if not positions:
            return None

        # Stärkste zuerst — confirmed vor inferred, dann nach Confidence
        positions.sort(key=lambda p: (
            0 if p["epistemic"] == "confirmed" else 1,
            -p["confidence"]
        ))

        lines = ["## Meine Positionen"]
        for p in positions[:4]:
            marker = "✓" if p["epistemic"] == "confirmed" else "→"
            lines.append(f"{marker} {p['text'][:150]}")

        return "\n".join(lines)

    except Exception as e:
        logger.debug(f"build_perspective_context fehlgeschlagen (unkritisch): {e}")
        return None


def build_goals_context() -> str | None:
    """
    Baut einen Block mit Kimis langfristigen Zielen für den System-Prompt.
    Liest aus SQLite (kimi_goals) — nicht mehr aus ChromaDB.
    Eine Wahrheit für Goals: SQLite.
    """
    try:
        from core.goal_service import get_active_goals
        from config import OWNER_ID
        goals = get_active_goals(OWNER_ID)
        if not goals:
            return None

        lines = ["## Meine Ziele"]
        for g in goals[:5]:
            progress = g.get("progress", 0)
            progress_str = f" ({progress}%)" if progress > 0 else ""
            lines.append(f"→ {g['title'][:150]}{progress_str}")
        return "\n".join(lines)

    except Exception as e:
        logger.debug(f"build_goals_context fehlgeschlagen (unkritisch): {e}")
        return None

def build_system_prompt(context_name=None, user_id=None, user_message=None, doc_context=None, mode="chat", extra_system=None, prefetched_chunks=None):
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

    # 1. Datum/Uhrzeit + Zeitkontext (Berliner Zeit für Tommy)
    from core.datetime_utils import format_berlin
    parts.append(f"Aktuelles Datum und Uhrzeit: {format_berlin()}")

    # Zeitgefühl: Tagesphase, letztes Gespräch, nächster Kognitions-Run
    time_ctx = build_time_context(user_id=user_id)
    if time_ctx:
        parts.append(time_ctx)

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

        # Tool-Syntax im internen Modus — nur Todo-Anlage
        parts.append(
            "VERFÜGBARE WERKZEUGE (interne Nutzung):\n"
            "Todo anlegen:\n"
            "  [TODO_ACTION: {\"action\": \"create\", \"title\": \"...\"," 
            " \"category\": \"kimi\", \"priority\": \"mittel\"}]\n"
            "Wenn ich ein Vorhaben habe: TODO anlegen. Nicht beschreiben — anlegen."
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

    # ==========================================================================
    # WP3: Memory Layer — Reihenfolge der Kontextquellen
    # Priorität: AWC → Fast-Track → Typed Memory → Diary → Rest
    # Memory unterstützt Kimi Core, führt nicht.
    # ==========================================================================

    # 7a. Active Working Context — Primäranker (WP2), bereits in kimi_core injiziert
    # Hier nochmal als expliziter Marker in der Reihenfolge:
    # AWC kommt via doc_context aus kimi_core.process() -- kein doppeltes Laden nötig

    # 7b. Fast-Track — kurzfristige Relevanz (vor breitem Typed Memory)
    if not doc_context:
        try:
            from memory.fast_track import get_fast_track_chunks
            ft_chunks = get_fast_track_chunks(user_id=user_id, limit=5)
            if ft_chunks:
                ft_lines = ["## Fast-Track (aktuelle Relevanz)"]
                for fc in ft_chunks:
                    ft_lines.append(f"- {fc.get('text','')[:200]}")
                parts.append("\n".join(ft_lines))
        except Exception as _ft_e:
            logger.debug(f"Fast-Track nicht verfügbar: {_ft_e}")

    # 7c. Typed Memory / ChromaDB — nachgeordnete Kontextquelle
    # NUR prefetched_chunks, kein heimliches Retrieval
    # Retrieval wird immer vom Aufrufer gemacht (chat() oder chat_internal())
    # build_system_prompt() formatiert nur — es entscheidet nicht selbst was relevant ist
    if not doc_context and prefetched_chunks is not None:
        try:
            chunks = prefetched_chunks
            if global_rule_ids:
                chunks = [c for c in chunks if c["id"] not in global_rule_ids]
            # WP3: Diary-Chunks (self_reflection source=robot) werden hier mitformatiert
            # aber nicht als operative Triggerquelle behandelt -- nur als Stil-/Identitätsspur
            memory_prompt = build_memory_prompt(chunks)
            if memory_prompt:
                parts.append(memory_prompt)
        except Exception as e:
            logger.warning(f"Memory-Prompt-Bau fehlgeschlagen: {e}")

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

        # Kimis eigene Positionen und Haltungen
        try:
            perspective = build_perspective_context()
            if perspective:
                parts.append(perspective)
        except Exception as _pe:
            logger.debug(f"build_perspective_context fehlgeschlagen (unkritisch): {_pe}")

        # Kimis langfristige Ziele (chat)
        try:
            goals = build_goals_context()
            if goals:
                parts.append(goals)
        except Exception as _ge:
            logger.debug(f"build_goals_context fehlgeschlagen (unkritisch): {_ge}")

    # 7.6 Ziele auch im internen Modus — Kimi soll intern zielgerichtet denken
    if mode != "chat":
        try:
            goals = build_goals_context()
            if goals:
                parts.append(goals)
        except Exception as _ge:
            logger.debug(f"build_goals_context (internal) fehlgeschlagen: {_ge}")

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

    WP3 Memory-Verbote:
    - Memory darf keine Prioritäten setzen (nur Kimi Core entscheidet)
    - Memory darf den AWC nicht überschreiben
    - Memory darf keine Tasks starten
    - Memory darf keine Workspace-Dokumente anlegen
    - Retrieval nur als Unterstützung, nicht als Steuerinstanz

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
        user_message=message,  # Memory-Retrieval hier — gleiche Chunks für Prompt und MIRROR
        doc_context=doc_context,
        mode="chat",
        prefetched_chunks=retrieved_chunks,  # bereits geladen, nicht nochmal fetchen
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


def chat_internal(user_id, message, chat_history=None, context_name=None, doc_context=None,
                  extra_system=None, retrieval_query=None, prefetched_chunks=None):
    """
    Interner Kimi-Call für Heartbeat, Moltbook, Diary, MIRROR, ORBIT.

    Schritt D: Retrieval explizit, einmal, vor dem Prompt-Bau.
    prefetched_chunks übernimmt wenn bereits geholt.
    retrieval_query überschreibt message als Retrieval-Basis (für interne Themen).

    Returns:
        str — Kimi-Antwort
        dict — turn_meta mit chunks + global_rules (dieselbe Basis wie Prompt)
    """
    chat_history = chat_history or []
    retrieved_chunks = []
    active_global_rules = []

    # Retrieval — einmal, bewusst
    if prefetched_chunks is not None:
        # Bereits von außen geholt — direkt verwenden
        retrieved_chunks = prefetched_chunks
    elif not doc_context:
        query = retrieval_query or message
        if query:
            try:
                retrieved_chunks = score_and_select(query)
            except Exception as e:
                logger.warning(f"chat_internal: Retrieval fehlgeschlagen: {e}")

    try:
        active_global_rules = _load_global_rules()
    except Exception:
        pass

    system_prompt = build_system_prompt(
        context_name=context_name,
        user_id=user_id,
        user_message=message,
        doc_context=doc_context,
        mode="internal",
        extra_system=extra_system,
        prefetched_chunks=retrieved_chunks,
    )

    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(chat_history)
    messages.append({"role": "user", "content": message})

    result = _call_ollama(messages)
    if not result:
        return "", {}

    response_text = result.get("message", {}).get("content", "").strip()
    turn_meta = {
        "chunks": retrieved_chunks,
        "global_rules": active_global_rules,
    }
    return response_text, turn_meta
