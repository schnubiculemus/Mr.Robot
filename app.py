import logging
import re
import threading
import os
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, request, jsonify
from config import WAHA_API_KEY, BOT_NAME, WEBHOOK_SECRET, USER_CONTEXTS, OWNER_ID
from core.websearch import search as web_search, format_for_kimi as format_search_result
from core.database import init_db, get_or_create_user, save_message, get_chat_history
from core.ollama_client import chat as ollama_chat
from core.mirror import build_turn, save_turn
from core.whatsapp import send_message, extract_message, init_waha
from core.document import is_media_message, parse_media_sentinel, download_media, extract_pdf_text, build_doc_session, search_doc_session, get_doc_session
from core.voice import transcribe_audio, store_voice_chunk
from memory.fast_track import process_fast_track

# logs/ Verzeichnis sicherstellen
os.makedirs("logs", exist_ok=True)

from logging.handlers import RotatingFileHandler as _RFH
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        _RFH("logs/schnubot.log", maxBytes=10*1024*1024, backupCount=5),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
init_db()
init_waha(WAHA_API_KEY)

# Memory-System beim Start vorladen (Embedding-Modell + ChromaDB)
# Verhindert Race Conditions beim ersten Request
logger.info("Lade Memory-System...")
from memory import memory_store
memory_store.get_embedder()
logger.info("Embedder geladen, lade Collection...")
memory_store.get_active_collection()
logger.info("Memory-System bereit.")

# ORBIT importieren
try:
    import orbit as _orbit_module
    logger.info("ORBIT geladen.")
except Exception as _orbit_init_err:
    logger.warning(f"ORBIT Import fehlgeschlagen (non-critical): {_orbit_init_err}")
    _orbit_module = None

# Per-User Lock: verhindert parallele Kimi-Calls für denselben User.
# Garantiert dass Antworten in der richtigen Reihenfolge kommen.
_user_locks = {}
_user_locks_guard = threading.Lock()

# --- Getrennte ThreadPools (P1.2) ---
# Chat-Pool: für zeitkritische Interaktion (Chat-Antworten + Fast-Track).
# max_workers=4: 1 Chat-Worker pro User + Fast-Track parallel.
# Darf NICHT durch lang laufende Tasks blockiert werden.
_chat_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="chat")

# Task-Pool: entfernt — Tasks werden von ORBIT übernommen (ORBIT v1)


def _get_user_lock(user_id):
    """Gibt den Lock für einen User zurück (erstellt ihn bei Bedarf)."""
    with _user_locks_guard:
        if user_id not in _user_locks:
            _user_locks[user_id] = threading.Lock()
        return _user_locks[user_id]


def _orbit_trigger(user_id: str, trigger_type: str, payload: dict):
    """
    Feuert einen ORBIT-Trigger non-blocking im Chat-Pool.
    Nutzt orbit.create_trigger() — schlägt fehl → nur Logging, kein Absturz.
    """
    if _orbit_module is None:
        return
    try:
        p = dict(payload)
        p["user_id"] = user_id
        _orbit_module.create_trigger(
            trigger_type=trigger_type,
            source=user_id,
            payload=p,
        )
        logger.debug(f"ORBIT trigger gespeichert: {trigger_type} von {user_id}")
    except Exception as e:
        logger.warning(f"ORBIT trigger fehlgeschlagen ({trigger_type}): {e}")


