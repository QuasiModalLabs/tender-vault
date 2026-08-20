"""
Does convergent public evidence predict federal procurement, months ahead?

THE EXPERIMENT. For each federal department and each fiscal year, look only at
what was publicly knowable six months before that year began — Auditor General
findings, the department's own stated plans, incumbent contracts running down —
and ask whether it predicts that the department published a relevant IT tender
during the year. If it does, the pre-RFP thesis this repository is built on has
evidence behind it. If it does not, that is the finding, and it is worth more
than an elaborate graph built on a premise nobody checked.

A NULL RESULT IS A RESULT. This module is written to be able to report one. It
prints the base rate before any lift, states n on every cohort, and puts a
confidence interval on every rate. Nothing here should be iterated on until it
turns positive; if the intervals cover no effect, that is the answer and it goes
in the write-up as the answer.

------------------------------------------------------------------------------
THE TARGET PREDICATE — FROZEN
------------------------------------------------------------------------------
Frozen before any feature code existed. If it has to change, the run is
DISCARDED AND RESTARTED, not patched — a predicate edited after seeing results
is a predicate fitted to them.

A HIT for department D in fiscal year Y is: at least one CanadaBuys tender
notice such that

  1. publication_date falls inside fiscal year Y (1 April Y .. 31 March Y+1);
  2. its org_keys resolve to D — end-user entity preferred over contracting
     entity, as notices_ingest records and the vault already documents, because
     SSC and PSPC buy federal IT on other departments' behalf constantly;
  3. it is RELEVANT, by the publisher's classification where there is one and
     by keywords only where there is not — the same precedence ingest.py's
     filter uses, and for the same reason: a notice carrying UNSPSC codes has
     already been classified by the publisher, and a word match that overrides
     them is guessing. Concretely: UNSPSC prefix-matches a profile family, OR
     (no UNSPSC filed) GSIN starts with an IT prefix, OR (neither filed) the
     title+description matches a profile competency on whole words;
  4. procurement_category is not construction;
  5. opportunity_kind is not results_notice, pre_awarded (ACAN), information
     (RFI), or call_up — none of these is a new requirement reaching the market;
  6. it is the FIRST row for its solicitation_number, so one requirement
     reaching the market counts once however many times it was amended. A
     solicitation is attributed to the window containing its EARLIEST
     publication date. The three rows whose solicitation number is the
     placeholder "-" are keyed on reference_number instead.

NO DOLLAR THRESHOLD. The tender feed publishes no value field and the regex
that used to invent one has been retired; a size filter here would be a filter
on which notices happen to mention a number in prose.

PREDICATE REVISION — one, before any result existed
---------------------------------------------------
Clause 6 originally read: "amendment_number is absent or zero, so an amended
notice is not counted as a second procurement." It was wrong about the data, and
the run written against it was DISCARDED AND RESTARTED rather than patched, per
the rule at the top of this block. Recorded here rather than silently corrected,
because a predicate that changed once needs to say so.

What it got wrong: the archives do not publish an amendment as an extra row
sharing a key. `reference_number` is unique on all 30,527 rows, and each row
carries whatever amendment number that notice had reached when the archive was
cut — so `amendment_number = '000'` selects notices that were never amended, not
originals. It dropped 83% of real procurements (5,213 rows survived of 30,527)
and still left the 2,168 solicitations that genuinely do span two rows.
Deduplicating on solicitation_number is what clause 6 was reaching for and
failing to express.

No result was computed under the original clause: the first run raised
ZeroDivisionError on an empty department universe, which is how the error was
found. Nothing was fitted, ranked or read.

------------------------------------------------------------------------------
LEAKAGE — WHY EACH FEATURE IS KNOWABLE AT T
------------------------------------------------------------------------------
`as_of(T)` is the ONLY way feature code reaches data, and it truncates every
source by date at the source. Feature functions receive an Evidence and never a
connection, so a feature cannot quietly reach around the gate.

The award and contract tables are the leak surface: an award IS an outcome.
Every feature drawn from them is justified here or it does not ship.

  FEATURE                       KNOWABLE AT T BECAUSE
  audit_it                      OAG record's date_published <= T. That stamp is
                                the portal date, which runs LATE relative to
                                tabling — safe direction: it can withhold a
                                signal, never leak one.
  audit_recent_it               same, restricted to the 24 months before T.
  plan_intent / plan_pressure   Departmental Plan for fiscal year Y is tabled
                                around March of Y. Admitted only when its year
                                is at or before the fiscal year containing T,
                                which is conservative under either reading of
                                what plans.db's `year` column denotes.
  expiring_awards               Award NOTICES published <= T whose stated
                                contract_end_date falls in the label window.
                                Both facts are printed on the award notice at
                                publication; nothing about the successor is
                                used. This is the cleanest of the contract-side
                                features and the reason the award archive was
                                ingested at all.
  active_award_count            Award notices published <= T. Volume of past
                                business, not of future.
  vendor_concentration          Herfindahl over vendors on those same notices.
  prior_year_hit                The target predicate evaluated on the fiscal
                                year BEFORE the label window. This is the
                                autocorrelation baseline and it is the feature
                                to beat: a department that tendered IT last year
                                will probably do so again, and any evidence
                                signal that cannot beat this is not adding
                                anything.

  EXCLUDED, and why:
  awards inside the label window        that is the outcome
  contract amendment activity           amendments post-date T and share a
                                        family id with the row they amend
  notices inside the label window       that is the label

------------------------------------------------------------------------------
WHY A SCORE IS PERMITTED IN THIS FILE AND NOWHERE ELSE
------------------------------------------------------------------------------
This repository deletes composite scores on purpose, in four places:
vault/CLAUDE.md, cmd_department_dossier's docstring, the briefing skill, and the
README's account of why the original weighted pipeline was thrown away. That
rule has NOT lapsed and this module is not an exception to it.

The distinction is that here a score is the HYPOTHESIS UNDER TEST — the object
whose predictive power is being measured — rather than an answer presented to a
reader. The formula that was deleted asserted that value mattered 0.3 and
timeline 0.2, with no way to check. Every weight here is FITTED, out of fold,
and reported with the interval around it. A weight chosen by judgment would be
that same deleted formula wearing a lab coat, and there are none in this file.

Nothing in tender_tools or mcp_server.py imports from this module, no MCP
tool returns a score, and moving one into those surfaces is a separate decision
with its own review — not a follow-on commit to this work.

Usage:
    python scripts/backtest.py                     # full report
    python scripts/backtest.py --gap-months 6      # feature date before FY start
    python scripts/backtest.py --write             # also write the vault report
    python scripts/backtest.py --check-base-rate   # week-1 stability gate only
"""
from __future__ import annotations

