"""
Tools for Claude Code to work with the tender corpus.

This is the 'wrap' layer — Claude decides what to search for, when to fetch
full details, when to promote, etc. These tools just execute cleanly and
return JSON.

Design principle: each command is a single verb. Each prints JSON to stdout.
Errors go to stderr and non-zero exit codes. Claude can chain these freely.

Usage:
    python scripts/tender_tools.py search "cloud migration federal"
    python scripts/tender_tools.py get W1234-567890
    python scripts/tender_tools.py similar W1234-567890
    python scripts/tender_tools.py list-corpus --window imminent
    python scripts/tender_tools.py list-watching
    python scripts/tender_tools.py list-parked
    python scripts/tender_tools.py contracts-intel "cloud"
    python scripts/tender_tools.py promote W1234-567890
    python scripts/tender_tools.py park some-file.md "no clearance" "after hiring cleared architect"
    python scripts/tender_tools.py archive some-file.md "lost to competitor"
    python scripts/tender_tools.py attach cb-342-92719341 --platform merx
    python scripts/tender_tools.py list-attachments cb-342-92719341
    python scripts/tender_tools.py read-attachment cb-342-92719341 RFP-W2187-SPO.pdf --limit 40
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import attachments  # noqa: E402
import org_resolve  # noqa: E402

# The single definition of what kind of thing a notice is, shared with the
# ingest filter. Imported rather than reimplemented: when the dossier and the
# corpus disagree about whether an "RFP against Supply Arrangement" is work,
# one of them is lying to the reader, and it is not obvious which.
from contracts_ingest import normalize_vendor  # noqa: E402
from ingest import classify_notice as _classify_notice  # noqa: E402
from ingest import entity_org_keys as _entity_org_keys  # noqa: E402



PROJECT_ROOT = Path(__file__).parent.parent
DB_PATH = PROJECT_ROOT / "chroma_db"
VAULT = PROJECT_ROOT / "vault"
PROFILE = VAULT / "profiles" / "my-company.md"
WATCHING = VAULT / "tenders" / "watching"
ARCHIVED = VAULT / "tenders" / "archived"
PARKED = VAULT / "tenders" / "parked"

# Committed, unlike chroma_db/ — which is what makes the corpus stamp
# comparable across machines. CI rebuilds the corpus on its runner and pushes
# only the digest, so the digest's stamp is the one observable fact about what
# another machine last built.
DIGESTS = VAULT / "digests"

# Department nodes, named by canonical key. The target of every [[key]] link a
# tender file writes, and the file whose backlinks answer "what are we looking
# at from this department".
#
# HAND-EDITABLE AND NEVER OVERWRITTEN. Deliberately not derived from the
# contracts ingest: that generator writes <key>-contracts.md, rewrites it every
# run, and only runs at all if someone took the optional ~630MB download. Graph
# connectivity cannot be a side effect of an optional step.
#
# CREATED ON PROMOTE, not generated in bulk. The registry carries ~100 keys and
# a vault holding 100 department stubs for the handful actually in play is a
# graph that hides its own signal.
AGENCIES = VAULT / "agencies"


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

    if not DB_PATH.exists():
        sys.stderr.write(
            f"ChromaDB not found at {DB_PATH}.\n"
            f"Run: python scripts/ingest\n"
        )
        sys.exit(2)

    client = chromadb.PersistentClient(path=str(DB_PATH))
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


def _display_agency(meta: dict) -> str:
    """
    Which department to SHOW for a tender. End user is the department that
    needs the work and is what we care about; the contracting entity is the
    fallback for the ~half of rows where end user is blank. This is a display
    convenience only — the two fields are stored separately in ChromaDB and
    anything matching on department should read them separately.
    """
    return meta.get("end_user_entity") or meta.get("contracting_entity") or ""


def _yaml_list(raw: str) -> list[str]:
    """
    One inline frontmatter sequence as a list, for the no-dependency parse.

    cmd_list_watching reads frontmatter by hand rather than pulling in a YAML
    parser, so the `[a, b]` flow form is unpacked here. Quotes come off because
    department entries are written with them: a bare [[ircc]] is a nested
    sequence in YAML, not a string.
    """
    inner = raw.strip()
    if not inner.startswith("["):
        return [inner.strip('"')] if inner else []
    return [p.strip().strip('"') for p in inner[1:].rstrip("]").split(",") if p.strip()]


# ---------------------------------------------------------------------------
# Corpus provenance — how old is what you are reading
# ---------------------------------------------------------------------------
# A briefing once dated the corpus from `chroma_db/` file times and got it
# wrong: ChromaDB rewrites its HNSW segment files whenever anything LOADS the
# collection, so those mtimes report when the corpus was last queried. Reading
# them describes your own read. The ingest therefore stamps the collection, and
# this is where that stamp is read back.
#
# TWO stamps, because they answer different questions. `feed_downloaded_at`
# says how old the DATA is; `corpus_built_at` says when it was last processed.
# A rebuild off an unchanged cache moves the second and not the first, and that
# is a filter or profile change rather than new notices.
#
# NO THRESHOLD, and deliberately no "stale" field. How old is too old is not
# stateable — the ingest cron is weekly, so two days is normal — and a boolean
# verdict in a data field is the kind of judgement this project keeps out of
# its tools. Report the stamps beside the newest digest's and let the reader
# compare two observed dates.

def _digest_frontmatter(path: Path) -> dict[str, str]:
    """
    Frontmatter of one digest as a flat dict of STRINGS.

    Hand-parsed rather than run through PyYAML, and the type matters more than
    the parser. `corpus_built_at: 2026-08-09T14:27:11` unquoted is a YAML
    timestamp scalar: safe_load returns a datetime.datetime, while the stamp it
    gets compared against — read out of Chroma metadata — is a str. Every
    equality check between the two is then False without anything raising, and
    the machine that produced the digest reports itself as behind. digest.py
    quotes the values for exactly this reason.

    So the string type is asserted rather than assumed. If this is ever swapped
    for safe_load, or the quoting in digest.py is dropped, it fails loudly here
    instead of returning a confident wrong answer downstream.
    """
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    match = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
    if not match:
        return {}
    fields: dict[str, str] = {}
    for line in match.group(1).split("\n"):
        if ":" in line:
            key, _, value = line.partition(":")
            fields[key.strip()] = value.strip().strip('"')
    bad = {k: type(v).__name__ for k, v in fields.items() if not isinstance(v, str)}
    if bad:
        raise TypeError(
            f"{path.name}: frontmatter values must be str, got {bad}. An ISO "
            f"timestamp parsed as a YAML date compares unequal to the string "
            f"stamp in the collection metadata, silently, in every direction."
        )
    return fields


def _newest_digest() -> Path | None:
    """
    The most recent digest file, or None if there are none.

    Same glob and ordering as digest._previous_digest, which documents the
    invariant this relies on: "Filenames are ISO-dated and prefixed, so a
    lexicographic sort is a chronological one." Kept identical on purpose — two
    lookups over the same directory that sort differently is a bug nobody sees.
    """
    digests = sorted(DIGESTS.glob("digest-*.md"), key=lambda p: p.stem)
    return digests[-1] if digests else None


def _corpus_provenance() -> dict:
    """
    When this corpus was built, and how that compares to the newest digest.

    Four states per source, and they are NOT interchangeable:

      stamped           both fields present.
      unstamped         neither — built before provenance stamping. Rebuild.
      no_feed_at_build  built, but with no cached feed to date. The corpus is
                        current as a build and its DATA cannot be dated. That
                        is not the same fact as `unstamped`, which is why it
                        gets its own name: reading it as "predates stamping"
                        invites a rebuild that would fix nothing.
      not_found         (digest only) no digest file resolved at all. Distinct
                        from `unstamped` so a glob that silently matches
                        nothing cannot masquerade as working code.
    """
    meta = dict(getattr(_collection, "metadata", None) or {})
    built_at = meta.get("corpus_built_at")
    feed_at = meta.get("feed_downloaded_at")

    if built_at is None:
        local_state = "unstamped"
        local_note = ("This corpus predates provenance stamping. "
                      "Re-run: python scripts/ingest")
    elif feed_at is None:
        local_state = "no_feed_at_build"
        local_note = ("Ingest ran with no cached feed, so the corpus is "
                      "stamped but the age of the data in it is unknown. "
                      "This is not the same as predating stamping — a rebuild "
                      "alone will not date it.")
    else:
        local_state, local_note = "stamped", None

    out = {
        "corpus_built_at": built_at,
        "feed_downloaded_at": feed_at,
        "state": local_state,
    }
    if local_note:
        out["note"] = local_note

    newest = _newest_digest()
    if newest is None:
        out["newest_digest"] = None
        out["newest_digest_state"] = "not_found"
        out["newest_digest_note"] = (
            f"No digest matched {DIGESTS}/digest-*.md. Expected at least one; "
            f"if digests exist, the lookup is broken rather than empty."
        )
        return out

    fm = _digest_frontmatter(newest)
    d_built = fm.get("corpus_built_at") or None
    d_feed = fm.get("feed_downloaded_at") or None
    out["newest_digest"] = newest.stem
    out["newest_digest_corpus_built_at"] = d_built
    out["newest_digest_feed_downloaded_at"] = d_feed

    if d_built is None:
        out["newest_digest_state"] = "unstamped"
        out["newest_digest_note"] = (
            "This digest predates provenance stamping. "
            "Re-run: python scripts/digest.py")
        return out
    if d_feed is None:
        out["newest_digest_state"] = "no_feed_at_build"
    else:
        out["newest_digest_state"] = "stamped"

    # The comparison, and it reads BOTH stamps. "The digest is newer so I am
    # behind" only holds if the FEED moved; an equal feed stamp with a later
    # build is a rebuild of the same data.
    if local_state == "stamped" and d_feed is not None:
        if feed_at == d_feed:
            if built_at == d_built:
                out["reading"] = "this corpus produced the newest digest"
            else:
                out["reading"] = (
                    "same feed, different build — a membership difference is a "
                    "filter or profile effect, not new notices")
        elif feed_at < d_feed:
            out["reading"] = (
                "behind on data — the newest digest was built from a feed this "
                "machine has not downloaded. Run: python scripts/ingest")
        else:
            out["reading"] = (
                "this machine has a feed the newest digest has not seen")
    return out


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


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_search(args) -> dict:
    """Hybrid search. Returns list of {tender_id, title, score, snippet}."""
    load_collection()
    n_pool = max(args.n * 3, 30)  # Pull more for fusion, then trim

    # Semantic side — ChromaDB, max-pooled from chunks back to tenders so that
    # what reaches the fusion below is one entry per tender, exactly as before.
    semantic = _semantic_ranked(args.query, n_pool)

    # Keyword side — BM25
    bm25_hits = _bm25.search(args.query, top_k=n_pool)
    keyword = [(doc_index[idx]["id"], score) for idx, score in bm25_hits]

    # Fuse
    fused = _rrf_fuse(semantic, keyword)[:args.n]

    # Build response
    id_to_doc = {d["id"]: d for d in doc_index}
    results = []
    for doc_id, fused_score in fused:
        doc = id_to_doc.get(doc_id)
        if not doc:
            continue
        snippet = doc["document"][:300].replace("\n", " ").strip() + "..."
        results.append({
            "tender_id": doc_id,
            "title": doc["metadata"].get("title", ""),
            "agency": _display_agency(doc["metadata"]),
            "closing_date": doc["metadata"].get("closing_date", ""),
            # Derived now, not stored — see _window_fields. `imminent` means
            # inside the profile threshold; those notices used to be deleted at
            # ingest. `closed` means it expired since the corpus was built.
            **_window_fields(doc["metadata"]),
            # None, never 0. The ingest omits this key when no value was
            # extracted, and defaulting it to 0 is what made every tender in
            # the corpus read as a $0 contract.
            "estimated_value": doc["metadata"].get("estimated_value"),
            "matched_competencies": doc["metadata"].get("matched_competencies", ""),
            # What KIND of thing this is, before any judgement of fit. A
            # qualification is a vehicle, not work; see classify_notice.
            "opportunity_kind": doc["metadata"].get("opportunity_kind", "unknown"),
            "kind_basis": doc["metadata"].get("kind_basis", "unclassified"),
            "unspsc_families": doc["metadata"].get("unspsc_families", ""),
            "score": round(fused_score, 4),
            "in_watching": (WATCHING / f"{_slugify(doc_id)}.md").exists(),
            "snippet": snippet,
        })
    return {"query": args.query, "n": len(results), "results": results}


def cmd_get(args) -> dict:
    """Full details for one tender."""
    load_collection()
    id_to_doc = {d["id"]: d for d in doc_index}
    doc = id_to_doc.get(args.tender_id)
    if not doc:
        return {"error": f"Tender {args.tender_id} not found in corpus"}
    return {
        "tender_id": doc["id"],
        "metadata": doc["metadata"],
        # Alongside metadata rather than merged into it: metadata is what the
        # ingest wrote and is stable until the next one, these two are computed
        # for today and change under a corpus that hasn't moved.
        "derived": _window_fields(doc["metadata"]),
        # A third category again: not this notice's data, but how old the
        # corpus it came out of is. `derived` changes daily under a static
        # corpus; this says how static the corpus actually is.
        "provenance": _corpus_provenance(),
        "document": doc["document"],
        "in_watching": (WATCHING / f"{_slugify(doc['id'])}.md").exists(),
    }


def cmd_similar(args) -> dict:
    """Find tenders similar to a given one (by its embedding)."""
    load_collection()
    # Query using the target tender's document text as the query
    id_to_doc = {d["id"]: d for d in doc_index}
    target = id_to_doc.get(args.tender_id)
    if not target:
        return {"error": f"Tender {args.tender_id} not found"}

    # The query text is still capped: this one is a genuine cap, not a silent
    # truncation of stored data. A query longer than the model's window is
    # pointless, and the target's opening is what characterises it.
    ranked = _semantic_ranked(target["document"][:1000], args.n + 1,
                              exclude_tender_id=args.tender_id)
    similar = []
    for doc_id, score in ranked:
        doc = id_to_doc.get(doc_id)
        if not doc:
            continue
        similar.append({
            "tender_id": doc_id,
            "title": doc["metadata"].get("title", ""),
            "agency": _display_agency(doc["metadata"]),
            "similarity": round(score, 4),
        })
    return {"target": args.tender_id, "similar": similar[:args.n]}


def cmd_list_corpus(args) -> dict:
    """
    Every notice in the corpus, ordered by closing date.

    The briefing is instructed to read the corpus end to end rather than search
    it, and until now there was no command that returned it — doing that meant
    reading ChromaDB directly, outside the tool layer that every other corpus
    operation goes through. `search` cannot substitute: it ranks against a query
    and returns n, which is the opposite of surveying what is open.

    Sorted by closing date with `standing` and `unknown` last, because a
    placeholder year sorted numerically puts a permanent supply arrangement at
    the bottom of the list and a missing date at the top of it.
    """
    load_collection()
    rows = []
    for doc in doc_index:
        meta = doc["metadata"]
        derived = _window_fields(meta)
        if args.window and derived["closing_window"] != args.window:
            continue
        rows.append({
            "tender_id": doc["id"],
            "title": meta.get("title", ""),
            "agency": _display_agency(meta),
            "closing_date": meta.get("closing_date", ""),
            **derived,
            "opportunity_kind": meta.get("opportunity_kind", "unknown"),
            "kind_basis": meta.get("kind_basis", "unclassified"),
            "matched_competencies": meta.get("matched_competencies", ""),
            "unspsc_families": meta.get("unspsc_families", ""),
            "in_watching": (WATCHING / f"{_slugify(doc['id'])}.md").exists(),
        })

    rank = {"closed": 0, "imminent": 1, "open": 2, "standing": 3, "unknown": 4}
    rows.sort(key=lambda r: (rank.get(r["closing_window"], 9), r["closing_date"]))

    counts: dict[str, int] = {}
    for r in rows:
        counts[r["closing_window"]] = counts.get(r["closing_window"], 0) + 1
    return {
        "count": len(rows),
        # First, because a survey of what is open is worth exactly as much as
        # the corpus it reads, and the reader cannot judge that from the rows.
        "provenance": _corpus_provenance(),
        "by_window": counts,
        "imminent_within_days": _profile_imminence_threshold(),
        "filtered_to": args.window,
        "corpus": rows,
    }


def cmd_list_watching(args) -> dict:
    """List all tenders in the watching folder with basic metadata."""
    if not WATCHING.exists():
        return {"watching": []}
    files = sorted(WATCHING.glob("*.md"))
    tenders = []
    for f in files:
        content = f.read_text(encoding="utf-8")
        # Extract a few fields from frontmatter without a full YAML parse
        fm_match = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
        fields = {}
        if fm_match:
            for line in fm_match.group(1).split("\n"):
                if ":" in line:
                    k, _, v = line.partition(":")
                    fields[k.strip()] = v.strip().strip('"')
        tenders.append({
            "filename": f.name,
            "tender_id": fields.get("tender_id", ""),
            "title": fields.get("title", ""),
            "closing_date": fields.get("closing_date", ""),
            "status": fields.get("status", ""),
            # Both department fields, so "which departments are we watching" and
            # "what did the registry fail to resolve" are answerable from the
            # tool rather than by grepping the vault. The second is the evidence
            # for whether the registry needs a Crown-corporation tier.
            "department": _yaml_list(fields.get("department", "")),
            "department_unresolved": _yaml_list(
                fields.get("department_unresolved", "")),
        })
    return {"watching": tenders}


# The three entity_source values in prose. Keyed rather than formatted so an
# unrecognised source raises here instead of being quietly rendered as a bare
# key — the set is fixed and test_dossier locks it.
_SOURCE_PROSE = {
    "end_user": "named as end user",
    "contracting_entity_end_user_unstated": "contracting entity; end user unstated",
    "contracting_entity_end_user_names_others":
        "contracting entity; end user is another department",
}


def _attribution_note(attribution: dict[str, dict], unresolved: list[str]) -> str:
    """
    Spell the attribution out in the BODY, not only in frontmatter.

    Someone reading this file in three weeks should not have to go and check a
    metadata field to learn that its department is a buyer of record rather than
    the stated customer, or that a registry miss means "not a department" rather
    than "not federal". Both are easy to reconstruct wrongly and expensive to
    reconstruct wrongly.
    """
    weak = [k for k, a in attribution.items() if a["entity_source"] != "end_user"]
    named = ", ".join(f'"{u}"' for u in unresolved) or "either entity field"

    if not attribution:
        return (
            f"\n> **Attribution note:** The organization registry did not resolve "
            f"{named}. The registry indexes *departments and agencies*, so a miss "
            "means **not a department**, not **not federal** — federal Crown "
            "corporations such as CDIC, BDC and Canada Post have no entry in it. "
            "See [[dossier]].\n"
        )

    lines = []
    if weak:
        lines.append(
            ", ".join(f"[[{k}]]" for k in weak)
            + (" is " if len(weak) == 1 else " are ")
            + "the contracting entity here, not a stated end user. Federal IT is "
            "routinely bought by SSC or PSPC on behalf of the department that "
            "actually needs the work, so this is weaker evidence than an end-user "
            "attribution. See [[dossier]]."
        )
    if unresolved:
        lines.append(
            f"The registry also did not resolve {named} — a miss there means "
            "*not a department*, not *not federal*."
        )
    if not lines:
        return ""
    lines[0] = f"**Attribution note:** {lines[0]}"
    return "\n" + "\n>\n".join(f"> {line}" for line in lines) + "\n"


def _ensure_agency_nodes(attribution: dict[str, dict]) -> list[str]:
    """
    Create a department node for every key this tender links, if absent.

    CREATE-IF-MISSING, NEVER OVERWRITE. An existing node is left exactly as it
    is — it is the one file in this pair a person is expected to write in, and
    a promote that rewrote it would eat notes accumulated across every tender
    that department has ever appeared in. The check is `exists()`, not a marker
    check, because there is nothing here worth clobbering a stranger's file for.

    Returns the keys actually created, so promote can report them rather than
    leaving new vault files to be noticed later.
    """
    created: list[str] = []
    if not attribution:
        return created

    AGENCIES.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d")

    for key in attribution:
        target = AGENCIES / f"{key}.md"
        if target.exists():
            continue

        # display_name falls back to the key itself for anything the registry
        # cannot name, which is the right failure: a node named after a key that
        # resolves to nothing is still a working link target.
        try:
            title = org_resolve.default_resolver().display_name(key) or key
        except Exception:
            title = key

        target.write_text(
            f"""---
