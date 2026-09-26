"""
The admission predicates of scripts/ingest.py, one per stage, independently evaluable.

------------------------------------------------------------------------------
THE STAGE LIST - FROZEN
------------------------------------------------------------------------------
A stage edited after seeing a replay result is a stage fitted to it. Changing
any stage bumps `predicates_sha256` (see version.py); every audit record already
written keeps its old version label and is never rewritten.

Seven stages. Five of them drop rows today; the other two are modelled anyway,
because a gate that is invisible is a gate nobody audits.

  1 closed         closing date is in the past relative to `as_of`.
                   NaT is NOT closed and is deliberately KEPT - see ingest.py's
                   comment; undated notices are tagged unknown, not dropped.
  2 exclusion      a profile `exclude` term appears in title+description.
                   BARE SUBSTRING, case-insensitive - deliberately unlike
                   stage 5's keyword matching, which is word-boundary. Recorded
                   as match_kind='substring' so the asymmetry is visible.
  3 construction   classify_notice(...) == 'construction', which happens only
                   when procurementCategory is EXACTLY {*CNST}. A mapped notice
                   type outranks the category, so a *CNST notice typed
                   'Invitation to Qualify' is a qualification, not construction.
  4 jurisdiction   classify_jurisdiction(...) == 'non_federal'. ONLY that value
                   drops. `unrecognised` is deliberately admitted - CDIC, BDC,
                   Canada Post and Service Canada land there and they are
                   federal. Dropping on it would drop real work.
  5 relevance      (has_codes AND family match) OR (no codes AND keyword match).
                   Mutually exclusive branches, NOT an OR across everything.
                   See stage_relevance for why, and for what it refuses to
                   collapse into one boolean.
  6 value          INACTIVE. Dead twice over: it needs --extract-values AND a
                   non-default range, and the profile ships value_min/value_max
                   commented out. Modelled so the funnel can say `inactive`
                   rather than silently omitting a stage that exists in code.
  7 identifiable   blank or 'nan' reference number. Enforced in ingest.py's
                   _write_chroma, NOT in filter_tenders, and it appears in no
                   production funnel line. Carried here with
                   enforced_in='_write_chroma' so the equivalence test knows to
                   subtract it rather than reporting a phantom disagreement.

`outcome` is one of pass / drop / skipped / inactive. SKIPPED IS NOT PASSED. A
reader must be able to tell "we did not ask" from "we asked and it passed";
collapsing them is how an audit starts overstating what it checked.

------------------------------------------------------------------------------
WHAT THIS MODULE MUST NOT DO
------------------------------------------------------------------------------
Nothing here reads a review, a golden-set entry, or filter_audit.variants. A
review is a note about the past; it is not an input to a decision. Keeping the
import absent is the enforcement - a decision function physically cannot reach
a label.

Every predicate delegates to the existing functions in ingest.py rather than
reimplementing them, for the reason notices_ingest.py already states about its
own derived columns: the same classifiers, so the two cannot drift into
disagreeing.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Optional

import pandas as pd

import ingest

# The stage vocabulary, closed on purpose. A typo in an outcome string would
# otherwise read as a new state nobody handles.
OUTCOMES = ("pass", "drop", "skipped", "inactive")
FAMILY_RESULTS = ("matched", "wrong_family", "no_codes_filed")
KEYWORD_RESULTS = ("matched", "no_hit")


# ---------------------------------------------------------------------------
# The notice, in one shape
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Notice:
    """
    One notice, normalized, from whichever source it came from.

    The `cols` mapping the CSV feed needs is resolved ONCE at the adapter
    boundary, so no predicate downstream ever sees two column vocabularies.
    That mismatch - vectorized pandas over a resolved-name feed versus sqlite
    rows with fixed names - is the whole reason this class exists.
    """
    notice_id: str
    title: str
    description: str
    contracting_entity: Any
    end_user: Any
    notice_type: Any
    procurement_category: Any
    unspsc: Any
    gsin: Any
    publication_date: Optional[str]
    raw_closing_date: Any
    closing_day: Any            # pre-parsed Timestamp or NaT - see parse_closing_days
    source: str                 # 'archive' | 'feed' | 'frozen'

    @property
    def text(self) -> str:
        """title + ' ' + description. ONE definition, matching filter_tenders."""
        return f"{self.title} {self.description}"

    @staticmethod
    def from_archive_row(row, closing_day=None) -> "Notice":
        """A `notices` row from data/notices.db (sqlite3.Row or dict)."""
        get = _row_getter(row)
        raw_closing = get("closing_date")
        return Notice(
            notice_id=_s(get("reference_number")),
            title=_s(get("title")),
            description=_s(get("description")),
            contracting_entity=get("contracting_entity"),
            end_user=get("end_user"),
            notice_type=get("notice_type"),
            procurement_category=get("procurement_category"),
            unspsc=get("unspsc"),
            gsin=get("gsin"),
            publication_date=_s(get("publication_date")) or None,
            raw_closing_date=raw_closing,
            closing_day=_one_closing_day(raw_closing, closing_day),
            source="archive",
        )

    @staticmethod
    def from_feed_row(row, cols: dict, closing_day=None) -> "Notice":
        """A row of the CanadaBuys open-notice feed, through resolve_columns."""
        def col(key):
            name = cols.get(key)
            return row.get(name) if name else None

        raw_closing = col("closing_date")
        return Notice(
            notice_id=_s(col("tender_id")),
            title=_s(col("title")),
            description=_s(col("description")),
            contracting_entity=col("contracting_entity"),
            end_user=col("end_user"),
            notice_type=col("notice_type"),
            procurement_category=col("procurement_category"),
            unspsc=col("unspsc"),
            gsin=col("gsin"),
            publication_date=None,      # the open feed carries no publication date
            raw_closing_date=raw_closing,
            closing_day=_one_closing_day(raw_closing, closing_day),
            source="feed",
        )

    @staticmethod
    def from_frozen_row(row: dict) -> "Notice":
        """A row frozen into the golden set, which outlives both sources."""
        raw_closing = row.get("closing_date")
        return Notice(
            notice_id=_s(row.get("reference_number")),
            title=_s(row.get("title")),
            description=_s(row.get("description")),
            contracting_entity=row.get("contracting_entity"),
            end_user=row.get("end_user"),
            notice_type=row.get("notice_type"),
            procurement_category=row.get("procurement_category"),
            unspsc=row.get("unspsc"),
            gsin=row.get("gsin"),
            publication_date=row.get("publication_date"),
            raw_closing_date=raw_closing,
            closing_day=_one_closing_day(raw_closing, None),
            source="frozen",
        )


def _row_getter(row):
    """sqlite3.Row indexes but has no .get; a dict has both. Normalize."""
    if isinstance(row, dict):
        return row.get
    keys = set(row.keys())
    return lambda k: row[k] if k in keys else None


def _s(value) -> str:
    """NaN-safe string. float('nan') is truthy, so `str(x) or ''` yields 'nan'."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    return str(value)


