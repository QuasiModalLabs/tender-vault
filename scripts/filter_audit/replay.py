"""
Replay the filter over a corpus, and record why each notice landed where it did.

THE CORPUS IS data/notices.db AND NOTHING IS COPIED INTO IT. Audit records carry
a reference_number and the decision; title, description and entity names stay
where they already live. notices_ingest.py writes that table as facts rather
than as a filtered corpus - "a base rate cannot be computed from a table that
has already dropped the negatives" - which is exactly what makes it the right
replay population.

AS_OF, AND WHY `publication` IS THE DEFAULT
-------------------------------------------
Production reads datetime.now().date(), which cannot be replayed: run it twice
on two days and you get two answers. Three modes are offered and the default is
the only one that is both deterministic and meaningful.

  publication   each notice judged against its own publication_date. Immutable,
                stored, so two runs on two machines on two days agree. Asks the
                question worth asking: would this have been admitted when it was
                live?
  YYYY-MM-DD    one fixed clock for the whole run.
  none          stage 1 is SKIPPED and recorded as skipped, never as passed.

A single present-day as_of is deterministic but degenerate on an archive: at
2026-08-13 the closed drop takes 30,484 of 30,527 rows and every later stage
reports ~0. The funnel says nothing because it never got past stage 1.

THIS IS THE ARCHIVE, NOT THE PRODUCTION POPULATION
--------------------------------------------------
notices.db holds every federal notice for the fiscal years ingested, including
ones that were never in the open feed on any day ingest.py ran. Production
filters the open feed. The two are different populations and every replay says
so in its own output; `compare` refuses to cross them.

STORED DERIVED COLUMNS ARE NEVER TRUSTED
----------------------------------------
notices.db stores opportunity_kind from whichever classifier ran on first
insert, and INSERT OR IGNORE never updates it. Recomputing is not a preference:
34 rows currently store `construction`, which is the label that drops at stage
3, so trusting the column would make the replay reject notices the current
classifier keeps - auditing a classifier that no longer exists. The drift is
itself a finding, so it is counted and printed rather than papered over.
"""
from __future__ import annotations

import hashlib
import json
import random
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Iterator, Optional

import pandas as pd

import ingest

from . import predicates as P
from .version import filter_version

PROJECT_ROOT = Path(__file__).parent.parent.parent
NOTICES_DB = PROJECT_ROOT / "data" / "notices.db"
AUDIT_DB = PROJECT_ROOT / "data" / "filter_audit.db"
DEFAULT_PROFILE = PROJECT_ROOT / "vault" / "profiles" / "my-company.md"
FEED_CSV = PROJECT_ROOT / ".cache" / "tenders.csv"
SNAPSHOT_DIR = FEED_CSV.parent / "snapshots"

TOOL_VERSION = "filter_audit/1"

SCHEMA = """
-- APPEND-ONLY. There is no UPDATE statement in this package against these two
-- tables. A run is keyed on a content-addressed id, so re-running an identical
-- replay is a no-op rather than a rewrite, and an older run's rows are never
-- touched by a newer one. That is what makes "historical records are never
-- rewritten" a property of the schema rather than a promise.
CREATE TABLE IF NOT EXISTS runs (
    run_id                TEXT PRIMARY KEY,
    filter_version_label  TEXT NOT NULL,
    profile_sha256        TEXT NOT NULL,
    predicates_sha256     TEXT NOT NULL,
    stage_manifest_sha256 TEXT NOT NULL,
    variant_id            TEXT,
    as_of_spec            TEXT NOT NULL,
    source                TEXT NOT NULL,
    corpus_fingerprint    TEXT NOT NULL,
    corpus_rows           INTEGER NOT NULL,
    sample_size           INTEGER,
    sample_seed           INTEGER,
    -- The only non-deterministic column, and it is excluded from run_id for
    -- exactly that reason.
    first_written_at      TEXT NOT NULL,
    tool_version          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS decisions (
    run_id                TEXT NOT NULL,
    notice_id             TEXT NOT NULL,
    as_of_used            TEXT,
    production_admitted   INTEGER NOT NULL,
    first_rejecting_stage TEXT,
    audit_admitted        INTEGER NOT NULL,
    audit_stage_results   TEXT NOT NULL,
    has_codes             INTEGER NOT NULL,
    family_result         TEXT NOT NULL,
    keyword_result        TEXT NOT NULL,
    matched_families      TEXT NOT NULL,
    matched_keywords      TEXT NOT NULL,
    kind_recomputed       TEXT NOT NULL,
    kind_stored           TEXT,
    PRIMARY KEY (run_id, notice_id)
);

CREATE INDEX IF NOT EXISTS decisions_by_stage
    ON decisions(run_id, first_rejecting_stage);
CREATE INDEX IF NOT EXISTS decisions_by_branch
    ON decisions(run_id, has_codes, family_result, keyword_result);
"""


