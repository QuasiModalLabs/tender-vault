"""
Leakage and predicate invariants for the backtest.

Runs with plain Python — no pytest needed:

    python tests/test_backtest.py

Exit code 0 = all passed. Without data/notices.db — which is 120MB and
gitignored, so CI never has it — the checks that need a corpus are skipped BY
NAME and counted, and the ones that need no data still run. See
`runs_without_corpus` for why that is opt-in rather than opt-out.

WHAT THIS GUARDS, and why it is the only test file that matters here. A backtest
that leaks is worse than no backtest: it produces a confident number, the number
is wrong in the optimistic direction, and nothing about it looks broken. Every
other property of this module — the fit, the intervals, the report layout — is
inspectable by reading the output. Leakage is not.

So the assertions below are almost all one shape: NOTHING `as_of(T)` RETURNS MAY
BE DATED AFTER T. They check the gate against each source table directly rather
than trusting that the SQL says `<= ?`, because the failure being guarded
against is someone adding a sixth accessor and forgetting the bound.

The second group guards the frozen predicate, which was already wrong once —
see the revision note in backtest.py's module docstring — and cost a discarded
run. `test_amendments_do_not_multiply_a_procurement` is that specific bug,
written down so it cannot come back.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import conftest  # noqa: E402,F401  — import-time vault guard, same as every suite
import backtest as bt  # noqa: E402

NOTICES_DB = ROOT / "data" / "notices.db"
PASSED = FAILED = SKIPPED = 0

# A date late enough that every source has rows before it and rows after it. A
# gate only proves something when both sides are non-empty: `as_of` over a date
# before all the data trivially returns nothing and leaks nothing.
PROBE_DATE = "2024-04-01"


def check(cond: bool, label: str) -> None:
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  ok    {label}")
    else:
        FAILED += 1
        print(f"  FAIL  {label}")


def skip(label: str) -> None:
    global SKIPPED
    SKIPPED += 1
    print(f"  skip  {label}")


def runs_without_corpus(fn):
    """
    Mark a test as needing no database, so CI runs it.

    data/notices.db is 120MB and gitignored, so it does not exist on a fresh
    runner and every test here used to be skipped there — including the frozen
    predicate guard, which is pure logic and is the one thing standing between a
    rebuilt corpus and a silently changed predicate.

    OPT IN, NOT OPT OUT, and the direction is the whole point. A test that
    reaches a database and finds it empty does not fail; it passes over nothing.
    `as_of` returning no rows dated after T is trivially true when it returns no
    rows at all, and a green tick that means "there was nothing to check" is the
    failure mode this repository refuses everywhere else - see the `vacuous`
    verdict in filter_audit/equivalence.py. So a new test is assumed to need the
    corpus and is skipped loudly without it; only a test that touches no data at
    all says so here.
    """
    fn.runs_without_corpus = True
    return fn


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------

def test_as_of_returns_nothing_dated_after_t():
    """
    The core invariant, checked per accessor against the accessor's own date
    column. Each is listed explicitly rather than discovered by reflection: a
    new accessor should fail this file by being absent from the list, which is a
    visible omission, rather than pass by being skipped, which is not.
    """
    ev = bt.as_of(PROBE_DATE)
    cases = [
        ("audits", ev.audits, "date_published"),
        ("award_notices", ev.award_notices, "publication_date"),
        ("notices", ev.notices, "publication_date"),
        ("contracts", ev.contracts, "contract_date"),
    ]
    for name, accessor, column in cases:
        rows = accessor()
        if not rows:
            skip(f"{name}: no rows at {PROBE_DATE} (source not built?)")
            continue
        late = [r[column] for r in rows if r[column] and r[column] > PROBE_DATE]
        check(not late,
              f"{name}: none of {len(rows):,} rows dated after {PROBE_DATE} "
              f"({len(late)} leaked{', e.g. ' + late[0] if late else ''})")


def test_plans_are_bounded_by_tabling_year():
    """
    Plans carry a fiscal year, not a date. The gate admits `year <= FY(T)`,
    which is conservative under either reading of that column — see
    Evidence.plans. A plan for a year that has not opened has not been tabled.
    """
    ev = bt.as_of(PROBE_DATE)
    rows = ev.plans()
    if not rows:
        skip("plans: no rows (plans.db not built?)")
        return
    limit = bt.fiscal_year_of(PROBE_DATE)
    late = [r["year"] for r in rows if r["year"] > limit]
    check(not late, f"plans: no row later than FY{limit} ({len(late)} leaked)")


def test_the_gate_is_actually_filtering():
    """
    A gate that returns everything passes every leak test above. This is the
    control: an earlier date must return strictly fewer rows than a later one.
    """
    early, late = bt.as_of("2023-04-01"), bt.as_of("2025-04-01")
    n_early, n_late = len(early.notices()), len(late.notices())
    if not n_late:
        skip("gate control: no notices at all")
        return
    check(n_early < n_late,
          f"gate control: {n_early:,} notices at 2023-04-01 < {n_late:,} at "
          f"2025-04-01")


def test_evidence_hands_out_no_connection():
    """
    Feature functions get an Evidence and must not be able to reach around it.
    An accessor returning a live cursor or connection would make the gate
    advisory, which is the same as not having one.
    """
    ev = bt.as_of(PROBE_DATE)
    leaked = [name for name in dir(ev)
              if isinstance(getattr(ev, name, None),
                            (sqlite3.Connection, sqlite3.Cursor))]
    check(not leaked, f"Evidence exposes no connection or cursor ({leaked})")


def test_every_feature_is_reachable_only_through_evidence():
    """
    Each registered feature must run against an Evidence alone and must carry a
    written justification of why its inputs are knowable at T. The docstring
    table is the argument; this is the check that no feature ships without one.
    """
    ev = bt.as_of(PROBE_DATE)
    start, end = bt.fy_bounds(2024)
    for feat in bt.FEATURES:
        try:
            value = feat.fn(ev, "ssc", start, end)
            ran = isinstance(value, float)
        except Exception as exc:                      # noqa: BLE001
            ran = False
            print(f"        {feat.name} raised {exc!r}")
        check(ran, f"feature {feat.name} runs against Evidence alone")
        check(bool(feat.knowable_because.strip()),
              f"feature {feat.name} states why it is knowable at T")


# ---------------------------------------------------------------------------
# The frozen predicate
# ---------------------------------------------------------------------------

def test_amendments_do_not_multiply_a_procurement(con):
    """
    THE BUG THAT COST A RUN. The first version of clause 6 kept only rows whose
    amendment_number was zero, on the assumption that an amendment arrives as an
    extra row sharing a key. It does not: reference_number is unique on every
    row, and each row carries the amendment count the notice had reached. That
    filter dropped 83% of real procurements.

    A solicitation that appears as several rows must be counted ONCE.
    """
    row = con.execute(
        """SELECT solicitation_number FROM notices
            WHERE solicitation_number NOT IN ('', '-')
            GROUP BY solicitation_number HAVING COUNT(*) > 1 LIMIT 1"""
    ).fetchone()
    if not row:
        skip("no multi-row solicitation in the archive to test against")
        return
    soln = row[0]
    cur = con.execute(
        """SELECT reference_number, solicitation_number, org_keys,
                  publication_date FROM notices WHERE solicitation_number = ?""",
        (soln,))
    names = [c[0] for c in cur.description]
    rows = [dict(zip(names, r)) for r in cur]
    collapsed = bt.target_solicitations(rows)
    check(len(collapsed) == 1,
          f"solicitation {soln}: {len(rows)} rows collapse to "
          f"{len(collapsed)} procurement")
    if collapsed:
        record = next(iter(collapsed.values()))
        check(record["first_published"] == min(r["publication_date"] for r in rows),
              "a procurement is dated to its EARLIEST publication, not its latest")


def test_reference_numbers_are_unique(con):
    """
    The fact the original predicate got wrong, asserted so a future change to
    notices_ingest's primary key cannot quietly restore the bad assumption.
    """
    total, distinct = con.execute(
        "SELECT COUNT(*), COUNT(DISTINCT reference_number) FROM notices"
    ).fetchone()
    check(total == distinct,
          f"reference_number is unique across all {total:,} notice rows "
          f"({distinct:,} distinct)")


def test_placeholder_solicitations_do_not_collide(con):
    """
    Three rows carry "-" as their solicitation number. Keying on it would merge
    unrelated procurements; _solicitation_key falls back to reference_number.
    """
    rows = [{"reference_number": f"REF-{i}", "solicitation_number": "-",
             "org_keys": "ssc", "publication_date": f"2023-0{i}-01"}
            for i in (1, 2, 3)]
    collapsed = bt.target_solicitations(rows)
    check(len(collapsed) == 3,
          f"three placeholder-solicitation notices stay three procurements "
          f"(got {len(collapsed)})")


def test_label_window_is_half_open(con):
    """
    A notice published exactly on 1 April belongs to the year that opens, not
    the one that closes. Both-inclusive bounds would double-count it.
    """
    start, end = bt.fy_bounds(2024)
    check((start, end) == ("2024-04-01", "2025-04-01"),
          f"fiscal year 2024 spans [{start}, {end})")
    check(bt.fiscal_year_of("2024-04-01") == 2024
          and bt.fiscal_year_of("2024-03-31") == 2023,
          "1 April opens a fiscal year and 31 March closes the previous one")


def test_partial_years_are_excluded_from_the_panel(con):
    """
    A year missing four months cannot carry a label: a department with no hit in
    it has been observed incompletely, not observed to abstain. FY2022-23 is
    partial because CanadaBuys launched 2022-08-08.
    """
    try:
        covered = bt.covered_fiscal_years(con)
    except sqlite3.OperationalError:
        skip("notices.db predates the sources table; re-run notices_ingest.py")
        return
    partial = {fy.split("-")[0] for fy, note in con.execute(
        "SELECT fiscal_year, partial_note FROM sources WHERE partial_note != ''")}
    overlap = {str(y) for y in covered} & partial
    check(not overlap, f"no partial fiscal year is in the panel ({overlap or 'none'})")
    check(all(isinstance(y, int) for y in covered),
          f"covered fiscal years are years: {covered}")


@runs_without_corpus
def test_split_point_handles_a_binary_feature():
    """
    A binary feature has median 1.0 among its non-zero values, so a naive
    "split above the median" selects nothing and the row reads as no-signal
    rather than as broken. That emptied prior_year_hit — the baseline every
    other feature has to beat.
    """
    values = [0.0] * 60 + [1.0] * 95
    nonzero = sorted(v for v in values if v > 0)
    threshold = bt._split_point(values, nonzero)
    selected = [v for v in values if v > threshold]
    check(len(selected) == 95,
          f"binary feature splits into 95 with-signal rows (got {len(selected)})")


@runs_without_corpus
def test_a_lift_on_thin_data_is_not_called_significant():
    """
    THE SECOND BUG THIS FILE EXISTS FOR. The first verdict function called a
    1.41x lift on six observations "a signal worth pursuing". The Wilson
    interval around 5/6 runs from roughly 44% to 97% and swallows the 71%
    stratum base rate whole — the lift was noise with a decimal point.

    A bare threshold on the lift cannot catch that, because the lift alone
    carries no information about how many rows produced it. The interval must
    exclude the base rate.
    """
    thin = bt.StratumLift("audit_it", lift=1.41, k=5, n=6,
                          stratum_base=0.71, ci=bt.wilson(5, 6))
    check(not thin.significant,
          f"5/6 at a 71% base rate is not significant (CI "
          f"{100 * thin.ci[0]:.0f}-{100 * thin.ci[1]:.0f}%)")

    thick = bt.StratumLift("audit_it", lift=1.41, k=50, n=60,
                           stratum_base=0.50, ci=bt.wilson(50, 60))
    check(thick.significant,
          f"50/60 at a 50% base rate is significant (CI "
          f"{100 * thick.ci[0]:.0f}-{100 * thick.ci[1]:.0f}%)")

    at_base = bt.StratumLift("x", lift=1.0, k=30, n=60,
                             stratum_base=0.50, ci=bt.wilson(30, 60))
    check(not at_base.significant, "a lift of exactly 1.0 is never significant")


@runs_without_corpus
def test_wilson_interval_brackets_the_point_estimate():
    """A CI that does not contain its own estimate would silently invert every
    significance call above."""
    for k, n in [(0, 10), (1, 10), (5, 10), (9, 10), (10, 10), (33, 52)]:
        lo, hi = bt.wilson(k, n)
        check(lo <= k / n <= hi and 0.0 <= lo <= hi <= 1.0,
              f"wilson({k}/{n}) = {100 * lo:.1f}-{100 * hi:.1f}% brackets "
              f"{100 * k / n:.1f}%")
    check(bt.wilson(0, 0) == (0.0, 1.0),
          "wilson on an empty sample is total ignorance, not a crash")


@runs_without_corpus
def test_the_frozen_predicate_refuses_a_moved_classifier():
    """
    Clause 5 freezes five kind STRINGS. When what those strings denote changes,
    the predicate must refuse to run rather than quietly describe a different
    experiment — and it must name what moved, because "something changed" is not
    something a human can act on.

    The drift is driven by a STAND-IN classifier rather than by the repository's
    current state. It fired for real once — "Directed Contract" was mapped to
    `pre_awarded` after the freeze, and the manifest was re-frozen against it on
    2026-08-21 — and a test that asserted the live state would have passed only
    until that decision was made. The mechanism is what has to keep working.
    """
    row = {"opportunity_kind": "solicitation", "unspsc": "*81111500"}

    # The live classifier and the recorded manifest agree: the re-freeze holds,
    # and the predicate runs.
    bt.non_procurement_kinds.cache_clear()
    check(bt._manifest_sha256(bt._live_kind_manifest())
          == bt.FROZEN_KIND_MANIFEST_SHA256,
          "the recorded manifest describes the live classifier")
    check(bt.is_target_notice(row, ["8111"], []) is True,
          "...so the frozen predicate runs")

    # Now move a literal into one of the frozen kinds, as 6aea2d0 did.
    moved = {kind: list(literals)
             for kind, literals in bt.FROZEN_KIND_MANIFEST.items()}
    moved["pre_awarded"] = moved["pre_awarded"] + ["notice_type:letter of interest"]
    original_manifest = bt.kind_manifest
    bt.kind_manifest = lambda: moved
    bt.non_procurement_kinds.cache_clear()
    raised = None
    try:
        bt.is_target_notice(row, ["8111"], [])
    except bt.FrozenPredicateDrift as exc:
        raised = exc
    finally:
        bt.kind_manifest = original_manifest
        bt.non_procurement_kinds.cache_clear()

    check(raised is not None,
          "is_target_notice refuses to run under a moved classifier")
    if raised is not None:
        message = str(raised)
        check("pre_awarded" in message,
              "...naming the kind whose membership changed")
        check("letter of interest" in message,
              "...and the literal it gained")
        check("construction" not in message,
              "...and NOT a kind whose membership held still")
        check("discarded and restarted" in message,
              "...and what resolving it means, rather than how to silence it")

    # A literal LOST from a frozen kind is the same class of change and must
    # also fire: a notice type that stops meaning `information` starts being
    # admitted by clause 5.
    shrunk = {kind: list(literals)
              for kind, literals in bt.FROZEN_KIND_MANIFEST.items()}
    shrunk["information"] = []
    bt.kind_manifest = lambda: shrunk
    bt.non_procurement_kinds.cache_clear()
    lost = None
    try:
        bt.non_procurement_kinds()
    except bt.FrozenPredicateDrift as exc:
        lost = str(exc)
    finally:
        bt.kind_manifest = original_manifest
        bt.non_procurement_kinds.cache_clear()
    check(lost is not None and "lost" in lost,
          "a literal LOST from a frozen kind fires too, and says so")

    # The manifest is restricted to the kinds the predicate actually reads. A
    # literal joining `qualification` cannot change what clause 5 excludes, and
    # an alarm with no consequence behind it trains people to ignore alarms.
    check(set(bt.FROZEN_KIND_MANIFEST) == set(bt._NON_PROCUREMENT_KINDS),
          "the frozen manifest covers exactly the frozen kinds")

    # The recorded hash must describe the recorded manifest. Editing one without
    # the other leaves neither as a record, and that is a different failure from
    # the classifier moving.
    original_hash = bt.FROZEN_KIND_MANIFEST_SHA256
    bt.FROZEN_KIND_MANIFEST_SHA256 = "0" * 64
    bt.non_procurement_kinds.cache_clear()
    bookkeeping = None
    try:
        bt.non_procurement_kinds()
    except bt.FrozenPredicateDrift as exc:
        bookkeeping = str(exc)
    finally:
        bt.FROZEN_KIND_MANIFEST_SHA256 = original_hash
        bt.non_procurement_kinds.cache_clear()
    check(bookkeeping is not None and "does not describe" in bookkeeping,
          "a recorded hash that does not match its manifest is its own failure")

    # And the guard is a gate, not a wall: with the classifier back where it was
    # frozen, the accessor returns the frozen set unchanged. Asserted by
    # standing in a manifest, never by re-deriving the set from the live one —
    # which is the thing the module refuses to do.
    original_manifest = bt.kind_manifest
    bt.kind_manifest = lambda: dict(bt.FROZEN_KIND_MANIFEST)
    bt.non_procurement_kinds.cache_clear()
    try:
        check(bt.non_procurement_kinds() == bt._NON_PROCUREMENT_KINDS,
              "an unmoved classifier returns the frozen set unchanged")
        check(bt.is_target_notice(row, ["8111"], []) is True,
              "...and the predicate runs again")
    finally:
        bt.kind_manifest = original_manifest
        bt.non_procurement_kinds.cache_clear()


def main() -> int:
    have_corpus = NOTICES_DB.exists()
    if not have_corpus:
        print(f"no {NOTICES_DB.name} (build it with python scripts/notices_ingest.py).\n"
              f"Running the checks that need no corpus; the rest are SKIPPED, "
              f"which is NOT the same as passing.")
    con = sqlite3.connect(NOTICES_DB) if have_corpus else None

    for name, fn in sorted(globals().items()):
        if not (name.startswith("test_") and callable(fn)):
            continue
        # A test taking `con` needs the corpus by construction; a test with no
        # arguments needs it unless it declared otherwise. See runs_without_corpus.
        needs_corpus = bool(fn.__code__.co_argcount) or not getattr(
            fn, "runs_without_corpus", False)
        if needs_corpus and not have_corpus:
            skip(f"{name} - needs {NOTICES_DB.name}")
            continue
        print(f"\n{name}")
        if fn.__code__.co_argcount:
            fn(con)
        else:
            fn()
    if con is not None:
        con.close()

    print(f"\n{PASSED} passed, {FAILED} failed, {SKIPPED} skipped")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
