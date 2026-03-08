"""
fix_style_chunks.py

Korrigiert falsch als hard_fact gespeicherte Stil-/Formatierungsregeln.
Setzt chunk_type auf 'preference', fügt Tags global-preference + response-style hinzu.

Ausführen: python3 fix_style_chunks.py
"""

import sys
import re
sys.path.insert(0, "/opt/whatsapp-bot")

import chromadb
from memory.memory_config import CHROMA_PERSIST_DIR

# Style-Keywords (analog fast_track.py)
_STYLE_KEYWORDS = [
    re.compile(r"\b(fett|bold|hervorheb|markdown|formatier)", re.I),
    re.compile(r"\b(fließtext|fliesstext|prosa|ohne liste)", re.I),
    re.compile(r"\b(emoji|smiley|emoticon)", re.I),
    re.compile(r"\b(duzen|siezen|du\b|sie\b|anrede)", re.I),
    re.compile(r"\b(kurz|knapp|ausführlich|lang|wortreich|kompakt)", re.I),
    re.compile(r"\b(ton|tonfall|stil|schreibstil|antwortst)", re.I),
    re.compile(r"\b(überschrift|header|bullet|aufzählung|nummerier)", re.I),
    re.compile(r"\b(smalltalk|floskeln?|höflichkeit)", re.I),
    re.compile(r"\b(sprache|deutsch|englisch|denglisch)", re.I),
]

def is_style_related(text):
    for pattern in _STYLE_KEYWORDS:
        if pattern.search(text):
            return True
    return False

client = chromadb.PersistentClient(path=CHROMA_PERSIST_DIR)
collection = client.get_collection("memory_active")

result = collection.get(include=["documents", "metadatas"])
ids = result["ids"]
docs = result["documents"]
metas = result["metadatas"]

fixed = []
for chunk_id, text, meta in zip(ids, docs, metas):
    if meta.get("chunk_type") == "hard_fact" and is_style_related(text):
        print(f"\n[KANDIDAT] {chunk_id[:12]}...")
        print(f"  Text: {text[:100]}")
        print(f"  Tags: {meta.get('tags', '')}")

        # Tags aktualisieren
        existing_tags = meta.get("tags", "")
        tag_set = set(t.strip() for t in existing_tags.split(",") if t.strip())
        tag_set.update(["fast-track", "global-preference", "response-style"])
        new_tags = ", ".join(sorted(tag_set))

        new_meta = dict(meta)
        new_meta["chunk_type"] = "preference"
        new_meta["tags"] = new_tags

        collection.update(
            ids=[chunk_id],
            metadatas=[new_meta]
        )
        fixed.append(chunk_id)
        print(f"  → Korrigiert: hard_fact → preference, Tags: {new_tags}")

print(f"\n{len(fixed)} Chunk(s) korrigiert.")
