"""
Ingest the Office of the Commissioner of Lobbying's Lobbying Registrations —
who is registered to lobby which federal institution, AS OF WHEN — into a local
SQLite database keyed on the canonical organization registry.

Data: https://open.canada.ca/data/en/dataset/... (Lobbying Registrations, OCL)
Licence: Open Government Licence - Canada.

WHY THIS EXISTS, and how it differs from the communications ingest. A monthly
communication report says a meeting HAPPENED. A registration says what a
lobbyist is REGISTERED TO PURSUE, and against which institutions — a standing
declaration rather than an event. Communications answer "who was in the room in
March"; registrations answer "who was declared to be working this department at
the time."

THE WHOLE DESIGN IS VERSIONING, and the reason is a measurement. A registration
is amended over its life, each amendment producing a new VERSION with its own
effective window and its OWN institution list. Measured against the 2026-05
archive: 170,281 versions across 54,941 registrations, and of the 27,704
registrations with more than one version, **14,779 — 53% — have an institution
list that changes between versions.**

One real chain drops from eight institutions (Canadian Heritage, CRTC, Finance,
Foreign Affairs and four others, 2001) to a single "Industry Canada" (2005).
Flattening to the latest version would assert that registration never targeted
Canadian Heritage at all. That is not a rounding error, it is silent deletion of
history, and it is why this stores versions rather than a current state.

    registration_versions      one row per VERSION, with its effective window
    registration_institutions  joins on reg_id, which is the VERSION id

AS-OF IS MANDATORY, NEVER DEFAULTED. Every query path must be given a date.
There is deliberately no default that means "latest", because a default is
exactly how flattened behaviour returns six months from now through a different
door — a caller who never thought about time gets the present tense for free and
writes a time-ordered claim on top of it. A caller who wants current state says
so explicitly. See `as_of_clause`.

WHAT AN OPEN-ENDED VERSION MEANS. 7,331 versions (4.3%) carry no END_DATE_FIN.
This was measured rather than assumed: **all 7,331 are the latest-effective
version of their own chain, with zero mid-chain gaps.** So a null end date means
"still in force", not "unknown", and the as-of predicate treats it as running to
infinity:

    effective_date <= as_of AND (end_date IS NULL OR as_of < end_date)

The interval is half-open on purpose. A version ending 2010-01-01 and its
successor starting 2010-01-01 must not both match an as-of of 2010-01-01.

ORDERING, AND THE BROKEN-CHAIN QUESTION. PREV_REG_ID_ENR_PRECEDNT is populated
on 117,929 of 170,281 versions, which looks like a third of the chains are
broken and is not what it means: 52,331 of the 52,352 absences are the FIRST
version of their chain, where a predecessor pointer is correctly absent. The
genuine mid-chain breaks number **21**, and they are counted in meta rather than
excluded — nothing is dropped for a missing pointer.

That is affordable because THE CHAIN IS NOT THE QUERY MECHANISM. An as-of
question is answered from the effective/end interval, which is populated on 100%
and 95.7% and whose semantics are measured above. The pointer is provenance —
useful for reconstructing an audit trail, never load-bearing for a filter. Where
the REG_NUM version suffix and the effective-date order disagree (921 chains of
27,704, 3.3%), the DATE governs, because the date is what the predicate reads.

DEFERRED ON PURPOSE. The archive carries eleven more members, including
Registration_SubjectMatterDetailsExport.csv (free-prose descriptions of what a
registrant intends to pursue) and the Codes_* vocabularies behind it. None are
ingested here. Free prose is the substrate least suited to reliable matching,
and what it can support needs deciding before it reaches any output rather than
after. This ingest covers the part with unambiguous semantics: who, which
institution, over which interval.

THE ARCHIVE IS AN INPUT ARTIFACT. Same acquisition story as the communications
ingest, and the same helpers — lobbycanada.gc.ca answers 403 with
`cf-mitigated: challenge` to every plain HTTP client, so the zip is downloaded
out-of-band and passed with --source or dropped at the documented path.

Usage:
    # after dropping the zip at data/source/lobbying/
    python scripts/registrations_ingest.py
    python scripts/registrations_ingest.py --source ~/Downloads/registrations_enregistrements_ocl_cal.zip
    python scripts/registrations_ingest.py --source <zip> --show-institutions
"""
from __future__ import annotations

