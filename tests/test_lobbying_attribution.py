"""
Invariants for lobbying institution attribution, checked against the built DB.

Runs with plain Python — no pytest needed:

    python tests/test_lobbying_attribution.py

Exit code 0 = all passed. Skips cleanly when data/lobbying.db has not been
built, so a fresh clone without the hand-downloaded zip still passes.

WHAT THIS GUARDS. The published INSTITUTION column mixes five populations that
look alike as strings and are nothing alike as evidence: departments, ministers'
offices, parliamentarians, Crown corporations, and a free-text 'Other'. The
largest single value in the file is "House of Commons" — members of parliament
are designated public office holders, and they have no procurement authority
whatever. A pipeline that resolved institution names by similarity would attach
a third of this corpus to departments that were never in the room.

The structural fix is that `dept_key` is a CANONICAL KEY and is populated only
for `institution_kind = 'department'`, so the wrong attribution is not
representable. test_every_department_key_is_in_the_registry and
test_dept_key_only_on_departments are the assertions that keep it that way.

The rest are the specific defects found while building this: a subset count
that could exceed the total it was a subset of, and the two populations that
correctly have no department and must not be read as a coverage gap.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import conftest  # noqa: E402,F401  (vault redirect — see its docstring)
import crosswalk as cw  # noqa: E402
import lobbying_ingest as li  # noqa: E402

DB = ROOT / "data" / "lobbying.db"
PASSED = FAILED = SKIPPED = 0


def check(cond: bool, label: str) -> None:
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  ok    {label}")
    else:
        FAILED += 1
        print(f"  FAIL  {label}")


def test_every_department_key_is_in_the_registry(con):
    """
    THE structural guarantee. A dept_key outside org_aliases.yaml means
    something invented an identity, and every cross-source join downstream —
    dossier, program-signals, expiring-contracts — is keyed on this string.
    """
    registry = set(cw.load_aliases())
    used = {r[0] for r in con.execute(
        "SELECT DISTINCT dept_key FROM communication_dpohs "
        "WHERE dept_key IS NOT NULL")}
    check(used <= registry,
          f"all {len(used)} department keys are in the registry "
          f"(strays: {sorted(used - registry) or 'none'})")


def test_dept_key_only_on_departments(con):
    """
    A key on any other population would silently widen every department filter.
    The parliamentary rows are the hazard: they are the largest population in
    the file, and attaching them to a department would put an MP's meeting into
    a department's dossier.
    """
    leaked = con.execute(
        "SELECT institution_kind, COUNT(*) FROM communication_dpohs "
        "WHERE dept_key IS NOT NULL AND institution_kind != ? GROUP BY 1",
        (li.KIND_DEPARTMENT,)).fetchall()
    check(not leaked, f"dept_key appears only on '{li.KIND_DEPARTMENT}' rows "
                      f"(leaked: {leaked or 'none'})")
    missing = con.execute(
        "SELECT COUNT(*) FROM communication_dpohs "
        "WHERE institution_kind = ? AND (dept_key IS NULL OR dept_key = '')",
        (li.KIND_DEPARTMENT,)).fetchone()[0]
    check(missing == 0,
          f"every '{li.KIND_DEPARTMENT}' row carries a key ({missing} without)")


def test_every_row_is_classified(con):
    """
    The populations are exhaustive by construction. An unclassified row would
    be invisible to every filter at once — neither a department nor explicitly
    not one.
    """
    kinds = {r[0] for r in con.execute(
        "SELECT DISTINCT institution_kind FROM communication_dpohs")}
    known = {li.KIND_DEPARTMENT, li.KIND_DEPARTMENT_UNMAPPED,
             li.KIND_MINISTER_OFFICE, li.KIND_PARLIAMENT,
             li.KIND_OTHER_FEDERAL, li.KIND_UNSPECIFIED}
    check(kinds <= known, f"every institution_kind is a declared population "
                          f"(unknown: {sorted(kinds - known) or 'none'})")
    blank = con.execute(
        "SELECT COUNT(*) FROM communication_dpohs "
        "WHERE institution_kind IS NULL OR institution_kind = ''").fetchone()[0]
    check(blank == 0, f"no office-holder row is unclassified ({blank} blank)")


def test_parliament_is_never_a_department(con):
    """
    The specific misattribution this classification exists to prevent. "House of
    Commons" is a third of the office-holder rows, and an MP is not a buyer.
    """
    for name in ("House of Commons", "Senate of Canada",
                 "Members of the House of Commons"):
        rows = con.execute(
            "SELECT DISTINCT institution_kind FROM communication_dpohs "
            "WHERE institution = ?", (name,)).fetchall()
        if not rows:
            continue
        check([r[0] for r in rows] == [li.KIND_PARLIAMENT],
              f"{name!r} classifies as parliament, not a department")


def test_ministers_offices_do_not_resolve_to_their_portfolio(con):
    """
    "Rural Economic Development (Minister's Office)" contains a portfolio name.
    Resolving it would attribute a meeting with political staff to the
    department's officials, who were precisely not there — the same error
    org_resolve refuses for parliamentary committee names.
    """
    rows = con.execute(
        "SELECT institution, dept_key FROM communication_dpohs "
        "WHERE institution_kind = ? AND dept_key IS NOT NULL LIMIT 5",
        (li.KIND_MINISTER_OFFICE,)).fetchall()
    check(not rows, f"no minister's office carries a department key ({rows})")


def test_alias_table_points_at_real_entries():
    """
    The local OCL-spelling map is hand-written, and a key that no longer exists
    in the registry would drop its institution into `other_federal` looking like
    an ordinary coverage gap rather than a broken mapping.
    """
    registry = set(cw.load_aliases())
    strays = sorted(set(li.LOBBYING_INSTITUTION_ALIASES.values()) - registry)
    check(not strays,
          f"LOBBYING_INSTITUTION_ALIASES targets exist in the registry "
          f"(strays: {strays or 'none'})")


def test_unmapped_departments_stay_unmapped():
    """
    The split-mandate predecessors are recorded as ambiguous ON PURPOSE. If one
    of them ever gains an entry in the alias table, the ambiguity has been
    resolved by guessing rather than by evidence.
    """
    overlap = sorted(set(li.UNMAPPED_DEPARTMENTS) &
                     set(li.LOBBYING_INSTITUTION_ALIASES))
    check(not overlap,
          f"no institution is both unmapped and aliased (overlap: {overlap or 'none'})")


def test_amendments_do_not_double_count(con):
    """
    A communication that is amended points at the one it replaces. The export
    drops the replaced version, so amendments are provenance rather than
    duplicates — but that is a property of the published file, not a guarantee.
    If superseded ids start appearing as rows, every count in every query is
    inflated and this is where it surfaces.
    """
    both = con.execute("""
        SELECT COUNT(*) FROM communications a
        JOIN communications b ON b.comlog_id = a.amends_comlog_id
    """).fetchone()[0]
    check(both == 0,
          f"no superseded communication is still present as its own row "
          f"({both} found — counts would be inflated)")


def test_subject_other_text_only_on_other(con):
    """
    OTHER_SUBJ_MATTER repeats the subject, or holds its FRENCH translation, on
    every row where the filer did NOT choose 'Other'. Storing that would put
    French labels in a column callers filter in English.
    """
    stray = con.execute(
        "SELECT COUNT(*) FROM communication_subjects "
        "WHERE other_subject != '' AND lower(subject) != 'other'").fetchone()[0]
    check(stray == 0, f"free-text subject is kept only for 'Other' ({stray} strays)")


def test_procurement_subset_never_exceeds_its_total(con):
    """
    The dossier reports procurement-subject communications as a SUBSET of a
    client's total with this department. Both counts cross the office-holder
    join, which emits a row per person, so a SUM over it counts one meeting
    several times and reports a subset larger than its own total. Measured
    against the real data, this is the query that got it wrong.
    """
    import tender_tools as tt
    from types import SimpleNamespace

    keys = [r[0] for r in con.execute(
        "SELECT dept_key, COUNT(*) c FROM communication_dpohs "
        "WHERE dept_key IS NOT NULL GROUP BY 1 ORDER BY c DESC LIMIT 5")]
    violations = []
    for key in keys:
        section = tt._dossier_lobbying(key, 25)
        for row in section.get("top_clients", []):
            if row["on_government_procurement"] > row["communications"]:
                violations.append((key, row["client"],
                                   row["on_government_procurement"],
                                   row["communications"]))
    check(not violations,
          f"procurement subset <= total for every client in the top 5 "
          f"departments ({violations[:3] or 'none'})")


def test_client_norm_matches_the_contracts_normalizer(con):
    """
    The client/vendor join is only real if both sides normalize identically.
    The ingest imports the contracts normalizer rather than copying it; this
    re-derives it over stored rows so a future divergence fails here instead of
    quietly returning nothing.
    """
    from contracts_ingest import normalize_vendor
    rows = con.execute(
        "SELECT client_name, client_norm FROM communications "
        "WHERE client_name != '' LIMIT 500").fetchall()
    bad = [(n, stored) for n, stored in rows if normalize_vendor(n) != stored]
    check(not bad, f"client_norm matches normalize_vendor on {len(rows)} rows "
                   f"(mismatches: {bad[:3] or 'none'})")


# ---------------------------------------------------------------------------
# Acquisition — the archive is an out-of-band INPUT ARTIFACT
# ---------------------------------------------------------------------------
# These need no database and no network. They cover the handoff itself: the
# official zip is fetched by a human because lobbycanada.gc.ca answers 403
# (`cf-mitigated: challenge`) to every plain HTTP client, so the failure modes
# worth guarding are the ones that handoff introduces — a missing file, a saved
# challenge page renamed .zip, and a build whose input cannot be identified
# afterwards.

def _fixture_archive(tmp: Path, members: dict[str, str]) -> Path:
    """A minimal but real zip, so the checks exercise the actual code path."""
    import zipfile
    path = tmp / "communications_ocl_cal.zip"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, body in members.items():
            zf.writestr(name, body)
    return path


def _valid_members() -> dict[str, str]:
    """One row per file, with the real published headers."""
    return {
        "Communication_PrimaryExport.csv":
            "COMLOG_ID,CLIENT_ORG_CORP_NUM,EN_CLIENT_ORG_CORP_NM_AN,"
            "FR_CLIENT_ORG_CORP_NM,REGISTRANT_NUM_DECLARANT,RGSTRNT_LAST_NM_DCLRNT,"
            "RGSTRNT_1ST_NM_PRENOM_DCLRNT,COMM_DATE,REG_TYPE_ENR,"
            "SUBMISSION_DATE_SOUMISSION,POSTED_DATE_PUBLICATION,"
            "PREV_COMLOG_ID_PRECEDNT\n"
            "1,42,Test Vendor Inc.,null,777,Smith,Jo,2026-01-15,2,"
            "2026-02-01,2026-02-05,null\n",
        "Communication_DpohExport.csv":
            "COMLOG_ID,DPOH_LAST_NM_TCPD,DPOH_FIRST_NM_PRENOM_TCPD,"
            "DPOH_TITLE_TITRE_TCPD,BRANCH_UNIT_DIRECTION_SERVICE,"
            "OTHER_INSTITUTION_AUTRE,INSTITUTION\n"
            "1,Doe,Pat,ADM,null,null,Shared Services Canada (SSC)\n",
        "Communication_SubjectMattersExport.csv":
            "COMLOG_ID,SUBJ_MATTER_OBJET,OTHER_SUBJ_MATTER_AUTRE_OBJET\n"
            "1,Government Procurement,Government Procurement\n",
    }


def test_missing_source_names_the_acquisition_step():
    """
    The error a person hits most often, and the one that has to teach rather
    than just fail. It must say the archive is required, that automated
    download is blocked, and how to supply it — never imply the dataset is
    empty, because "no lobbying data" and "we could not fetch it" are opposite
    conclusions.
    """
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        original, li.SOURCE_DIR = li.SOURCE_DIR, Path(tmp)
        try:
            li.locate_source(None)
            check(False, "locate_source raises when no archive is present")
        except li.SourceUnavailable as exc:
            text = str(exc)
            check(True, "locate_source raises SourceUnavailable when absent")
            for phrase in ("Official OCL lobbying archive required",
                           "blocked by Cloudflare", "--source"):
                check(phrase in text, f"the error states {phrase!r}")
            check("lobbycanada.gc.ca/media/mqbbmaqk" in text,
                  "the error cites the official communications URL")
            check("registrations_enregistrements" in text,
                  "the error cites the official registrations URL")
            check("do not read this as an empty dataset" in text.lower(),
                  "the error distinguishes unfetchable from empty")
        finally:
            li.SOURCE_DIR = original


def test_saved_challenge_page_is_rejected():
    """
    THE failure mode this handoff creates. A browser that hits the challenge
    and saves the result produces 5.8KB of HTML with a .zip name. Caught at the
    archive check it reads as a failed download; caught later it looks like the
    Office renamed a member, which sends the next person to the wrong problem.
    """
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        fake = Path(tmp) / "communications_ocl_cal.zip"
        fake.write_bytes(
            b'<!DOCTYPE html><html lang="en-US"><head>'
            b"<title>Just a moment...</title></head></html>")
        try:
            li.validate_archive(fake)
            check(False, "a saved challenge page is rejected")
        except li.SourceUnavailable as exc:
            check("not a ZIP archive" in str(exc),
                  "a saved challenge page is rejected as not-a-ZIP")
            check("Cloudflare" in str(exc),
                  "the rejection names the likely cause")


def test_valid_archive_lists_its_members():
    """A real zip validates and reports every member with size and compression."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        path = _fixture_archive(Path(tmp), _valid_members())
        members = li.validate_archive(path)
        check(len(members) == 3, f"all 3 members listed (got {len(members)})")
        check(all(m["compression"] == "deflate" for m in members),
              "compression type is reported per member")
        check(all(m["size"] > 0 for m in members), "member sizes are reported")


