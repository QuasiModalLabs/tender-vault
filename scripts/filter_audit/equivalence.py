"""
Proof that the audit path and ingest.py agree about who gets in.

WHY THIS EXISTS RATHER THAN A COMMENT ASKING NICELY. predicates.py restates the
combination logic of filter_tenders. Every restatement is a chance to drift, and
a drifted audit is worse than no audit: it reports confidently about a filter
that is not the one running. So the two are run over the same rows and the
symmetric difference must be empty.

TWO SUBTRACTIONS, both deliberate and both stated rather than quietly applied:

  stage 7 (identifiable) is enforced in _write_chroma, AFTER filter_tenders has
  returned, so filter_tenders' output still contains blank/'nan' reference
  numbers. The audit drops them at stage 7. Comparing raw would report a
  disagreement that is really a difference in where the gate lives, so those
  rows are subtracted from the audit side and counted separately.

  stage 1 reads a clock. filter_tenders calls datetime.now().date() internally,
  so the comparison pins that clock rather than racing it - by monkeypatching
  ingest.datetime for the duration and handing production_decision the same
  date. Once filter_tenders takes an explicit as_of the patch goes away and the
  argument is passed directly; the patch path is kept for running this check
  against an unmodified ingest.py, which is how it was first proved.

SKIPPED IS REPORTED LOUDLY. The feed is gitignored, so on a fresh runner there
may be nothing to compare. A check that silently passes when it had no data is
the failure mode this whole package exists to prevent.
"""
from __future__ import annotations

import contextlib
import io
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import pandas as pd

import ingest

from . import predicates as P

PROJECT_ROOT = Path(__file__).parent.parent.parent
FEED_CSV = PROJECT_ROOT / ".cache" / "tenders.csv"
NOTICES_DB = PROJECT_ROOT / "data" / "notices.db"
DEFAULT_PROFILE = PROJECT_ROOT / "vault" / "profiles" / "my-company.md"


class _PinnedDatetime:
    """datetime with now() frozen, so filter_tenders' clock stops moving."""

    def __init__(self, pinned: date):
        self._pinned = pinned

    def now(self, tz=None):
        return datetime(self._pinned.year, self._pinned.month, self._pinned.day)

    def __getattr__(self, name):
        return getattr(datetime, name)


@contextlib.contextmanager
def _pinned_clock(as_of: date):
    """
    Freeze ingest.datetime.now() for the length of one filter_tenders call.

    Only needed while filter_tenders still reads the clock itself. It is a
    narrow patch on one module attribute, restored unconditionally.
    """
    original = ingest.datetime
    ingest.datetime = _PinnedDatetime(as_of)
    try:
        yield
    finally:
        ingest.datetime = original


def _frame_from_archive(notices_db: Path, limit: Optional[int] = None):
    """
    Archive rows shaped as a feed frame, so filter_tenders can read them.

    The archive's fixed column names are renamed to the feed's resolved names.
    That is the same mapping Notice.from_archive_row applies, expressed once
    more for the one consumer that needs a DataFrame.
    """
    import sqlite3
    conn = sqlite3.connect(f"file:{notices_db}?mode=ro", uri=True)
    sql = "SELECT * FROM notices ORDER BY rowid"
    if limit:
        sql += f" LIMIT {int(limit)}"
    df = pd.read_sql_query(sql, conn)
    conn.close()
    cols = {
        "tender_id": "reference_number", "title": "title",
        "description": "description", "closing_date": "closing_date",
        "contracting_entity": "contracting_entity", "end_user": "end_user",
        "notice_type": "notice_type",
        "procurement_category": "procurement_category", "unspsc": "unspsc",
        "gsin": "gsin",
    }
    return df, cols


