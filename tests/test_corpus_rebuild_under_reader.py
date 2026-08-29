"""
A rebuild must succeed while another process holds the live corpus open.

THE BUG THIS EXISTS TO PREVENT. `scripts/mcp_server.py` keeps a ChromaDB client
open for the life of a Claude Desktop session. The rebuild this replaced began
by renaming `chroma_db/` out of the way, and Windows refuses to rename or
delete a directory while any process holds a handle inside it. On 2026-08-28
that produced:

    PermissionError: [WinError 5] Access is denied:
        'C:\\code\\tender-vault\\chroma_db' -> 'C:\\code\\tender-vault\\chroma_db.old'

after the feed had been downloaded and every notice embedded — so the run cost
its full price, advanced `.cache/tenders.csv`, and left the corpus untouched
behind it. That is exactly the feed-newer-than-corpus mismatch the provenance
block exists to report, manufactured by the ingest itself.

WHY THIS TEST IS PLATFORM-HONEST. On Linux the old code passes this test:
rename() there succeeds with open descriptors, which is why CI never caught it.
The assertions below are written so they hold on both platforms — what is
checked is that the previously-live build directory is still present and
readable afterwards, and that the pointer moved to a new one. Under the old
strategy that is false on every platform: the old directory is renamed away
whether or not the OS allowed it.

Run: python tests/test_corpus_rebuild_under_reader.py
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

import conftest  # noqa: F401  — makes the real vault unreachable at import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import pandas as pd  # noqa: E402

from ingest import paths as ingest_paths  # noqa: E402
from ingest.corpus import build_chroma, stored_identity  # noqa: E402
from ingest.schema import TENDER_COLUMNS  # noqa: E402

PASS = FAIL = 0


def check(cond: bool, label: str) -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok    {label}")
    else:
        FAIL += 1
        print(f"  FAIL  {label}")


def _frame(n: int, tag: str) -> pd.DataFrame:
    """A minimal corpus. Reference numbers must be non-blank to survive stage 7."""
    return pd.DataFrame({
        "referenceNumber-numeroReference": [f"{tag}-{i}" for i in range(n)],
        "title-titre-eng": [f"{tag} notice {i}" for i in range(n)],
        "tenderDescription-descriptionAppelOffres-eng":
            [f"cloud modernization work, build {tag}, item {i}" for i in range(n)],
        "contractingEntityName-nomEntitContractante-eng":
            ["Shared Services Canada"] * n,
        "endUserEntitiesName-nomEntitesUtilisateurFinal-eng":
            ["Shared Services Canada"] * n,
        "tenderClosingDate-appelOffresDateCloture": ["2026-12-31"] * n,
        "noticeType-avisType-eng": ["Request for Proposal"] * n,
        "procurementCategory-categorieApprovisionnement": ["SRV"] * n,
        "unspsc": ["81111500"] * n,
        # Derived columns that filter_tenders adds before build_chroma is ever
        # called. Supplied here rather than run through the whole filter, so
        # this test stays about the rebuild and not about the funnel.
        "_closing": pd.to_datetime(["2026-12-31"] * n),
        "_matched": [["cloud"]] * n,
        "_unspsc_families": [["8111"]] * n,
        "_kind": [{"opportunity_kind": "solicitation",
                   "kind_basis": "procurement_category_residual"}] * n,
        "_jurisdiction": [{"jurisdiction": "federal", "org_keys": "ssc"}] * n,
    })


# The real schema, resolved the way cli.py resolves it, so this test breaks if
# the feed's column contract changes rather than drifting away from it quietly.
COLS = {key: names[0] for key, names in TENDER_COLUMNS.items()}


def test_rebuild_while_a_reader_holds_the_corpus_open() -> None:
    print("\nRebuild while a reader holds the live corpus open")
    import chromadb

    tmp = Path(tempfile.mkdtemp(prefix="corpus-rebuild-"))
    container = tmp / "chroma_db"
    try:
        build_chroma(_frame(3, "first"), container, COLS, identity={"v": "1"})

        first = ingest_paths.active_db(container)
        check(first != container, "the first build lands in its own directory")
        check((container / "CURRENT").exists(), "a CURRENT pointer names it")
        check(stored_identity(container) == {"v": "1"},
              "stored_identity resolves through the pointer")

        # THE READER. Held open across the rebuild exactly as the MCP server
        # holds it across a Claude Desktop session.
        reader = chromadb.PersistentClient(path=str(first))
        held = reader.get_collection("tenders")
        check(held.count() == 3, "the reader sees the first build's 3 notices")

        # THE REBUILD. This is the call that used to raise PermissionError.
        build_chroma(_frame(5, "second"), container, COLS, identity={"v": "2"})

        second = ingest_paths.active_db(container)
        check(second != first, "the pointer moved to a new build")
        check(second.exists(), "the new build is on disk")
        check(stored_identity(container) == {"v": "2"},
              "the identity file now describes the new build")

        # The old directory must still be THERE and READABLE. This is the
        # assertion the old rename-aside strategy fails on every platform.
        check(first.exists(),
              "the build the reader holds was not renamed or deleted")
        check(held.count() == 3,
              "the open reader still answers from the build it opened")

        fresh = chromadb.PersistentClient(path=str(second)).get_collection("tenders")
        check(fresh.count() == 5, "a new reader sees the new build's 5 notices")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_legacy_layout_still_loads() -> None:
    """A corpus built before the container layout has no pointer and must work."""
    print("\nA pre-pointer corpus still resolves")
    tmp = Path(tempfile.mkdtemp(prefix="corpus-legacy-"))
    try:
        legacy = tmp / "chroma_db"
        legacy.mkdir()
        (legacy / "chroma.sqlite3").write_text("not a real store", encoding="utf-8")
        check(ingest_paths.active_db(legacy) == legacy,
              "with no CURRENT, the container itself is the corpus")

        (legacy / "CURRENT").write_text("build-that-was-swept\n", encoding="utf-8")
        check(ingest_paths.active_db(legacy) == legacy,
              "a pointer naming a missing build falls back rather than failing")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    test_rebuild_while_a_reader_holds_the_corpus_open()
    test_legacy_layout_still_loads()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
