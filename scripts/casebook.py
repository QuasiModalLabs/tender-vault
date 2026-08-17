"""
Analyst-blind case construction for the evidence-condition experiment.

THE QUESTION. Given only what was public at time T0, does adding contracts,
departmental plans and audits improve an evaluator's decision to investigate a
department x capability as a future procurement? Not "does the graph predict" —
the department-year backtest already answered that with a null (0 of 15
feature-by-stratum comparisons cleared their stratum base rate) and said why:
the target was "does this department tender anything relevant this year", at a
68.6% base rate and 94% in the largest third. There was no room to be right.

WHAT THIS MODULE IS NOT. It computes no score, fits no weight, ranks nothing and
writes no graph edge. The evaluator emits a decision and a stated confidence;
confidence is an OUTPUT under test, never an input to anything here. Nothing in
tender_tools.py or mcp_server.py imports this module and no MCP tool exposes it.

THE GATE IS THE WHOLE PRODUCT. Everything else is bookkeeping around one
promise: no row reaches a bundle whose public availability at T0 cannot be
defended from a date the government itself published. Where that date does not
exist, the row is EXCLUDED rather than estimated. The three ways it goes wrong:

  1. A COLUMN THAT MEANS "NOW". notices.status is Expired or Cancelled on all
     30,527 rows and Open on none, because it is state at ingest. Rendered at a
     2023 T0 it is pure 2026 information. first_seen is the same failure with a
     friendlier name: every row carries the 2026-08-15 backfill stamp, so it
     records when this project ran, not when the public could see the row.
     Both are suppressed, and SUPPRESSED_COLUMNS is asserted against the
     rendered bundles rather than trusted.

  2. AN EFFECTIVE DATE WORN AS A PUBLICATION DATE. contract_date is when a
     contract was signed. Proactive disclosure publishes quarterly, at least 30
     days after quarter end, and the gap is 70 days at the median and 1,020 at
     the 95th percentile. backtest.py's Evidence.contracts() bounds on
     contract_date and therefore shows contracts months before they existed
     publicly. This module bounds on the disclosure quarter instead; see
     disclosure_date().

  3. TWO PUBLICATIONS SHARING A ROW. plans.programs holds Departmental Plan
     fields and Departmental Results Report fields side by side, and they are
     tabled about twenty months apart. The counts prove it — intent_score is
     non-null exactly where planning_explanation is, pressure_score exactly
     where variance_explanation is, in all six years. backtest.py admits the
     whole row at `year <= fiscal_year_of(T)`, which is right for the DP half
     and 8-20 months early for the DRR half. Here the halves are gated
     separately and a row can arrive half-visible.

MISSING IS NOT NEGATIVE. A cell with no plan row renders "NONE — no rows were
published by T0". It never renders "the department has no plan for this", which
is a different and unsupported claim. The same holds for audits, and for the
corpus as a whole: absence from CanadaBuys is not evidence a procurement did not
happen. Sparse C and D are a FINDING about those sources, and this module is
forbidden from fixing sparsity by inventing inferred relations to fill it.

EXPIRY IS A REJECTED HYPOTHESIS. Contract end dates appear in bundles as
descriptive fact with their dates attached. They are not a signal. The
incumbent-expiry-to-recompete hypothesis tested at 1.00-1.05x against a
department x capability permutation null across four windows, p 0.07-0.50, and
nothing here is allowed to quietly promote it back.
"""
from __future__ import annotations

import argparse
import csv
import datetime as _dt
import hashlib
import json
import random
import re
import sqlite3
import sys
from bisect import bisect_left, bisect_right
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

PROJECT_ROOT = Path(__file__).resolve().parent.parent
NOTICES_DB = PROJECT_ROOT / "data" / "notices.db"
CONTRACTS_DB = PROJECT_ROOT / "data" / "contracts_full.db"
PLANS_DB = PROJECT_ROOT / "data" / "plans.db"
OAG_DB = PROJECT_ROOT / "data" / "oag.db"
CROSSWALK_DB = PROJECT_ROOT / "data" / "crosswalk.db"
CASEBOOK_DB = PROJECT_ROOT / "data" / "casebook.db"

CAPABILITIES_YAML = PROJECT_ROOT / "vault" / "reference" / "capabilities.yaml"
CASES_DIR = PROJECT_ROOT / "data" / "cases"

# The corpus opens 2022-04-01. Every T0 needs a full year of history behind it
# and a full outcome window ahead of it inside the corpus, which leaves exactly
# this range. Widening either end would be borrowing from a year that is not
# there; the notices table simply stops.
CORPUS_START = "2022-04-01"
FIRST_T0 = "2023-04-01"
LAST_T0 = "2025-08-01"
OUTCOME_MONTHS = 12
HISTORY_MONTHS = 12

# Capability selection reads ONLY notices published before this date, which is
# the first T0. No case's outcome window can therefore influence which
# capabilities exist to be cased. See select_capabilities().
SELECTION_CUTOFF = FIRST_T0

# Disclosure deadline. TB policy requires proactive disclosure within 30 days of
# quarter end; publication is often later. Using the deadline is the EARLIEST
# defensible availability, which is the aggressive end of the honest range, so
# DISCLOSURE_LAG_DAYS is a knob the sensitivity check moves rather than a
# constant anyone should read as exact.
DISCLOSURE_LAG_DAYS = 30

# Never rendered into a bundle, and asserted absent rather than merely omitted.
# See the module docstring, failure 1.
SUPPRESSED_COLUMNS = frozenset({
    "status", "first_seen", "intent_score", "pressure_score", "it_score",
    "vendor_focus", "observed_names",
})

CONDITIONS = ("Z", "A", "B", "C", "D")   # Z is the zero-evidence arm
CONDITION_LABELS = {
    "Z": "no supplied evidence",
    "A": "procurement",
    "B": "procurement + contracts",
    "C": "procurement + contracts + plans",
    "D": "procurement + contracts + plans + audits",
}

SOURCE_URLS = {
    "notices": "https://canadabuys.canada.ca/opendata/pub/{fy}-TenderNotice-AvisAppelOffres.csv",
    "awards": "https://canadabuys.canada.ca/opendata/pub/{fy}-awardNotice-avisAttribution.csv",
    "contracts": ("https://open.canada.ca/data/dataset/d8f85d91-7dec-4fd1-8055-483b77225d8b/"
                  "resource/fac950c0-00d5-4ec1-a4d3-9cbebf98a305/download/contracts.csv"),
    "plans": ("https://open.canada.ca/data/en/datastore/dump/"
              "64774bc1-c90a-4ae2-a3ac-d9b50673a895?bom=True"),
}


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------

def fiscal_year_of(iso_date: str) -> int:
    """Fiscal year START year. 2023-04-01 -> 2023; 2023-03-31 -> 2022."""
    year, month = int(iso_date[:4]), int(iso_date[5:7])
    return year if month >= 4 else year - 1


def shift_months(iso_date: str, months: int) -> str:
    """Calendar-month shift, clamped to the target month's last day."""
    y, m, d = int(iso_date[:4]), int(iso_date[5:7]), int(iso_date[8:10])
    total = (y * 12 + (m - 1)) + months
    ny, nm = total // 12, total % 12 + 1
    last = [31, 29 if (ny % 4 == 0 and (ny % 100 != 0 or ny % 400 == 0)) else 28,
            31, 30, 31, 30, 31, 31, 30, 31, 30, 31][nm - 1]
    return f"{ny:04d}-{nm:02d}-{min(d, last):02d}"


def add_days(iso_date: str, days: int) -> str:
    return (_dt.date.fromisoformat(iso_date) + _dt.timedelta(days=days)).isoformat()


def t0_grid(first: str = FIRST_T0, last: str = LAST_T0) -> list[str]:
    """Monthly evaluation dates, inclusive of both ends."""
    out, cur = [], first
    while cur <= last:
        out.append(cur)
        cur = shift_months(cur, 1)
    return out


# ---------------------------------------------------------------------------
# Contract disclosure dates
# ---------------------------------------------------------------------------
# Two independent statements of the disclosure quarter exist and neither covers
# the file alone. Measured over all 1,313,348 raw rows:
#
#   reporting_period parses          79.1%   (blank on 20.9%)
#   reference_number carries it      98.6%
#   union                            99.3%
#   agreement where both present     99.3%
#
# Where they disagree we take the LATER quarter. The asymmetry is deliberate: a
# quarter too late withholds evidence the evaluator could legitimately have had,
# which costs recall; a quarter too early shows evidence that did not exist,
# which invalidates the run. Rows with neither are dropped and counted, never
# dated by inference from contract_date.

_RP_RE = re.compile(r"^(\d{4})-(\d{4})-Q([1-4])$")
_REF_RE = re.compile(r"^[A-Za-z]*-?(\d{4})-(\d{4})-Q([1-4])\b")

# Quarter end by fiscal quarter: (month, day, whether it lands in fy+1).
_QUARTER_END = {1: (6, 30, 0), 2: (9, 30, 0), 3: (12, 31, 0), 4: (3, 31, 1)}


def _quarter(text: str, pattern: re.Pattern) -> tuple[int, int] | None:
    """(fiscal year start, quarter) from a disclosure string, or None."""
    m = pattern.match((text or "").strip())
    if not m:
        return None
    fy1, fy2, q = int(m.group(1)), int(m.group(2)), int(m.group(3))
    # A fiscal year label must name consecutive years. "2010-11-Q4" and
    # "2018/2019/Q3" are real values in this column; they fail here and fall
    # through to the other source rather than being coerced into a guess.
    if fy2 != fy1 + 1:
        return None
    return fy1, q