def verify(source: str = "feed",
           as_of: Optional[date] = None,
           profile_path: Optional[Path] = None,
           limit: Optional[int] = None) -> dict:
    """
    Run both paths over the same rows and compare the admitted sets.

    Returns `agreed: True` only when the symmetric difference is empty AND the
    comparison actually had rows to run on.
    """
    criteria = ingest.parse_profile(Path(profile_path or DEFAULT_PROFILE))
    as_of = as_of or date.today()

    if source == "feed":
        if not FEED_CSV.exists():
            return {"skipped": True, "agreed": None,
                    "reason": f"SKIPPED - no cached feed at {FEED_CSV}. "
                              f"This check had nothing to compare, which is NOT "
                              f"the same as passing."}
        df = pd.read_csv(FEED_CSV, dtype=str, low_memory=False)
        if limit:
            df = df.head(int(limit))
        cols = ingest.resolve_columns(df, ingest.TENDER_COLUMNS,
                                      ingest.TENDER_REQUIRED, "tenders")
    elif source == "archive":
        if not NOTICES_DB.exists():
            return {"skipped": True, "agreed": None,
                    "reason": f"SKIPPED - no archive at {NOTICES_DB}."}
        df, cols = _frame_from_archive(NOTICES_DB, limit)
    else:
        return {"error": f"Unknown source {source!r}"}

    # --- production, through the real filter -------------------------------
    with _pinned_clock(as_of):
        with contextlib.redirect_stdout(io.StringIO()) as captured:
            survivors = ingest.filter_tenders(df.copy(), criteria, cols)
    filter_ids = {str(v) for v in survivors[cols["tender_id"]]}

    # --- the audit path, over the same rows --------------------------------
    closing = P.parse_closing_days(df[cols["closing_date"]])
    audit_ids = set()
    blank_ids = 0
    per_stage = {}
    for (_, row), day in zip(df.iterrows(), closing):
        notice = P.Notice.from_feed_row(row, cols, closing_day=day)
        decision = P.production_decision(notice, criteria, as_of)
        if decision.first_rejecting_stage:
            per_stage[decision.first_rejecting_stage] = per_stage.get(
                decision.first_rejecting_stage, 0) + 1
        if decision.first_rejecting_stage == "identifiable":
            blank_ids += 1
        if decision.admitted:
            audit_ids.add(notice.notice_id)

    # filter_tenders returns rows _write_chroma would later skip. Subtract them
    # from ITS side, not the audit's, and say how many.
    filter_ids_identifiable = {
        i for i in filter_ids if i and i.strip() and i.strip() != "nan"}
    dropped_late = len(filter_ids) - len(filter_ids_identifiable)

    only_filter = sorted(filter_ids_identifiable - audit_ids)
    only_audit = sorted(audit_ids - filter_ids_identifiable)
    matched = not only_filter and not only_audit

    # AGREEMENT ON AN EMPTY SET IS NOT EVIDENCE. Replaying the archive at a
    # present-day as_of closes every row at stage 1, and both paths then admit
    # nothing and "agree" by arithmetic. Reporting that as a pass is the same
    # error as scoring two arms that received identical inputs. So a comparison
    # with no survivors on either side is `vacuous`, never `agreed`.
    vacuous = matched and not filter_ids_identifiable and not audit_ids
    agreed = matched and not vacuous

    return {
        "skipped": False,
        "agreed": agreed,
        "vacuous": vacuous,
        "source": source,
        "as_of": str(as_of),
        "rows": len(df),
        "filter_tenders_admitted": len(filter_ids),
        "blank_ids_in_filter_output": dropped_late,
        "filter_tenders_admitted_identifiable": len(filter_ids_identifiable),
        "audit_admitted": len(audit_ids),
        "audit_dropped_at_identifiable": blank_ids,
        "only_filter_tenders": only_filter[:25],
        "only_audit": only_audit[:25],
        "only_filter_tenders_total": len(only_filter),
        "only_audit_total": len(only_audit),
        "audit_first_rejecting_stage": per_stage,
        "funnel_printed_by_ingest": captured.getvalue().splitlines(),
    }


def render(result: dict) -> str:
    if "error" in result:
        return f"ERROR: {result['error']}"
    if result.get("skipped"):
        return result["reason"]
    lines = [
        f"Equivalence check - {result['source']}, {result['rows']:,} rows, "
        f"as_of {result['as_of']}",
        f"  filter_tenders admitted        {result['filter_tenders_admitted']:,}"
        f"  ({result['blank_ids_in_filter_output']} blank/nan ids _write_chroma "
        f"would skip)",
        f"  audit path admitted            {result['audit_admitted']:,}"
        f"  ({result['audit_dropped_at_identifiable']} dropped at stage 7)",
        f"  admitted only by filter_tenders {result['only_filter_tenders_total']:,}",
        f"  admitted only by audit          {result['only_audit_total']:,}",
    ]
    if result["only_filter_tenders"]:
        lines.append(f"    filter-only: {result['only_filter_tenders']}")
    if result["only_audit"]:
        lines.append(f"    audit-only:  {result['only_audit']}")
    lines.append("")
    if result.get("vacuous"):
        lines.append("  VACUOUS - both paths admitted nothing, so they agree by "
                     "arithmetic and this run is not evidence. Pick an as_of at "
                     "which the corpus still has open notices.")
    elif result["agreed"]:
        lines.append(f"  AGREED - the audit reproduces production exactly on "
                     f"{result['audit_admitted']:,} admitted notices")
    else:
        lines.append("  DISAGREED - the audit does NOT reproduce production. "
                     "filter_tenders is authoritative; predicates.py is wrong.")
    return "\n".join(lines)