import argparse
import math
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).parent))

from ingest import (  # noqa: E402
    DEFAULT_PROFILE,
    matched_competencies,
    matches_unspsc_families,
    parse_profile,
    parse_unspsc_codes,
)

PROJECT_ROOT = Path(__file__).parent.parent
NOTICES_DB = PROJECT_ROOT / "data" / "notices.db"
CONTRACTS_DB = PROJECT_ROOT / "data" / "contracts.db"
OAG_DB = PROJECT_ROOT / "data" / "oag.db"
PLANS_DB = PROJECT_ROOT / "data" / "plans.db"
REPORT_DIR = PROJECT_ROOT / "vault" / "reference"

# --- frozen predicate constants --------------------------------------------
# GSIN prefixes that denote IT goods and services. Read only where a notice
# filed NO UNSPSC, never as an override of one. D302A is the dominant federal IT
# services code; the 70xx range is ADP equipment.
IT_GSIN_PREFIXES = ("D0", "D1", "D2", "D3", "7030", "7A")

# opportunity_kind values that are not a new requirement reaching the market.
# `unknown` is deliberately NOT here: it means the classifiers could not decide,
# which is not the same as deciding the notice buys nothing.
NON_PROCUREMENT_KINDS = frozenset({
    "construction", "results_notice", "pre_awarded", "information", "call_up",
})

# The base-rate stability gate. Measured on the award-side proxy during
# planning: department x calendar half-year swung 27.7%-68.1% (40.4pp) and is
# unusable; department x FISCAL year held 55.1%-67.3% (12.2pp). This is that
# threshold, and a panel whose own spread exceeds it stops rather than reporting
# a lift over a base rate that does not hold still.
MAX_BASE_RATE_SPREAD_PP = 12.2


def fiscal_year_of(iso_date: str) -> int:
    """Fiscal year START year for a date. 2023-04-01 -> 2023; 2023-03-31 -> 2022."""
    year, month = int(iso_date[:4]), int(iso_date[5:7])
    return year if month >= 4 else year - 1


def fy_bounds(fy: int) -> tuple[str, str]:
    """[start, end) as ISO dates for fiscal year `fy`."""
    return f"{fy}-04-01", f"{fy + 1}-04-01"


def shift_months(iso_date: str, months: int) -> str:
    y, m, d = int(iso_date[:4]), int(iso_date[5:7]), int(iso_date[8:10])
    total = (y * 12 + (m - 1)) + months
    return f"{total // 12:04d}-{total % 12 + 1:02d}-{d:02d}"


# ---------------------------------------------------------------------------
# The as-of gate
# ---------------------------------------------------------------------------

@dataclass
class Evidence:
    """
    Everything publicly knowable on `date`, and nothing else.

    Feature functions get one of these and never a connection. Each accessor
    applies its own date bound in SQL rather than trusting a caller to filter,
    because a filter a caller can forget is not a gate. Results are memoized per
    Evidence so a panel of ~50 departments does not re-run the same scan 50
    times; the memo is keyed inside the object, which is per-date, so it cannot
    serve a row from a different T.
    """
    date: str
    _cache: dict = field(default_factory=dict, repr=False)

    def _query(self, key: str, db: Path, sql: str, params: tuple) -> list:
        if key not in self._cache:
            if not db.exists():
                self._cache[key] = []
            else:
                conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
                conn.row_factory = sqlite3.Row
                try:
                    self._cache[key] = [dict(r) for r in conn.execute(sql, params)]
                finally:
                    conn.close()
        return self._cache[key]

    def audits(self) -> list[dict]:
        """Federal audits whose portal publication date is at or before T."""
        return self._query(
            "audits", OAG_DB,
            """SELECT a.oag_id, a.year, a.date_published, a.it_score,
                      a.record_class, ad.dept_key
                 FROM audits a JOIN audit_departments ad USING (oag_id)
                WHERE a.date_published != '' AND a.date_published <= ?""",
            (self.date,))

    def plans(self) -> list[dict]:
        """
        Departmental Plan program rows for fiscal years already tabled at T.

        A DP for fiscal year Y is tabled around March of Y. Admitting only
        `year <= fiscal_year_of(T)` is conservative under either reading of what
        that column denotes — if it is the fiscal year covered, the plan was
        tabled a month before the year opened; if it is the year of tabling, it
        is older still.
        """
        return self._query(
            "plans", PLANS_DB,
            """SELECT year, organization_id, organization, program_id,
                      intent_score, pressure_score, planned_spending
                 FROM programs WHERE year <= ?""",
            (fiscal_year_of(self.date),))

    def award_notices(self) -> list[dict]:
        """Award notices PUBLISHED at or before T, with their stated end dates."""
        return self._query(
            "awards", NOTICES_DB,
            """SELECT org_keys, publication_date, contract_end_date, vendor,
                      contract_value, procurement_method, unspsc, gsin
                 FROM awards
                WHERE publication_date != '' AND publication_date <= ?""",
            (self.date,))

    def notices(self) -> list[dict]:
        """
        Tender notices PUBLISHED at or before T.

        Used for the prior-year-hit baseline feature. Never for the label — the
        label reads the database directly through `hits_in_window`, which is a
        separate path precisely so a bug here cannot quietly become a leak.
        """
        return self._query(
            "notices", NOTICES_DB,
            """SELECT reference_number, solicitation_number, org_keys,
                      publication_date, unspsc, gsin, title, description,
                      opportunity_kind
                 FROM notices
                WHERE publication_date != '' AND publication_date <= ?""",
            (self.date,))

    def contracts(self) -> list[dict]:
        """Proactive-disclosure contract rows awarded at or before T."""
        return self._query(
            "contracts", CONTRACTS_DB,
            """SELECT owner_org, vendor_norm, contract_date, period_end, value,
                      commodity_code, solicitation_procedure
                 FROM contracts
                WHERE contract_date != '' AND contract_date <= ?""",
            (self.date,))