import argparse
import collections
import csv
import io
import sys
import zipfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from ingest_common import output_path, resolve_columns, staged_db  # noqa: E402
from contracts_ingest import normalize_vendor  # noqa: E402
from org_resolve import OrgResolver  # noqa: E402
# The acquisition, validation and provenance path, and the institution
# classifier, are shared with the communications ingest rather than copied —
# a second copy of the five-population rule would drift from the first, and the
# two datasets publish the same institution vocabulary.
from lobbying_ingest import (  # noqa: E402
    KIND_DEPARTMENT, KIND_DEPARTMENT_UNMAPPED, KIND_MINISTER_OFFICE,
    KIND_OTHER_FEDERAL, KIND_PARLIAMENT, KIND_UNSPECIFIED,
    LOBBYING_INSTITUTION_ALIASES, SOURCE_DIR, SourceUnavailable,
    _decode, _member, _num, _txt, archive_provenance, classify_institution,
    locate_source, validate_archive,
)

PROJECT_ROOT = Path(__file__).parent.parent
DB_PATH = PROJECT_ROOT / "data" / "registrations.db"

DATASET_PAGE = "https://open.canada.ca/data/en/organization/ocl-cal"
ZIP_URL = ("https://lobbycanada.gc.ca/media/zwcjycef/"
           "registrations_enregistrements_ocl_cal.zip")
REGISTRATIONS_ARCHIVE = "registrations_enregistrements_ocl_cal.zip"

PRIMARY_MEMBER = "registration_primaryexport.csv"
INSTITUTION_MEMBER = "registration_governmentinstexport.csv"

ACQUISITION_HELP = (
    "Official OCL lobbying registrations archive required.\n"
    "Direct automated download is blocked by Cloudflare.\n"
    "Obtain the archive out-of-band and rerun:\n"
    "  registrations_ingest.py --source <path-to-registrations-zip>\n"
    "\n"
    "Or drop it at the documented path and rerun with no arguments:\n"
    f"  data/source/lobbying/{REGISTRATIONS_ARCHIVE}\n"
    "\n"
    "Official source (returns a Cloudflare challenge to HTTP clients, and\n"
    "downloads normally in a browser):\n"
    f"  {ZIP_URL}\n"
    "\n"
    "The data is published and current. What is unavailable is this\n"
    "environment's ability to fetch it — do not read this as an empty dataset.\n"
)

PRIMARY_COLUMNS = {
    "reg_id": ["REG_ID_ENR"],
    "reg_type": ["REG_TYPE_ENR"],
    "reg_num": ["REG_NUM_ENR"],
    "version_code": ["VERSION_CODE"],
    "firm_en": ["EN_FIRM_NM_FIRME_AN"],
    "registrant_num": ["RGSTRNT_NUM_DECLARANT"],
    "registrant_last": ["RGSTRNT_LAST_NM_DCLRNT"],
    "registrant_first": ["RGSTRNT_1ST_NM_PRENOM_DCLRNT"],
    "client_num": ["CLIENT_ORG_CORP_NUM"],
    "client_en": ["EN_CLIENT_ORG_CORP_NM_AN"],
    "client_fr": ["FR_CLIENT_ORG_CORP_NM"],
    "effective_date": ["EFFECTIVE_DATE_VIGUEUR"],
    "end_date": ["END_DATE_FIN"],
    "prev_reg_id": ["PREV_REG_ID_ENR_PRECEDNT"],
    "posted_date": ["POSTED_DATE_PUBLICATION"],
}
INSTITUTION_COLUMNS = {
    "reg_id": ["REG_ID_ENR"],
    "institution": ["INSTITUTION"],
}

# REG_TYPE_ENR, per the published data dictionary — the same coding the
# communications file uses.
REG_TYPES = {
    1: "consultant",
    2: "in_house_corporation",
    3: "in_house_organization",
}


# ---------------------------------------------------------------------------
# The as-of predicate — the one piece every reader of this database needs
# ---------------------------------------------------------------------------

AS_OF_SQL = ("v.effective_date <= :as_of AND "
             "(v.end_date IS NULL OR :as_of < v.end_date)")


