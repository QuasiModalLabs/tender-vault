"""
The corpus itself: the BM25 index, the ChromaDB handle, and the loaded docs.

THIS MODULE OWNS `_collection`, `_chunk_collection`, `_bm25` and `doc_index`. Like paths.py, they are
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
_chunk_collection = None   # None when the corpus predates chunking - see _do_load
_bm25 = None
doc_index: list[dict] = []  # Parallel to BM25 corpus, for ID lookup
_load_lock = __import__("threading").Lock()


def _reset_corpus_state() -> None:
    """
    Clear every cached corpus global, in ONE place.

    Tests that swap in a temporary corpus have to reset this module's caches, and
    they used to do it by listing the globals at each call site. That list is a
    silent trap: adding a global (as `_chunk_collection` did) leaks state between
    tests everywhere the new name was not added, and the symptom is a test
    passing against the previous test's corpus. Resetting through one function
    makes the next global impossible to forget.
    """
    global _collection, _chunk_collection, _bm25, doc_index
    _collection = None
    _chunk_collection = None
    _bm25 = None
    doc_index = []


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
    global _collection, _chunk_collection, _bm25, doc_index

    # Imported here rather than at module level so that SQLite-only commands
    # (contracts-intel) and vault-only commands (list-watching, park, archive)
    # run instantly without paying the ChromaDB/torch import cost.
    import chromadb
    from chromadb.utils import embedding_functions

    if not paths.DB_PATH.exists():
        sys.stderr.write(
            f"ChromaDB not found at {paths.DB_PATH}.\n"
            f"Run: python scripts/ingest\n"
        )
        sys.exit(2)

    client = chromadb.PersistentClient(path=str(paths.DB_PATH))
    embedder = embedding_functions.SentenceTransformerEmbeddingFunction(
        model_name="all-MiniLM-L6-v2"
    )
    _collection = client.get_collection("tenders", embedding_function=embedder)

    # OPTIONAL. A corpus built before chunking has no `tender_chunks`, and
    # test_provenance assembles a `tenders` collection by hand with no sibling.
    # Absent means semantic search falls back to the tender-level collection
    # rather than failing to load the corpus at all.
    try:
        _chunk_collection = client.get_collection(
            "tender_chunks", embedding_function=embedder)
    except Exception:
        _chunk_collection = None

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



def _pool_chunk_hits(results: dict, exclude_tender_id: str | None = None
                     ) -> list[tuple[str, float]]:
    """
    Collapse chunk hits into one ranked score per tender, taking the MAX.

    MAX, NOT MEAN — and this is the whole point, not a detail. Mean pooling
    divides a strong single-chunk match by the number of chunks the notice
    happens to have, so the longer the document the more a real match is
    diluted. Long documents are exactly the case chunking exists to serve: a
    forty-chunk notice with one perfectly matching requirements section would
    score near zero under mean and rank top under max. Mean would
    reintroduce the very bug this fixes, wearing a different hat.

    `exclude_tender_id` drops self-matches for find_similar. It is matched on
    the TENDER, not the chunk id, because every one of a notice's own chunks
    matches itself and excluding a single chunk id would leave all its others.
    """
    best: dict[str, float] = {}
    ids = (results.get("ids") or [[]])[0]
    if not ids:
        return []
    distances = (results.get("distances") or [[]])[0] or [0] * len(ids)
    metadatas = (results.get("metadatas") or [[]])[0] or [{}] * len(ids)
    for chunk_id, distance, meta in zip(ids, distances, metadatas):
        # Prefer the recorded tender_id; fall back to the id's stem. rsplit, not
        # split: a tender_id containing '#' still resolves.
        tender_id = (meta or {}).get("tender_id") or str(chunk_id).rsplit("#", 1)[0]
        if exclude_tender_id is not None and tender_id == exclude_tender_id:
            continue
        score = 1 / (1 + distance)
        if score > best.get(tender_id, -1.0):
            best[tender_id] = score
    return sorted(best.items(), key=lambda kv: kv[1], reverse=True)


def _semantic_ranked(query_text: str, n_pool: int,
                     exclude_tender_id: str | None = None
                     ) -> list[tuple[str, float]]:
    """
    Tender-level semantic ranking.

    Reads the chunk collection when there is one and max-pools it back to
    tenders, so the caller receives the same (tender_id, score) shape it always
    did and the RRF fusion downstream is untouched. Feeding chunk-level ranks
    into RRF would let one long notice occupy several rank slots and outvote
    everything else.

    Falls back to the tender-level collection when `tender_chunks` is absent —
    a corpus built before this change, or a collection assembled by hand in a
    test. Degraded, not broken: that path truncates at the model's window, which
    is the behaviour this fix replaced.
    """
    if _chunk_collection is None:
        results = _collection.query(query_texts=[query_text], n_results=n_pool)
        return _pool_chunk_hits(results, exclude_tender_id)

    # Ask for more chunks than the caller's tender budget: chunks collapse into
    # tenders, so k chunk hits yield at most k tenders and usually far fewer.
    #
    # The headroom matters when a tender is being excluded. Every one of the
    # target's own chunks scores near-perfectly against its own text, so they
    # occupy the top of the chunk ranking as a block - forty of them for the
    # longest notice in the current corpus. Without room to clear that block,
    # find_similar asks for n*3 chunks, discards every one as a self-match and
    # returns nothing.
    #
    # The block is MEASURED, not guessed at. A constant sized against today's
    # longest notice silently under-provisions the day a longer one arrives -
    # the feed already carries a 33k-character description, which chunks well
    # past any round number worth hard-coding. Silent under-provisioning is the
    # exact failure mode this change exists to remove, so it is not reintroduced
    # here in miniature.
    want = n_pool * 3
    if exclude_tender_id is not None:
        own = _chunk_collection.get(where={"tender_id": exclude_tender_id})
        want += len(own.get("ids") or [])
    want = min(want, max(_chunk_collection.count(), 1))
    results = _chunk_collection.query(query_texts=[query_text], n_results=want)
    return _pool_chunk_hits(results, exclude_tender_id)


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