def disclosure_date(reporting_period: str, reference: str) -> str | None:
    """
    When a contract row became public, or None if that cannot be established.

    quarter end + DISCLOSURE_LAG_DAYS, taking the later of the two quarters when
    both parse. None is a refusal, not a default — callers exclude the row.
    """
    quarters = [q for q in (_quarter(reporting_period, _RP_RE),
                            _quarter(reference, _REF_RE)) if q]
    if not quarters:
        return None
    fy, q = max(quarters)
    month, day, carry = _QUARTER_END[q]
    return add_days(f"{fy + carry:04d}-{month:02d}-{day:02d}", DISCLOSURE_LAG_DAYS)


# ---------------------------------------------------------------------------
# Plan field availability
# ---------------------------------------------------------------------------
# One programs row, two publications, twenty months apart.
#
#   DP  fields  planned_spending, planned_spending_next, planned_spending_next2,
#               planned_ftes, planning_explanation
#               A Departmental Plan for fiscal year Y is tabled around March of
#               Y, before the year opens.
#   DRR fields  actual_spending, actual_ftes, variance_explanation
#               The Departmental Results Report for fiscal year Y is tabled
#               around November-December of Y+1, after the year has closed.
#
# The proof that the split is real and not a guess about the upstream file: for
# year 2025 the dump carries 1,146 planned_spending values and ZERO
# actual_spending values, because FY2025-26's DRR has not been tabled.

DP_FIELDS = ("planned_spending", "planned_spending_next", "planned_spending_next2",
             "planned_ftes", "planning_explanation")
DRR_FIELDS = ("actual_spending", "actual_ftes", "variance_explanation")


def dp_available(year: int) -> str:
    return f"{year:04d}-03-01"


def drr_available(year: int) -> str:
    return f"{year + 1:04d}-12-01"


# ---------------------------------------------------------------------------
# Capability registry
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Capability:
    """
    A UNSPSC segment, matched against each source in that source's own terms.

    THE GSIN BRIDGE DOES NOT EXIST, AND THAT IS A FINDING. Notices are
    classified by the publisher in UNSPSC; contracts carry commodity_code, which
    is 73.5% GSIN-form and only 8.4% UNSPSC. Two bridges were considered and
    both are refused:

      - PSPC's own GSIN-UNSPSC crosswalk. unspsc_discover.py documents why it is
        unusable: the linkages were assessed at higher levels and carried
        through indiscriminately, so telecom cable laying and highway paving
        share a GSIN. Rolling a tender up through it would put paving in the
        cloud results.

      - Co-occurrence inside one notice, where the publisher filed both codes on
        the same record. MEASURED: of 7,152 notices published before the
        selection cutoff, the number carrying both a UNSPSC code and a GSIN code
        is ZERO. The two systems are mutually exclusive in this corpus. There is
        no evidence from which to derive the mapping.

    So contracts are capability-matched ONLY through the 8-digit UNSPSC minority
    of commodity_code, and the GSIN-coded majority is presented at department
    level, flagged as unmatched-to-capability. That leaves condition B thinner
    than it looks, which is the honest state of the data rather than a gap to be
    filled by inventing a mapping.

    Segment (2-digit) rather than family (4-digit) grain: at family grain only 5
    candidates cleared the coverage floors in the pre-cutoff window, across 3
    segments, which cannot produce 6 capabilities spanning the corpus.
    """
    id: str
    label: str
    unspsc_families: tuple[str, ...]
    segment: str


def _yaml_dump(caps: list[Capability], meta: dict) -> str:
    lines = ["# Capability registry for the analyst-blind evidence experiment.",
             "#",
             "# FROZEN. Selected by a deterministic rule (casebook.py",
             "# select_capabilities) reading only notices published before",
             f"# {SELECTION_CUTOFF}, the first evaluation date, so no case's outcome",
             "# window could influence which capabilities exist. Editing this file",
             "# changes matcher_hash and invalidates every case built against it.",
             "#",
             "# Each source keeps its own vocabulary and they are allowed to",
             "# disagree, the same way the profile keeps unspsc_families and",
             "# contracts_categories apart.",
             ""]
    lines.append(f"frame_version: {meta['frame_version']}")
    lines.append(f"selection_cutoff: '{meta['selection_cutoff']}'")
    lines.append(f"generated: '{meta['generated']}'")
    lines.append("capabilities:")
    for c in caps:
        lines.append(f"  - id: {c.id}")
        lines.append(f"    label: \"{c.label}\"")
        lines.append(f"    segment: '{c.segment}'")
        lines.append("    notices:            # observed - the publisher's own classification")
        lines.append("      unspsc_families: [" +
                     ", ".join(f"'{f}'" for f in c.unspsc_families) + "]")
        lines.append("    contracts:          # observed, but only the 8-digit UNSPSC minority")
        lines.append("      unspsc_families: [" +
                     ", ".join(f"'{f}'" for f in c.unspsc_families) + "]")
        lines.append("      gsin_bridge: none   # measured: 0 notices carry both code systems")
        lines.append("")
    return "\n".join(lines) + "\n"


def load_capabilities(path: Path = CAPABILITIES_YAML) -> tuple[list[Capability], str]:
    """
    Parse the frozen registry and return it with the hash of the file bytes.

    Hand-rolled rather than pulled through a YAML library because the file is
    written by _yaml_dump in one shape and nothing else may widen it: a registry
    that accepts arbitrary YAML is a registry someone can extend with a matcher
    the tests never see.
    """
    text = path.read_text(encoding="utf-8")
    matcher_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    caps: list[Capability] = []
    cur: dict = {}
    section = None
    for raw in text.splitlines():
        line = raw.split("#")[0].rstrip()
        if not line.strip():
            continue
        stripped = line.strip()
        if stripped.startswith("- id:"):
            if cur:
                caps.append(_capability_from(cur))
            cur = {"id": stripped.split(":", 1)[1].strip()}
            section = None
        elif stripped.startswith("label:"):
            cur["label"] = stripped.split(":", 1)[1].strip().strip('"')
        elif stripped.startswith("segment:"):
            cur["segment"] = stripped.split(":", 1)[1].strip().strip("'")
        elif stripped in ("notices:", "contracts:"):
            section = stripped[:-1]
        elif stripped.startswith("unspsc_families:") and section == "notices":
            cur["unspsc_families"] = _list_of(stripped)
    if cur:
        caps.append(_capability_from(cur))
    if not caps:
        raise ValueError(f"No capabilities parsed from {path}")
    return caps, matcher_hash


def _list_of(line: str) -> tuple[str, ...]:
    inner = line.split("[", 1)[1].rsplit("]", 1)[0]
    return tuple(p.strip().strip("'\"") for p in inner.split(",") if p.strip())


def _capability_from(d: dict) -> Capability:
    return Capability(
        id=d["id"], label=d.get("label", d["id"]),
        unspsc_families=d.get("unspsc_families", ()),
        segment=d.get("segment", ""),
    )


# ---------------------------------------------------------------------------
# Source readers
# ---------------------------------------------------------------------------

def _rows(db: Path, sql: str, params: tuple = ()) -> list[dict]:
    if not db.exists():
        raise FileNotFoundError(
            f"{db} is missing. The experiment cannot distinguish an empty source "
            f"from an absent one, so it stops rather than reporting NONE.")
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, params)]
    finally:
        conn.close()


def parse_unspsc(raw) -> set[str]:
    """8-digit codes from the multi-valued, '*'-prefixed, newline-joined field."""
    if not raw:
        return set()
    out = set()
    for part in str(raw).split("\n"):
        code = part.strip().lstrip("*").strip()
        if len(code) == 8 and code.isdigit():
            out.add(code)
    return out


def parse_gsin(raw) -> set[str]:
    if not raw:
        return set()
    return {p.strip().lstrip("*").strip().upper()
            for p in str(raw).split("\n") if p.strip().lstrip("*").strip()}


def split_org_keys(raw) -> list[str]:
    """
    The canonical keys a notice names. Multi-valued and stays that way.

    A joint notice genuinely names several departments and assigning it to one
    of them would be inventing a fact. Each key makes its own case; the shared
    event_id is what stops that inflating the sample.
    """
    if not raw:
        return []
    return [k.strip() for k in str(raw).replace("\n", ",").split(",") if k.strip()]


def event_id_of(row: dict) -> str:
    """
    The underlying procurement, not the row.

    One requirement reaching the market is one event however many amendments it
    accumulates and however many departments it names. solicitation_number is
    that identity; three rows carry the placeholder "-" and fall back to their
    reference number.
    """
    soln = (row.get("solicitation_number") or "").strip()
    if soln and soln != "-":
        return soln
    return (row.get("reference_number") or "").strip()


# ---------------------------------------------------------------------------
# The as-of gate
# ---------------------------------------------------------------------------

