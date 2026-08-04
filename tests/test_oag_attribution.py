"""
Invariants for OAG department attribution, checked against the built database.

Runs with plain Python — no pytest needed:

    python tests/test_oag_attribution.py

Exit code 0 = all passed. Skips cleanly when data/oag.db has not been built, so
a fresh clone without a network fetch still passes.

WHAT THIS GUARDS. The old extractor matched a hardcoded list by substring and
fell back to a regex, producing 4 junk values including two territorial
departments in a federal dataset. The structural fix is that a department is now
a CANONICAL KEY, so junk is not representable — test_every_department_is_a_
registry_key is the assertion that keeps it that way.

The rest are the specific precision failures found while rebuilding: records
that resolved to a department they had nothing to do with, and populations that
were being counted as a coverage gap when they correctly have no department.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import crosswalk as cw  # noqa: E402

DB = ROOT / "data" / "oag.db"
PASSED = FAILED = SKIPPED = 0


def check(cond: bool, label: str) -> None:
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  ok    {label}")
    else:
        FAILED += 1
        print(f"  FAIL  {label}")


def test_every_department_is_a_registry_key(con):
    """
    THE structural guarantee. A dept_key that is not in org_aliases.yaml means
    something invented one, which is how "Department of Health and Social
    Services provided timely" — a Yukon department with the sentence still
    attached — ended up in a federal dataset.
    """
    registry = set(cw.load_aliases())
    used = {r[0] for r in con.execute("SELECT DISTINCT dept_key FROM audit_departments")}
    check(used <= registry,
          f"all {len(used)} department keys are in the registry "
          f"(strays: {sorted(used - registry) or 'none'})")
    check("auditor-general" not in used,
          "the OAG never appears as an audited department")


def test_the_auditors_own_paperwork_has_no_department(con):
    """
    OAG quarterly financial reports cite Treasury Board accounting policy, and a
    plain text scan attributed all 10 of them to TBS. They are the auditor's own
    books, and audit nobody.
    """
    leaked = con.execute("""
        SELECT COUNT(*) FROM audits a
        WHERE a.record_class = 'oag_own_admin'
          AND EXISTS (SELECT 1 FROM audit_departments d WHERE d.oag_id = a.oag_id)
    """).fetchone()[0]
    total = con.execute(
        "SELECT COUNT(*) FROM audits WHERE record_class = 'oag_own_admin'").fetchone()[0]
    check(leaked == 0,
          f"none of the {total} OAG admin documents carries a department "
          f"({leaked} leaked)")


def test_non_federal_audits_are_refused_not_guessed(con):
    """
    Territorial governments and Crown corporations have no federal department by
    construction, so they must be REFUSED with a reason rather than resolved to
    whichever federal name their text happens to contain.
    """
    for cls in ("territorial", "crown_or_other_entity"):
        rows = con.execute(
            "SELECT COUNT(*), SUM(attribution_status = 'UNRESOLVED_NOT_FEDERAL_EXEC') "
            "FROM audits WHERE record_class = ?", (cls,)).fetchone()
        total, marked = rows[0], rows[1] or 0
        check(total > 0 and total == marked,
              f"all {total} {cls} records say why they have no department ({marked})")


def test_multi_department_audits_keep_every_department(con):
    """
    ArriveCAN audited three organizations together. The old single column kept
    whichever name matched first and dropped the other two.
    """
    row = con.execute("""
        SELECT oag_id FROM audits
        WHERE record_class = 'federal_audit' AND title LIKE '%ArriveCAN%'
    """).fetchone()
    if not row:
        print("  skip  ArriveCAN not in this build")
        return
    got = {r[0] for r in con.execute(
        "SELECT dept_key FROM audit_departments WHERE oag_id = ?", (row[0],))}
    check(got == {"cbsa", "phac", "pspc"},
          f"ArriveCAN attaches to exactly cbsa, phac, pspc (got {sorted(got)})")

    multi = con.execute("""
        SELECT COUNT(*) FROM (
            SELECT d.oag_id FROM audit_departments d
            JOIN audits a ON a.oag_id = d.oag_id
            WHERE a.record_class = 'federal_audit' AND d.method = 'direct'
            GROUP BY d.oag_id HAVING COUNT(*) > 1)
    """).fetchone()[0]
    check(multi >= 40,
          f"multi-department audits are the normal case, not an edge case ({multi})")


def test_status_and_edges_never_disagree(con):
    """
    A record marked RESOLVED with no edges, or unresolved with edges, means the
    status and the join table were written from different decisions.
    """
    contradictions = con.execute("""
        SELECT COUNT(*) FROM audits a
        WHERE (a.attribution_status = 'RESOLVED') !=
              (EXISTS (SELECT 1 FROM audit_departments d WHERE d.oag_id = a.oag_id))
    """).fetchone()[0]
    check(contradictions == 0,
          f"attribution_status agrees with the join table everywhere ({contradictions} disagree)")

    orphans = con.execute("""
        SELECT COUNT(*) FROM audit_departments d
        WHERE NOT EXISTS (SELECT 1 FROM audits a WHERE a.oag_id = d.oag_id)
    """).fetchone()[0]
    check(orphans == 0, f"no edge references a missing record ({orphans} orphans)")


def test_inherited_edges_carry_their_provenance(con):
    """
    An inherited department must say where it came from, or a dossier cannot
    show direct findings and exclude a five-report hearing bundle — which is the
    entire reason the method lives on the edge rather than on the record.
    """
    bad = con.execute("""
        SELECT COUNT(*) FROM audit_departments
        WHERE method <> 'direct' AND (n_parent_reports IS NULL OR n_parent_reports < 1)
    """).fetchone()[0]
    check(bad == 0, f"every inherited edge records how many reports it spans ({bad} do not)")

    direct_polluted = con.execute(
        "SELECT COUNT(*) FROM audit_departments "
        "WHERE method = 'direct' AND n_parent_reports IS NOT NULL").fetchone()[0]
    check(direct_polluted == 0,
          f"direct edges carry no parent-report count ({direct_polluted} do)")

    no_evidence = con.execute(
        "SELECT COUNT(*) FROM audit_departments WHERE evidence IS NULL OR evidence = ''"
    ).fetchone()[0]
    check(no_evidence == 0,
          f"every edge names the string that produced it ({no_evidence} do not)")


def test_briefings_never_resolve_themselves(con):
    """
    A briefing package names no department of its own. Scanning its text is how
    the Standing Committee on Canadian Heritage ends up owning an audit of ISED
    and the CRTC, so every department on one must be inherited.
    """
    self_resolved = con.execute("""
        SELECT COUNT(*) FROM audit_departments d
        JOIN audits a ON a.oag_id = d.oag_id
        WHERE a.record_class = 'committee_briefing' AND d.method = 'direct'
    """).fetchone()[0]
    check(self_resolved == 0,
          f"no briefing package resolves its own text ({self_resolved} do)")


def test_vendor_audits_stay_retrievable(con):
    """
    An audit into a supplier spans too many departments to attach to one, so
    without vendor_focus it would sit unreachable behind a department filter.
    """
    rows = con.execute(
        "SELECT vendor_focus, title FROM audits WHERE vendor_focus <> ''").fetchall()
    names = {v for v, _ in rows}
    check(any("GCStrategies" in v for v in names),
          f"the GCStrategies audit is tagged ({sorted(names)})")
    check(any("McKinsey" in v for v in names), "the McKinsey audit is tagged")
    # A corporate suffix alone is not a vendor: Atomic Energy of Canada Limited
    # is a Crown corporation named as an AUDITED ENTITY, and matched before the
    # contracting-relationship requirement was added.
    check(not any("Atomic Energy" in v for v in names),
          "Atomic Energy of Canada Limited is not tagged as a supplier")


def test_coverage_is_recorded_per_population(con):
    """
    Direct and inherited attribution are different evidence, and most of this
    corpus correctly has no department — so the build must record them
    separately rather than leaving a single blended number to be quoted.
    """
    meta = dict(con.execute("SELECT key, value FROM meta").fetchall())
    for key in ("federal_audits", "federal_audits_resolved",
                "briefings", "briefings_resolved", "department_edges"):
        check(key in meta, f"meta records {key} ({meta.get(key)})")

    total = int(meta.get("federal_audits", 0))
    got = int(meta.get("federal_audits_resolved", 0))
    check(total and got / total > 0.85,
          f"federal-audit attribution is {got}/{total} "
          f"({100 * got / total:.0f}%) — the headline number")


def main() -> int:
    if not DB.exists():
        print(f"no {DB.name}; run python scripts/oag_ingest.py — skipping")
        return 0
    con = sqlite3.connect(DB)
    try:
        con.execute("SELECT 1 FROM audit_departments LIMIT 1")
    except sqlite3.OperationalError:
        print("oag.db predates the join table; rebuild with oag_ingest.py — skipping")
        return 0

    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(f"\n{name}")
            fn(con)
    con.close()

    print(f"\n{PASSED} passed, {FAILED} failed, {SKIPPED} skipped")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