def _one_closing_day(raw, provided):
    """Batch-parsed value when the caller has one, else a batch of one."""
    if provided is not None:
        return provided
    return parse_closing_days(pd.Series([raw])).iloc[0]


def parse_closing_days(series: pd.Series) -> pd.Series:
    """
    THE closing-date parse, shared with filter_tenders so it cannot drift.

    Vectorized, and that is not an implementation detail. pandas infers a single
    format from the first non-null element and coerces anything shaped
    differently to NaT, so a row's result depends on the batch it was parsed in.
    Both live corpora are format-uniform today (notices.db is all YYYY-MM-DD,
    the feed all YYYY-MM-DDTHH:MM:SS), so this bites nothing - but a scalar
    "twin" would silently disagree with production the day that stops being
    true, which is why there isn't one. `closing_date_shapes` reports the
    uniformity so the assumption is checked rather than trusted.

    DELIBERATELY NOT NORMALIZED. filter_tenders keeps the time component on
    `_closing` and downstream readers use it - closing_window() and the
    date-conflict report both do - so stripping it here would quietly change
    what the corpus stores. Day-granularity belongs to the closed comparison
    alone, and stage_closed does it there by normalizing the CUTOFF instead,
    which is equivalent: `closing < midnight(as_of)` is exactly
    `date(closing) < as_of`.
    """
    return (pd.to_datetime(series, errors="coerce", utc=True)
            .dt.tz_localize(None))


