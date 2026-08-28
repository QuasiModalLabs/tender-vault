"""
Ingest Office of the Auditor General (OAG) performance-audit metadata and score
each audit for IT-relevance, so the departments an independent authority has
flagged become a pre-RFP signal.

Source: open.canada.ca CKAN Action API, organization 'oag-bvg'. Read-only GET,
no API key. Licence: Open Government Licence - Canada.

WHY THIS EXISTS — the third signal, and the strongest for credibility. Contracts
say WHAT a department bought; plans say what they INTEND to modernize; OAG says
what an independent watchdog has PUBLICLY found them failing at. For a pre-RFP
conversation "the AG flagged your processing backlog in 2023" is far more citable
than a department's own planning prose. And OAG findings are exactly the
"impending scrutiny" that, in the chronic-strain thesis, forces a department to
procure a fix.

CONVERGENCE IS THE POINT. This is built to JOIN, not stand alone. Each audit
attaches to the departments it examined, by CANONICAL KEY from
vault/crosswalk/org_aliases.yaml — the same registry the crosswalk keys on, so
an audit, a plan and a contract name one organization the same way. The real
signal is CONVERGENCE: when OAG, plans, and an expiring contract all point at
the same department+capability, that is the strongest possible pre-RFP case.

ATTRIBUTION IS A JOIN TABLE, not a column, for two reasons.

Half the resolved federal audits name MORE THAN ONE department — ArriveCAN
audited CBSA, PHAC and PSPC together — so a single column threw away half the
signal by keeping whichever name matched first.

And most of this corpus audits no federal department AT ALL. Of 364 records
only ~110 are federal-executive audits; the rest are committee briefing
packages, the OAG's own quarterly financials and annual returns, Crown
corporation special examinations, and territorial audits. Each of those carries
an attribution_status saying WHY it has no department, because an empty string
reads as failure and invented a 62% shortfall that was never real.

Briefing packages INHERIT from the report they are about — found by the report's
document id in their own resource links, or failing that by the report number
they cite — because a briefing names no department of its own, and scanning its
text attributes the audit to whichever parliamentary committee heard it.

Scoring reuses the two-pole semantic theming proven in plans_ingest: score the
audit's title+description toward an IT/systems theme, away from a non-IT-audit
theme (financial statements, environmental, benefits-administration). Runs
locally at ingest, zero Claude tokens.

Usage:
    python scripts/oag_ingest.py
    python scripts/oag_ingest.py --show-extremes
    python scripts/oag_ingest.py --max-audits 500   # cap the API pull
"""
from __future__ import annotations

import argparse
import collections
import re
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))
from ingest import parse_profile  # noqa: E402
from ingest_common import (  # noqa: E402
    REQUEST_HEADERS, output_path, resolve_columns, staged_db,
)
# Reuse the proven scoring helpers from plans_ingest.
from plans_ingest import theme_vector, _strip_md, EMBED_MODEL  # noqa: E402
# Departments resolve against vault/crosswalk/org_aliases.yaml — the same
# registry the crosswalk keys on, so an audit and a contract name one thing.
from org_resolve import OrgResolver  # noqa: E402

API = "https://open.canada.ca/data/api/3/action/package_search"
ORG = "oag-bvg"
PROJECT_ROOT = Path(__file__).parent.parent
DEFAULT_PROFILE = PROJECT_ROOT / "vault" / "profiles" / "my-company.md"
DB_PATH = PROJECT_ROOT / "data" / "oag.db"
# What a full, publishable run pulls. --max-audits below this is a spot-check.
FULL_MAX_AUDITS = 400