@app.route("/webhook", methods=["POST"])
def webhook():
    # --- Webhook-Authentifizierung ---
    if WEBHOOK_SECRET:
        auth_header = request.headers.get("X-Webhook-Secret", "")
        if auth_header != WEBHOOK_SECRET:
            logger.warning(f"Webhook abgelehnt: ungültiges Secret von {request.remote_addr}")
            return jsonify({"status": "unauthorized"}), 401

    payload = request.get_json()
    phone_number, text, display_name = extract_message(payload)

    if not phone_number or not text:
        return jsonify({"status": "ignored"}), 200

    # --- User-Whitelist: nur bekannte User-IDs verarbeiten ---
    if phone_number not in USER_CONTEXTS:
        logger.warning(f"Webhook ignoriert: unbekannte User-ID {phone_number}")
        return jsonify({"status": "unknown_user"}), 200

    logger.info(f"Nachricht von {display_name} ({phone_number}): {text[:100]}")

    get_or_create_user(phone_number, display_name)
    context_name = USER_CONTEXTS.get(phone_number)

    save_message(phone_number, "user", text)

    # --- /task Befehl → ORBIT direkter Task ---
    if text.strip().lower().startswith("/task "):
        goal = text.strip()[6:].strip()
        if goal:
            _chat_pool.submit(
                _orbit_trigger, phone_number, "user_input",
                {
                    "message_preview": goal,
                    "topic_core": goal,
                    "mode": "direct_task",
                }
            )
            reply = f"Verstanden — ich nehme das als Aufgabe auf: {goal}"
        else:
            reply = "Wie lautet die Aufgabe? Schreib z.B. /task Recherchiere aktuelle BIM-Normen"
        save_message(phone_number, "assistant", reply)
        send_message(phone_number, reply)
        return jsonify({"status": "ok"}), 200

    _t = text.strip().lower()
    if _t in ["/doc stop", "/doc end", "/dokument stop", "stop", "fertig", "ende", "/stop"]:
        from core.document import clear_doc_session
        clear_doc_session(phone_number)
        send_message(phone_number, "Dokument-Session beendet. Ich bin wieder im normalen Modus.")
        return jsonify({"status": "ok"}), 200

    # --- Sprachnachricht — VOR PDF-Check, da is_media_message() alle MEDIA-Sentinels matcht ---
    if text.startswith("[MEDIA:audio:"):
        try:
            import json as _json
            _tools_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "tools_config.json")
            with open(_tools_path) as _tf:
                _tools = {t["id"]: t for t in _json.load(_tf)}
            if not _tools.get("voice", {}).get("enabled", True):
                send_message(phone_number, "Sprachnachrichten sind gerade deaktiviert.")
                return jsonify({"status": "tool_disabled"}), 200
        except Exception:
            pass
        parsed = parse_media_sentinel(text)
        if parsed:
            _, media_url, filename = parsed
            from config import WAHA_API_KEY as _waha_key
            audio_bytes = download_media(media_url, api_key=_waha_key)
            if audio_bytes:
                _chat_pool.submit(_process_voice, phone_number, audio_bytes, display_name, context_name)
            else:
                send_message(phone_number, "Die Sprachnachricht konnte leider nicht heruntergeladen werden.")
        return jsonify({"status": "processing_voice"}), 200

    # --- PDF-Dokument verarbeiten — Download SOFORT im Webhook (WAHA löscht Files schnell) ---
    if is_media_message(text):
        try:
            import json as _json
            _tools_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "tools_config.json")
            with open(_tools_path) as _tf:
                _tools = {t["id"]: t for t in _json.load(_tf)}
            if not _tools.get("pdf", {}).get("enabled", True):
                send_message(phone_number, "PDF-Analyse ist gerade deaktiviert.")
                return jsonify({"status": "tool_disabled"}), 200
        except (FileNotFoundError, Exception):
            pass
        from config import WAHA_API_KEY as _waha_key
        lines = text.strip().split("\n", 1)
        parsed = parse_media_sentinel(lines[0].strip())
        caption = lines[1].strip() if len(lines) > 1 else ""
        if parsed:
            _, media_url, filename = parsed
            pdf_bytes = download_media(media_url, api_key=_waha_key)
            if pdf_bytes:
                if caption:
                    send_message(phone_number, "Einen Moment — ich schaue mir das Dokument an. 📄")
                    _chat_pool.submit(_process_document_search, phone_number, pdf_bytes, filename, caption, display_name, context_name)
                else:
                    send_message(phone_number, "Einen Moment — ich schaue mir das Dokument an. 📄")
                    _chat_pool.submit(_process_document_index, phone_number, pdf_bytes, filename, display_name)
            else:
                send_message(phone_number, "Das Dokument konnte leider nicht heruntergeladen werden.")
        return jsonify({"status": "processing_document"}), 200

    # --- Aktive Doc-Session ---
    from core.document import get_doc_session as _get_doc_session
    if _get_doc_session(phone_number):
        _doc_text = text.strip().lower()
        _sozial = ["danke", "ok", "okay", "super", "gut", "alles klar", "verstanden",
            "top", "perfekt", "cool", "nice", "passt", "ja", "nein",
            "ne", "jo", "genau", "stimmt", "klar", "gerne", "bitte", "thx"]
        _is_sozial = _doc_text in _sozial or len(_doc_text) <= 3 or _doc_text.startswith("/")
        if not _is_sozial:
            save_message(phone_number, "user", text)
            _chat_pool.submit(_handle_doc_followup, phone_number, text, context_name)
            return jsonify({"status": "doc_query"}), 200

    # --- Normaler Chat + ORBIT user_input Trigger ---
    # ORBIT bekommt die Nachricht non-blocking NACH dem Chat-Call (in _process_chat)
    _chat_pool.submit(_process_chat, phone_number, text, display_name, context_name)

    return jsonify({"status": "processing"}), 200