def notices_from_frame(df, cols: dict, closing_col: str = "_closing"):
    """
    One Notice per DataFrame row, reusing an already-parsed closing column.

    This is what lets filter_tenders route its stage decisions through the same
    functions the audit uses. It rebuilds a Notice per stage rather than caching
    one per row, which costs nothing at the ~900 rows a production run sees and
    keeps the narrowing sequence — and therefore every funnel count — untouched.
    """
    days = df[closing_col] if closing_col in df.columns else [None] * len(df)
    for (_, row), day in zip(df.iterrows(), days):
        yield Notice.from_feed_row(row, cols, closing_day=day)


def stage_mask(df, cols: dict, criteria: dict, stage_fn, as_of=None,
               closing_col: str = "_closing", imputations: Optional[dict] = None):
    """
    A keep-mask for one stage, aligned to the frame's index.

    True means the row survives. Returned as a plain list so the caller applies
    it exactly as it applied its old inline expression.

    `imputations` (notice_id -> Imputation) is passed through to the relevance
    stage only. A notice absent from it is decided as if no imputer existed.
    """
    if imputations is None:
        return [not stage_fn(n, criteria, as_of).drops
                for n in notices_from_frame(df, cols, closing_col)]
    return [not stage_fn(n, criteria, as_of,
                         imputation=imputations.get(n.notice_id)).drops
            for n in notices_from_frame(df, cols, closing_col)]


def _evaluate(stage, notice, criteria, as_of, imputation):
    if stage.name == "relevance":
        return stage.evaluate(notice, criteria, as_of, imputation=imputation)
    return stage.evaluate(notice, criteria, as_of)


def closing_date_shapes(values) -> dict:
    """Digit-masked shapes of a closing-date column, for the uniformity check."""
    out: dict = {}
    for value in values:
        shape = "NULL" if value is None else re.sub(r"\d", "9", str(value))
        out[shape] = out.get(shape, 0) + 1
    return out


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StageResult:
    stage: str
    order: int
    outcome: str                       # pass | drop | skipped | inactive
    basis: str                         # which field decided it
    evidence: dict = field(default_factory=dict)
    detail: dict = field(default_factory=dict)

    @property
    def drops(self) -> bool:
        return self.outcome == "drop"

    def to_dict(self) -> dict:
        return {"stage": self.stage, "order": self.order, "outcome": self.outcome,
                "basis": self.basis, "evidence": self.evidence, "detail": self.detail}


@dataclass(frozen=True)
class Stage:
    order: int
    name: str
    evaluate: Any
    drops: bool                        # can this stage reject at all
    active: bool                       # is it live in production today
    enforced_in: str
    why: str


@dataclass(frozen=True)
class ProductionDecision:
    """Short-circuit. Exactly what ingest.py does, and no more."""
    notice_id: str
    admitted: bool
    first_rejecting_stage: Optional[str]
    evaluated: tuple                   # only the stages production actually reached

    def to_dict(self) -> dict:
        return {"notice_id": self.notice_id, "admitted": self.admitted,
                "first_rejecting_stage": self.first_rejecting_stage,
                "evaluated": [r.to_dict() for r in self.evaluated]}


@dataclass(frozen=True)
class AuditDecision:
    """Every stage, evaluated against the original notice. Never short-circuits."""
    notice_id: str
    results: tuple                     # one StageResult per stage, always all seven
    admitted: bool                     # would pass every ACTIVE dropping stage

    def by_stage(self, name: str) -> StageResult:
        for result in self.results:
            if result.stage == name:
                return result
        raise KeyError(name)

    def to_dict(self) -> dict:
        return {"notice_id": self.notice_id, "admitted": self.admitted,
                "results": [r.to_dict() for r in self.results]}


# ---------------------------------------------------------------------------
# The stages
# ---------------------------------------------------------------------------