canonical_key: {key}
agency: "{title[:150].replace('"', "'")}"
type: department
created: {stamp}
created_by: tender_tools.promote
---

# {title}

Department node — **hand-editable, and nothing regenerates it.** Created on the
first promote of a tender attributed to `{key}`; never rewritten afterwards.

The backlinks on this file are every tender, briefing and note in the vault that
links [[{key}]]. That list is the reason this file exists.

Awarded-contract intelligence is a separate, generated file: [[{key}-contracts]],
written by `scripts/contracts_ingest.py` and rewritten on every run. It is not
committed and will not exist until that ingest has been run, so treat a dead
link there as "not built yet" rather than "no contracts".

## Notes

What we've learned about dealing with this department: how they buy, who keeps
winning, which constraints recur, and what we decided and why. Both of us write
here — Claude appends dated entries as things come up, per `vault/CLAUDE.md`.
Append-only, newest at the bottom; nothing above is edited or removed.
""",
            encoding="utf-8",
            newline="\n",
        )
        created.append(key)

    return created


def cmd_promote(args) -> dict:
    """Copy a tender from ChromaDB into vault/tenders/watching/ as markdown."""
    load_collection()
    id_to_doc = {d["id"]: d for d in doc_index}
    doc = id_to_doc.get(args.tender_id)
    if not doc:
        return {"error": f"Tender {args.tender_id} not found"}

    meta = doc["metadata"]
    filename = f"{_slugify(doc['id'])}.md"
    target = WATCHING / filename
    if target.exists():
        return {"error": f"Already promoted: {filename}"}

    WATCHING.mkdir(parents=True, exist_ok=True)

    # Build the markdown file with YAML frontmatter
    matched = meta.get("matched_competencies", "")
    matched_list = [m.strip() for m in matched.split(",") if m.strip()]
    families = meta.get("unspsc_families", "")
    family_list = [f.strip() for f in families.split(",") if f.strip()]

    # The ingest omits estimated_value entirely when nothing was extracted, so
    # "not stated" survives into the vault instead of being written down as $0
    # and later read back as a fact about the contract.
    value = meta.get("estimated_value")
    value_yaml = "null" if value is None else f"{value}"
    value_prose = "Not stated" if value is None else f"${value:,.0f}"
    kind = meta.get("opportunity_kind", "unknown")

    # Departments as WIKILINKS on the CANONICAL KEY, never the display string.
    # The key is the identity the rest of the project already uses, so linking on
    # it is what lets the vault graph connect a tender to its department — and,
    # through that department's backlinks, to every other tender touching it.
    # Resolved through _entity_attribution, shared with the dossier, so a tender
    # file and a dossier can never disagree about who a notice is for.
    end_user_raw = str(meta.get("end_user_entity") or "").strip()
    contracting_raw = str(meta.get("contracting_entity") or "").strip()
    attribution = _entity_attribution(end_user_raw, contracting_raw)

    # Quoted, because a bare [[ircc]] is a nested YAML sequence rather than a
    # string. Always a list, even for one department: a sometimes-scalar field
    # means every reader needs a type check and one of them will forget.
    dept_yaml = ", ".join(f'"[[{key}]]"' for key in attribution)
    source_yaml = ", ".join(a["entity_source"] for a in attribution.values())

    # Entity strings the registry did not resolve, kept as EVIDENCE rather than
    # dropped. Recorded even when the other field DID resolve, because the
    # question this list answers — should the registry grow a Crown-corporation
    # tier — deserves to be settled by counts rather than by someone noticing.
    unresolved: list[str] = []
    for raw in (end_user_raw, contracting_raw):
        if raw and raw not in unresolved and not _entity_keys(raw):
            unresolved.append(raw)
    unresolved_yaml = (
        "\ndepartment_unresolved: ["
        + ", ".join(f'"{u.replace(chr(34), chr(39))}"' for u in unresolved)
        + "]"
    ) if unresolved else ""

    dept_prose = ", ".join(
        f"[[{key}]] ({_SOURCE_PROSE[a['entity_source']]})"
        for key, a in attribution.items()
    ) or "None resolved — see the attribution note below."

    content = f"""---
tender_id: {doc['id']}
title: "{meta.get('title', '').replace('"', "'")}"
agency: "{_display_agency(meta).replace('"', "'")}"
department: [{dept_yaml}]
entity_source: [{source_yaml}]{unresolved_yaml}
closing_date: {meta.get('closing_date', '')}
estimated_value: {value_yaml}
matched_competencies: [{', '.join(matched_list)}]
unspsc_families: [{', '.join(family_list)}]
opportunity_kind: {kind}
kind_basis: {meta.get('kind_basis', 'unclassified')}
status: watching
promoted_at: {datetime.now().strftime('%Y-%m-%d')}
---

# {meta.get('title', 'Untitled')}

**Agency:** {_display_agency(meta) or 'Unknown'}
**Departments:** {dept_prose}
**Closes:** {meta.get('closing_date', 'Unknown')}
**Estimated value:** {value_prose}
**Instrument:** {kind} (per {meta.get('kind_basis', 'unclassified')})
**Matched on:** {', '.join(matched_list) if matched_list else 'none'}
**UNSPSC families:** {', '.join(family_list) if family_list else 'none'}
{_attribution_note(attribution, unresolved)}
## Description

{doc['document']}

## My notes