@dataclass
class AvailabilityGate:
    """
    Everything defensibly public on `date`, and nothing else.

    Each accessor applies its own bound in SQL rather than trusting a caller to
    filter, because a filter a caller can forget is not a gate. No accessor
    returns a connection, so no feature code can widen its own query. Results
    memoize per instance, and the instance is per-date, so a cached row cannot
    serve a different T0.
    """
    date: str
    _cache: dict = field(default_factory=dict, repr=False)

    def _memo(self, key: str, fn):
        if key not in self._cache:
            self._cache[key] = fn()
        return self._cache[key]

    def notices(self) -> list[dict]:
        """Tender notices published at or before T0. status is not selected."""
        return self._memo("notices", lambda: _rows(
            NOTICES_DB,
            """SELECT reference_number, amendment_number, solicitation_number,
                      publication_date, fiscal_year, title, description,
                      closing_date, contracting_entity, end_user, notice_type,
                      procurement_category, unspsc, gsin, opportunity_kind,
                      kind_basis, org_keys, org_key_source
                 FROM notices
                WHERE publication_date != '' AND publication_date <= ?""",
            (self.date,)))

    def awards(self) -> list[dict]:
        """Award notices published at or before T0. status is not selected."""
        return self._memo("awards", lambda: _rows(
            NOTICES_DB,
            """SELECT reference_number, solicitation_number, publication_date,
                      title, description, contracting_entity, end_user, vendor,
                      award_date, contract_start_date, contract_end_date,
                      contract_value, unspsc, gsin, procurement_method,
                      org_keys, fiscal_year
                 FROM awards
                WHERE publication_date != '' AND publication_date <= ?""",
            (self.date,)))

    def contracts(self) -> list[dict]:
        """
        Contract rows DISCLOSED at or before T0.

        Reads the derived disclosure_date built by build_casebook_db, never
        contract_date. Rows whose disclosure quarter could not be established
        are absent from that table entirely and so cannot appear here.
        """
        return self._memo("contracts", lambda: _rows(
            CASEBOOK_DB,
            """SELECT family_id, reference, vendor_norm, org, owner_org,
                      description, contract_date, period_start, period_end,
                      value, commodity_code, solicitation_procedure,
                      number_of_bids, reporting_period, disclosure_date, dept_key
                 FROM contracts_disclosed
                WHERE disclosure_date <= ?""",
            (self.date,)))

    def plan_rows(self) -> list[dict]:
        """
        Program rows with each half gated on its own tabling date.

        A row can come back half-visible: DP fields present, DRR fields None,
        because the plan was tabled and the results report was not. That is the
        actual state of public knowledge at T0 and collapsing it to a whole-row
        decision is what leaks.
        """
        def _load():
            out = []
            for r in _rows(PLANS_DB,
                           """SELECT year, organization_id, organization,
                                     core_responsibility, program_id, program_name,
                                     planned_spending, planned_spending_next,
                                     planned_spending_next2, planned_ftes,
                                     planning_explanation, actual_spending,
                                     actual_ftes, variance_explanation
                                FROM programs"""):
                dp_ok = dp_available(r["year"]) <= self.date
                drr_ok = drr_available(r["year"]) <= self.date
                if not dp_ok and not drr_ok:
                    continue
                row = dict(r)
                row["_dp_visible"], row["_drr_visible"] = dp_ok, drr_ok
                if not dp_ok:
                    for f in DP_FIELDS:
                        row[f] = None
                if not drr_ok:
                    for f in DRR_FIELDS:
                        row[f] = None
                out.append(row)
            return out
        return self._memo("plans", _load)

    def audits(self) -> list[dict]:
        """
        Audits whose portal publication date is at or before T0.

        The portal date runs LATE relative to tabling, which is the safe
        direction: a late date withholds a signal, it never leaks one. Rows with
        a blank date are excluded rather than guessed; there are currently none.
        it_score and vendor_focus are not selected.
        """
        return self._memo("audits", lambda: _rows(
            OAG_DB,
            """SELECT a.oag_id, a.year, a.date_published, a.doc_type,
                      a.record_class, a.attribution_status, a.title,
                      a.description, a.html_url, a.pdf_url,
                      ad.dept_key, ad.method, ad.evidence, ad.n_parent_reports
                 FROM audits a JOIN audit_departments ad USING (oag_id)
                WHERE a.date_published != '' AND a.date_published <= ?""",
            (self.date,)))


def as_of(date: str) -> AvailabilityGate:
    if not (len(date) == 10 and date[4] == "-" and date[7] == "-"):
        raise ValueError(f"as_of needs an ISO date, got {date!r}")
    return AvailabilityGate(date=date)


# ---------------------------------------------------------------------------
# Derived contract table
# ---------------------------------------------------------------------------

def build_casebook_db(verbose: bool = True) -> dict:
    """
    Materialize contracts with a disclosure date and a canonical department key.

    Done once, into its own database, for two reasons. contracts_full.db stays
    exactly as the ingest wrote it, so the derivation is inspectable against its
    source rather than baked into it. And the exclusion is recorded as a count
    in meta rather than happening silently inside a query no one reads.
    """
    xwalk = {r["contract_owner_org"]: r["canonical_key"]
             for r in _rows(CROSSWALK_DB,
                            "SELECT canonical_key, contract_owner_org FROM org_crosswalk "
                            "WHERE contract_owner_org IS NOT NULL")}
    src = _rows(CONTRACTS_DB,
                """SELECT family_id, reference, vendor, vendor_norm, org, owner_org,
                          description, contract_date, period_start, period_end, value,
                          commodity_code, solicitation_procedure, number_of_bids,
                          reporting_period
                     FROM contracts""")
    kept, no_date, no_dept = [], 0, 0
    for r in src:
        dd = disclosure_date(r.get("reporting_period") or "", r.get("reference") or "")
        if dd is None:
            no_date += 1
            continue
        dept = xwalk.get((r.get("owner_org") or "").strip())
        if not dept:
            no_dept += 1
            continue
        kept.append((r["family_id"], r["reference"], r["vendor_norm"], r["org"],
                     r["owner_org"], (r["description"] or "")[:600], r["contract_date"],
                     r["period_start"], r["period_end"], r["value"], r["commodity_code"],
                     r["solicitation_procedure"], r["number_of_bids"],
                     r["reporting_period"], dd, dept))

    CASEBOOK_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(CASEBOOK_DB)
    try:
        conn.execute("DROP TABLE IF EXISTS contracts_disclosed")
        conn.execute("""
            CREATE TABLE contracts_disclosed (
                family_id TEXT, reference TEXT, vendor_norm TEXT, org TEXT,
                owner_org TEXT, description TEXT, contract_date TEXT,
                period_start TEXT, period_end TEXT, value REAL,
                commodity_code TEXT, solicitation_procedure TEXT,
                number_of_bids INTEGER, reporting_period TEXT,
                -- quarter end + 30d, from the LATER of reporting_period and the
                -- quarter in reference. Never from contract_date.
                disclosure_date TEXT,
                dept_key TEXT
            )""")
        conn.executemany("INSERT INTO contracts_disclosed VALUES "
                         "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", kept)
        conn.execute("CREATE INDEX idx_cd_disc ON contracts_disclosed(disclosure_date)")
        conn.execute("CREATE INDEX idx_cd_dept ON contracts_disclosed(dept_key)")
        conn.execute("DROP TABLE IF EXISTS meta")
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        stats = {
            "built_at": _dt.datetime.now().isoformat(timespec="seconds"),
            "source_rows": str(len(src)),
            "kept": str(len(kept)),
            "excluded_no_disclosure_quarter": str(no_date),
            "excluded_no_department_match": str(no_dept),
            "disclosure_lag_days": str(DISCLOSURE_LAG_DAYS),
        }
        conn.executemany("INSERT INTO meta VALUES (?,?)", list(stats.items()))
        conn.commit()
    finally:
        conn.close()
    if verbose:
        print(f"contracts_disclosed: kept {len(kept):,} of {len(src):,}")
        print(f"  excluded, no disclosure quarter : {no_date:,}")
        print(f"  excluded, no department match   : {no_dept:,}")
    return stats


# ---------------------------------------------------------------------------
# Capability selection
# ---------------------------------------------------------------------------

# Coverage floors. SET BY INSPECTING PRE-CUTOFF COVERAGE, NEVER OUTCOMES — no
# label had been computed when these moved, and the frame did not exist yet.
# Recorded because they moved rather than pretending they were right first time:
#
#   first attempt   4-digit family grain, >=100 notices, >=5 departments,
#                   >=100 contract rows.  Result: 5 candidates across 3
#                   segments, which cannot yield 6 capabilities spanning the
#                   corpus. The pre-cutoff window is one year and 7,152
#                   notices, so a 4-digit family is simply too narrow a bin.
#   now             2-digit segment grain and the floors below.
#
# Nothing here consults whether a capability later procured. The rule is a
# COVERAGE rule; a capability chosen because it looked predictive would be the
# selection bias this whole design exists to avoid.
MIN_SELECT_NOTICES = 50
MIN_SELECT_DEPTS = 10
MIN_SELECT_CONTRACTS = 100
N_CAPABILITIES = 6


