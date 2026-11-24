"""Embeddings behind a small protocol, so tests use a fake and the model is swappable.

Default: local `BAAI/bge-small-en-v1.5` via sentence-transformers (384 dims, free, CPU
is fine). Install with `uv sync --extra dev --extra ml`. Vectors are L2-normalised, so
cosine distance is what pgvector's `vector_cosine_ops` index measures.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Sequence
from functools import lru_cache
from typing import Any, Protocol

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from analyst.db.models import EMBEDDING_DIM, Chunk

DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"
# BGE v1.5 recommends this instruction on short queries (not on passages).
BGE_QUERY_PREFIX = "Represent this sentence for searching relevant passages: "


class Embedder(Protocol):
    name: str
    dim: int

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


class SentenceTransformerEmbedder:
    """Local sentence-transformers model. Loaded lazily (the import pulls in torch)."""

    def __init__(self, model_name: str = DEFAULT_MODEL, batch_size: int = 32) -> None:
        self.name = model_name
        self.batch_size = batch_size
        self._model: Any = None
        self.dim = EMBEDDING_DIM

    @property
    def model(self) -> Any:
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as exc:  # pragma: no cover - depends on the install
                raise SystemExit(
                    "sentence-transformers is not installed: run `uv sync --extra dev --extra ml`"
                ) from exc
            self._model = SentenceTransformer(self.name, device="cpu")
            self.dim = int(self._model.get_sentence_embedding_dimension())
        return self._model

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        vecs = self.model.encode(list(texts), batch_size=self.batch_size, normalize_embeddings=True)
        return [v.tolist() for v in vecs]

    def embed_query(self, text: str) -> list[float]:
        prefix = BGE_QUERY_PREFIX if "bge" in self.name.lower() else ""
        return self.model.encode([prefix + text], normalize_embeddings=True)[0].tolist()


_WORD = re.compile(r"[a-z0-9]+")


class HashEmbedder:
    """Deterministic bag-of-words embedder for tests: texts sharing words are close."""

    name = "test-hash"

    def __init__(self, dim: int = EMBEDDING_DIM) -> None:
        self.dim = dim

    def _vec(self, text: str) -> list[float]:
        v = [0.0] * self.dim
        for w in _WORD.findall(text.lower()):
            h = int.from_bytes(hashlib.blake2b(w.encode(), digest_size=8).digest(), "big")
            v[h % self.dim] += 1.0
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / n for x in v]

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vec(text)


@lru_cache
def default_embedder() -> Embedder:
    return SentenceTransformerEmbedder()


def embed_pending(
    session: Session,
    embedder: Embedder,
    batch_size: int = 64,
    limit: int | None = None,
    progress: Any = None,
) -> int:
    """Embed chunks that have no embedding (or one from another model). Returns count.

    Commits after each batch, so an interrupted run keeps its progress.
    """
    pending = or_(Chunk.embedding.is_(None), Chunk.embed_model.is_distinct_from(embedder.name))
    ids = list(session.scalars(select(Chunk.id).where(pending).order_by(Chunk.id)))
    if limit is not None:
        ids = ids[:limit]
    done = 0
    for i in range(0, len(ids), batch_size):
        batch = ids[i : i + batch_size]
        # header + text: chunk text says "we", the header names the company and section
        rows = [
            (c.id, c.indexed_text)
            for c in session.scalars(select(Chunk).where(Chunk.id.in_(batch)).order_by(Chunk.id))
        ]
        vecs = embedder.embed_documents([t for _, t in rows])
        session.execute(
            update(Chunk),
            [
                {"id": cid, "embedding": v, "embed_model": embedder.name}
                for (cid, _), v in zip(rows, vecs, strict=True)
            ],
        )
        session.commit()
        done += len(rows)
        if progress:
            progress(done, len(ids))
    return done