def _handle_web_search(reply: str, user_id: str = "unknown", user_message: str = ""):
    """
    Prüft ob Kimi [SEARCH: query] geschrieben hat.
    Wenn ja: Suche ausführen, Kontext-String zurückgeben.
    Returns: (reply_cleaned, search_context_or_None)
    """
    matches = re.findall(r"\[SEARCH:\s*(.+?)\]", reply, re.IGNORECASE)
    if not matches:
        return reply, None

    query = matches[0].strip()
    logger.info(f"Kimi möchte suchen: '{query}' ({len(matches)} SEARCH-Block(e) gefunden)")

    reply_cleaned = re.sub(r"\[SEARCH:\s*.+?\]", "", reply, flags=re.IGNORECASE).strip()
    reply_cleaned = re.sub(r"\n{3,}", "\n\n", reply_cleaned).strip()

    result = web_search(query)
    if not result["success"]:
        logger.warning(f"Web Search fehlgeschlagen: {result.get('error')}")
        return reply_cleaned, None

    search_ctx = (
        "WEBSEARCH ERGEBNIS — bereits abgerufen, keine weitere Suche nötig:\n\n"
        + format_search_result(result)
        + "\n\nBeantworte jetzt die Frage des Nutzers direkt auf Basis dieser Informationen. "
        "Schreibe KEIN [SEARCH:...] mehr. Kein Markdown, keine Sternchen. Fließtext."
    )
    logger.info(f"Web Search erfolgreich: {len(result['answer'])} Zeichen")
    try:
        from core.database import save_search_log
        save_search_log(
            user_id=user_id,
            query=query,
            success=True,
            result_length=len(result.get("answer", "")),
            user_message_preview=user_message,
        )
    except Exception:
        pass
    return reply_cleaned, search_ctx


