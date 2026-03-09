"""
SchnuBot.ai - Dokument-Analyse (P2.7)

Pipeline:
1. PDF herunterladen (WAHA URL, sofort im Webhook)
2. Seitenbasierte Extraktion via pymupdf
3. Semantisches Chunking pro Seite (800-1500 Zeichen, 150 Overlap)
4. TOC/Header/Footer-Filter
5. Chunks lokal embeddeden (nomic-embed-text)
6. Query-Retrieval: Embedding + Keyword-Boost
7. Top-Chunks an Kimi mit Seitenangaben

Doc-Sessions leben im RAM, getrennt von User-Memory (ChromaDB).
TTL: 2 Stunden Inaktivität.
"""

import os
import re
import time
import logging
import threading
import requests

logger = logging.getLogger(__name__)

MEDIA_SENTINEL_RE = re.compile(r"^\[MEDIA:(\w+):(.+?):([^:]+)\]$")
WAHA_API_URL = "http://localhost:3000"

# ---------------------------------------------------------------------------
# Doc-Sessions
# ---------------------------------------------------------------------------
_doc_sessions = {}
_doc_sessions_lock = threading.Lock()
DOC_SESSION_TTL = 7200


def _expire_old_sessions():
    now = time.time()
    with _doc_sessions_lock:
        expired = [uid for uid, s in _doc_sessions.items() if s["expires_at"] < now]
        for uid in expired:
            del _doc_sessions[uid]
            logger.info(f"Doc-Session abgelaufen: {uid}")


def get_doc_session(user_id):
    _expire_old_sessions()
    with _doc_sessions_lock:
        session = _doc_sessions.get(user_id)
        if session and session["expires_at"] > time.time():
            session["expires_at"] = time.time() + DOC_SESSION_TTL
            return session
        return None


def set_doc_session(user_id, filename, chunks, embeddings, page_count, status="ready"):
    with _doc_sessions_lock:
        _doc_sessions[user_id] = {
            "filename": filename,
            "chunks": chunks,
            "embeddings": embeddings,
            "page_count": page_count,
            "status": status,  # indexing | ready | failed
            "embedded_count": len([e for e in embeddings if e]),
            "expires_at": time.time() + DOC_SESSION_TTL,
        }
    logger.info(f"Doc-Session gesetzt: {filename} ({len(chunks)} Chunks, {page_count} Seiten, status={status}) fuer {user_id}")


def clear_doc_session(user_id):
    with _doc_sessions_lock:
        _doc_sessions.pop(user_id, None)


# ---------------------------------------------------------------------------
# Sentinel
# ---------------------------------------------------------------------------

def is_media_message(text):
    if not text:
        return False
    first_line = text.strip().split("\n", 1)[0].strip()
    return bool(MEDIA_SENTINEL_RE.match(first_line))


def parse_media_sentinel(text):
    m = MEDIA_SENTINEL_RE.match(text.strip())
    if not m:
        return None
    return m.group(1), m.group(2), m.group(3)


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

def download_media(media_url, api_key=None):
    import logging as _l
    _l.getLogger(__name__).info(f"download_media: key={repr(api_key[:4]) if api_key else None}, url={media_url[:60]}")
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


# ---------------------------------------------------------------------------
# Extraktion
# ---------------------------------------------------------------------------

def extract_pages(pdf_bytes):
    try:
        import fitz
        pages = []
        with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
            for page in doc:
                text = page.get_text().strip()
                if text:
                    pages.append({"page": page.number + 1, "text": text})
        logger.info(f"PDF extrahiert: {len(pages)} Seiten mit Text")
        return pages
    except ImportError:
        logger.error("pymupdf nicht installiert")
        return []
    except Exception as e:
        logger.error(f"PDF-Extraktion fehlgeschlagen: {e}")
        return []


def _is_toc_or_noise(text):
    lines = text.strip().split("\n")
    if len(lines) < 3:
        noise_pattern = re.compile(r"^[\d\s\.\-]+$")
        if all(noise_pattern.match(l.strip()) for l in lines if l.strip()):
            return True
    toc_lines = sum(1 for l in lines if re.search(r"\s+\d+\s*$", l))
    if len(lines) > 3 and toc_lines / len(lines) > 0.5:
        return True
    return False


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------

def chunk_pages(pages, chunk_size=1200, overlap=150):
    chunks = []
    chunk_counter = 0

    for page_data in pages:
        page_num = page_data["page"]
        page_text = page_data["text"]
        paragraphs = re.split(r"\n{2,}", page_text)
        paragraphs = [p.strip() for p in paragraphs if p.strip()]

        current = ""
        for para in paragraphs:
            if len(current) + len(para) <= chunk_size:
                current = (current + "\n\n" + para).strip()
            else:
                if current and not _is_toc_or_noise(current):
                    chunks.append({
                        "chunk_id": f"doc_{chunk_counter:04d}",
                        "page": page_num,
                        "text": current,
                    })
                    chunk_counter += 1
                overlap_text = current[-overlap:] if len(current) > overlap else current
                current = (overlap_text + "\n\n" + para).strip()

        if current and not _is_toc_or_noise(current):
            chunks.append({
                "chunk_id": f"doc_{chunk_counter:04d}",
                "page": page_num,
                "text": current,
            })
            chunk_counter += 1

    logger.info(f"Chunking: {len(chunks)} Chunks aus {len(pages)} Seiten")
    return chunks