def stage_closed(notice: Notice, criteria: dict, as_of) -> StageResult:
    """
    Dead notices only. Everything still open enters and is tagged.

    `as_of=None` means the caller declined to fix a clock, and the honest record
    of that is `skipped` - not `pass`. Production's own clock is
    datetime.now().date(), which is why replay makes it an argument: a predicate
    that reads a wall clock cannot be replayed.
    """
    if as_of is None:
        return StageResult("closed", 1, "skipped", "no as_of supplied",
                           detail={"reason": "caller declined to fix a clock"})
    closing = notice.closing_day
    if closing is None or pd.isna(closing):
        # NaT is not "closed". The old expression dropped undated notices as a
        # side effect of the arithmetic rather than as a decision.
        return StageResult("closed", 1, "pass", "closing_date",
                           detail={"undated": True, "as_of": str(as_of)})
    cutoff = pd.Timestamp(as_of).normalize()
    closed = closing < cutoff
    return StageResult(
        "closed", 1, "drop" if closed else "pass", "closing_date",
        evidence={"closing_day": str(closing.date()), "as_of": str(as_of)},
        detail={"undated": False, "days_from_as_of": int((closing - cutoff).days)},
    )


def stage_exclusion(notice: Notice, criteria: dict, as_of) -> StageResult:
    """
    Profile `exclude` terms. BARE SUBSTRING, unlike relevance's word boundaries.

    The asymmetry is real and is recorded rather than smoothed over: `catering`
    here would match inside `catering` in a longer word, where a competency
    would not. Kept faithful to contains_excluded; the audit's job is to report
    the filter that exists.
    """
    terms = criteria.get("exclude") or []
    if not terms:
        return StageResult("exclusion", 2, "pass", "no exclude terms configured")
    lowered = notice.text.lower()
    hits = [t for t in terms if t in lowered]
    return StageResult(
        "exclusion", 2, "drop" if hits else "pass", "title+description",
        evidence={"matched_terms": hits, "match_kind": "substring",
                  "windows": [_window(lowered, t) for t in hits[:3]]},
        detail={"configured_terms": len(terms)},
    )


def stage_construction(notice: Notice, criteria: dict, as_of) -> StageResult:
    """
    The publisher's own category, never a keyword. Recomputed, never read from
    a stored column - notices.db is append-only and its stored opportunity_kind
    was written by whichever classifier ran on first insert.
    """
    kind = ingest.classify_notice(notice.notice_type, notice.procurement_category,
                                  notice.text)
    is_construction = kind["opportunity_kind"] == "construction"
    return StageResult(
        "construction", 3, "drop" if is_construction else "pass",
        kind.get("kind_basis", "procurement_category"),
        evidence={"opportunity_kind": kind["opportunity_kind"],
                  "procurement_category": _s(notice.procurement_category),
                  "notice_type": _s(notice.notice_type)},
        detail={"kind_basis": kind.get("kind_basis"),
                "notice_type_outranks_category": kind.get("kind_basis") == "notice_type"},
    )


@lru_cache(maxsize=8192)
def _jurisdiction_cached(contracting: str, end_user: str) -> tuple:
    """
    Memoized classify_jurisdiction. 1,094 distinct entity pairs across 30,527
    archive rows, so the cache hit rate is ~96%: 2.7s of org resolution becomes
    ~0.1s over a full replay. Keyed on the stringified pair because the resolver
    reads nothing else.

    NOT in ingest.py: production sees ~900 rows a run and would gain nothing,
    while a process-lifetime cache there would add an invalidation question to
    the test suite for no benefit.
    """
    result = ingest.classify_jurisdiction(contracting, end_user)
    return tuple(sorted(result.items()))


def stage_jurisdiction(notice: Notice, criteria: dict, as_of) -> StageResult:
    """
    ONLY `non_federal` drops. `unrecognised` is the honest third answer and is
    admitted on purpose - federal Crown corporations land there.
    """
    verdict = dict(_jurisdiction_cached(_s(notice.contracting_entity),
                                        _s(notice.end_user)))
    jurisdiction = verdict["jurisdiction"]
    return StageResult(
        "jurisdiction", 4, "drop" if jurisdiction == "non_federal" else "pass",
        verdict.get("jurisdiction_basis", ""),
        evidence={"jurisdiction": jurisdiction,
                  "contracting_entity": _s(notice.contracting_entity),
                  "end_user": _s(notice.end_user),
                  "org_keys": verdict.get("org_keys", "")},
        detail={"note": verdict.get("jurisdiction_note", ""),
                "unrecognised_is_not_non_federal": jurisdiction == "unrecognised"},
    )