def connect(path: Path = AUDIT_DB) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


# ---------------------------------------------------------------------------
# Reading the corpus
# ---------------------------------------------------------------------------

def _archive_closing_days(conn: sqlite3.Connection) -> dict:
    """
    Every closing date parsed in ONE batch, keyed by reference number.

    Batched because pandas infers a format from the first element and coerces
    mismatches to NaT, so a per-row parse can disagree with production's
    whole-column parse. Reading only this column keeps 30k descriptions out of
    memory. See predicates.parse_closing_days.
    """
    rows = conn.execute(
        "SELECT reference_number, closing_date FROM notices").fetchall()
    refs = [r[0] for r in rows]
    parsed = P.parse_closing_days(pd.Series([r[1] for r in rows]))
    return dict(zip(refs, parsed))


def corpus_fingerprint(conn: sqlite3.Connection) -> tuple:
    """
    Which corpus this was, independent of when it was read.

    A hash of every reference number in rowid order plus the count. Two machines
    with the same archive agree; a re-ingest that adds rows does not.
    """
    digest = hashlib.sha256()
    count = 0
    for (ref,) in conn.execute("SELECT reference_number FROM notices ORDER BY rowid"):
        digest.update(str(ref).encode())
        digest.update(b"\x00")
        count += 1
    return digest.hexdigest(), count


def iter_archive(conn: sqlite3.Connection, closing_days: dict) -> Iterator:
    """Stream `notices` rows as (Notice, stored_opportunity_kind)."""
    for row in conn.execute("SELECT * FROM notices ORDER BY rowid"):
        notice = P.Notice.from_archive_row(
            row, closing_day=closing_days.get(row["reference_number"]))
        yield notice, row["opportunity_kind"]


def first_snapshot_date(snapshot_dir: Path = SNAPSHOT_DIR) -> Optional[str]:
    """
    The earliest day whose feed was kept, or None when none was.

    ingest.py copies the outgoing .cache/tenders.csv into that directory before
    each overwrite, so feed membership is recoverable from this date forward and
    not before it. Read from the FILENAMES rather than from an index: there is
    deliberately no index, and a directory listing cannot disagree with its own
    contents.

    The date is taken as the filename says it - not parsed into a date object
    and reformatted - so a file the naming convention does not fit is skipped
    rather than being coerced into a day it does not describe.
    """
    if not snapshot_dir.exists():
        return None
    dates = []
    for path in snapshot_dir.glob("*.csv.gz"):
        stem = path.name[:-len(".csv.gz")]
        candidate = stem.rsplit("-", 3)[-3:]
        if len(candidate) == 3 and all(part.isdigit() for part in candidate):
            year, month, day = candidate
            if len(year) == 4 and len(month) == 2 and len(day) == 2:
                dates.append(f"{year}-{month}-{day}")
    return min(dates) if dates else None