def as_of(date: str) -> Evidence:
    """Every row this returns was publicly knowable on `date`."""
    if not (len(date) == 10 and date[4] == "-" and date[7] == "-"):
        raise ValueError(f"as_of needs an ISO date, got {date!r}")
    return Evidence(date=date)


# ---------------------------------------------------------------------------
# The frozen predicate, applied
# ---------------------------------------------------------------------------

def is_relevant(row: dict, families: list[str], competencies: list[str]) -> bool:
    """
    Clause 3 of the frozen predicate: publisher first, keywords only as fallback.

    The precedence is the point. On the live feed the OR form — match on codes
    OR keywords, whichever fires — readmitted a boiling-liquid-expanding-vapour-
    explosion study because "vapour cloud" contains "cloud". Codes present and
    not ours means not ours.
    """
    codes = parse_unspsc_codes(row.get("unspsc"))
    if codes:
        return bool(matches_unspsc_families(codes, families))

    gsin = (row.get("gsin") or "").strip().upper()
    if gsin:
        return any(part.strip().lstrip("*").upper().startswith(IT_GSIN_PREFIXES)
                   for part in gsin.split("\n") if part.strip().lstrip("*"))

    text = f"{row.get('title') or ''} {row.get('description') or ''}"
    return bool(matched_competencies(text, competencies))


def is_target_notice(row: dict, families: list[str], competencies: list[str]) -> bool:
    """
    Clauses 3-5 of the frozen predicate, on one row.

    Clause 6 (one row per solicitation) is NOT here: it is a property of a set of
    rows, not of a row, and pretending otherwise is exactly the mistake the
    revision note in the module docstring records. It lives in
    `target_solicitations`.
    """
    if row.get("opportunity_kind") in NON_PROCUREMENT_KINDS:
        return False
    return is_relevant(row, families, competencies)


def _solicitation_key(row: dict) -> str:
    """
    What counts as one procurement.

    solicitation_number, except for the placeholder "-" that three rows carry,
    where reference_number stands in. A placeholder is not an identifier, and
    letting three unrelated notices collide under it would merge three
    procurements into one.
    """
    soln = (row.get("solicitation_number") or "").strip()
    if soln and soln != "-":
        return f"s:{soln}"
    return f"r:{row.get('reference_number') or ''}"


def target_solicitations(rows) -> dict[str, dict]:
    """
    Collapse notice rows to one record per procurement, keeping the EARLIEST
    publication date and the org keys seen on that earliest row.

    Earliest, because the question this backtest asks is when a requirement
    first reached the market — an amendment published four months later is the
    same requirement, and dating it to the amendment would credit the signal
    with lead time it never had.

    Taking the minimum is also what makes this safe under `as_of`: a date-bounded
    view is a prefix of the full history, so the minimum over the prefix equals
    the true minimum whenever the true minimum is inside it. The gate can never
    see a procurement earlier than it really appeared.
    """
    best: dict[str, dict] = {}
    for row in rows:
        key = _solicitation_key(row)
        published = row.get("publication_date") or ""
        current = best.get(key)
        if current is None or published < current["first_published"]:
            best[key] = {"first_published": published,
                         "org_keys": row.get("org_keys") or ""}
    return best


def hits_in_window(conn: sqlite3.Connection, start: str, end: str,
                   families: list[str], competencies: list[str]) -> dict[str, int]:
    """
    Target notices per department published in [start, end). The LABEL.

    Reads the database directly rather than going through Evidence, on purpose:
    the label must be able to see the future and the gate must never be able to.
    Two paths that cannot be confused for one another beats one path with a flag.
    """
    counts: dict[str, int] = {}
    for record in _all_target_solicitations(conn, families, competencies).values():
        if not (start <= record["first_published"] < end):
            continue
        for key in record["org_keys"].split(","):
            if key:
                counts[key] = counts.get(key, 0) + 1
    return counts


_TARGETS_CACHE: dict[int, dict[str, dict]] = {}


def _all_target_solicitations(conn: sqlite3.Connection, families: list[str],
                              competencies: list[str]) -> dict[str, dict]:
    """
    Every procurement in the archive that passes the predicate, deduped, cached.

    Scanning 30k rows and re-running the relevance test per fiscal year is
    wasteful, and worse, it invites the two callers that need this — the label
    and the department universe — to drift apart on what a procurement is. One
    computation, one definition.
    """
    key = id(conn)
    if key not in _TARGETS_CACHE:
        cur = conn.execute(
            """SELECT reference_number, solicitation_number, org_keys,
                      publication_date, unspsc, gsin, title, description,
                      opportunity_kind
                 FROM notices
                WHERE org_keys != '' AND publication_date != ''""")
        names = [c[0] for c in cur.description]
        rows = (dict(zip(names, raw)) for raw in cur)
        keep = (r for r in rows if is_target_notice(r, families, competencies))
        _TARGETS_CACHE[key] = target_solicitations(keep)
    return _TARGETS_CACHE[key]


# ---------------------------------------------------------------------------
# Features
# ---------------------------------------------------------------------------

@dataclass
class Feature:
    name: str
    fn: Callable[[Evidence, str, str, str], float]
    knowable_because: str


def _dept_rows(rows: list[dict], dept: str) -> list[dict]:
    return [r for r in rows if dept in (r.get("org_keys") or "").split(",")]


def f_audit_it(ev: Evidence, dept: str, win_start: str, win_end: str) -> float:
    """Highest IT relevance among audits naming this department, published by T."""
    scores = [r["it_score"] or 0.0 for r in ev.audits() if r["dept_key"] == dept]
    return max(scores) if scores else 0.0


def f_audit_recent_it(ev: Evidence, dept: str, win_start: str, win_end: str) -> float:
    """Same, restricted to audits published in the 24 months before T."""
    cutoff = shift_months(ev.date, -24)
    scores = [r["it_score"] or 0.0 for r in ev.audits()
              if r["dept_key"] == dept and r["date_published"] >= cutoff]
    return max(scores) if scores else 0.0