# CKAN package fields this script depends on, resolved against the real response
# so an API-side rename exits loudly instead of scoring empty strings. Presence
# of the KEY is what's checked — a null or empty `notes` is legitimate data,
# exactly as a null cell in a CSV column is.
OAG_PACKAGE_FIELDS = {
    "oag_id": ["id"],
    "title": ["title"],
    "notes": ["notes"],
    "resources": ["resources"],
    # open.canada.ca's own document taxonomy. It separates the three populations
    # this corpus mixes far more reliably than reading the title ever did:
    # parliament_committee_deputy (briefing packages), parliament_report and
    # publication (the reports and OAG's own admin documents).
    "collection": ["collection"],
    # When this record became public, as a real date rather than the year
    # scraped out of the title. `year` is what the title says and is enough to
    # sort by; it is not enough to answer "was this knowable on 2022-04-01",
    # which is the question an as-of gate asks, and answering it by assuming
    # January 1 credits an audit tabled in November with ten months it did not
    # have.
    #
    # NOT a required field, and not the tabling date. An OAG report is tabled in
    # Parliament and public before it reaches open.canada.ca, so this stamp runs
    # LATE. That is the safe direction — a late date can only withhold a signal
    # from a backtest, never leak one — and it is a real lag, not a rounding:
    # checked across all 374 records, date_published tracks the report year
    # closely at the low end (2023 reports first appear 2023-03-27) with a long
    # tail from the briefing packages, which are prepared for hearings held
    # months or years after the report they are about.
    "date_published": ["date_published", "portal_release_date"],
}
# Sub-fields read off each entry in `resources` when picking HTML/PDF links.
OAG_RESOURCE_FIELDS = {
    "format": ["format"],
    "url": ["url"],
}


def resolve_resource_fields(records: list[dict], pkg_cols: dict) -> dict[str, str | None]:
    """
    Resolve the resource sub-fields against the first package that actually has
    resources. A response where no package carries any is legal (nothing to
    link), so that resolves to empty rather than exiting — there is no schema
    to check, which is different from a schema that changed.
    """
    for pkg in records:
        resources = pkg.get(pkg_cols["resources"]) or []
        if resources:
            return resolve_columns(
                list(resources[0].keys()), OAG_RESOURCE_FIELDS,
                list(OAG_RESOURCE_FIELDS), "scripts/oag_ingest.py (resources)",
            )
    print("  no package carries resources; skipping resource-field check")
    return {k: None for k in OAG_RESOURCE_FIELDS}

# Only these look like actual performance/audit reports (not briefing packages,
# financial-statement audits, or committee hearing materials). We keep audits
# AND committee briefing packages, because the latter are the "scrutiny
# materialized" signal — but tag which is which.
def classify_doc(title: str, notes: str) -> str:
    t = (title or "").lower()
    if "briefing package" in t or "hearing before" in t:
        return "committee_hearing"      # scrutiny materialized (PACP/OGGO/etc.)
    if "reports of the auditor general" in t or "report of the commissioner" in t:
        return "performance_audit"
    if "special examination" in t:
        return "special_examination"
    if "financial audit" in t or "financial statements" in t:
        return "financial_audit"
    return "other"


# IT/systems theme vs non-IT-audit theme. Defined by examples (profile can
# override via oag_themes); the model generalizes to unlisted phrasings.
DEFAULT_IT_AUDIT = [
    "audit of the department's aging IT systems and technology modernization",
    "failures in a case management or processing system causing backlogs",
    "problems delivering a digital service or online application platform",
    "weaknesses in cyber security of government networks and systems",
    "delays and cost overruns in a major IT or software project",
    "modernizing legacy technology infrastructure to meet service demand",
]
DEFAULT_NON_IT_AUDIT = [
    "audit of financial statements and public accounts",
    "environmental and climate change protection programs",
    "administration of benefits, grants and transfer payments",
    "physical infrastructure such as bridges, buildings and procurement of goods",
]

# ---------------------------------------------------------------------------
# What KIND of record this is — decided before any department is looked for
# ---------------------------------------------------------------------------
#
# THE CORPUS IS NOT 364 FEDERAL AUDITS, and treating it as though it were is
# what made the old attribution look 62% empty. Only 110 records audit a federal
# department. 148 are committee briefing packages, whose subject is a report
# published separately and already in this corpus. 73 are the OAG's own
# quarterly financials, Ombuds reports, ATIP and privacy returns — documents
# about the auditor, not audits of anyone. 26 examine Crown corporations and 7
# audit territorial governments, and neither has a federal department by
# construction.
#
# So 106 records SHOULD carry no department, and reporting them as a gap
# invented a shortfall. Each class below is refused explicitly, with a status
# saying why, instead of resolving to an empty string that reads like failure.

