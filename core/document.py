"""
SchnuBot.ai - Dokument-Analyse (Tool 1)
Lädt Medien von WAHA herunter und extrahiert Text aus PDFs.
"""

import os
import re
import logging
import tempfile
import requests

logger = logging.getLogger(__name__)

MEDIA_SENTINEL_RE = re.compile(r"^\[MEDIA:(\w+):(.+?):([^:]+)\]$")

WAHA_API_URL = "http://localhost:3000"
WAHA_SESSION = "default"


def is_media_message(text):
    """Prüft ob der Text ein Media-Sentinel ist (erste Zeile)."""
    if not text:
        return False
    first_line = text.strip().split("\n", 1)[0].strip()
    return bool(MEDIA_SENTINEL_RE.match(first_line))


def parse_media_sentinel(text):
    """Parst [MEDIA:typ:id:filename] → (typ, id, filename) oder None."""
    m = MEDIA_SENTINEL_RE.match(text.strip())
    if not m:
        return None
    return m.group(1), m.group(2), m.group(3)


def download_media(media_url, api_key=None):
    """Lädt ein Medium von WAHA herunter. Gibt bytes zurück oder None."""
    headers = {}
    if api_key:
        headers["X-Api-Key"] = api_key

    try:
        resp = requests.get(media_url, headers=headers, timeout=60)
        resp.raise_for_status()
        return resp.content
    except Exception as e:
        logger.error(f"Media-Download fehlgeschlagen ({media_url}): {e}")
        return None


def extract_pdf_text(pdf_bytes, max_chars=30000):
    """Extrahiert Text aus PDF-Bytes via pymupdf. Gibt String zurück."""
    try:
        import fitz  # pymupdf
        with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
            pages = []
            total = 0
            for page in doc:
                page_text = page.get_text().strip()
                if not page_text:
                    continue
                pages.append(f"[Seite {page.number + 1}]\n{page_text}")
                total += len(page_text)
                if total >= max_chars:
                    break
            text = "\n\n".join(pages)
            if len(text) > max_chars:
                text = text[:max_chars] + "\n\n[... Text gekürzt]"
            return text
    except ImportError:
        logger.error("pymupdf nicht installiert — pip install pymupdf")
        return None
    except Exception as e:
        logger.error(f"PDF-Extraktion fehlgeschlagen: {e}")
        return None


def process_media_message(text, api_key=None):
    """
    Vollständige Pipeline: Sentinel → Download → Text-Extraktion.
    text kann [MEDIA:...] allein oder kombiniert mit Caption sein.

    Returns:
        (document_context: str, filename: str, caption: str) oder (None, None, None)
    """
    # Sentinel aus erster Zeile, Rest ist Caption
    lines = text.strip().split("\n", 1)
    sentinel_line = lines[0].strip()
    caption = lines[1].strip() if len(lines) > 1 else ""

    parsed = parse_media_sentinel(sentinel_line)
    if not parsed:
        return None, None, None

    media_type, media_url, filename = parsed
    logger.info(f"Medien-Verarbeitung: {media_type} / {filename} / {media_id}")

    if media_type != "pdf":
        return None, None

    pdf_bytes = download_media(media_url, api_key=api_key)
    if not pdf_bytes:
        return None, filename, caption

    extracted = extract_pdf_text(pdf_bytes)
    if not extracted:
        return None, filename, caption

    context = f"[DOKUMENT: {filename}]\n\n{extracted}"
    logger.info(f"PDF extrahiert: {filename} ({len(extracted)} Zeichen)")
    return context, filename, caption