# ---------------------------------------------------------------------------
# Embedding
# ---------------------------------------------------------------------------

def embed_chunks(chunks):
    """
    Bettet eine Liste von Chunks per Batch ein.
    Filtert leere/zu kurze Chunks vor dem Embedding heraus (Platzhalter [] bleibt).
    Reihenfolge ist garantiert stabil.
    """
    from memory.memory_store import embed_texts

    # Texte extrahieren — leere oder sehr kurze Chunks werden übersprungen
    MIN_CHUNK_LEN = 10
    texts = []
    valid_indices = []
    for i, chunk in enumerate(chunks):
        text = chunk.get("text", "").strip()
        if len(text) >= MIN_CHUNK_LEN:
            texts.append(text)
            valid_indices.append(i)
        else:
            logger.debug(f"Chunk {i} übersprungen (zu kurz: {len(text)} Zeichen)")

    skipped = len(chunks) - len(valid_indices)
    if skipped:
        logger.info(f"Embedding: {skipped} Chunks übersprungen (zu kurz/leer)")

    # Batch-Embedding der gefilterten Texte
    batch_embeddings = embed_texts(texts)  # verwendet EMBEDDING_BATCH_SIZE aus Config

    # Ergebnis-Liste mit stabiler Reihenfolge aufbauen
    # Ungültige Chunks bekommen [] als Platzhalter
    embeddings = [[]] * len(chunks)
    for list_pos, chunk_idx in enumerate(valid_indices):
        embeddings[chunk_idx] = batch_embeddings[list_pos] if list_pos < len(batch_embeddings) else []

    success = sum(1 for e in embeddings if e)
    logger.info(f"embed_chunks abgeschlossen: {success}/{len(chunks)} Chunks eingebettet")
    return embeddings


def embed_query(query_text):
    """Query-Embedding — nutzt dieselbe Normalisierung wie embed_texts, aber mit search_query Prefix."""
    from memory.memory_store import embed_query as _embed_query
    try:
        return _embed_query(query_text)
    except Exception as e:
        logger.error(f"Query-Embedding fehlgeschlagen: {e}")
        return []


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------

def _cosine_similarity(a, b):
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def retrieve_chunks(query, chunks, embeddings, top_k=6):
    query_emb = embed_query(query)
    query_lower = query.lower()
    keywords = [w for w in re.findall(r"\w+", query_lower) if len(w) > 3]

    scored = []
    for i, chunk in enumerate(chunks):
        emb = embeddings[i] if i < len(embeddings) else []
        sim = _cosine_similarity(query_emb, emb)
        chunk_lower = chunk["text"].lower()
        keyword_hits = sum(1 for kw in keywords if kw in chunk_lower)
        keyword_boost = min(keyword_hits * 0.05, 0.2)
        score = sim + keyword_boost
        scored.append((score, chunk))

    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:top_k]
    if not top:
        return [], "none"

    best_score = top[0][0]
    if best_score >= 0.75:
        relevance = "strong"
    elif best_score >= 0.55:
        relevance = "weak"
    else:
        relevance = "none"

    return [(score, chunk) for score, chunk in top if score > 0.4], relevance


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def build_doc_session(user_id, pdf_bytes, filename):
    """Baut Doc-Session auf. Gibt page_count zurueck."""
    pages = extract_pages(pdf_bytes)
    if not pages:
        return 0
    chunks = chunk_pages(pages)
    if not chunks:
        return len(pages)
    embeddings = embed_chunks(chunks)
    set_doc_session(user_id, filename, chunks, embeddings, len(pages))
    return len(pages)


def search_doc_session(user_id, query):
    """
    Sucht in aktiver Doc-Session.
    Returns: (fundstellen_text, relevance, filename)
    relevance: 'strong' | 'weak' | 'none' | 'no_session'
    """
    session = get_doc_session(user_id)
    if not session:
        return None, "no_session", None

    results, relevance = retrieve_chunks(query, session["chunks"], session["embeddings"])
    filename = session["filename"]

    if relevance == "none" or not results:
        return None, "none", filename

    fundstellen = []
    for score, chunk in results:
        fundstellen.append(f"[Seite {chunk['page']}]\n{chunk['text']}")

    context = "\n\n---\n\n".join(fundstellen)
    return context, relevance, filename


# Legacy-Kompatibilität
def extract_pdf_text(pdf_bytes, max_chars=60000, skip_pages=0):
    """Fallback: einfache lineare Extraktion."""
    try:
        import fitz
        with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
            pages = []
            total = 0
            for page in doc:
                if page.number < skip_pages:
                    continue
                page_text = page.get_text().strip()
                if not page_text:
                    continue
                pages.append(f"[Seite {page.number + 1}]\n{page_text}")
                total += len(page_text)
                if total >= max_chars:
                    break
            text = "\n\n".join(pages)
            if len(text) > max_chars:
                text = text[:max_chars] + "\n\n[... Text gekuerzt]"
            return text
    except Exception as e:
        logger.error(f"PDF-Extraktion fehlgeschlagen: {e}")
        return None