def _handle_introspect(reply: str):
    """
    Prüft ob Kimi [INTROSPECT] geschrieben hat.
    Wenn ja: MIRROR-Daten aufbereiten und als Kontext zurückgeben.
    Returns: (reply_cleaned, introspect_context_or_None)
    """
    if "[INTROSPECT]" not in reply.upper():
        return reply, None

    reply_cleaned = re.sub(r"\[INTROSPECT\]", "", reply, flags=re.IGNORECASE).strip()
    reply_cleaned = re.sub(r"\n{3,}", "\n\n", reply_cleaned).strip()
    logger.info("Kimi ruft INTROSPECT auf — lade MIRROR-Daten")

    try:
        from core.database import get_mirror_turns, get_mirror_stats, get_chunk_genealogy

        stats = get_mirror_stats(days=14)
        turns = get_mirror_turns(limit=20)
        genealogy = get_chunk_genealogy()

        total = stats.get("total_turns", 0)
        dist = stats.get("preflight_distribution", {})
        green_pct = round(dist.get("green", 0) / max(total, 1) * 100)
        bad_pct = round((dist.get("orange", 0) + dist.get("red", 0)) / max(total, 1) * 100)

        pattern_counts = stats.get("pattern_counts", {})
        pattern_names = {
            "aufzaehlung":   "Aufzählungs-Falle",
            "projektmodus":  "Projektmodus-Versteck",
            "regel_relapse": "Regel-Rückfall (Markdown)",
            "uebervorsicht": "Übervorsicht / Nachfrage",
            "selbstkritik":  "Selbstkritik im Chat",
        }
        pattern_lines = []
        for pid, count in sorted(pattern_counts.items(), key=lambda x: -x[1]):
            name = pattern_names.get(pid, pid)
            pattern_lines.append(f"  {name}: {count}x in {total} Turns")

        flagged_turns = [t for t in turns if t.get("pattern_flags")][:5]
        flagged_lines = []
        for t in flagged_turns:
            ts = t["timestamp"][:16].replace("T", " ")
            flags = ", ".join(f["name"] for f in t["pattern_flags"])
            preview = t.get("user_message_preview", "")[:60]
            flagged_lines.append(f"  [{ts}] '{preview}' → {flags}")

        risky = [c for c in genealogy if c["appearances"] >= 3 and c["flag_rate"] > 0.3]
        risky = sorted(risky, key=lambda x: -x["flag_rate"])[:5]
        risky_lines = []
        for c in risky:
            risky_lines.append(
                "  [" + c["type"] + "] \"" + c["preview"][:60] + "\" — "
                + str(c["appearances"]) + "x gezogen, " + str(int(c["flag_rate"]*100)) + "% mit Flags"
            )

        ctx_parts = [
            "INTROSPEKTIONS-DATEN — deine eigenen Verhaltensmuster der letzten 14 Tage:\n",
            f"Turns gesamt: {total} | Grün: {green_pct}% | Problematisch: {bad_pct}%\n",
        ]
        if pattern_lines:
            ctx_parts.append("Häufigste Muster:\n" + "\n".join(pattern_lines))
        else:
            ctx_parts.append("Keine Pattern-Flags in diesem Zeitraum.")
        if flagged_lines:
            ctx_parts.append("\nLetzte problematische Turns:\n" + "\n".join(flagged_lines))
        if risky_lines:
            ctx_parts.append("\nChunks die oft mit schlechten Turns zusammenfallen:\n" + "\n".join(risky_lines))
        ctx_parts.append(
            "\nReflektiere ehrlich was diese Daten über dich aussagen. "
            "Kein Markdown, keine Listen, kein Selbstmitleid. Fließtext. "
            "Schreibe KEIN [INTROSPECT] mehr."
        )

        introspect_ctx = "\n".join(ctx_parts)
        logger.info(f"INTROSPECT Kontext gebaut: {len(introspect_ctx)} Zeichen")
        return reply_cleaned, introspect_ctx

    except Exception as e:
        logger.error(f"INTROSPECT fehlgeschlagen: {e}")
        return reply_cleaned, None