def as_of_clause(alias: str = "v") -> str:
    """
    The SQL predicate selecting the versions in force on a given date.

    Exported as a function so every caller uses ONE definition. A query that
    writes its own interval test is a query that will eventually get the
    half-open boundary wrong, and the failure is silent: a version ending
    2010-01-01 and its successor beginning 2010-01-01 both match a closed
    interval, so an as-of of that date returns two versions of one registration
    and any count built on it doubles.

    `end_date IS NULL` means still in force — measured, not assumed. All 7,331
    open-ended versions are the latest-effective in their own chain.

    THERE IS NO DEFAULT VALUE FOR :as_of, here or anywhere downstream. That is
    the design, not an omission.
    """
    return (f"{alias}.effective_date <= :as_of AND "
            f"({alias}.end_date IS NULL OR :as_of < {alias}.end_date)")


def reg_base(reg_num: str) -> str:
    """
    The registration identity, which is REG_NUM_ENR minus its version suffix.

    "777408-4993-10" and "777408-4993-8" are versions 10 and 8 of one
    registration by registrant 777408 for client 4993. Splitting on the last
    hyphen is what makes a chain addressable without trusting the prev pointer.
    """
    value = (reg_num or "").strip()
    return value.rsplit("-", 1)[0] if "-" in value else value


def version_seq(reg_num: str) -> int | None:
    """The trailing version number, or None when it is not numeric."""
    tail = (reg_num or "").strip().rsplit("-", 1)[-1]
    return int(tail) if tail.isdigit() else None


def read_members(source: Path) -> dict[str, tuple[list[dict], dict]]:
    """Read the two members this ingest uses, resolving headers against them."""
    wanted = {
        "primary": (PRIMARY_MEMBER, PRIMARY_COLUMNS),
        "institutions": (INSTITUTION_MEMBER, INSTITUTION_COLUMNS),
    }
    out: dict[str, tuple[list[dict], dict]] = {}
    zf = zipfile.ZipFile(source) if not source.is_dir() else None

    for logical, (basename, candidates) in wanted.items():
        if zf is not None:
            raw = zf.open(_member(zf, basename)).read()
        else:
            match = [p for p in source.iterdir() if p.name.lower() == basename]
            if not match:
                sys.stderr.write(f"Missing {basename} in {source}\n")
                sys.exit(2)
            raw = match[0].read_bytes()
        text, damaged = _decode(raw)
        reader = csv.DictReader(io.StringIO(text))
        cols = resolve_columns(
            list(reader.fieldnames or []), candidates, list(candidates),
            f"scripts/registrations_ingest.py ({basename})",
        )
        rows = list(reader)
        print(f"  {basename}: {len(rows):,} rows"
              + (f", {damaged:,} replacement chars" if damaged else ""))
        out[logical] = (rows, cols)
    return out


