"""
The corpus itself: the BM25 index, the ChromaDB handle, and the loaded docs.

THIS MODULE OWNS `_collection`, `_bm25` and `doc_index`. Like paths.py, they are
rebound from outside — the provenance and lifecycle suites swap in fakes — so
consumers must read them as `corpus.doc_index`, never `from .corpus import
doc_index`. A `from` import freezes the value at import time and the fake never
lands.
"""
from __future__ import annotations

import math
import re
import sys
from collections import Counter

from . import paths


# ---------------------------------------------------------------------------
# Lightweight BM25 — paired with ChromaDB's vector search for hybrid retrieval
# ---------------------------------------------------------------------------
# We keep this simple and in-process. At a few hundred tenders it's instant.

class BM25:
    def __init__(self, corpus: list[str], k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self.corpus_tokens = [self._tokenize(doc) for doc in corpus]
        self.corpus_size = len(corpus)
        self.avgdl = (
            sum(len(t) for t in self.corpus_tokens) / self.corpus_size
            if self.corpus_size else 0
        )
        self._build_index()

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        return re.findall(r"\b\w+\b", text.lower())

    def _build_index(self):
        self.doc_freqs = [Counter(tokens) for tokens in self.corpus_tokens]
        self.doc_lens = [len(tokens) for tokens in self.corpus_tokens]
        df: Counter = Counter()
        for tokens in self.corpus_tokens:
            df.update(set(tokens))
        self.idf = {
            term: math.log((self.corpus_size - freq + 0.5) / (freq + 0.5) + 1)
            for term, freq in df.items()
        }

    def search(self, query: str, top_k: int = 20) -> list[tuple[int, float]]:
        query_tokens = self._tokenize(query)
        scores = []
        for idx in range(self.corpus_size):
            doc_freqs = self.doc_freqs[idx]
            doc_len = self.doc_lens[idx]
            score = 0.0
            for term in query_tokens:
                if term not in doc_freqs:
                    continue
                freq = doc_freqs[term]
                idf = self.idf.get(term, 0)
                numerator = freq * (self.k1 + 1)
                denominator = freq + self.k1 * (1 - self.b + self.b * doc_len / self.avgdl)
                score += idf * numerator / denominator
            if score > 0:
                scores.append((idx, score))
        scores.sort(key=lambda x: x[1], reverse=True)
        return scores[:top_k]


# ---------------------------------------------------------------------------
# ChromaDB access
# ---------------------------------------------------------------------------

_collection = None
_bm25 = None
doc_index: list[dict] = []  # Parallel to BM25 corpus, for ID lookup
_load_lock = __import__("threading").Lock()


def load_collection():
    """
    Lazy-load ChromaDB and build BM25 index. Thread-safe: the MCP server
    preloads in a background thread at startup, and a tool call may arrive
    mid-load — the lock makes the second caller wait for the first load
    instead of racing it.
    """
    global _collection, _bm25, doc_index
    if _collection is not None:
        return _collection

    with _load_lock:
        if _collection is not None:  # Loaded while we waited for the lock
            return _collection
        return _do_load()


def _do_load():
    global _collection, _bm25, doc_index

    # Imported here rather than at module level so that SQLite-only commands
    # (contracts-intel) and vault-only commands (list-watching, park, archive)
    # run instantly without paying the ChromaDB/torch import cost.
    import chromadb
    from chromadb.utils import embedding_functions

    if not paths.DB_PATH.exists():
        sys.stderr.write(
            f"ChromaDB not found at {paths.DB_PATH}.\n"
            f"Run: python scripts/ingest.py\n"
        )
        sys.exit(2)

    client = chromadb.PersistentClient(path=str(paths.DB_PATH))
    embedder = embedding_functions.SentenceTransformerEmbeddingFunction(
        model_name="all-MiniLM-L6-v2"
    )
    _collection = client.get_collection("tenders", embedding_function=embedder)

    # Pull everything once to build BM25 (this is fine at our scale)
    all_data = _collection.get()
    doc_index = [
        {"id": tid, "metadata": meta, "document": doc}
        for tid, meta, doc in zip(
            all_data["ids"], all_data["metadatas"], all_data["documents"]
        )
    ]
    _bm25 = BM25([d["document"] for d in doc_index])
    return _collection



# ---------------------------------------------------------------------------
# Reciprocal Rank Fusion — combine BM25 + semantic without tuning weights
# ---------------------------------------------------------------------------

def _rrf_fuse(
    semantic: list[tuple[str, float]],
    keyword: list[tuple[str, float]],
    k: int = 60,
) -> list[tuple[str, float]]:
    """
    Standard RRF. We score by rank position rather than the raw scores, which
    dodges the problem of BM25 and cosine similarity being on different scales.
    """
    scores: dict[str, float] = {}
    for rank, (doc_id, _) in enumerate(semantic, start=1):
        scores[doc_id] = scores.get(doc_id, 0) + 1 / (k + rank)
    for rank, (doc_id, _) in enumerate(keyword, start=1):
        scores[doc_id] = scores.get(doc_id, 0) + 1 / (k + rank)
    return sorted(scores.items(), key=lambda x: x[1], reverse=True)

