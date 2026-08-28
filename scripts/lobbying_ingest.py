"""
Ingest the Office of the Commissioner of Lobbying's Monthly Communication
Reports — who has been in the room with which federal institution, about what —
into a local SQLite database keyed on the canonical organization registry.

Data: https://open.canada.ca/data/en/dataset/a34eb330-7136-4f5e-9f5f-3ba41df58b06
Licence: Open Government Licence - Canada.

WHY THIS EXISTS — the fifth signal, and the earliest one. Read in order, the
sources already here move steadily backwards in time: a tender notice says an
RFP is open now, an expiring contract says one is predictably coming, a
departmental plan says what a department intends, an OAG audit says what it has
been publicly found failing at. This one sits earlier still. A monthly
communication report is filed when a lobbyist has an oral, arranged
communication with a designated public office holder about a listed subject
matter, and "Government Procurement" is one of the 54 subject matters on that
list. It is the public record of a conversation that happened while the
requirement was still being decided.

WHAT IT IS NOT, and this matters more than what it is. A communication report
is evidence of PRESENCE, never of influence, and never of wrongdoing. Filing
one is what compliance with the Lobbying Act looks like — the firms in this
data are the ones following the rules. Nothing downstream should read "met with
PSPC about Government Procurement" as "steered a procurement," and no output of
this pipeline should imply it. What the record supports is narrower and still
valuable: this department has been hearing from this set of firms on this
subject, for this long, and here is the citation. Treat it as context for
reading the other four sources, not as a finding about anyone.

It is also SELF-REPORTED and INCOMPLETE BY DESIGN. Only arranged oral
communications with designated office holders are reportable; a written
submission, an unarranged conversation, or a meeting with a public servant
below the DPOH threshold generates no record. Absence from this data is not
evidence that nobody was in the room.

THREE FILES, ONE RECORD. The published zip is a parent and two child tables,
and they are stored that way rather than flattened, for the reason the OAG
attribution is a join table: one communication routinely names several office
holders across more than one institution, and carries several subject matters.
Flattening to one row picks a winner and throws the rest away.

  Communication_PrimaryExport.csv        one row per communication
  Communication_DpohExport.csv           the office holders present (1:many)
  Communication_SubjectMattersExport.csv the subjects discussed (1:many)

INSTITUTIONS ARE CLASSIFIED, NOT FORCED. 162 distinct institution strings
appear, and only about half are federal departments the registry knows. The
single largest value is "House of Commons" — 106k of 339k office-holder rows,
because members of parliament are designated office holders. An MP is not a
buyer; procurement authority sits in departments. So every row carries an
`institution_kind` saying which population it belongs to, and `dept_key` is set
only for the population where a canonical key is a true statement. This is the
same refusal `oag_ingest` makes with attribution_status: a row that has no
department says WHY, because an empty column reads as a coverage failure and
invents a gap that was never there.

CLIENTS JOIN TO CONTRACT VENDORS through `contracts_ingest.normalize_vendor` —
imported, never reimplemented, so "HP CANADA CO." on the lobbying side and the
contracts side reduce to the same key or the join is wrong on both sides at
once rather than quietly on one.

THE ARCHIVE IS AN INPUT ARTIFACT, not something this script fetches. Acquisition
happens out-of-band, in a browser, and ingestion starts once the file is on
disk — which keeps everything from that point deterministic and testable.

    1. a person downloads the official zip in a browser
    2. it lands at data/source/lobbying/communications_ocl_cal.zip
    3. this script validates it, lists its members, checks the schema,
       records its provenance, and builds the database

lobbycanada.gc.ca answers 403 with `cf-mitigated: challenge` to every plain HTTP
client, whatever headers it carries. Measured across all four published
resources — both bulk archives and both data dictionaries — on 2026-08-26. The
Portal links to these files rather than hosting them, and both resources are
`datastore_active: false`, so there is no CKAN dump to read either.

Unlike every sibling ingest, --source here is the ONLY route to the real
published data, not a spot-check — so it does NOT redirect output to a .sample
database. --max-comms is the truncating flag, and that one does.

NOTHING IS EVER SILENTLY SUBSTITUTED. With no archive present this script exits
non-zero with instructions and builds nothing; it never falls back to a database
built earlier. "The source is unavailable to this environment" and "the source
data is unavailable" are different facts, and only the first one is ever true
here — the registry is published, current, and updated weekly. A pipeline that
conflated them would report that no lobbying happened, which is the worst
available answer and is worse than no answer at all.

BROWSER AUTOMATION DOES NOT SOLVE THIS. Measured 2026-08-25, so nobody spends
another afternoon on it. Three Playwright configurations were tried against
lobbycanada.gc.ca and all three sat on "Just a moment..." until timeout:
headless bundled Chromium, HEADED bundled Chromium, and headed real Chrome
(channel="chrome") with a persistent user-data profile. The challenge is
detecting the CDP automation itself, not the absence of a display, so the usual
"just run it headed" fix does not apply.

What remains would be anti-detect tooling — stealth plugins, patched browser
builds, a solver service. That is deliberately not built here. Beyond what it
is, it fails the engineering test: it breaks whenever Cloudflare updates, and a
dependency that rots silently underneath a data pipeline is worse than a manual
step somebody performs on purpose. The registry publishes weekly; downloading
it by hand once a month is the cheaper, more honest answer.

For the record, this is not a permissions question. lobbycanada.gc.ca/robots.txt
carries a generic Cloudflare-managed `User-agent: * / Allow: /`, and the dataset
is Open Government Licence material published expressly for bulk download. The
challenge is blanket DDoS protection on the domain, not a policy about this
file.

WINDOWED TO THE RECENT PAST, default three years, from
`lobbying_window_years` in the profile frontmatter. The published file runs
back to July 2008 and most of it is dead weight for this purpose: a meeting
with a department in 2011 is not evidence about who is in the room today, and
half the institutions named in the early years no longer exist. The window is
CONFIG, not sampling — it is the intended scope of the database, the same way
`contracts_window_years` is — so narrowing it does not redirect the output. It
is applied to COMM_DATE, the date the communication happened, never to
POSTED_DATE: a report filed late would otherwise drift into a window whose
conversations it was not part of.

Usage:
    # after dropping the zip at data/source/lobbying/
    python scripts/lobbying_ingest.py
    # or point at it anywhere
    python scripts/lobbying_ingest.py --source ~/Downloads/communications_ocl_cal.zip
    python scripts/lobbying_ingest.py --source <zip> --show-institutions
    python scripts/lobbying_ingest.py --source <zip> --window-years 10
    python scripts/lobbying_ingest.py --source <zip> --all-years
    python scripts/lobbying_ingest.py --source <zip> --max-comms 5000   # spot-check
"""
from __future__ import annotations