# ---------------------------------------------------------------------------
# The imputed relevance gate (ref-006)
# ---------------------------------------------------------------------------
#
# Uncoded notices are admitted when an imputer's summed probability across the
# profile's families reaches IMPUTER_THRESHOLD. The imputation arrives as DATA:
# this module imports nothing from scripts/family_imputer and makes no call,
# so replay stays deterministic and a missing imputation is simply the keyword
# branch, recorded as such.
#
# t = 0.20 was chosen on corpus size, not recall - see ref-006. Summed mass,
# not top choice and not top-2: top-2 admits a profile option sitting second at
# 0.02 and rejects two profile options at 0.20 each behind a 0.25 non-profile
# option. Mass handles both.
#
# THE MASS NEVER LEAVES THE AUDIT SIDE. It is recorded in StageResult.evidence
# for the filter audit; ingest writes only the basis, family and model to the
# corpus. Anything Claude reads must never see a probability - see ref-006.
IMPUTER_THRESHOLD = 0.20


def mass_reaches(mass: float, threshold: float) -> bool:
    """
    Summed profile mass compared at Jev's own resolution, 0.01.

    A DEFECT, NOT A TUNING CHOICE (recorded in ref-006). Jev quantises each
    probability to 0.01, and the mass is a float sum of those, so a sum that is
    exactly a threshold can land one ulp under it: 0.08 + 0.09 + 0.03 is
    0.19999999999999998 and was rejected at t = 0.20. Measured over the 23,314
    coded archive notices in the ref-004 cache on 2026-09-25: 1 row sat on the
    wrong side at 0.20 (cb-97-2152788) and 7 at 0.90, every one of them
    0.8999999999999999. Silent - no error, just a notice decided the other way.
    Rounding to 2 places loses nothing, because no input carries finer detail.
    """
    return round(mass, 2) >= threshold


@dataclass(frozen=True)
class Imputation:
    family: str             # the highest-probability profile option's prefix
    mass: float             # summed probability across the profile options
    model: str              # the versioned model that answered
    question_sha256: str
    content_sha256: str = ""  # the gate cache key: hash of the exact state sent


def stage_relevance(notice: Notice, criteria: dict, as_of,
                    imputation: Optional[Imputation] = None) -> StageResult:
    """
    Publisher first; for uncoded notices, the imputer where one answered and
    keywords where none did.

    `relevance_basis` in the detail says which decided: `unspsc`, `imputed` or
    `keyword`. An imputation handed in for a CODED notice is ignored and
    flagged - codes are the publisher's answer and nothing overrides them.

    NOT COLLAPSED TO ONE BOOLEAN, and that is the single most important thing in
    this module. "Coded into a family we don't buy" and "uncoded and no keyword
    fired" are different failures implying different fixes, and on the archive
    they split 21,471 / 6,121. A record that says only `relevance=false` throws
    away the primary signal of the whole exercise.

    The branches are mutually exclusive on purpose. The OR form readmitted a
    boiling-liquid-expanding-vapour-explosion study because "vapour cloud"
    contains "cloud". Codes present and not ours means not ours. The audit still
    RUNS the keyword branch on coded notices so the counterfactual is
    measurable, and records `keyword_consulted_in_production=False` to say
    production never looked - the relevance-level analogue of
    `first_rejecting_stage`.
    """
    families = criteria.get("unspsc_families") or []
    competencies = criteria.get("competencies") or []
    codes = ingest.parse_unspsc_codes(notice.unspsc)
    has_codes = bool(codes)
    matched_families = ingest.matches_unspsc_families(codes, families)
    matched_keywords = ingest.matched_competencies(notice.text, competencies)

    if not families and not competencies:
        # ingest.py skips the whole gate when the profile configures neither.
        return StageResult("relevance", 5, "pass", "no families or competencies configured",
                           detail={"gate_configured": False})

    imputed_evidence = {}
    if has_codes:
        family_result = "matched" if matched_families else "wrong_family"
        keyword_result = "matched" if matched_keywords else "no_hit"
        relevant = bool(matched_families)
        branch = "coded"
        relevance_basis = "unspsc"
        basis = "unspsc"
    elif imputation is not None:
        family_result = "no_codes_filed"
        keyword_result = "matched" if matched_keywords else "no_hit"
        relevant = mass_reaches(imputation.mass, IMPUTER_THRESHOLD)
        branch = "uncoded"
        relevance_basis = "imputed"
        basis = "imputed family mass"
        imputed_evidence = {"imputed_family": imputation.family,
                            "imputed_mass": imputation.mass,
                            "imputer_model": imputation.model,
                            "imputer_threshold": IMPUTER_THRESHOLD}
    else:
        family_result = "no_codes_filed"
        keyword_result = "matched" if matched_keywords else "no_hit"
        relevant = bool(matched_keywords)
        branch = "uncoded"
        relevance_basis = "keyword"
        basis = "title+description"

    return StageResult(
        "relevance", 5, "pass" if relevant else "drop",
        basis,
        evidence={"codes": sorted(codes), "matched_families": matched_families,
                  "matched_keywords": matched_keywords,
                  "source_system": ingest._source_system(notice.notice_id),
                  **imputed_evidence},
        detail={
            "relevance_basis": relevance_basis,
            "imputation_ignored_on_coded": has_codes and imputation is not None,
            "branch": branch,
            "has_codes": has_codes,
            "expected_families": list(families),
            "family_result": family_result,
            "keyword_match": bool(matched_keywords),
            "keyword_result": keyword_result,
            # False on every coded notice: the audit looked, production did not.
            # False on coded notices, and on uncoded ones the imputer decided.
            "keyword_consulted_in_production": relevance_basis == "keyword",
            "gate_configured": True,
        },
    )