CLASS_FEDERAL_AUDIT = "federal_audit"
CLASS_BRIEFING = "committee_briefing"
CLASS_OAG_ADMIN = "oag_own_admin"
CLASS_CROWN_OTHER = "crown_or_other_entity"
CLASS_TERRITORIAL = "territorial"

# Committee briefing packages. `collection` names these exactly, but the title
# is checked too because collection is a publisher field and this classification
# decides whether a record is scanned at all.
_BRIEFING_COLLECTION = "parliament_committee_deputy"
_BRIEFING_TITLE = re.compile(
    r"briefing package|briefing binder|hearing before|appearance of the|tabling of", re.I)

# Audits of territorial governments. OAG audits Yukon, the NWT and Nunavut under
# separate statutory mandates; their departments are territorial and must never
# reach a federal dossier. The old regex fallback produced exactly these —
# "Department of Health and Social Services" is Yukon's, not Canada's.
_TERRITORIAL = re.compile(
    r"\b(yukon|northwest territories|nunavut|legislative assembly)\b", re.I)

# Special examinations and other-entity audits. The audited body is a Crown
# corporation or a separate legal entity, not a department.
_CROWN_OTHER = re.compile(
    r"special examination|board of directors|to the trustees of|"
    r"to the governor in council|joint auditors", re.I)

# The OAG's own administrative publications.
_OAG_ADMIN = re.compile(
    r"quarterly financial report|departmental (plan|results)|employment equity"
    r"|travel, hospitality|accessibility plan|fees report|ombuds annual report"
    r"|commentary on the .{0,20}financial audits|multiculturalism questionnaire"
    r"|annual report on the (access to information|privacy) act"
    r"|report of the audit committee|international peer review"
    r"|open government implementation|opening statement"
    r"|status of the official languages|costs of crown corporation"
    r"|future-oriented|petitions annual report|biodiversity in canada", re.I)


def classify_record(title: str, notes: str, collection: str | None) -> str:
    """
    Which population this record belongs to.

    Order matters: a briefing package ABOUT a Crown corporation audit is still a
    briefing package, and inherits from the report rather than being refused.
    """
    title = title or ""
    if collection == _BRIEFING_COLLECTION or _BRIEFING_TITLE.search(title):
        return CLASS_BRIEFING
    body = f"{title} {notes or ''}"
    if _TERRITORIAL.search(body):
        return CLASS_TERRITORIAL
    if _CROWN_OTHER.search(body):
        return CLASS_CROWN_OTHER
    if _OAG_ADMIN.search(body):
        return CLASS_OAG_ADMIN
    return CLASS_FEDERAL_AUDIT


# Why a record carries no department. An empty string said "we failed"; these
# distinguish "nothing to find" from "look at this one".
STATUS_RESOLVED = "RESOLVED"
STATUS_GOVWIDE = "UNRESOLVED_GOVWIDE"
STATUS_NOT_FEDERAL_EXEC = "UNRESOLVED_NOT_FEDERAL_EXEC"
STATUS_OAG_INTERNAL = "UNRESOLVED_OAG_INTERNAL"
STATUS_REVIEW = "UNRESOLVED_REVIEW"

_CLASS_REFUSAL = {
    CLASS_OAG_ADMIN: STATUS_OAG_INTERNAL,
    CLASS_CROWN_OTHER: STATUS_NOT_FEDERAL_EXEC,
    CLASS_TERRITORIAL: STATUS_NOT_FEDERAL_EXEC,
}

# Named private vendors. An audit INTO a vendor is competitive intelligence
# whatever department it touches — "did the AG examine the firm I am bidding
# against" is a question this corpus can answer, and it would otherwise be
# buried in a review queue because these audits span too many departments to
# attribute to one.
#
# A corporate suffix ALONE does not identify a vendor, and this is the trap: the
# federal family is full of incorporated bodies. "Atomic Energy of Canada
# Limited" is a Crown corporation named as one of three AUDITED ENTITIES, and a
# bare suffix match tags it as a supplier. So the vendor must appear in a
# CONTRACTING relationship — contracts or payments awarded TO it, or held WITH
# it — which is what makes the audit about the firm rather than about a
# department that happens to be incorporated.
_CORPORATE_NAME = (
    r"(?:[A-Z][\w&.‑-]*\s+(?:of\s+|and\s+|de\s+)?){0,4}"
    r"[A-Z][\w&.‑-]*\s*(?:Inc\.?|Ltd\.?|LLP|LLC|Limited|Corp\.?|&\s*Company)"
)
_VENDOR = re.compile(
    r"(?:contracts?|payments?|procurement)[^.]{0,60}?\b(?:to|with|awarded to)\s+"
    r"(" + _CORPORATE_NAME + r")\b"
    r"|\b(?:with|to)\s+(" + _CORPORATE_NAME + r")\b[^.]{0,40}?\bcontracts?\b",
    re.I,
)