import argparse
import collections
import csv
import hashlib
import io
import re
import sqlite3
import sys
import zipfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from ingest import parse_profile  # noqa: E402
from ingest_common import output_path, resolve_columns, staged_db  # noqa: E402
# The one normalizer both sides of the client/vendor join must share. Importing
# it is the join's only guarantee of agreement; a second copy here would drift.
from contracts_ingest import normalize_vendor  # noqa: E402
from org_resolve import AmbiguousOrganization, OrgResolver  # noqa: E402

PROJECT_ROOT = Path(__file__).parent.parent
DB_PATH = PROJECT_ROOT / "data" / "lobbying.db"
DEFAULT_PROFILE = PROJECT_ROOT / "vault" / "profiles" / "my-company.md"

DATASET_PAGE = (
    "https://open.canada.ca/data/en/dataset/"
    "a34eb330-7136-4f5e-9f5f-3ba41df58b06"
)
ZIP_URL = "https://lobbycanada.gc.ca/media/mqbbmaqk/communications_ocl_cal.zip"
# The sibling dataset. Not ingested here — this script builds the
# communications corpus — but named so the acquisition instructions cover both
# files a person is likely to be told to fetch in one trip.
REGISTRATIONS_URL = (
    "https://lobbycanada.gc.ca/media/zwcjycef/"
    "registrations_enregistrements_ocl_cal.zip"
)

# The documented drop path. An input artifact needs a location that is part of
# the repo's contract rather than a habit, so a re-run is `--source`-free and a
# second person knows where to put the file without being told.
SOURCE_DIR = PROJECT_ROOT / "data" / "source" / "lobbying"
COMMUNICATIONS_ARCHIVE = "communications_ocl_cal.zip"

COMPRESSION_NAMES = {
    zipfile.ZIP_STORED: "stored",
    zipfile.ZIP_DEFLATED: "deflate",
    zipfile.ZIP_BZIP2: "bzip2",
    zipfile.ZIP_LZMA: "lzma",
}

ACQUISITION_HELP = (
    "Official OCL lobbying archive required.\n"
    "Direct automated download is blocked by Cloudflare.\n"
    "Obtain the archive out-of-band and rerun:\n"
    "  lobbying_ingest.py --source <path-to-communications-zip>\n"
    "\n"
    f"Or drop it at the documented path and rerun with no arguments:\n"
    f"  data/source/lobbying/{COMMUNICATIONS_ARCHIVE}\n"
    "\n"
    "Official sources (both return a Cloudflare challenge to HTTP clients,\n"
    "and download normally in a browser):\n"
    f"  communications  {ZIP_URL}\n"
    f"  registrations   {REGISTRATIONS_URL}\n"
    "\n"
    "The data is published and current. What is unavailable is this\n"
    "environment's ability to fetch it — do not read this as an empty dataset.\n"
)

# Members of the published zip. Matched case-insensitively on the basename so a
# recased or re-pathed member does not count as a missing file — the zip has
# been repathed once already (media/1863/ to media/mqbbmaqk/).
PRIMARY_MEMBER = "communication_primaryexport.csv"
DPOH_MEMBER = "communication_dpohexport.csv"
SUBJECT_MEMBER = "communication_subjectmattersexport.csv"
# THE SECOND HALF OF THE SUBJECT DATA, and reading only the file above silently
# loses it. The Office changed how subjects are exported partway through the
# series, and the two files partition the dataset by communication rather than
# duplicating it:
#
#   SubjectMatters   COMLOG_ID 73,734 - 689,815   308,091 comms   ends 2024-09-30
#   SubjectMatterDetails  COMLOG_ID 615,832 - 698,638    72,339 comms   begins there
#
# 308,091 + 72,339 = 380,430 of 380,442 published communications; the residue is
# communications filed with no subject at all. So there is no gap in the source
# — an ingest that reads SubjectMatters alone simply stops seeing subjects at
# 2024-09-30 while the communications keep arriving, which reads downstream as
# "nobody lobbied about this any more" and is the most dangerous shape an error
# can take in this project.
#
# The two files do not share a format. SubjectMatters is normalized, one row per
# code. Details packs the codes into ONE field as a comma-joined list —
# "SMT-8, SMT-17, SMT-20, ..." — beside a free-text DESCRIPTION. Splitting that
# list is what makes the second era filterable on the same vocabulary as the
# first: Government Procurement (SMT-17) appears on 7,651 communications in
# Details against 1,669 in the whole of SubjectMatters.
SUBJECT_DETAILS_MEMBER = "communication_subjectmatterdetailsexport.csv"
# The subject vocabulary, shipped inside the same zip as the data it decodes.
# Read rather than transcribed: a 54-entry list copied into this file would be
# a second source of truth for the labels callers filter on, and the day the
# Office adds a subject it would be a silently stale one.
CODES_MEMBER = "codes_subjectmattertypesexport.csv"

PRIMARY_COLUMNS = {
    "comlog_id": ["COMLOG_ID"],
    "client_num": ["CLIENT_ORG_CORP_NUM"],
    "client_en": ["EN_CLIENT_ORG_CORP_NM_AN"],
    "client_fr": ["FR_CLIENT_ORG_CORP_NM"],
    "registrant_num": ["REGISTRANT_NUM_DECLARANT"],
    "registrant_last": ["RGSTRNT_LAST_NM_DCLRNT"],
    "registrant_first": ["RGSTRNT_1ST_NM_PRENOM_DCLRNT"],
    "comm_date": ["COMM_DATE"],
    "reg_type": ["REG_TYPE_ENR"],
    "submission_date": ["SUBMISSION_DATE_SOUMISSION"],
    "posted_date": ["POSTED_DATE_PUBLICATION"],
    "amends": ["PREV_COMLOG_ID_PRECEDNT"],
}
DPOH_COLUMNS = {
    "comlog_id": ["COMLOG_ID"],
    "last": ["DPOH_LAST_NM_TCPD"],
    "first": ["DPOH_FIRST_NM_PRENOM_TCPD"],
    "title": ["DPOH_TITLE_TITRE_TCPD"],
    "branch": ["BRANCH_UNIT_DIRECTION_SERVICE"],
    "other_institution": ["OTHER_INSTITUTION_AUTRE"],
    "institution": ["INSTITUTION"],
}
# The subject file carries a CODE, not a label. It named its subjects inline
# once (SUBJ_MATTER_OBJET, the schema this ingest was first written against);
# the published export now normalizes them into Codes_SubjectMatterTypesExport
# and stores SMT-17 where it used to store "Government Procurement". Resolving
# the code is therefore not a nicety — PROCUREMENT_SUBJECT below is matched as
# English text, so an unresolved code would leave every procurement filter in
# the project returning nothing, which is the failure that looks like an answer.
SUBJECT_COLUMNS = {
    "comlog_id": ["COMLOG_ID"],
    "code": ["SUBJECT_CODE_OBJET"],
    "other_subject": ["CUSTOM_SUBJ_OBJET_PERSO"],
}
# Same code column name, different contents: here it is a comma-joined LIST.
# DESCRIPTION is the filer's free prose about the requirement and takes the
# place CUSTOM_SUBJ_OBJET_PERSO holds in the older file.
SUBJECT_DETAIL_COLUMNS = {
    "comlog_id": ["COMLOG_ID"],
    "code": ["SUBJECT_CODE_OBJET"],
    "description": ["DESCRIPTION"],
}
CODES_COLUMNS = {
    "code": ["SUBJECT_CODE_OBJET"],
    "subject": ["SMT_EN_DESC"],
}

