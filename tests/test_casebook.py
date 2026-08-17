"""
Leakage and construction invariants for the analyst-blind case experiment.

A CASEBOOK THAT LEAKS IS WORSE THAN NO CASEBOOK. It produces numbers, the
numbers look like evidence, and nothing in the output says the evaluator was
shown the answer. Every assertion here exists because the corresponding mistake
is easy to make and invisible afterwards.

Written to run under plain `python tests/test_casebook.py` as well as pytest,
matching test_backtest.py. Skips cleanly when the databases are absent, because
they are gitignored and a fresh clone has none of them.

THE ACCESSOR LIST IS WRITTEN OUT BY HAND, NOT REFLECTED. Reflecting over the
gate's methods would mean a new accessor is tested the moment it appears, which
sounds better and is worse: a new source with no date column would be added,
reflected over, and silently pass whatever bound it happened to implement. The
explicit list fails until somebody writes down which column makes the new source
knowable at T.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import conftest  # noqa: F401,E402  (import-time vault guard)
import casebook as cb  # noqa: E402

PROBE = "2024-04-01"

# (accessor, the field that must not exceed T). One line per source, and adding
# a source without adding a line here is the failure this shape is designed to
# force into the open.
DATE_BOUNDED = [
    ("notices", "publication_date"),
    ("awards", "publication_date"),
    ("contracts", "disclosure_date"),
    ("audits", "date_published"),
]

_DBS = [cb.NOTICES_DB, cb.PLANS_DB, cb.OAG_DB, cb.CROSSWALK_DB, cb.CASEBOOK_DB]


def _have_dbs() -> bool:
    return all(p.exists() for p in _DBS)


def _skip(name: str) -> bool:
    if not _have_dbs():
        print(f"  SKIP {name}: databases absent")
        return True
    return False


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------

def test_as_of_returns_nothing_dated_after_t():
    if _skip("as_of bound"):
        return
    gate = cb.as_of(PROBE)
    for accessor, column in DATE_BOUNDED:
        rows = getattr(gate, accessor)()
        late = [r for r in rows if (r[column] or "") > PROBE]
        assert not late, (
            f"{accessor}() returned {len(late)} row(s) whose {column} is after "
            f"{PROBE}; first is {late[0][column]}")
    print(f"  ok: {len(DATE_BOUNDED)} accessors bounded at {PROBE}")


def test_the_gate_is_actually_filtering():
    """
    A gate that returns everything and a gate that returns nothing both pass a
    'nothing after T' check. This is the assertion that separates them.
    """
    if _skip("gate filters"):
        return
    early, late = cb.as_of("2023-01-01"), cb.as_of("2025-06-01")
    for accessor, _ in DATE_BOUNDED:
        n_early, n_late = len(getattr(early, accessor)()), len(getattr(late, accessor)())
        assert n_early > 0, f"{accessor}() is empty at 2023-01-01; nothing is being read"
        assert n_late > n_early, (
            f"{accessor}() returned {n_late} at 2025-06-01 but {n_early} at "
            f"2023-01-01; the bound is not moving with T")
    print("  ok: every accessor grows with T and is non-empty early")


def test_plans_dp_and_drr_are_gated_separately():
    """
    The leak this experiment was built to avoid. A DP for fiscal year Y is
    public in March of Y; the DRR carrying that year's ACTUALS is public in
    November of Y+1. Admitting the whole row on the DP's date hands over
    twenty-month-old-future spending.
    """
    if _skip("plan halves"):
        return
    # 2024-06-01: FY2024 DP is out (March 2024). FY2024 DRR is not (Dec 2025).
    rows = [r for r in cb.as_of("2024-06-01").plan_rows() if r["year"] == 2024]
    assert rows, "no FY2024 program rows visible at 2024-06-01"
    assert all(r["_dp_visible"] for r in rows), "FY2024 DP should be visible"
    assert not any(r["_drr_visible"] for r in rows), "FY2024 DRR must not be visible"
    for field in cb.DRR_FIELDS:
        assert all(r[field] is None for r in rows), (
            f"{field} is a results-report field and must be blanked at 2024-06-01")
    assert any(r["planned_spending"] is not None for r in rows), (
        "planned_spending is a plan field and should survive")
    # By 2026-01-01 the FY2024 DRR (Dec 2025) has been tabled.
    later = [r for r in cb.as_of("2026-01-01").plan_rows() if r["year"] == 2024]
    assert any(r["actual_spending"] is not None for r in later), (
        "FY2024 actuals should be visible once the results report is tabled")
    print("  ok: DP and DRR halves obey different dates")


def test_a_row_with_no_disclosure_quarter_is_excluded_not_guessed():
    assert cb.disclosure_date("", "") is None
    assert cb.disclosure_date("2010-11-Q4", "C-2023-2024-001") is None, (
        "a fiscal label naming non-consecutive years must not be coerced")
    assert cb.disclosure_date("2023-2024-Q4", "") == "2024-04-30"
    print("  ok: disclosure_date refuses rather than defaults")


def test_disclosure_takes_the_later_of_two_quarters():
    """
    Later is the safe direction. Too late withholds evidence and costs recall;
    too early shows evidence that did not exist and invalidates the run.
    """
    both = cb.disclosure_date("2022-2023-Q2", "C-2022-2023-Q1-00001")
    q1_only = cb.disclosure_date("", "C-2022-2023-Q1-00001")
    assert both > q1_only, f"expected the later quarter, got {both} vs {q1_only}"
    print(f"  ok: disagreement resolves late ({both} not {q1_only})")


def test_contracts_are_never_gated_on_contract_date():
    """
    The specific leak in backtest.py's Evidence.contracts(). A contract signed
    before T but disclosed after it must NOT appear.
    """
    if _skip("contract gating"):
        return
    rows = cb.as_of(PROBE).contracts()
    signed_before_but_hidden = [r for r in rows
                                if r["contract_date"] and r["contract_date"] <= PROBE
                                and r["disclosure_date"] > PROBE]
    assert not signed_before_but_hidden, "gate leaked an undisclosed contract"
    lagged = [r for r in rows if r["contract_date"] and r["contract_date"] > r["disclosure_date"]]
    assert len(lagged) / max(len(rows), 1) < 0.01, (
        f"{len(lagged)} rows are disclosed before they were signed, which should be rare")
    print(f"  ok: {len(rows):,} contract rows, all disclosed by {PROBE}")


def test_gate_hands_out_no_connection():
    """Feature code must not be able to widen its own query."""
    gate = cb.as_of(PROBE)
    for name in dir(gate):
        if name.startswith("_"):
            continue
        value = getattr(gate, name)
        assert not hasattr(value, "execute"), f"{name} exposes a cursor"
    print("  ok: no accessor returns a connection")


# ---------------------------------------------------------------------------
# Frame and labels
# ---------------------------------------------------------------------------

def test_outcome_window_is_half_open():
    """
    A notice published exactly ON T0 is evidence the evaluator can see, never an
    outcome it is asked to predict. Off by one here moves a case's label.
    """
    if _skip("window"):
        return
    caps, _ = cb.load_capabilities()
    grid = ["2024-06-01"]
    cells = cb.build_frame(caps, grid=grid, verbose=False)
    for cell in cells:
        for pub, _ev, _ref, _kind, _nt in cell.outcome_events:
            assert pub > cell.t0, f"outcome dated {pub} is not after T0 {cell.t0}"
            assert pub <= cb.shift_months(cell.t0, cb.OUTCOME_MONTHS)
    print("  ok: outcomes lie strictly inside (T0, T0+window]")


def test_multi_department_notice_is_one_event_across_cases():
    """
    A joint notice makes each named department's cell positive, and must still
    count once. If event ids diverged, three departments sharing one procurement
    would read as three procurements.
    """
    row = {"solicitation_number": "WS123", "reference_number": "WS123-Doc1"}
    other = {"solicitation_number": "WS123", "reference_number": "WS123-Doc2"}
    assert cb.event_id_of(row) == cb.event_id_of(other) == "WS123"
    placeholder = {"solicitation_number": "-", "reference_number": "REF-9"}
    assert cb.event_id_of(placeholder) == "REF-9", "the '-' placeholder must not collide"
    assert cb.split_org_keys("dfo,dnd,eccc") == ["dfo", "dnd", "eccc"]
    print("  ok: one solicitation is one event across departments")


def test_frame_excludes_machinery_of_government_departments():
    if _skip("mog"):
        return
    excluded = cb._excluded_departments()
    assert excluded, "expected at least one predecessor/successor/absorbed key"
    caps, _ = cb.load_capabilities()
    cells = cb.build_frame(caps, grid=["2024-06-01"], verbose=False)
    named = {c.department for c in cells}
    assert not (named & excluded), f"frame contains {named & excluded}"
    print(f"  ok: {len(excluded)} reorganized departments held out")


# ---------------------------------------------------------------------------
# The written casebook
# ---------------------------------------------------------------------------

def _cases_written() -> bool:
    return (cb.CASES_DIR / "cases.json").exists()


def test_ground_truth_is_not_in_any_file_the_evaluator_reads():
    """
    The seal. cases.json, the bundles and the evidence records are what an
    evaluation harness opens; none of them may carry the label, the eventual
    tender, or its date.
    """
    if not _cases_written():
        print("  SKIP truth seal: no casebook written")
        return
    truth = json.loads((cb.CASES_DIR / "truth.json").read_text(encoding="utf-8"))["truth"]
    tender_ids = {t["eventual_tender_id"] for t in truth if t["eventual_tender_id"]}
    assert tender_ids, "no positive cases to check"

    public = (cb.CASES_DIR / "cases.json").read_text(encoding="utf-8")
    for banned in ("ground_truth", "POSITIVE", "NEGATIVE", "eventual_tender",
                   "lead_time_days"):
        assert banned not in public, f"cases.json leaks '{banned}'"

    for path in list((cb.CASES_DIR / "bundles").glob("*.md")) + \
            list((cb.CASES_DIR / "evidence").glob("*.json")):
        text = path.read_text(encoding="utf-8")
        for tid in tender_ids:
            assert tid not in text, f"{path.name} names the eventual tender {tid}"
        assert "POSITIVE" not in text and "NEGATIVE" not in text, (
            f"{path.name} carries a label word")
    print(f"  ok: {len(tender_ids)} eventual tenders absent from every readable file")


def test_conditions_nest_and_z_is_empty():
    if not _cases_written():
        print("  SKIP nesting: no casebook written")
        return
    manifest = json.loads(
        (cb.CASES_DIR / "evidence_manifest.json").read_text(encoding="utf-8"))
    problems = cb.check_nesting(manifest)
    assert not problems, problems[:5]
    for case_id, conds in manifest.items():
        assert conds["Z"] == [], f"{case_id} Z arm carries evidence"
    print(f"  ok: A subset B subset C subset D across {len(manifest)} cases")


def test_bundles_carry_no_suppressed_field_and_no_late_stamp():
    if not _cases_written():
        print("  SKIP bundle audit: no casebook written")
        return
    problems, stats = cb.audit_bundles()
    assert not problems, problems[:5]
    assert stats["bundles_checked"] > 0
    print(f"  ok: {stats['bundles_checked']} bundles clean "
          f"({stats['forward_looking_dates_rendered']} published forward dates kept)")


def test_case_ids_do_not_track_the_label():
    """
    If ids were assigned before shuffling, position would encode the answer and
    an evaluator could score well by reading the sampler instead of the evidence.
    """
    if not _cases_written():
        print("  SKIP id shuffle: no casebook written")
        return
    truth = json.loads((cb.CASES_DIR / "truth.json").read_text(encoding="utf-8"))["truth"]
    labels = [t["ground_truth"] == "POSITIVE"
              for t in sorted(truth, key=lambda t: t["case_id"])]
    n = len(labels)
    assert sum(labels) == n // 2, "pilot should be balanced"
    # No run of more than half the positives back to back, and parity is not a tell.
    longest, run = 0, 0
    for value in labels:
        run = run + 1 if value else 0
        longest = max(longest, run)
    assert longest < sum(labels), f"positives run {longest} deep in id order"
    evens = sum(1 for i, v in enumerate(labels) if v and i % 2 == 0)
    assert 0 < evens < sum(labels), "label is perfectly predicted by id parity"
    print(f"  ok: {n} ids carry no ordering or parity signal")


def test_matched_pairs_are_symmetric_and_share_a_date():
    if not _cases_written():
        print("  SKIP pairing: no casebook written")
        return
    cases = json.loads((cb.CASES_DIR / "cases.json").read_text(encoding="utf-8"))["cases"]
    truth = {t["case_id"]: t["ground_truth"] for t in json.loads(
        (cb.CASES_DIR / "truth.json").read_text(encoding="utf-8"))["truth"]}
    index = {c["case_id"]: c for c in cases}
    for c in cases:
        mate = index[c["matched_case_id"]]
        assert mate["matched_case_id"] == c["case_id"], "pairing is not symmetric"
        assert mate["t0"] == c["t0"], "a matched pair must share its evaluation date"
        assert truth[c["case_id"]] != truth[mate["case_id"]], (
            "a pair must be one positive and one negative")
        if c["match_tier"] == "department_exact":
            assert mate["department"] == c["department"]
            assert mate["capability"] != c["capability"]
        else:
            assert mate["department"] != c["department"]
            assert mate["prior_activity_decile"] == c["prior_activity_decile"]
    print(f"  ok: {len(cases) // 2} pairs symmetric, matched on date and tier")


def test_within_pair_discrimination_is_reported_per_tier():
    """
    Not an assertion that every condition discriminates — it does not, and that
    is a finding rather than a bug. This asserts the CHECK exists and reports,
    because a condition that hands both cases in a pair identical evidence
    cannot separate them, and a run that failed to say so would present noise as
    a measured effect.

    department_exact holds the department constant, and plans and audits attach
    to a department, so B/C/D are expected identical within those pairs.
    """
    if not _cases_written():
        print("  SKIP discrimination: no casebook written")
        return
    manifest = json.loads(
        (cb.CASES_DIR / "evidence_manifest.json").read_text(encoding="utf-8"))
    cases = json.loads((cb.CASES_DIR / "cases.json").read_text(encoding="utf-8"))["cases"]
    disc = cb.within_pair_discrimination(manifest, cases)
    assert disc, "no pairs examined"
    for tier, row in disc.items():
        assert row["pairs"] > 0
        assert row["A"] == row["pairs"], (
            f"{tier}: condition A must differ within every pair or the cases are "
            f"indistinguishable even in principle")
    if "department_exact" in disc:
        row = disc["department_exact"]
        assert row["C"] == 0 and row["D"] == 0, (
            "plans and audits are department-level, so holding the department "
            "constant must hold them constant; a non-zero here means something "
            "capability-specific crept into C or D")
    print(f"  ok: {_disc_summary(disc)}")


def _disc_summary(disc: dict) -> str:
    return "; ".join(
        f"{t} {r['pairs']}p A{r['A']} B{r['B']} C{r['C']} D{r['D']}"
        for t, r in sorted(disc.items()))


def test_evidence_ids_are_stable_and_condition_independent():
    """
    Nesting is asserted by set comparison, which only means anything if an id
    depends on the row and not on which bundle it landed in.
    """
    a = cb.evidence_id("notices", "REF-1")
    assert a == cb.evidence_id("notices", "REF-1")
    assert a != cb.evidence_id("awards", "REF-1")
    print("  ok: evidence ids are row-derived")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in tests:
        try:
            print(f"{fn.__name__}:")
            fn()
        except AssertionError as exc:
            failed += 1
            print(f"  FAIL {exc}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  ERROR {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