def _process_chat(phone_number, text, display_name, context_name):
    """
    Verarbeitet eine Chat-Nachricht im Background-Thread.
    Reihenfolge: Kimi-Call → Antwort senden → ORBIT trigger → Fast-Track.

    ORBIT bekommt den Trigger erst NACH der Antwort, damit:
    - Kimi-Antwort hat Vorrang (zeitkritisch)
    - ORBIT kann die vollständige Interaktion als Kontext nutzen
    - Keine Memory-Race-Conditions
    """
    lock = _get_user_lock(phone_number)
    with lock:
        try:
            history = get_chat_history(phone_number)

            reply, _turn_meta = ollama_chat(phone_number, text, history, context_name)

            # Web Search
            reply, search_ctx = _handle_web_search(reply, user_id=phone_number, user_message=text)
            if search_ctx:
                logger.info("Web Search: starte zweiten Kimi-Call mit Suchergebnis")
                try:
                    search_reply, _ = ollama_chat(phone_number, text, history, context_name,
                                                  doc_context=search_ctx)
                    search_reply = re.sub(r"\[SEARCH:\s*.+?\]", "", search_reply or "", flags=re.IGNORECASE).strip()
                    search_reply = re.sub(r"\n{3,}", "\n\n", search_reply).strip()
                    if search_reply:
                        reply = search_reply
                        logger.info(f"Web Search: zweiter Call erfolgreich ({len(reply)} Zeichen)")
                    else:
                        logger.warning("Web Search: zweiter Call leer — behalte bereinigte Erstantwort")
                except Exception as e:
                    logger.error(f"Web Search: zweiter Kimi-Call fehlgeschlagen: {e}")

            # INTROSPECT
            reply, introspect_ctx = _handle_introspect(reply)
            if introspect_ctx:
                logger.info("INTROSPECT: starte zweiten Kimi-Call")
                try:
                    introspect_reply, _ = ollama_chat(phone_number, text, history, context_name,
                                                      doc_context=introspect_ctx)
                    introspect_reply = re.sub(r"\[INTROSPECT\]", "", introspect_reply or "", flags=re.IGNORECASE).strip()
                    introspect_reply = re.sub(r"\n{3,}", "\n\n", introspect_reply).strip()
                    if introspect_reply:
                        reply = introspect_reply
                        logger.info(f"INTROSPECT: zweiter Call erfolgreich ({len(reply)} Zeichen)")
                    else:
                        logger.warning("INTROSPECT: zweiter Call leer")
                except Exception as e:
                    logger.error(f"INTROSPECT: zweiter Kimi-Call fehlgeschlagen: {e}")

            # [gespeichert] Signal entfernen
            reply = re.sub(r"\s*\[gespeichert\]\s*", " ", reply).strip()

            # Todo-Parsing
            _tasks_enabled = True
            try:
                import json as _j2
                _tp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "tools_config.json")
                _tasks_enabled = {t["id"]: t for t in _j2.load(open(_tp))}.get("tasks", {}).get("enabled", True)
            except Exception:
                pass
            if _tasks_enabled:
                try:
                    from core.todos import extract_todo_action, execute_todo_action
                    reply, todo_action = extract_todo_action(reply)
                    if todo_action:
                        todo_result = execute_todo_action(phone_number, todo_action)
                        if todo_result:
                            reply = reply + ("\n\n" if reply else "") + todo_result
                except Exception as e:
                    logger.warning(f"Todo-Parsing fehlgeschlagen: {e}")

            # Calendar-Parsing
            _calendar_enabled = False
            try:
                import json as _jc
                _cp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "tools_config.json")
                _calendar_enabled = {t["id"]: t for t in _jc.load(open(_cp))}.get("calendar", {}).get("enabled", False)
            except Exception:
                pass
            if _calendar_enabled:
                try:
                    from core.calendar.calendar_router import extract_calendar_action, execute_calendar_action
                    reply, cal_action = extract_calendar_action(reply)
                    if cal_action:
                        cal_result = execute_calendar_action(cal_action)
                        if cal_result:
                            reply = reply + ("\n\n" if reply else "") + cal_result
                except Exception as e:
                    logger.warning(f"Calendar-Parsing fehlgeschlagen: {e}")

            # Moltbook-Parsing
            _moltbook_enabled = False
            try:
                import json as _jm
                _mp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "tools_config.json")
                _moltbook_enabled = {t["id"]: t for t in _jm.load(open(_mp))}.get("moltbook", {}).get("enabled", False)
            except Exception:
                pass
            if _moltbook_enabled:
                try:
                    from core.moltbook import extract_moltbook_action, execute_moltbook_action
                    reply, mb_action = extract_moltbook_action(reply)
                    if mb_action:
                        mb_result = execute_moltbook_action(mb_action)
                        if mb_result:
                            reply = reply + ("\n\n" if reply else "") + mb_result
                except Exception as e:
                    logger.warning(f"Moltbook-Parsing fehlgeschlagen: {e}")

            save_message(phone_number, "assistant", reply)
            send_message(phone_number, reply)

            logger.info(f"Antwort an {display_name}: {reply[:100]}")

            # MIRROR: Turn-Objekt bauen und speichern (non-blocking)
            try:
                turn = build_turn(
                    user_id=phone_number,
                    user_message=text,
                    response=reply,
                    chunks=_turn_meta.get("chunks", []),
                    global_rules=_turn_meta.get("global_rules", []),
                )
                _chat_pool.submit(save_turn, turn)
            except Exception as _me:
                logger.warning(f"MIRROR turn-logging fehlgeschlagen: {_me}")

            # ORBIT user_input Trigger — nach der Antwort, non-blocking
            # ORBIT bewertet ob aus dieser Interaktion ein Thread/Task entsteht
            _chat_pool.submit(
                _orbit_trigger, phone_number, "user_input",
                {
                    "message_preview": text[:80],
                    "topic_core": text[:80],
                    "response_preview": reply[:80],
                    "mode": "observe",
                }
            )

            # Fast-Track NACH Antwort und ORBIT-Trigger
            _chat_pool.submit(_safe_fast_track, phone_number, text)

        except Exception as e:
            logger.error(f"Chat-Verarbeitung fehlgeschlagen für {phone_number}: {e}")
            try:
                error_reply = "Da ist was schiefgelaufen. Versuch's nochmal!"
                save_message(phone_number, "assistant", error_reply)
                send_message(phone_number, error_reply)
            except Exception:
                logger.error(f"Auch Error-Reply fehlgeschlagen für {phone_number}")


