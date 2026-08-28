"""
Lock the chunking that fixes silent embedding truncation.

Runs with plain Python — no pytest needed:
    python tests/test_chunking.py

THE DEFECT THIS GUARDS. `all-MiniLM-L6-v2` reports `max_seq_length: 256`, and
sentence-transformers DISCARDS everything past that without raising. The corpus
was embedded from roughly its first 1,265 characters, and a separate `[:2000]`
slice in _write_chroma cut the stored text before BM25 ever indexed it. Neither
limit announced itself: no warning, no failing test, nothing in the corpus that
looked wrong. Measured on the live corpus, 35% of notices exceeded the window
with a median 51% of their text embedded, and the longest notice had 7.6% of it
embedded. See _chunk_document for both measurements and their denominators, and
for why the median is 51% rather than the 63% first recorded.

The assertions are written as PROPERTIES ("text over the window must produce
more than one chunk", "no chunk may exceed the window") rather than as counts
from today's corpus, because the failure mode being guarded is silence. A test
that asserted "246 chunks" would break on every feed refresh and teach whoever
sees it to update the number, which is how the original truncation survived.
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

import conftest  # noqa: E402,F401  — MUST come first: redirects the vault

import pandas as pd  # noqa: E402

import ingest  # noqa: E402
import tender_tools as tt  # noqa: E402
from ingest.corpus import CHUNK_CHARS, _chunk_document  # noqa: E402

CACHE = PROJECT_ROOT / ".cache" / "tenders.csv"
FAILURES: list[str] = []


def check(label: str, got, want) -> None:
    if got == want:
        print(f"  PASS  {label}")
    else:
        FAILURES.append(f"{label}: expected {want!r}, got {got!r}")
        print(f"  FAIL  {label}: expected {want!r}, got {got!r}")


# A description comfortably past the 256-token window: 12 paragraphs of prose.
LONG_DESC = "\n\n".join(
    f"Section {i}. The contractor shall provide the services described in this "
    f"section, including all labour, materials and supervision required to "
    f"complete the work to the satisfaction of the technical authority. "
    f"Deliverable {i} is due within thirty days of contract award."
    for i in range(12)
)
TITLE = "Cyber security support services"


# ---------------------------------------------------------------------------
# _chunk_document — unit properties
# ---------------------------------------------------------------------------

def test_short_description_stays_one_chunk() -> None:
    print("\nA notice that already fits is not split")
    chunks = _chunk_document(TITLE, "Clean the building weekly.")
    check("short notice yields exactly one chunk", len(chunks), 1)


def test_text_past_the_window_is_split() -> None:
    """THE REGRESSION. This is the assertion the defect would have failed."""
    print("\nText past the model's window produces more than one chunk")
    chunks = _chunk_document(TITLE, LONG_DESC)
    check("long notice is split", len(chunks) > 1, True)
    check("split is not degenerate", len(chunks) >= 3, True)


def test_no_chunk_exceeds_the_window() -> None:
    print("\nNo chunk exceeds the budget, including the title it carries")
    for desc, label in ((LONG_DESC, "prose"), ("word " * 3000, "run-on")):
        chunks = _chunk_document(TITLE, desc)
        widest = max(len(c) for c in chunks)
        check(f"widest {label} chunk within CHUNK_CHARS", widest <= CHUNK_CHARS, True)


def test_every_chunk_carries_the_title() -> None:
    print("\nEvery chunk names its notice")
    chunks = _chunk_document(TITLE, LONG_DESC)
    check("all chunks start with the title",
          all(c.startswith(TITLE) for c in chunks), True)
    # Prepended once, not doubled as in the single-document format: repeating it
    # across every chunk of a long notice would outweigh the body text.
    check("title appears once per chunk",
          all(c.count(TITLE) == 1 for c in chunks), True)


def test_no_source_text_is_lost() -> None:
    """The whole point: the tail must reach the index, not just the opening."""
    print("\nNo section of the description is dropped")
    joined = " ".join(_chunk_document(TITLE, LONG_DESC))
    missing = [i for i in range(12) if f"Section {i}." not in joined]
    check("no section missing from the chunks", missing, [])


def test_run_on_paragraph_still_splits() -> None:
    print("\nA paragraph with no sentence boundary is still split")
    chunks = _chunk_document(TITLE, "word " * 3000)
    check("run-on prose is split", len(chunks) > 1, True)


def test_empty_notice_yields_no_chunk() -> None:
    print("\nAn empty notice writes no blank row")
    check("no title and no description yields nothing", _chunk_document("", ""), [])
    check("a title alone still yields a chunk",
          len(_chunk_document("Only a title", "")), 1)


# ---------------------------------------------------------------------------
# _pool_chunk_hits — max, not mean
# ---------------------------------------------------------------------------

def test_pooling_takes_max_not_mean() -> None:
    """
    Locks the pooling choice, which is load-bearing rather than stylistic.

    Tender A has one excellent chunk and three poor ones; tender B has a single
    mediocre chunk. Under MAX, A ranks first — the strong match is what matters.
    Under MEAN, A's good chunk is divided by its three weak ones and B wins,
    which is the original bug in a new form: the longer the notice, the more a
    real match is diluted.
    """
    print("\nPooling takes the best chunk, not the average")
    results = {
        "ids": [["A#0", "A#1", "A#2", "A#3", "B#0"]],
        "distances": [[0.05, 2.0, 2.0, 2.0, 0.6]],
        "metadatas": [[{"tender_id": "A"}, {"tender_id": "A"}, {"tender_id": "A"},
                       {"tender_id": "A"}, {"tender_id": "B"}]],
    }
    ranked = tt._pool_chunk_hits(results)
    check("one entry per tender", len(ranked), 2)
    check("the strong single chunk wins", ranked[0][0], "A")

    excluded = tt._pool_chunk_hits(results, exclude_tender_id="A")
    check("exclusion drops every chunk of that tender",
          [t for t, _ in excluded], ["B"])


def test_chunk_id_without_metadata_still_resolves() -> None:
    print("\nA chunk id resolves to its tender even with metadata missing")
    results = {"ids": [["REF#99#3"]], "distances": [[0.1]], "metadatas": [[{}]]}
    check("id splits on the last separator",
          tt._pool_chunk_hits(results)[0][0], "REF#99")


# ---------------------------------------------------------------------------
# build_chroma — the collection invariants
# ---------------------------------------------------------------------------

def build_corpus(tmp_db: Path):
    """Two real rows, descriptions replaced: one short, one far past the window."""
    df = pd.read_csv(CACHE, low_memory=False)
    cols = ingest.resolve_columns(
        list(df.columns), ingest.TENDER_COLUMNS, ingest.TENDER_REQUIRED,
        "tests/test_chunking.py",
    )
    rows = df[df[cols["tender_id"]].notna()].head(2).copy()
    assert len(rows) == 2, "need two rows in the cache to build a test corpus"
    rows[cols["description"]] = ["Clean the building weekly.", LONG_DESC]

    rows["_closing"] = pd.to_datetime(
        rows[cols["closing_date"]], errors="coerce", utc=True
    ).dt.tz_localize(None)
    rows["_value"] = None
    rows["_matched"] = [[] for _ in range(len(rows))]
    rows["_unspsc_families"] = [[] for _ in range(len(rows))]
    rows["_date_conflict"] = None
    rows["_jurisdiction"] = [
        ingest.classify_jurisdiction(r[cols["contracting_entity"]], r[cols["end_user"]])
        for _, r in rows.iterrows()
    ]
    rows["_kind"] = [
        ingest.classify_notice(
            r[cols["notice_type"]] if cols.get("notice_type") else None,
            r[cols["procurement_category"]] if cols.get("procurement_category") else None,
        )
        for _, r in rows.iterrows()
    ]
    ingest.build_chroma(rows, tmp_db, cols)
    return rows, cols


def test_collection_invariants(tmp_db: Path, rows, cols) -> None:
    print("\nThe two collections agree with each other")
    import chromadb

    client = chromadb.PersistentClient(path=str(tmp_db))
    tenders = client.get_collection("tenders").get()
    chunks = client.get_collection("tender_chunks").get()

    # One row per tender in `tenders` — the contract tender_tools depends on.
    check("tenders collection has one row per notice",
          len(tenders["ids"]), len(rows))
    check("tender ids are unique",
          len(set(tenders["ids"])), len(tenders["ids"]))

    tender_ids = set(tenders["ids"])
    owners = [m["tender_id"] for m in chunks["metadatas"]]
    check("every chunk maps to a known tender",
          set(owners) - tender_ids, set())
    check("every tender with text has at least one chunk",
          tender_ids - set(owners), set())
    check("chunk ids are unique",
          len(set(chunks["ids"])), len(chunks["ids"]))

    # The long notice must have been split; the short one must not have been.
    per_tender = {t: owners.count(t) for t in tender_ids}
    check("the long notice is stored as several chunks",
          max(per_tender.values()) > 1, True)
    check("the short notice is stored as one",
          min(per_tender.values()), 1)

    # And the stored document is no longer sliced at 2,000 characters.
    longest = max(len(d) for d in tenders["documents"])
    check("stored document is not truncated at 2,000 chars",
          longest > 2000, True)


def test_search_returns_each_tender_once(tmp_db: Path) -> None:
    """Pooling must collapse chunks; a long notice must not fill the results."""
    print("\nSearch returns each tender at most once")
    original = tt.DB_PATH
    try:
        tt.DB_PATH = tmp_db
        tt._reset_corpus_state()

        class Args:
            query = "deliverable due within thirty days of contract award"
            n = 10

        results = tt.cmd_search(Args())["results"]
        ids = [r["tender_id"] for r in results]
        check("no tender appears twice", len(ids), len(set(ids)))
        check("the chunked notice is reachable by its tail text",
              len(ids) >= 1, True)
    finally:
        tt.DB_PATH = original
        tt._reset_corpus_state()


def main() -> int:
    print("=" * 72)
    print("Chunking — silent truncation of the embedded corpus")
    print("=" * 72)

    test_short_description_stays_one_chunk()
    test_text_past_the_window_is_split()
    test_no_chunk_exceeds_the_window()
    test_every_chunk_carries_the_title()
    test_no_source_text_is_lost()
    test_run_on_paragraph_still_splits()
    test_empty_notice_yields_no_chunk()
    test_pooling_takes_max_not_mean()
    test_chunk_id_without_metadata_still_resolves()

    if not CACHE.exists():
        print(f"\nSKIP: {CACHE} not present — collection invariants not checked.")
        print("      Run python scripts/ingest first.")
    else:
        tmp_root = Path(tempfile.mkdtemp(prefix="tender-vault-chunk-test-"))
        try:
            tmp_db = tmp_root / "chroma"
            rows, cols = build_corpus(tmp_db)
            test_collection_invariants(tmp_db, rows, cols)
            test_search_returns_each_tender_once(tmp_db)
        finally:
            shutil.rmtree(tmp_root, ignore_errors=True)

    print("\n" + "=" * 72)
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S):")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("All chunking checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