def extract_vendor(title: str, notes: str) -> str:
    """The private firm an audit is ABOUT, if it is about one."""
    m = _VENDOR.search(f"{title or ''} . {notes or ''}")
    if not m:
        return ""
    return re.sub(r"\s+", " ", m.group(1) or m.group(2)).strip(" .,")


def fetch_oag(max_audits: int) -> list[dict]:
    """Page through the CKAN API for all oag-bvg datasets."""
    out, start, rows = [], 0, 100
    while len(out) < max_audits:
        params = {"q": f"organization:{ORG}", "rows": rows, "start": start}
        r = requests.get(API, params=params, headers=REQUEST_HEADERS, timeout=60)
        r.raise_for_status()
        payload = r.json()
        if not payload.get("success") or "result" not in payload:
            sys.stderr.write(
                f"CKAN API returned no result envelope from {API}\n"
                f"  {payload.get('error', payload)}\n"
            )
            sys.exit(2)
        result = payload["result"]
        batch = result["results"]
        if not batch:
            break
        out.extend(batch)
        total = result["count"]
        print(f"  fetched {len(out)}/{total}")
        start += rows
        if start >= total:
            break
        time.sleep(0.3)  # be polite to the API
    return out[:max_audits]


def year_from_title(title: str) -> int | None:
    m = re.search(r"\b(20\d{2})\b", title or "")
    return int(m.group(1)) if m else None


# ---------------------------------------------------------------------------
# Linking a briefing package to the report it is about
# ---------------------------------------------------------------------------
#
# A briefing package names no department of its own — it is prepared for a
# hearing ON a report, and the report is what audited someone. Scanning the
# briefing's own text is how the Canadian Heritage committee ends up owning an
# audit of ISED. So briefings inherit, and the only question is which report.
#
# Two ways to answer it, in strict order of trustworthiness.

# 1. THE DOCUMENT ID. OAG report URLs end in a numeric document id, and a
#    briefing package's resources include the report's own URL. 140 ids are
#    owned by non-briefing records with zero collisions, so this is a structural
#    join and not a heuristic.
_OAG_DOC_ID = re.compile(r"/internet/(?:English|Francais)/\w*?_(\d+)\.html", re.I)

# 2. THE CITATION. "2024 Report 1, ArriveCAN". Weaker, and unusable without the
#    ISSUER: the Auditor General and the Commissioner of the Environment each
#    number their reports 1..N within a year, so (year, number) alone collides
#    on 43 of 55 keys. With the issuer, zero collide.
_REPORT_NO = re.compile(r"\bReports?\s+(\d+)\b", re.I)
_REPORT_RANGE = re.compile(r"\bReports?\s+(\d+)\s*(?:to|through|-|–)\s*(\d+)\b", re.I)
_LEADING_YEAR = re.compile(r"^\s*(20\d{2})")
_ANY_YEAR = re.compile(r"\b(20\d{2})\b")
# A bundle wider than this is a parsing accident, not a hearing agenda.
MAX_REPORT_RANGE = 15


def doc_ids(pkg: dict, cols: dict, res_cols: dict) -> set[str]:
    """OAG document ids referenced by a package's resources."""
    if not res_cols.get("url"):
        return set()
    out = set()
    for res in (pkg.get(cols["resources"]) or []):
        m = _OAG_DOC_ID.search(res.get(res_cols["url"]) or "")
        if m:
            out.add(m.group(1))
    return out


def issuer_of(text: str) -> str | None:
    """AG or CESD — which office published this. Required to read a citation."""
    low = (text or "").lower()
    if "commissioner of the environment" in low or "cesd" in low:
        return "CESD"
    if "auditor general" in low or re.search(r"\bag\b", low):
        return "AG"
    return None