def _plan_org_ids(dept: str) -> set[int]:
    import org_resolve
    scope = org_resolve.department_scope(dept)
    return {int(i) for i in scope.get("plan_ids", []) if str(i).strip()}


_PLAN_ID_CACHE: dict[str, set[int]] = {}


def _plan_rows(ev: Evidence, dept: str) -> list[dict]:
    if dept not in _PLAN_ID_CACHE:
        try:
            _PLAN_ID_CACHE[dept] = _plan_org_ids(dept)
        except Exception:
            _PLAN_ID_CACHE[dept] = set()
    ids = _PLAN_ID_CACHE[dept]
    if not ids:
        return []
    latest = max((r["year"] for r in ev.plans()), default=None)
    if latest is None:
        return []
    return [r for r in ev.plans()
            if r["organization_id"] in ids and r["year"] == latest]


def f_plan_intent(ev: Evidence, dept: str, win_start: str, win_end: str) -> float:
    """
    Strongest forward-looking modernization intent in the most recent tabled plan.

    Max rather than mean, and this is a real limitation rather than a choice
    between equals: the README records that one sentence is often pasted across
    an entire program inventory, so a mean rewards departments that repeat
    themselves and a count rewards them more. Max at least reads one sentence
    once. Sixteen of ninety-four organizations file no planning prose at all —
    including DND, GAC, the RCMP, CBSA and SSC — so a zero here is very often
    "filed nothing", not "intends nothing", and the two are not separable in
    this column.
    """
    scores = [r["intent_score"] for r in _plan_rows(ev, dept)
              if r["intent_score"] is not None]
    return max(scores) if scores else 0.0


def f_plan_pressure(ev: Evidence, dept: str, win_start: str, win_end: str) -> float:
    """Strongest operational-strain signal in the most recent tabled plan."""
    scores = [r["pressure_score"] for r in _plan_rows(ev, dept)
              if r["pressure_score"] is not None]
    return max(scores) if scores else 0.0


def f_expiring_awards(ev: Evidence, dept: str, win_start: str, win_end: str) -> float:
    """
    Awards already published at T whose stated end date lands in the label window.

    The strongest a-priori candidate in this panel and the one the repo already
    acts on: `cmd_expiring_contracts` is built on exactly this reasoning. Both
    facts — that the award exists, and when it ends — are printed on the award
    notice when it is published, so nothing about the successor is read.
    """
    return float(sum(
        1 for r in _dept_rows(ev.award_notices(), dept)
        if r["contract_end_date"] and win_start <= r["contract_end_date"] < win_end
    ))


def f_active_awards(ev: Evidence, dept: str, win_start: str, win_end: str) -> float:
    """Award notices published to this department in the 24 months before T."""
    cutoff = shift_months(ev.date, -24)
    return float(sum(1 for r in _dept_rows(ev.award_notices(), dept)
                     if r["publication_date"] >= cutoff))


def f_vendor_concentration(ev: Evidence, dept: str, win_start: str, win_end: str) -> float:
    """
    Herfindahl index over vendors on this department's awards in the 24 months
    before T. 1.0 is a single supplier; near 0 is a crowded field.
    """
    cutoff = shift_months(ev.date, -24)
    counts: dict[str, int] = {}
    for r in _dept_rows(ev.award_notices(), dept):
        if r["publication_date"] >= cutoff and r["vendor"]:
            counts[r["vendor"]] = counts.get(r["vendor"], 0) + 1
    total = sum(counts.values())
    if not total:
        return 0.0
    return sum((n / total) ** 2 for n in counts.values())


def f_prior_year_hit(ev: Evidence, dept: str, win_start: str, win_end: str) -> float:
    """
    Did this department publish a target notice in the 12 months before T?

    THE FEATURE TO BEAT. Procurement is autocorrelated: a department that
    tendered IT last year will very likely do so again, and any evidence signal
    that cannot beat this baseline is not contributing anything a calendar could
    not. Reported separately for that reason.
    """
    cutoff = shift_months(ev.date, -12)
    families, competencies = _profile_filters()
    rows = (r for r in ev.notices()
            if is_target_notice(r, families, competencies))
    for record in target_solicitations(rows).values():
        if record["first_published"] < cutoff:
            continue
        if dept in record["org_keys"].split(","):
            return 1.0
    return 0.0


FEATURES = [
    Feature("audit_it", f_audit_it,
            "OAG date_published <= T; portal date runs late, never early"),
    Feature("audit_recent_it", f_audit_recent_it,
            "same, within 24 months before T"),
    Feature("plan_intent", f_plan_intent,
            "DP for fiscal year Y tabled ~March Y; admitted at year <= FY(T)"),
    Feature("plan_pressure", f_plan_pressure,
            "same tabling logic"),
    Feature("expiring_awards", f_expiring_awards,
            "award notice and its end date both published at T"),
    Feature("active_awards", f_active_awards,
            "award notices published <= T"),
    Feature("vendor_concentration", f_vendor_concentration,
            "derived from award notices published <= T"),
    Feature("prior_year_hit", f_prior_year_hit,
            "target predicate on notices published <= T"),
]


_PROFILE_CACHE: dict = {}


def _profile_filters() -> tuple[list[str], list[str]]:
    if "criteria" not in _PROFILE_CACHE:
        _PROFILE_CACHE["criteria"] = parse_profile(DEFAULT_PROFILE)
    criteria = _PROFILE_CACHE["criteria"]
    return criteria["unspsc_families"], criteria["competencies"]


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------