# ---------------------------------------------------------------------------
# Flags on coded notices (ref-007) - FLAG ONLY, never an admission
# ---------------------------------------------------------------------------
#
# A coded notice whose filed codes reject, and which the imputer places in the
# profile's families with summed mass >= FLAG_THRESHOLD, is recorded as a
# flag. It is NOT admitted: stage_relevance ignores any imputation on a coded
# notice, and this function is not a stage and is never consulted by one.
#
# THE MASS STOPS HERE. CodedFlag carries a band, not the mass, and has no
# admit field, so nothing downstream of this function can hold the number or
# mistake a flag for a decision. The band is what the committed store keeps.
#
# 0.90 was chosen on the same 23,314 archive notices the ref-004 sweep was
# fitted on; ref-007 requires a split-half check before any promotion.
FLAG_THRESHOLD = 0.90
MASS_BANDS = ((0.99, "0.99+"), (0.95, "0.95-0.99"), (FLAG_THRESHOLD, "0.90-0.95"))


@dataclass(frozen=True)
class CodedFlag:
    notice_id: str
    filed_codes: tuple      # the publisher's codes, sorted
    jev_choice: str         # top profile family; at mass >= 0.90 also Jev's overall top choice
    mass_band: str          # one of MASS_BANDS' labels - never the mass itself
    model: str
    question_sha256: str
    content_sha256: str


def mass_band(mass: float) -> Optional[str]:
    """The band a mass falls in, at 0.01 resolution; None below FLAG_THRESHOLD."""
    for floor, label in MASS_BANDS:
        if mass_reaches(mass, floor):
            return label
    return None


def coded_flag(notice: Notice, criteria: dict,
               imputation: Optional[Imputation]) -> Optional[CodedFlag]:
    """
    A flag, or None. None for an uncoded notice, a coded notice its codes
    admit, a notice with no imputation, and a mass under FLAG_THRESHOLD.
    """
    codes = ingest.parse_unspsc_codes(notice.unspsc)
    if not codes or imputation is None:
        return None
    if ingest.matches_unspsc_families(codes, criteria.get("unspsc_families") or []):
        return None
    band = mass_band(imputation.mass)
    if band is None:
        return None
    return CodedFlag(notice.notice_id, tuple(sorted(codes)), imputation.family, band,
                     imputation.model, imputation.question_sha256,
                     imputation.content_sha256)


def stage_value(notice: Notice, criteria: dict, as_of) -> StageResult:
    """
    Retired from the default path, and dead twice over.

    It needs --extract-values AND a non-default range, and the profile ships
    value_min/value_max commented out so parse_profile defaults them to
    0 / 100_000_000. Reported as `inactive` rather than omitted: a stage that
    exists in code and decides nothing should say so, not vanish.
    """
    vmin = criteria.get("value_min", 0)
    vmax = criteria.get("value_max", 100_000_000)
    range_is_default = (vmin <= 0) and (vmax >= 100_000_000)
    return StageResult(
        "value", 6, "inactive", "no value extractor on the default path",
        detail={"value_min": vmin, "value_max": vmax,
                "range_is_default": range_is_default,
                "would_need": "--extract-values AND a non-default range"},
    )


