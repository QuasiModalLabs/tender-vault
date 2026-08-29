"""
Test that multi-valued end-user entities survive ingest with every value intact.

WHY THIS BYPASSES THE FILTER. One tender can legitimately name several
departments, slash-delimited ("Department of National Defence (DND) /
Department of Transport (TC) / ..."). That field is the one most likely to be
silently collapsed by a future refactor — and a non-empty assertion would not
notice, because a collapsed value is still non-empty.

Worse, it cannot be tested against the built corpus at all: of the 7 multi-valued
rows in .cache/tenders.csv, ZERO currently pass the profile filter (closing date
+ competency + value). A corpus-level check would pass vacuously, confirming
nothing while looking green. So we drive build_chroma directly on those rows and
round-trip them against the source.

Runs with plain Python — no pytest needed:

    python tests/test_end_user_multivalue.py

Requires .cache/tenders.csv (run `python scripts/ingest` once to populate it).
Exit code 0 = all passed.
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import pandas as pd  # noqa: E402

import ingest  # noqa: E402

CACHE = PROJECT_ROOT / ".cache" / "tenders.csv"
EU_COL = "endUserEntitiesName-nomEntitesUtilisateurFinal-eng"


def build_multivalued_corpus(tmp_db: Path):
    """Feed only the slash-delimited rows through build_chroma, filter bypassed."""
    df = pd.read_csv(CACHE, low_memory=False)
    cols = ingest.resolve_columns(
        list(df.columns), ingest.TENDER_COLUMNS, ingest.TENDER_REQUIRED,
        "tests/test_end_user_multivalue.py",
    )

    multi = df[df[EU_COL].fillna("").astype(str).str.contains("/")].copy()
    assert len(multi) >= 1, "no multi-valued end-user rows in the cache to test"

    # The columns filter_tenders would have added, supplied directly.
    multi["_closing"] = pd.to_datetime(
        multi[cols["closing_date"]], errors="coerce", utc=True
    ).dt.tz_localize(None)
    multi["_value"] = None
    multi["_matched"] = [[] for _ in range(len(multi))]
    multi["_unspsc_families"] = [[] for _ in range(len(multi))]
    multi["_date_conflict"] = None
    multi["_jurisdiction"] = [
        ingest.classify_jurisdiction(
            row[cols["contracting_entity"]], row[cols["end_user"]]
        )
        for _, row in multi.iterrows()
    ]
    # Classified for real rather than stubbed: build_chroma reads _kind, and a
    # stub here would let a change to the metadata shape pass this test.
    multi["_kind"] = [
        ingest.classify_notice(
            row[cols["notice_type"]] if cols.get("notice_type") else None,
            row[cols["procurement_category"]] if cols.get("procurement_category") else None,
        )
        for _, row in multi.iterrows()
    ]

    ingest.build_chroma(multi, tmp_db, cols)
    return multi, cols


def test_all_values_survive(multi: pd.DataFrame, cols: dict, tmp_db: Path) -> int:
    import chromadb

    collection = chromadb.PersistentClient(
        path=str(ingest.paths.active_db(tmp_db))).get_collection("tenders")
    stored = {
        m["tender_id"]: m["end_user_entity"]
        for m in collection.get(include=["metadatas"])["metadatas"]
    }

    checked = 0
    for _, row in multi.iterrows():
        tid = str(row[cols["tender_id"]])
        assert tid in stored, f"{tid}: row dropped from the corpus entirely"
        raw_parts = [p.strip() for p in str(row[EU_COL]).split("/") if p.strip()]
        got_parts = [p.strip() for p in stored[tid].split("/") if p.strip()]

        # Three distinct failure modes, each invisible to a non-empty check:
        assert "/" in stored[tid], f"{tid}: delimiter lost, value collapsed to one"
        assert len(got_parts) >= 2, (
            f"{tid}: kept {len(got_parts)} department(s), expected >= 2"
        )
        assert got_parts == raw_parts, (
            f"{tid}: values altered\n  source: {raw_parts}\n  stored: {got_parts}"
        )
        checked += 1
    return checked


def test_fields_are_distinct(multi: pd.DataFrame, cols: dict, tmp_db: Path) -> None:
    """The two entity fields must be stored separately, never merged."""
    import chromadb

    collection = chromadb.PersistentClient(
        path=str(ingest.paths.active_db(tmp_db))).get_collection("tenders")
    metadatas = collection.get(include=["metadatas"])["metadatas"]
    for m in metadatas:
        assert "contracting_entity" in m, "contracting_entity missing from metadata"
        assert "end_user_entity" in m, "end_user_entity missing from metadata"
        assert m["end_user_entity"] not in m["contracting_entity"] or not m["end_user_entity"], (
            f"{m['tender_id']}: end user appears inside contracting entity — merged?"
        )
    differing = [m for m in metadatas if m["contracting_entity"] != m["end_user_entity"]]
    assert differing, (
        "no row has a contracting entity different from its end user — the two "
        "fields are the whole point; a central buyer purchasing on behalf of "
        "another department must be distinguishable"
    )


def main():
    if not CACHE.exists():
        print(f"SKIP: {CACHE} not present. Run python scripts/ingest first.")
        return
    tmp_root = Path(tempfile.mkdtemp(prefix="tender-vault-eu-test-"))
    try:
        tmp_db = tmp_root / "chroma"
        multi, cols = build_multivalued_corpus(tmp_db)
        checked = test_all_values_survive(multi, cols, tmp_db)
        test_fields_are_distinct(multi, cols, tmp_db)
        print(f"All multi-value tests passed ({checked} rows round-tripped intact).")
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)


if __name__ == "__main__":
    main()