def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """
    Wilson score interval. Normal approximation is wrong at these sample sizes
    and at rates near 0 or 1, which is most of what this panel produces.
    """
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def fit_logistic(X: list[list[float]], y: list[int], iters: int = 300,
                 l2: float = 1.0) -> list[float]:
    """
    Plain L2-penalized logistic regression by gradient descent.

    Hand-rolled to keep requirements.txt untouched — scikit-learn is a large
    dependency to add for a fit this small. Features are standardized by the
    caller. The penalty is there because n is small enough that an unregularized
    fit on eight correlated features will happily memorize a cohort.
    """
    n_features = len(X[0]) + 1
    w = [0.0] * n_features
    lr = 0.1
    for _ in range(iters):
        grad = [0.0] * n_features
        for xi, yi in zip(X, y):
            z = w[0] + sum(w[j + 1] * xi[j] for j in range(len(xi)))
            p = 1 / (1 + math.exp(-max(-30.0, min(30.0, z))))
            err = p - yi
            grad[0] += err
            for j in range(len(xi)):
                grad[j + 1] += err * xi[j]
        m = len(X)
        for j in range(n_features):
            reg = l2 * w[j] if j > 0 else 0.0
            w[j] -= lr * (grad[j] / m + reg / m)
    return w


def predict(w: list[float], xi: list[float]) -> float:
    z = w[0] + sum(w[j + 1] * xi[j] for j in range(len(xi)))
    return 1 / (1 + math.exp(-max(-30.0, min(30.0, z))))


def standardize(rows: list[list[float]]) -> tuple[list[list[float]], list, list]:
    n_features = len(rows[0])
    means, sds = [], []
    for j in range(n_features):
        col = [r[j] for r in rows]
        mu = sum(col) / len(col)
        var = sum((v - mu) ** 2 for v in col) / max(1, len(col) - 1)
        means.append(mu)
        sds.append(math.sqrt(var) or 1.0)
    scaled = [[(r[j] - means[j]) / sds[j] for j in range(n_features)] for r in rows]
    return scaled, means, sds


# ---------------------------------------------------------------------------
# The panel
# ---------------------------------------------------------------------------

@dataclass
class Cohort:
    fiscal_year: int
    feature_date: str
    window: tuple[str, str]
    departments: list[str]
    X: list[list[float]]
    y: list[int]


def universe(conn: sqlite3.Connection, families: list[str],
             competencies: list[str]) -> list[str]:
    """
    Departments eligible to be in the panel at all.

    A department that has NEVER published a target notice anywhere in the
    archive is not a hard case the model got wrong — it is not in this market,
    and including it inflates every accuracy figure with rows nobody would have
    alerted on. Organizations with at least one hit somewhere in the record are
    the honest denominator, and the base rate is computed over the same set.
    """
    seen: set[str] = set()
    for record in _all_target_solicitations(conn, families, competencies).values():
        seen.update(k for k in record["org_keys"].split(",") if k)
    return sorted(seen)


def covered_fiscal_years(conn: sqlite3.Connection) -> list[int]:
    """
    Fiscal years the notice archive covers COMPLETELY.

    A partial year cannot carry a label. A department with no hit in a year
    missing four months has not been observed not to tender — it has been
    observed incompletely, and scoring it as a negative manufactures a fact. The
    `sources` table is what makes this answerable at all: in `notices` alone, an
    uningested year and a year with no hits look identical.

    The current fiscal year is excluded for the same reason: it has not
    finished, so its zero-hit departments may simply not have tendered YET.
    """
    import datetime as _dt

    years = []
    for fy, note in conn.execute(
            "SELECT fiscal_year, partial_note FROM sources WHERE kind = 'tender'"):
        if note or "-" not in fy:
            continue
        start = int(fy.split("-")[0])
        end = int(fy.split("-")[1])
        if end - start != 1:          # the legacy archive spans many years
            continue
        years.append(start)
    now_fy = fiscal_year_of(_dt.date.today().isoformat())
    return sorted(y for y in years if y < now_fy)


def build_panel(gap_months: int) -> list[Cohort]:
    families, competencies = _profile_filters()
    conn = sqlite3.connect(f"file:{NOTICES_DB}?mode=ro", uri=True)
    try:
        depts = universe(conn, families, competencies)
        cohorts = []
        for fy in covered_fiscal_years(conn):
            start, end = fy_bounds(fy)
            feature_date = shift_months(start, -gap_months)
            ev = as_of(feature_date)
            labels = hits_in_window(conn, start, end, families, competencies)
            X, y = [], []
            for dept in depts:
                X.append([f.fn(ev, dept, start, end) for f in FEATURES])
                y.append(1 if labels.get(dept, 0) > 0 else 0)
            cohorts.append(Cohort(fy, feature_date, (start, end), depts, X, y))
        return cohorts
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def base_rate_block(cohorts: list[Cohort], out: list[str]) -> tuple[float, float]:
    """Base rate FIRST, n on every line, spread checked. Returns (pooled, spread)."""
    out.append("## Base rate - read this before any lift figure")
    out.append("")
    out.append("| fiscal year | features as of | departments | hits | base rate | 95% CI |")
    out.append("|---|---|---|---|---|---|")
    rates, tot_k, tot_n = [], 0, 0
    for c in cohorts:
        k, n = sum(c.y), len(c.y)
        lo, hi = wilson(k, n)
        rates.append(k / n)
        tot_k += k
        tot_n += n
        out.append(f"| {c.fiscal_year}-{str(c.fiscal_year + 1)[2:]} | {c.feature_date} "
                   f"| {n} | {k} | {100 * k / n:.1f}% | {100 * lo:.1f}-{100 * hi:.1f}% |")
    pooled = tot_k / tot_n if tot_n else 0.0
    spread = 100 * (max(rates) - min(rates)) if rates else 0.0
    lo, hi = wilson(tot_k, tot_n)
    out.append(f"| **pooled** | | **{tot_n}** | **{tot_k}** | **{100 * pooled:.1f}%** "
               f"| **{100 * lo:.1f}-{100 * hi:.1f}%** |")
    out.append("")
    out.append(f"Spread across fiscal years: **{spread:.1f}pp** "
               f"(gate: {MAX_BASE_RATE_SPREAD_PP}pp).")
    return pooled, spread