# REG_TYPE_ENR, per the published data dictionary. Not guessable from the
# counts: the largest bucket is 3, and reading that as "consultant" because
# consultants are the largest population in the registry gets it backwards.
REG_TYPES = {
    1: "consultant",                # a hired lobbyist; the client is who paid
    2: "in_house_corporation",      # the company's own staff
    3: "in_house_organization",     # an association's own staff
}

# The subject matter that makes this dataset a procurement signal rather than a
# general-politics one. Stored as data, not filtered on — the ingest keeps every
# subject, and callers narrow.
PROCUREMENT_SUBJECT = "Government Procurement"


# ---------------------------------------------------------------------------
# Institution classification
# ---------------------------------------------------------------------------
# Every office-holder row lands in exactly one population, and the populations
# exist to keep `dept_key` honest rather than to organize the data prettily.

# Parliamentarians and their staff. Designated office holders, and the single
# biggest population in the file, but not departments and not buyers: an MP has
# no procurement authority. Matched on the exact published strings, because
# these are enumerated values from a dropdown, not prose.
PARLIAMENT_INSTITUTIONS = {
    "house of commons",
    "members of the house of commons",
    "senate of canada",
    "senate",
    "library of parliament",
}

# Ministers' offices and the centre. Political staff, distinct from the
# department's officials: "Minister's Office" appears as a parenthetical tail on
# several values, and the PMO/Deputy PM offices are named outright.
_MINISTER_OFFICE = re.compile(
    r"minister\s*.?\s*s\s+office|bureau du ministre|"
    r"prime minister\s*.?\s*s office|deputy prime minister",
    re.I,
)

# The 'Other' dropdown option, whose free text lands in OTHER_INSTITUTION_AUTRE.
# The Office stopped accepting manual entry, so this population is historical.
UNSPECIFIED_INSTITUTIONS = {"other (specify)", "other", "autre"}

# Federal departments the registry cannot name with ONE key, recorded here
# rather than resolved. Each is a body whose mandate was later SPLIT, so every
# available key would be a partial answer, and picking one would attribute a
# meeting to a department that may not have existed when it happened. The value
# is the reason, stored on the row so a reader sees why the key is null.
#
# Clean 1:1 renames and amalgamations are NOT here — those are recorded as
# `observed_names` in org_aliases.yaml, where the registry can attest them, and
# they resolve normally.
UNMAPPED_DEPARTMENTS = {
    "aboriginal affairs and northern development canada":
        "Split in 2017 into Crown-Indigenous Relations and Northern Affairs "
        "(crown-indigenous-relations) and Indigenous Services (isc). No single "
        "successor holds the mandate these meetings were about.",
    "indigenous and northern affairs canada":
        "Split in 2017 into Crown-Indigenous Relations and Northern Affairs "
        "(crown-indigenous-relations) and Indigenous Services (isc). No single "
        "successor holds the mandate these meetings were about.",
    "revenue canada":
        "The Canada Customs and Revenue Agency was split in 2003 into the "
        "Canada Revenue Agency (cra) and the Canada Border Services Agency "
        "(cbsa). Which one a pre-2003 meeting concerned is not recoverable "
        "from the record.",
    "human resources development canada":
        "Split in 2003 into Human Resources and Skills Development and Social "
        "Development Canada, later recombined as ESDC. The split years are "
        "ambiguous; the HRSDC name that followed is mapped and resolves.",
}

# Institution strings that ARE a department the registry knows, under a name the
# registry does not carry. Kept HERE rather than added to org_aliases.yaml as
# `observed_names`, deliberately, for two reasons.
#
# The registry is shared. Its observed_names feed OrgResolver's phrase index,
# which scans OAG audit PROSE — so adding "National Energy Board" there would
# silently re-attribute existing audits to `cer`, a change to a different
# source's results made as a side effect of ingesting this one.
#
# And attestation would reject them. vault/crosswalk/attestation.yaml records
# where each observed_name was seen, and crosswalk.py checks every declared name
# against the union of oag.db and the tender feed. A name attested only by the
# lobbying file has no evidence in either and would be reported as unfounded —
# correctly, because it IS unfounded there. Provenance stays honest by keeping
# an OCL spelling in the OCL ingest.
#
# Every value is checked against the registry at ingest, so a typo fails loudly
# rather than dropping rows into `other_federal`. Only clean one-to-one renames
# and amalgamations belong here; anything whose mandate SPLIT goes in
# UNMAPPED_DEPARTMENTS above, where the ambiguity is recorded instead of guessed.
LOBBYING_INSTITUTION_ALIASES = {
    # Spelling variant, not a rename: the statute says Canada Energy Regulator,
    # the Office's dropdown says Canadian. Same body, both live today.
    "canadian energy regulator": "cer",
    # Renamed, mandate carried over whole, on the same day (2019-08-28) under
    # the Canadian Energy Regulator Act and the Impact Assessment Act.
    "national energy board": "cer",
    "canadian environmental assessment agency": "impact-assessment-agency",
    # Amalgamated into Global Affairs: DFAIT absorbed CIDA in 2013 and was
    # renamed in 2015. One successor, no split.
    "foreign affairs and international trade canada": "gac",
    "canadian international development agency": "gac",
    # The ESDC naming sequence after the 2003 split settled: HRSDC and HRSD are
    # the same department under two spellings, both wholly ESDC today. The
    # earlier HRDC, which is the split itself, stays unmapped.
    "human resources and skills development canada": "esdc",
    "human resources and social development canada": "esdc",
    # Renamed to Women and Gender Equality Canada in December 2018.
    "status of women canada": "wage",
}