def select_capabilities(n: int = N_CAPABILITIES, verbose: bool = True) -> list[Capability]:
    """
    Choose the capability set by rule, from pre-cutoff data only.

    Every notice and contract row it reads was public before the first
    evaluation date, so no case's outcome window can reach back and influence
    which capabilities exist.

      1. candidates are UNSPSC 2-digit segments in the pre-cutoff window
      2. keep segments with >= MIN_SELECT_NOTICES notices and
         >= MIN_SELECT_DEPTS distinct departments
      3. keep segments with >= MIN_SELECT_CONTRACTS contract rows disclosed by
         the cutoff and carrying an 8-digit UNSPSC commodity code, so condition
         B is not vacuous by construction
      4. the top n by department count, ties broken by notice count then by the
         segment code, so the choice is total and reproducible

    Plan and audit coverage is REPORTED, not gated on. Filtering to capabilities
    that happen to have plan prose would manufacture the C coverage this
    experiment exists to measure.
    """
    gate = as_of(SELECTION_CUTOFF)
    seg_notices: dict[str, int] = defaultdict(int)
    seg_depts: dict[str, set[str]] = defaultdict(set)
    both_codes = 0

    for row in gate.notices():
        codes = parse_unspsc(row["unspsc"])
        segs = {c[:2] for c in codes}
        gsins = parse_gsin(row["gsin"])
        if codes and gsins:
            both_codes += 1
        depts = split_org_keys(row["org_keys"])
        for sg in segs:
            seg_notices[sg] += 1
            seg_depts[sg].update(depts)

    seg_contracts: dict[str, int] = defaultdict(int)
    seg_con_depts: dict[str, set[str]] = defaultdict(set)
    for c in gate.contracts():
        code = (c["commodity_code"] or "").strip().upper()
        if len(code) == 8 and code.isdigit():
            seg_contracts[code[:2]] += 1
            seg_con_depts[code[:2]].add(c["dept_key"])

    rows = [{"segment": sg, "notices": nn, "depts": len(seg_depts[sg]),
             "contracts": seg_contracts.get(sg, 0),
             "con_depts": len(seg_con_depts.get(sg, ()))}
            for sg, nn in seg_notices.items()
            if nn >= MIN_SELECT_NOTICES
            and len(seg_depts[sg]) >= MIN_SELECT_DEPTS
            and seg_contracts.get(sg, 0) >= MIN_SELECT_CONTRACTS]

    chosen = sorted(rows, key=lambda r: (-r["depts"], -r["notices"], r["segment"]))[:n]
    chosen.sort(key=lambda r: r["segment"])

    if verbose:
        print(f"Pre-cutoff window: notices published before {SELECTION_CUTOFF}")
        print(f"Notices carrying BOTH a UNSPSC and a GSIN code: {both_codes} "
              f"(this is why there is no GSIN bridge)")
        print(f"Segments passing all three floors: {len(rows)}; taking {len(chosen)}")
        print(f"{'seg':>4} {'notices':>8} {'depts':>6} {'contracts':>10} {'con_depts':>10}  label")
        for r in chosen:
            print(f"{r['segment']:>4} {r['notices']:>8} {r['depts']:>6} "
                  f"{r['contracts']:>10} {r['con_depts']:>10}  "
                  f"{UNSPSC_LABELS.get(r['segment'], '?')[:44]}")

    return [Capability(
        id=f"unspsc-{r['segment']}",
        label=UNSPSC_LABELS.get(r["segment"], f"UNSPSC segment {r['segment']}"),
        unspsc_families=(r["segment"],), segment=r["segment"])
        for r in chosen]


# Human-readable names for whatever the rule picks. Read from the PSPC reference
# file at write time, so the registry does not have to carry 7,748 rows.
UNSPSC_LABELS: dict[str, str] = {}


def load_unspsc_labels() -> dict[str, str]:
    path = PROJECT_ROOT / ".cache" / "unspsc_reference.csv"
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    with open(path, encoding="utf-8-sig", errors="replace", newline="") as fh:
        for row in csv.DictReader(fh):
            code = (row.get("UNSPSC-Code") or "").strip()
            desc = (row.get("UNSPSC-Description-eng") or "").strip()
            if len(code) == 8 and code.isdigit() and desc:
                # The segment-level (xx000000) row is the segment's own name;
                # anything else is only a fallback so a missing header row does
                # not leave a capability nameless.
                if code.endswith("000000"):
                    out[code[:2]] = desc
                else:
                    out.setdefault(code[:2], desc)
    return out


# ---------------------------------------------------------------------------
# The frame
# ---------------------------------------------------------------------------
# Departments whose crosswalk relation is not "same" are excluded. ircc
# (absorbed), prairiescan (successor), wage and nsira (predecessor) each name an
# organization whose identity moved during or near the corpus window, so
# "was this department procuring at T0" has no single answer for them. Four
# cells' worth of coverage is a cheap price for not having to caveat every
# count; the exclusion is recorded on the frame rather than done quietly.
EXCLUDED_RELATIONS = ("absorbed", "successor", "predecessor")


def _excluded_departments() -> set[str]:
    return {r["canonical_key"] for r in _rows(
        CROSSWALK_DB, "SELECT canonical_key, relation FROM org_crosswalk")
        if (r["relation"] or "") in EXCLUDED_RELATIONS}


def _display_names() -> dict[str, str]:
    return {r["canonical_key"]: (r["contract_org_en"] or r["plan_organization"]
                                 or r["canonical_key"])
            for r in _rows(CROSSWALK_DB,
                           "SELECT canonical_key, contract_org_en, plan_organization "
                           "FROM org_crosswalk")}


def capability_matches_notice(cap: Capability, row: dict) -> bool:
    """
    Does this notice belong to this capability?

    UNSPSC only, and deliberately so: it is the publisher's own classification
    of their own notice, which makes it observed rather than inferred. GSIN is
    filed on 20% of notices and is not consulted here — a fallback that fires
    only where the primary is missing would make the capability definition
    depend on which system published the notice.
    """
    codes = parse_unspsc(row.get("unspsc"))
    return any(c.startswith(f) for c in codes for f in cap.unspsc_families)


def capability_matches_contract(cap: Capability, row: dict) -> str | None:
    """
    'unspsc' if this contract row is capability-matched, else None.

    Only the 8-digit UNSPSC minority of commodity_code can be matched — see
    Capability for why the GSIN majority has no defensible bridge. A row that
    returns None is NOT discarded: it goes into the department-level contract
    context, labelled unmatched, so the bundle says "this department bought
    this, we cannot tell whether it is this capability" rather than pretending
    the purchase did not happen.
    """
    code = (row.get("commodity_code") or "").strip().upper()
    if len(code) == 8 and code.isdigit():
        return "unspsc" if any(code.startswith(f) for f in cap.unspsc_families) else None
    return None


@dataclass
class FrameCell:
    department: str
    capability: str
    t0: str
    eligible: bool
    n_prior_notices: int
    n_prior_awards: int
    label: str                      # POSITIVE | NEGATIVE
    outcome_events: tuple           # (event_id, pub_date, ref, kind, notice_type)
    prior_activity: int


def build_frame(caps: list[Capability], grid: list[str] | None = None,
                verbose: bool = True) -> list[FrameCell]:
    """
    Every department x capability x T0 cell, with eligibility and label.

    Built in memory from two full table scans rather than one query per cell:
    86 departments x 6 capabilities x 29 dates is 14,964 cells, and a query
    apiece would be 45,000 scans of the same 30,000 rows.
    """
    grid = grid or t0_grid()
    excluded = _excluded_departments()
    # The gate at the LAST date sees everything any T0 in the grid can see, and
    # each cell then bisects it. One load, and the per-cell bound is still a
    # date comparison rather than a promise.
    horizon = as_of(shift_months(grid[-1], OUTCOME_MONTHS))
    all_notices, all_awards = horizon.notices(), horizon.awards()

    notice_idx: dict[tuple[str, str], list[tuple]] = defaultdict(list)
    dept_activity: dict[str, list[str]] = defaultdict(list)
    for row in all_notices:
        depts = [d for d in split_org_keys(row["org_keys"]) if d not in excluded]
        pub = row["publication_date"]
        for d in depts:
            dept_activity[d].append(pub)
        matched = [c for c in caps if capability_matches_notice(c, row)]
        if not matched:
            continue
        item = (pub, event_id_of(row), row["reference_number"],
                row.get("opportunity_kind") or "", row.get("notice_type") or "")
        for d in depts:
            for c in matched:
                notice_idx[(d, c.id)].append(item)

    award_idx: dict[tuple[str, str], list[str]] = defaultdict(list)
    for row in all_awards:
        depts = [d for d in split_org_keys(row["org_keys"]) if d not in excluded]
        matched = [c for c in caps if capability_matches_notice(c, row)]
        for d in depts:
            for c in matched:
                award_idx[(d, c.id)].append(row["publication_date"])

    for key in notice_idx:
        notice_idx[key].sort()
    for key in award_idx:
        award_idx[key].sort()
    for d in dept_activity:
        dept_activity[d].sort()

    departments = sorted(dept_activity)
    cells: list[FrameCell] = []
    for t0 in grid:
        window_end = shift_months(t0, OUTCOME_MONTHS)
        for d in departments:
            acts = dept_activity[d]
            prior_activity = (bisect_left(acts, t0)
                              - bisect_left(acts, shift_months(t0, -HISTORY_MONTHS)))
            for c in caps:
                items = notice_idx.get((d, c.id), [])
                awards = award_idx.get((d, c.id), [])
                dates = [i[0] for i in items]
                n_prior = bisect_right(dates, t0)
                n_prior_aw = bisect_right(awards, t0)
                # Half-open (T0, T0+12mo]: a notice published exactly on T0 is
                # evidence the evaluator can see, never an outcome it predicts.
                lo, hi = bisect_right(dates, t0), bisect_right(dates, window_end)
                outcome = tuple(items[lo:hi])
                cells.append(FrameCell(
                    department=d, capability=c.id, t0=t0,
                    eligible=(n_prior + n_prior_aw) >= 1,
                    n_prior_notices=n_prior, n_prior_awards=n_prior_aw,
                    label="POSITIVE" if outcome else "NEGATIVE",
                    outcome_events=outcome, prior_activity=prior_activity))

    if verbose:
        elig = [c for c in cells if c.eligible]
        pos = [c for c in elig if c.label == "POSITIVE"]
        events = {e[1] for c in pos for e in c.outcome_events}
        print(f"Frame: {len(cells):,} cells, {len(elig):,} eligible "
              f"({len(departments)} departments x {len(caps)} capabilities x {len(grid)} dates)")
        print(f"  eligible positives {len(pos):,}  negatives {len(elig) - len(pos):,}  "
              f"base rate {len(pos) / max(len(elig), 1):.1%}")
        print(f"  distinct underlying events across positives: {len(events):,}")
    return cells


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------

@dataclass
class Case:
    case_id: str
    department: str
    department_display: str
    capability: str
    capability_label: str
    t0: str
    outcome_window_months: int
    outcome_window_end: str
    frame_version: int
    matcher_hash: str
    eligibility_basis: str
    history_depth_days: int
    prior_activity: int
    prior_activity_decile: int
    match_tier: str
    matched_case_id: str
    # Sealed. Written to a separate file the evaluation harness never opens.
    ground_truth: str = ""
    outcome_event_ids: tuple = ()
    outcome_kinds: tuple = ()
    eventual_tender_id: str = ""
    eventual_tender_date: str = ""
    lead_time_days: int | None = None