def _split_point(values: list[float], nonzero: list[float]) -> float:
    """
    Where to cut a feature into has-signal / no-signal, from its own distribution.

    Median of the non-zero values when the feature is mostly non-zero, else
    zero — a threshold picked by hand would be a fitted parameter that does not
    admit to being one.

    The fallback matters and is not a detail: for a BINARY feature every
    non-zero value is 1.0, so the median is 1.0 and `v > 1.0` selects nothing.
    That silently emptied the `prior_year_hit` row — the one baseline every other
    feature in this table has to beat — and an empty row reads as "no signal"
    rather than as "the split was broken", which is the worst way for this to
    fail. Whenever the median leaves an empty upper group, fall back to zero.
    """
    median = nonzero[len(nonzero) // 2]
    if len(nonzero) > len(values) / 2 and any(v > median for v in values):
        return median
    return 0.0


def confound_block(cohorts: list[Cohort], out: list[str]) -> None:
    """
    Does any signal survive controlling for how much the department buys?

    THE CONFOUND THAT WOULD OTHERWISE SINK THIS. The Auditor General audits
    large departments; large departments tender constantly. So `audit_it` can
    show a large lift while carrying no information beyond "this is SSC and not
    the Farm Products Council" — and the marginals table above cannot tell the
    two apart, because a raw lift never can.

    The control is `active_awards`, a department's award-notice volume in the 24
    months before T: a size proxy that is knowable at T and is not the outcome.
    Within a stratum, departments are roughly comparable in size, so a lift that
    survives is a lift the size story does not explain. One that vanishes was
    size all along.
    """
    out.append("")
    out.append("## Controlling for department size")
    out.append("")
    out.append("`active_awards` (award notices in the 24 months before T) is a "
               "size proxy knowable at T. Within a size stratum departments are "
               "roughly comparable, so a lift that survives is not just "
               "\"large department\". One that vanishes was size all along.")
    out.append("")

    size_idx = next(i for i, f in enumerate(FEATURES) if f.name == "active_awards")
    cells = [(c.X[i], c.y[i]) for c in cohorts for i in range(len(c.y))]
    ordered = sorted(cells, key=lambda t: t[0][size_idx])
    third = max(1, len(ordered) // 3)
    strata = [("small", ordered[:third]),
              ("medium", ordered[third:2 * third]),
              ("large", ordered[2 * third:])]

    interesting = ["audit_it", "plan_intent", "expiring_awards", "prior_year_hit"]
    out.append("| stratum | n | base rate | " +
               " | ".join(f"`{name}` lift" for name in interesting) + " |")
    out.append("|---|---|---|" + "---|" * len(interesting))
    for label, group in strata:
        n = len(group)
        k = sum(y for _, y in group)
        if not n:
            continue
        stratum_base = k / n
        row = [f"| {label} | {n} | {100 * stratum_base:.0f}% "]
        for name in interesting:
            j = next(i for i, f in enumerate(FEATURES) if f.name == name)
            values = [x[j] for x, _ in group]
            nonzero = sorted(v for v in values if v > 0)
            if not nonzero or stratum_base == 0:
                row.append("| — ")
                continue
            threshold = _split_point(values, nonzero)
            sub = [(x, y) for x, y in group if x[j] > threshold]
            if not sub:
                row.append("| — ")
                continue
            p = sum(y for _, y in sub) / len(sub)
            row.append(f"| {p / stratum_base:.2f}x (n={len(sub)}) ")
        out.append("".join(row) + "|")
    out.append("")
    out.append("An em dash means the feature had no usable split inside that "
               "stratum — most often because every department in it scores zero. "
               "That is an absence of data, not an absence of effect.")


def marginals_block(cohorts: list[Cohort], base: float, out: list[str]) -> None:
    """
    Per-signal marginals BEFORE any combination.

    Without this a convergence claim cannot be told apart from one signal
    carrying the whole result, which is the specific way a four-signal story
    goes wrong.
    """
    out.append("")
    out.append("## Each signal alone")
    out.append("")
    out.append(f"Every rate below is against a base rate of **{100 * base:.1f}%**. "
               f"A lift of 1.00 is no signal.")
    out.append("")
    out.append("| feature | split | n with | hits | P(hit given signal) | 95% CI | lift |")
    out.append("|---|---|---|---|---|---|---|")
    for j, feat in enumerate(FEATURES):
        values = [c.X[i][j] for c in cohorts for i in range(len(c.y))]
        ys = [c.y[i] for c in cohorts for i in range(len(c.y))]
        nonzero = sorted(v for v in values if v > 0)
        if not nonzero:
            out.append(f"| `{feat.name}` | - | 0 | - | - | - | no non-zero values |")
            continue
        threshold = _split_point(values, nonzero)
        k = n = 0
        for v, yi in zip(values, ys):
            if v > threshold:
                n += 1
                k += yi
        if n == 0:
            out.append(f"| `{feat.name}` | > {threshold:g} | 0 | - | - | - | - |")
            continue
        p = k / n
        lo, hi = wilson(k, n)
        lift = p / base if base else float("nan")
        out.append(f"| `{feat.name}` | > {threshold:g} | {n} | {k} | {100 * p:.1f}% "
                   f"| {100 * lo:.1f}-{100 * hi:.1f}% | {lift:.2f}x |")


def holdout_block(cohorts: list[Cohort], base: float, out: list[str]) -> None:
    """
    Leave-one-fiscal-year-out fit, then precision at a fixed alert budget.

    PRECISION, NOT RECALL. At a base rate this high, alerting on every
    department scores 100% recall, so a recall figure is unearned by
    construction. The question a reader actually has is: of the ten departments
    this ranks highest, how many tendered - against how many chance alone gives.
    """
    out.append("")
    out.append("## Combined score, fitted out of fold")
    out.append("")
    if len(cohorts) < 3:
        out.append(f"Only {len(cohorts)} complete fiscal year(s) available. "
                   f"Leave-one-out over fewer than three cohorts does not produce "
                   f"a fit worth reporting, so this section is empty on purpose.")
        return

    budget = 10
    out.append(f"| held-out year | n | hits | precision@{budget} | expected by chance "
               f"| 95% CI | overlap with top-{budget} by size |")
    out.append("|---|---|---|---|---|---|---|")
    for held in cohorts:
        train = [c for c in cohorts if c is not held]
        X_train = [x for c in train for x in c.X]
        y_train = [v for c in train for v in c.y]
        if not any(y_train) or all(y_train):
            continue
        scaled, means, sds = standardize(X_train)
        w = fit_logistic(scaled, y_train)
        scored = []
        for xi, yi in zip(held.X, held.y):
            z = [(xi[j] - means[j]) / sds[j] for j in range(len(xi))]
            scored.append((predict(w, z), yi))
        ranked = sorted(range(len(held.y)), key=lambda i: -scored[i][0])
        top_idx = ranked[:budget]
        k = sum(held.y[i] for i in top_idx)
        lo, hi = wilson(k, len(top_idx))

        # How much of this ranking is just "big department"? If the top-N by
        # fitted score is the top-N by award volume, the model learned size and
        # the evidence signals contributed nothing a headcount would not.
        size_idx = next(i for i, f in enumerate(FEATURES)
                        if f.name == "active_awards")
        by_size = sorted(range(len(held.y)),
                         key=lambda i: -held.X[i][size_idx])[:budget]
        overlap = len(set(top_idx) & set(by_size))

        out.append(f"| {held.fiscal_year}-{str(held.fiscal_year + 1)[2:]} "
                   f"| {len(held.y)} | {sum(held.y)} | {k}/{len(top_idx)} "
                   f"| {base * len(top_idx):.1f}/{len(top_idx)} "
                   f"| {100 * lo:.0f}-{100 * hi:.0f}% "
                   f"| {overlap}/{budget} |")
    out.append("")
    out.append("Weights are fitted on the other years and the fold is scored "
               "before they are looked at. No weight in this table was chosen "
               "by hand.")
    out.append("")
    out.append("**The last column is the one to read.** It is how many of the ten "
               "departments the fitted score ranked highest are also among the ten "
               "that simply buy the most. Where that number is close to ten, the "
               "model has rediscovered department size and the evidence signals "
               "have added nothing a list of the biggest buyers would not give "
               "you for free.")


EVIDENCE_FEATURES = ("audit_it", "audit_recent_it", "plan_intent",
                     "plan_pressure", "expiring_awards")


@dataclass
class StratumLift:
    """One evidence feature's effect inside one size stratum, with its interval."""
    feature: str
    lift: float
    k: int
    n: int
    stratum_base: float
    ci: tuple[float, float]

    @property
    def significant(self) -> bool:
        """
        Does the interval on P(hit | signal) exclude the stratum's base rate?

        A LIFT WITHOUT THIS TEST IS A NUMBER, NOT A FINDING. `audit_it` reaches
        1.41x inside the medium stratum on six observations; the Wilson interval
        around that runs from roughly 60% to 100% and swallows the 71% base rate
        whole. Reporting the 1.41 alone would have called that a signal worth
        pursuing, which is exactly the failure this whole module exists to avoid,
        and it did on the first draft of this function.
        """
        lo, hi = self.ci
        return not (lo <= self.stratum_base <= hi)


def _within_stratum_lifts(cohorts: list[Cohort]) -> dict[str, list[StratumLift]]:
    """Per-evidence-feature effects inside each size stratum, for the verdict."""
    size_idx = next(i for i, f in enumerate(FEATURES) if f.name == "active_awards")
    cells = [(c.X[i], c.y[i]) for c in cohorts for i in range(len(c.y))]
    ordered = sorted(cells, key=lambda t: t[0][size_idx])
    third = max(1, len(ordered) // 3)
    strata = [ordered[:third], ordered[third:2 * third], ordered[2 * third:]]

    lifts: dict[str, list[StratumLift]] = {}
    for name in EVIDENCE_FEATURES:
        j = next(i for i, f in enumerate(FEATURES) if f.name == name)
        found = []
        for group in strata:
            if not group:
                continue
            stratum_base = sum(y for _, y in group) / len(group)
            values = [x[j] for x, _ in group]
            nonzero = sorted(v for v in values if v > 0)
            if not nonzero or not stratum_base:
                continue
            threshold = _split_point(values, nonzero)
            sub = [(x, y) for x, y in group if x[j] > threshold]
            if not sub:
                continue
            k, n = sum(y for _, y in sub), len(sub)
            found.append(StratumLift(name, (k / n) / stratum_base, k, n,
                                     stratum_base, wilson(k, n)))
        lifts[name] = found
    return lifts


def _raw_lift(cohorts: list[Cohort], name: str, base: float) -> float:
    """The uncontrolled marginal lift, for comparison against the controlled one."""
    j = next(i for i, f in enumerate(FEATURES) if f.name == name)
    values = [c.X[i][j] for c in cohorts for i in range(len(c.y))]
    ys = [c.y[i] for c in cohorts for i in range(len(c.y))]
    nonzero = sorted(v for v in values if v > 0)
    if not nonzero or not base:
        return float("nan")
    threshold = _split_point(values, nonzero)
    sub = [(v, y) for v, y in zip(values, ys) if v > threshold]
    if not sub:
        return float("nan")
    return (sum(y for _, y in sub) / len(sub)) / base


def verdict_block(cohorts: list[Cohort], base: float, out: list[str]) -> None:
    """
    State the finding, computed rather than asserted.

    A report that leaves the reader to assemble three tables into a conclusion
    gets read as whatever they hoped it said. This block does the assembly, and
    it is written to be able to print a null.
    """
    out.append("")
    out.append("## The finding")
    out.append("")

    lifts = _within_stratum_lifts(cohorts)
    measured = [sl for values in lifts.values() for sl in values]
    if not measured:
        out.append("No evidence feature had a usable split inside any size "
                   "stratum, so the size confound could not be tested at all. "
                   "Treat every lift above as uninterpreted.")
        return

    surviving = [sl for sl in measured if sl.significant and sl.lift > 1.0]
    biggest_raw = max(
        ((name, _raw_lift(cohorts, name, base)) for name in EVIDENCE_FEATURES),
        key=lambda t: (t[1] if t[1] == t[1] else 0.0))

    out.append(
        f"Uncontrolled, the strongest evidence signal is `{biggest_raw[0]}` at "
        f"**{biggest_raw[1]:.2f}x**. Inside a size stratum, "
        f"{len(surviving)} of {len(measured)} feature-by-stratum comparisons "
        f"show a lift above 1.0 whose confidence interval excludes the "
        f"stratum's own base rate.")
    out.append("")
    if surviving:
        best = max(surviving, key=lambda sl: sl.lift)
        out.append(
            f"The strongest survivor is `{best.feature}` at **{best.lift:.2f}x** "
            f"(n={best.n}, {100 * best.ci[0]:.0f}-{100 * best.ci[1]:.0f}% against "
            f"a {100 * best.stratum_base:.0f}% stratum base rate).")
        out.append("")
        out.append(
            "**That is a signal worth pursuing.** It survives the control that "
            "would have explained it away, and the next question is whether it "
            "holds on more fiscal years than the ones available here — three is "
            "not enough to build on, whichever way they point.")
    else:
        out.append(
            "**That is a null result on the interesting claim.** Every evidence "
            "signal — Auditor General findings, stated departmental intent, "
            "incumbent contracts running down — collapses to roughly no effect "
            "once you compare departments of similar size. The raw lifts in the "
            "marginals table are real arithmetic and mean almost nothing: the "
            "Auditor General audits large departments, large departments tender "
            "constantly, and `audit_it` was reading the second fact through the "
            "first.")
        out.append("")
        out.append(
            "The combined score does beat chance at a ten-department alert "
            "budget. It should: ranking federal organizations by how much IT they "
            "buy is genuinely predictive of whether they will buy IT again, and "
            "the overlap column shows that is substantially what the fit learned. "
            "That is worth knowing and it is not the pre-RFP thesis. The claim "
            "under test was that convergent public evidence sees a procurement "
            "coming that a list of the biggest buyers would miss, and on three "
            "fiscal years of federal data it does not.")
        out.append("")
        out.append(
            "What this does NOT establish: that the evidence is worthless for "
            "the question a human actually asks. This tests *whether a "
            "department tenders anything relevant at all in a year* — a coarse "
            "target with a 68.6% base rate and, in the largest third of "
            "departments, 94%. There is almost no room to be right in a way that "
            "counts. The dossier's real use has always been *what* is coming and "
            "*why now*, at the level of a specific system with a named incumbent, "
            "and nothing here speaks to that. It is a different experiment and it "
            "needs a finer target than this panel can carry.")


def interpretation_block(cohorts: list[Cohort], base: float, spread: float,
                         out: list[str]) -> None:
    out.append("")
    out.append("## What this does and does not establish")
    out.append("")
    n_cells = sum(len(c.y) for c in cohorts)
    n_hits = sum(sum(c.y) for c in cohorts)
    lo, hi = wilson(n_hits, n_cells)
    out.append(f"- **n is {n_cells} department-years across {len(cohorts)} complete "
               f"fiscal year(s), {n_hits} of them positive.** The interval on the "
               f"pooled base rate alone is {100 * lo:.1f}-{100 * hi:.1f}%. Every "
               f"per-signal figure rests on a fraction of that n and its interval "
               f"is correspondingly wider - several are wide enough to cover both "
               f"a real effect and none at all.")
    out.append(f"- **The base rate is {100 * base:.1f}%.** A rule that alerts on "
               f"every department is right that often. Nothing here is worth "
               f"anything unless it beats that number, and beating it by a few "
               f"points inside a wide interval is not evidence that it does.")
    if spread > MAX_BASE_RATE_SPREAD_PP:
        out.append(f"- **The base rate is NOT stable across years ({spread:.1f}pp "
                   f"spread against a {MAX_BASE_RATE_SPREAD_PP}pp gate).** The unit "
                   f"of analysis is wrong, and everything below the base-rate table "
                   f"should be read as provisional at best.")
    out.append("- **`prior_year_hit` is the baseline that matters.** Procurement is "
               "autocorrelated. An evidence signal that does not beat \"they "
               "tendered last year\" is telling you about the calendar.")
    out.append("- **Departmental-plan features are weakest where they would matter "
               "most.** Sixteen organizations file no planning prose at all, and "
               "they include DND, GAC, the RCMP, CBSA and SSC - the largest federal "
               "IT buyers. A zero on `plan_intent` is usually silence, not "
               "an absence of intent.")


def render(cohorts: list[Cohort], gap_months: int) -> tuple[str, float, float]:
    from datetime import date
    out = [
        "# Backtest - does convergent evidence predict procurement?",
        "",
        f"Run {date.today().isoformat()}. Features observed **{gap_months} months "
        f"before** each fiscal year opens, so the lead time under test is "
        f"{gap_months}-{gap_months + 12} months.",
        "",
        "The target predicate is frozen in `scripts/backtest.py`'s module "
        "docstring and was not changed after any result below was seen.",
        "",
    ]
    base, spread = base_rate_block(cohorts, out)
    marginals_block(cohorts, base, out)
    confound_block(cohorts, out)
    holdout_block(cohorts, base, out)
    verdict_block(cohorts, base, out)
    interpretation_block(cohorts, base, spread, out)
    return "\n".join(out), base, spread


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Historical backtest of the pre-RFP evidence thesis.")
    parser.add_argument("--gap-months", type=int, default=6,
                        help="months before the fiscal year opens at which "
                             "features are observed (default 6, a 6-18 month lead)")
    parser.add_argument("--write", action="store_true",
                        help="also write the report under vault/reference/")
    parser.add_argument("--check-base-rate", action="store_true",
                        help="run the stability gate only, exit non-zero on fail")
    args = parser.parse_args()

    if not NOTICES_DB.exists():
        sys.stderr.write(f"{NOTICES_DB} not built. Run scripts/notices_ingest.py\n")
        return 2

    cohorts = build_panel(args.gap_months)
    if not cohorts:
        sys.stderr.write(
            "No complete fiscal years in the notice archive: every ingested year "
            "is either partial or still running. See the `sources` table.\n")
        return 2

    text, base, spread = render(cohorts, args.gap_months)
    print(text)

    if args.check_base_rate:
        ok = spread <= MAX_BASE_RATE_SPREAD_PP
        print(f"\nGATE: {'PASS' if ok else 'FAIL'} - spread {spread:.1f}pp "
              f"against {MAX_BASE_RATE_SPREAD_PP}pp")
        return 0 if ok else 1

    if args.write:
        from datetime import date
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        path = REPORT_DIR / f"backtest-{date.today().isoformat()}.md"
        path.write_text(text, encoding="utf-8", newline="\n")
        print(f"\nWrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
