"""
Temporal invariants for the lobbying registrations layer.

Runs with plain Python — no pytest needed:

    python tests/test_registrations_temporal.py

Exit code 0 = all passed. Skips the database checks cleanly when
data/registrations.db has not been built, so a fresh clone without the
hand-downloaded archive still passes.

WHAT THIS GUARDS. This database exists in versioned form because flattening it
would be wrong in the majority of amended cases: of 27,704 registrations with
more than one version, 14,779 — 53% — change which institutions they name
between versions. One real chain drops from eight institutions to one, so a
flattened read asserts it never targeted the other seven.

Versioning only helps if the query side honours it, and there are exactly three
ways it silently stops honouring it:

  * an as-of parameter acquires a default, so callers who never thought about
    time get the present tense for free — the flat behaviour returning through
    a different door;
  * the interval test is written closed instead of half-open, so a version and
    its successor both match on the changeover date and every count doubles;
  * a null end date is read as "expired" or "unknown" rather than "in force",
    which silently drops the 4.3% of versions that are current.

Each has a test below. The fourth guard is that output carries the version id,
because a time-ordered claim nobody can trace back to a version is not
checkable.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import conftest  # noqa: E402,F401  (vault redirect — see its docstring)
import crosswalk as cw  # noqa: E402
import registrations_ingest as ri  # noqa: E402
import tender_tools as tt  # noqa: E402

DB = ROOT / "data" / "registrations.db"
PASSED = FAILED = SKIPPED = 0


def check(cond: bool, label: str) -> None:
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  ok    {label}")
    else:
        FAILED += 1
        print(f"  FAIL  {label}")


# ---------------------------------------------------------------------------
# No database needed
# ---------------------------------------------------------------------------

def test_as_of_has_no_default():
    """
    THE guard on the whole design. A default meaning "latest" reintroduces the
    flattened behaviour for every caller who never considered time — and those
    are exactly the callers who go on to write a time-ordered sentence.
    """
    result = tt.cmd_registrations_signals(SimpleNamespace(as_of=None))
    check("error" in result, "a missing as-of is refused rather than defaulted")
    check("no default" in result.get("error", "").lower(),
          "the refusal says there is no default")
    check("53%" in result.get("why", ""),
          "the refusal explains why, with the measurement behind it")
    check("today" in result.get("how", "").lower(),
          "the refusal names the explicit way to ask for current state")


def test_as_of_clause_is_half_open():
    """
    A closed interval double-counts on every changeover date. The predicate is
    exported as one function so no caller writes its own; this asserts the
    shape that function produces.
    """
    sql = ri.as_of_clause("v")
    check("v.effective_date <= :as_of" in sql,
          "the lower bound is inclusive of the effective date")
    check("v.end_date IS NULL" in sql,
          "a null end date is treated as still in force, not as expired")
    check(":as_of < v.end_date" in sql,
          "the upper bound is EXCLUSIVE — half-open, so a version and its "
          "successor never both match a changeover date")
    check("<=  :as_of < " not in sql.replace(" ", "  "),
          "the upper bound is not written as <=")


def test_reg_base_and_version_seq_split_the_number():
    """Chain identity comes from REG_NUM, not from the prev pointer — which is
    what lets a broken pointer stay harmless."""
    check(ri.reg_base("777408-4993-10") == "777408-4993",
          "reg_base drops the version suffix")
    check(ri.version_seq("777408-4993-10") == 10, "version_seq reads the suffix")
    check(ri.version_seq("777408-4993-x") is None,
          "a non-numeric suffix reads as None rather than raising")
    check(ri.reg_base("") == "", "an empty registration number is handled")


# ---------------------------------------------------------------------------
# Database checks
# ---------------------------------------------------------------------------

def test_open_ended_versions_are_latest_in_their_chain(con):
    """
    The measured fact the null-end-date semantics rest on. If a null end date
    ever appears mid-chain, "still in force" stops being true and the as-of
    predicate starts returning two versions of one registration.
    """
    bad = con.execute("""
        SELECT COUNT(*) FROM registration_versions a
        WHERE a.end_date IS NULL AND EXISTS (
            SELECT 1 FROM registration_versions b
            WHERE b.reg_base = a.reg_base
              AND b.effective_date > a.effective_date)
    """).fetchone()[0]
    check(bad == 0,
          f"every open-ended version is the latest in its chain ({bad} mid-chain)")


def test_no_two_versions_of_one_registration_are_in_force_at_once(con):
    """
    The half-open interval, checked against the data rather than the string.
    Two versions of one registration in force on the same date means every
    per-registration count on that date is inflated.
    """
    for as_of in ("2010-01-01", "2019-06-01", "2026-05-01"):
        dupes = con.execute(f"""
            SELECT COUNT(*) FROM (
                SELECT v.reg_base FROM registration_versions v
                WHERE {ri.as_of_clause('v')}
                GROUP BY v.reg_base HAVING COUNT(*) > 1)
        """, {"as_of": as_of}).fetchone()[0]
        check(dupes == 0,
              f"as-of {as_of}: no registration has two versions in force "
              f"({dupes} overlapping)")


def test_as_of_actually_changes_the_answer(con):
    """
    A versioned store that returns the same rows whatever date you ask is a
    flattened store with extra steps.
    """
    counts = {}
    for as_of in ("2010-01-01", "2019-06-01", "2026-05-01"):
        counts[as_of] = con.execute(
            f"SELECT COUNT(*) FROM registration_versions v "
            f"WHERE {ri.as_of_clause('v')}", {"as_of": as_of}).fetchone()[0]
    check(len(set(counts.values())) > 1,
          f"different as-of dates return different populations ({counts})")


def test_institution_lists_really_do_change_between_versions(con):
    """
    The measurement that justifies storing versions at all. If this ever comes
    back at zero, the versioning is ceremony and the docstring is wrong.
    """
    per_version = con.execute("""
        SELECT v.reg_base, v.reg_id, COUNT(*) n
        FROM registration_versions v
        JOIN registration_institutions i ON i.reg_id = v.reg_id
        GROUP BY v.reg_base, v.reg_id
    """).fetchall()
    by_base: dict[str, set] = {}
    for base, _rid, n in per_version:
        by_base.setdefault(base, set()).add(n)
    varying = sum(1 for s in by_base.values() if len(s) > 1)
    check(varying > 0,
          f"institution counts vary across versions for {varying:,} registrations "
          f"— flattening would delete that difference")


def test_dept_key_only_on_departments(con):
    """Same rule as the communications layer: a canonical key is only set where
    naming a department is a true statement."""
    leaked = con.execute(
        "SELECT COUNT(*) FROM registration_institutions "
        "WHERE dept_key IS NOT NULL AND institution_kind != ?",
        (ri.KIND_DEPARTMENT,)).fetchone()[0]
    check(leaked == 0, f"dept_key appears only on department rows ({leaked} leaked)")
    registry = set(cw.load_aliases())
    used = {r[0] for r in con.execute(
        "SELECT DISTINCT dept_key FROM registration_institutions "
        "WHERE dept_key IS NOT NULL")}
    check(used <= registry,
          f"all {len(used)} department keys are in the registry "
          f"(strays: {sorted(used - registry)[:3] or 'none'})")


def test_broken_chains_are_counted_not_dropped(con):
    """
    A missing predecessor pointer must never remove a version from the corpus.
    The pointer is provenance; the interval is what queries read.
    """
    meta = dict(con.execute("SELECT key, value FROM meta").fetchall())
    check("chain_breaks_midchain" in meta,
          f"mid-chain pointer breaks are counted in meta "
          f"({meta.get('chain_breaks_midchain')})")
    stored = con.execute("SELECT COUNT(*) FROM registration_versions").fetchone()[0]
    check(stored == int(meta["versions"]),
          f"every version is stored, breaks included ({stored:,})")
    orphan_kept = con.execute(
        "SELECT COUNT(*) FROM registration_versions WHERE prev_reg_id IS NULL"
    ).fetchone()[0]
    check(orphan_kept > 0 and stored > orphan_kept,
          "versions without a predecessor pointer are present in the corpus")


def test_output_carries_the_version_id(con):
    """
    A time-ordered claim that cannot be traced to the version it rests on is
    not checkable, which is the whole reason this data is stored by version.
    """
    result = tt.cmd_registrations_signals(
        SimpleNamespace(as_of="2026-05-01", department=None, client=None,
                        vendor=None, limit=3))
    rows = result.get("registrations", [])
    check(bool(rows), f"a populated as-of query returns rows ({len(rows)})")
    for row in rows:
        check(isinstance(row.get("version_id"), int),
              f"row for {row.get('client','?')[:28]!r} carries version_id")
        check("effective" in row and "still_in_force" in row,
              "row carries the window that made it match")
    check(result.get("as_of") == "2026-05-01",
          "the response echoes the as-of it answered for")


def test_provenance_is_recorded(con):
    """A hand-acquired archive carries no request log, so the database must
    carry its own — including which archive a given build came from."""
    meta = dict(con.execute("SELECT key, value FROM meta").fetchall())
    for key in ("source_url", "source_sha256", "source_acquired", "archive_members"):
        check(bool(meta.get(key)), f"meta records {key}")
    check(len(meta.get("source_sha256", "")) == 64, "the hash is a full sha256")
    check("registrations_enregistrements" in meta.get("source_url", ""),
          "the recorded URL is the registrations archive, not communications")


def main() -> int:
    global SKIPPED
    print(__doc__.strip().splitlines()[0])
    print()

    print("Checks needing no database:")
    test_as_of_has_no_default()
    test_as_of_clause_is_half_open()
    test_reg_base_and_version_seq_split_the_number()

    if not DB.exists():
        SKIPPED = 1
        print(f"\n  SKIP  {DB.relative_to(ROOT)} not built — database checks skipped.")
        print("        The official archive is acquired out-of-band; see "
              "docs/SETUP.md, then:")
        print("          python scripts/registrations_ingest.py --source <zip>")
        print(f"\n{PASSED} passed, {FAILED} failed, {SKIPPED} skipped")
        return 1 if FAILED else 0

    con = sqlite3.connect(DB)
    try:
        print("\nDatabase checks:")
        test_open_ended_versions_are_latest_in_their_chain(con)
        test_no_two_versions_of_one_registration_are_in_force_at_once(con)
        test_as_of_actually_changes_the_answer(con)
        test_institution_lists_really_do_change_between_versions(con)
        test_dept_key_only_on_departments(con)
        test_broken_chains_are_counted_not_dropped(con)
        test_output_carries_the_version_id(con)
        test_provenance_is_recorded(con)
    finally:
        con.close()

    print(f"\n{PASSED} passed, {FAILED} failed, {SKIPPED} skipped")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