def test_schema_is_validated_against_the_archive():
    """
    A renamed column must exit loudly rather than ingest empty strings. The
    resolver exits(2) with the real header list, so a schema change at the
    Office is legible instead of silently producing a corpus of blanks.
    """
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        broken = dict(_valid_members())
        broken["Communication_DpohExport.csv"] = \
            broken["Communication_DpohExport.csv"].replace("INSTITUTION", "INSTITUTION_RENAMED")
        path = _fixture_archive(Path(tmp), broken)
        # The resolver prints the real header list to stderr on its way out,
        # which is the point of it — but here that is the EXPECTED result, and
        # letting it through makes a passing run look like a failing one.
        import contextlib
        import io as _io
        try:
            with contextlib.redirect_stderr(_io.StringIO()), \
                    contextlib.redirect_stdout(_io.StringIO()):
                li.read_members(path)
            check(False, "a renamed column is rejected")
        except SystemExit as exc:
            check(exc.code == 2, "a renamed column exits(2) rather than ingesting blanks")


def test_provenance_identifies_the_exact_bytes():
    """
    A hand-acquired input has no request log, so the database has to carry its
    own provenance. The hash is what tells a rebuild from a re-run against a
    stale copy still sitting in the drop directory.
    """
    import hashlib
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        path = _fixture_archive(Path(tmp), _valid_members())
        prov = li.archive_provenance(path)
        expected = hashlib.sha256(path.read_bytes()).hexdigest()
        check(prov["source_sha256"] == expected, "sha256 matches the archive bytes")
        check(prov["source_url"] == li.ZIP_URL,
              "the official source URL is recorded, not the local path alone")
        check(prov["source_bytes"] == str(path.stat().st_size), "byte count recorded")
        check(bool(prov["source_acquired"]), "acquisition timestamp recorded")