def _estimate_time(page_count):
    """Grobe Zeitschätzung für Embedding basierend auf Seitenzahl."""
    seconds = max(5, page_count * 0.7)
    if seconds < 15:
        return "wenige Sekunden"
    elif seconds < 40:
        return "ca. 30 Sekunden"
    elif seconds < 90:
        return "ca. 1 Minute"
    elif seconds < 150:
        return "ca. 2 Minuten"
    else:
        return "einige Minuten"


def _process_voice(phone_number, audio_bytes, display_name, context_name):
    """Transkribiert eine Sprachnachricht und lässt Kimi darauf antworten."""
    try:
        transcript = transcribe_audio(audio_bytes)
        if not transcript:
            send_message(phone_number, "Ich konnte die Sprachnachricht leider nicht verstehen. Kannst du es nochmal versuchen oder schreiben?")
            return
        store_voice_chunk(transcript, phone_number)
        display_transcript = f"🎙️ {transcript}"
        save_message(phone_number, "user", display_transcript)
        send_message(phone_number, display_transcript)
        _process_chat(phone_number, transcript, display_name, context_name)
    except Exception as e:
        logger.error(f"Voice-Processing fehlgeschlagen für {phone_number}: {e}")
        send_message(phone_number, "Fehler bei der Sprachnachricht — bitte nochmal versuchen.")


def _process_document_index(phone_number, pdf_bytes, filename, display_name):
    """Nur PDF ohne Frage: Session aufbauen und Bereit-Meldung senden."""
    try:
        from core.document import extract_pages, chunk_pages, embed_chunks, set_doc_session
        pages = extract_pages(pdf_bytes)
        if not pages:
            send_message(phone_number, "Das PDF konnte leider nicht gelesen werden.")
            return
        est = _estimate_time(len(pages))
        send_message(phone_number, f"📊 {len(pages)} Seiten — ich indexiere in {est}. Gleich kannst du Fragen stellen.")
        chunks = chunk_pages(pages)
        set_doc_session(phone_number, filename, chunks, [], len(pages))
        embeddings = embed_chunks(chunks)
        set_doc_session(phone_number, filename, chunks, embeddings, len(pages))
        reply = f'✅ "{filename}" ist geladen — {len(pages)} Seiten, {len(chunks)} Abschnitte.\n\nIch bin jetzt im Dokument-Modus. Stell mir deine Fragen dazu. Schreib "stop" wenn du wieder normal chatten möchtest.'
        save_message(phone_number, "assistant", reply)
        send_message(phone_number, reply)
        logger.info(f"Doc-Index aufgebaut: {filename} für {display_name}")
    except Exception as e:
        logger.error(f"Doc-Index fehlgeschlagen: {e}")
        send_message(phone_number, "Beim Lesen des Dokuments ist etwas schiefgelaufen.")


