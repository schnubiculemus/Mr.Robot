"""
core/voice.py — Sprachnachrichten-Transkription via faster-whisper

Transkribiert eingehende WhatsApp-Sprachnachrichten (ogg/mp3/m4a).
Modell: base (140MB, ~10s auf CPU, gut für Deutsch + Dialekt).

Singleton-Pattern wie beim Embedding-Modell — wird einmal geladen,
dann wiederverwendet. Erster Aufruf dauert ~30s (Modell-Download).

Ablauf in app.py:
  1. WAHA liefert [MEDIA:audio:url:filename] Sentinel
  2. Audio-Bytes via download_media() holen (sofort, WAHA loescht schnell)
  3. transcribe_audio(audio_bytes) → Text
  4. Transkript als user-Nachricht speichern + als knowledge-Chunk
  5. Kimi antwortet auf den transkribierten Text
"""

import io
import logging
import os
import tempfile
import threading

logger = logging.getLogger(__name__)

WHISPER_MODEL_SIZE = "base"   # tiny/base/small — base ist Empfehlung fuer CPU
WHISPER_LANGUAGE  = "de"      # Deutsch; None = auto-detect (langsamer)
WHISPER_DEVICE    = "cpu"
WHISPER_COMPUTE   = "int8"    # int8 spart RAM + ist schneller auf CPU

_model = None
_model_lock = threading.Lock()


def _get_model():
    """Laedt das Whisper-Modell (Singleton, thread-safe)."""
    global _model
    if _model is None:
        with _model_lock:
            if _model is None:
                try:
                    import os as _os
                    _os.environ["HF_HUB_OFFLINE"] = "0"
                    from faster_whisper import WhisperModel
                    logger.info(f"Lade Whisper-Modell '{WHISPER_MODEL_SIZE}' ...")
                    _model = WhisperModel(
                        WHISPER_MODEL_SIZE,
                        device=WHISPER_DEVICE,
                        compute_type=WHISPER_COMPUTE,
                    )
                    logger.info("Whisper-Modell geladen.")
                except ImportError:
                    logger.error("faster-whisper nicht installiert: pip install faster-whisper")
                    return None
                except Exception as e:
                    logger.error(f"Whisper-Modell laden fehlgeschlagen: {e}")
                    return None
    return _model


def transcribe_audio(audio_bytes: bytes) -> str | None:
    """
    Transkribiert Audio-Bytes zu Text.

    Args:
        audio_bytes: Rohe Audio-Daten (ogg/mp3/m4a — faster-whisper
                     akzeptiert alles was ffmpeg lesen kann).

    Returns:
        Transkribierter Text oder None bei Fehler.
    """
    model = _get_model()
    if not model:
        return None

    if not audio_bytes:
        logger.warning("Voice: Leere Audio-Bytes, Transkription uebersprungen")
        return None

    # Temp-Datei — faster-whisper braucht einen Dateipfad, keine Bytes direkt
    suffix = ".ogg"  # WAHA liefert meist ogg/opus
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(audio_bytes)
            tmp_path = tmp.name

        logger.info(f"Voice: Transkribiere {len(audio_bytes) // 1024}KB Audio ...")

        import time
        t_start = time.time()

        segments, info = model.transcribe(
            tmp_path,
            language=WHISPER_LANGUAGE,
            beam_size=5,
            vad_filter=True,          # Stille herausfiltern
            vad_parameters=dict(min_silence_duration_ms=500),
        )

        # Segmente zusammenfuehren
        text_parts = [seg.text.strip() for seg in segments if seg.text.strip()]
        transcript = " ".join(text_parts).strip()

        elapsed = time.time() - t_start
        logger.info(f"Voice: Transkription fertig in {elapsed:.1f}s — '{transcript[:80]}'")

        return transcript if transcript else None

    except Exception as e:
        logger.error(f"Voice: Transkription fehlgeschlagen: {e}")
        return None
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass


def store_voice_chunk(transcript: str, user_id: str) -> str | None:
    """
    Speichert ein Sprachnachrichten-Transkript als knowledge-Chunk in ChromaDB.

    Returns: chunk_id oder None
    """
    try:
        from memory.chunk_schema import create_chunk
        from memory.memory_store import store_chunk

        chunk = create_chunk(
            text=f"Sprachnachricht: {transcript}",
            chunk_type="knowledge",
            source="tommy",
            confidence=0.80,
            epistemic_status="stated",
            tags=["sprachnachricht", "voice"],
        )
        store_chunk(chunk)
        logger.info(f"Voice-Chunk gespeichert: {chunk['id'][:8]}")
        return chunk["id"]
    except Exception as e:
        logger.warning(f"Voice-Chunk speichern fehlgeschlagen: {e}")
        return None