def cited_reports(text: str) -> tuple[str | None, set[str]]:
    """The (issuer, {report numbers}) a briefing package says it is about."""
    issuer = issuer_of(text)
    years = _ANY_YEAR.findall(text or "")
    if not issuer or not years:
        return None, set()
    nums = set()
    for m in _REPORT_RANGE.finditer(text):
        lo, hi = int(m.group(1)), int(m.group(2))
        if 0 < hi - lo < MAX_REPORT_RANGE:
            nums |= {str(n) for n in range(lo, hi + 1)}
    for m in _REPORT_NO.finditer(text):
        nums.add(m.group(1))
    return (issuer, years[0]), nums


def index_reports(records: list[dict], classes: list[str], cols: dict,
                  res_cols: dict) -> tuple[dict[str, int], dict[tuple, int]]:
    """
    Two lookups from the non-briefing records: by document id, and by citation.

    A citation key claimed by more than one report is dropped rather than
    guessed — an ambiguous inheritance is exactly the plausibly-wrong tag this
    rebuild exists to stop producing.
    """
    by_doc: dict[str, int] = {}
    by_cite: dict[tuple, list[int]] = {}
    for i, (pkg, cls) in enumerate(zip(records, classes)):
        if cls == CLASS_BRIEFING:
            continue
        for doc in doc_ids(pkg, cols, res_cols):
            by_doc.setdefault(doc, i)
        title = pkg.get(cols["title"], "") or ""
        year = _LEADING_YEAR.search(title)
        number = _REPORT_NO.search(title)
        issuer = issuer_of(title)
        if year and number and issuer:
            by_cite.setdefault((issuer, year.group(1), number.group(1)), []).append(i)
    return by_doc, {k: v[0] for k, v in by_cite.items() if len(v) == 1}


def pick_urls(pkg: dict, cols: dict, res_cols: dict) -> tuple[str, str]:
    """
    The best English HTML and PDF link for a record.

    NOT simply the first resource of each format, which is what this used to
    take. Packages carry English and French pairs in arbitrary order and some
    URLs are site-relative, so the old picker stored French pages and bare paths.

    Nothing here can rescue the www.oag-bvg.gc.ca links: all 890 of them now
    return an error page under HTTP 200, which is why the department comes from
    the metadata and no report body is fetched. Preference order puts the hosts
    that still serve content first.
    """
    if not res_cols.get("url"):
        return "", ""

    def rank(url: str) -> int:
        if "/Francais/" in url or "/fr/" in url or "noscommunes" in url:
            return 3                      # French pair member
        if not url.startswith("http"):
            return 2                      # site-relative, unusable as stored
        if "oag-bvg.gc.ca" in url:
            return 1                      # resolves, but every one is a soft 404
        return 0

    best: dict[str, tuple[int, str]] = {}
    for res in (pkg.get(cols["resources"]) or []):
        fmt = (res.get(res_cols["format"]) or "").upper() if res_cols["format"] else ""
        if fmt not in ("HTML", "PDF"):
            continue
        url = res.get(res_cols["url"]) or ""
        if not url:
            continue
        if fmt not in best or rank(url) < best[fmt][0]:
            best[fmt] = (rank(url), url)
    return best.get("HTML", (0, ""))[1], best.get("PDF", (0, ""))[1]