# NOT mapped, and each is a deliberate refusal rather than an oversight. These
# are sub-units and service brands whose parent department is obvious, and
# mapping them would assert that a meeting with the part was a meeting with the
# whole: "Service Canada" (delivery arm of ESDC), "Intergovernmental Affairs
# Secretariat" and "Privy Council Office" sub-secretariats, "Competition Bureau
# Canada" (inside ISED, but an independent law-enforcement function). The
# registry makes the same distinction when it refuses "Receiver General" for
# PSPC. They classify as other_federal, keep their published name, and stay
# visible under --show-institutions for anyone who disagrees.

# Kinds. Exhaustive: classify_institution returns one of these for every row.
KIND_DEPARTMENT = "department"
KIND_DEPARTMENT_UNMAPPED = "department_unmapped"
KIND_MINISTER_OFFICE = "minister_office"
KIND_PARLIAMENT = "parliament"
KIND_OTHER_FEDERAL = "other_federal"
KIND_UNSPECIFIED = "unspecified"

# "Innovation, Science and Economic Development Canada (ISED)" — the published
# values carry the acronym as a parenthetical tail, which no registry name has.
_PAREN_TAIL = re.compile(r"\s*\([^)]*\)\s*$")


def strip_acronym(name: str) -> str:
    """
    Drop the trailing "(ACRONYM)" the institution list appends to most names.

    Repeated, because a value can carry two tails. The acronym itself is
    DELIBERATELY NOT tried as a resolver input afterwards: measured against the
    published list, the acronym path resolves nothing the full name did not
    already resolve, so it adds only the risk of a two-or-three letter string
    landing on an unrelated registry key by coincidence.
    """
    s = (name or "").strip()
    prev = None
    while s != prev:
        prev = s
        s = _PAREN_TAIL.sub("", s).strip()
    return s


def classify_institution(raw: str, resolver: OrgResolver) -> tuple[str, str | None, str | None]:
    """
    Sort one published institution string into a population.

    Returns (kind, dept_key, note). `dept_key` is non-null only for
    KIND_DEPARTMENT, and is always a canonical key from org_aliases.yaml — the
    same identity the audits, plans and contracts join on, so a department means
    one thing across all five sources.

    ORDER IS LOAD-BEARING. Parliament and ministers' offices are tested BEFORE
    the registry, because several of those strings contain a department's name
    ("Rural Economic Development (Minister's Office)") and would otherwise
    resolve to the department whose officials were precisely not in the room.
    """
    name = (raw or "").strip()
    if not name:
        return KIND_UNSPECIFIED, None, "no institution recorded"

    folded = name.lower()
    if folded in UNSPECIFIED_INSTITUTIONS:
        return KIND_UNSPECIFIED, None, "filer chose 'Other'; see other_institution"
    if folded in PARLIAMENT_INSTITUTIONS:
        return KIND_PARLIAMENT, None, "parliamentarians, not a buying department"
    if _MINISTER_OFFICE.search(name):
        return KIND_MINISTER_OFFICE, None, "political staff, not departmental officials"

    base = strip_acronym(name)
    if base.lower() in UNMAPPED_DEPARTMENTS:
        return KIND_DEPARTMENT_UNMAPPED, None, UNMAPPED_DEPARTMENTS[base.lower()]

    alias = LOBBYING_INSTITUTION_ALIASES.get(base.lower())
    if alias:
        return KIND_DEPARTMENT, alias, "mapped by LOBBYING_INSTITUTION_ALIASES"

    for candidate in (name, base):
        try:
            key = resolver.resolve(candidate)
        except AmbiguousOrganization as exc:
            # A registry defect, not a guess to make here. Surfaced loudly.
            sys.stderr.write(f"  ambiguous institution {candidate!r}: {exc}\n")
            key = None
        if key:
            return KIND_DEPARTMENT, key, None

    return KIND_OTHER_FEDERAL, None, (
        "not a department in vault/crosswalk/org_aliases.yaml — Crown "
        "corporation, agency, tribunal or port authority"
    )


# ---------------------------------------------------------------------------
# Reading the published zip
# ---------------------------------------------------------------------------

def _decode(raw: bytes) -> tuple[str, int]:
    """
    Decode one member, trying the encodings the Office actually publishes.

    THE TWO ARCHIVES DIFFER, which is why this tries rather than assumes. The
    communications members are clean UTF-8 — all three decode strictly and
    contain zero U+FFFD. The registrations members are CP1252: the primary
    export fails strict UTF-8 at the first accented name it reaches
    ("Les brasseries Sleeman Ltée") and decodes cleanly as CP1252.

    Getting this wrong is not cosmetic. Decoding CP1252 as UTF-8 with
    errors="replace" mangled 97,122 characters on one pass — every accented
    French character in the file, including client names. Those names feed
    `normalize_vendor`, so a mangled name produces a different key from the
    correct one and the join to the contracts data silently misses every
    French-named company.

    The returned count is replacement characters, and it is zero on both
    archives today. A non-zero count means neither encoding worked and the
    damage is real — reported rather than hidden, because the alternative is a
    corpus that looks fine and joins wrong.
    """
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(encoding), 0
        except UnicodeDecodeError:
            continue
    # Neither worked. Decode lossily rather than aborting, and report the count
    # so the damage is visible instead of silently entering the database.
    text = raw.decode("utf-8-sig", errors="replace")
    return text, text.count("\ufffd")


def _member(zf: zipfile.ZipFile, basename: str) -> str:
    """Find a member by case-insensitive basename, or exit(2) naming what's there."""
    for info in zf.infolist():
        if Path(info.filename).name.lower() == basename:
            return info.filename
    sys.stderr.write(
        f"Missing {basename} in the archive.\nMembers present:\n"
        + "\n".join(f"  {i.filename}" for i in zf.infolist())
        + f"\nUpdate the member names in {Path(__file__).name}.\n"
    )
    sys.exit(2)


class SourceUnavailable(Exception):
    """
    No official archive is present, so there is nothing to ingest.

    Deliberately NOT the same condition as "the data does not exist". The
    Monthly Communication Reports are published, current and correct; what is
    unavailable is this environment's ability to fetch them over plain HTTP.
    Conflating the two produces the worst possible outcome — a pipeline that
    treats a network restriction as an empty dataset and reports that no
    lobbying happened.
    """