def stage_identifiable(notice: Notice, criteria: dict, as_of) -> StageResult:
    """
    Blank or 'nan' reference number.

    Enforced in _write_chroma, AFTER the production funnel has already counted
    the row, so it appears in no funnel line ingest.py prints. Modelled here so
    the replay can print the count production never has - and so the equivalence
    test knows to subtract these rows rather than report a phantom disagreement.
    """
    ident = (notice.notice_id or "").strip()
    bad = (not ident) or ident == "nan"
    return StageResult(
        "identifiable", 7, "drop" if bad else "pass", "reference_number",
        evidence={"notice_id": notice.notice_id},
        detail={"enforced_in": "_write_chroma",
                "absent_from_production_funnel": True},
    )


STAGES: tuple = (
    Stage(1, "closed", stage_closed, True, True, "filter_tenders",
          "Dead notices only; NaT is kept and tagged unknown."),
    Stage(2, "exclusion", stage_exclusion, True, True, "filter_tenders",
          "Profile exclude terms, bare substring."),
    Stage(3, "construction", stage_construction, True, True, "filter_tenders",
          "procurementCategory exactly {*CNST}; notice type outranks it."),
    Stage(4, "jurisdiction", stage_jurisdiction, True, True, "filter_tenders",
          "Only non_federal drops; unrecognised is admitted."),
    Stage(5, "relevance", stage_relevance, True, True, "filter_tenders",
          "Publisher classification first, keywords only where none was filed."),
    Stage(6, "value", stage_value, False, False, "filter_tenders",
          "Inactive: needs --extract-values and a non-default range."),
    Stage(7, "identifiable", stage_identifiable, True, True, "_write_chroma",
          "Blank/nan reference number; absent from the production funnel."),
)

STAGE_NAMES: tuple = tuple(s.name for s in STAGES)

# The stages filter_tenders itself enforces, in order. The equivalence test
# compares against these and subtracts the rest.
FILTER_TENDERS_STAGES: tuple = tuple(
    s.name for s in STAGES if s.enforced_in == "filter_tenders" and s.active
)


# ---------------------------------------------------------------------------
# The two decisions
# ---------------------------------------------------------------------------

def production_decision(notice: Notice, criteria: dict, as_of,
                        imputation: Optional[Imputation] = None) -> ProductionDecision:
    """
    Short-circuit, in stage order, stopping at the first drop.

    This is what ingest.py does. It reports WHICH gate stopped the notice and
    says nothing about the gates it never reached, because production never
    asked them.
    """
    evaluated = []
    for stage in STAGES:
        if not stage.active:
            continue
        result = _evaluate(stage, notice, criteria, as_of, imputation)
        evaluated.append(result)
        if result.drops:
            return ProductionDecision(notice.notice_id, False, stage.name,
                                      tuple(evaluated))
    return ProductionDecision(notice.notice_id, True, None, tuple(evaluated))


def audit_decision(notice: Notice, criteria: dict, as_of,
                   imputation: Optional[Imputation] = None) -> AuditDecision:
    """
    Every stage, against the original notice, never short-circuiting.

    A notice production rejected at `construction` still gets a real relevance
    verdict here, and that is the point: the production funnel cannot tell you
    that 2,174 construction drops would ALSO have failed relevance, or that some
    would have passed it.
    """
    results = tuple(_evaluate(stage, notice, criteria, as_of, imputation)
                    for stage in STAGES)
    admitted = not any(
        r.drops for r, stage in zip(results, STAGES) if stage.active
    )
    return AuditDecision(notice.notice_id, results, admitted)


def _window(text: str, term: str, span: int = 40) -> str:
    """A term with surrounding context, so evidence is readable without the field."""
    idx = text.find(term)
    if idx < 0:
        return ""
    start, end = max(0, idx - span), min(len(text), idx + len(term) + span)
    return text[start:end].replace("\n", " ")
