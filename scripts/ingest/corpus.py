"""
ChromaDB persistence — this is what Claude's tools read from.

The build is staged: the old corpus moves aside and is only deleted once the
new one is complete, so a failure part-way leaves you with the corpus you had.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd

from . import paths


def _meta_str(value, limit: int) -> str:
    """NaN-safe string for ChromaDB metadata. float('nan') is truthy, so the
    obvious `str(x) or ''` yields the literal string 'nan'."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    return str(value).strip()[:limit]


def _feed_mtime_iso(feed_path: Optional[Path]) -> Optional[str]:
    """
    When the feed CSV this run read was downloaded, or None if there wasn't one.

    Separate from the build time on purpose. A rebuild off an unchanged cache
    moves `corpus_built_at` and leaves this alone, and that difference is the
    only way to tell "I have newer data" from "I re-ran the ingest" — which
    matters because only the first one changes what is in the corpus.

    Still a LOCAL fact, and that is its limit: two machines that downloaded the
    same bytes an hour apart carry different values here, so this date orders
    events on one machine and cannot compare two. `feed_sha256` is what does
    that - see corpus_identity. Kept because "when did this arrive" is a real
    question that a hash cannot answer, and because a 304 now leaves it alone,
    which makes it mean when the data last arrived rather than when we last asked.

    Returns None rather than raising: --cache can point anywhere, and a test
    harness that drives build_chroma directly has no feed at all.
    """
    if feed_path is None or not feed_path.exists():
        return None
    return datetime.fromtimestamp(
        feed_path.stat().st_mtime).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------
# Budgets are expressed in CHARACTERS, not tokens, on purpose: this module is
# driven directly by tests that never load a model, and importing a tokenizer
# here to count exactly would make chunking untestable without one. The ratio
# below is measured on this corpus, not assumed.

CHARS_PER_TOKEN = 4.94       # measured across the retained corpus
CHUNK_TOKENS = 200           # comfortably inside the model's 256-token window
OVERLAP_TOKENS = 40          # so a match spanning a boundary survives in one piece
CHUNK_CHARS = int(CHUNK_TOKENS * CHARS_PER_TOKEN)
OVERLAP_CHARS = int(OVERLAP_TOKENS * CHARS_PER_TOKEN)


# Joins paragraphs within a chunk and the overlap tail onto the next chunk. Named
# because its length is subtracted from the chunk budget in _chunk_document.
_SEP = "\n\n"


def _split_oversized(text: str, budget: int) -> list[str]:
    """Break a single paragraph too long for one chunk, preferring sentence
    ends, falling back to a hard slice when one sentence exceeds the budget."""
    parts, buf = [], ""
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        while len(sentence) > budget:
            # A single sentence larger than the budget: hard-slice it. Rare, but
            # tender prose contains run-on requirement lists with no terminator.
            if buf:
                parts.append(buf)
                buf = ""
            parts.append(sentence[:budget])
            sentence = sentence[budget:]
        if not buf:
            buf = sentence
        elif len(buf) + 1 + len(sentence) <= budget:
            buf = f"{buf} {sentence}"
        else:
            parts.append(buf)
            buf = sentence
    if buf:
        parts.append(buf)
    return parts