def locate_source(explicit: Path | None, filenames: tuple[str, ...] = (),
                  help_text: str | None = None) -> Path:
    """
    Find the official archive, or raise SourceUnavailable with the fix.

    `filenames` and `help_text` are parameterized so the sibling registrations
    ingest reuses this rather than copying it; both default to the
    communications values, which is what every existing caller wants.

    Acquisition is an OUT-OF-BAND STEP and the archive is an INPUT ARTIFACT.
    lobbycanada.gc.ca answers 403 with `cf-mitigated: challenge` to every plain
    HTTP client — measured across all four published resources, both zips and
    both dictionaries — so a fetch belongs to whatever process can run a
    browser, and this script's job starts once the file exists on disk.

    With no --source, the documented drop path is checked so the routine case
    is a bare re-run. Nothing else is searched: guessing at Downloads folders
    would make the input silently machine-dependent.
    """
    help_text = help_text or ACQUISITION_HELP
    filenames = filenames or (COMMUNICATIONS_ARCHIVE,)

    if explicit is not None:
        if not explicit.exists():
            raise SourceUnavailable(
                f"{explicit} does not exist.\n\n" + help_text)
        return explicit

    for name in filenames:
        candidate = SOURCE_DIR / name
        if candidate.exists():
            print(f"  using the archive at {candidate.relative_to(PROJECT_ROOT)}")
            return candidate
    raise SourceUnavailable(help_text)


