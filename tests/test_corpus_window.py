"""
Lock the corpus window: what enters, what is only labelled, and reproducibility.

Runs with plain Python — no pytest needed:
    python tests/test_corpus_window.py

Two bugs lived in three lines of filter_tenders, and neither was visible from the
corpus — which is why they need a test that names them rather than a comment.

BUG 1 — the cutoff DELETED near-close notices.
`min_days_until_close: 10` dropped everything closing sooner, and it ran before
body_date_conflict, so a notice closing in eight days was never read for a prose
deadline contradicting its field. A tender promoted to watching/ fell out of the
corpus in its final ten days. And the briefing's 7-day "act now" section could
never fill against a 10-day cutoff — 7 < 10 is not a tuning problem, it is an
unreachable section. The key is now an imminence THRESHOLD: it labels, it does
not exclude.

BUG 2 — the cutoff was a wall-clock INSTANT, not a date.
`datetime.now() + timedelta(days=10)` was compared against a full timestamp, so
notices closing at 14:00 on the boundary day survived a 09:00 ingest and were
dropped by a 15:00 one. Same feed, same day: 53 notices versus 48. Corpus size
was not reproducible within a day, which put noise into every week-over-week
diff and every gating denominator taken from one.

The assertions are written as directions ("X must be KEPT and tagged Y") rather
than as a snapshot of today's corpus, so a future change that reintroduces a
hard drop fails here loudly instead of quietly shrinking the corpus.
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

import conftest  # noqa: E402,F401  — MUST come first: redirects the vault

import pandas as pd  # noqa: E402

from ingest import (  # noqa: E402
    SENTINEL_HORIZON_YEARS,
    TENDER_COLUMNS,
    TENDER_REQUIRED,
    filter_tenders,
    closing_window,
    parse_profile,
    resolve_columns,
)


FAILURES: list[str] = []


def check(label: str, got, want) -> None:
    if got == want:
        print(f"  PASS  {label}")
    else:
        FAILURES.append(f"{label}: expected {want!r}, got {got!r}")
        print(f"  FAIL  {label}: expected {want!r}, got {got!r}")


# ---------------------------------------------------------------------------
# A synthetic feed. Real column names, so a rename in TENDER_COLUMNS breaks
# this test rather than letting it pass against a shape that no longer exists.
# ---------------------------------------------------------------------------

def _row(tender_id: str, closing: str, title: str = "Informatics services") -> dict:
    return {
        "referenceNumber-numeroReference": tender_id,
        "tenderClosingDate-appelOffresDateCloture": closing,
        "title-titre-eng": title,
        "tenderDescription-descriptionAppelOffres-eng":
            "Software and cloud informatics professional services requirement.",
        "unspscDescription-unspscDescription-eng": "",
        "unspsc-unspsc": "81111800",
        "procurementCategory-categorieApprovisionnement": "SRV",
        "noticeType-avisType-eng": "Request for Proposal",
        "contractingEntityName-nomEntitePart1-eng": "Department of National Defence (DND)",
        "endUserEntitiesName-nomEntitesUtilisateurFinal-eng": "",
    }


def _feed(rows: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    for col_names in TENDER_COLUMNS.values():
        for name in col_names:
            if name not in df.columns:
                df[name] = ""
    return df


def _criteria(threshold: int = 10) -> dict:
    crit = parse_profile(
        Path(__file__).parent.parent / "vault" / "profiles" / "my-company.md"
    )
    crit["imminent_within_days"] = threshold
    return crit


def _run(df: pd.DataFrame, threshold: int = 10) -> set[str]:
    """Filtered tender IDs. Output is swallowed — the funnel is not under test."""
    import contextlib
    import io

    cols = resolve_columns(
        list(df.columns), TENDER_COLUMNS, TENDER_REQUIRED, "tests/test_corpus_window.py"
    )
    with contextlib.redirect_stdout(io.StringIO()):
        out = filter_tenders(df, _criteria(threshold), cols, None)
    return set(out[cols["tender_id"]])


def _iso(days_from_today: int, hour: int = 14) -> str:
    stamp = datetime.combine(date.today(), datetime.min.time()) + timedelta(
        days=days_from_today, hours=hour
    )
    return stamp.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Bug 1 — near-close notices are LABELLED, never dropped
# ---------------------------------------------------------------------------

def test_imminent_notices_stay_in_the_corpus() -> None:
    print("\nNear-close notices are kept, not deleted:")
    kept = _run(_feed([
        _row("IMMINENT-2D", _iso(2)),
        _row("IMMINENT-9D", _iso(9)),
        _row("OPEN-40D", _iso(40)),
    ]), threshold=10)

    # The whole point. Under the old cutoff both of these were deleted before
    # anything downstream — including the date-conflict detector — could read them.
    check("a notice closing in 2 days is KEPT", "IMMINENT-2D" in kept, True)
    check("a notice closing in 9 days is KEPT", "IMMINENT-9D" in kept, True)
    check("a comfortably open notice is KEPT", "OPEN-40D" in kept, True)
    check("corpus holds all three", len(kept), 3)


def test_the_threshold_labels_and_does_not_exclude() -> None:
    print("\nRaising the threshold changes labels, never membership:")
    feed = _feed([_row("A", _iso(2)), _row("B", _iso(20)), _row("C", _iso(200))])

    at_10 = _run(feed, threshold=10)
    at_60 = _run(feed, threshold=60)
    at_0 = _run(feed, threshold=0)

    # If any of these differ, the key has gone back to being a filter.
    check("threshold 10 and 60 give the same corpus", at_10 == at_60, True)
    check("threshold 0 gives the same corpus too", at_10 == at_0, True)
    check("nothing was excluded at any threshold", len(at_10), 3)


def test_closed_notices_are_dropped() -> None:
    print("\nClosed is the only thing dropped on date:")
    kept = _run(_feed([
        _row("CLOSED-YESTERDAY", _iso(-1)),
        _row("CLOSING-TODAY", _iso(0)),
        _row("OPEN", _iso(30)),
    ]))
    check("a notice that closed yesterday is DROPPED",
          "CLOSED-YESTERDAY" in kept, False)
    # Closing today is still open today. Dropping it would re-introduce the
    # class of error this whole change exists to remove.
    check("a notice closing today is KEPT", "CLOSING-TODAY" in kept, True)
    check("an open notice is KEPT", "OPEN" in kept, True)


def test_undated_notices_are_kept_not_silently_dropped() -> None:
    print("\nNo parseable date is a fact, not an absence:")
    # The old expression dropped these as a side effect of NaN comparison —
    # never as a decision, and never reported.
    kept = _run(_feed([_row("NO-DATE", ""), _row("OPEN", _iso(30))]))
    check("a notice with no closing date is KEPT", "NO-DATE" in kept, True)
    check("it is not confused with a closed one", len(kept), 2)


# ---------------------------------------------------------------------------
# Bug 2 — the cutoff is a date, so the corpus is reproducible within a day
# ---------------------------------------------------------------------------

def test_corpus_is_stable_across_the_working_day() -> None:
    print("\nSame feed, same day, different hour — same corpus:")
    # The exact shape of the real regression: notices closing at 14:00 on the
    # boundary day. Under an instant-based cutoff these survived a morning
    # ingest and vanished from an afternoon one.
    feed = _feed([
        _row("BOUNDARY-0900", _iso(10, hour=9)),
        _row("BOUNDARY-1400", _iso(10, hour=14)),
        _row("BOUNDARY-2330", _iso(10, hour=23)),
    ])
    check("all three boundary-day notices are kept", _run(feed), {
        "BOUNDARY-0900", "BOUNDARY-1400", "BOUNDARY-2330"
    })

    # And the same holds at the other end: a notice closing later today, at any
    # hour, is open today.
    today = _feed([
        _row("TODAY-0001", _iso(0, hour=0)),
        _row("TODAY-2359", _iso(0, hour=23)),
    ])
    check("time of day never decides membership", len(_run(today)), 2)


# ---------------------------------------------------------------------------
# closing_window — the five states, and the two that must not carry an integer
# ---------------------------------------------------------------------------

def test_window_classification() -> None:
    print("\nWindow classification:")
    today = date(2026, 8, 9)

    check("inside the threshold is imminent",
          closing_window("2026-08-12", 10, today)[0], "imminent")
    check("on the threshold is open, not imminent",
          closing_window("2026-08-19", 10, today)[0], "open")
    check("well out is open",
          closing_window("2026-10-01", 10, today)[0], "open")
    check("a past date is closed, not unknown",
          closing_window("2026-08-01", 10, today)[0], "closed")
    check("an empty date is unknown, not closed",
          closing_window("", 10, today)[0], "unknown")
    check("an unparseable date is unknown",
          closing_window("not-a-date", 10, today)[0], "unknown")

    check("days_until_close counts days",
          closing_window("2026-08-12", 10, today)[1], 3)
    check("a closed notice reports negative days",
          closing_window("2026-08-01", 10, today)[1], -8)


def test_sentinel_dates_are_standing_not_imminent_or_numeric() -> None:
    print("\nPlaceholder dates are not deadlines:")
    today = date(2026, 8, 9)
    # Both appear in the live feed on standing arrangements.
    for placeholder in ("2076-12-31", "2100-12-14"):
        check(f"{placeholder} is standing",
              closing_window(placeholder, 10, today)[0], "standing")
        # A five-digit integer here is worse than no answer: it sorts, it
        # renders, and it reads as a real countdown.
        check(f"{placeholder} reports no day count",
              closing_window(placeholder, 10, today)[1], None)

    check("unknown reports no day count either",
          closing_window("", 10, today)[1], None)

    inside = date(today.year + SENTINEL_HORIZON_YEARS, 1, 1).isoformat()
    check("a date inside the horizon is still a real date",
          closing_window(inside, 10, today)[0], "open")


def test_one_horizon_definition_not_two() -> None:
    print("\nThe sentinel horizon is defined once:")
    import tender_tools

    # It used to be declared in both modules with the same value. That is fine
    # until one is tuned and the corpus and the dossier disagree about what
    # counts as a placeholder.
    check("tender_tools reuses ingest's constant",
          tender_tools._SENTINEL_HORIZON_YEARS is SENTINEL_HORIZON_YEARS, True)


def main() -> int:
    print("=" * 72)
    print("Corpus window — membership, labelling and reproducibility")
    print("=" * 72)
    test_imminent_notices_stay_in_the_corpus()
    test_the_threshold_labels_and_does_not_exclude()
    test_closed_notices_are_dropped()
    test_undated_notices_are_kept_not_silently_dropped()
    test_corpus_is_stable_across_the_working_day()
    test_window_classification()
    test_sentinel_dates_are_standing_not_imminent_or_numeric()
    test_one_horizon_definition_not_two()

    print("\n" + "=" * 72)
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S):")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("All corpus-window checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