def build_db(members: dict, provenance: dict, archive_members: list,
             db_path: Path = DB_PATH) -> dict:
    """
    Write the two versioned tables, classifying institutions on the way in.

    NOTHING IS FLATTENED and nothing is dropped. Every version is stored with
    its own interval and its own institution rows, because 53% of amended
    registrations change their institution list and collapsing them deletes the
    difference. Versions with a broken predecessor pointer are kept and counted,
    not excluded — the pointer is provenance, and the interval is what queries
    read.
    """
    primary_rows, pcols = members["primary"]
    inst_rows, icols = members["institutions"]

    resolver = OrgResolver()
    strays = sorted(set(LOBBYING_INSTITUTION_ALIASES.values()) - set(resolver.aliases))
    if strays:
        sys.stderr.write(
            f"LOBBYING_INSTITUTION_ALIASES names keys not in the registry: {strays}\n")
        sys.exit(2)

    versions = []
    by_base: dict[str, list[tuple[str, int]]] = collections.defaultdict(list)
    seen_ids = set()
    for r in primary_rows:
        reg_id = _num(r[pcols["reg_id"]])
        if reg_id is None:
            continue
        seen_ids.add(reg_id)
        reg_num = _txt(r[pcols["reg_num"]])
        effective = _txt(r[pcols["effective_date"]])
        end = _txt(r[pcols["end_date"]])
        reg_type = _num(r[pcols["reg_type"]])
        client = _txt(r[pcols["client_en"]])
        by_base[reg_base(reg_num)].append((effective, reg_id))
        versions.append((
            reg_id, reg_num, reg_base(reg_num), version_seq(reg_num),
            _txt(r[pcols["version_code"]]),
            reg_type, REG_TYPES.get(reg_type or 0),
            effective,
            # Stored as NULL rather than "" so the as-of predicate can say
            # `end_date IS NULL` and mean it. An empty string would compare as
            # less than every date and silently expire every open version.
            end or None,
            _num(r[pcols["prev_reg_id"]]),
            _txt(r[pcols["posted_date"]]),
            _num(r[pcols["client_num"]]), client, _txt(r[pcols["client_fr"]]),
            normalize_vendor(client),
            _num(r[pcols["registrant_num"]]),
            _txt(r[pcols["registrant_last"]]), _txt(r[pcols["registrant_first"]]),
            _txt(r[pcols["firm_en"]]),
        ))

    institutions = {_txt(r[icols["institution"]]) for r in inst_rows}
    classified = {name: classify_institution(name, resolver) for name in institutions}
    kind_counts: collections.Counter = collections.Counter()

    inst_out = []
    orphans = 0
    for r in inst_rows:
        reg_id = _num(r[icols["reg_id"]])
        if reg_id is None:
            continue
        if reg_id not in seen_ids:
            # An institution row whose version is absent from the primary file.
            # Counted rather than silently kept: it cannot be placed in time,
            # and an as-of query would never reach it anyway.
            orphans += 1
            continue
        raw = _txt(r[icols["institution"]])
        kind, dept_key, note = classified[raw]
        kind_counts[kind] += 1
        inst_out.append((reg_id, raw, kind, dept_key, note))

    # Chain diagnostics. Reported, never acted on — see the module docstring on
    # why the pointer is provenance rather than the query mechanism.
    first_of_chain = {min(v)[1] for v in by_base.values()}
    broken = sum(1 for v in versions
                 if v[9] is None and v[0] not in first_of_chain)
    # Built once, not per chain. Rebuilding this lookup inside the loop made the
    # diagnostic quadratic — 170k versions scanned for each of 27k chains — and
    # turned a sub-minute ingest into one that never finished.
    seq_by_id = {v[0]: v[3] for v in versions}
    disagree = 0
    for entries in by_base.values():
        if len(entries) < 2:
            continue
        ids_by_date = [i for _d, i in sorted(entries)]
        if any(seq_by_id.get(i) is None for i in ids_by_date):
            continue
        if ids_by_date != sorted(ids_by_date, key=lambda i: seq_by_id[i]):
            disagree += 1

    with staged_db(db_path) as con:
        con.execute("""
            CREATE TABLE registration_versions (
                reg_id INTEGER PRIMARY KEY,
                reg_num TEXT, reg_base TEXT, version_seq INTEGER,
                version_code TEXT,
                reg_type INTEGER, reg_type_label TEXT,
                effective_date TEXT NOT NULL,
                end_date TEXT,
                prev_reg_id INTEGER, posted_date TEXT,
                client_num INTEGER, client_name TEXT, client_name_fr TEXT,
                client_norm TEXT,
                registrant_num INTEGER, registrant_last TEXT,
                registrant_first TEXT, firm_name TEXT
            )
        """)
        con.execute("""
            CREATE TABLE registration_institutions (
                reg_id INTEGER, institution TEXT, institution_kind TEXT,
                dept_key TEXT, classification_note TEXT
            )
        """)
        con.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        con.executemany(
            "INSERT INTO registration_versions VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", versions)
        con.executemany(
            "INSERT INTO registration_institutions VALUES (?,?,?,?,?)", inst_out)
        # The as-of predicate reads effective_date and end_date together, so
        # they are indexed together.
        con.execute("CREATE INDEX idx_ver_window ON "
                    "registration_versions(effective_date, end_date)")
        con.execute("CREATE INDEX idx_ver_base ON registration_versions(reg_base)")
        con.execute("CREATE INDEX idx_ver_client ON registration_versions(client_norm)")
        con.execute("CREATE INDEX idx_inst_reg ON registration_institutions(reg_id)")
        con.execute("CREATE INDEX idx_inst_dept ON registration_institutions(dept_key)")

        effs = [v[7] for v in versions if v[7]]
        stats = {
            "ingest_date": datetime.now().strftime("%Y-%m-%d"),
            "dataset_page": DATASET_PAGE,
            **provenance,
            "archive_members": ", ".join(m["filename"] for m in archive_members)
                               or "(directory source)",
            "licence": "Open Government Licence - Canada",
            "versions": str(len(versions)),
            "registrations": str(len(by_base)),
            "institution_rows": str(len(inst_out)),
            "earliest_effective": min(effs) if effs else "",
            "latest_effective": max(effs) if effs else "",
            "open_ended_versions": str(sum(1 for v in versions if v[8] is None)),
            # The three diagnostics the docstring commits to reporting.
            "chain_breaks_midchain": str(broken),
            "chain_seq_date_disagreements": str(disagree),
            "institution_rows_orphaned": str(orphans),
            "deferred_members": (
                "SubjectMatterDetails, SubjectMatters, Codes_*, Beneficiaries, "
                "CommunicationTechniques, InHouseLobbyists, GovtFunding, "
                "PublicOffice, ConsultantLobbyists, ManuallyEntered_GovernmentInst "
                "— free prose and vocabularies, deliberately not ingested"),
        }
        for kind in (KIND_DEPARTMENT, KIND_DEPARTMENT_UNMAPPED, KIND_MINISTER_OFFICE,
                     KIND_PARLIAMENT, KIND_OTHER_FEDERAL, KIND_UNSPECIFIED):
            stats[f"institution_rows_{kind}"] = str(kind_counts.get(kind, 0))
        for k, v in stats.items():
            con.execute("INSERT INTO meta VALUES (?, ?)", (k, v))

    return {"versions": len(versions), "registrations": len(by_base),
            "institutions": len(inst_out), "kinds": kind_counts,
            "classified": classified, "broken": broken,
            "disagree": disagree, "orphans": orphans}


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=ACQUISITION_HELP,
    )
    parser.add_argument("--source", type=Path, default=None,
                        help="Path to the official registrations zip, or a "
                             "directory of the extracted CSVs. Defaults to "
                             f"data/source/lobbying/{REGISTRATIONS_ARCHIVE}.")
    parser.add_argument("--db", type=Path, default=None,
                        help=f"Output DB path (default {DB_PATH}).")
    parser.add_argument("--show-institutions", action="store_true",
                        help="Print every institution and its population")
    args = parser.parse_args()

    try:
        source = locate_source(args.source, (REGISTRATIONS_ARCHIVE,), ACQUISITION_HELP)
        archive_members = validate_archive(source, ACQUISITION_HELP)
    except SourceUnavailable as exc:
        sys.stderr.write("\n" + str(exc) + "\n")
        sys.exit(2)

    db_path = output_path(DB_PATH, args.db, None)
    provenance = archive_provenance(source, ZIP_URL)
    print(f"  sha256 {provenance['source_sha256']}")
    print(f"  acquired {provenance['source_acquired']}")

    print(f"Reading {source}")
    members = read_members(source)
    result = build_db(members, provenance, archive_members, db_path)

    print(f"\nWrote {result['versions']:,} versions across "
          f"{result['registrations']:,} registrations and "
          f"{result['institutions']:,} institution rows to {db_path} "
          f"({db_path.stat().st_size/1e6:.1f} MB)")
    print(f"  mid-chain pointer breaks: {result['broken']:,} (kept, not excluded)")
    print(f"  seq/date order disagreements: {result['disagree']:,} (date governs)")
    print(f"  orphaned institution rows: {result['orphans']:,}")
    print("  institution rows by population:")
    for kind, n in result["kinds"].most_common():
        print(f"    {kind:<22} {n:>8,}")

    if args.show_institutions:
        by_kind: dict[str, list] = collections.defaultdict(list)
        for name, (kind, key, _n) in result["classified"].items():
            by_kind[kind].append((name, key))
        for kind, rows in by_kind.items():
            print(f"\n=== {kind} ({len(rows)}) ===")
            for name, key in sorted(rows):
                print(f"  {name}" + (f"   -> {key}" if key else ""))

    print("\nAttribution: contains information licensed under the "
          "Open Government Licence - Canada.")


if __name__ == "__main__":
    main()