def archive_provenance(source: Path, url: str | None = None) -> dict[str, str]:
    """
    Identify the exact bytes this build came from.

    A derived database whose input cannot be named is not auditable, and this
    input arrives by hand — so it carries no request log, no ETag and no
    server-side timestamp to fall back on. The SHA-256 is what makes two builds
    comparable and what catches a re-run against a stale copy still sitting in
    the drop directory.

    `acquired` is the file's mtime, which is when the archive landed here, NOT
    when the Office published it. The published coverage is recorded separately
    from the data itself as earliest/latest_communication.
    """
    digest = hashlib.sha256()
    with source.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    # Whether the bytes came from the documented drop path — the one place an
    # official download is supposed to land. NOT proof of provenance (a file in
    # ~/Downloads may be perfectly official), but it distinguishes the routine
    # case from a build against a development copy, which is exactly the
    # confusion that lets an archived or sampled file become the provenance of
    # a database everything downstream then treats as current.
    try:
        from_drop_path = source.resolve().parent == SOURCE_DIR.resolve()
    except OSError:
        from_drop_path = False

    return {
        "source_path": str(source),
        "source_url": url or ZIP_URL,
        "source_from_drop_path": "yes" if from_drop_path else "no",
        "source_sha256": digest.hexdigest(),
        "source_bytes": str(source.stat().st_size),
        "source_acquired": datetime.fromtimestamp(
            source.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
    }


def validate_archive(source: Path, help_text: str | None = None) -> list[dict]:
    """
    Confirm the file is a real ZIP and return its member listing.

    Checked before anything is read because the failure this catches is the
    common one: a Cloudflare interstitial saved with a .zip extension. That is
    5.8KB of HTML beginning `<!DO`, and without this check it surfaces much
    later as a confusing missing-member error that reads like a schema change
    at the Office rather than a failed download.

    A directory of already-extracted CSVs is accepted and skips the check —
    there is no archive to validate — because that is the shape a caller has
    after unpacking one by hand.
    """
    if source.is_dir():
        print(f"  source is a directory; no archive to validate")
        return []
    if not zipfile.is_zipfile(source):
        head = source.read_bytes()[:120]
        hint = ""
        if head[:1] == b"<":
            hint = ("\n  The first bytes are HTML. This is almost certainly a "
                    "saved Cloudflare\n  challenge page rather than the archive "
                    "— see the note above.")
        raise SourceUnavailable(
            f"{source} is not a ZIP archive "
            f"({source.stat().st_size:,} bytes).\n"
            f"  First bytes: {head[:60]!r}{hint}\n\n"
            + (help_text or ACQUISITION_HELP))

    with zipfile.ZipFile(source) as zf:
        members = [
            {"filename": i.filename,
             "size": i.file_size,
             "compressed": i.compress_size,
             "compression": COMPRESSION_NAMES.get(i.compress_type,
                                                  str(i.compress_type))}
            for i in zf.infolist()
        ]
    print(f"  archive validated: {len(members)} members")
    for m in members:
        print(f"    {m['size']:>14,} bytes  {m['compression']:<8} {m['filename']}")
    return members


def read_members(source: Path) -> dict[str, tuple[list[dict], dict, int]]:
    """
    Read the four CSVs from the published zip (or a directory of them).

    Returns {logical_name: (rows, resolved_columns, replacement_chars)}. Each
    file's headers are resolved against the candidates above, so a column rename
    at the Office exits with the real header list instead of ingesting empties.
    """
    wanted = {
        "primary": (PRIMARY_MEMBER, PRIMARY_COLUMNS),
        "dpoh": (DPOH_MEMBER, DPOH_COLUMNS),
        "subjects": (SUBJECT_MEMBER, SUBJECT_COLUMNS),
        "subject_details": (SUBJECT_DETAILS_MEMBER, SUBJECT_DETAIL_COLUMNS),
        "codes": (CODES_MEMBER, CODES_COLUMNS),
    }
    out: dict[str, tuple[list[dict], dict, int]] = {}

    if source.is_dir():
        def read(basename: str) -> bytes:
            for path in source.iterdir():
                if path.name.lower() == basename:
                    return path.read_bytes()
            sys.stderr.write(f"Missing {basename} in {source}\n")
            sys.exit(2)
    else:
        zf = zipfile.ZipFile(source)

        def read(basename: str) -> bytes:
            return zf.open(_member(zf, basename)).read()

    for logical, (basename, candidates) in wanted.items():
        text, damaged = _decode(read(basename))
        reader = csv.DictReader(io.StringIO(text))
        cols = resolve_columns(
            list(reader.fieldnames or []), candidates, list(candidates),
            f"scripts/lobbying_ingest.py ({basename})",
        )
        rows = list(reader)
        print(f"  {basename}: {len(rows):,} rows"
              + (f", {damaged:,} replacement chars" if damaged else ""))
        out[logical] = (rows, cols, damaged)
    return out


def _txt(value) -> str:
    """The Office writes the string 'null' for empty cells, not an empty cell."""
    s = (value or "").strip()
    return "" if s.lower() == "null" else s


def _num(value) -> int | None:
    s = _txt(value)
    try:
        return int(s)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

# The two composite indexes the read path actually needs, kept in one place so
# a build and a migration cannot disagree about what "indexed" means.
#
# Every department filter in tender_tools is a correlated EXISTS of the shape
#   EXISTS (SELECT 1 FROM communication_dpohs d
#           WHERE d.comlog_id = c.comlog_id AND d.dept_key = ?)
# and the single-column idx_dpoh_dept is the wrong index for it: SQLite seeks on
# dept_key and then filters comlog_id, so the subquery walks EVERY row for that
# department once per communication in the outer scan. That is 110,619 x 11,429
# for ISED, and it is why lobbying-signals took ~80 seconds a department and
# looked hung rather than slow. Leading with comlog_id turns each subquery into a
# point lookup: measured 23.28s -> 0.13s on the same query, ~180x.
#
# Both are covering indexes for their subquery, so neither touches the table.
QUERY_INDEXES = (
    ("idx_dpoh_comlog_dept", "communication_dpohs(comlog_id, dept_key)"),
    ("idx_subj_comlog_subject", "communication_subjects(comlog_id, subject)"),
)


def ensure_query_indexes(con) -> list[str]:
    """
    Create the read-path composite indexes if they are missing.

    Idempotent and additive — it writes no data and changes no row, so it is
    safe on a database built by an older ingest. Returns the names it created,
    empty if they were already there.
    """
    existing = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type = 'index'")}
    created = []
    for name, target in QUERY_INDEXES:
        if name not in existing:
            con.execute(f"CREATE INDEX {name} ON {target}")
            created.append(name)
    if created:
        con.execute("ANALYZE")
    return created


def build_db(members: dict, provenance: dict, archive_members: list,
             max_comms: int | None, window_years: int | None,
             db_path: Path = DB_PATH) -> dict:
    """
    Write the three tables, classifying every office-holder row on the way in.

    `window_years` keeps communications whose COMM_DATE falls within the last N
    years; None keeps all of them. Applied BEFORE --max-comms so a spot-check
    samples the window rather than the archive.

    AMENDMENTS ARE NOT DEDUPLICATED, because the export has already done it.
    A communication that is amended keeps its own COMLOG_ID and points at the
    one it replaces through PREV_COMLOG_ID, and measured against the published
    file the replaced ids are ABSENT from the export — 6,520 amendments, one of
    whose predecessors still appeared as a row. So the file is current-versions
    only, `amends_comlog_id` is provenance rather than a filter, and the counts
    are reported in meta so the next reader can re-measure instead of trusting
    this sentence.
    """
    primary_rows, pcols, _ = members["primary"]
    dpoh_rows, dcols, _ = members["dpoh"]
    subject_rows, scols, _ = members["subjects"]

    total_published = len(primary_rows)
    cutoff = ""
    if window_years is not None:
        cutoff = (datetime.now().replace(year=datetime.now().year - window_years)
                  .strftime("%Y-%m-%d"))
        # A row with no COMM_DATE cannot be placed in time, so it cannot be
        # asserted to be inside the window either. Dropped, and counted.
        primary_rows = [r for r in primary_rows
                        if _txt(r[pcols["comm_date"]]) >= cutoff]
        print(f"  window: {len(primary_rows):,} of {total_published:,} "
              f"communications on or after {cutoff}")
    if max_comms is not None:
        primary_rows = primary_rows[:max_comms]
    keep = {_txt(r[pcols["comlog_id"]]) for r in primary_rows}

    resolver = OrgResolver()
    # A hand-written key that no longer exists in the registry would send its
    # institution quietly into `other_federal` and look like a coverage gap. The
    # check costs nothing and turns that into a startup failure.
    strays = sorted(set(LOBBYING_INSTITUTION_ALIASES.values()) - set(resolver.aliases))
    if strays:
        sys.stderr.write(
            f"LOBBYING_INSTITUTION_ALIASES names keys that are not in "
            f"vault/crosswalk/org_aliases.yaml: {strays}\n"
            f"Fix the mapping in {Path(__file__).name} or add the entry.\n"
        )
        sys.exit(2)

    comms = []
    for r in primary_rows:
        reg_type = _num(r[pcols["reg_type"]])
        client = _txt(r[pcols["client_en"]])
        comms.append((
            _num(r[pcols["comlog_id"]]),
            _txt(r[pcols["comm_date"]]),
            _txt(r[pcols["submission_date"]]),
            _txt(r[pcols["posted_date"]]),
            reg_type,
            REG_TYPES.get(reg_type or 0),
            _num(r[pcols["client_num"]]),
            client,
            _txt(r[pcols["client_fr"]]),
            normalize_vendor(client),
            _num(r[pcols["registrant_num"]]),
            _txt(r[pcols["registrant_last"]]),
            _txt(r[pcols["registrant_first"]]),
            _num(r[pcols["amends"]]),
        ))

    # Classify each DISTINCT institution once, not once per row: 339k rows carry
    # 162 distinct values, and resolution is the expensive part.
    institutions = {_txt(r[dcols["institution"]]) for r in dpoh_rows}
    classified = {name: classify_institution(name, resolver) for name in institutions}
    kind_counts: collections.Counter = collections.Counter()

    dpohs = []
    for r in dpoh_rows:
        comlog = _txt(r[dcols["comlog_id"]])
        if comlog not in keep:
            continue
        raw = _txt(r[dcols["institution"]])
        kind, dept_key, note = classified[raw]
        kind_counts[kind] += 1
        dpohs.append((
            _num(r[dcols["comlog_id"]]),
            _txt(r[dcols["last"]]), _txt(r[dcols["first"]]),
            _txt(r[dcols["title"]]), _txt(r[dcols["branch"]]),
            raw, kind, dept_key, note,
            _txt(r[dcols["other_institution"]]),
        ))

    # Code -> English label, from the archive's own vocabulary member. The
    # table is stored decoded rather than as codes: `subject` is what every
    # caller filters and what `how_to_read` tells them to pass, and a database
    # of SMT-17 would push this join onto every reader of it.
    code_rows, ccols, _ = members["codes"]
    code_labels = {}
    for r in code_rows:
        code, label = _txt(r[ccols["code"]]), _txt(r[ccols["subject"]])
        if code and label:
            code_labels[code] = label

    subjects = []
    unknown_codes: dict[str, int] = {}
    for r in subject_rows:
        comlog = _txt(r[scols["comlog_id"]])
        if comlog not in keep:
            continue
        code = _txt(r[scols["code"]])
        subject = code_labels.get(code, "")
        if not subject:
            # Counted, then fatal below. Dropping the row would understate a
            # subject's coverage and substituting the raw code would put a
            # value in the column that no documented filter matches — both are
            # ways for a filter to answer "none" when the truth is "unmapped".
            unknown_codes[code] = unknown_codes.get(code, 0) + 1
            continue
        # The custom text is only free text when the filer chose 'Other'.
        # Otherwise it repeats the subject, or holds its FRENCH translation
        # ("Mining" / "Mines", "Defence" / "Defense") — 14,113 rows of the
        # published file. Storing that would put French labels in a column
        # callers would reasonably filter in English, so it is dropped where it
        # is not what the dictionary says it is. Measured against the current
        # export it is populated on 8,032 rows and every one of them is 'Other'.
        other = _txt(r[scols["other_subject"]]) if subject.lower() == "other" else ""
        subjects.append((_num(r[scols["comlog_id"]]), subject, other))

    # --- second era: the Details file -------------------------------------
    # Same vocabulary, different packing. Deduplicated against what the first
    # file already produced because the two id ranges overlap (615,832-689,815)
    # even though the date ranges do not, and a communication counted twice
    # would inflate every `matched` this project prints.
    detail_rows, dcols, _ = members["subject_details"]
    seen = {(c, s) for c, s, _ in subjects}
    subjects_from_details = 0
    for r in detail_rows:
        comlog = _txt(r[dcols["comlog_id"]])
        if comlog not in keep:
            continue
        packed = _txt(r[dcols["code"]])
        description = _txt(r[dcols["description"]])
        for code in (x.strip() for x in packed.split(",")):
            if not code:
                continue
            subject = code_labels.get(code, "")
            if not subject:
                unknown_codes[code] = unknown_codes.get(code, 0) + 1
                continue
            key = (_num(r[dcols["comlog_id"]]), subject)
            if key in seen:
                continue
            seen.add(key)
            # DESCRIPTION is prose about the requirement, not a subject label,
            # so it is kept only where 'Other' means the vocabulary genuinely
            # could not carry it — the same rule the older file's custom column
            # gets, for the same reason.
            other = description if subject.lower() == "other" else ""
            subjects.append((key[0], subject, other))
            subjects_from_details += 1

    if unknown_codes:
        listed = ", ".join(f"{c or '(blank)'} x{n:,}"
                           for c, n in sorted(unknown_codes.items(),
                                              key=lambda kv: -kv[1])[:10])
        sys.stderr.write(
            f"Subject codes not in {CODES_MEMBER}: {listed}\n"
            "The vocabulary ships in the same archive as the data, so a code "
            "it does not define means the two members disagree.\n"
            "Resolve it before building: a subject silently missing from the "
            "table is a filter that answers 'none' rather than failing.\n")
        sys.exit(2)

    with staged_db(db_path) as con:
        con.execute("""
            CREATE TABLE communications (
                comlog_id INTEGER PRIMARY KEY,
                comm_date TEXT, submission_date TEXT, posted_date TEXT,
                reg_type INTEGER, reg_type_label TEXT,
                client_num INTEGER, client_name TEXT, client_name_fr TEXT,
                client_norm TEXT,
                registrant_num INTEGER, registrant_last TEXT, registrant_first TEXT,
                amends_comlog_id INTEGER
            )
        """)
        con.execute("""
            CREATE TABLE communication_dpohs (
                comlog_id INTEGER,
                dpoh_last TEXT, dpoh_first TEXT, dpoh_title TEXT, branch TEXT,
                institution TEXT, institution_kind TEXT, dept_key TEXT,
                classification_note TEXT, other_institution TEXT
            )
        """)
        con.execute("""
            CREATE TABLE communication_subjects (
                comlog_id INTEGER, subject TEXT, other_subject TEXT
            )
        """)
        con.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        con.executemany("INSERT INTO communications VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", comms)
        con.executemany("INSERT INTO communication_dpohs VALUES (?,?,?,?,?,?,?,?,?,?)", dpohs)
        con.executemany("INSERT INTO communication_subjects VALUES (?,?,?)", subjects)
        con.execute("CREATE INDEX idx_comm_date ON communications(comm_date)")
        con.execute("CREATE INDEX idx_client_norm ON communications(client_norm)")
        con.execute("CREATE INDEX idx_dpoh_comlog ON communication_dpohs(comlog_id)")
        con.execute("CREATE INDEX idx_dpoh_dept ON communication_dpohs(dept_key)")
        con.execute("CREATE INDEX idx_subj_comlog ON communication_subjects(comlog_id)")
        con.execute("CREATE INDEX idx_subj ON communication_subjects(subject)")
        ensure_query_indexes(con)

        dates = [c[1] for c in comms if c[1]]
        # Hoisted deliberately. Inlining this set into the generator that reads
        # it rebuilds all ~446k entries once per communication — 110,619 x
        # 446,000, which ran for 77 minutes at full CPU with no disk activity
        # before it was caught. The cost of the comprehension is invisible at
        # the point of use, which is exactly why it belongs on its own line.
        coded_comlogs = {s[0] for s in subjects}
        amendments = sum(1 for c in comms if c[13] is not None)
        superseded_present = len(
            {c[13] for c in comms if c[13] is not None} & {c[0] for c in comms}
        )
        stats = {
            "ingest_date": datetime.now().strftime("%Y-%m-%d"),
            "dataset_page": DATASET_PAGE,
            # Provenance for a hand-acquired input: which bytes, from where,
            # when they landed, and the members they contained. Without this a
            # rebuild cannot be told from a re-run against a stale copy.
            **provenance,
            "archive_members": ", ".join(
                m["filename"] for m in archive_members) or "(directory source)",
            "licence": "Open Government Licence - Canada",
            "communications": str(len(comms)),
            "dpoh_rows": str(len(dpohs)),
            "subject_rows": str(len(subjects)),
            # Split by era so a reader can see the seam rather than infer it.
            # If the second number is ever 0 on an archive that contains the
            # Details member, subject coverage has silently reverted to ending
            # 2024-09-30 and every --subject answer is describing the wrong
            # period — which is exactly how this was missed the first time.
            "subject_rows_from_details": str(subjects_from_details),
            "subject_coverage_latest": max(
                (c[1] for c in comms if c[1] and c[0] in coded_comlogs),
                default=""),
            # What the window means, in the terms a reader needs to judge a
            # count: how many the Office published, how many survived, and the
            # date the cut was made at. "No meetings" and "no meetings in the
            # last three years" are different claims.
            "communications_published": str(total_published),
            "window_years": "" if window_years is None else str(window_years),
            "window_cutoff": cutoff,
            "earliest_communication": min(dates) if dates else "",
            "latest_communication": max(dates) if dates else "",
            "amendments": str(amendments),
            "superseded_still_present": str(superseded_present),
            "distinct_institutions": str(len(institutions)),
            "procurement_communications": str(len(
                {s[0] for s in subjects if s[1] == PROCUREMENT_SUBJECT})),
        }
        for kind in (KIND_DEPARTMENT, KIND_DEPARTMENT_UNMAPPED, KIND_MINISTER_OFFICE,
                     KIND_PARLIAMENT, KIND_OTHER_FEDERAL, KIND_UNSPECIFIED):
            stats[f"dpoh_rows_{kind}"] = str(kind_counts.get(kind, 0))
        for k, v in stats.items():
            con.execute("INSERT INTO meta VALUES (?, ?)", (k, v))

    return {"communications": len(comms), "dpohs": len(dpohs),
            "subjects": len(subjects), "kinds": kind_counts,
            "institutions": institutions, "classified": classified,
            "amendments": amendments, "superseded_present": superseded_present}


def show_institutions(result: dict, dpoh_rows: list, dcols: dict) -> None:
    """
    Print every distinct institution with its population and key — the audit aid.

    The classification is the one judgement call this ingest makes, so it is
    printable in full. 162 values is a list a person can actually read, and
    moving a string between populations is a one-line change once you can see
    which population it landed in.
    """
    counts = collections.Counter(_txt(r[dcols["institution"]]) for r in dpoh_rows)
    by_kind: dict[str, list] = collections.defaultdict(list)
    for name, (kind, key, _note) in result["classified"].items():
        by_kind[kind].append((counts.get(name, 0), name, key))
    for kind in (KIND_DEPARTMENT, KIND_DEPARTMENT_UNMAPPED, KIND_MINISTER_OFFICE,
                 KIND_PARLIAMENT, KIND_OTHER_FEDERAL, KIND_UNSPECIFIED):
        rows = sorted(by_kind.get(kind, []), reverse=True)
        print(f"\n=== {kind} ({len(rows)} institutions, "
              f"{sum(r[0] for r in rows):,} office-holder rows) ===")
        for n, name, key in rows:
            print(f"  {n:>7,}  {name}" + (f"   -> {key}" if key else ""))


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "The zip cannot be fetched automatically: lobbycanada.gc.ca answers\n"
            "403 to every non-browser request (Cloudflare interactive challenge).\n"
            f"Download it once from\n  {DATASET_PAGE}\n"
            f"(direct link {ZIP_URL})\nand pass the file with --source.\n"
        ),
    )
    parser.add_argument(
        "--source", type=Path, default=None,
        help=f"Path to the official communications zip, or a directory holding "
             f"the extracted CSVs. Defaults to data/source/lobbying/"
             f"{COMMUNICATIONS_ARCHIVE} if present. The archive is an INPUT "
             f"ARTIFACT acquired out-of-band — see --help notes.",
    )
    parser.add_argument(
        "--db", type=Path, default=None,
        help=f"Output DB path (default {DB_PATH}).",
    )
    parser.add_argument(
        "--window-years", type=int, default=None,
        help="Keep communications from the last N years (default: "
             "lobbying_window_years in the profile, or 3).",
    )
    parser.add_argument(
        "--all-years", action="store_true",
        help="Keep every communication back to July 2008. Roughly 5x the rows "
             "of the default window, and most of it is about institutions that "
             "no longer exist.",
    )
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    parser.add_argument(
        "--max-comms", type=int, default=None,
        help="Keep only the first N communications (spot-check). Redirects "
             "output to a .sample database unless --db says otherwise.",
    )
    parser.add_argument("--show-institutions", action="store_true",
                        help="Print every institution and the population it "
                             "was classified into (audit aid)")
    parser.add_argument(
        "--reindex", action="store_true",
        help="Add the read-path composite indexes to an existing database and "
             "stop. Additive and idempotent: it writes no rows and re-reads no "
             "archive, so it does not touch provenance. For a database built "
             "by an ingest that predates those indexes.",
    )
    args = parser.parse_args()

    if args.reindex:
        db_path = args.db or DB_PATH
        if not db_path.exists():
            sys.stderr.write(f"\nNo database at {db_path} — nothing to reindex.\n")
            sys.exit(2)
        con = sqlite3.connect(db_path)
        created = ensure_query_indexes(con)
        con.commit()
        con.close()
        if created:
            print(f"Created on {db_path}: {', '.join(created)}")
        else:
            print(f"{db_path} already carries the read-path indexes.")
        return

    try:
        source = locate_source(args.source)
        archive_members = validate_archive(source)
    except SourceUnavailable as exc:
        # The distinction the whole acquisition design rests on: this is the
        # environment failing to reach a live, published dataset, not the
        # dataset being empty. Exiting non-zero keeps a caller from proceeding
        # as though the answer were "no lobbying activity", and NOTHING falls
        # back to a previously built database — a stale corpus served as
        # current is the one outcome worse than no corpus at all.
        sys.stderr.write("\n" + str(exc) + "\n")
        sys.exit(2)

    # --source is NOT a sampling reason here, unlike every sibling ingest: it is
    # the only way to reach the published data at all, so treating it as a
    # spot-check would mean this ingest could never write its real database.
    # --max-comms is the flag that truncates, and it is the one guarded.
    db_path = output_path(
        DB_PATH, args.db,
        f"--max-comms {args.max_comms} keeps only the first "
        f"{args.max_comms} communications" if args.max_comms else None,
    )

    if args.all_years and args.window_years is not None:
        sys.stderr.write("--all-years and --window-years contradict each other.\n")
        sys.exit(2)
    if args.all_years:
        window_years = None
    elif args.window_years is not None:
        window_years = args.window_years
    else:
        window_years = parse_profile(args.profile).get("lobbying_window_years", 3)

    provenance = archive_provenance(source)
    print(f"  sha256 {provenance['source_sha256']}")
    print(f"  acquired {provenance['source_acquired']}")

    print(f"Reading {source}")
    members = read_members(source)
    result = build_db(members, provenance, archive_members,
                      args.max_comms, window_years, db_path)

    print(f"\nWrote {result['communications']:,} communications, "
          f"{result['dpohs']:,} office-holder rows and "
          f"{result['subjects']:,} subject rows to {db_path} "
          f"({db_path.stat().st_size/1e6:.1f} MB)")
    print(f"  amendments: {result['amendments']:,} "
          f"(superseded ids still present: {result['superseded_present']})")
    print("  office-holder rows by population:")
    for kind, n in result["kinds"].most_common():
        print(f"    {kind:<22} {n:>8,}")

    if args.show_institutions:
        show_institutions(result, members["dpoh"][0], members["dpoh"][1])

    print("\nAttribution: contains information licensed under the "
          "Open Government Licence - Canada.")


if __name__ == "__main__":
    main()