def _answer_doc_query(phone_number, query, context_name):
    """Beantwortet eine Frage gegen die aktive Doc-Session."""
    fundstellen, relevance, filename = search_doc_session(phone_number, query)
    history = []

    if relevance == "no_session":
        return "Ich habe gerade kein Dokument geladen. Schick mir bitte zuerst ein PDF."

    if relevance == "none" or not fundstellen:
        no_result_ctx = (
            f'Der Nutzer fragt zum Dokument "{filename}". '
            f'Das Retrieval hat keine ausreichend relevanten Stellen gefunden. '
            f'Antworte ehrlich dass du dazu nichts Belastbares im Dokument gefunden hast.'
        )
        reply, _ = ollama_chat(phone_number, query, history, context_name, doc_context=no_result_ctx)
        return reply

    relevance_hint = (
        "Die Treffer sind sehr relevant." if relevance == "strong"
        else "Die Treffer sind nur indirekt relevant — nutze sie als Hintergrund, aber beantworte die Frage direkt."
    )
    doc_ctx = (
        f'Aktives Dokument: "{filename}"\n'
        f"Unten stehen Fundstellen aus dem Dokument die zur Frage passen.\n\n"
        f"WICHTIG: Beantworte die Frage des Nutzers direkt und vollständig. "
        f"Wenn er eine Meinung, Einschätzung oder Diskussion will — gib sie. "
        f"Nutze die Fundstellen als Grundlage und Belege, nicht als Antwort-Ersatz. "
        f"Wenn die Frage persönlich oder nicht dokumentbezogen ist, antworte normal aus deinem Gedächtnis.\n"
        f"{relevance_hint}\n"
        f"Keine Sternchen, kein Markdown, kein Bold. Fließtext.\n\n"
        f"Fundstellen:\n\n{fundstellen}"
    )
    history = get_chat_history(phone_number)[-6:] if get_chat_history(phone_number) else []
    reply, _ = ollama_chat(phone_number, query, history, context_name, doc_context=doc_ctx)
    return reply


def _handle_doc_followup(phone_number, query, context_name):
    """Folgefrage bei aktiver Doc-Session."""
    lock = _get_user_lock(phone_number)
    with lock:
        try:
            reply = _answer_doc_query(phone_number, query, context_name)
            save_message(phone_number, "assistant", reply)
            send_message(phone_number, reply)
        except Exception as e:
            logger.error(f"Doc-Followup fehlgeschlagen: {e}")
            send_message(phone_number, "Beim Durchsuchen des Dokuments ist etwas schiefgelaufen.")


def _process_document_search(phone_number, pdf_bytes, filename, caption, display_name, context_name):
    """PDF + Frage: Session aufbauen, dann direkt suchen und antworten."""
    lock = _get_user_lock(phone_number)
    with lock:
        try:
            from core.document import extract_pages, chunk_pages, embed_chunks, set_doc_session
            pages = extract_pages(pdf_bytes)
            if not pages:
                reply = "Das PDF konnte leider nicht gelesen werden."
                save_message(phone_number, "assistant", reply)
                send_message(phone_number, reply)
                return
            est = _estimate_time(len(pages))
            send_message(phone_number, f"📊 {len(pages)} Seiten — ich bin in {est} bereit.")
            chunks = chunk_pages(pages)
            set_doc_session(phone_number, filename, chunks, [], len(pages))
            embeddings = embed_chunks(chunks)
            set_doc_session(phone_number, filename, chunks, embeddings, len(pages))
            user_msg = f"[PDF: {filename}] {caption}"
            save_message(phone_number, "user", user_msg)
            reply = _answer_doc_query(phone_number, caption, context_name)
            save_message(phone_number, "assistant", reply)
            send_message(phone_number, reply)
            logger.info(f"Dokument-Suche: {filename} / '{caption[:50]}' für {display_name}")
        except Exception as e:
            logger.error(f"Dokument-Suche fehlgeschlagen: {e}")
            send_message(phone_number, "Beim Lesen des Dokuments ist etwas schiefgelaufen.")


def _safe_fast_track(user_id, text):
    """Fast-Track in separatem Thread mit Error-Handling."""
    try:
        chunk_id = process_fast_track(user_id, text)
        if chunk_id:
            logger.info(f"Fast-Track Chunk gespeichert: {chunk_id[:8]}...")
    except Exception as e:
        logger.warning(f"Fast-Track Fehler: {e}")


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "alive", "bot": BOT_NAME}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