<!-- Claude can append analysis here under "## Fit assessment" -->
"""
    target.write_text(content, encoding="utf-8", newline="\n")

    # After the tender is on disk: the tender file is the artifact being asked
    # for, and the nodes exist to serve it. Reported rather than silent — these
    # are new vault files, and a tool that creates files without saying so is a
    # tool you have to audit afterwards.
    created = _ensure_agency_nodes(attribution)

    result = {"promoted": str(target.relative_to(PROJECT_ROOT))}
    if created:
        result["agency_nodes_created"] = [
            str((AGENCIES / f"{key}.md").relative_to(PROJECT_ROOT)) for key in created
        ]
    return result


def cmd_archive(args) -> dict:
    """
    Move a tender to archived/ with a reason. Source can be watching/ or parked/.

    Archived = decision is final. Use park instead if you might revisit.
    """
    # Look in watching first, then parked
    for source_dir in (WATCHING, PARKED):
        candidate = source_dir / args.filename
        if candidate.exists():
            source = candidate
            break
    else:
        return {
            "error": f"Not found in watching/ or parked/: {args.filename}",
        }

    ARCHIVED.mkdir(parents=True, exist_ok=True)
    target = ARCHIVED / args.filename

    # Documents move BEFORE the note does. See _move_attachment_dir.
    moved, error = _move_attachment_dir(source, target)
    if error:
        return error

    # Append the archive reason to the file before moving
    content = source.read_text(encoding="utf-8")
    stamp = datetime.now().strftime("%Y-%m-%d")
    from_dir = source.parent.name
    content += f"\n\n## Archived {stamp} (from {from_dir})\n\n{args.reason}\n"
    target.write_text(content, encoding="utf-8", newline="\n")
    source.unlink()
    result = {
        "archived": str(target.relative_to(PROJECT_ROOT)),
        "from": from_dir,
        "reason": args.reason,
    }
    if moved:
        result["attachments_moved"] = moved
    return result


def cmd_park(args) -> dict:
    """
    Move a watching tender to parked/ with a reason and a revisit trigger.

    Park is for "not pursuing now but the situation might change." The trigger
    is a freeform string describing what would make this worth re-evaluating
    (e.g. "after we hire a cleared architect", "if reissued in 2027").
    Distinct from archive, which is for permanent close-out.
    """
    source = WATCHING / args.filename
    if not source.exists():
        return {"error": f"Not in watching/: {args.filename}"}

    PARKED.mkdir(parents=True, exist_ok=True)
    target = PARKED / args.filename

    # Documents move BEFORE the note does. See _move_attachment_dir.
    moved, error = _move_attachment_dir(source, target)
    if error:
        return error

    content = source.read_text(encoding="utf-8")
    stamp = datetime.now().strftime("%Y-%m-%d")
    content += (
        f"\n\n## Parked {stamp}\n\n"
        f"**Reason:** {args.reason}\n\n"
        f"**Revisit when:** {args.revisit_when}\n"
    )
    target.write_text(content, encoding="utf-8", newline="\n")
    source.unlink()
    result = {
        "parked": str(target.relative_to(PROJECT_ROOT)),
        "reason": args.reason,
        "revisit_when": args.revisit_when,
    }
    if moved:
        result["attachments_moved"] = moved
    return result


# ---------------------------------------------------------------------------
# Attached documents — manually dropped, never fetched
# ---------------------------------------------------------------------------
# The RFP package lives on MERX or Ariba behind an account wall and this
# project deliberately does not scrape those platforms. A human pulls the
# files in a browser and drops them into the folder these commands manage.
# scripts/attachments.py holds the extraction and manifest logic; everything
# here is path resolution and the CLI/MCP shape.
#
# The folder is created on demand rather than on promote, and its existence is
# itself a signal: it marks the tenders that got real effort, which watching /
# parked / archived do not distinguish.


def _note_for_tender(tender_id: str) -> Path | None:
    """
    The note for a tender_id, wherever it currently lives.

    Deliberately searches all three lifecycle states, unlike the four inline
    `WATCHING / f"{_slugify(id)}.md"` checks elsewhere in this file — those
    answer `in_watching`, which is a narrower question, and widening them would
    change what the corpus commands report. Attachments have to keep working
    after a tender is parked or archived, so they need this one instead.
    """
    name = f"{_slugify(tender_id)}.md"
    for directory in (WATCHING, PARKED, ARCHIVED):
        candidate = directory / name
        if candidate.exists():
            return candidate
    return None


def _attachment_dir(note: Path) -> Path:
    """Beside the note, named for it: cb-342-92719341.md -> cb-342-92719341/."""
    return note.parent / note.stem


def _move_attachment_dir(source: Path, target: Path) -> tuple[str | None, dict | None]:
    """
    Move a tender's document folder to follow its note. Returns (moved, error).

    ORDER IS THE POINT, and the caller must run this BEFORE writing the note.
    A crash between the two leaves a note pointing at a folder that isn't there
    — wrong, but detectable by anyone who looks. The other order leaves a folder
    that no note references, which nothing will ever surface again.
    """
    src_dir = _attachment_dir(source)
    if not src_dir.is_dir():
        return None, None

    dst_dir = _attachment_dir(target)
    if dst_dir.exists():
        return None, {
            "error": f"Attachment folder already exists at the destination: "
                     f"{dst_dir}. Resolve it by hand; nothing was moved."
        }
    try:
        shutil.move(str(src_dir), str(dst_dir))
    except OSError as exc:
        # Report and stop. Moving the note anyway would produce exactly the
        # invisible orphan this ordering exists to prevent.
        return None, {"error": f"Could not move attachment folder: {exc}"}
    return str(dst_dir.relative_to(PROJECT_ROOT)), None


def _reveal_in_file_manager(path: Path) -> None:
    """
    Best effort, and nothing more. Never raises, never blocks for long.

    Skipped entirely when stdout is not a terminal: over SSH, in CI, and on the
    MCP server — where stdout is the protocol channel and a file manager is
    meaningless — these calls variously fail, hang, or open something on the
    wrong machine. The absolute path printed by the caller is the actual
    contract with the user; this is a convenience on top of it.
    """
    if not sys.stdout.isatty():
        return
    try:
        if sys.platform == "win32":
            os.startfile(str(path))  # noqa: S606
        elif sys.platform == "darwin":
            subprocess.run(["open", str(path)], timeout=5, check=False)
        else:
            subprocess.run(["xdg-open", str(path)], timeout=5, check=False)
    except Exception:
        # Including timeouts: xdg-open under WSL can block until it is killed.
        pass


def cmd_attach(args) -> dict:
    """
    Create the document folder for a tender and print its absolute path.

    Nothing is downloaded. The user opens MERX or Ariba in a browser, gets past
    the account wall by hand, and drops the RFP package into this folder.
    """
    note = _note_for_tender(args.tender_id)
    if note is None:
        return {
            "error": f"No tender note for {args.tender_id} in watching/, "
                     f"parked/ or archived/. Promote it first."
        }

    # Validated here, not only in the parser. argparse enforces `choices` on the
    # CLI, but the MCP tool builds its own namespace and never touches it — so
    # without this an unrecognised platform would be written into the manifest
    # as provenance and read back later as though someone had recorded it.
    platform = getattr(args, "platform", None)
    if platform not in attachments.SOURCE_PLATFORMS:
        return {
            "error": f"Unknown source platform {platform!r}. "
                     f"Expected one of: {', '.join(attachments.SOURCE_PLATFORMS)}."
        }

    folder = _attachment_dir(note)
    created = not folder.exists()
    folder.mkdir(parents=True, exist_ok=True)
    (folder / attachments.EXTRACTED_DIRNAME).mkdir(exist_ok=True)

    manifest = attachments.read_manifest(folder)
    if not manifest:
        manifest = attachments.new_manifest(
            tender_id=args.tender_id,
            note_stem=note.stem,
            source_platform=platform,
        )
        attachments.write_manifest(folder, manifest)

    if not getattr(args, "no_reveal", False):
        _reveal_in_file_manager(folder)

    return {
        # ABSOLUTE, unlike promote's relative_to(PROJECT_ROOT). This path exists
        # to be pasted into a file manager or a shell, so it has to stand alone.
        "attachment_folder": str(folder.resolve()),
        "created": created,
        "tender_id": args.tender_id,
        "note": str(note.relative_to(PROJECT_ROOT)),
        "source_platform": manifest.get("source_platform"),
        "next": "Drop the files from MERX/Ariba into that folder, then run "
                "list-attachments to extract them.",
    }


def cmd_list_attachments(args) -> dict:
    """
    List a tender's documents, extracting anything new or changed first.

    This is where the directory is diffed against the manifest — there is no
    watcher and no daemon, so detection happens on the next call.
    """
    note = _note_for_tender(args.tender_id)
    if note is None:
        return {"error": f"No tender note for {args.tender_id}."}

    folder = _attachment_dir(note)
    if not folder.is_dir():
        return {
            "error": f"No attachment folder for {args.tender_id}. Create one "
                     f"with: attach {args.tender_id} --platform merx"
        }

    outcome = attachments.refresh(folder)
    manifest = outcome["manifest"]
    return {
        "tender_id": args.tender_id,
        "attachment_folder": str(folder.resolve()),
        "source_platform": manifest.get("source_platform"),
        "retrieved_at": manifest.get("retrieved_at"),
        "files": manifest.get("files", []),
        "added": outcome["added"],
        # An amended document re-dropped under the same name. Surfaced at the
        # top level because stale text that looks current is the failure mode
        # the hashes exist to catch.
        "changed": outcome["changed"],
        "removed": outcome["removed"],
        "warnings": outcome["warnings"],
    }


def cmd_read_attachment(args) -> dict:
    """
    Read a window of one document's extracted text.

    Paginated, never one blob: a 40-page RFP must not arrive as a single return
    value. Offsets and limits are in LINES of the extracted text.
    """
    note = _note_for_tender(args.tender_id)
    if note is None:
        return {"error": f"No tender note for {args.tender_id}."}

    folder = _attachment_dir(note)
    if not folder.is_dir():
        return {"error": f"No attachment folder for {args.tender_id}."}

    source = folder / args.filename
    if not source.is_file():
        return {"error": f"No such document: {args.filename}"}

    # Re-hash only the file being served. A full rescan is what
    # list-attachments is for; this is the narrow guarantee that a read never
    # returns text that no longer matches the document on disk.
    manifest = attachments.read_manifest(folder)
    record = attachments.find_record(manifest, args.filename)
    if record is None or record.get("sha256") != attachments.sha256_file(source):
        manifest = attachments.refresh(folder)["manifest"]
        record = attachments.find_record(manifest, args.filename)

    if record is None:
        return {"error": f"{args.filename} is not in the manifest."}

    status = record.get("extraction_status")
    if status != attachments.STATUS_EXTRACTED:
        return {
            "error": f"No extracted text for {args.filename} "
                     f"(extraction_status: {status}).",
            "extraction_status": status,
            "status_note": record.get("status_note"),
        }

    text_path = folder / record["extracted_path"]
    if not text_path.exists():
        return {"error": f"Extracted text missing for {args.filename}."}

    lines = text_path.read_text(encoding="utf-8").splitlines()
    offset = max(int(getattr(args, "offset", 0) or 0), 0)
    limit = min(max(int(getattr(args, "limit", 400) or 400), 1), 2000)
    window = lines[offset:offset + limit]

    return {
        "tender_id": args.tender_id,
        "filename": args.filename,
        "offset": offset,
        "limit": limit,
        "total_lines": len(lines),
        "lines_returned": len(window),
        "eof": offset + len(window) >= len(lines),
        "page_count": record.get("page_count"),
        "sha256": record.get("sha256"),
        "text": "\n".join(window),
    }


def cmd_contracts_intel(args) -> dict:
    """
    Competitive intelligence from the proactive-disclosure contracts DB.

    Pure SQLite. Deliberately does NOT touch ChromaDB, so it responds in
    milliseconds regardless of embedding-model state. Aggregates per contract
    family (procurement id) using each family's highest recorded value, which
    approximates 'current value including amendments'.
    """
    import sqlite3

    db = PROJECT_ROOT / "data" / "contracts.db"
    if not db.exists():
        return {"error": "Contracts DB not built. Run: python scripts/contracts_ingest.py"}

    con = sqlite3.connect(db)
    meta = dict(con.execute("SELECT key, value FROM meta").fetchall())
    like = f"%{args.query.lower()}%"

    # Same department identifier as every other signal tool. The contracts file
    # stores the CKAN slug, not a canonical key, so the key is translated
    # through the crosswalk and matched on the slug — an ID join, never a
    # comparison of display names.
    dept = org_resolve.resolve_department_arg(getattr(args, "department", None))
    where = ["(lower(description) LIKE ? OR lower(matched_terms) LIKE ?)"]
    params: list = [like, like]
    slugs = org_resolve.department_scope(dept)["contract_slugs"] if dept else []
    if dept:
        if not slugs:
            return {"query": args.query, "department": dept, "families": 0,
                    "note": f"{dept!r} publishes no contracts under its own slug, "
                            "so this dataset cannot answer for it. That is a real "
                            "fact about the organization, not a build failure."}
        where.append(f"owner_org IN ({','.join('?' * len(slugs))})")
        params += slugs

    families = con.execute(f"""
        SELECT family_id, vendor, vendor_norm, org, MAX(value) AS v,
               MAX(contract_date) AS d, MAX(period_end) AS pe, description
        FROM contracts
        WHERE {' AND '.join(where)}
        GROUP BY family_id
    """, params).fetchall()
    con.close()

    if not families:
        return {
            "query": args.query,
            "department": dept,
            "as_of": meta.get("ingest_date"),
            "families": 0,
            "note": "No matches. Try a broader term; matching is substring over descriptions.",
        }

    values = sorted(f[4] for f in families if f[4] and f[4] > 0)
    vendor_totals: dict[str, float] = {}
    org_counts: dict[str, int] = {}
    for _fam, _vendor, vnorm, org, v, *_rest in families:
        if vnorm:
            vendor_totals[vnorm] = vendor_totals.get(vnorm, 0) + (v or 0)
        if org:
            org_counts[org] = org_counts.get(org, 0) + 1

    recent = sorted(families, key=lambda f: f[5] or "", reverse=True)[:5]
    return {
        "query": args.query,
        "department": dept,
        "as_of": meta.get("ingest_date"),
        "window_years": meta.get("window_years"),
        "families": len(families),
        "total_value": round(sum(values), 0),
        "median_value": values[len(values) // 2] if values else 0,
        "top_vendors": [
            {"vendor": v, "total_value": round(t, 0)}
            for v, t in sorted(vendor_totals.items(), key=lambda x: -x[1])[:8]
        ],
        "top_departments": [
            {"org": o, "contract_families": n}
            for o, n in sorted(org_counts.items(), key=lambda x: -x[1])[:8]
        ],
        "recent_examples": [
            {"vendor": f[1], "org": f[3], "value": f[4],
             "contract_date": f[5], "period_end": f[6],
             "description": (f[7] or "")[:180]}
            for f in recent
        ],
        "caveats": "Unaudited data; vendor names lightly normalized (suffix/"
                   "punctuation only, not fuzzy-matched); families partially "
                   "outside the filter window may be incomplete.",
    }


def cmd_program_signals(args) -> dict:
    """
    Surface government programs showing operational pressure — the pre-RFP intent
    signal from Departmental Plans / Results.

    Ranks by single-year pressure_score (semantic strain in the department's own
    words, minus accounting noise), because the variance_explanation field is
    populated sparsely across years — most programs are explained in only some
    of their years — so requiring a multi-year chronic pattern threw away the
    real single-year signals (named IT modernizations, backlogs, failed
    recruitment) and kept boilerplate that happened to repeat.

    BUT chronic strain is the stronger signal in principle (a multi-year pattern
    is what draws the OAG audits, committee grillings, and media pressure that
    force a department to procure a fix). So where a program DOES have multiple
    scored years, we surface its year-by-year trail and a `multi_year` flag, so
    Claude can SEE chronicity when the data supports it — without requiring it.
    True chronic-pattern detection wants the OAG / committee data (which
    explicitly documents "this has failed for N years"); that's a documented
    future source, and the natural home for the "impending scrutiny" signal.

    Read the variance prose and judge IT-relevance — programs aren't
    capability-tagged, so an IT modernization can live inside any department.
    A lead list, not a forecast.

    Filters:
      --department SUBSTR : restrict to matching departments
      --min-score FLOAT   : only programs above this pressure_score (default 0)
      --include-internal  : include Internal Services (excluded by default —
                            back-office strain isn't program-delivery opportunity)
      --limit N           : how many to return (default 25)
    """
    import sqlite3

    db = PROJECT_ROOT / "data" / "plans.db"
    if not db.exists():
        return {"error": "Plans DB not built. Run: python scripts/plans_ingest.py"}

    con = sqlite3.connect(db)
    meta = dict(con.execute("SELECT key, value FROM meta").fetchall())

    # No score floor by default: real IT leads can score slightly negative
    # (a great lead phrased as a budget variance sits between the poles), so a
    # floor at 0 hid genuine signal. Rank by score, let --limit control volume;
    # --min-score is available to filter when wanted.
    min_score = getattr(args, "min_score", None)
    # Internal Services now INCLUDED by default: in this dataset departmental IT
    # spend is booked under Internal Services, so excluding it hid the exact
    # IT-modernization programs an IT firm wants. Use --exclude-internal to drop
    # it. Claude should still distinguish vendor-addressable IT modernization
    # from union-locked operational delivery when reading Internal Services rows.
    exclude_internal = getattr(args, "exclude_internal", False)

    where = ["intent_score IS NOT NULL"]
    params: list = []
    if min_score is not None:
        where.append("intent_score >= ?")
        params.append(min_score)
    # The plans file keys on the Infobase organization_id, which is stable
    # across rebrands — the reason the crosswalk carries it. Filtering on that
    # id rather than the organization name means a department that was renamed
    # mid-series still returns its whole history.
    dept = org_resolve.resolve_department_arg(getattr(args, "department", None))
    if dept:
        plan_ids = org_resolve.department_scope(dept)["plan_ids"]
        if not plan_ids:
            con.close()
            return {"department": dept, "programs": 0,
                    "note": f"{dept!r} files no departmental plans, so this "
                            "dataset cannot answer for it. Some organizations "
                            "publish contracts but no plans; that is a fact "
                            "about them, not a build failure."}
        where.append(f"organization_id IN ({','.join('?' * len(plan_ids))})")
        params += plan_ids
    if exclude_internal:
        where.append("lower(program_name) NOT LIKE '%internal service%'")

    limit = getattr(args, "limit", None) or 25
    rows = con.execute(f"""
        SELECT year, organization, program_name, core_responsibility,
               intent_score, planning_explanation,
               pressure_score, variance_explanation,
               planned_spending, actual_spending
        FROM programs
        WHERE {' AND '.join(where)}
        ORDER BY intent_score DESC
        LIMIT ?
    """, params + [limit]).fetchall()

    if not rows:
        # "Nothing above the floor" and "nothing scored at all" are different
        # answers and this used to give the first for both. Ranking is on
        # intent_score, which requires planning_explanation — and 16 of 94
        # organizations, including DND, GAC, RCMP, CBSA and SSC, file none in
        # any year. For them no floor is low enough, and saying so is the
        # difference between a tunable filter and a structural absence.
        scope_where, scope_params = "", []
        if dept:
            plan_ids = org_resolve.department_scope(dept)["plan_ids"]
            scope_where = f" WHERE organization_id IN ({','.join('?' * len(plan_ids))})"
            scope_params = plan_ids
        total, n_intent, n_pressure = con.execute(
            f"SELECT COUNT(*), SUM(intent_score IS NOT NULL), "
            f"SUM(pressure_score IS NOT NULL) FROM programs{scope_where}",
            scope_params).fetchone()
        con.close()

        if not n_intent:
            note = (f"No intent-scored programs{f' for {dept!r}' if dept else ''}: "
                    f"{total} program rows on file, 0 carrying a "
                    f"planning_explanation to score, {n_pressure or 0} carrying a "
                    "variance_explanation and scored for retrospective strain "
                    "instead. Lowering --min-score cannot help — there is nothing "
                    "on this axis to filter. The `dossier` command reads the "
                    "strain rows and the program inventory for exactly this case.")
        else:
            note = (f"{n_intent} intent-scored programs exist"
                    f"{f' for {dept!r}' if dept else ''} but none clear the "
                    f"current floor of {min_score}. Lower --min-score.")
        return {
            "as_of": meta.get("ingest_date"),
            "programs": 0,
            "intent_scored_rows": n_intent or 0,
            "pressure_scored_rows": n_pressure or 0,
            "note": note,
        }

    results = []
    for (year, org, prog, core, iscore, pe, pscore, ve, planned, actual) in rows:
        over = None
        if planned and actual and planned > 0:
            over = round((actual - planned) / planned * 100, 1)

        # Pull the program's intent-scored trail across years (context).
        trail = con.execute("""
            SELECT year, intent_score, planning_explanation
            FROM programs
            WHERE organization = ? AND program_name = ?
                  AND intent_score IS NOT NULL
            ORDER BY year
        """, (org, prog)).fetchall()

        other_years = []
        for (yr, sc, tpe) in trail:
            if yr == year:
                continue
            other_years.append({
                "year": yr, "intent_score": sc,
                "planning_explanation": (tpe or "")[:300],
            })

        results.append({
            "year": year,
            "department": org,
            "program": prog,
            "core_responsibility": core,
            "intent_score": iscore,
            "planning_explanation": pe,        # forward-looking: what they PLAN
            "pressure_score": pscore,          # retrospective strain (context)
            "variance_explanation": ve,
            "pct_over_plan": over,
            "multi_year": len(trail) > 1,
            "other_scored_years": other_years,
        })

    con.close()
    return {
        "as_of": meta.get("ingest_date"),
        "intent_scored_total": meta.get("intent_scored"),
        "returned": len(results),
        "filters": {"department": dept, "min_score": min_score,
                    "internal_services": "excluded" if exclude_internal else "included"},
        "programs": results,
        "how_to_read": "Ranked by intent_score — FORWARD-LOOKING modernization "
                       "intent from each program's planning_explanation (what the "
                       "department PLANS to spend on: modernize, replace, migrate, "
                       "build), scored toward IT/modernization language and away "
                       "from routine-operations noise. This is the pre-RFP signal: "
                       "a stated plan to modernize a system is a conversation to "
                       "open before the RFP. pressure_score/variance_explanation "
                       "are secondary context (retrospective strain); a program "
                       "that BOTH struggled and plans to modernize is strongest. "
                       "Internal Services is included by default (that's where "
                       "departmental IT is booked) — but distinguish vendor-buildable "
                       "IT modernization from union-locked operational delivery. "
                       "Judge IT-relevance and credibility (a 25-person firm isn't "
                       "a prime on a $90M program). A lead list, not a forecast.",
    }


def cmd_oag_signals(args) -> dict:
    """
    Surface OAG performance audits (and committee hearings) touching IT/systems,
    ranked by IT-relevance — the independent-scrutiny pre-RFP signal.

    Where contracts_intel says WHAT was bought, expiring_contracts says WHAT is
    up for renewal, and program-signals says what a department PLANS, this says
    what an independent authority has PUBLICLY found them failing at. "The AG
    flagged your processing backlog in 2023" is the most citable pre-RFP opener
    of all, and OAG/committee scrutiny is what forces a department to procure.

    CONVERGENCE is the point: filter by --department to cross-reference against
    program-signals (same dept planning to modernize?) and contracts-intel /
    expiring-contracts (who holds their IT work, expiring when?). When OAG +
    plans + an expiring contract point at the same department, that is the
    strongest pre-RFP case — and a live tender from that department should rank
    higher because of it.

    Read each audit and judge IT-relevance + whether the finding is a real
    opportunity. A lead list, not a forecast.

    Filters:
      --department SUBSTR : restrict to audits of matching departments
      --min-score FLOAT   : only audits with it_score at/above this (default: none)
      --doc-type TYPE     : performance_audit | committee_hearing | special_examination
      --since YYYY        : only audits from this year onward
      --limit N           : how many to return (default 20)
    """
    import sqlite3

    db = PROJECT_ROOT / "data" / "oag.db"
    if not db.exists():
        return {"error": "OAG DB not built. Run: python scripts/oag_ingest.py"}

    con = sqlite3.connect(db)
    meta = dict(con.execute("SELECT key, value FROM meta").fetchall())

    where, params = ["a.it_score IS NOT NULL"], []
    # A canonical key from org_aliases.yaml, joined on — never a substring of a
    # display name. "Immigration and Refugee Board" is an independent tribunal
    # and must not answer to IRCC; that refusal is written into the registry and
    # only an exact identifier honours it.
    dept = org_resolve.resolve_department_arg(getattr(args, "department", None))
    if dept:
        where.append("EXISTS (SELECT 1 FROM audit_departments d "
                     "WHERE d.oag_id = a.oag_id AND d.dept_key = ?)")
        params.append(dept)
    # Direct findings only — drop departments a briefing package inherited from
    # a multi-report hearing, which are real but much weaker evidence.
    if getattr(args, "direct_only", False):
        where.append("EXISTS (SELECT 1 FROM audit_departments d "
                     "WHERE d.oag_id = a.oag_id AND d.method = 'direct')")
    vendor = getattr(args, "vendor", None)
    if vendor:
        # Deliberately independent of --department: an audit into a supplier is
        # competitive intelligence whichever departments it touches, and the two
        # that exist are unattributable to any single one.
        where.append("lower(a.vendor_focus) LIKE ?")
        params.append(f"%{vendor.lower()}%")
    min_score = getattr(args, "min_score", None)
    if min_score is not None:
        where.append("a.it_score >= ?")
        params.append(min_score)
    doc_type = getattr(args, "doc_type", None)
    if doc_type:
        where.append("a.doc_type = ?")
        params.append(doc_type)
    since = getattr(args, "since", None)
    if since:
        where.append("a.year >= ?")
        params.append(since)

    limit = getattr(args, "limit", None) or 20
    rows = con.execute(f"""
        SELECT a.oag_id, a.year, a.doc_type, a.title, a.description,
               a.it_score, a.html_url, a.pdf_url,
               a.attribution_status, a.vendor_focus
        FROM audits a
        WHERE {' AND '.join(where)}
        ORDER BY a.it_score DESC, a.year DESC
        LIMIT ?
    """, params + [limit]).fetchall()

    # Departments per audit, split by how they were reached. A dossier shows the
    # direct findings; inherited ones are the same audit seen through a hearing
    # agenda, and a department reached through a 5-report bundle is weaker
    # evidence than one named in the audit itself.
    edges: dict[str, dict] = {}
    if rows:
        marks = ",".join("?" * len(rows))
        for oag_id, key, method, evidence, n_parents in con.execute(
            f"SELECT oag_id, dept_key, method, evidence, n_parent_reports "
            f"FROM audit_departments WHERE oag_id IN ({marks}) "
            f"ORDER BY method, dept_key", [r[0] for r in rows]
        ):
            slot = edges.setdefault(oag_id, {"direct": [], "inherited": []})
            if method == "direct":
                slot["direct"].append(key)
            else:
                slot["inherited"].append(
                    {"department": key, "via": method,
                     "reports_in_hearing": n_parents, "evidence": evidence})
    con.close()

    if not rows:
        return {"as_of": meta.get("ingest_date"), "audits": 0,
                "note": "No audits matched. Loosen --min-score/--department/"
                        "--since, or drop --direct-only."}

    results = []
    for (oag_id, year, dt, title, desc, score, html, pdf, status, vendor_focus) in rows:
        got = edges.get(oag_id, {"direct": [], "inherited": []})
        entry = {
            "year": year,
            "doc_type": dt,
            "departments": got["direct"],
            "title": title,
            "description": (desc or "")[:500],
            "it_score": score,
            "report_url": html or pdf,
        }
        if got["inherited"]:
            entry["inherited_departments"] = got["inherited"]
        if not got["direct"] and not got["inherited"]:
            entry["no_department_because"] = status
        if vendor_focus:
            entry["vendor_focus"] = vendor_focus
        results.append(entry)

    return {
        "as_of": meta.get("ingest_date"),
        "audit_count_total": meta.get("audit_count"),
        "coverage": {
            "federal_audits": f"{meta.get('federal_audits_resolved')}"
                              f"/{meta.get('federal_audits')} attributed directly",
            "committee_briefings": f"{meta.get('briefings_resolved')}"
                                   f"/{meta.get('briefings')} inherited from a report",
            "note": "Reported separately on purpose. A federal audit that names "
                    "its own departments and a briefing package that inherits "
                    "them are different evidence at different strength, and most "
                    "of this corpus correctly has no federal department at all.",
        },
        "returned": len(results),
        "filters": {"department": dept, "min_score": min_score,
                    "doc_type": doc_type, "since": since,
                    "vendor": vendor, "direct_only": getattr(args, "direct_only", False)},
        "audits": results,
        "how_to_read": "Ranked by it_score — how strongly the audit reads as "
                       "IT/systems/digital (vs financial/environmental/benefits), "
                       "scored semantically. doc_type distinguishes a performance_"
                       "audit (the AG's finding) from a committee_hearing (the "
                       "scrutiny materialized before PACP/OGGO). The pre-RFP power "
                       "is CONVERGENCE: take the department, then check program-"
                       "signals (are they planning to modernize the same thing?) "
                       "and expiring-contracts (who holds it, expiring when?). When "
                       "OAG + plans + contract all align on one department, that's "
                       "your strongest lead — and any live tender from them should "
                       "rank higher. Read the report_url for citable specifics. "
                       "A lead list, not a forecast.",
    }


def _lobbying_rows(con, where: list, params: list, limit: int) -> tuple[list, dict, dict]:
    """
    Fetch matching communications and their two child tables in three queries.

    Per-communication follow-ups would be a query per row; the office holders
    and subjects come back in one pass each, keyed by comlog_id.
    """
    rows = con.execute(f"""
        SELECT c.comlog_id, c.comm_date, c.client_name, c.client_norm,
               c.reg_type_label, c.registrant_first, c.registrant_last,
               c.posted_date, c.client_num
        FROM communications c
        WHERE {' AND '.join(where)}
        ORDER BY c.comm_date DESC
        LIMIT ?
    """, params + [limit]).fetchall()
    if not rows:
        return [], {}, {}

    marks = ",".join("?" * len(rows))
    ids = [r[0] for r in rows]
    dpohs: dict[int, list] = {}
    for comlog, last, first, title, institution, kind, key in con.execute(
        f"SELECT comlog_id, dpoh_last, dpoh_first, dpoh_title, institution, "
        f"institution_kind, dept_key FROM communication_dpohs "
        f"WHERE comlog_id IN ({marks}) ORDER BY institution, dpoh_last", ids
    ):
        entry = {"name": " ".join(x for x in (first, last) if x),
                 "title": title, "institution": institution, "kind": kind}
        if key:
            entry["department"] = key
        dpohs.setdefault(comlog, []).append(entry)

    subjects: dict[int, list] = {}
    for comlog, subject, other in con.execute(
        f"SELECT comlog_id, subject, other_subject FROM communication_subjects "
        f"WHERE comlog_id IN ({marks}) ORDER BY subject", ids
    ):
        subjects.setdefault(comlog, []).append(
            f"{subject}: {other}" if other else subject)
    return rows, dpohs, subjects


def _produced_by(command: str, db: Path, meta: dict) -> dict:
    """
    Name the path that produced a number, so a reader can tell a tool answer
    from something typed by hand.

    Every figure in a briefing is supposed to be reproducible by re-running one
    command. That guarantee is invisible in the output, which is how it gets
    broken: when this command was too slow to finish, its numbers were replaced
    with equivalent hand-written SQL against the same database and nothing on
    the page recorded the difference. The figures happened to be right; nobody
    reading the file could have known either way, and the next substitution
    might not be.

    So the answer carries its own attribution. A number quoted with a
    `produced_by` is one somebody else can re-derive; a number without one has
    no standing, and the briefing skill treats it as unciteable.
    """
    return {
        "command": command,
        "database": str(db.relative_to(PROJECT_ROOT)),
        "source_sha256": (meta.get("source_sha256") or "")[:12],
        "ingest_date": meta.get("ingest_date"),
        "rule": (
            "Quote this beside any figure taken from this result. If this "
            "command cannot complete, the briefing SAYS SO and the section "
            "goes without the number — it is never backfilled from an ad-hoc "
            "query, however obviously correct that query looks."
        ),
    }


def _subject_coverage(con, matched_where: list, matched_params: list) -> dict:
    """
    The date range a subject-filtered answer ACTUALLY describes, read off the
    data rather than off the window parameter.

    `window.latest` is the newest communication in the database. It is not
    necessarily the newest one that carries a subject code, and when those two
    dates diverge every --subject filter answers about the coded period while
    appearing to answer about the window.

    That happened. On the archive acquired 2026-08-24 the two were twenty-three
    months apart — subjects stopped at 2024-09-30 while communications ran to
    2026-08-21, leaving 72,341 rows (65% of the windowed database) invisible to
    any subject filter, and a briefing quoted the resulting counts as current.
    The cause was an ingest that read Communication_SubjectMattersExport.csv and
    not Communication_SubjectMatterDetailsExport.csv, which carries the same
    vocabulary for every communication after that date; lobbying_ingest.py now
    reads both, and a healthy build reports `state: complete`.

    This block stays regardless, because the fix and the guard answer different
    questions. The fix made today's data whole. The guard is what will say so
    the next time the Office changes the export and nobody notices for a month.

    That is the same failure the corpus provenance block exists to prevent: a
    number that is correct about its rows and wrong about its period, presented
    with a date that belongs to something else. So this is derived the same way
    — measured from the rows, reported next to the parameter it contradicts,
    and carrying a `state` a caller can branch on rather than prose it has to
    parse.

    `coded_*` describes the subject vocabulary across the whole database.
    `matched_*` describes the specific result being returned. They are separate
    because a department can be quiet inside a period that is fully coded, and
    that is a real finding, where a period that is not coded at all is not.
    """
    coded_earliest, coded_latest, coded_n = con.execute(
        "SELECT MIN(c.comm_date), MAX(c.comm_date), COUNT(DISTINCT c.comlog_id) "
        "FROM communications c JOIN communication_subjects s "
        "ON s.comlog_id = c.comlog_id").fetchone()
    db_earliest, db_latest, db_n = con.execute(
        "SELECT MIN(comm_date), MAX(comm_date), COUNT(*) FROM communications"
    ).fetchone()
    matched_earliest, matched_latest = con.execute(
        f"SELECT MIN(c.comm_date), MAX(c.comm_date) FROM communications c "
        f"WHERE {' AND '.join(matched_where)}", matched_params).fetchone()

    uncoded = (db_n or 0) - (coded_n or 0)
    truncated = bool(coded_latest and db_latest and coded_latest < db_latest)

    cov = {
        "effective_earliest": coded_earliest,
        "effective_latest": coded_latest,
        "matched_earliest": matched_earliest,
        "matched_latest": matched_latest,
        "communications_with_subject": coded_n,
        "communications_without_subject": uncoded,
        "state": "truncated" if truncated else "complete",
    }
    if truncated:
        pct = round(100 * uncoded / db_n) if db_n else 0
        cov["reading"] = (
            f"SUBJECT CODES STOP AT {coded_latest}. This result describes "
            f"{coded_earliest} to {coded_latest}, NOT the window's "
            f"{db_earliest} to {db_latest}. The archive carries no subject row "
            f"for any communication after {coded_latest}, so {uncoded:,} "
            f"communications ({pct}% of the database) are invisible to any "
            f"--subject filter. A count from this result is a count for the "
            f"coded period only — quote that period beside it, and never read "
            f"it as current. This is missing CODING, not missing activity. "
            f"Check first whether the ingest is reading every subject member of "
            f"the archive (this exact symptom was once "
            f"Communication_SubjectMatterDetailsExport.csv going unread, not a "
            f"defect at the Office); run without --subject meanwhile, to see "
            f"whether the department is still being met."
        )
    else:
        cov["reading"] = (
            f"Subject coding reaches {coded_latest}, the newest communication "
            f"in the database, so this result describes the full window."
        )
    return cov


def cmd_lobbying_signals(args) -> dict:
    """
    Who has been in the room with a department, about what, and when — the
    earliest signal in the corpus.

    Where oag-signals says what an independent authority found a department
    failing at and program-signals says what it plans, this says who has been
    talking to it while the requirement was still being decided. A monthly
    communication report is filed under the Lobbying Act when a lobbyist has an
    arranged oral communication with a designated public office holder, and
    "Government Procurement" is one of the 54 subject matters they file under.

    THIS IS EVIDENCE OF PRESENCE, NOT OF INFLUENCE, and the distinction is not
    a disclaimer — it governs how the output may be used. Filing is what
    COMPLIANCE looks like: the firms here are the ones following the rules, and
    a meeting is not a finding about anybody. What the record supports is that a
    department has been hearing from a particular set of firms on a particular
    subject, with a citable date. Never write it up as anything stronger.

    Read it two ways. Down the client column it is competitive intelligence —
    who is cultivating the department you are about to bid into, and for how
    long. Down the department column it is a pre-RFP signal: a department taking
    procurement meetings on a capability, whose plan says it intends to
    modernize that capability and whose incumbent contract expires next year, is
    the convergence case the dossier exists to show.

    Filters:
      --department KEY  : meetings with that department's office holders
      --subject STR     : one of the filed subject matters (see --list-subjects)
      --client SUBSTR   : client organization name contains this
      --vendor NAME     : client matched the way contracts.db matches vendors,
                          so a contract holder and a lobbying client compare
                          equal without the caller normalizing by hand
      --since YYYY-MM-DD: only communications on or after this date
      --limit N         : how many to return (default 25)
    """
    import sqlite3

    db = PROJECT_ROOT / "data" / "lobbying.db"
    if not db.exists():
        return _lobbying_not_built()

    con = sqlite3.connect(db)
    meta = dict(con.execute("SELECT key, value FROM meta").fetchall())

    if getattr(args, "list_subjects", False):
        subjects = [
            {"subject": s, "communications": n}
            for s, n in con.execute(
                "SELECT subject, COUNT(DISTINCT comlog_id) c "
                "FROM communication_subjects GROUP BY 1 ORDER BY c DESC")
        ]
        con.close()
        return {"as_of": meta.get("ingest_date"), "subjects": subjects,
                "note": "The filed subject matter is a controlled list chosen "
                        "by the filer. 'Government Procurement' is the direct "
                        "signal; 'Industry', 'Science and Technology' and "
                        "'Telecommunications' carry IT requirements too."}

    where, params = ["1=1"], []
    dept = org_resolve.resolve_department_arg(getattr(args, "department", None))
    if dept:
        # A canonical key, joined on — never a substring of the published
        # institution name. dept_key is set only where naming a department is a
        # true statement, so this filter cannot pick up the 31k parliamentary
        # rows or a Crown corporation that happens to share a word.
        where.append("EXISTS (SELECT 1 FROM communication_dpohs d "
                     "WHERE d.comlog_id = c.comlog_id AND d.dept_key = ?)")
        params.append(dept)
    subject = getattr(args, "subject", None)
    if subject:
        where.append("EXISTS (SELECT 1 FROM communication_subjects s "
                     "WHERE s.comlog_id = c.comlog_id AND lower(s.subject) = ?)")
        params.append(subject.lower())
    client = getattr(args, "client", None)
    if client:
        where.append("lower(c.client_name) LIKE ?")
        params.append(f"%{client.lower()}%")
    vendor = getattr(args, "vendor", None)
    if vendor:
        # The contracts-side normalizer, applied to the query rather than
        # re-implemented, so "HP Canada Co." and "HP CANADA LTD" reach the same
        # client_norm the ingest stored.
        where.append("c.client_norm = ?")
        params.append(normalize_vendor(vendor))
    since = getattr(args, "since", None)
    if since:
        where.append("c.comm_date >= ?")
        params.append(since)

    limit = getattr(args, "limit", None) or 25
    rows, dpohs, subjects = _lobbying_rows(con, where, params, limit)

    # Counted over everything that MATCHED, not over the page returned: "who
    # turns up most" is the question this data answers best, and answering it
    # from 25 rows would just describe the sort order.
    totals = con.execute(
        f"SELECT COUNT(*) FROM communications c WHERE {' AND '.join(where)}",
        params).fetchone()[0]
    top_clients = [
        {"client": name, "communications": n}
        for name, n in con.execute(
            f"SELECT c.client_name, COUNT(*) n FROM communications c "
            f"WHERE {' AND '.join(where)} AND c.client_name != '' "
            f"GROUP BY c.client_norm ORDER BY n DESC LIMIT 15", params)
    ]
    top_departments = [
        {"department": key, "communications": n}
        for key, n in con.execute(
            f"SELECT d.dept_key, COUNT(DISTINCT c.comlog_id) n "
            f"FROM communications c JOIN communication_dpohs d "
            f"ON d.comlog_id = c.comlog_id "
            f"WHERE {' AND '.join(where)} AND d.dept_key IS NOT NULL "
            f"GROUP BY 1 ORDER BY n DESC LIMIT 15", params)
    ]
    # Derived before the connection closes, and attached to every subject-filtered
    # answer including the empty one — an empty result is exactly where a missing
    # period is most likely to be misread as a quiet department.
    coverage = _subject_coverage(con, where, params) if subject else None
    con.close()

    if not rows:
        empty = {"as_of": meta.get("ingest_date"), "communications": 0,
                 "produced_by": _produced_by("lobbying-signals", db, meta),
                 "window": _lobbying_window(meta),
                 "note": "Nothing matched. Check --subject against "
                         "--list-subjects, and remember the database holds only "
                         f"the window described above ({meta.get('window_cutoff')} "
                         "onward) — an older meeting is not absent, it is out of "
                         "scope."}
        if coverage:
            empty["subject_coverage"] = coverage
        return empty

    results = []
    for (comlog, date, client_name, _norm, reg_type,
         reg_first, reg_last, posted, client_num) in rows:
        results.append({
            "date": date,
            "client": client_name,
            "lobbyist": " ".join(x for x in (reg_first, reg_last) if x),
            "registration": reg_type,
            "subjects": subjects.get(comlog, []),
            "office_holders": dpohs.get(comlog, []),
            "posted": posted,
            # The registry's own composite identifier, not a URL. The published
            # data dictionary defines the communication number as the client
            # number followed by the comlog id, and that is the string the
            # Registry of Lobbyists search takes. No deep link is offered
            # because the registry has none: its report views are reached
            # through a session-scoped search form, so any per-communication
            # URL built here would be invented rather than cited.
            "communication_number": f"{client_num}-{comlog}"
            if client_num is not None else str(comlog),
        })

    out = {
        "as_of": meta.get("ingest_date"),
        "produced_by": _produced_by("lobbying-signals", db, meta),
        "window": _lobbying_window(meta),
        "matched": totals,
        "returned": len(results),
        "filters": {"department": dept, "subject": subject, "client": client,
                    "vendor": vendor, "since": since},
        "top_clients": top_clients,
        "top_departments": top_departments,
        "communications": results,
        "how_to_read": (
            "Each row is one disclosed meeting: who met which office holders, "
            "on what date, filed under which subject matters. This is evidence "
            "of PRESENCE and nothing more — filing is what compliance with the "
            "Lobbying Act looks like, and no row here is a finding about "
            "anyone. Do not write it up as influence over a procurement. "
            "top_clients answers the question the page cannot: who turns up "
            "most across everything that matched. The registration type "
            "matters — consultant means a hired lobbyist and the client is who "
            "paid, in_house means the organization's own staff. Coverage is "
            "partial by law: only ARRANGED ORAL communications with designated "
            "office holders are reportable, so absence is not evidence that "
            "nobody was in the room. communication_number is the registry's "
            "own identifier, for looking a record up at "
            "lobbycanada.gc.ca — cite it rather than a URL. "
            "The pre-RFP read is convergence — take a "
            "department that is taking procurement meetings and check "
            "program-signals and expiring-contracts for the same capability."
        ),
    }
    if coverage:
        # Placed after `window` in reading order but asserted here so it cannot
        # be dropped by an edit to the literal above: a subject-filtered result
        # without its effective range is the defect this block exists to close.
        out["subject_coverage"] = coverage
        if coverage["state"] == "truncated":
            out["window"] = dict(out["window"])
            out["window"]["note"] = (
                "SUPERSEDED FOR THIS RESULT by subject_coverage — `latest` "
                f"below is the newest communication in the database "
                f"({coverage['effective_latest']} is the newest one carrying a "
                "subject code). A --subject filter cannot see past that date. "
                + out["window"].get("note", "")
            )
    return out


def cmd_registrations_signals(args) -> dict:
    """
    Who was REGISTERED to lobby a department, as of a given date.

    The standing declaration behind the meetings. `lobbying-signals` says a
    communication happened; this says who was on the record as working that
    department at a point in time, and which registration version says so.

    AS-OF IS MANDATORY AND HAS NO DEFAULT. Not an oversight — the whole reason
    this database stores versions is that 53% of amended registrations change
    their institution list, and a default meaning "latest" is how flattened
    behaviour returns through a different door. A caller who never thinks about
    time would silently get the present tense and build a time-ordered claim on
    it. Wanting current state is fine; saying so is the requirement. Pass
    --as-of today for that.

    EVERY ROW CARRIES ITS VERSION so a claim can be traced back to the exact
    registration version it rests on: `reg_id` is the version id, `reg_num`
    ends in the version sequence, and `effective`/`ends` are the window that
    made it match. A briefing line citing this data can name the version.

    A null `ends` means still in force, measured rather than assumed — every
    open-ended version in the archive is the latest-effective of its own chain.

    Filters:
      --as-of YYYY-MM-DD : REQUIRED. 'today' is accepted as an explicit request
                           for current state.
      --department KEY   : registrations naming that department
      --client SUBSTR    : client organization name contains this
      --vendor NAME      : client matched the way contracts.db matches vendors
      --limit N          : how many to return (default 25)
    """
    import sqlite3

    # The as-of contract is checked BEFORE the database, deliberately. A caller
    # who omitted it has made a malformed request, and telling them the
    # database is missing sends them to install something instead of fixing
    # their query — and worse, hides the requirement until the day the data
    # exists, which is the day a defaulted answer would start being believed.
    as_of = getattr(args, "as_of", None)
    if not as_of:
        return {
            "error": "--as-of is required and has no default.",
            "why": "This database stores every registration version because "
                   "53% of amended registrations change which departments they "
                   "name. A default meaning 'latest' would quietly answer a "
                   "time-ordered question with present-tense data.",
            "how": "Pass --as-of YYYY-MM-DD for a point in time, or "
                   "--as-of today if you genuinely want current state.",
        }
    if str(as_of).lower() in ("today", "now", "current"):
        as_of = datetime.now().strftime("%Y-%m-%d")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(as_of)):
        return {"error": f"--as-of must be YYYY-MM-DD or 'today', got {as_of!r}"}

    db = PROJECT_ROOT / "data" / "registrations.db"
    if not db.exists():
        return {
            "error": "Registrations DB not built.",
            "state": "source_archive_not_acquired",
            "why": "lobbycanada.gc.ca returns 403 to automated clients "
                   "(Cloudflare challenge), so the archive is downloaded by "
                   "hand. This is NOT an absence of data.",
            "download": "https://lobbycanada.gc.ca/media/zwcjycef/"
                        "registrations_enregistrements_ocl_cal.zip",
            "save_to": "data/source/lobbying/"
                       "registrations_enregistrements_ocl_cal.zip",
            "then_run": "python scripts/registrations_ingest.py",
        }

    con = sqlite3.connect(db)
    meta = dict(con.execute("SELECT key, value FROM meta").fetchall())

    where = ["1=1"]
    params: dict = {"as_of": as_of}
    dept = org_resolve.resolve_department_arg(getattr(args, "department", None))
    if dept:
        where.append("EXISTS (SELECT 1 FROM registration_institutions i "
                     "WHERE i.reg_id = v.reg_id AND i.dept_key = :dept)")
        params["dept"] = dept
    client = getattr(args, "client", None)
    if client:
        where.append("lower(v.client_name) LIKE :client")
        params["client"] = f"%{client.lower()}%"
    vendor = getattr(args, "vendor", None)
    if vendor:
        where.append("v.client_norm = :vendor")
        params["vendor"] = normalize_vendor(vendor)

    limit = getattr(args, "limit", None) or 25
    params["limit"] = limit
    sql_where = " AND ".join(where) + " AND " + _registrations_as_of_clause()
    rows = con.execute(f"""
        SELECT v.reg_id, v.reg_num, v.version_seq, v.effective_date, v.end_date,
               v.client_name, v.reg_type_label, v.registrant_first,
               v.registrant_last, v.firm_name
        FROM registration_versions v
        WHERE {sql_where}
        ORDER BY v.effective_date DESC
        LIMIT :limit
    """, params).fetchall()

    total = con.execute(
        f"SELECT COUNT(*) FROM registration_versions v WHERE {sql_where}",
        params).fetchone()[0]

    departments = [
        {"department": key, "registrations": n}
        for key, n in con.execute(f"""
            SELECT i.dept_key, COUNT(DISTINCT v.reg_base) n
            FROM registration_versions v
            JOIN registration_institutions i ON i.reg_id = v.reg_id
            WHERE {sql_where} AND i.dept_key IS NOT NULL
            GROUP BY 1 ORDER BY n DESC LIMIT 15
        """, params)
    ]

    results = []
    for (reg_id, reg_num, seq, eff, end, client_name, reg_type,
         r_first, r_last, firm) in rows:
        insts = [
            r[0] for r in con.execute(
                "SELECT DISTINCT dept_key FROM registration_institutions "
                "WHERE reg_id = ? AND dept_key IS NOT NULL ORDER BY 1", (reg_id,))
        ]
        results.append({
            "client": client_name,
            "registration": reg_num,
            # The version this claim rests on. Anything time-ordered built from
            # this row must cite it — that is what makes the claim checkable.
            "version_id": reg_id,
            "version_seq": seq,
            "effective": eff,
            "ends": end,
            "still_in_force": end is None,
            "registration_type": reg_type,
            "registrant": " ".join(x for x in (r_first, r_last) if x),
            "firm": firm,
            "departments": insts,
        })
    con.close()

    return {
        "as_of": as_of,
        "ingest_date": meta.get("ingest_date"),
        "source_sha256": meta.get("source_sha256"),
        "matched_versions": total,
        "returned": len(results),
        "filters": {"department": dept, "client": client, "vendor": vendor},
        "departments": departments,
        "registrations": results,
        "corpus": {
            "versions": meta.get("versions"),
            "registrations": meta.get("registrations"),
            "open_ended_versions": meta.get("open_ended_versions"),
            "chain_breaks_midchain": meta.get("chain_breaks_midchain"),
        },
        "how_to_read": (
            f"Every row was in force on {as_of} — that date selected them, and "
            "a different date returns a different answer, which is the point. "
            "version_id is the registration version the row rests on; cite it "
            "for any claim about a point in time. A null `ends` means still in "
            "force. This is a DECLARATION of intent to lobby, not evidence any "
            "meeting occurred — pair it with lobbying-signals for that — and "
            "like all of this data it is presence, never influence."
        ),
    }


def _registrations_as_of_clause() -> str:
    """The one as-of predicate, imported from the ingest so there is a single
    definition of the half-open interval rather than a copy that drifts."""
    sys.path.insert(0, str(Path(__file__).parent))
    from registrations_ingest import as_of_clause
    return as_of_clause("v")


def _lobbying_not_built() -> dict:
    """
    The "no database" response, written to be ACTED ON rather than relayed.

    This is the one signal layer whose source cannot be fetched by anything in
    this project, so the error a reader gets has to carry the whole remedy: the
    two official URLs, where the file goes, and the command. An error that only
    says "not built" gets repeated to the user verbatim and leaves them to go
    find all three.

    `state` is deliberately not "no data". A caller that cannot distinguish an
    unbuilt layer from an empty one will eventually report that no lobbying
    happened, which is false — the registry is published and updated weekly.
    """
    return {
        "error": "Lobbying DB not built.",
        "state": "source_archive_not_acquired",
        "why": "lobbycanada.gc.ca returns 403 to automated clients "
               "(Cloudflare challenge), so the archive is downloaded by hand. "
               "This is NOT an absence of data — the registry is published, "
               "current and updated weekly.",
        "download": {
            "communications (required)":
                "https://lobbycanada.gc.ca/media/mqbbmaqk/"
                "communications_ocl_cal.zip",
            "registrations (optional, not yet ingested)":
                "https://lobbycanada.gc.ca/media/zwcjycef/"
                "registrations_enregistrements_ocl_cal.zip",
        },
        "save_to": "data/source/lobbying/communications_ocl_cal.zip",
        "then_run": "python scripts/lobbying_ingest.py",
        "tell_the_user": (
            "Give them the communications URL and the save path, say in one "
            "line that Cloudflare blocks automated download, and offer to run "
            "the ingest once the file is in place. Do not report this as 'no "
            "lobbying data available'."
        ),
    }


def _lobbying_window(meta: dict) -> dict:
    """What the database covers, so a zero can be read correctly."""
    return {
        "communications_in_db": meta.get("communications"),
        "communications_published": meta.get("communications_published"),
        "window_years": meta.get("window_years") or "all",
        "earliest": meta.get("earliest_communication"),
        "latest": meta.get("latest_communication"),
        "note": "The database is windowed at ingest. A department with no rows "
                "may simply have had no reportable meetings inside the window.",
    }


def cmd_resolve_department(args) -> dict:
    """
    What does this string mean, as a department identifier?

    Exists so a caller can check an identifier WITHOUT running a query and
    guessing from an empty result whether the department has no signal or the
    name was simply wrong. Those are very different answers and every other tool
    would otherwise conflate them.

    Resolution is registry-only and exact after normalization — the same rule
    all four signal tools use, including the `not:` exclusions, so
    "Immigration and Refugee Board" resolves to the tribunal and never to IRCC.
    """
    resolver = org_resolve.default_resolver()
    key, how, found = org_resolve.resolve_identifier(args.name)

    if how == "ambiguous":
        return {
            "query": args.name,
            "resolved": None,
            "names": found,
            "note": "This string names more than one organization — a real "
                    "multi-department entity value, and a meaningless filter. "
                    "Pass one of the keys listed in `names`.",
        }
    if not key:
        return {
            "query": args.name,
            "resolved": None,
            "closest": resolver.suggest(args.name),
            "note": "Not a known organization. Pass a canonical key from "
                    "vault/crosswalk/org_aliases.yaml or an organization's "
                    "registered name; fragments and substrings are refused on "
                    "purpose, because that is how one department's name lands "
                    "on another.",
        }

    entry = resolver.aliases[key]
    scope = org_resolve.department_scope(key)
    return {
        "query": args.name,
        "resolved": key,
        # exact = the string is a registry name or key. variant = it resolved
        # only after the parenthetical acronym tail was stripped, which is what
        # every entity field in this project looks like.
        "matched_via": how,
        "name": resolver.display_name(key),
        "also_known_as": entry.get("observed_names") or [],
        "never_matches": entry.get("not") or [],
        "sources": {
            "oag": "audit_departments.dept_key",
            "plans": scope["plan_ids"] or "files no departmental plans",
            "contracts": scope["contract_slugs"] or "publishes no contracts",
        },
        "note": entry.get("note"),
    }


def cmd_expiring_contracts(args) -> dict:
    """
    Surface contracts whose delivery period ends inside a window — the highest
    public signal for a future re-procurement.

    The thesis: a contract in our competency space that expires in 6-24 months
    is a near-certain future RFP. We already know the incumbent, the value, the
    department, and (from the description) roughly what the work is — 12-18
    months before the government formally asks for it. That head start is where
    proactive business development actually happens.

    This is deliberately NOT prediction. It's a lead list. It surfaces
    candidates; a human (with Claude's help via the opportunity-shaping lens)
    decides which are worth a proactive conversation.

    Window comes from --months-min / --months-max (default 6-24). Ordered by
    expiry soonest-first within the window, then by value, so the most urgent
    and most consequential surface at the top.

    Notes on signal quality, stated honestly:
    - Aggregates per family; a family's latest period_end is its true expiry
      (amendments extend contracts, so we take the MAX).
    - Standing offers / supply arrangements often have far-future or open end
      dates and are re-competed differently than project contracts; they'll
      appear but are weaker signals — the description usually reveals which.
    - Value is directional (see contracts-intel caveats).
    """
    import sqlite3
    from datetime import datetime, timedelta

    db = PROJECT_ROOT / "data" / "contracts.db"
    if not db.exists():
        return {"error": "Contracts DB not built. Run: python scripts/contracts_ingest.py"}

    months_min = getattr(args, "months_min", None) or 6
    months_max = getattr(args, "months_max", None) or 24
    today = datetime.now()
    lo = (today + timedelta(days=30 * months_min)).strftime("%Y-%m-%d")
    hi = (today + timedelta(days=30 * months_max)).strftime("%Y-%m-%d")

    # Minimum value: CLI flag overrides profile; profile default is
    # expiry_min_value in the frontmatter (0 = no floor). This keeps the
    # threshold tunable per company profile, like every other filter.
    min_value = getattr(args, "min_value", None)
    if min_value is None:
        min_value = _profile_expiry_min_value()

    con = sqlite3.connect(db)
    meta = dict(con.execute("SELECT key, value FROM meta").fetchall())

    # Same department identifier as the other three signal tools, translated to
    # the CKAN slug the contracts file actually stores.
    dept = org_resolve.resolve_department_arg(getattr(args, "department", None))
    where = ["period_end != ''"]
    params: list = []
    if dept:
        slugs = org_resolve.department_scope(dept)["contract_slugs"]
        if not slugs:
            con.close()
            return {"department": dept, "expiring": 0,
                    "note": f"{dept!r} publishes no contracts under its own slug, "
                            "so this dataset cannot answer for it."}
        where.append(f"owner_org IN ({','.join('?' * len(slugs))})")
        params += slugs

    # Per family: latest expiry, incumbent, department, value, description.
    # Filter families whose MAX(period_end) lands in the window AND whose
    # value clears the floor.
    rows = con.execute(f"""
        SELECT family_id,
               MAX(period_end)   AS expiry,
               vendor,
               vendor_norm,
               org,
               MAX(value)        AS v,
               MAX(contract_date) AS awarded,
               description
        FROM contracts
        WHERE {' AND '.join(where)}
        GROUP BY family_id
        HAVING expiry >= ? AND expiry <= ? AND v >= ?
        ORDER BY expiry ASC, v DESC
    """, params + [lo, hi, min_value]).fetchall()
    con.close()

    if not rows:
        return {
            "window": f"{months_min}-{months_max} months out ({lo} to {hi})",
            "min_value": min_value,
            "department": dept,
            "as_of": meta.get("ingest_date"),
            "expiring": 0,
            "note": "No contracts expiring in this window above the value floor. "
                    "Lower expiry_min_value in the profile (or --min-value), or "
                    "widen --months-max.",
        }

    def months_until(expiry: str) -> int:
        try:
            d = datetime.strptime(expiry, "%Y-%m-%d")
            return round((d - today).days / 30)
        except ValueError:
            return -1

    results = [
        {
            "incumbent": r[2],
            "incumbent_norm": r[3],
            "department": r[4],
            "expiry": r[1],
            "months_until_expiry": months_until(r[1]),
            "value": r[5],
            "awarded": r[6],
            "description": (r[7] or "")[:220],
        }
        for r in rows
    ]

    return {
        "window": f"{months_min}-{months_max} months out ({lo} to {hi})",
        "min_value": min_value,
        "department": dept,
        "as_of": meta.get("ingest_date"),
        "expiring": len(results),
        "contracts": results[:40],  # cap so the agent isn't flooded
        "truncated": len(results) > 40,
        "how_to_read": "Each row is a near-certain future re-procurement. The "
                       "incumbent is who you'd displace; the description hints at "
                       "the work. Not every row is worth pursuing — this is a lead "
                       "list for human judgment, not a forecast.",
    }


# ---------------------------------------------------------------------------
# The department dossier — convergence
# ---------------------------------------------------------------------------
# One query, five sources. This ASSEMBLES; it does not score. There is
# deliberately no convergence number and no cross-signal ranking: weighting five
# incommensurable signals into one figure buries the reasoning that makes the
# dossier worth reading, and the reader is a model that can weigh them itself.
# Nothing here may combine a value from one section with a value from another.

_TENDERS_CSV = PROJECT_ROOT / ".cache" / "tenders.csv"

# The CanadaBuys open-notice feed. The dossier reads the raw feed rather than
# the profile-filtered corpus, so it keeps its own column map; the ingest reads
# most of these into ChromaDB but not the notice URL.
_TENDER_COLS = {
    "tender_id": "referenceNumber-numeroReference",
    "title": "title-titre-eng",
    "closing": "tenderClosingDate-appelOffresDateCloture",
    "contracting": "contractingEntityName-nomEntitContractante-eng",
    "end_user": "endUserEntitiesName-nomEntitesUtilisateurFinal-eng",
    "notice_type": "noticeType-avisType-eng",
    "procurement_category": "procurementCategory-categorieApprovisionnement",
    # Read for classification only, never rendered — the dossier lists notices,
    # it doesn't reproduce them. Without it classify_notice cannot see the
    # prose signals, and the dossier would call a TBIPS call-up open work.
    "description": "tenderDescription-descriptionAppelOffres-eng",
    "url": "noticeURL-URLavis-eng",
}

# Single definition, in scripts/ingest/, imported here. It used to be declared in both
# modules with the same value and the same comment — which is fine until one of
# them is tuned and the corpus and the dossier start disagreeing about what a
# placeholder date is.
from ingest import SENTINEL_HORIZON_YEARS as _SENTINEL_HORIZON_YEARS  # noqa: E402


def _profile_expiry_min_value() -> float:
    """Value floor for expiring contracts, from the company profile frontmatter."""
    try:
        import sys as _sys
        _sys.path.insert(0, str(Path(__file__).parent))
        from ingest import parse_profile
        return parse_profile(PROFILE).get("expiry_min_value", 0)
    except Exception:
        return 0


def _profile_imminence_threshold() -> int:
    """
    Days-until-close below which a notice is `imminent`, from the profile.

    Read at call time, mirroring _profile_expiry_min_value: the window is
    derived per query rather than stored, so retuning the threshold takes effect
    on the next command with no re-ingest.
    """
    try:
        import sys as _sys
        _sys.path.insert(0, str(Path(__file__).parent))
        from ingest import parse_profile
        return int(parse_profile(PROFILE).get("imminent_within_days", 5))
    except Exception:
        return 5


def _window_fields(meta: dict) -> dict:
    """
    `closing_window` and `days_until_close` for a corpus notice, computed now.

    Deliberately not stored in ChromaDB. Both values depend on today's date, and
    a corpus is read for up to a week after it is built — a stored `imminent`
    would still say `imminent` after the notice had closed. Computing here means
    a briefing written three days after an ingest sees the truth on the day it
    is written, including notices that expired in between.
    """
    from ingest import closing_window
    window, days = closing_window(
        meta.get("closing_date", ""), _profile_imminence_threshold()
    )
    return {"closing_window": window, "days_until_close": days}


def _months_until(date_str: str, today: datetime) -> int | None:
    try:
        return round((datetime.strptime(date_str, "%Y-%m-%d") - today).days / 30)
    except (ValueError, TypeError):
        return None


def _entity_keys(value: str) -> dict[str, str]:
    """
    Canonical keys named by one tender entity field, each with the verbatim
    phrase that produced it.

    Delegates to ingest.entity_org_keys, which is the single definition — the
    ingest filter uses the same resolution to decide whether a notice is
    federal, and two copies of that would eventually disagree about which
    organizations exist. The matched phrase rides along because an attribution
    that cannot say which string produced it is not reviewable, which is the
    rule OrgResolver.scan already follows for audits.
    """
    return _entity_org_keys(value)


def _entity_attribution(end_user_value, contracting_value) -> dict[str, dict]:
    """
    Every canonical department a notice attributes to, and how each was reached.

    ONE definition, shared by the dossier's notice section and by promote. The
    dossier asks "does this notice reach the department I am looking at"; promote
    asks "which departments does this notice name" — the same resolution read from
    two directions, and two copies of it would eventually disagree about which
    field carried an attribution.

    END-USER KEYS FIRST, and the order is load-bearing rather than cosmetic: it is
    the order they are written into a promoted tender file, and the department that
    needs the work outranks the one that happens to be buying. A contracting entity
    only ever ADDS a department the end-user field did not already name; it never
    overwrites one, because that would downgrade a stated customer to a buyer of
    record.

    The `entity_source` values are the distinction the whole thing exists for.
    Federal IT is routinely bought by SSC or PSPC on behalf of the department that
    actually needs it, so 'they are buying this' and 'they are the buyer of record
    and nobody said who it is for' have to stay apart. The matched phrase rides
    along as `entity_evidence` because an attribution that cannot say which string
    produced it is not reviewable.
    """
    end_user = _entity_keys(end_user_value)
    contracting = _entity_keys(contracting_value)

    attribution: dict[str, dict] = {}
    for key, evidence in end_user.items():
        attribution[key] = {"entity_source": "end_user", "entity_evidence": evidence}

    # Whether the end-user field named ANYONE is a property of the notice, not of
    # the department being asked about — so it is decided once, here, rather than
    # per lookup.
    fallback = ("contracting_entity_end_user_unstated" if not end_user
                else "contracting_entity_end_user_names_others")
    for key, evidence in contracting.items():
        attribution.setdefault(
            key, {"entity_source": fallback, "entity_evidence": evidence}
        )
    return attribution


def _profile_corpus_ids() -> set[str]:
    """
    Tender ids in the profile-filtered ChromaDB corpus, read straight out of
    Chroma's SQLite rather than through the client, so the dossier never pays
    for the embedding model just to answer "is this one we already track?".
    """
    import sqlite3
    db = DB_PATH / "chroma.sqlite3"
    if not db.exists():
        return set()
    con = sqlite3.connect(db)
    try:
        return {
            r[0] for r in con.execute(
                "SELECT string_value FROM embedding_metadata WHERE key = 'tender_id'"
            ) if r[0]
        }
    except sqlite3.OperationalError:
        return set()
    finally:
        con.close()


def _contract_rows_by_slug(slugs: list) -> dict[str, int]:
    """
    Rows each CKAN slug contributes to this extract, zeros included.

    Zeros are the point. A slug that contributes nothing has to say so rather
    than be missing from a dict, because a fold whose contribution is invisible
    is a fold the reader cannot check — pptc is folded into ircc and contributes
    0 rows here, and 'absent from the mapping' and 'present but empty' are
    different facts about the same department.
    """
    import sqlite3
    counts = {s: 0 for s in slugs}
    db = PROJECT_ROOT / "data" / "contracts.db"
    if not slugs or not db.exists():
        return counts
    con = sqlite3.connect(db)
    try:
        for slug, n in con.execute(
            f"SELECT owner_org, COUNT(*) FROM contracts "
            f"WHERE owner_org IN ({','.join('?' * len(slugs))}) GROUP BY owner_org",
            slugs
        ):
            counts[slug] = n
    except sqlite3.OperationalError:
        pass
    finally:
        con.close()
    return counts


def _dossier_identity(dept: str, rows_by_slug: dict) -> dict:
    """
    Who this department is, and every caveat the registry records about the
    records folded into it.

    The notes are read from org_aliases.yaml verbatim and never restated here.
    A predecessor/successor/absorbed edge means this dossier is mixing one
    organization's records into another's, and the registry is where the
    explanation of that lives; duplicating it in code is how the two drift
    apart. Where the registry records no note, that is said plainly rather than
    papered over with generated prose.
    """
    import crosswalk
    aliases = crosswalk.load_aliases()
    entry = aliases.get(dept, {})
    resolver = org_resolve.default_resolver()

    folds = []
    for item in entry.get("ckan") or []:
        relation = item.get("relation") or "same"
        slug = item.get("slug")
        fold = {"slug": slug, "relation": relation,
                "contract_rows_in_extract": rows_by_slug.get(slug, 0)}
        if relation != "same":
            fold["note"] = item.get("note") or (
                f"The registry records this as a {relation} of {dept} but carries "
                "no note explaining it. Treat the fold as unexplained rather than "
                "assuming it is routine."
            )
            fold["note_source"] = ("registry" if item.get("note")
                                   else "none recorded in registry")
            if not fold["contract_rows_in_extract"]:
                fold["contribution"] = (
                    "Contributes 0 rows to this extract, so nothing in the "
                    "contracts section below comes from it. The caveat still "
                    "stands for any wider query — it is empty here, not absent "
                    "from the department.")
        folds.append(fold)

    return {
        "canonical_key": dept,
        "name": resolver.display_name(dept),
        "registry_note": entry.get("note"),
        "also_known_as": entry.get("observed_names") or [],
        "never_matches": entry.get("not") or [],
        "records_folded_in": folds,
    }


def _dossier_audits(dept: str, limit: int) -> dict:
    """
    What an independent authority has publicly found. Direct findings and
    bundle-attached edges are returned as two lists and are never interleaved:
    being named in the Auditor General's own finding and being cited inside a
    committee briefing package are different evidence at different strength,
    and a merged list silently promotes the weaker one.
    """
    import sqlite3
    db = PROJECT_ROOT / "data" / "oag.db"
    if not db.exists():
        return {"error": "OAG DB not built. Run: python scripts/oag_ingest.py"}

    con = sqlite3.connect(db)
    try:
        meta = dict(con.execute("SELECT key, value FROM meta").fetchall())
        rows = con.execute("""
            SELECT a.oag_id, a.year, a.doc_type, a.title, a.description,
                   a.it_score, a.html_url, d.method, d.evidence, d.n_parent_reports
            FROM audits a
            JOIN audit_departments d ON d.oag_id = a.oag_id
            WHERE d.dept_key = ?
            ORDER BY a.year DESC, a.it_score DESC
        """, (dept,)).fetchall()

        # The other departments named directly in the same audits. An audit
        # naming six departments is a different thing from one naming only this
        # department, and that is invisible without it.
        co_named: dict[str, list[str]] = {}
        for oag_id, *_ in rows:
            co_named[oag_id] = [
                r[0] for r in con.execute(
                    "SELECT DISTINCT dept_key FROM audit_departments "
                    "WHERE oag_id = ? AND method = 'direct' AND dept_key != ? "
                    "ORDER BY dept_key", (oag_id, dept))
            ]
    finally:
        con.close()

    direct, bundled = [], []
    for (oag_id, year, doc_type, title, desc, it_score,
         html_url, method, evidence, n_parents) in rows:
        item = {
            "year": year,
            "doc_type": doc_type,
            "title": title,
            "description": (desc or "")[:500],
            "it_score": it_score,
            # Every oag_id is the CKAN dataset uuid, so a working link can
            # always be built. Needed because html_url is dead for the 214
            # oag-bvg.gc.ca rows — the AG restructured its site and the old
            # deep links 404. Those are carried as text, never as a link.
            "source_url": f"https://open.canada.ca/data/dataset/{oag_id}",
        }
        if html_url and "oag-bvg.gc.ca" in html_url:
            item["report_url_dead"] = html_url
            item["link_note"] = ("oag-bvg.gc.ca deep links no longer resolve; "
                                 "use source_url. Kept for citation only.")
        elif html_url:
            item["report_url"] = html_url

        if method == "direct":
            item["also_named_in_this_audit"] = co_named.get(oag_id, [])
            direct.append(item)
        else:
            item["via"] = method
            item["parent_reports_in_bundle"] = n_parents
            item["evidence"] = evidence
            bundled.append(item)

    federal = int(meta.get("federal_audits") or 0)
    resolved = int(meta.get("federal_audits_resolved") or 0)
    section = {
        "direct_findings": direct[:limit],
        "bundle_attached": bundled[:limit],
        "direct_count": len(direct),
        "bundle_attached_count": len(bundled),
        "corpus": {
            "audits_total": meta.get("audit_count"),
            "federal_audits": federal,
            "federal_audits_attributed": resolved,
            "unattributed": federal - resolved if federal else None,
        },
    }
    if not direct and not bundled:
        section["state"] = "no_audits_attributed"
        section["note"] = (
            f"No audit in this corpus is attributed to {dept!r}. That is not the "
            f"same as a clean record: of {federal} federal audits, {resolved} were "
            f"attributed to a department and {federal - resolved} were not — an "
            "audit that names no department, or names a body outside the federal "
            "executive, resolves to nobody. The residual is where a missed finding "
            "would be hiding."
        )
    else:
        section["state"] = "attributed"
    section["how_to_read"] = (
        "direct_findings are audits that name this department in the finding "
        "itself. bundle_attached are committee briefing packages that cite a "
        "report naming it — real scrutiny, weaker evidence, and "
        "parent_reports_in_bundle says how many reports the package covered "
        "(the more it covered, the less it is about this department "
        "specifically). Never read the two as one list."
    )
    return section


def _dossier_plans(dept: str, plan_ids: list, limit: int) -> dict:
    """
    Forward-looking modernization intent. Intent and strain are returned as two
    separate blocks and are never merged into one ranked list: intent is a
    stated forward plan, strain is retrospective over-commitment, the magnitudes
    are not comparable, and blending them is exactly how variance-based ranking
    once scored real IT modernizations negative.
    """
    import sqlite3
    db = PROJECT_ROOT / "data" / "plans.db"
    if not db.exists():
        return {"error": "Plans DB not built. Run: python scripts/plans_ingest.py"}

    if not plan_ids:
        return {
            "state": "files_no_plans",
            "note": f"{dept!r} files no departmental plans at all — it has no "
                    "Infobase organization id, so this dataset has nothing to "
                    "say about it. A handful of organizations publish contracts "
                    "but file no plans. That is a fact about the organization, "
                    "not missing data.",
        }

    con = sqlite3.connect(db)
    placeholders = ",".join("?" * len(plan_ids))
    try:
        meta = dict(con.execute("SELECT key, value FROM meta").fetchall())
        totals = con.execute(f"""
            SELECT COUNT(*),
                   SUM(intent_score IS NOT NULL),
                   SUM(pressure_score IS NOT NULL),
                   MIN(year), MAX(year)
            FROM programs WHERE organization_id IN ({placeholders})
        """, plan_ids).fetchone()
        total_rows, n_intent, n_pressure, first_year, last_year = totals

        def scored_block(score_col: str, prose_col: str) -> list:
            """Top programs for the most recent year that has any scored row."""
            year = con.execute(f"""
                SELECT MAX(year) FROM programs
                WHERE organization_id IN ({placeholders}) AND {score_col} IS NOT NULL
            """, plan_ids).fetchone()[0]
            if year is None:
                return []
            rows = con.execute(f"""
                SELECT program_name, core_responsibility, {score_col}, {prose_col},
                       planned_spending
                FROM programs
                WHERE organization_id IN ({placeholders})
                      AND year = ? AND {score_col} IS NOT NULL
                ORDER BY {score_col} DESC
            """, plan_ids + [year]).fetchall()

            # Boilerplate: one sentence filed against N programs is one signal,
            # not N. Departments routinely paste a single FTE explanation across
            # every program in the inventory (384 such groups government-wide),
            # and rendering it N times manufactures a pattern that isn't there.
            spread = Counter(r[3] for r in rows)
            out, seen = [], set()
            for (prog, core, score, prose, planned) in rows:
                shared = spread[prose]
                if shared > 1 and prose in seen:
                    continue
                seen.add(prose)
                item = {
                    "year": year, "program": prog, "core_responsibility": core,
                    score_col: score, prose_col: prose,
                    "planned_spending": planned,
                }
                if shared > 1:
                    item["shared_with"] = shared
                    item["boilerplate_note"] = (
                        f"This same sentence is filed against {shared} programs "
                        f"in {year}. One replicated sentence is one signal, not "
                        f"{shared} — shown once, with the programs listed."
                    )
                    item["programs_sharing_it"] = [
                        r[0] for r in rows if r[3] == prose
                    ]
                out.append(item)
            return out[:limit]

        intent = scored_block("intent_score", "planning_explanation")
        strain = scored_block("pressure_score", "variance_explanation") if not intent else []

        inventory = [
            {"program": r[0], "core_responsibility": r[1], "planned_spending": r[2]}
            for r in con.execute(f"""
                SELECT program_name, core_responsibility, planned_spending
                FROM programs
                WHERE organization_id IN ({placeholders}) AND year = ?
                ORDER BY planned_spending DESC
            """, plan_ids + [last_year]).fetchall()
        ][:limit]
    finally:
        con.close()

    section = {
        "as_of": meta.get("ingest_date"),
        "years_on_file": f"{first_year}-{last_year}" if first_year else None,
        "program_rows": total_rows,
        "intent_scored_rows": n_intent,
        "pressure_scored_rows": n_pressure,
        "intent": intent,
        "inventory": inventory,
        "inventory_note": (
            f"Every program on the books in {last_year}, ordered by planned "
            "spending — the only ranking available when nothing is scored. "
            "Presence and budget, not intent."
        ),
    }

    if intent:
        section["state"] = "intent_scored"
    elif n_pressure:
        section["state"] = "no_intent_prose"
        section["strain"] = strain
        section["strain_note"] = (
            "RETROSPECTIVE, and not a plan. These score variance_explanation — "
            "the department explaining why actual spending or headcount diverged "
            "from what it planned. Read as evidence of over-commitment already "
            "incurred, never as stated forward intent, and never ranked against "
            "intent_score: the two measure different things on different scales."
        )
        section["note"] = _NO_INTENT_PROSE_NOTE.format(dept=dept, rows=total_rows,
                                                       pressure=n_pressure)
    elif total_rows:
        section["state"] = "no_prose_at_all"
        section["note"] = (
            f"{dept!r} files {total_rows} program rows but populates neither "
            "planning_explanation nor variance_explanation in any year, so "
            "nothing is scored on either axis. The spending and FTE figures are "
            "filed; the prose the signal is built from is not. Nothing here is "
            "readable as intent or strain — the inventory below is all this "
            "source has."
        )
    else:
        section["state"] = "no_program_rows"
        section["note"] = (f"{dept!r} has an Infobase id but no program rows in "
                           "this extract.")
    return section


# Stated once, because it is a property of the source rather than of any one
# department, and a reader meeting it on SSC needs to know that immediately.
_NO_INTENT_PROSE_NOTE = (
    "{dept!r} files {rows} program rows but zero planning_explanation in any "
    "year, so no program is intent-scored. This is a filing pattern in the "
    "source, not something odd about this department: 16 of 94 organizations "
    "file none at all, including DND, GAC, RCMP, CBSA and SSC — together 11.7% "
    "of planned spending. The field is an optional note explaining why planned "
    "spending or FTE figures move between years, not a general statement of "
    "intent, and a department with steady figures has nothing to file. The "
    "narrative Departmental Plan, where modernization commitments are actually "
    "written down, is a separate publication that is not in this dataset. "
    "{pressure} rows do carry variance_explanation and are scored for strain "
    "below."
)


def _dossier_contracts(dept: str, slugs: list, rows_by_slug: dict,
                       months_min: int, months_max: int,
                       min_value: float, limit: int) -> dict:
    """
    Who holds the work and when it runs out. The expiry timeline is the most
    actionable field in the dossier: a contract in our competency space ending
    in 6-24 months is a near-certain re-procurement with the incumbent, the
    value and the scope already public.

    Both halves aggregate per contract family taking the family's highest
    recorded value and latest period_end, matching contracts-intel and
    expiring-contracts exactly, so the dossier can never disagree with the
    standalone tools about the same department.
    """
    import sqlite3
    from datetime import timedelta
    db = PROJECT_ROOT / "data" / "contracts.db"
    if not db.exists():
        return {"error": "Contracts DB not built. Run: python scripts/contracts_ingest.py"}

    if not slugs:
        return {
            "state": "no_slug",
            "note": f"{dept!r} publishes no contracts under its own slug, so this "
                    "dataset cannot answer for it. Some organizations file plans "
                    "but disclose no contracts of their own. That is a fact about "
                    "the organization, not a build failure.",
        }

    today = datetime.now()
    lo = (today + timedelta(days=30 * months_min)).strftime("%Y-%m-%d")
    hi = (today + timedelta(days=30 * months_max)).strftime("%Y-%m-%d")
    placeholders = ",".join("?" * len(slugs))

    con = sqlite3.connect(db)
    try:
        meta = dict(con.execute("SELECT key, value FROM meta").fetchall())
        vendors = con.execute(f"""
            SELECT vendor_norm, COUNT(*) AS families, SUM(v) AS total
            FROM (SELECT family_id, vendor_norm, MAX(value) AS v
                  FROM contracts WHERE owner_org IN ({placeholders})
                  GROUP BY family_id, vendor_norm)
            WHERE vendor_norm IS NOT NULL
            GROUP BY vendor_norm
            ORDER BY total DESC
            LIMIT ?
        """, slugs + [limit]).fetchall()

        expiring = con.execute(f"""
            SELECT MAX(period_end) AS expiry, vendor, vendor_norm,
                   MAX(value) AS v, MAX(contract_date) AS awarded, description
            FROM contracts
            WHERE owner_org IN ({placeholders}) AND period_end != ''
            GROUP BY family_id
            HAVING expiry >= ? AND expiry <= ? AND v >= ?
            ORDER BY expiry ASC, v DESC
        """, slugs + [lo, hi, min_value]).fetchall()
    finally:
        con.close()

    rows_total = sum(rows_by_slug.values())
    if not rows_total:
        return {
            "state": "slug_but_no_rows",
            "slugs": slugs,
            "as_of": meta.get("ingest_date"),
            "note": f"{dept!r} owns {len(slugs)} CKAN slug(s) but has no rows in "
                    f"this extract, which covers the last "
                    f"{meta.get('window_years')} years filtered to IT and "
                    "telecom categories. The organization discloses contracts; "
                    "none of them are recent IT work.",
        }

    timeline = [
        {"incumbent": r[1], "incumbent_norm": r[2], "expiry": r[0],
         "months_until_expiry": _months_until(r[0], today),
         "value": r[3], "awarded": r[4], "description": (r[5] or "")[:220]}
        for r in expiring
    ]

    return {
        "state": "has_contracts",
        "as_of": meta.get("ingest_date"),
        "window_years": meta.get("window_years"),
        "slugs": slugs,
        "rows_by_slug": rows_by_slug,
        "top_vendors": [
            {"vendor": v, "contract_families": n, "total_value": round(t or 0)}
            for v, n, t in vendors
        ],
        "expiry_window": f"{months_min}-{months_max} months out ({lo} to {hi})",
        "expiry_min_value": min_value,
        "expiring_count": len(timeline),
        "expiry_timeline": timeline[:limit],
        "expiry_truncated": len(timeline) > limit,
        "caveats": "Unaudited; vendor names lightly normalized (suffix and "
                   "punctuation only, not fuzzy-matched), so one vendor may "
                   "appear under two spellings. Standing offers and supply "
                   "arrangements carry far-future end dates and are re-competed "
                   "differently — the description usually reveals which.",
    }


def _dossier_lobbying(dept: str, limit: int) -> dict:
    """
    Who has been in the room with this department, and on what subject.

    The earliest section, and the one most easily misread, so it returns two
    things rather than a list of meetings: WHO turns up (ranked by how many
    disclosed communications name this department) and WHAT they filed under.
    A dossier reader wants the pattern — this department has been hearing from
    these firms about procurement for two years — not twenty individual dates.

    Procurement-subject counts are reported separately from the total for the
    same reason the audit section splits direct from bundle-attached: a firm
    that meets a department about Health and a firm that meets it about
    Government Procurement are not the same signal, and one list merges them.
    """
    import sqlite3
    db = PROJECT_ROOT / "data" / "lobbying.db"
    if not db.exists():
        return _lobbying_not_built()

    con = sqlite3.connect(db)
    try:
        meta = dict(con.execute("SELECT key, value FROM meta").fetchall())
        # Both counts are COUNT(DISTINCT comlog_id), and the second one has to
        # be: the join to communication_dpohs emits a row per office holder, so
        # a meeting with three SSC officials appears three times. A SUM(CASE)
        # over that counts the duplicates and reports a procurement subset
        # LARGER than the total it is a subset of.
        clients = con.execute("""
            SELECT c.client_name, COUNT(DISTINCT c.comlog_id) n,
                   MIN(c.comm_date), MAX(c.comm_date),
                   COUNT(DISTINCT CASE WHEN EXISTS (
                       SELECT 1 FROM communication_subjects s
                       WHERE s.comlog_id = c.comlog_id
                         AND s.subject = 'Government Procurement')
                       THEN c.comlog_id END) procurement
            FROM communications c
            JOIN communication_dpohs d ON d.comlog_id = c.comlog_id
            WHERE d.dept_key = ? AND c.client_name != ''
            GROUP BY c.client_norm
            ORDER BY n DESC
            LIMIT ?
        """, (dept, limit)).fetchall()
        subjects = con.execute("""
            SELECT s.subject, COUNT(DISTINCT s.comlog_id) n
            FROM communication_subjects s
            JOIN communication_dpohs d ON d.comlog_id = s.comlog_id
            WHERE d.dept_key = ?
            GROUP BY 1 ORDER BY n DESC LIMIT ?
        """, (dept, limit)).fetchall()
        total = con.execute(
            "SELECT COUNT(DISTINCT d.comlog_id) FROM communication_dpohs d "
            "WHERE d.dept_key = ?", (dept,)).fetchone()[0]
    finally:
        con.close()

    section = {
        "communications": total,
        "top_clients": [
            {"client": name, "communications": n, "first": first, "latest": last,
             "on_government_procurement": proc}
            for name, n, first, last, proc in clients
        ],
        "subjects_filed": [{"subject": s, "communications": n} for s, n in subjects],
        "window": _lobbying_window(meta),
    }
    if not total:
        section["state"] = "no_communications_in_window"
        section["note"] = (
            f"No disclosed communication in the window names {dept!r}. That is "
            "not evidence that nobody met them: only ARRANGED ORAL "
            "communications with designated public office holders are "
            "reportable, and the database is windowed at ingest."
        )
    else:
        section["state"] = "present"
    section["how_to_read"] = (
        "Evidence of PRESENCE, never of influence, and never a finding about "
        "any firm named — filing these reports is what compliance with the "
        "Lobbying Act looks like. Read it as: this department has been hearing "
        "from these organizations, on these subjects, over this period. "
        "on_government_procurement is the subset filed under that subject "
        "specifically, which is the one that bears directly on a coming "
        "requirement. Cross-read against the plans and contracts sections: a "
        "firm meeting a department about procurement while that department "
        "plans to modernize a system it already holds the contract for is an "
        "incumbent defending a renewal."
    )
    return section


def _dossier_tenders(dept: str, limit: int) -> dict:
    """
    Open notices, if any — and the absence is the interesting case.

    A department with an audit finding, a stated plan, an expiring incumbent and
    NO open tender is the pre-RFP position this whole project exists to find:
    the work is coming and nobody has been asked yet. So this section is not
    required, carries no weight against the other three, and must read as a
    finding rather than as a failure when it is empty.

    Reads the full open-notice feed rather than the profile-filtered corpus,
    because "is this department in market right now" is a question about the
    department, not about our fit. Notices that ALSO cleared the profile filter
    are flagged, so the two questions stay separable.
    """
    import csv
    if not _TENDERS_CSV.exists():
        return {"state": "no_feed",
                "note": "No open-notice feed cached. Run: python scripts/ingest"}

    in_corpus = _profile_corpus_ids()
    today = datetime.now()
    horizon = today.replace(year=today.year + _SENTINEL_HORIZON_YEARS).strftime("%Y-%m-%d")
    today_str = today.strftime("%Y-%m-%d")

    notices, scanned, expired = [], 0, 0
    with open(_TENDERS_CSV, encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            scanned += 1
            # The distinction the whole section turns on — which field carried
            # the attribution — is decided by _entity_attribution, shared with
            # promote so a tender file and a dossier never disagree about it.
            attribution = _entity_attribution(
                row.get(_TENDER_COLS["end_user"]),
                row.get(_TENDER_COLS["contracting"]),
            )
            if dept not in attribution:
                continue
            source = attribution[dept]["entity_source"]
            evidence = attribution[dept]["entity_evidence"]
            end_user = [k for k, a in attribution.items()
                        if a["entity_source"] == "end_user"]

            closing = (row.get(_TENDER_COLS["closing"]) or "")[:10]
            item = {
                "tender_id": row.get(_TENDER_COLS["tender_id"]),
                "title": row.get(_TENDER_COLS["title"]),
                "notice_type": row.get(_TENDER_COLS["notice_type"]) or None,
                "entity_source": source,
                "entity_evidence": evidence,
                "in_profile_corpus": row.get(_TENDER_COLS["tender_id"]) in in_corpus,
                "url": (row.get(_TENDER_COLS["url"]) or "").strip() or None,
            }
            if not item["url"]:
                # Only 423 of 896 notices carry one, and they are the ones also
                # posted to a third-party site like MERX. A CanadaBuys-native
                # notice files no URL, so there is nothing to link to — say so
                # rather than let a null read as a broken field. No URL is
                # constructed: guessing one is how the dead oag-bvg.gc.ca links
                # in the audits section happened.
                item["url_note"] = ("No notice URL filed — search CanadaBuys by "
                                    "the reference number.")
            if source != "end_user":
                item["attribution_note"] = (
                    "Attributed via the contracting entity; the end user is "
                    "unstated." if source.endswith("unstated") else
                    "Attributed via the contracting entity, which is buying on "
                    "behalf of a different named end user.")
            if end_user and source != "end_user":
                item["end_user_named"] = sorted(end_user)

            # ONE definition, imported — not a second copy that agrees today.
            # The copy that used to live here substring-matched "supply
            # arrangement", which labelled all 54 "RFP against Supply
            # Arrangement" notices `qualification` when a call-up is the exact
            # opposite: real work, competed among vehicle holders. It also
            # labelled "Invitation to Qualify" as work. See classify_notice.
            item.update(_classify_notice(
                item["notice_type"],
                row.get(_TENDER_COLS["procurement_category"]),
                f"{row.get(_TENDER_COLS['title']) or ''} "
                f"{row.get(_TENDER_COLS['description']) or ''}",
            ))

            if not closing:
                item["closing_date"] = None
            elif closing > horizon:
                # Not a date. Standing arrangements park the field decades out.
                item["closing_date"] = None
                item["date_note"] = (
                    f"Filed as {closing} — a sentinel meaning the arrangement "
                    "has no real close, not a deadline.")
            elif closing < today_str:
                # The feed is 'open tender notices', but 55 of 896 rows carry a
                # closing date already past. Trust the date over the status.
                expired += 1
                continue
            else:
                item["closing_date"] = closing
                item["days_until_close"] = (
                    datetime.strptime(closing, "%Y-%m-%d") - today).days
            notices.append(item)

    notices.sort(key=lambda n: (n["closing_date"] is None, n["closing_date"] or ""))
    by_source = Counter(n["entity_source"] for n in notices)

    section = {
        "open_notices": len(notices),
        "notices": notices[:limit],
        "truncated": len(notices) > limit,
        "by_entity_source": dict(by_source),
        "in_profile_corpus": sum(1 for n in notices if n["in_profile_corpus"]),
        "feed_scanned": scanned,
        "dropped_already_closed": expired,
        # How old the feed is, on the same footing as the `as_of` the
        # contracts, plans and audits sections already carry — this section
        # was the only one reporting undated numbers.
        #
        # The mtime of the file just read, NOT the stamp in the corpus
        # metadata: this section deliberately bypasses ChromaDB and reads the
        # CSV directly, so it must report the provenance of what IT read. The
        # two can legitimately differ — a corpus built this morning off a feed
        # downloaded yesterday, then the feed re-downloaded since.
        "feed_downloaded_at": datetime.fromtimestamp(
            _TENDERS_CSV.stat().st_mtime).isoformat(timespec="seconds"),
    }
    if not notices:
        section["state"] = "none_open"
        section["note"] = (
            f"No open notice in the feed attributes to {dept!r}. This is a "
            "finding, not a gap — a department with an audit finding, a stated "
            "plan and an expiring incumbent but no open tender is the pre-RFP "
            "position worth acting on, because the work is coming and nobody "
            "has been asked yet. Read the other three sections on their own "
            "terms; nothing here weakens them."
        )
    else:
        section["state"] = "open_notices"
    section["how_to_read"] = (
        "entity_source says how the notice reached this department. 'end_user' "
        "means it named them as the customer. Anything else means they are the "
        "contracting entity — attributed via contracting entity, end user "
        "unstated — which for SSC and PSPC frequently means they are buying for "
        "somebody else entirely. in_profile_corpus marks the notices that also "
        "cleared our profile filter; the rest are the department being in "
        "market regardless of our fit."
    )
    return section


def cmd_department_dossier(args) -> dict:
    """
    Everything all five sources know about one department, in one query.

    The convergence view. Each signal is useful alone; the payoff is when they
    line up — the Auditor General flagged a department, its own plan says it
    intends to modernize that system, the incumbent's contract expires in five
    months, and the incumbent has been filing procurement meetings with them all
    year. That is about as strong a pre-RFP case as public data can produce, and
    a live tender from that department should be read in its light.

    This tool ASSEMBLES AND PRESENTS. It does not score. There is no convergence
    number, no weighting of the five signals into one figure, and no ranking of
    departments by it — deliberately. The five signals are incommensurable, any
    weighting would be invented, and a single number would hide the reasoning
    that makes the dossier worth reading in the first place. Claude reads the
    five sections and judges. That is the architecture.

    Sections are ordered by their own native logic and never against each other:
    audits by year, plans by intent within the most recent scored year,
    contracts by soonest expiry, lobbying by who appears most often, tenders by
    soonest close.

    THE LOBBYING SECTION IS THE ONE THAT CAN BE MISUSED. It records who has been
    in the room, which is exactly what makes it valuable and exactly what makes
    a careless reading defamatory. It supports "this department has been hearing
    from these firms about procurement" and never "this firm influenced a
    procurement." The section carries that constraint in its own how_to_read;
    it is repeated here because this is the tool that puts it next to four
    sources that DO support inference.

    TENDERS ARE NOT REQUIRED. The highest-value output of this tool is a
    department with an audit finding, a stated plan, an expiring incumbent and
    NO open tender.
    """
    dept = org_resolve.resolve_department_arg(
        getattr(args, "department", None), flag="dossier")
    if not dept:
        return {"error": "A department is required. Pass a canonical key from "
                         "vault/crosswalk/org_aliases.yaml or a registered name."}

    limit = getattr(args, "limit", None) or 10
    months_min = getattr(args, "months_min", None) or 6
    months_max = getattr(args, "months_max", None) or 24
    min_value = getattr(args, "min_value", None)
    if min_value is None:
        min_value = _profile_expiry_min_value()

    scope = org_resolve.department_scope(dept)
    rows_by_slug = _contract_rows_by_slug(scope["contract_slugs"])
    return {
        "department": dept,
        "generated": datetime.now().strftime("%Y-%m-%d"),
        "identity": _dossier_identity(dept, rows_by_slug),
        "audits": _dossier_audits(dept, limit),
        "plans": _dossier_plans(dept, scope["plan_ids"], limit),
        "contracts": _dossier_contracts(dept, scope["contract_slugs"], rows_by_slug,
                                        months_min, months_max, min_value, limit),
        "lobbying": _dossier_lobbying(dept, limit),
        "tenders": _dossier_tenders(dept, limit),
        "how_to_read": (
            "Five independent sources on one department. Read them together and "
            "form your own judgement — there is no score here on purpose. What "
            "you are looking for is agreement between sections: an audit finding "
            "and a plan and an expiring incumbent that are all about the same "
            "system. An empty section is information too, and each one says "
            "which kind of empty it is — no data, or no signal. The lobbying "
            "section is the exception to 'read them together': it is evidence "
            "of who was present, never of influence, and it may not be used to "
            "explain why any other section says what it says. Check "
            "identity.records_folded_in before quoting any total: where a "
            "predecessor or absorbed organization has been folded in, the "
            "registry's note says what the number actually covers."
        ),
    }


def cmd_list_parked(args) -> dict:
    """List all parked tenders with their revisit triggers."""
    if not PARKED.exists():
        return {"parked": []}
    files = sorted(PARKED.glob("*.md"))
    tenders = []
    for f in files:
        content = f.read_text(encoding="utf-8")
        # Frontmatter — same lightweight parse as list-watching
        fm_match = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
        fields = {}
        if fm_match:
            for line in fm_match.group(1).split("\n"):
                if ":" in line:
                    k, _, v = line.partition(":")
                    fields[k.strip()] = v.strip().strip('"')
        # Pull the most recent "Revisit when:" line from the body so Claude can
        # see at a glance what would unstick this tender
        revisit = ""
        for match in re.finditer(r"\*\*Revisit when:\*\*\s*(.+)", content):
            revisit = match.group(1).strip()
        tenders.append({
            "filename": f.name,
            "tender_id": fields.get("tender_id", ""),
            "title": fields.get("title", ""),
            "closing_date": fields.get("closing_date", ""),
            "revisit_when": revisit,
        })
    return {"parked": tenders}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _slugify(text: str, max_len: int = 80) -> str:
    slug = re.sub(r"[^\w\s-]", "", text.lower())
    slug = re.sub(r"[-\s]+", "-", slug).strip("-")
    return slug[:max_len] or "untitled"


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------

# ONE department identifier across all four signal tools. Inconsistency here
# would be its own bug: the entire point of these tools is cross-referencing a
# department between them, and that fails if each takes a different spelling.
_DEPT_HELP = (
    "Canonical key from vault/crosswalk/org_aliases.yaml (e.g. pspc, ircc, dnd) "
    "or an organization's registered name. Exact after normalization — "
    "substrings are refused, so one department's name cannot land on another. "
    "Use `resolve-department` to check a string first."
)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("search", help="Hybrid search the full corpus")
    s.add_argument("query")
    s.add_argument("--n", type=int, default=10)
    s.set_defaults(func=cmd_search)

    g = sub.add_parser("get", help="Full details for one tender")
    g.add_argument("tender_id")
    g.set_defaults(func=cmd_get)

    sim = sub.add_parser("similar", help="Find tenders similar to a given one")
    sim.add_argument("tender_id")
    sim.add_argument("--n", type=int, default=5)
    sim.set_defaults(func=cmd_similar)

    lc = sub.add_parser(
        "list-corpus",
        help="Every notice in the corpus by closing date — for reading it end to end",
    )
    lc.add_argument(
        "--window",
        choices=["closed", "imminent", "open", "standing", "unknown"],
        help="Only notices in this closing window (default: all)",
    )
    lc.set_defaults(func=cmd_list_corpus)

    lw = sub.add_parser("list-watching", help="List promoted tenders")
    lw.set_defaults(func=cmd_list_watching)

    lp = sub.add_parser("list-parked", help="List parked tenders with revisit triggers")
    lp.set_defaults(func=cmd_list_parked)

    ci = sub.add_parser(
        "contracts-intel",
        help="Who won similar contracts: vendors, departments, values (SQLite, instant)",
    )
    ci.add_argument("query")
    ci.add_argument("--department", help=_DEPT_HELP)
    ci.set_defaults(func=cmd_contracts_intel)

    ec = sub.add_parser(
        "expiring-contracts",
        help="Contracts expiring in a window — near-certain future re-procurements",
    )
    ec.add_argument("--months-min", type=int, default=6,
                    help="Earliest expiry, months from now (default 6)")
    ec.add_argument("--months-max", type=int, default=24,
                    help="Latest expiry, months from now (default 24)")
    ec.add_argument("--min-value", type=float, default=None,
                    help="Minimum contract value (overrides profile's "
                         "expiry_min_value; default reads from profile)")
    ec.add_argument("--department", help=_DEPT_HELP)
    ec.set_defaults(func=cmd_expiring_contracts)

    ps = sub.add_parser(
        "program-signals",
        help="Programs showing operational pressure (pre-RFP intent signal)",
    )
    ps.add_argument("--department", help=_DEPT_HELP)
    ps.add_argument("--min-score", type=float, default=None,
                    help="Only programs with intent_score at or above this "
                         "(default: no floor, since real leads can score slightly negative)")
    ps.add_argument("--exclude-internal", action="store_true",
                    help="Exclude Internal Services programs (included by default)")
    ps.add_argument("--limit", type=int, default=25, help="How many to return (default 25)")
    ps.set_defaults(func=cmd_program_signals)

    og = sub.add_parser(
        "oag-signals",
        help="OAG audits touching IT/systems — independent-scrutiny pre-RFP signal",
    )
    og.add_argument("--department", help=_DEPT_HELP)
    og.add_argument("--vendor",
                    help="Audits INTO a named supplier (e.g. GCStrategies, "
                         "McKinsey). Independent of --department: these audits "
                         "span too many departments to attach to one, but an "
                         "audit of a firm you bid against is intelligence anyway")
    og.add_argument("--direct-only", action="store_true",
                    help="Only departments named in the audit itself, dropping "
                         "those a briefing package inherited from a hearing")
    og.add_argument("--min-score", type=float, default=None,
                    help="Only audits with it_score at or above this")
    og.add_argument("--doc-type", choices=["performance_audit", "committee_hearing",
                                           "special_examination", "financial_audit"],
                    help="Restrict to one document type")
    og.add_argument("--since", type=int, default=None, help="Only audits from this year onward")
    og.add_argument("--limit", type=int, default=20, help="How many to return (default 20)")
    og.set_defaults(func=cmd_oag_signals)

    lb = sub.add_parser(
        "lobbying-signals",
        help="Who has been meeting a department, on what subject — the "
             "earliest pre-RFP signal. Presence, never influence",
    )
    lb.add_argument("--department", help=_DEPT_HELP)
    lb.add_argument("--subject",
                    help="One filed subject matter, e.g. 'Government "
                         "Procurement'. See --list-subjects for the list")
    lb.add_argument("--client", help="Client organization name contains this")
    lb.add_argument("--vendor",
                    help="Client matched the way contracts.db matches vendors, "
                         "so an incumbent and a lobbying client compare equal")
    lb.add_argument("--since", help="Only communications on or after YYYY-MM-DD")
    lb.add_argument("--list-subjects", action="store_true",
                    help="Print the filed subject matters and their counts, "
                         "then stop")
    lb.add_argument("--limit", type=int, default=25,
                    help="How many to return (default 25)")
    lb.set_defaults(func=cmd_lobbying_signals)

    rg = sub.add_parser(
        "registrations-signals",
        help="Who was REGISTERED to lobby a department, as of a date. "
             "--as-of is required and has no default",
    )
    rg.add_argument("--as-of", dest="as_of", required=True,
                    help="REQUIRED, YYYY-MM-DD, or 'today' for current state. "
                         "There is no default: this database stores every "
                         "registration version because 53%% of amended "
                         "registrations change which departments they name, "
                         "and a default meaning 'latest' would answer a "
                         "time-ordered question with present-tense data")
    rg.add_argument("--department", help=_DEPT_HELP)
    rg.add_argument("--client", help="Client organization name contains this")
    rg.add_argument("--vendor",
                    help="Client matched the way contracts.db matches vendors")
    rg.add_argument("--limit", type=int, default=25,
                    help="How many to return (default 25)")
    rg.set_defaults(func=cmd_registrations_signals)

    rd = sub.add_parser(
        "resolve-department",
        help="What a department string resolves to — check before querying",
    )
    rd.add_argument("name")
    rd.set_defaults(func=cmd_resolve_department)

    do = sub.add_parser(
        "dossier",
        help="Everything all five sources know about one department — the "
             "convergence view. Assembles; does not score",
    )
    do.add_argument("department", help=_DEPT_HELP)
    do.add_argument("--months-min", type=int, default=6,
                    help="Expiry window opens this many months out (default 6)")
    do.add_argument("--months-max", type=int, default=24,
                    help="Expiry window closes this many months out (default 24)")
    do.add_argument("--min-value", type=float, default=None,
                    help="Value floor for the expiry timeline (default: "
                         "expiry_min_value from the company profile)")
    do.add_argument("--limit", type=int, default=10,
                    help="Rows per section (default 10)")
    do.set_defaults(func=cmd_department_dossier)

    pr = sub.add_parser("promote", help="Copy a tender into vault/tenders/watching/")
    pr.add_argument("tender_id")
    pr.set_defaults(func=cmd_promote)

    pk = sub.add_parser(
        "park",
        help="Move a watching tender to parked/ (not pursuing now, might revisit)",
    )
    pk.add_argument("filename")
    pk.add_argument("reason", help="Why we're parking it")
    pk.add_argument(
        "revisit_when",
        help="What event would make this worth re-evaluating",
    )
    pk.set_defaults(func=cmd_park)

    ar = sub.add_parser(
        "archive",
        help="Move a tender to archived/ (final). Source can be watching/ or parked/.",
    )
    ar.add_argument("filename")
    ar.add_argument("reason")
    ar.set_defaults(func=cmd_archive)

    at = sub.add_parser(
        "attach",
        help="Create the document folder for a tender (you drop the files in)",
    )
    at.add_argument("tender_id")
    at.add_argument(
        "--platform",
        choices=attachments.SOURCE_PLATFORMS,
        required=True,
        help="Where you pulled the package from, recorded as provenance",
    )
    at.add_argument(
        "--no-reveal",
        action="store_true",
        help="Don't try to open a file manager",
    )
    at.set_defaults(func=cmd_attach)

    la = sub.add_parser(
        "list-attachments",
        help="List a tender's dropped documents, extracting new/changed ones",
    )
    la.add_argument("tender_id")
    la.set_defaults(func=cmd_list_attachments)

    ra = sub.add_parser(
        "read-attachment",
        help="Read a window of one document's extracted text",
    )
    ra.add_argument("tender_id")
    ra.add_argument("filename", help="The dropped filename, e.g. RFP-W2187-SPO.pdf")
    ra.add_argument("--offset", type=int, default=0, help="First line (0-based)")
    ra.add_argument("--limit", type=int, default=400, help="Lines to return (max 2000)")
    ra.set_defaults(func=cmd_read_attachment)

    return p


def main():
    parser = build_parser()
    args = parser.parse_args()
    result = args.func(args)
    print(json.dumps(result, indent=2, default=str))
    # Error responses → non-zero exit so Claude notices
    if isinstance(result, dict) and "error" in result:
        sys.exit(1)


if __name__ == "__main__":
    main()