def _chunk_document(title: str, desc: str) -> list[str]:
    """
    Split one notice into overlapping windows that each fit the embedding model.

    WHY THIS EXISTS — this fixes a silent, systematic retrieval failure, and the
    measurement is recorded here because the fix looks arbitrary without it.

    `all-MiniLM-L6-v2` reports `max_seq_length: 256`. sentence-transformers
    DISCARDS everything past that without raising, so before this change the
    corpus was embedded from roughly its first 1,265 characters and no warning
    was ever emitted.

    Measured twice on 2026-08-28, either side of a feed refresh. The truncation
    rate is stable across both, which is the point of recording both: it is a
    property of federal tender prose, not of one day's corpus.

      * 71-tender corpus, 68 with English descriptions, joined back to the raw
        feed for untruncated text: 24/68 = 35% exceeded the 256-token window.
      * 66-tender corpus after the refresh, measured on the stored documents
        directly: 23/66 = 35% exceeded it. Those had a MEDIAN 51% of their text
        embedded; the longest notice, 17,389 chars / 3,350 tokens, had 7.6%.
        Corpus-wide tokens: median 176, p90 910, max 3,350.

    THE MEDIAN IS 51%, NOT THE 63% FIRST RECORDED HERE. The 63% was computed
    against documents the old `[:2000]` cap had ALREADY shortened, so the
    denominator was the truncated text rather than the real description and the
    loss came out flattering. Measured against full text it is 51%. A figure
    quantifying a truncation must not be derived from the truncated artefact.

    Do not confuse either number with the share of notices that CHUNK into more
    than one piece - 33/66 = 50% on the same corpus. The chunk budget is about
    756 characters of body text, well under the ~1,265 the window allows, so ten
    notices split without ever having been truncated. Every truncated notice is
    in the split set; the reverse does not hold.

    A longer-context model does not resolve this: 512 tokens covers ~2,530 chars
    and still misses the top 10% of descriptions. The text is genuinely long, so
    it has to be chunked rather than fitted into a bigger window.

    The title is prepended to EVERY chunk. A chunk taken from the middle of a
    description carries no trace of which notice it belongs to, and a query that
    names the kind of work would otherwise miss it. It is prepended ONCE, not
    doubled as in the single-document format - repeating it across twenty-one
    chunks of one long notice would drown the body text it is meant to label.

    Returns [] only when the notice has no title and no description at all;
    any text at all yields at least one chunk. Callers rely on that: a tender
    with text is never absent from the chunk collection, and a blank document
    is never written into it.
    """
    title = (title or "").strip()
    desc = (desc or "").strip()
    if not title and not desc:
        return []

    prefix = f"{title}{_SEP}" if title else ""
    # The title, the carried-over overlap AND the separator between them all
    # cost budget in every chunk, so all three are subtracted here rather than
    # added on afterwards - otherwise a chunk silently grows to prefix + overlap
    # + separator + budget and drifts back over the window this function exists
    # to stay inside. Every term is accounted for because the omitted one is
    # always the one that puts a chunk over. The floor keeps a very long title
    # from squeezing the body down to nothing.
    budget = max(CHUNK_CHARS - len(prefix) - OVERLAP_CHARS - len(_SEP), 200)

    segments: list[str] = []
    for para in re.split(r"\n\s*\n", desc):
        para = para.strip()
        if not para:
            continue
        segments.extend(
            _split_oversized(para, budget) if len(para) > budget else [para])

    if not segments:
        return [prefix.strip()] if prefix.strip() else []

    bodies: list[str] = []
    buf = ""
    for seg in segments:
        if not buf:
            buf = seg
        elif len(buf) + len(_SEP) + len(seg) <= budget:
            buf = f"{buf}{_SEP}{seg}"
        else:
            bodies.append(buf)
            # Overlap: carry the tail of the chunk we just closed into the next
            # one, cut at a word boundary so we never start mid-token.
            tail = buf[-OVERLAP_CHARS:]
            tail = tail[tail.find(" ") + 1:] if " " in tail else ""
            buf = f"{tail}{_SEP}{seg}".strip() if tail else seg
    if buf:
        bodies.append(buf)

    return [f"{prefix}{b}" for b in bodies]


# ---------------------------------------------------------------------------
# Corpus identity — what a build was made FROM, by content
# ---------------------------------------------------------------------------

def _sha256_file(path: Optional[Path]) -> Optional[str]:
    """Content hash of a file, or None when there is no file to hash."""
    if path is None or not Path(path).exists():
        return None
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def corpus_identity(feed_path: Optional[Path],
                    profile_path: Optional[Path]) -> dict[str, str]:
    """
    The inputs this corpus was built from, identified by CONTENT.

    Two hashes, because the two answer different questions and a rebuild that
    changes one is a different event from a rebuild that changes the other. A
    new `feed_sha256` is new notices; a new `profile_sha256` is a re-scoring of
    the same ones. `feed_downloaded_at` cannot separate them — it moves whenever
    bytes arrive, including when the bytes are identical — which is tolerable at
    one run a week and is a daily false alarm at one run a day.

    Keys are OMITTED when unknown, never None, for the reason `_write_chroma`
    documents: Chroma raises on a None metadata value. An absent key means "no
    hash to compare", which is a different finding from "the hashes differ" and
    must stay distinguishable downstream.

    Deliberately NOT a hash of this file. A change to the filter code alters the
    corpus without altering either input, so `--skip-unchanged` would sit on a
    stale build after a deploy. Run without the flag when the code changes; the
    flag is for the scheduled case, where the code is fixed and the feed is not.
    """
    out: dict[str, str] = {}
    feed = _sha256_file(feed_path)
    if feed is not None:
        out["feed_sha256"] = feed
    profile = _sha256_file(profile_path)
    if profile is not None:
        out["profile_sha256"] = profile
    return out


