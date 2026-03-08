"""
fix_style_chunks_rollback.py
Setzt die 4 falsch korrigierten Chunks zurück auf hard_fact.
"""
import sys
sys.path.insert(0, "/opt/whatsapp-bot")

import chromadb
from memory.memory_config import CHROMA_PERSIST_DIR

# Diese 4 IDs wurden fälschlicherweise zu preference geändert
ROLLBACK_IDS = [
    "73bc9c8e-4ff",  # "nun war die idee..."
    "19bcc66d-61e",  # "## Wie ich arbeite..."
    "693b3aa6-503",  # Tommy Geburtsdaten
    "5d74cb7a-540",  # Katzen
]

client = chromadb.PersistentClient(path=CHROMA_PERSIST_DIR)
collection = client.get_collection("memory_active")

result = collection.get(include=["documents", "metadatas"])
ids = result["ids"]
metas = result["metadatas"]
docs = result["documents"]

fixed = 0
for chunk_id, meta, doc in zip(ids, metas, docs):
    short = chunk_id[:11]
    if any(short == rb[:11] for rb in ROLLBACK_IDS):
        new_meta = dict(meta)
        new_meta["chunk_type"] = "hard_fact"
        # global-preference und response-style entfernen
        tags = set(t.strip() for t in new_meta.get("tags", "").split(",") if t.strip())
        tags.discard("global-preference")
        tags.discard("response-style")
        new_meta["tags"] = ", ".join(sorted(tags))
        collection.update(ids=[chunk_id], metadatas=[new_meta])
        print(f"Zurückgesetzt: {chunk_id[:12]} → hard_fact | Tags: {new_meta['tags']}")
        fixed += 1

print(f"\n{fixed} Chunk(s) zurückgesetzt.")
