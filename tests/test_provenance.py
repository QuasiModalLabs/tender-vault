"""
Lock corpus provenance: how old the corpus is must be a reported fact.

Runs with plain Python — no pytest needed:
    python tests/test_provenance.py

THE BUG THIS EXISTS TO PREVENT. A briefing dated the corpus from `chroma_db/`
file times and got it wrong. ChromaDB rewrites its HNSW segment files whenever
anything LOADS the collection, so those mtimes report when the corpus was last
queried — a briefing reading them describes its own read. `vault/CLAUDE.md` had
told it to do exactly that. The fix is a stamp written by the ingest, and every
assertion here defends some part of that stamp being trustworthy.

FOUR FAILURE MODES, none of which raises on its own:

1. None in Chroma metadata. `metadata={"feed_downloaded_at": None}` raises
   TypeError at create_collection (chromadb 1.5.9), so the obvious inline
   version crashes the whole ingest the first time there is no cached feed.
   The key must be OMITTED instead.

2. Unquoted ISO timestamps in the digest frontmatter. `corpus_built_at:
   2026-08-09T14:27:11` is a YAML timestamp scalar — safe_load returns a
   datetime while the stamp from Chroma metadata is a str, so every equality
   check reads False and the machine that produced the digest reports itself
   as behind. Silent, confident, wrong.

3. States collapsing into one another. `unstamped` (predates stamping — a
   rebuild fixes it), `no_feed_at_build` (built, but its data cannot be dated —
   a rebuild does NOT fix it) and `not_found` (no digest resolved at all, i.e.
   the lookup is broken) are three different facts. Once null is a legal value,
   any of them can masquerade as any other.

4. A threshold sneaking back in. There is deliberately no `stale` field: how
   old is too old is not stateable, and a boolean verdict in a data field is
   the judgement this project keeps out of its tools.
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

import conftest  # noqa: E402,F401  — MUST come first: redirects the vault

import pandas as pd  # noqa: E402

import digest as digest_mod  # noqa: E402
import ingest  # noqa: E402
import tender_tools as tt  # noqa: E402


FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {label}")
    else:
        FAILURES.append(f"{label}{': ' + detail if detail else ''}")
        print(f"  FAIL  {label}{': ' + detail if detail else ''}")


class FakeCollection:
    """Stands in for a loaded Chroma collection; only .metadata is read."""

    def __init__(self, metadata):
        self.metadata = metadata


# ---------------------------------------------------------------------------
# A minimal corpus, built through the real ingest path
# ---------------------------------------------------------------------------

def _one_row_frame() -> tuple[pd.DataFrame, dict]:
    """
    One synthetic notice with the columns build_chroma needs.

    Synthetic rather than read from .cache/tenders.csv on purpose: this suite
    must be able to assert what happens when there is NO cached feed, so it
    cannot require one to run.
    """
    col = {k: v[0] for k, v in ingest.TENDER_COLUMNS.items()}
    df = pd.DataFrame([{
        col["tender_id"]: "TEST-PROV-1",
        col["title"]: "Provenance fixture notice",
        col["description"]: "A synthetic notice used to test corpus stamping.",
        col["closing_date"]: "2027-01-01",
        col["contracting_entity"]: "Department of National Defence (DND)",
        col["end_user"]: "Department of National Defence (DND)",
        col["notice_type"]: "Request for Proposal",
        col["procurement_category"]: "SRV",
        col["unspsc"]: "81111500",
    }])
    cols = ingest.resolve_columns(
        list(df.columns), ingest.TENDER_COLUMNS, ingest.TENDER_REQUIRED,
        "tests/test_provenance.py",
    )
    # The columns filter_tenders would have added, supplied directly.
    df["_closing"] = pd.to_datetime(df[cols["closing_date"]], errors="coerce")
    df["_value"] = None
    df["_matched"] = [[]]
    df["_unspsc_families"] = [["8111"]]
    df["_date_conflict"] = None
    df["_jurisdiction"] = [ingest.classify_jurisdiction(
        df.iloc[0][cols["contracting_entity"]], df.iloc[0][cols["end_user"]])]
    df["_kind"] = [ingest.classify_notice(
        df.iloc[0][cols["notice_type"]],
        df.iloc[0][cols["procurement_category"]])]
    return df, cols


def _collection_metadata(db_path: Path) -> dict:
    import chromadb
    return dict(
        chromadb.PersistentClient(path=str(db_path))
        .get_collection("tenders").metadata or {})


# ---------------------------------------------------------------------------
# 1 — the ingest stamps, and never writes None
# ---------------------------------------------------------------------------

def test_ingest_stamps(tmp: Path) -> None:
    print("\nIngest stamping")
    df, cols = _one_row_frame()

    feed = tmp / "tenders.csv"
    feed.write_text("stand-in for the feed CSV\n", encoding="utf-8")
    db = tmp / "chroma-stamped"
    ingest.build_chroma(df, db, cols, feed_path=feed)
    meta = _collection_metadata(db)

    check("corpus_built_at written", "corpus_built_at" in meta, str(meta))
    check("feed_downloaded_at written", "feed_downloaded_at" in meta, str(meta))
    check("both stamps are str",
          all(isinstance(meta.get(k), str)
              for k in ("corpus_built_at", "feed_downloaded_at")),
          str({k: type(meta.get(k)).__name__ for k in meta}))
    check("feed stamp matches the file's mtime",
          meta.get("feed_downloaded_at") == datetime.fromtimestamp(
              feed.stat().st_mtime).isoformat(timespec="seconds"))

    # THE CRASH GUARD. Before the fix this raised
    # TypeError: argument 'metadata': Cannot convert Python object to
    # MetadataValue — the ingest died rather than omitting the key.
    db2 = tmp / "chroma-nofeed"
    raised = None
    try:
        ingest.build_chroma(df, db2, cols, feed_path=tmp / "does-not-exist.csv")
    except BaseException as exc:  # noqa: BLE001 — we want to report any type
        raised = exc
    check("ingest survives a missing feed", raised is None, repr(raised))
    if raised is None:
        meta2 = _collection_metadata(db2)
        check("build stamp still written with no feed",
              "corpus_built_at" in meta2, str(meta2))
        check("feed key is ABSENT, not None",
              "feed_downloaded_at" not in meta2,
              f"got {meta2.get('feed_downloaded_at')!r}")


# ---------------------------------------------------------------------------
# 2 — reading the corpus must not move the stamp
# ---------------------------------------------------------------------------

def test_stamp_survives_reads(tmp: Path) -> None:
    """The whole bug: chroma_db/ mtimes move on read, so the stamp must not."""
    print("\nStamp is immune to reads")
    df, cols = _one_row_frame()
    feed = tmp / "tenders2.csv"
    feed.write_text("feed\n", encoding="utf-8")
    db = tmp / "chroma-reads"
    ingest.build_chroma(df, db, cols, feed_path=feed)

    first = _collection_metadata(db)
    segment_mtimes_1 = sorted(p.stat().st_mtime for p in db.rglob("*") if p.is_file())
    second = _collection_metadata(db)
    third = _collection_metadata(db)
    segment_mtimes_2 = sorted(p.stat().st_mtime for p in db.rglob("*") if p.is_file())

    check("corpus_built_at identical across three reads",
          first.get("corpus_built_at") == second.get("corpus_built_at")
          == third.get("corpus_built_at"),
          f"{first.get('corpus_built_at')} / {second.get('corpus_built_at')}")
    check("feed_downloaded_at identical across three reads",
          first.get("feed_downloaded_at") == third.get("feed_downloaded_at"))
    # Not an assertion about Chroma's internals so much as a record of WHY the
    # stamp exists: if these ever stop moving, the file times would have been
    # usable after all and this note should be revisited.
    if segment_mtimes_1 != segment_mtimes_2:
        print("        (confirmed: chroma_db/ file times moved on read -- "
              "which is exactly why the stamp is not derived from them)")


# ---------------------------------------------------------------------------
# 3 — the four states, and that none can impersonate another
# ---------------------------------------------------------------------------

def test_states(tmp: Path) -> None:
    print("\nProvenance states")
    digests = tmp / "digests"
    digests.mkdir(parents=True, exist_ok=True)
    original_collection, original_digests = tt._collection, tt.DIGESTS
    tt.DIGESTS = digests
    try:
        # -- local corpus states -------------------------------------------
        tt._collection = FakeCollection({"hnsw:space": "cosine"})
        out = tt._corpus_provenance()
        check("unstamped corpus reports 'unstamped'",
              out["state"] == "unstamped", out["state"])
        check("unstamped corpus carries a note", bool(out.get("note")))

        tt._collection = FakeCollection(
            {"corpus_built_at": "2026-08-09T14:27:11"})
        out = tt._corpus_provenance()
        check("built with no feed reports 'no_feed_at_build'",
              out["state"] == "no_feed_at_build", out["state"])
        check("no_feed_at_build is NOT reported as unstamped",
              out["state"] != "unstamped")
        check("no_feed_at_build keeps its build stamp",
              out["corpus_built_at"] == "2026-08-09T14:27:11")

        # -- digest states --------------------------------------------------
        out = tt._corpus_provenance()
        check("no digest on disk reports 'not_found'",
              out["newest_digest_state"] == "not_found",
              out.get("newest_digest_state"))

        # A frontmatterless digest — the state every digest on disk was in
        # when this shipped. Must NOT read as not_found.
        (digests / "digest-2026-08-09.md").write_text(
            "# Weekly digest — 2026-08-09\n\n**Corpus size:** 70\n",
            encoding="utf-8")
        out = tt._corpus_provenance()
        check("frontmatterless digest reports 'unstamped'",
              out["newest_digest_state"] == "unstamped",
              out.get("newest_digest_state"))
        check("frontmatterless digest is NOT reported as not_found",
              out["newest_digest_state"] != "not_found")
        check("the digest lookup resolved the real file",
              out["newest_digest"] == "digest-2026-08-09",
              str(out.get("newest_digest")))

        # Newest wins, and the glob actually matches the prefixed filenames.
        (digests / "digest-2026-08-11.md").write_text(
            '---\ncorpus_built_at: "2026-08-11T09:00:00"\n'
            'feed_downloaded_at: "2026-08-11T08:30:00"\n---\n\n# d\n',
            encoding="utf-8")
        out = tt._corpus_provenance()
        check("newest digest wins the lookup",
              out["newest_digest"] == "digest-2026-08-11",
              str(out.get("newest_digest")))
        check("stamped digest reports 'stamped'",
              out["newest_digest_state"] == "stamped",
              out.get("newest_digest_state"))
    finally:
        tt._collection, tt.DIGESTS = original_collection, original_digests


# ---------------------------------------------------------------------------
# 4 — the comparison reads BOTH stamps
# ---------------------------------------------------------------------------

def test_comparison(tmp: Path) -> None:
    """
    "The digest is newer so I am behind" holds only if the FEED moved.

    An equal feed stamp with a later build is a rebuild of the same data, and
    calling that "behind" would send the reader to re-run an ingest that
    changes nothing — while hiding that a membership change was a filter
    effect.
    """
    print("\nComparison against the newest digest")
    digests = tmp / "digests-cmp"
    digests.mkdir(parents=True, exist_ok=True)
    original_collection, original_digests = tt._collection, tt.DIGESTS
    tt.DIGESTS = digests

    def digest_with(built: str, feed: str) -> None:
        for old in digests.glob("digest-*.md"):
            old.unlink()
        (digests / "digest-2026-08-11.md").write_text(
            f'---\ncorpus_built_at: "{built}"\n'
            f'feed_downloaded_at: "{feed}"\n---\n\n# d\n', encoding="utf-8")

    try:
        # Identical on both stamps.
        digest_with("2026-08-09T14:27:11", "2026-08-09T13:52:45")
        tt._collection = FakeCollection({
            "corpus_built_at": "2026-08-09T14:27:11",
            "feed_downloaded_at": "2026-08-09T13:52:45"})
        out = tt._corpus_provenance()
        check("equal stamps -> 'produced the newest digest'",
              "produced the newest digest" in out.get("reading", ""),
              out.get("reading"))

        # Same feed, later build. NOT behind.
        tt._collection = FakeCollection({
            "corpus_built_at": "2026-08-11T20:00:00",
            "feed_downloaded_at": "2026-08-09T13:52:45"})
        out = tt._corpus_provenance()
        check("same feed + later build -> filter effect, not new notices",
              "filter or profile effect" in out.get("reading", ""),
              out.get("reading"))
        check("same feed + later build is NOT reported as behind",
              "behind" not in out.get("reading", ""), out.get("reading"))

        # Feed moved: genuinely behind.
        digest_with("2026-08-11T09:00:00", "2026-08-11T08:30:00")
        tt._collection = FakeCollection({
            "corpus_built_at": "2026-08-09T14:27:11",
            "feed_downloaded_at": "2026-08-09T13:52:45"})
        out = tt._corpus_provenance()
        check("older feed -> 'behind on data'",
              "behind on data" in out.get("reading", ""), out.get("reading"))
    finally:
        tt._collection, tt.DIGESTS = original_collection, original_digests


# ---------------------------------------------------------------------------
# 5 — digest frontmatter round-trip, and the YAML timestamp trap
# ---------------------------------------------------------------------------

def test_digest_roundtrip(tmp: Path) -> None:
    print("\nDigest frontmatter round-trip")
    df, cols = _one_row_frame()
    feed = tmp / "tenders3.csv"
    feed.write_text("feed\n", encoding="utf-8")
    db = tmp / "chroma-digest"
    ingest.build_chroma(df, db, cols, feed_path=feed)
    meta = _collection_metadata(db)

    digests = tmp / "digests-rt"
    digests.mkdir(parents=True, exist_ok=True)
    original = (tt.DIGESTS,
                digest_mod.DIGEST_DIR, digest_mod.CORPUS_SNAPSHOT, tt.DB_PATH)
    try:
        tt.DB_PATH = db
        # One call, not a hand-listed tuple of globals: see _reset_corpus_state.
        tt._reset_corpus_state()
        tt.DIGESTS = digests
        digest_mod.DIGEST_DIR = digests
        digest_mod.CORPUS_SNAPSHOT = digests / "corpus-latest.txt"

        content = digest_mod.generate_digest()
        path = digests / "digest-2026-08-11.md"
        path.write_text(content, encoding="utf-8", newline="\n")

        check("frontmatter is present", content.startswith("---\n"),
              content[:40])
        check("values are QUOTED (else YAML parses them as datetimes)",
              f'corpus_built_at: "{meta["corpus_built_at"]}"' in content,
              content.split("\n---")[0])

        fm = tt._digest_frontmatter(path)
        check("parsed build stamp is a str",
              isinstance(fm.get("corpus_built_at"), str),
              type(fm.get("corpus_built_at")).__name__)
        check("parsed feed stamp is a str",
              isinstance(fm.get("feed_downloaded_at"), str),
              type(fm.get("feed_downloaded_at")).__name__)
        # The comparison the whole design turns on. A datetime here would
        # compare unequal to the str from Chroma and never raise.
        check("parsed stamp EQUALS the collection's",
              fm.get("corpus_built_at") == meta["corpus_built_at"],
              f"{fm.get('corpus_built_at')!r} != {meta['corpus_built_at']!r}")

        # And that PyYAML would have mangled it without the quotes, so this
        # test is protecting against something real rather than a theory.
        try:
            import yaml
            unquoted = yaml.safe_load(
                f"corpus_built_at: {meta['corpus_built_at']}\n")
            quoted = yaml.safe_load(
                f'corpus_built_at: "{meta["corpus_built_at"]}"\n')
            check("unquoted ISO really does parse as a non-str under YAML",
                  not isinstance(unquoted["corpus_built_at"], str),
                  type(unquoted["corpus_built_at"]).__name__)
            check("quoted ISO parses as str under YAML",
                  isinstance(quoted["corpus_built_at"], str))
        except ImportError:
            print("        (PyYAML absent -- skipped the YAML coercion check)")
    finally:
        tt._reset_corpus_state()
        (tt.DIGESTS,
         digest_mod.DIGEST_DIR, digest_mod.CORPUS_SNAPSHOT,
         tt.DB_PATH) = original


def test_digest_from_unstamped_corpus(tmp: Path) -> None:
    """
    An unstamped corpus must produce NO frontmatter — not an empty `---\\n---`.

    The degenerate form happens to read as "no frontmatter" only because the
    closing delimiter never matches the regex. Right answer, wrong reason, and
    it writes garbage into a committed file.
    """
    print("\nDigest from an unstamped corpus")
    import chromadb
    from chromadb.utils import embedding_functions
    db = tmp / "chroma-unstamped"
    client = chromadb.PersistentClient(path=str(db))
    # Built by hand rather than through build_chroma, because the whole point
    # is a collection with NO provenance keys — exactly the shape every corpus
    # on disk had before this change. The embedding function must match the one
    # _do_load passes, or get_collection refuses on a config conflict; the
    # collection persists whichever it was created with.
    collection = client.create_collection(
        name="tenders",
        embedding_function=embedding_functions.
        SentenceTransformerEmbeddingFunction(model_name="all-MiniLM-L6-v2"),
        metadata={"hnsw:space": "cosine"},
    )
    # A document is REQUIRED, not incidental: generate_digest returns early
    # with "No tenders in corpus" on an empty one, which would skip the
    # frontmatter code entirely and pass this test having tested nothing.
    collection.add(
        ids=["TEST-PROV-UNSTAMPED"],
        documents=["A notice in a corpus that predates provenance stamping."],
        metadatas=[{"tender_id": "TEST-PROV-UNSTAMPED", "title": "Fixture",
                    "closing_date": "2027-01-01", "matched_competencies": "",
                    "opportunity_kind": "solicitation"}],
    )

    digests = tmp / "digests-unstamped"
    digests.mkdir(parents=True, exist_ok=True)
    original = (digest_mod.DIGEST_DIR, digest_mod.CORPUS_SNAPSHOT, tt.DB_PATH)
    try:
        tt.DB_PATH = db
        tt._reset_corpus_state()
        digest_mod.DIGEST_DIR = digests
        digest_mod.CORPUS_SNAPSHOT = digests / "corpus-latest.txt"

        content = digest_mod.generate_digest()
        check("no frontmatter block at all",
              not content.startswith("---"), content[:40])
        check("no empty '---\\n---' emitted",
              "---\n---" not in content, content[:60])
    finally:
        tt._reset_corpus_state()
        (digest_mod.DIGEST_DIR, digest_mod.CORPUS_SNAPSHOT,
         tt.DB_PATH) = original


# ---------------------------------------------------------------------------
# 6 — invariants: no verdict field, no impossible state
# ---------------------------------------------------------------------------

def test_no_verdict_field(tmp: Path) -> None:
    """
    There is deliberately no `stale` field, and no threshold anywhere.

    How old is too old is not stateable — the ingest cron is weekly, so two
    days old is normal — and a boolean verdict in a data field is exactly the
    judgement `vault/CLAUDE.md` keeps out of these tools. Asserted rather than
    trusted, because "a boolean would be convenient here" is how it comes back.
    """
    print("\nNo verdict field")
    digests = tmp / "digests-verdict"
    digests.mkdir(parents=True, exist_ok=True)
    (digests / "digest-2026-08-11.md").write_text(
        '---\ncorpus_built_at: "2026-08-11T09:00:00"\n'
        'feed_downloaded_at: "2026-08-11T08:30:00"\n---\n\n# d\n',
        encoding="utf-8")
    original_collection, original_digests = tt._collection, tt.DIGESTS
    tt.DIGESTS = digests
    banned = {"stale", "is_stale", "fresh", "age_days", "corpus_age_days",
              "days_old", "ok", "healthy"}
    try:
        for meta in (
            {"hnsw:space": "cosine"},
            {"corpus_built_at": "2026-08-09T14:27:11"},
            {"corpus_built_at": "2026-08-09T14:27:11",
             "feed_downloaded_at": "2026-08-09T13:52:45"},
        ):
            tt._collection = FakeCollection(meta)
            out = tt._corpus_provenance()
            hit = banned & set(out)
            check(f"no verdict field for {out['state']}", not hit, str(hit))
            check(f"no bool value for {out['state']}",
                  not any(isinstance(v, bool) for v in out.values()),
                  str({k: v for k, v in out.items() if isinstance(v, bool)}))
            # Unreachable by construction — the build stamp is unconditional.
            check(f"no feed-without-build for {out['state']}",
                  not (out.get("feed_downloaded_at")
                       and not out.get("corpus_built_at")))
    finally:
        tt._collection, tt.DIGESTS = original_collection, original_digests


# ---------------------------------------------------------------------------
# 7 — the dossier path carries its own stamp
# ---------------------------------------------------------------------------

def test_dossier_feed_stamp() -> None:
    """
    _dossier_tenders reads .cache/tenders.csv directly, bypassing ChromaDB, so
    it must date what IT read rather than inherit the corpus stamp.
    """
    print("\nDossier feed stamp")
    if not tt._TENDERS_CSV.exists():
        print(f"  SKIP  {tt._TENDERS_CSV} not present")
        return
    section = tt._dossier_tenders("dnd", limit=1)
    if section.get("state") == "no_feed":
        print("  SKIP  feed reported as absent")
        return
    stamp = section.get("feed_downloaded_at")
    check("dossier reports feed_downloaded_at", stamp is not None)
    check("dossier stamp is a str", isinstance(stamp, str), repr(stamp))
    check("dossier stamp matches the CSV it read",
          stamp == datetime.fromtimestamp(
              tt._TENDERS_CSV.stat().st_mtime).isoformat(timespec="seconds"),
          repr(stamp))


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="tender-vault-prov-test-"))
    try:
        test_ingest_stamps(tmp)
        test_stamp_survives_reads(tmp)
        test_states(tmp)
        test_comparison(tmp)
        test_digest_roundtrip(tmp)
        test_digest_from_unstamped_corpus(tmp)
        test_no_verdict_field(tmp)
        test_dossier_feed_stamp()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} provenance check(s) FAILED:")
        for f in FAILURES:
            print(f"  - {f}")
        sys.exit(1)
    print("All provenance tests passed.")


if __name__ == "__main__":
    main()