def test_built_database_carries_its_provenance(con):
    """
    The recording has to survive into the database, or it is not auditable
    after the fact — which is the only time anyone needs it.
    """
    meta = dict(con.execute("SELECT key, value FROM meta").fetchall())
    for key in ("source_url", "source_sha256", "source_acquired", "archive_members"):
        check(bool(meta.get(key)), f"meta records {key}")
    check(len(meta.get("source_sha256", "")) == 64,
          "the recorded hash is a full sha256")


def main() -> int:
    global SKIPPED
    print(__doc__.strip().splitlines()[0])
    print()

    print("Registry-only checks (no database needed):")
    test_alias_table_points_at_real_entries()
    test_unmapped_departments_stay_unmapped()

    print("\nAcquisition checks (no database or network needed):")
    test_missing_source_names_the_acquisition_step()
    test_saved_challenge_page_is_rejected()
    test_valid_archive_lists_its_members()
    test_schema_is_validated_against_the_archive()
    test_provenance_identifies_the_exact_bytes()

    if not DB.exists():
        SKIPPED = 1
        print(f"\n  SKIP  {DB.relative_to(ROOT)} not built — database checks skipped.")
        print("        The official archive is acquired out-of-band (Cloudflare "
              "blocks automated\n        download). See docs/SETUP.md, then:")
        print("          python scripts/lobbying_ingest.py --source <zip>")
        print(f"\n{PASSED} passed, {FAILED} failed, {SKIPPED} skipped")
        return 1 if FAILED else 0

    con = sqlite3.connect(DB)
    try:
        print("\nDatabase checks:")
        test_every_department_key_is_in_the_registry(con)
        test_dept_key_only_on_departments(con)
        test_every_row_is_classified(con)
        test_parliament_is_never_a_department(con)
        test_ministers_offices_do_not_resolve_to_their_portfolio(con)
        test_amendments_do_not_double_count(con)
        test_subject_other_text_only_on_other(con)
        test_procurement_subset_never_exceeds_its_total(con)
        test_client_norm_matches_the_contracts_normalizer(con)
        test_built_database_carries_its_provenance(con)
    finally:
        con.close()

    print(f"\n{PASSED} passed, {FAILED} failed, {SKIPPED} skipped")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