def iter_feed(csv_path: Path = FEED_CSV) -> Iterator:
    """Stream the open-notice feed as (Notice, None) - the feed stores no kind."""
    df = pd.read_csv(csv_path, dtype=str, low_memory=False)
    cols = ingest.resolve_columns(df, ingest.TENDER_COLUMNS,
                                  ingest.TENDER_REQUIRED, "tenders")
    closing = P.parse_closing_days(df[cols["closing_date"]])
    for (_, row), day in zip(df.iterrows(), closing):
        yield P.Notice.from_feed_row(row, cols, closing_day=day), None


# ---------------------------------------------------------------------------
# as_of resolution
# ---------------------------------------------------------------------------

def resolve_as_of(spec: str, notice: P.Notice):
    """
    The clock for one notice, from the run's spec.

    `publication` returns the notice's own publication_date, which is why the
    mode is deterministic. A notice with no publication date (the open feed
    carries none) yields None, and stage 1 records `skipped` rather than
    inventing a clock.
    """
    if spec == "none":
        return None
    if spec == "publication":
        return notice.publication_date or None
    return spec


# ---------------------------------------------------------------------------
# The replay
# ---------------------------------------------------------------------------

def replay(source: str = "archive",
           as_of_spec: str = "publication",
           profile_path: Optional[Path] = None,
           sample: Optional[int] = None,
           seed: Optional[int] = None,
           variant_id: Optional[str] = None,
           persist: bool = False,
           notices_db: Path = NOTICES_DB,
           audit_db: Path = AUDIT_DB) -> dict:
    """
    Evaluate every notice twice - once as production, once as the audit.

    Returns the funnel. With persist=True the per-notice records land in
    data/filter_audit.db under a content-addressed run_id, so an identical
    replay inserts nothing rather than duplicating itself.
    """
    criteria = ingest.parse_profile(Path(profile_path or DEFAULT_PROFILE))
    version = filter_version(profile_path)

    apply_variant = None
    if variant_id:
        from .variants import get_variant
        apply_variant = get_variant(variant_id)

    if source == "archive":
        if not notices_db.exists():
            return {"error": f"No corpus at {notices_db}. "
                             f"Run scripts/notices_ingest.py first."}
        src = sqlite3.connect(f"file:{notices_db}?mode=ro", uri=True)
        src.row_factory = sqlite3.Row
        fingerprint, rows_total = corpus_fingerprint(src)
        closing_days = _archive_closing_days(src)
        shapes = P.closing_date_shapes(
            r[0] for r in src.execute("SELECT closing_date FROM notices"))
        stream = iter_archive(src, closing_days)
    elif source == "feed":
        if not FEED_CSV.exists():
            return {"error": f"No cached feed at {FEED_CSV}."}
        feed_rows = list(iter_feed())
        rows_total = len(feed_rows)
        fingerprint = hashlib.sha256(
            "".join(n.notice_id + "\x00" for n, _ in feed_rows).encode()).hexdigest()
        shapes = P.closing_date_shapes(n.raw_closing_date for n, _ in feed_rows)
        stream = iter(feed_rows)
    else:
        return {"error": f"Unknown source {source!r}. Use archive or feed."}

    run_id = hashlib.sha256("|".join([
        version["label"], version["predicates_sha256"], as_of_spec, source,
        fingerprint, str(variant_id), str(sample), str(seed),
    ]).encode()).hexdigest()[:16]

    first_reject = Counter()
    audit_fires = Counter()
    unique_drop = Counter()
    without_relevance = 0
    branch = Counter()
    kind_drift = Counter()
    admitted_ids = []
    records = []
    evaluated = 0

    rng = random.Random(seed) if sample and seed is not None else None
    keep_probability = None
    if sample and rows_total:
        keep_probability = min(1.0, sample / rows_total)

    for notice, stored_kind in stream:
        if keep_probability is not None:
            picker = rng.random() if rng else random.random()
            if picker > keep_probability:
                continue
        evaluated += 1
        as_of = resolve_as_of(as_of_spec, notice)

        if apply_variant is not None:
            prod, audit = apply_variant(notice, criteria, as_of)
        else:
            prod = P.production_decision(notice, criteria, as_of)
            audit = P.audit_decision(notice, criteria, as_of)

        first_reject[prod.first_rejecting_stage or "ADMITTED"] += 1
        for result in audit.results:
            if result.drops:
                audit_fires[result.stage] += 1

        # WHAT EACH STAGE IS WORTH, which no short-circuiting funnel can say.
        # `audit_fires` counts every stage that would drop a notice, so a row
        # failing construction AND relevance is counted twice and both stages
        # look load-bearing. A stage's unique contribution is the rows it drops
        # that NOTHING else would - remove the stage and exactly these notices
        # enter the corpus. See dropping_stages.
        dropped_by = dropping_stages(audit)
        if len(dropped_by) == 1:
            unique_drop[next(iter(dropped_by))] += 1
        if dropped_by and "relevance" not in dropped_by:
            # Rows removed with no relevance drop behind them. Relevance is the
            # stage that carries the archive, so this is the honest measure of
            # what every other stage adds on top of it - and the answer is
            # small enough that it belongs in the output rather than in a note
            # someone computed once.
            without_relevance += 1

        rel = audit.by_stage("relevance").detail
        con = audit.by_stage("construction").evidence
        recomputed = con.get("opportunity_kind", "")
        if stored_kind is not None and stored_kind != recomputed:
            kind_drift[f"{stored_kind} -> {recomputed}"] += 1

        if rel.get("gate_configured", True):
            if rel["has_codes"]:
                branch["coded_pass" if rel["family_result"] == "matched"
                       else "coded_wrong_family"] += 1
            else:
                branch["uncoded_pass" if rel["keyword_result"] == "matched"
                       else "uncoded_no_keyword"] += 1

        if prod.admitted:
            admitted_ids.append(notice.notice_id)

        if persist:
            relevance_evidence = audit.by_stage("relevance").evidence
            records.append((
                run_id, notice.notice_id, str(as_of) if as_of else None,
                int(prod.admitted), prod.first_rejecting_stage,
                int(audit.admitted),
                json.dumps([r.to_dict() for r in audit.results],
                           separators=(",", ":"), default=str),
                int(rel.get("has_codes", False)),
                rel.get("family_result", ""), rel.get("keyword_result", ""),
                json.dumps(relevance_evidence.get("matched_families", [])),
                json.dumps(relevance_evidence.get("matched_keywords", [])),
                recomputed, stored_kind,
            ))

    written = 0
    if persist:
        conn = connect(audit_db)
        with conn:
            conn.execute(
                "INSERT OR IGNORE INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, version["label"], version["profile_sha256"],
                 version["predicates_sha256"], version["stage_manifest_sha256"],
                 variant_id, as_of_spec, source, fingerprint, rows_total,
                 sample, seed, datetime.now().isoformat(timespec="seconds"),
                 TOOL_VERSION))
            before = conn.execute(
                "SELECT COUNT(*) FROM decisions WHERE run_id=?", (run_id,)
            ).fetchone()[0]
            conn.executemany(
                "INSERT OR IGNORE INTO decisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                records)
            after = conn.execute(
                "SELECT COUNT(*) FROM decisions WHERE run_id=?", (run_id,)
            ).fetchone()[0]
            written = after - before
        conn.close()

    admitted = first_reject.get("ADMITTED", 0)
    snapshot_from = first_snapshot_date()
    return {
        "run_id": run_id,
        "filter_version": version,
        "variant_id": variant_id,
        "source": source,
        "as_of_spec": as_of_spec,
        "corpus_rows": rows_total,
        "evaluated": evaluated,
        "admitted": admitted,
        "rejected": evaluated - admitted,
        "production_first_rejecting_stage": {
            name: first_reject.get(name, 0)
            for name in P.STAGE_NAMES if first_reject.get(name)
        },
        "audit_stage_fires": {name: audit_fires.get(name, 0)
                              for name in P.STAGE_NAMES if audit_fires.get(name)},
        "audit_hidden_by_short_circuit": {
            name: audit_fires.get(name, 0) - first_reject.get(name, 0)
            for name in P.STAGE_NAMES if audit_fires.get(name)
        },
        "unique_contribution": {name: unique_drop.get(name, 0)
                                for name in P.STAGE_NAMES if audit_fires.get(name)},
        "dropped_without_relevance": without_relevance,
        "unique_contribution_note": (
            "Rows a stage drops that no other stage would - remove the stage "
            "and exactly these notices enter the corpus. THESE DO NOT SUM: a "
            "row dropped by two stages is unique to neither, so per-stage "
            "figures fall short of what any GROUP of stages uniquely removes. "
            "`dropped_without_relevance` is the group figure that matters here "
            "- everything the other stages remove that relevance would have "
            "left in."),
        "relevance_branches": dict(sorted(branch.items())),
        "stored_kind_drift": dict(kind_drift.most_common()),
        "closing_date_shapes": shapes,
        "persisted": persist,
        "rows_written": written,
        "population_note": (
            "ARCHIVE, not the production population. notices.db holds every "
            "federal notice for the fiscal years ingested, including notices "
            "never present in the open feed on any day ingest.py ran."
            if source == "archive" else
            "The open-notice feed as last downloaded - a snapshot of what was "
            "open that day. The outgoing copy is kept in .cache/snapshots/ "
            "before each overwrite, so past days are recoverable from the first "
            "snapshot forward."),
        "first_snapshot_date": snapshot_from,
        "limitation": _limitation(snapshot_from),
    }


ACTIVE_STAGES = frozenset(s.name for s in P.STAGES if s.active)


def dropping_stages(audit: P.AuditDecision) -> set:
    """
    Every active stage that would drop this notice, evaluated independently.

    The set, not the first element: `first_rejecting_stage` is what production
    reports and it cannot answer "what is this stage worth", because a stage
    that only ever fires on notices some other stage also rejects removes
    nothing on its own. A stage's unique contribution is the rows whose set is
    exactly {that stage}, and a row belonging to two stages is unique to
    NEITHER - it is removed by the pair, and per-stage figures therefore fall
    short of what a group of stages removes together.
    """
    return {r.stage for r in audit.results
            if r.drops and r.stage in ACTIVE_STAGES}


def _limitation(snapshot_from: Optional[str]) -> str:
    """
    What this replay cannot recover, stated against a date once there is one.

    Two wordings, not one with a date appended, because the claims differ in
    kind. With no snapshots the limitation is unbounded - every past day is
    gone. With snapshots it is bounded, and the boundary is the only part a
    reader can act on: before that date the question is unanswerable, after it
    the feed is on disk and a future replay can read it. Saying "cannot be
    reconstructed" flat once that stopped being true would be the audit
    overstating its own blindness, which is the same failure as overstating what
    it checked.

    Note what a snapshot does NOT give back: the predicates of that day. Before
    filter_audit existed no filter version was recorded, so a replay over an old
    snapshot judges old notices by today's rules.
    """
    if snapshot_from is None:
        return (".cache/tenders.csv is overwritten on every download and no "
                "snapshot has been taken yet, so the exact set of notices "
                "ChromaDB admitted on a past day cannot be reconstructed. This "
                "replay answers a different, answerable question - see the "
                "module docstring.")
    return (f"Replay of feed membership is unavailable before {snapshot_from}: "
            f".cache/tenders.csv was overwritten on every download until the "
            f"first snapshot was taken, so no set of notices open before that "
            f"date can be reconstructed. From {snapshot_from} forward the feed "
            f"as downloaded is in .cache/snapshots/. The PREDICATES of a past "
            f"day are still not recoverable - no filter version was recorded "
            f"before this package existed.")


def render_funnel(result: dict) -> str:
    """The funnel as a human reads it. `--json` gets the dict instead."""
    if "error" in result:
        return f"ERROR: {result['error']}"
    out = []
    version = result["filter_version"]
    out.append(f"Filter replay - {result['source']}, {result['corpus_rows']:,} rows")
    out.append(f"  filter version   {version['label']}  "
               f"(profile {version['profile_sha256'][:6]}.. / "
               f"predicates {version['predicates_sha256'][:6]}.. / "
               f"stages {version['stage_manifest_sha256'][:6]}..)")
    out.append(f"  as-of            {result['as_of_spec']}")
    if result["variant_id"]:
        out.append(f"  variant          {result['variant_id']}")
    out.append(f"  run              {result['run_id']}")
    out.append("")
    out.append("PRODUCTION DECISION - short-circuit, first rejecting stage")
    for stage in P.STAGES:
        count = result["production_first_rejecting_stage"].get(stage.name)
        if not stage.active:
            out.append(f"  {stage.order} {stage.name:<14} INACTIVE"
                       f"        {stage.why}")
        elif count:
            out.append(f"  {stage.order} {stage.name:<14} dropped {count:>9,}")
        else:
            out.append(f"  {stage.order} {stage.name:<14} dropped {0:>9,}")
    out.append(f"  {'admitted':<19} {result['admitted']:>15,}")
    out.append("")
    out.append("AUDIT DECISION - every stage evaluated independently")
    for name, fires in result["audit_stage_fires"].items():
        hidden = result["audit_hidden_by_short_circuit"].get(name, 0)
        unique = result.get("unique_contribution", {}).get(name, 0)
        out.append(f"  {name:<16} would drop {fires:>9,}   "
                   f"(short-circuit hid {hidden:,}; "
                   f"only this stage would drop {unique:,})")
    if result.get("unique_contribution"):
        out.append("")
        out.append(f"  rows dropped with no relevance drop behind them: "
                   f"{result['dropped_without_relevance']:,}")
        out.append("    Everything the other stages remove that relevance would "
                   "have left in.")
        out.append("    Per-stage figures above do not sum to it: a row dropped "
                   "by two stages is unique to")
        out.append("    neither, but is still removed by the pair.")
    if result["relevance_branches"]:
        out.append("")
        out.append("  relevance branches - two different failures, never one boolean")
        for key, value in result["relevance_branches"].items():
            out.append(f"    {key:<22} {value:>9,}")
    if result["stored_kind_drift"]:
        total = sum(result["stored_kind_drift"].values())
        out.append("")
        out.append(f"  stored vs recomputed opportunity_kind: {total} disagree "
                   f"of {result['evaluated']:,}")
        for key, value in result["stored_kind_drift"].items():
            out.append(f"    {key} {value}")
        out.append("    notices.db is append-only; these rows were written by an "
                   "older classifier.")
        out.append("    Rebuild with scripts/notices_ingest.py to reconcile.")
    shapes = result["closing_date_shapes"]
    if len(shapes) > 1:
        out.append("")
        out.append(f"  NOTE: closing_date is NOT format-uniform ({len(shapes)} shapes). "
                   f"pandas infers one format and coerces the rest to NaT, which "
                   f"reads as 'undated, kept'. Shapes: {shapes}")
    out.append("")
    out.append(f"  {result['population_note']}")
    # Printed, not just carried in the JSON. A limitation only a --json reader
    # ever sees is a limitation the person reading the funnel does not know
    # about, and this one bounds what every number above can be checked against.
    out.append(f"  {result['limitation']}")
    if result["persisted"]:
        out.append(f"  persisted {result['rows_written']:,} decision rows "
                   f"(0 means this exact run was already recorded)")
    return "\n".join(out)