def _deciles(values: list[int]) -> list[int]:
    """Decile cut points, so activity matching bins rather than demands equality."""
    if not values:
        return []
    ordered = sorted(values)
    return [ordered[min(len(ordered) - 1, (len(ordered) * k) // 10)] for k in range(1, 10)]


def _decile_of(value: int, cuts: list[int]) -> int:
    return sum(1 for c in cuts if value > c)


def _quarter_of(t0: str) -> str:
    return f"{t0[:4]}Q{(int(t0[5:7]) - 1) // 3 + 1}"


def sample_pilot(cells: list[FrameCell], caps: list[Capability], matcher_hash: str,
                 n_pairs: int = 10, seed: int = 20260816,
                 dept_exact_target: int = 5, verbose: bool = True) -> list[Case]:
    """
    Draw n_pairs positives, each with one matched negative.

    MATCHING IS DELIBERATELY SPLIT between two tiers, because they trade against
    each other and running only one would hide something:

      department_exact  same department, same T0, different capability. The
                        strongest control available — department size, budget,
                        procurement culture and calendar are held literally
                        constant, so "big departments procure more" carries no
                        information at all. But plans and audits attach to a
                        DEPARTMENT, not to a department x capability, so
                        conditions C and D are IDENTICAL across such a pair.
                        They cannot discriminate within it, by construction
                        rather than by weakness of signal.

      activity_decile   a different department in the same prior-activity decile
                        at the same T0. Weaker control, but C and D differ, so
                        they can contribute at all.

    Half of each, so the pilot measures both what the strong control permits and
    what the weak one reveals. The tier is recorded per pair and every metric
    can be split on it.

    Diversity caps stop the draw collapsing onto one department or one quarter;
    a pilot that is all DND in 2024 validates nothing about the method.
    """
    rng = random.Random(seed)
    eligible = [c for c in cells if c.eligible]
    positives = [c for c in eligible if c.label == "POSITIVE"]
    negatives = [c for c in eligible if c.label == "NEGATIVE"]

    activity_by_t0: dict[str, list[int]] = defaultdict(list)
    for c in eligible:
        activity_by_t0[c.t0].append(c.prior_activity)
    cuts = {t0: _deciles(v) for t0, v in activity_by_t0.items()}

    neg_by_dept_t0: dict[tuple, list[FrameCell]] = defaultdict(list)
    neg_by_dec_t0: dict[tuple, list[FrameCell]] = defaultdict(list)
    for c in negatives:
        neg_by_dept_t0[(c.department, c.t0)].append(c)
        neg_by_dec_t0[(c.t0, _decile_of(c.prior_activity, cuts[c.t0]))].append(c)

    caps_by_id = {c.id: c for c in caps}
    display = _display_names()

    MAX_PER_DEPT, MAX_PER_CAP, MAX_PER_QUARTER = 3, 3, 4
    used_dept: dict[str, int] = defaultdict(int)
    used_cap: dict[str, int] = defaultdict(int)
    used_quarter: dict[str, int] = defaultdict(int)
    taken_neg: set = set()
    taken_pos: set = set()
    pairs: list[tuple] = []

    order = positives[:]
    rng.shuffle(order)
    for want_exact in (True, False):
        target = dept_exact_target if want_exact else n_pairs - dept_exact_target
        got = 0
        for pos in order:
            if got >= target:
                break
            key = (pos.department, pos.capability, pos.t0)
            if key in taken_pos:
                continue
            quarter = _quarter_of(pos.t0)
            if (used_dept[pos.department] >= MAX_PER_DEPT
                    or used_cap[pos.capability] >= MAX_PER_CAP
                    or used_quarter[quarter] >= MAX_PER_QUARTER):
                continue
            if want_exact:
                pool = [c for c in neg_by_dept_t0.get((pos.department, pos.t0), [])
                        if (c.department, c.capability, c.t0) not in taken_neg]
                tier = "department_exact"
            else:
                dec = _decile_of(pos.prior_activity, cuts[pos.t0])
                pool = [c for c in neg_by_dec_t0.get((pos.t0, dec), [])
                        if c.department != pos.department
                        and (c.department, c.capability, c.t0) not in taken_neg]
                tier = "activity_decile"
            if not pool:
                continue
            neg = rng.choice(pool)
            taken_neg.add((neg.department, neg.capability, neg.t0))
            taken_pos.add(key)
            used_dept[pos.department] += 1
            used_cap[pos.capability] += 1
            used_quarter[quarter] += 1
            pairs.append((pos, neg, tier))
            got += 1
        if got < target and verbose:
            tier_name = "department_exact" if want_exact else "activity_decile"
            print(f"  WARNING: wanted {target} {tier_name} pairs, got {got}")

    # Case ids are assigned AFTER shuffling the pooled cells, so no id ordering,
    # parity or block structure tracks the label. An evaluator that noticed
    # "odd ids are positive" would be reading the sampler, not the evidence.
    flat: list[tuple] = []
    for idx, (pos, neg, tier) in enumerate(pairs):
        flat.append((pos, tier, f"pair{idx:02d}"))
        flat.append((neg, tier, f"pair{idx:02d}"))
    rng.shuffle(flat)

    by_pair: dict[str, list[str]] = defaultdict(list)
    cases: list[Case] = []
    for i, (cell, tier, pair_key) in enumerate(flat, start=1):
        cid = f"CASE-{i:03d}"
        by_pair[pair_key].append(cid)
        events = sorted(cell.outcome_events)
        cap = caps_by_id[cell.capability]
        lead = None
        if events:
            lead = (_dt.date.fromisoformat(events[0][0])
                    - _dt.date.fromisoformat(cell.t0)).days
        cases.append(Case(
            case_id=cid, department=cell.department,
            department_display=display.get(cell.department, cell.department),
            capability=cell.capability, capability_label=cap.label, t0=cell.t0,
            outcome_window_months=OUTCOME_MONTHS,
            outcome_window_end=shift_months(cell.t0, OUTCOME_MONTHS),
            frame_version=1, matcher_hash=matcher_hash,
            eligibility_basis=(f"{cell.n_prior_notices} tender notice(s) and "
                               f"{cell.n_prior_awards} award notice(s) matching this "
                               f"capability published on or before T0"),
            history_depth_days=(_dt.date.fromisoformat(cell.t0)
                                - _dt.date.fromisoformat(CORPUS_START)).days,
            prior_activity=cell.prior_activity,
            prior_activity_decile=_decile_of(cell.prior_activity, cuts[cell.t0]),
            match_tier=tier, matched_case_id="",
            ground_truth=cell.label,
            outcome_event_ids=tuple(sorted({e[1] for e in events})),
            outcome_kinds=tuple(sorted({e[3] for e in events if e[3]})),
            eventual_tender_id=events[0][2] if events else "",
            eventual_tender_date=events[0][0] if events else "",
            lead_time_days=lead,
        ))
    index = {c.case_id: c for c in cases}
    for ids in by_pair.values():
        if len(ids) == 2:
            index[ids[0]].matched_case_id = ids[1]
            index[ids[1]].matched_case_id = ids[0]

    if verbose:
        pos_n = sum(1 for c in cases if c.ground_truth == "POSITIVE")
        events = {e for c in cases for e in c.outcome_event_ids}
        quarters = {_quarter_of(c.t0) for c in cases}
        print(f"Pilot: {len(cases)} cases ({pos_n} positive, {len(cases) - pos_n} negative)")
        print(f"  distinct procurement events behind the positives: {len(events)}")
        print(f"  departments {len({c.department for c in cases})}, "
              f"capabilities {len({c.capability for c in cases})}, "
              f"T0 quarters {len(quarters)}")
        for tier in ("department_exact", "activity_decile"):
            n = sum(1 for c in cases if c.match_tier == tier) // 2
            print(f"  {tier}: {n} pairs")
    return cases


# ---------------------------------------------------------------------------
# Evidence bundles
# ---------------------------------------------------------------------------
# One evidence_id per underlying row, INDEPENDENT of condition, so
# A subset B subset C subset D can be asserted by set comparison rather than
# trusted. If an id appeared in A and not in B the nesting is broken and the
# whole incremental comparison is meaningless.

MAX_NOTICES, MAX_AWARDS, MAX_CONTRACTS, MAX_PLANS, MAX_AUDITS = 20, 15, 20, 15, 12


def evidence_id(source: str, row_key: str) -> str:
    return f"{source[:3]}-{hashlib.sha256(f'{source}|{row_key}'.encode()).hexdigest()[:12]}"


def _rec(source: str, row_key: str, published_at: str, relation: str,
         availability_rule: str, url: str, **extra) -> dict:
    rec = {
        "evidence_id": evidence_id(source, row_key),
        "source": source,
        "source_row_key": row_key,
        "source_url": url,
        "published_at": published_at,
        "availability_basis": published_at,
        "availability_rule": availability_rule,
        "relation_class": relation,
    }
    rec.update(extra)
    return rec


def collect_evidence(case: Case, cap: Capability, gate: AvailabilityGate) -> dict:
    """
    Everything visible at this case's T0, bucketed by the condition that adds it.

    Returns {"A": [...], "B": [...], "C": [...], "D": [...]} where each list
    holds only what that condition ADDS. The rendered bundle for condition X is
    the union of A..X, which is what makes the nesting structural.
    """
    dept = case.department
    out = {"A": [], "B": [], "C": [], "D": []}

    for row in gate.notices():
        if dept not in split_org_keys(row["org_keys"]):
            continue
        if not capability_matches_notice(cap, row):
            continue
        out["A"].append(_rec(
            "notices", row["reference_number"], row["publication_date"], "observed",
            "notices.publication_date <= T0",
            SOURCE_URLS["notices"].format(fy=row.get("fiscal_year") or ""),
            title=row["title"], description=(row["description"] or "")[:400],
            closing_date=row["closing_date"], notice_type=row["notice_type"],
            opportunity_kind=row["opportunity_kind"],
            solicitation_number=row["solicitation_number"],
            amendment_number=row["amendment_number"],
            end_user=row["end_user"], contracting_entity=row["contracting_entity"],
            unspsc=row["unspsc"], org_key_source=row["org_key_source"]))

    for row in gate.awards():
        if dept not in split_org_keys(row["org_keys"]):
            continue
        if not capability_matches_notice(cap, row):
            continue
        out["A"].append(_rec(
            "awards", row["reference_number"], row["publication_date"], "observed",
            "awards.publication_date <= T0",
            SOURCE_URLS["awards"].format(fy=row.get("fiscal_year") or ""),
            title=row["title"], vendor=row["vendor"], award_date=row["award_date"],
            contract_start_date=row["contract_start_date"],
            contract_end_date=row["contract_end_date"],
            contract_value=row["contract_value"],
            procurement_method=row["procurement_method"],
            solicitation_number=row["solicitation_number"]))

    for row in gate.contracts():
        if row["dept_key"] != dept:
            continue
        matched = capability_matches_contract(cap, row)
        out["B"].append(_rec(
            "contracts", f"{row['family_id']}|{row['reference']}",
            row["disclosure_date"], "derived",
            "quarter_end(max(reporting_period, reference)) + 30d <= T0",
            SOURCE_URLS["contracts"],
            capability_matched=bool(matched), matcher=matched or "none",
            vendor=row["vendor_norm"], description=(row["description"] or "")[:200],
            effective_at=row["contract_date"], period_start=row["period_start"],
            period_end=row["period_end"], value=row["value"],
            commodity_code=row["commodity_code"],
            solicitation_procedure=row["solicitation_procedure"],
            number_of_bids=row["number_of_bids"],
            reporting_period=row["reporting_period"]))

    plan_ids = _plan_org_ids().get(dept, set())
    for row in gate.plan_rows():
        if row["organization_id"] not in plan_ids:
            continue
        # Department level ONLY. No text classifier attributes a program to a
        # capability here, deliberately: inventing an inferred relation to make
        # condition C look fuller is the one thing this experiment must not do,
        # since how much C actually connects is part of what it measures.
        published = dp_available(row["year"]) if row["_dp_visible"] else drr_available(row["year"])
        out["C"].append(_rec(
            "plans", f"{row['year']}|{row['organization_id']}|{row['program_id']}",
            published, "derived",
            "DP fields at {Y}-03-01, DRR fields at {Y+1}-12-01, gated separately",
            SOURCE_URLS["plans"],
            fiscal_year=row["year"], program=row["program_name"],
            core_responsibility=row["core_responsibility"],
            dp_visible=row["_dp_visible"], drr_visible=row["_drr_visible"],
            planned_spending=row["planned_spending"],
            planned_spending_next=row["planned_spending_next"],
            planned_ftes=row["planned_ftes"],
            planning_explanation=(row["planning_explanation"] or "")[:500],
            actual_spending=row["actual_spending"],
            variance_explanation=(row["variance_explanation"] or "")[:500],
            capability_attributed=False))

    for row in gate.audits():
        if row["dept_key"] != dept:
            continue
        out["D"].append(_rec(
            "audits", f"{row['oag_id']}|{row['dept_key']}", row["date_published"],
            "observed" if row["method"] == "direct" else "inferred",
            "audits.date_published <= T0 (portal date, runs late)",
            row["html_url"] or row["pdf_url"] or "",
            title=row["title"], description=(row["description"] or "")[:400],
            doc_type=row["doc_type"], record_class=row["record_class"],
            report_year=row["year"],
            method=row["method"], confidence=("high" if row["method"] == "direct" else "medium"),
            supporting_evidence=row["evidence"],
            n_parent_reports=row["n_parent_reports"],
            capability_attributed=False))

    return out


_PLAN_ORG_CACHE: dict = {}


def _plan_org_ids() -> dict:
    """canonical key -> the InfoBase organization_ids that belong to it."""
    if not _PLAN_ORG_CACHE:
        for r in _rows(CROSSWALK_DB,
                       "SELECT canonical_key, plan_organization_id FROM org_crosswalk "
                       "WHERE plan_organization_id IS NOT NULL"):
            _PLAN_ORG_CACHE.setdefault(r["canonical_key"], set()).add(r["plan_organization_id"])
    return _PLAN_ORG_CACHE


def _money(v) -> str:
    if v is None or v == "":
        return "not stated"
    try:
        return f"${float(v):,.0f}"
    except (TypeError, ValueError):
        return str(v)


def _none_block(source: str, t0: str) -> list[str]:
    """
    The absence wording, in one place so it cannot drift into a claim.

    "No rows were published by T0" is a statement about the corpus. "The
    department has no plan for this" is a statement about the department, it
    does not follow, and it is exactly the inference this experiment is
    supposed to be testing rather than assuming.
    """
    what = {"plans": "departmental plan or results-report rows",
            "audits": "Auditor General records attributed to this department",
            "contracts": "disclosed contract rows for this department",
            "procurement": "tender or award notices for this department and capability"}[source]
    return [f"NONE — no {what} had been published on or before {t0}.",
            "",
            "This records an absence in the published corpus as of that date. It is not",
            "evidence that the underlying activity did not exist, and it is not a",
            "statement about what the department was doing or planning."]


def render_bundle(case: Case, condition: str, evidence: dict) -> str:
    """The analyst-facing case file. Identical artifact for human and model."""
    L: list[str] = []
    L.append(f"# {case.case_id}")
    L.append("")
    L.append(f"**Department:** {case.department_display}")
    L.append(f"**Capability:** {case.capability_label} (UNSPSC segment {case.capability[-2:]})")
    L.append(f"**Evaluation date:** {case.t0}")
    L.append("")
    L.append("All evidence below was published on or before the evaluation date. Nothing")
    L.append("published after it appears, including how any of it turned out.")
    L.append("")

    if condition == "Z":
        L.append("## Evidence available as of that date")
        L.append("")
        L.append("NONE SUPPLIED. This case deliberately provides no retrieved evidence —")
        L.append("only the department, the capability and the date above.")
        L.append("")
    else:
        active = [c for c in ("A", "B", "C", "D") if c <= condition]
        L.append("## Evidence available as of that date")
        L.append("")
        L += _render_procurement(case, evidence["A"]) if "A" in active else []
        L += _render_contracts(case, evidence["B"]) if "B" in active else []
        L += _render_plans(case, evidence["C"]) if "C" in active else []
        L += _render_audits(case, evidence["D"]) if "D" in active else []

    L.append("---")
    L.append("")
    L.append("## Question")
    L.append("")
    L.append("Based only on this evidence, would you investigate this department/capability")
    L.append("as a potential future procurement?")
    L.append("")
    L.append("Answer with exactly these headings:")
    L.append("")
    L.append("**Decision:** INVESTIGATE or DO NOT INVESTIGATE")
    L.append("**Supporting evidence:** ")
    L.append("**Contradictory evidence:** ")
    L.append("**Unknowns:** ")
    L.append("**Confidence:** an integer 0-100")
    L.append("**Earliest plausible procurement window:** ")
    L.append("**Reasoning:** a short paragraph")
    L.append("")
    L.append("Distinguish what the evidence states as fact from what you are inferring.")
    return "\n".join(L) + "\n"


def _render_procurement(case: Case, recs: list[dict]) -> list[str]:
    L = ["### [PROCUREMENT]", ""]
    notices = sorted([r for r in recs if r["source"] == "notices"],
                     key=lambda r: r["published_at"], reverse=True)
    awards = sorted([r for r in recs if r["source"] == "awards"],
                    key=lambda r: r["published_at"], reverse=True)
    if not notices and not awards:
        return L + _none_block("procurement", case.t0) + [""]

    events = {r.get("solicitation_number") or r["source_row_key"] for r in notices}
    L.append(f"{len(notices)} tender notice(s) covering {len(events)} distinct solicitation(s), "
             f"and {len(awards)} award notice(s), matching this department and capability.")
    if len(notices) > MAX_NOTICES:
        L.append(f"The {MAX_NOTICES} most recent tender notices are shown below; "
                 f"{len(notices) - MAX_NOTICES} older ones are counted but not listed.")
    L.append("")
    if notices:
        L.append("**Tender notices**")
        L.append("")
        for r in notices[:MAX_NOTICES]:
            L.append(f"- `{r['published_at']}` — {r['title']}")
            L.append(f"  - reference {r['source_row_key']}, amendment {r['amendment_number']}, "
                     f"solicitation {r['solicitation_number']}")
            L.append(f"  - notice type: {r['notice_type'] or 'not stated'}; "
                     f"kind: {r['opportunity_kind'] or 'unknown'}; "
                     f"closing date as published: {r['closing_date'] or 'not stated'}")
            L.append(f"  - end user: {r['end_user'] or 'not stated'}; "
                     f"attributed via {r['org_key_source']}")
            if r["description"]:
                L.append(f"  - {r['description'].strip()[:300]}")
        L.append("")
    if awards:
        L.append("**Award notices**")
        L.append("")
        if len(awards) > MAX_AWARDS:
            L.append(f"The {MAX_AWARDS} most recent are shown; "
                     f"{len(awards) - MAX_AWARDS} older ones are counted but not listed.")
            L.append("")
        for r in awards[:MAX_AWARDS]:
            L.append(f"- `{r['published_at']}` — {r['title']}")
            L.append(f"  - vendor: {r['vendor'] or 'not stated'}; "
                     f"value as published: {_money(r['contract_value'])}")
            L.append(f"  - contract period as published: "
                     f"{r['contract_start_date'] or '?'} to {r['contract_end_date'] or '?'}")
            L.append(f"  - procurement method: {r['procurement_method'] or 'not stated'}")
        L.append("")
    return L


def _render_contracts(case: Case, recs: list[dict]) -> list[str]:
    L = ["### [CONTRACTS]", ""]
    if not recs:
        return L + _none_block("contracts", case.t0) + [""]

    matched = sorted([r for r in recs if r["capability_matched"]],
                     key=lambda r: r["published_at"], reverse=True)
    unmatched = [r for r in recs if not r["capability_matched"]]
    L.append("Proactive contract disclosure. A row appears here only once its disclosure")
    L.append("quarter had closed and the 30-day publication deadline had passed, so these")
    L.append("were public at the evaluation date; the award dates below are earlier and are")
    L.append("shown as the contract's own effective dates, not as availability.")
    L.append("")
    L.append(f"{len(recs)} disclosed contract row(s) for this department. "
             f"{len(matched)} carry a UNSPSC commodity code matching this capability; "
             f"{len(unmatched)} cannot be matched to a capability at all.")
    L.append("")
    L.append("The unmatched majority is a limit of the source, not a finding: contract")
    L.append("commodity codes are mostly GSIN, tender notices are classified in UNSPSC, and")
    L.append("no notice in this corpus carries both, so there is no evidence from which to")
    L.append("build a mapping. Those rows are summarised, not dropped.")
    L.append("")
    if matched:
        L.append("**Capability-matched contracts**")
        L.append("")
        if len(matched) > MAX_CONTRACTS:
            L.append(f"The {MAX_CONTRACTS} most recently disclosed are shown; "
                     f"{len(matched) - MAX_CONTRACTS} older ones are counted but not listed.")
            L.append("")
        for r in matched[:MAX_CONTRACTS]:
            L.append(f"- disclosed `{r['published_at']}` ({r['reporting_period'] or 'quarter from reference'})"
                     f" — {r['vendor'] or 'vendor not stated'}, {_money(r['value'])}")
            L.append(f"  - awarded {r['effective_at'] or '?'}; period "
                     f"{r['period_start'] or '?'} to {r['period_end'] or '?'}")
            L.append(f"  - commodity code {r['commodity_code']}; procedure "
                     f"{r['solicitation_procedure'] or 'not stated'}; "
                     f"bids {r['number_of_bids'] if r['number_of_bids'] is not None else 'not stated'}")
            if r["description"]:
                L.append(f"  - {r['description'].strip()[:160]}")
        L.append("")
    if unmatched:
        vendors: dict[str, float] = defaultdict(float)
        for r in unmatched:
            vendors[r["vendor"] or "not stated"] += (r["value"] or 0)
        top = sorted(vendors.items(), key=lambda kv: -kv[1])[:8]
        total = sum(r["value"] or 0 for r in unmatched)
        L.append("**Department-level contract context (not capability-matched)**")
        L.append("")
        L.append(f"{len(unmatched)} rows, {_money(total)} total as disclosed. "
                 f"Largest suppliers by disclosed value:")
        L.append("")
        for v, amt in top:
            L.append(f"- {v} — {_money(amt)}")
        L.append("")
    return L


def _render_plans(case: Case, recs: list[dict]) -> list[str]:
    L = ["### [PLANS]", ""]
    if not recs:
        return L + _none_block("plans", case.t0) + [""]
    L.append("Departmental Plans and Departmental Results Reports, at DEPARTMENT level.")
    L.append("These are not attributed to the capability — no classifier was run to link a")
    L.append("program to a capability, because inventing that link to make this section")
    L.append("look fuller would corrupt what the experiment is measuring. Judge relevance")
    L.append("yourself from the program names and prose.")
    L.append("")
    L.append("Planned figures come from the Departmental Plan, tabled around March of the")
    L.append("fiscal year. Actual figures come from the Results Report, tabled around")
    L.append("November of the following year. Where a row shows planned figures but no")
    L.append("actuals, the results report had not been tabled at the evaluation date.")
    L.append("")
    with_text = [r for r in recs if r["planning_explanation"] or r["variance_explanation"]]
    show = sorted(with_text or recs,
                  key=lambda r: (-r["fiscal_year"], -(r["planned_spending"] or 0)))
    years = sorted({r["fiscal_year"] for r in recs})
    L.append(f"{len(recs)} program row(s) visible, fiscal years {years[0]}-{years[-1]}. "
             f"{len(with_text)} carry explanatory prose.")
    if len(show) > MAX_PLANS:
        L.append(f"The {MAX_PLANS} shown are the most recent by fiscal year, "
                 f"largest planned spending first.")
    L.append("")
    for r in show[:MAX_PLANS]:
        L.append(f"- **FY{r['fiscal_year']}** {r['program']} "
                 f"({r['core_responsibility'] or 'core responsibility not stated'})")
        L.append(f"  - planned spending: {_money(r['planned_spending'])}; "
                 f"next year planned: {_money(r['planned_spending_next'])}; "
                 f"planned FTEs: {r['planned_ftes'] if r['planned_ftes'] is not None else 'not stated'}")
        if r["drr_visible"]:
            L.append(f"  - actual spending: {_money(r['actual_spending'])}")
        else:
            L.append("  - actual spending: results report not yet tabled at this date")
        if r["planning_explanation"]:
            L.append(f"  - planning explanation: {r['planning_explanation'].strip()}")
        if r["variance_explanation"]:
            L.append(f"  - variance explanation: {r['variance_explanation'].strip()}")
    L.append("")
    return L


def _render_audits(case: Case, recs: list[dict]) -> list[str]:
    L = ["### [AUDITS]", ""]
    if not recs:
        return L + _none_block("audits", case.t0) + [""]
    L.append("Office of the Auditor General records naming this department, at DEPARTMENT")
    L.append("level and not attributed to the capability, for the same reason as plans.")
    L.append("")
    L.append("Attribution is either direct (the record names the department in its own")
    L.append("fields) or inherited (a committee briefing package inherits the departments")
    L.append("of the report it concerns). Inherited attribution is an inference and is")
    L.append("labelled as one below.")
    L.append("")
    ordered = sorted(recs, key=lambda r: r["published_at"], reverse=True)
    L.append(f"{len(recs)} record(s) published on or before the evaluation date.")
    if len(ordered) > MAX_AUDITS:
        L.append(f"The {MAX_AUDITS} most recent are shown.")
    L.append("")
    for r in ordered[:MAX_AUDITS]:
        L.append(f"- `{r['published_at']}` — {r['title']}")
        L.append(f"  - type: {r['doc_type']}; class: {r['record_class']}; "
                 f"report year {r['report_year']}")
        L.append(f"  - attribution: {r['method']} "
                 f"({'observed' if r['relation_class'] == 'observed' else 'inferred'}), "
                 f"matched on \"{r['supporting_evidence']}\"")
        if r["description"]:
            L.append(f"  - {r['description'].strip()[:300]}")
    L.append("")
    return L


# ---------------------------------------------------------------------------
# Writing the casebook
# ---------------------------------------------------------------------------
# THE SEAL IS A SEPARATE FILE, not a flag on the case record. A field the
# harness is asked politely not to read is not hidden; a file it never opens is.
# cases.json holds everything the evaluator may see, truth.json holds the label
# and the eventual tender, and case_metrics.py is the only module that opens the
# second one — after predictions are written and hashed.

PUBLIC_CASE_FIELDS = (
    "case_id", "department", "department_display", "capability",
    "capability_label", "t0", "outcome_window_months", "outcome_window_end",
    "frame_version", "matcher_hash", "eligibility_basis", "history_depth_days",
    "prior_activity", "prior_activity_decile", "match_tier", "matched_case_id",
)
SEALED_CASE_FIELDS = (
    "case_id", "ground_truth", "outcome_event_ids", "outcome_kinds",
    "eventual_tender_id", "eventual_tender_date", "lead_time_days",
)


def write_casebook(cases: list[Case], caps: list[Capability], matcher_hash: str,
                   out_dir: Path = CASES_DIR, verbose: bool = True) -> dict:
    bundles_dir = out_dir / "bundles"
    bundles_dir.mkdir(parents=True, exist_ok=True)
    caps_by_id = {c.id: c for c in caps}

    public = [{f: getattr(c, f) for f in PUBLIC_CASE_FIELDS} for c in cases]
    sealed = [{f: list(getattr(c, f)) if isinstance(getattr(c, f), tuple)
               else getattr(c, f) for f in SEALED_CASE_FIELDS} for c in cases]

    manifest: dict = {}
    sizes: dict = defaultdict(list)
    for case in cases:
        gate = as_of(case.t0)
        ev = collect_evidence(case, caps_by_id[case.capability], gate)
        ids = {"Z": []}
        cumulative: list[str] = []
        for cond in ("A", "B", "C", "D"):
            cumulative = cumulative + [r["evidence_id"] for r in ev[cond]]
            ids[cond] = list(cumulative)
        for cond in CONDITIONS:
            text = render_bundle(case, cond, ev)
            (bundles_dir / f"{case.case_id}-{cond}.md").write_text(text, encoding="utf-8")
            sizes[cond].append(len(text))
        (out_dir / "evidence").mkdir(parents=True, exist_ok=True)
        (out_dir / "evidence" / f"{case.case_id}.json").write_text(
            json.dumps({"case_id": case.case_id, "t0": case.t0, "by_condition": ev},
                       indent=2, default=str), encoding="utf-8")
        manifest[case.case_id] = {c: ids[c] for c in CONDITIONS}

    (out_dir / "cases.json").write_text(json.dumps({
        "frame_version": 1, "matcher_hash": matcher_hash,
        "outcome_window_months": OUTCOME_MONTHS,
        "generated": _dt.datetime.now().isoformat(timespec="seconds"),
        "cases": public}, indent=2), encoding="utf-8")
    (out_dir / "truth.json").write_text(json.dumps({
        "matcher_hash": matcher_hash,
        "warning": "SEALED. Do not open before predictions are written and hashed.",
        "truth": sealed}, indent=2), encoding="utf-8")
    (out_dir / "evidence_manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8")

    if verbose:
        print(f"Wrote {len(cases)} cases x {len(CONDITIONS)} conditions "
              f"= {len(cases) * len(CONDITIONS)} bundles to {bundles_dir}")
        for cond in CONDITIONS:
            n = sizes[cond]
            print(f"  {cond}: median bundle {sorted(n)[len(n) // 2]:,} chars "
                  f"({CONDITION_LABELS[cond]})")
    return manifest


def check_nesting(manifest: dict) -> list[str]:
    """A subset B subset C subset D, per case, by evidence id. Structural, not trusted."""
    problems = []
    for case_id, conds in manifest.items():
        for lo, hi in (("A", "B"), ("B", "C"), ("C", "D")):
            if not set(conds[lo]).issubset(set(conds[hi])):
                problems.append(f"{case_id}: {lo} is not a subset of {hi}")
        if conds["Z"]:
            problems.append(f"{case_id}: Z carries evidence and must carry none")
    return problems


def condition_deltas(manifest: dict) -> dict:
    """How many items each condition actually adds, and how often it adds none."""
    out = {}
    for lo, hi in (("Z", "A"), ("A", "B"), ("B", "C"), ("C", "D")):
        added = [len(set(c[hi]) - set(c[lo])) for c in manifest.values()]
        out[f"{lo}->{hi}"] = {
            "median_added": sorted(added)[len(added) // 2],
            "cases_adding_nothing": sum(1 for a in added if a == 0),
            "n_cases": len(added),
        }
    return out


def within_pair_discrimination(manifest: dict, cases: list) -> dict:
    """
    Per matched pair, does each condition supply DIFFERENT evidence to the two?

    THE CHECK THAT DECIDES WHETHER A CONDITION CAN CONTRIBUTE AT ALL. If the two
    cases in a pair receive byte-identical contract, plan and audit sections,
    then no evaluator however good can use those sections to tell them apart,
    and any difference in its answers is noise. That is not a weak signal, it is
    the absence of an input.

    It is the expected outcome for department_exact pairs and it is worth
    surfacing rather than discovering later: plans and audits attach to a
    DEPARTMENT, so holding the department constant holds them constant too. The
    same turns out to be true of contracts in practice, because only the 8.4%
    UNSPSC-coded minority can be capability-matched at all.

    Reported per tier so the two controls can be read apart.
    """
    by_id = {c["case_id"]: c for c in cases}
    seen: set = set()
    out: dict = {}
    for case_id, case in by_id.items():
        mate = case["matched_case_id"]
        if not mate or (key := tuple(sorted((case_id, mate)))) in seen:
            continue
        seen.add(key)
        a, b = manifest[key[0]], manifest[key[1]]
        tier = case["match_tier"]
        row = out.setdefault(tier, {"pairs": 0, "A": 0, "B": 0, "C": 0, "D": 0})
        row["pairs"] += 1
        if set(a["A"]) != set(b["A"]):
            row["A"] += 1
        for lo, hi in (("A", "B"), ("B", "C"), ("C", "D")):
            if (set(a[hi]) - set(a[lo])) != (set(b[hi]) - set(b[lo])):
                row[hi] += 1
    return out


def audit_bundles(out_dir: Path = CASES_DIR) -> tuple[list[str], dict]:
    """
    Read the written artifacts back and look for leaks. Reads FILES, not memory.

    TWO KINDS OF DATE, AND CONFLATING THEM IS THE MISTAKE THIS FUNCTION MADE
    FIRST. An AVAILABILITY date says when the public could see a record and must
    never exceed T0. A STATED FORWARD date — a closing date, a contract period
    end, next year's planned spending — is a forecast the source published
    BEFORE T0, and a bundle that hid it would be hiding evidence the analyst
    genuinely had. The first version flagged every date after T0 and produced
    195 findings, all of them the second kind.

    So: availability dates are checked and must hold. Forward dates are counted
    and reported, not failed, and the count is what a reviewer eyeballs to
    confirm they are the kind of thing they should be.
    """
    cases = json.loads((out_dir / "cases.json").read_text(encoding="utf-8"))["cases"]
    t0_by_id = {c["case_id"]: c["t0"] for c in cases}
    problems: list[str] = []
    forward = 0

    # 1. The invariant, checked against the structured record rather than prose.
    for case in cases:
        t0 = case["t0"]
        path = out_dir / "evidence" / f"{case['case_id']}.json"
        blob = json.loads(path.read_text(encoding="utf-8"))
        for cond, recs in blob["by_condition"].items():
            for r in recs:
                if r["published_at"] and r["published_at"] > t0:
                    problems.append(
                        f"{case['case_id']}/{cond}: {r['source']} {r['evidence_id']} "
                        f"is available {r['published_at']}, after T0 {t0}")

    # 2. The rendered stamps. Every availability date in a bundle is written as
    #    a leading backticked date, so these are the ones that must hold; other
    #    dates in the prose are the published forecasts.
    stamp_re = re.compile(r"`(20\d{2}-\d{2}-\d{2})`")
    any_re = re.compile(r"\b(20\d{2}-\d{2}-\d{2})\b")
    for path in sorted((out_dir / "bundles").glob("*.md")):
        case_id = path.stem.rsplit("-", 1)[0]
        t0 = t0_by_id[case_id]
        text = path.read_text(encoding="utf-8")
        low = text.lower()
        stamps = set(stamp_re.findall(text))
        for stamp in stamps:
            if stamp > t0:
                problems.append(f"{path.name}: availability stamp {stamp} after T0 {t0}")
        forward += sum(1 for d in any_re.findall(text)
                       if d > t0 and d not in stamps)
        for col in SUPPRESSED_COLUMNS:
            if re.search(rf"(^|[^a-z_]){col}\s*[:=]", low, re.M):
                problems.append(f"{path.name}: suppressed field '{col}' is rendered")

    return problems, {"forward_looking_dates_rendered": forward,
                      "bundles_checked": len(list((out_dir / "bundles").glob("*.md")))}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("build-db", help="Materialize contracts with disclosure dates")
    sub.add_parser("freeze", help="Select capabilities by rule and write the registry")
    sub.add_parser("frame", help="Build the frame and report coverage")
    p = sub.add_parser("pilot", help="Draw the pilot, write bundles, audit them")
    p.add_argument("--pairs", type=int, default=10)
    p.add_argument("--seed", type=int, default=20260816)
    sub.add_parser("audit", help="Re-check written bundles for leaks")
    args = parser.parse_args()

    if args.cmd == "build-db":
        build_casebook_db()
        return 0

    if args.cmd == "freeze":
        UNSPSC_LABELS.update(load_unspsc_labels())
        caps = select_capabilities()
        CAPABILITIES_YAML.parent.mkdir(parents=True, exist_ok=True)
        CAPABILITIES_YAML.write_text(_yaml_dump(caps, {
            "frame_version": 1, "selection_cutoff": SELECTION_CUTOFF,
            "generated": _dt.date.today().isoformat()}), encoding="utf-8")
        print(f"\nWrote {CAPABILITIES_YAML}")
        _, h = load_capabilities()
        print(f"matcher_hash {h}")
        return 0

    caps, matcher_hash = load_capabilities()

    if args.cmd == "frame":
        build_frame(caps)
        return 0

    if args.cmd == "pilot":
        cells = build_frame(caps)
        cases = sample_pilot(cells, caps, matcher_hash,
                             n_pairs=args.pairs, seed=args.seed)
        manifest = write_casebook(cases, caps, matcher_hash)
        print()
        deltas = condition_deltas(manifest)
        print("What each condition adds:")
        for k, v in deltas.items():
            print(f"  {k:>6}: median {v['median_added']:>4} items, "
                  f"{v['cases_adding_nothing']}/{v['n_cases']} cases gain nothing")
        cases_public = json.loads(
            (CASES_DIR / "cases.json").read_text(encoding="utf-8"))["cases"]
        disc = within_pair_discrimination(manifest, cases_public)
        print()
        print("Pairs where a condition supplies DIFFERENT evidence to the two cases")
        print("(a condition that is identical within a pair cannot discriminate there):")
        for tier, row in sorted(disc.items()):
            print(f"  {tier}: {row['pairs']} pairs | "
                  f"A {row['A']}/{row['pairs']}  B adds differ {row['B']}/{row['pairs']}  "
                  f"C {row['C']}/{row['pairs']}  D {row['D']}/{row['pairs']}")
        nesting = check_nesting(manifest)
        print(f"\nNesting A subset B subset C subset D: "
              f"{'OK' if not nesting else 'BROKEN'}")
        for x in nesting[:10]:
            print(f"  {x}")
        problems, stats = audit_bundles()
        print(f"Leak audit over written artifacts: "
              f"{'CLEAN' if not problems else str(len(problems)) + ' PROBLEMS'}")
        for x in problems[:20]:
            print(f"  {x}")
        print(f"  ({stats['forward_looking_dates_rendered']} published forward-looking "
              f"dates across {stats['bundles_checked']} bundles - closing dates, "
              f"contract period ends, next-year planned spending)")
        return 1 if (nesting or problems) else 0

    if args.cmd == "audit":
        problems, stats = audit_bundles()
        print(f"{len(problems)} problem(s); {stats}")
        for x in problems:
            print(f"  {x}")
        return 1 if problems else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