def attribute(records: list[dict], cols: dict, res_cols: dict,
              resolver) -> tuple[list[str], list[str], list[dict]]:
    """
    Decide each record's class, status, and the departments it attaches to.

    Returns (classes, statuses, edges). One record can produce several edges —
    half the resolved federal audits name more than one department — and each
    edge carries how it was reached, so a dossier can show a direct finding and
    exclude an inherited one.
    """
    classes = [
        classify_record(pkg.get(cols["title"], "") or "",
                        pkg.get(cols["notes"], "") or "",
                        pkg.get(cols["collection"]) if cols.get("collection") else None)
        for pkg in records
    ]

    # Every non-briefing record's own departments, needed before briefings can
    # inherit from them.
    own: list[dict[str, str]] = []
    for pkg, cls in zip(records, classes):
        if cls == CLASS_FEDERAL_AUDIT:
            text = f"{pkg.get(cols['title'], '') or ''} . {pkg.get(cols['notes'], '') or ''}"
            own.append(resolver.scan(text))
        else:
            own.append({})

    by_doc, by_cite = index_reports(records, classes, cols, res_cols)
    ids = [pkg.get(cols["oag_id"], "") for pkg in records]

    statuses: list[str] = []
    edges: list[dict] = []
    for i, (pkg, cls) in enumerate(zip(records, classes)):
        if cls in _CLASS_REFUSAL:
            statuses.append(_CLASS_REFUSAL[cls])
            continue

        if cls == CLASS_FEDERAL_AUDIT:
            for key, evidence in own[i].items():
                edges.append({
                    "oag_id": ids[i], "dept_key": key, "method": "direct",
                    "evidence": evidence, "source_oag_id": None,
                    "n_parent_reports": None,
                })
            statuses.append(STATUS_RESOLVED if own[i] else STATUS_GOVWIDE)
            continue

        # A briefing package: find its parent report(s), then inherit.
        parents = sorted({by_doc[d] for d in doc_ids(pkg, cols, res_cols) if d in by_doc})
        method = "inherited_url"
        if not parents:
            text = f"{pkg.get(cols['title'], '') or ''} . {pkg.get(cols['notes'], '') or ''}"
            key, numbers = cited_reports(text)
            if key:
                parents = sorted({by_cite[(key[0], key[1], n)] for n in numbers
                                  if (key[0], key[1], n) in by_cite})
            method = "inherited_citation"

        inherited: dict[str, tuple[str, str]] = {}
        for p in parents:
            for dept, evidence in own[p].items():
                inherited.setdefault(dept, (evidence, ids[p]))
        for dept, (evidence, source_id) in inherited.items():
            edges.append({
                "oag_id": ids[i], "dept_key": dept, "method": method,
                "evidence": evidence,
                # Which report carried the finding, and how many reports the
                # hearing covered. A department reached through a 5-report
                # bundle is weaker evidence than one reached through a single
                # report — recorded as a number to threshold on, not as a rule
                # baked in here.
                "source_oag_id": source_id if len(parents) == 1 else None,
                "n_parent_reports": len(parents),
            })
        statuses.append(STATUS_RESOLVED if inherited else STATUS_REVIEW)
    return classes, statuses, edges


def _published_date(pkg: dict, cols: dict) -> str:
    """
    The record's portal publication date as a bare ISO date, or "".

    CKAN returns `2026-08-11 00:00:00`; the midnight is not a time the record
    was published, it is the absence of one. Truncating to ten characters keeps
    the column comparable by string inequality and stops that fake precision
    reading as real. "" means the field was missing — which is a third answer,
    distinct from an early date and a late one, and callers must not read it as
    either.
    """
    col = cols.get("date_published")
    if not col:
        return ""
    raw = str(pkg.get(col) or "").strip()
    if len(raw) >= 10 and raw[:4].isdigit() and raw[4] == "-":
        return raw[:10]
    return ""