# The identity of the build that produced a corpus, kept INSIDE the corpus
# directory. Two reasons it is a file rather than a read of the collection
# metadata that carries the same values. First, opening a ChromaDB client takes
# OS-level handles on the directory for the life of the process, and
# `build_chroma` renames that directory — reading the corpus to decide whether
# to replace it would break replacing it, on Windows, exactly as the comment
# there records. Second, it needs no chromadb import and no dependency on
# Chroma's storage schema.
#
# The collection metadata remains the portable record: this file is local and
# gitignored with the rest of chroma_db/, and only ever an optimisation. When it
# is missing the answer is "rebuild", which is the safe direction.
IDENTITY_FILENAME = "corpus-identity.json"


def stored_identity(db_path: Path) -> dict[str, str]:
    """Identity of the corpus already at `db_path`. {} when there isn't one."""
    try:
        loaded = json.loads(
            (paths.active_db(db_path) / IDENTITY_FILENAME)
            .read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def build_chroma(df: pd.DataFrame, db_path: Path, cols: dict,
                 feed_path: Optional[Path] = None,
                 identity: Optional[dict] = None) -> None:
    """
    Embed filtered tenders and write to a persistent ChromaDB collection.

    `feed_path` is the CSV this run read, recorded as provenance. Optional and
    keyword-defaulted because tests drive this directly with no feed on disk;
    absent means the corpus is stamped with a build time and no feed date,
    which is a different fact from an unstamped corpus.

    `identity` is `corpus_identity()` for this build - content hashes of the
    inputs. Also optional and for the same reason, and an absent hash stays
    absent rather than becoming a placeholder.
    """
    # Imported here, not at module level: this is the only function that needs
    # ChromaDB, and contracts_ingest.py imports this module purely for its
    # profile parser and HTTP headers.
    import chromadb
    from chromadb.utils import embedding_functions

    # A REBUILD NEVER TOUCHES THE DIRECTORY A READER MAY HAVE OPEN. The corpus
    # is a container holding build directories plus a one-line CURRENT file
    # naming the live one; a rebuild writes a new build beside the old and then
    # repoints CURRENT. See paths.active_db for the failure that forced this.
    # Briefly: scripts/mcp_server.py holds a ChromaDB client open for the life
    # of a Claude Desktop session, and the previous strategy began by renaming
    # chroma_db/ out of the way, which Windows refuses while those handles
    # exist. On 2026-08-28 that cost a full feed download and embed before
    # dying with PermissionError [WinError 5], leaving an untouched corpus
    # behind an advanced feed cache - the exact stale-mismatch state the
    # provenance block exists to report.
    #
    # The ordering also gives rollback for free: CURRENT still names the old
    # build until the new one is complete, so a failure below leaves the corpus
    # as it was rather than needing to be put back.
    container = db_path
    container.mkdir(parents=True, exist_ok=True)
    previous = paths.active_db(container)
    build_dir = container / f"build-{datetime.now():%Y%m%dT%H%M%S-%f}"

    try:
        _write_chroma(df, build_dir, cols, feed_path, identity)
    except BaseException:
        # Nothing was moved, so there is nothing to restore. Clear the partial
        # build if ChromaDB has let go of it; if it has not, the directory
        # costs nothing and the next successful run sweeps it up.
        shutil.rmtree(build_dir, ignore_errors=True)
        sys.stderr.write(
            f"\nIngest failed. Your corpus was not modified and is still live:\n"
            f"  {previous}\n"
        )
        raise

    # AFTER the build succeeds, never before. This file is what --skip-unchanged
    # trusts, so it must not be able to describe a corpus that was never
    # completed - an absent file costs a rebuild, a premature one costs a
    # silently stale corpus.
    (build_dir / IDENTITY_FILENAME).write_text(
        json.dumps(identity or {}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8", newline="\n")

    # The switch, and the only step that makes the new build visible.
    # os.replace is atomic: a reader sees the old name or the new one, never a
    # half-written pointer.
    pointer = container / paths.POINTER_FILENAME
    tmp_pointer = container / (paths.POINTER_FILENAME + ".tmp")
    tmp_pointer.write_text(build_dir.name + "\n", encoding="utf-8", newline="\n")
    os.replace(tmp_pointer, pointer)

    # Sweep, but KEEP THE BUILD WE JUST SUPERSEDED. A reader that opened the
    # previous build is still serving from it - that is the whole premise of
    # this layout - and deleting it underneath them is the same rug-pull the
    # rename used to be, just with better timing. One generation of grace
    # bounds the disk cost at two builds while never removing what a live
    # reader most likely holds. Anything older is fair game, and still best
    # effort: a directory that refuses to go waits for the next run.
    keep = {build_dir, previous}
    for stale in sorted(container.glob("build-*")):
        if stale not in keep:
            shutil.rmtree(stale, ignore_errors=True)

    # A corpus built before this layout leaves its store at the container top
    # level. It is deliberately NOT deleted: on the first run under this code
    # that store is the one every already-running reader has open. It is
    # superseded the moment CURRENT is written, costs only disk, and can be
    # removed by hand once nothing is serving from it.


def _write_chroma(df: pd.DataFrame, db_path: Path, cols: dict,
                  feed_path: Optional[Path] = None,
                  identity: Optional[dict] = None) -> None:
    """
    Embed and write. Split out so build_chroma owns the rollback logic.

    ROWS ARE BUILT BEFORE THE COLLECTIONS ARE CREATED, and that ordering is what
    lets the provenance be complete at creation rather than patched afterwards.
    The gate below - a blank or 'nan' reference number - is the filter's stage 7
    (see filter_audit.predicates), and it runs HERE, after filter_tenders has
    returned and after the funnel has already counted the row. So the funnel's
    final count has always been an upper bound on what reached the corpus, and
    the difference has never appeared in any line this script prints. It does
    now, at zero or otherwise.
    """
    import chromadb
    from chromadb.utils import embedding_functions

    client = chromadb.PersistentClient(path=str(db_path))
    embedder = embedding_functions.SentenceTransformerEmbeddingFunction(
        model_name="all-MiniLM-L6-v2"
    )

    documents, metadatas, ids = [], [], []
    chunk_docs, chunk_metas, chunk_ids = [], [], []
    unidentifiable = []
    for _, row in df.iterrows():
        tender_id = str(row.get(cols["tender_id"], ""))
        if not tender_id or tender_id == "nan":
            # Counted, not just skipped. A row dropped silently between the
            # funnel and the corpus is a row nobody can account for later.
            unidentifiable.append(str(row.get(cols["title"], ""))[:60])
            continue

        title = str(row.get(cols["title"], ""))[:300]
        # NOT truncated. The former [:2000] here cut 21% of the corpus before
        # BM25 ever indexed it, while the embedding model was separately and
        # silently cutting 35% of it at 256 tokens - see _chunk_document. The
        # long text is the point: it is where requirements and evaluation
        # criteria live. Chunking handles the embedding side; BM25 and
        # get_tender want the whole thing.
        desc = str(row.get(cols["description"], ""))
        if desc == "nan":
            desc = ""
        # We embed title + description — weighting title higher by repeating it
        document = f"{title}\n{title}\n\n{desc}"

        metadata = {
            "tender_id": tender_id,
            "title": title[:200],
            # TWO fields, deliberately not merged and not collapsed to one.
            # Federal IT is routinely bought by a central authority (SSC, PSPC)
            # on behalf of the department that actually needs the work, so the
            # contracting entity is frequently NOT the customer. End user is the
            # demand signal; contracting entity is the fallback — it's always
            # populated, while end user is blank on roughly half the rows.
            "contracting_entity": _meta_str(row.get(cols["contracting_entity"]), 500),
            # Multi-valued and slash-delimited: one tender can legitimately name
            # several departments ("Department of National Defence (DND) /
            # Department of Transport (TC) / ..."). Stored VERBATIM, all values
            # kept. Do not re-join on commas — entity names contain commas
            # ("Foreign Affairs, Trade And Development (Department Of)"), which
            # would make the field unsplittable downstream.
            "end_user_entity": _meta_str(row.get(cols["end_user"]), 500),
            "closing_date": row["_closing"].strftime("%Y-%m-%d") if pd.notna(row["_closing"]) else "",
            "matched_competencies": ",".join(row.get("_matched", [])),
            # Instrument shape, from classify_notice — the same function the
            # dossier uses. `kind_basis` says which publisher field decided it,
            # because determinability is not uniform across the four shapes.
            "opportunity_kind": row["_kind"]["opportunity_kind"],
            "kind_basis": row["_kind"]["kind_basis"],
            # Which hand-checked UNSPSC families this notice fell under, empty
            # when it qualified on keywords alone (or carries no codes at all).
            "unspsc_families": ",".join(row.get("_unspsc_families") or []),
            # federal / unrecognised. `unrecognised` means the registry has no
            # entry, NOT that the notice is provincial — federal Crown
            # corporations land there. Non-federal never reaches this point.
            "jurisdiction": row["_jurisdiction"]["jurisdiction"],
        }
        if row["_jurisdiction"].get("org_keys"):
            metadata["org_keys"] = row["_jurisdiction"]["org_keys"]

        # Present ONLY when the prose contradicts the closing date. An absent
        # key means no conflict was found, not that the date was verified.
        if row.get("_date_conflict"):
            metadata["closing_date_conflict"] = row["_date_conflict"]
            metadata["closing_date_note"] = (
                f"The description states a submission deadline of "
                f"{row['_date_conflict']}, EARLIER than the closing_date field. "
                f"Confirm against the notice before planning to the later date.")

        # OMITTED, not zeroed, when no value was extracted. Chroma metadata
        # cannot hold None, and storing 0.0 made "nobody stated a value" render
        # as "this contract is worth nothing" on every one of 11 tenders.
        # Absent key -> consumers show "not stated".
        if pd.notna(row.get("_value")):
            metadata["estimated_value"] = float(row["_value"])

        documents.append(document)
        metadatas.append(metadata)
        ids.append(tender_id)

        # Chunk ids are derived, never sourced: "{tender_id}#{i}". The pooling in
        # tender_tools splits on the LAST '#' to recover the tender, so a
        # tender_id that itself contains '#' is still resolvable.
        for i, chunk in enumerate(_chunk_document(title, desc)):
            chunk_docs.append(chunk)
            chunk_metas.append({"tender_id": tender_id, "chunk_index": i})
            chunk_ids.append(f"{tender_id}#{i}")

    # Provenance, written here because it cannot be recovered afterwards:
    # ChromaDB rewrites its segment files whenever anything LOADS the
    # collection, so chroma_db/ mtimes report when the corpus was last queried,
    # not when it was built. A briefing that reads them describes its own read.
    provenance = {
        "corpus_built_at": datetime.now().isoformat(timespec="seconds"),
        # The funnel's last number, and what actually reached the corpus. Two
        # counts rather than one, and the delta between them stated rather than
        # left to subtraction, because a reader comparing a funnel line in a log
        # against a corpus size has no way to know a gate ran in between. ALWAYS
        # PRESENT, including at zero: an absent key would make "no rows were
        # lost" indistinguishable from "an older build never measured it".
        "funnel_admitted": int(len(df)),
        "corpus_written": int(len(ids)),
        "funnel_write_delta": int(len(df) - len(ids)),
    }
    # OMITTED when unknown, never None. Chroma raises
    # `TypeError: argument 'metadata': Cannot convert Python object to
    # MetadataValue` on a None value (verified, chromadb 1.5.9), so the obvious
    # inline `"feed_downloaded_at": _feed_mtime_iso(...)` crashes the whole
    # ingest the first time there is no cached feed. An absent key is the
    # signal — see the provenance states in tender_tools._corpus_provenance.
    # The three counts above are exempt by construction: a count is never None.
    feed_at = _feed_mtime_iso(feed_path)
    if feed_at is not None:
        provenance["feed_downloaded_at"] = feed_at
    # The content hashes ride alongside the timestamps rather than replacing
    # them. A timestamp says when something happened here; a hash says which
    # bytes it happened to, and only the second one is comparable across two
    # machines that downloaded the same feed at different moments. Already
    # omit-when-unknown by construction - see corpus_identity.
    provenance.update(identity or {})

    collection = client.create_collection(
        name="tenders",
        embedding_function=embedder,
        metadata={"hnsw:space": "cosine", **provenance},
    )
    # A SIBLING collection, not a reshaping of `tenders`. One row per chunk here;
    # `tenders` keeps its one row per tender, because that shape is load-bearing
    # in tender_tools - doc_index, the BM25 corpus, get_tender, list_corpus and
    # the digest snapshot all assume it. Chunking in place would return chunks
    # from _collection.get() and break every one of them. Both collections live
    # under the same db_path, so build_chroma's move-aside rollback already
    # covers them as a single unit.
    chunk_collection = client.create_collection(
        name="tender_chunks",
        embedding_function=embedder,
        metadata={"hnsw:space": "cosine", **provenance},
    )

    # Batch insert (ChromaDB handles this fine up to several thousand at a time)
    batch = 200
    for i in range(0, len(documents), batch):
        collection.add(
            documents=documents[i:i + batch],
            metadatas=metadatas[i:i + batch],
            ids=ids[i:i + batch],
        )
        print(f"  Embedded {min(i + batch, len(documents)):,} / {len(documents):,}")

    for i in range(0, len(chunk_docs), batch):
        chunk_collection.add(
            documents=chunk_docs[i:i + batch],
            metadatas=chunk_metas[i:i + batch],
            ids=chunk_ids[i:i + batch],
        )
        print(f"  Embedded chunk {min(i + batch, len(chunk_docs)):,} / "
              f"{len(chunk_docs):,}")

    # The counts above are a prediction about what the write would do. This is
    # the write's own answer, and a disagreement RAISES rather than being
    # reconciled: build_chroma restores the previous corpus, so the operator
    # gets a corpus that matches its recorded provenance or no new corpus.
    #
    # Nothing known makes these differ today - duplicate ids, the obvious
    # candidate, are refused by Chroma itself with DuplicateIDError (verified,
    # chromadb 1.5.9) rather than absorbed. That is exactly why the check is
    # cheap to keep: it costs one count and it is the only statement in this
    # function that is checked against the store rather than against the frame.
    #
    # BOTH collections are checked. The chunk collection is written by the same
    # loop and abandoned by the same rollback, so leaving it unchecked would
    # mean the corpus could disagree with its provenance in the one place the
    # semantic side actually reads.
    written = collection.count()
    if written != len(ids):
        raise RuntimeError(
            f"Corpus write disagrees with its own provenance: prepared "
            f"{len(ids):,} rows, the collection holds {written:,}. The recorded "
            f"corpus_written would describe a corpus that does not exist, so "
            f"this build is abandoned and the previous corpus restored. Rows "
            f"cannot go missing between add() and count() by any known path - "
            f"find out what changed before rebuilding.")

    chunks_written = chunk_collection.count()
    if chunks_written != len(chunk_ids):
        raise RuntimeError(
            f"Chunk write disagrees with what was prepared: {len(chunk_ids):,} "
            f"chunks built, the collection holds {chunks_written:,}. Semantic "
            f"search reads this collection, so a short write silently narrows "
            f"retrieval - the build is abandoned rather than shipped.")

    delta = len(df) - written
    print(f"\nChromaDB written to {db_path} ({written:,} tenders, "
          f"{chunks_written:,} chunks)")
    # Printed adjacent, always, so the two are read as one fact. The funnel's
    # own last line is several hundred lines of output back by now.
    print(f"  funnel admitted   {len(df):>7,}")
    print(f"  written to corpus {written:>7,}   "
          f"(delta {delta}"
          + (" - every admitted notice reached the corpus)" if delta == 0
             else " - dropped at stage 7, blank/'nan' reference number)"))
    for title in unidentifiable[:5]:
        print(f"    no reference number: {title}")
    if len(unidentifiable) > 5:
        print(f"    ... and {len(unidentifiable) - 5:,} more")
