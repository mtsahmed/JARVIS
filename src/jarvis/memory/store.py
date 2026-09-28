"""Remembered facts in SQLite (data/memory.db), with embedding-based recall.

Owner decisions (Phase 4): facts are saved only when the user asks; no conversation transcripts are
stored; recall uses OpenAI embeddings (the fact text is sent to OpenAI to compute them).
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from jarvis.core.db import ClosingDB
from jarvis.llm.base import Embedder, LLMError

logger = logging.getLogger(__name__)

INCLUDE_ALL_UP_TO = 20  # with this many memories or fewer, all are given to the model (no extra call)
TOP_K = 8
MIN_SIMILARITY = 0.25  # measured: related fact/question ~0.45, unrelated ~0.1-0.2
SIMILAR_NOTE = 0.8  # a new fact this similar to an old one is saved, but the old one is pointed out
MAX_TEXT_CHARS = 300


@dataclass(frozen=True)
class Memory:
    id: int
    text: str
    saved: datetime  # local time


class MemoryStore(ClosingDB):
    def __init__(self, db_path: Path, embedder: Embedder | None = None) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._embedder = embedder
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock, self._db:
            self._db.execute("CREATE TABLE IF NOT EXISTS memories (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                             "text TEXT NOT NULL, saved_utc TEXT NOT NULL, embedding BLOB, embedding_model TEXT)")
        # The user's words in the current turn; the `remember` tool only runs if they ask to remember.
        self.current_request = ""

    # --- saving and deleting ------------------------------------------------------------------

    def add(self, text: str) -> tuple[Memory, Memory | None]:
        """Save a fact. Returns (saved memory, the identical memory it replaced, if any).

        Only an identical fact (ignoring case and spacing) is replaced. Similar facts are kept side by side
        (see similar_to): silently merging them could lose a different fact.
        """
        text = " ".join(text.split())[:MAX_TEXT_CHARS]
        vector = self._embed_one(text)
        replaced = next((m for m in self.all() if m.text.lower() == text.lower()), None)
        now = datetime.now(timezone.utc)
        blob = vector.astype(np.float32).tobytes() if vector is not None else None
        model = self._embedder.model if (vector is not None and self._embedder) else None
        with self._lock, self._db:
            if replaced is not None:
                self._db.execute("UPDATE memories SET text = ?, saved_utc = ?, embedding = ?, embedding_model = ? "
                                 "WHERE id = ?", (text, _utc(now), blob, model, replaced.id))
                memory_id = replaced.id
            else:
                cursor = self._db.execute("INSERT INTO memories (text, saved_utc, embedding, embedding_model) "
                                          "VALUES (?, ?, ?, ?)", (text, _utc(now), blob, model))
                memory_id = int(cursor.lastrowid or 0)
        return Memory(memory_id, text, now.astimezone()), replaced

    def delete(self, memory_id: int) -> bool:
        """Really delete (privacy: a forgotten fact must not stay on disk)."""
        with self._lock, self._db:
            return self._db.execute("DELETE FROM memories WHERE id = ?", (memory_id,)).rowcount == 1

    # --- reading ------------------------------------------------------------------------------

    def all(self) -> list[Memory]:
        with self._lock:
            rows = self._db.execute("SELECT id, text, saved_utc FROM memories ORDER BY id").fetchall()
        return [_memory(r) for r in rows]

    def get(self, memory_id: int) -> Memory | None:
        return next((m for m in self.all() if m.id == memory_id), None)

    def relevant(self, query: str) -> list[Memory]:
        """Memories to give the model for this request: all of them if few, else the most similar."""
        memories = self.all()
        if len(memories) <= INCLUDE_ALL_UP_TO:
            return memories
        query_vector = self._embed_one(query)
        if query_vector is None:  # recall unavailable (offline / no embedder): use the most recent
            return memories[-TOP_K:]
        scored = [(float(v @ query_vector), m) for m, v in self._vectors()]
        best = sorted((s for s in scored if s[0] >= MIN_SIMILARITY), key=lambda s: -s[0])[:TOP_K]
        return sorted((m for _, m in best), key=lambda m: m.id)

    # --- embeddings ---------------------------------------------------------------------------

    def _embed_one(self, text: str) -> np.ndarray | None:
        if self._embedder is None:
            return None
        try:
            vector = np.asarray(self._embedder.embed([text])[0], dtype=np.float32)
        except LLMError as e:
            logger.warning("Memory embedding unavailable: %s", e)
            return None
        norm = float(np.linalg.norm(vector))
        return vector / norm if norm else None

    def _vectors(self) -> list[tuple[Memory, np.ndarray]]:
        model = self._embedder.model if self._embedder else None
        with self._lock:
            rows = self._db.execute("SELECT id, text, saved_utc, embedding, embedding_model FROM memories "
                                    "WHERE embedding IS NOT NULL").fetchall()
        # Vectors from another embedding model are not comparable; those memories wait for re-embedding.
        return [(_memory(r), np.frombuffer(r[3], dtype=np.float32)) for r in rows if r[4] == model]

    def similar_to(self, memory: Memory) -> list[Memory]:
        """Other memories very similar to this one (e.g. an older version of the same fact)."""
        vectors = self._vectors()
        own = next((v for m, v in vectors if m.id == memory.id), None)
        if own is None:
            return []
        return [m for m, v in vectors if m.id != memory.id and float(v @ own) >= SIMILAR_NOTE]


def _utc(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _memory(row: tuple) -> Memory:  # type: ignore[type-arg]
    saved = datetime.strptime(row[2], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).astimezone()
    return Memory(int(row[0]), str(row[1]), saved)