def build_db(records: list[dict], scores: list, source_note: str,
             cols: dict, res_cols: dict, resolver, db_path: Path = DB_PATH) -> dict:
    # Staged write: a failure part-way through leaves the previous database
    # untouched instead of leaving none.
    classes, statuses, edges = attribute(records, cols, res_cols, resolver)

    rows = []
    for pkg, score, cls, status in zip(records, scores, classes, statuses):
        title = pkg.get(cols["title"], "") or ""
        notes = _strip_md(pkg.get(cols["notes"], "") or "")
        html_url, pdf_url = pick_urls(pkg, cols, res_cols)
        rows.append((
            pkg.get(cols["oag_id"], ""),
            year_from_title(title),
            _published_date(pkg, cols),
            classify_doc(title, notes),
            cls, status,
            title, notes,
            extract_vendor(title, notes) if cls == CLASS_FEDERAL_AUDIT else "",
            score, html_url, pdf_url,
        ))

    with staged_db(db_path) as con:
        con.execute("""
            CREATE TABLE audits (
                oag_id TEXT PRIMARY KEY, year INTEGER,
                -- Portal publication date. Runs late relative to tabling; see
                -- OAG_PACKAGE_FIELDS. "" where the API filed none.
                date_published TEXT,
                doc_type TEXT,
                record_class TEXT, attribution_status TEXT,
                title TEXT, description TEXT, vendor_focus TEXT,
                it_score REAL, html_url TEXT, pdf_url TEXT
            )
        """)
        # One row per (audit, department). The department is a CANONICAL KEY
        # from org_aliases.yaml, never a free string, so the junk the old regex
        # produced is not representable here.
        con.execute("""
            CREATE TABLE audit_departments (
                oag_id TEXT NOT NULL,
                dept_key TEXT NOT NULL,
                method TEXT NOT NULL,
                evidence TEXT,
                source_oag_id TEXT,
                n_parent_reports INTEGER,
                PRIMARY KEY (oag_id, dept_key, method)
            )
        """)
        con.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        con.executemany("INSERT INTO audits VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        con.executemany(
            "INSERT OR IGNORE INTO audit_departments VALUES "
            "(:oag_id,:dept_key,:method,:evidence,:source_oag_id,:n_parent_reports)",
            edges)
        con.execute("CREATE INDEX idx_itscore ON audits(it_score)")
        con.execute("CREATE INDEX idx_published ON audits(date_published)")
        con.execute("CREATE INDEX idx_type ON audits(doc_type)")
        con.execute("CREATE INDEX idx_class ON audits(record_class)")
        con.execute("CREATE INDEX idx_status ON audits(attribution_status)")
        con.execute("CREATE INDEX idx_vendor ON audits(vendor_focus)")
        con.execute("CREATE INDEX idx_ad_dept ON audit_departments(dept_key)")
        con.execute("CREATE INDEX idx_ad_oag ON audit_departments(oag_id)")

        counts = collections.Counter(classes)
        resolved = collections.Counter(
            c for c, s in zip(classes, statuses) if s == STATUS_RESOLVED)
        for k, v in [
            ("ingest_date", datetime.now().strftime("%Y-%m-%d")),
            ("source", source_note),
            ("audit_count", str(len(rows))),
            ("federal_audits", str(counts[CLASS_FEDERAL_AUDIT])),
            ("federal_audits_resolved", str(resolved[CLASS_FEDERAL_AUDIT])),
            ("briefings", str(counts[CLASS_BRIEFING])),
            ("briefings_resolved", str(resolved[CLASS_BRIEFING])),
            ("department_edges", str(len(edges))),
            ("licence", "Open Government Licence - Canada"),
        ]:
            con.execute("INSERT INTO meta VALUES (?, ?)", (k, v))

    return {"rows": len(rows), "edges": len(edges),
            "classes": collections.Counter(classes),
            "resolved": resolved}


_EXTREMES_SQL = """
    SELECT a.title, a.it_score, a.doc_type, a.attribution_status,
           (SELECT group_concat(d.dept_key, ', ')
              FROM audit_departments d WHERE d.oag_id = a.oag_id)
    FROM audits a
    ORDER BY a.it_score {order} LIMIT ?
"""


def show_extremes(n: int = 10, db_path: Path = DB_PATH) -> None:
    con = sqlite3.connect(db_path)
    for label, order in [
        ("HIGHEST it_score (should read as IT/systems/digital audits)", "DESC"),
        ("LOWEST it_score (should read as financial/environmental/benefits)", "ASC"),
    ]:
        print(f"\n=== {label} ===")
        for title, sc, dt, status, depts in con.execute(
                _EXTREMES_SQL.format(order=order), (n,)):
            who = depts or f"({status})"
            print(f"[{sc:+.3f}] ({dt}) {who}\n    {title[:150]}\n")
    con.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    # default=None, not 400, so we can tell "user asked for a subset" apart from
    # "full run". Passing it at all is a sampling run and redirects the output.
    ap.add_argument(
        "--max-audits", type=int, default=None,
        help=f"Cap the API pull (full run: {FULL_MAX_AUDITS}). Passing this "
             "redirects output to a .sample DB unless --db is also given.",
    )
    ap.add_argument("--show-extremes", action="store_true")
    ap.add_argument(
        "--db", type=Path, default=None,
        help=f"Output DB path (default {DB_PATH}).",
    )
    args = ap.parse_args()

    max_audits = args.max_audits if args.max_audits is not None else FULL_MAX_AUDITS
    db_path = output_path(
        DB_PATH, args.db,
        f"--max-audits {args.max_audits} pulls a subset, not the full corpus"
        if args.max_audits is not None else None,
    )

    criteria = parse_profile(args.profile)
    themes = criteria.get("oag_themes") or {}
    it_ex = themes.get("it_audit") or DEFAULT_IT_AUDIT
    nonit_ex = themes.get("non_it_audit") or DEFAULT_NON_IT_AUDIT
    print(f"IT-audit theme: {len(it_ex)} examples | non-IT: {len(nonit_ex)} examples")

    print(f"Fetching OAG datasets from CKAN API (org={ORG})...")
    records = fetch_oag(max_audits)
    print(f"Fetched {len(records)} datasets")
    if not records:
        sys.stderr.write(f"CKAN returned no datasets for organization:{ORG}.\n")
        sys.exit(2)
    cols = resolve_columns(
        list(records[0].keys()), OAG_PACKAGE_FIELDS,
        list(OAG_PACKAGE_FIELDS), "scripts/oag_ingest.py",
    )
    res_cols = resolve_resource_fields(records, cols)

    # Score title + description (two-pole: IT audit minus non-IT audit)
    from sentence_transformers import SentenceTransformer
    print(f"Loading embedding model ({EMBED_MODEL})...")
    model = SentenceTransformer(EMBED_MODEL)
    it_vec = theme_vector(model, it_ex)
    nonit_vec = theme_vector(model, nonit_ex)

    texts = [
        _strip_md(f"{p.get(cols['title'],'') or ''}. {p.get(cols['notes'],'') or ''}")
        for p in records
    ]
    print(f"Embedding {len(texts)} audit title+descriptions...")
    embs = model.encode(texts, normalize_embeddings=True, show_progress_bar=False,
                        batch_size=64)
    scores = [round(float(e @ it_vec - e @ nonit_vec), 4) for e in embs]

    resolver = OrgResolver()
    clash = resolver.ambiguous_phrases()
    if clash:
        sys.stderr.write(
            f"Two organizations claim the same name in {resolver.__class__.__name__}'s "
            f"registry: {clash}\nFix vault/crosswalk/org_aliases.yaml; this cannot be "
            "resolved by guessing.\n")
        sys.exit(2)

    stats = build_db(records, scores, API, cols, res_cols, resolver, db_path)
    print(f"\nWrote {stats['rows']} records to {db_path} "
          f"({db_path.stat().st_size/1e6:.1f} MB)")
    report_attribution(stats)
    if args.show_extremes:
        show_extremes(db_path=db_path)
    print("\nAttribution: contains information licensed under the "
          "Open Government Licence - Canada.")


def report_attribution(stats: dict) -> None:
    """
    Coverage, BROKEN OUT BY POPULATION and never as one blended number.

    A federal audit that names its own departments and a briefing package that
    inherits them from a report are not the same evidence at the same strength,
    and averaging them produces a figure that flatters the weaker half. The
    headline for a dossier is the federal-audit line; the corpus total is
    context, because most of this corpus should carry no department at all.
    """
    classes, resolved = stats["classes"], stats["resolved"]

    def line(label: str, cls: str, basis: str) -> None:
        total = classes[cls]
        if not total:
            return
        got = resolved[cls]
        print(f"  {label:<24} {got:>4} / {total:<4} ({100*got/total:3.0f}%)  {basis}")

    print("\n=== DEPARTMENT ATTRIBUTION ===")
    line("Federal audits", CLASS_FEDERAL_AUDIT, "direct — the headline number")
    line("Committee briefings", CLASS_BRIEFING, "inherited from parent report")
    print(f"  {'(audit, department) edges':<24} {stats['edges']:>4}")

    refused = {CLASS_OAG_ADMIN: "OAG's own admin documents",
               CLASS_CROWN_OTHER: "Crown corp / other entity",
               CLASS_TERRITORIAL: "territorial audits"}
    total_refused = sum(classes[c] for c in refused)
    print(f"\n  Correctly carrying no federal department: {total_refused}")
    for cls, label in refused.items():
        if classes[cls]:
            print(f"    {classes[cls]:>4}  {label}")
    print("  These are not a coverage gap — they audit nobody, or nobody federal.")


if __name__ == "__main__":
    main()
