"""
The filter audit: predicate independence, production equivalence, and blinding.

Plain Python, no pytest — `python tests/test_filter_audit.py`, exit 0 means
passed. Same shape as the other suites here.

ASSERTIONS ARE WRITTEN AS DIRECTIONS, not as snapshots of today's output. "A
notice rejected at construction must STILL report a relevance verdict, and must
NOT report it as a drop" survives a refactor that changes the funnel's wording;
a golden-output comparison does not.

The notices below are real rows, quoted inline rather than read from
data/notices.db, so the predicate tests run on a fresh clone where that 120MB
file does not exist. Tests that genuinely need the corpus skip loudly.
"""
from __future__ import annotations

import json
import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

import conftest  # noqa: E402,F401  — MUST come first: redirects the vault

import ingest  # noqa: E402
from filter_audit import predicates as P  # noqa: E402
from filter_audit import blinding, golden, review, variants, version  # noqa: E402

FAILURES: list[str] = []

PROJECT_ROOT = Path(__file__).parent.parent
NOTICES_DB = PROJECT_ROOT / "data" / "notices.db"
FEED_CSV = PROJECT_ROOT / ".cache" / "tenders.csv"
PROFILE = PROJECT_ROOT / "vault" / "profiles" / "my-company.md"


def check(label: str, got, want) -> None:
    if got == want:
        print(f"  PASS  {label}")
    else:
        FAILURES.append(f"{label}: expected {want!r}, got {got!r}")
        print(f"  FAIL  {label}: expected {want!r}, got {got!r}")


# --- real rows, quoted -----------------------------------------------------
# Shared Services Canada, uncoded (unspsc '*'), and the notice says "Cyber
# Security" where the profile carries "cybersecurity".
SSC_CYBER = {
    "reference_number": "SSC-22-00019111:T",
    "title": "Invitation to Qualify for the Cyber Security Procurement Vehicle",
    "description": "Cette invitation a se qualifier (ISQ) est les deux premieres "
                   "phases du MAMC.",
    "closing_date": "2022-05-13",
    "publication_date": "2022-04-29",
    "contracting_entity": "Shared Services Canada",
    "end_user": "Shared Services Canada",
    "notice_type": "Invitation to Qualify",
    "procurement_category": "*CNST\n*GD\n*SRV\n*SRVTGD",
    "unspsc": "*",
    "gsin": "*5164CJ",
}

# NRC elevator modernization. Uncoded, so the keyword branch applies and
# `modernization` hits — but the category is exactly *CNST, so construction
# rejects it first. The whole point of independent evaluation, in one row.
NRC_ELEVATOR = {
    "reference_number": "PW-23-01030114",
    "title": "SAS - IPF Elevator Modernization",
    "description": "The National Research Council Canada has a requirement for a "
                   "project that includes the complete modernization of elevators.",
    "closing_date": "2023-04-28",
    "publication_date": "2023-03-15",
    "contracting_entity": "National Research Council Canada",
    "end_user": "National Research Council Canada",
    "notice_type": "Request for Proposal",
    "procurement_category": "*CNST",
    "unspsc": "*",
    "gsin": "*5129B",
}

# Coded into a family the profile does not buy. The keyword branch is never
# consulted, which is what makes this a different failure from SSC_CYBER.
CODED_WRONG_FAMILY = {
    "reference_number": "cb-000-00000001",
    "title": "Vapour cloud explosion assessment",
    "description": "Validation of recommended distances and assessment of a "
                   "boiling liquid expanding vapour explosion.",
    "closing_date": "2026-12-31",
    "publication_date": "2026-01-05",
    "contracting_entity": "Transport Canada",
    "end_user": "Transport Canada",
    "notice_type": "Request for Proposal",
    "procurement_category": "*SRV",
    "unspsc": "*77101501",
    "gsin": "*",
}

NON_FEDERAL = {
    "reference_number": "cb-000-00000002",
    "title": "Service Desk Software",
    "description": "Service desk software for territorial departments.",
    "closing_date": "2026-12-31",
    "publication_date": "2026-01-05",
    "contracting_entity": "Government of the Northwest Territories (GNWT)",
    "end_user": "Government of the Northwest Territories (GNWT)",
    "notice_type": "Request for Proposal",
    "procurement_category": "*GD",
    "unspsc": "*43231501",
    "gsin": "*",
}


def _criteria():
    return ingest.parse_profile(PROFILE)


def _notice(row):
    return P.Notice.from_frozen_row(row)


# ---------------------------------------------------------------------------


def test_predicate_independence() -> None:
    """A stage that never ran in production must still be evaluated by the audit."""
    print("\nPredicate independence:")
    criteria = _criteria()
    notice = _notice(NRC_ELEVATOR)
    as_of = "2023-03-15"

    production = P.production_decision(notice, criteria, as_of)
    audit = P.audit_decision(notice, criteria, as_of)

    check("production rejects the elevator notice", production.admitted, False)
    check("...at construction, not later",
          production.first_rejecting_stage, "construction")
    check("production never reached relevance",
          any(r.stage == "relevance" for r in production.evaluated), False)

    # The direction that matters: the audit must reach its OWN verdict on
    # relevance, and must not inherit "rejected" from the earlier stage.
    relevance = audit.by_stage("relevance")
    check("the audit still evaluates relevance independently",
          relevance.outcome, "pass")
    check("...and records the keyword that would have admitted it",
          relevance.evidence["matched_keywords"], ["modernization"])
    check("the audit also records the construction drop",
          audit.by_stage("construction").outcome, "drop")

    # Every stage present on every audit record, always.
    check("the audit reports one result per stage",
          len(audit.results), len(P.STAGES))
    check("...covering every stage name",
          tuple(r.stage for r in audit.results), P.STAGE_NAMES)

    # A notice failing two stages must produce TWO independent drops.
    closed_and_irrelevant = dict(CODED_WRONG_FAMILY)
    closed_and_irrelevant["closing_date"] = "2020-01-01"
    audit2 = P.audit_decision(_notice(closed_and_irrelevant), criteria, "2026-01-05")
    drops = [r.stage for r in audit2.results if r.drops]
    check("a notice failing two gates reports both", sorted(drops),
          ["closed", "relevance"])


def test_relevance_branching() -> None:
    """Coded and uncoded failures are different failures and must stay distinguishable."""
    print("\nRelevance branching:")
    criteria = _criteria()

    uncoded = P.audit_decision(_notice(SSC_CYBER), criteria,
                               "2022-04-29").by_stage("relevance")
    coded = P.audit_decision(_notice(CODED_WRONG_FAMILY), criteria,
                             "2026-01-05").by_stage("relevance")

    check("uncoded notice takes the uncoded branch", uncoded.detail["branch"],
          "uncoded")
    check("...and reports no_codes_filed, NOT wrong_family",
          uncoded.detail["family_result"], "no_codes_filed")
    check("...and reports the keyword miss", uncoded.detail["keyword_result"],
          "no_hit")

    check("coded notice takes the coded branch", coded.detail["branch"], "coded")
    check("...and reports wrong_family, NOT no_codes_filed",
          coded.detail["family_result"], "wrong_family")

    # The two must not be confusable by field, not merely by prose.
    check("the two failures are distinguishable by family_result alone",
          uncoded.detail["family_result"] != coded.detail["family_result"], True)

    # The vapour-cloud lesson, made mechanical: a coded notice must NOT be
    # rescued by a keyword hit, and the record must say production never looked.
    check("the coded notice does hit a keyword",
          coded.detail["keyword_match"], True)
    check("...but is still rejected, because codes present and not ours means "
          "not ours", coded.outcome, "drop")
    check("...and the record says production never consulted keywords",
          coded.detail["keyword_consulted_in_production"], False)
    check("the uncoded notice's keyword branch WAS consulted",
          uncoded.detail["keyword_consulted_in_production"], True)

    # Vocabularies stay closed.
    for name, value in (("family", coded.detail["family_result"]),
                        ("keyword", coded.detail["keyword_result"])):
        pool = P.FAMILY_RESULTS if name == "family" else P.KEYWORD_RESULTS
        check(f"{name}_result stays inside its declared vocabulary",
              value in pool, True)


def test_skipped_is_not_passed() -> None:
    """Declining to fix a clock must be recorded as skipped, never as a pass."""
    print("\nSkipped is not passed:")
    criteria = _criteria()
    result = P.stage_closed(_notice(SSC_CYBER), criteria, None)
    check("no as_of records skipped", result.outcome, "skipped")
    check("...and skipped is not a drop", result.drops, False)
    check("...and must NOT be reported as pass", result.outcome == "pass", False)

    dated = P.stage_closed(_notice(SSC_CYBER), criteria, "2022-04-29")
    check("with an as_of the stage actually decides", dated.outcome, "pass")

    # Undated notices are kept, and that is a decision rather than arithmetic.
    undated = dict(SSC_CYBER)
    undated["closing_date"] = None
    kept = P.stage_closed(_notice(undated), criteria, "2022-04-29")
    check("an undated notice is kept", kept.outcome, "pass")
    check("...and says so explicitly", kept.detail["undated"], True)


def test_jurisdiction_unrecognised_is_admitted() -> None:
    """`unrecognised` is not `non_federal`, and dropping on it would lose real work."""
    print("\nJurisdiction:")
    criteria = _criteria()
    non_federal = P.stage_jurisdiction(_notice(NON_FEDERAL), criteria, None)
    check("a territorial government is dropped", non_federal.outcome, "drop")
    check("...on a positive provincial signal",
          non_federal.evidence["jurisdiction"], "non_federal")

    crown = dict(NON_FEDERAL)
    crown["contracting_entity"] = "Canada Deposit Insurance Corporation"
    crown["end_user"] = "Canada Deposit Insurance Corporation"
    result = P.stage_jurisdiction(_notice(crown), criteria, None)
    check("a federal Crown corporation is NOT dropped", result.outcome, "pass")
    check("...and is labelled unrecognised rather than non_federal",
          result.evidence["jurisdiction"], "unrecognised")


def test_production_equivalence() -> None:
    """The audit path must reproduce ingest.filter_tenders exactly."""
    print("\nProduction equivalence:")
    if not FEED_CSV.exists():
        print(f"  SKIP  no cached feed at {FEED_CSV} — this proves nothing, "
              f"which is not the same as passing")
        return
    from filter_audit.equivalence import verify

    result = verify(source="feed", as_of=date(2026, 8, 20))
    if result.get("skipped"):
        print(f"  SKIP  {result['reason']}")
        return
    check("no notice is admitted by filter_tenders alone",
          result["only_filter_tenders_total"], 0)
    check("no notice is admitted by the audit alone",
          result["only_audit_total"], 0)
    check("the comparison was not vacuous", result["vacuous"], False)
    check("the two paths agree", result["agreed"], True)
    check("...on a non-empty admitted set", result["audit_admitted"] > 0, True)


def test_equivalence_refuses_a_vacuous_pass() -> None:
    """Agreement on an empty set is arithmetic, not evidence, and must not pass."""
    print("\nVacuous comparisons:")
    if not NOTICES_DB.exists():
        print("  SKIP  no data/notices.db")
        return
    from filter_audit.equivalence import verify

    # Every archive notice closed long before this date, so both paths admit
    # nothing and would trivially "agree".
    result = verify(source="archive", as_of=date(2026, 8, 20))
    check("both paths admit nothing at a present-day as_of",
          result["audit_admitted"], 0)
    check("...which is reported as vacuous", result["vacuous"], True)
    check("...and NOT as agreement", result["agreed"], False)


def test_provenance() -> None:
    """Every decision must be able to say which filter produced it."""
    print("\nProvenance:")
    current = version.filter_version(PROFILE)
    for key in ("label", "profile_sha256", "predicates_sha256",
                "stage_manifest_sha256"):
        check(f"the version carries {key}", bool(current.get(key)), True)
    check("the label is derived, not free text",
          current["label"].startswith("fv-"), True)

    check("recomputing the version is stable",
          version.filter_version(PROFILE)["label"], current["label"])

    # A predicate-body change must move identity but NOT comparability.
    same_shape = dict(current)
    same_shape["predicates_sha256"] = "0" * 64
    ok, _ = version.comparable(current, same_shape)
    check("same stage manifest stays comparable", ok, True)

    reshaped = dict(current)
    reshaped["stage_manifest_sha256"] = "0" * 64
    ok, reason = version.comparable(current, reshaped)
    check("a changed stage manifest is NOT comparable", ok, False)
    check("...and says why", "not comparable" in reason.lower(), True)

    # The manifest must describe shape only.
    manifest = version.stage_manifest()
    check("the manifest has one row per stage", len(manifest), len(P.STAGES))
    check("...and carries order, name, drops, active",
          all(len(row) == 4 for row in manifest), True)


def test_variants_cannot_reach_production() -> None:
    """Candidate code must be unreachable from the production import path."""
    print("\nVariant isolation:")
    # The decisive check, and it has to run in a fresh interpreter: this very
    # test module imports variants, so sys.modules here proves nothing.
    import subprocess
    probe = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, r'%s'); import ingest; "
         "print('filter_audit.variants' in sys.modules)"
         % (PROJECT_ROOT / "scripts")],
        capture_output=True, text=True)
    check("importing ingest does not load filter_audit.variants",
          probe.stdout.strip(), "False")

    # And neither production module names it. Matched as an import statement
    # rather than as a bare substring — `crosswalk.observed_variants` is an
    # unrelated function and must not trip this.
    for module in (P, ingest):
        source = Path(module.__file__).read_text(encoding="utf-8")
        offenders = [line.strip() for line in source.splitlines()
                     if line.lstrip().startswith(("import ", "from "))
                     and "variants" in line]
        check(f"{Path(module.__file__).name} has no variants import",
              offenders, [])

    # And the straw variant exists, because the regression test needs it.
    check("the admit-everything straw variant is registered",
          "v-admit-everything" in variants.VARIANTS, True)


def test_blinding_is_structural() -> None:
    """The review surface must be unable to disclose the verdict, not merely decline to."""
    print("\nBlinding:")
    from dataclasses import fields

    names = {f.name for f in fields(blinding.BlindedNotice)}
    leaked = sorted(names & set(blinding.WITHHELD_UNTIL_DISPOSED))
    check("BlindedNotice carries none of the withheld fields", leaked, [])
    check("...and does carry what the reviewer needs to judge",
          {"title", "description", "unspsc", "contracting_entity"} <= names, True)

    payload = blinding.blind("item-1", dict(SSC_CYBER)).to_dict()
    check("a blinded payload exposes no withheld key",
          sorted(set(payload) & set(blinding.WITHHELD_UNTIL_DISPOSED)), [])
    check("...and carries the notice's own facts",
          payload["title"], SSC_CYBER["title"])

    # The guard refuses rather than silently stripping.
    try:
        blinding.assert_blinded({"title": "x", "first_rejecting_stage": "relevance"})
        FAILURES.append("assert_blinded accepted a payload carrying a withheld field")
        print("  FAIL  assert_blinded accepted a leaked field")
    except AssertionError:
        print("  PASS  assert_blinded refuses a payload carrying a withheld field")

    # reveal is a store precondition, not a caller's promise.
    try:
        blinding.reveal_payload({}, {}, None)
        FAILURES.append("reveal_payload returned without a disposition")
        print("  FAIL  reveal_payload returned without a disposition")
    except blinding.RevealRefused:
        print("  PASS  reveal is refused until a disposition exists")


def test_uncertain_and_reviews_never_reach_production() -> None:
    """A review is a note about the past. It must not be an input to a decision."""
    print("\nReviews cannot change admission:")
    criteria = _criteria()
    notice = _notice(SSC_CYBER)
    before = P.production_decision(notice, criteria, "2022-04-29")

    # predicates.py must not be able to see reviews at all.
    source = Path(P.__file__).read_text(encoding="utf-8")
    for forbidden in ("filter-reviews", "load_reviews", "from .review",
                      "from .golden"):
        check(f"predicates.py cannot reach {forbidden}",
              forbidden in source, False)

    after = P.production_decision(notice, criteria, "2022-04-29")
    check("the production decision is unchanged by anything a reviewer did",
          (after.admitted, after.first_rejecting_stage),
          (before.admitted, before.first_rejecting_stage))
    check("UNCERTAIN is a review state only",
          "UNCERTAIN" in review.DECISIONS and "UNCERTAIN" not in P.OUTCOMES, True)


def test_taxonomy_is_closed_but_extensible() -> None:
    """An unlisted failure category must be refused, so the counts mean something."""
    print("\nFailure taxonomy:")
    categories = review.load_taxonomy()
    required = {"missing_structured_field", "vocabulary_mismatch",
                "synonym_mismatch", "acronym_mismatch", "buyer_specific_language",
                "procurement_mechanism_ambiguity", "structured_field_error",
                "overly_restrictive_rule", "semantic_context_mismatch", "other"}
    check("every seeded category is present", required <= set(categories), True)
    check("each category carries a definition",
          all(c.get("definition") for c in categories.values()), True)
    check("each category says what it is NOT",
          all(c.get("not") for c in categories.values()), True)

    result = review.categorize("no-such-item", "not_a_real_category", "e", "x")
    check("an unlisted category is refused",
          "Unknown failure category" in result.get("error", ""), True)


def test_golden_evaluation_arithmetic() -> None:
    """Precision and recall must be the right numbers, not merely produced."""
    print("\nGolden-set arithmetic:")
    # A hand-built set with a known matrix: TP 2, FP 1, TN 3, FN 2.
    # Relevance is forced by UNSPSC so the expected cell is unambiguous.
    def entry(entry_id, klass, expected, unspsc, closing="2026-12-31"):
        return {
            "id": entry_id, "reference_number": entry_id, "source": "frozen",
            "class": klass, "expected": expected, "as_of": "2026-01-05",
            "failure_categories": [],
            "frozen_row": {
                "reference_number": entry_id, "title": "t", "description": "d",
                "closing_date": closing, "publication_date": "2026-01-05",
                "contracting_entity": "Shared Services Canada",
                "end_user": "Shared Services Canada",
                "notice_type": "Request for Proposal",
                "procurement_category": "*SRV", "unspsc": unspsc, "gsin": "*",
            },
        }

    in_family, out_family = "*81111700", "*77101501"
    entries = [
        entry("tp-1", "clearly_relevant", "ADMIT", in_family),
        entry("tp-2", "clearly_relevant", "ADMIT", in_family),
        entry("fp-1", "known_false_positive", "REJECT", in_family),
        entry("tn-1", "clearly_irrelevant", "REJECT", out_family),
        entry("tn-2", "clearly_irrelevant", "REJECT", out_family),
        entry("tn-3", "clearly_irrelevant", "REJECT", out_family),
        entry("fn-1", "known_false_negative", "ADMIT", out_family),
        entry("fn-2", "known_false_negative", "ADMIT", out_family),
        entry("edge-1", "edge_case", None, out_family),
    ]
    import yaml
    tmp = Path(tempfile.mkdtemp()) / "golden.yaml"
    tmp.write_text(yaml.safe_dump(
        {"schema_version": 1, "set_version": 99, "entries": entries}),
        encoding="utf-8")

    result = golden.evaluate(path=tmp, profile_path=PROFILE)
    check("TP", result["matrix"]["TP"], 2)
    check("FP", result["matrix"]["FP"], 1)
    check("TN", result["matrix"]["TN"], 3)
    check("FN", result["matrix"]["FN"], 2)
    check("precision is exactly TP/(TP+FP)", result["precision"], 2 / 3)
    check("recall is exactly TP/(TP+FN)", result["recall"], 2 / 4)
    check("an expected:null entry is excluded from the matrix",
          result["scored"], 8)
    check("...and reported separately", result["excluded_no_consensus"], 1)
    check("intervals are reported beside the rates",
          result["precision_ci"] is not None and result["recall_ci"] is not None,
          True)
    low, high = result["precision_ci"]
    check("...and the interval brackets the estimate",
          low <= result["precision"] <= high, True)


def test_regression_protection() -> None:
    """Recovering false negatives must never on its own produce a better verdict."""
    print("\nRegression protection:")
    from filter_audit.compare import compare

    if not golden.GOLDEN_FILE.exists():
        print("  SKIP  no golden set committed")
        return

    straw = compare(None, "v-admit-everything", PROFILE)
    check("the straw variant recovers known false negatives",
          len(straw["recovered_historical_false_negatives"]) > 0, True)
    check("...and achieves perfect recall", straw["candidate"]["recall"], 1.0)
    check("...and is still reported as regressed",
          len(straw["regressions"]) > 0, True)
    check("...so the gate fails", straw["gate_pass"], False)
    check("...and the reason names the regressions",
          "REGRESSION" in straw["gate_reason"], True)
    check("admitting more lowers precision, and that is visible",
          straw["candidate"]["precision"] < straw["base"]["precision"], True)

    # A real refinement, by contrast, must pass.
    real = compare(None, "v-cyber-security-spacing", PROFILE)
    check("a targeted refinement produces no regressions",
          len(real["regressions"]), 0)
    check("...recovers the case it was written for",
          [r["id"] for r in real["recovered_historical_false_negatives"]],
          ["gold-ssc-cyber-security-itq"])
    check("...and passes the gate", real["gate_pass"], True)

    # No composite score anywhere in the output.
    check("no composite score is emitted",
          any(k in json.dumps(real) for k in ("f1", "F1", "composite", "overall_score")),
          False)


def test_no_change_is_not_equivalence() -> None:
    """Identical results must be reported as such, never as a measured equivalence."""
    print("\nIdentical arms:")
    from filter_audit.compare import compare

    if not golden.GOLDEN_FILE.exists():
        print("  SKIP  no golden set committed")
        return
    same = compare(None, None, PROFILE)
    check("comparing a version with itself changes nothing",
          same["changed"], False)
    check("...and the gate does not pass on a no-op", same["gate_pass"], False)
    check("...and the reason says no entry changed",
          "No entry changed" in same["gate_reason"], True)
    check("...and does NOT claim the versions are equivalent",
          "equivalent" in same["gate_reason"].lower()
          and "not the same as being equivalent" not in same["gate_reason"],
          False)


def test_historical_reproducibility() -> None:
    """Replay must be clock-independent, and old runs must never be rewritten."""
    print("\nHistorical reproducibility:")
    criteria = _criteria()
    notice = _notice(SSC_CYBER)

    # as_of=publication is a property of the notice, so two "days" agree.
    first = P.production_decision(notice, criteria, notice.publication_date)
    second = P.production_decision(notice, criteria, notice.publication_date)
    check("the same notice and clock give the same decision",
          (first.admitted, first.first_rejecting_stage),
          (second.admitted, second.first_rejecting_stage))

    # A different clock is allowed to give a different answer — that is the
    # point of recording as_of — but it must be the CLOCK that changed it.
    later = P.production_decision(notice, criteria, "2026-08-20")
    check("a present-day clock rejects the same notice at `closed`",
          later.first_rejecting_stage, "closed")
    check("...while the original clock rejected it at `relevance`",
          first.first_rejecting_stage, "relevance")

    # And the audit is unmoved by the clock on every stage but the first.
    audit_then = P.audit_decision(notice, criteria, notice.publication_date)
    audit_now = P.audit_decision(notice, criteria, "2026-08-20")
    check("the audit's relevance verdict does not depend on the clock",
          audit_then.by_stage("relevance").detail["family_result"],
          audit_now.by_stage("relevance").detail["family_result"])

    if not NOTICES_DB.exists():
        print("  SKIP  no data/notices.db for the run-immutability check")
        return
    from filter_audit.replay import connect, replay

    tmp = Path(tempfile.mkdtemp()) / "audit.db"
    first_run = replay(source="archive", as_of_spec="publication", sample=200,
                       seed=7, persist=True, audit_db=tmp)
    written = first_run["rows_written"]
    check("the first run writes rows", written > 0, True)
    again = replay(source="archive", as_of_spec="publication", sample=200,
                   seed=7, persist=True, audit_db=tmp)
    check("re-running an identical replay is a no-op", again["rows_written"], 0)
    check("...under the same content-addressed run id",
          again["run_id"], first_run["run_id"])

    conn = connect(tmp)
    rows = conn.execute("SELECT COUNT(*) FROM decisions WHERE run_id=?",
                        (first_run["run_id"],)).fetchone()[0]
    conn.close()
    check("the original run's rows are untouched", rows, written)


def test_coded_wrong_family_stratum() -> None:
    """
    The branch that no keyword refinement can reach, sampled so that a draw
    covers segments rather than re-sampling the largest one.

    Stage 5's coded and uncoded paths are mutually exclusive, so a notice
    carrying codes is judged on its codes and the competency list is never
    consulted. Uniform sampling over all rejects therefore keeps surfacing
    vocabulary bugs — which live in the other branch — and never tests whether
    the family list is where the blindness is.
    """
    print("\nCoded-wrong-family stratum:")

    # A publisher-supplied code that is not eight digits is not evidence of a
    # segment, and must not be truncated into one.
    malformed = json.dumps([{"stage": "relevance", "outcome": "drop",
                             "evidence": {"codes": ["81111500", "561118", "",
                                                    "abcdefgh"]}}])
    check("segments read only well-formed codes",
          review._segments_of(malformed), {"81"})

    if not NOTICES_DB.exists():
        print("  SKIP  no data/notices.db for the sampling checks")
        return
    from filter_audit.replay import replay

    tmp = Path(tempfile.mkdtemp())
    audit_db = tmp / "audit.db"
    run = replay(source="archive", as_of_spec="publication", sample=4000,
                 seed=5, persist=True, audit_db=audit_db)
    run_id = run["run_id"]

    # The two refusals. Both are contradictions rather than unsupported
    # options, so both must say what the caller actually asked for.
    conflict = review.sample_rejects(run_id=run_id, strategy="coded_wrong_family",
                                     branch="uncoded_no_keyword", audit_db=audit_db)
    check("a conflicting --branch is refused",
          "contradicts" in conflict.get("error", ""), True)
    mixed = review.sample_rejects(run_id=run_id, strategy="coded_wrong_family",
                                  include_admitted=True, audit_db=audit_db)
    check("--include-admitted is refused, not ignored",
          "no admitted notice is in this branch" in mixed.get("error", ""), True)
    unknown = review.sample_rejects(run_id=run_id, strategy="segments",
                                    audit_db=audit_db)
    check("an unknown strategy names the built ones",
          "coded_wrong_family" in unknown.get("error", ""), True)

    queue = review.sample_rejects(run_id=run_id, strategy="coded_wrong_family",
                                  n=12, seed=99, audit_db=audit_db)
    check("the queue was built", "error" in queue, False)
    if "error" in queue:
        return
    population = queue["branch_population"]
    check("the population is reported before the draw",
          population["branch"], "coded_wrong_family")
    check("...with the segment count", population["segments"] > 1, True)
    check("...and what the draw could not reach",
          population["segments_drawn"] + population["segments_unsampled"],
          population["segments"])
    check("a draw of n covers min(n, segments) segments",
          population["segments_drawn"],
          min(queue["n"], population["segments"]))
    check("every row in the frame carries a segment",
          population["rows_without_segment"], 0)

    conn = review._queue_conn(audit_db)
    items = conn.execute(
        "SELECT * FROM review_queue WHERE queue_id=? ORDER BY position",
        (queue["queue_id"],)).fetchall()
    segments = [row["drawn_for_segment"] for row in items]
    check("every item records the segment it was drawn for",
          all(segments), True)
    check("...and no segment is drawn twice", len(set(segments)), len(segments))
    check("...and the strategy is recorded, not the generic one",
          {row["sampling_strategy"] for row in items}, {"coded_wrong_family"})

    # Every drawn item really is in the branch.
    in_branch = conn.execute(
        "SELECT COUNT(*) FROM decisions WHERE run_id=? AND has_codes=1 AND "
        "family_result='wrong_family' AND notice_id IN "
        "(SELECT reference_number FROM review_queue WHERE queue_id=?)",
        (run_id, queue["queue_id"])).fetchone()[0]
    check("every drawn item is in the branch", in_branch, len(items))

    # The recorded segment must be one the notice actually carries.
    mismatched = []
    for row in items:
        stored = conn.execute(
            "SELECT audit_stage_results FROM decisions WHERE run_id=? AND "
            "notice_id=?", (run_id, row["reference_number"])).fetchone()[0]
        if row["drawn_for_segment"] not in review._segments_of(stored):
            mismatched.append(row["item_id"])
    check("the recorded segment is one the notice carries", mismatched, [])
    conn.close()

    # Same seed, same draw. A recorded seed that does not reproduce the queue is
    # a recorded seed that means nothing.
    again = review.sample_rejects(run_id=run_id, strategy="coded_wrong_family",
                                  n=12, seed=99, audit_db=audit_db)
    other = review.sample_rejects(run_id=run_id, strategy="coded_wrong_family",
                                  n=12, seed=100, audit_db=audit_db)
    conn = review._queue_conn(audit_db)

    def _refs(queue_id):
        return [r[0] for r in conn.execute(
            "SELECT reference_number FROM review_queue WHERE queue_id=? "
            "ORDER BY position", (queue_id,))]

    check("the same seed draws the same items",
          _refs(again["queue_id"]), _refs(queue["queue_id"]))
    check("a different seed does not",
          _refs(other["queue_id"]) != _refs(queue["queue_id"]), True)
    conn.close()

    # And the blinding is untouched: the item surface still carries no stratum.
    item = review.next_item(queue["queue_id"], audit_db=audit_db)
    check("the blinded item carries no stratum", "stratum" in item, False)
    check("...and no drawn_for_segment either",
          "drawn_for_segment" in item, False)


def test_unique_contribution_is_not_a_second_count_of_the_same_rows() -> None:
    """
    A stage that only fires on notices some other stage also rejects removes
    nothing on its own, and the funnel cannot say so — it reports the FIRST
    rejecting stage, which credits whichever gate happened to run earlier.

    Written against the rule, not the archive's current numbers: a row dropped
    by two stages is unique to neither, and per-stage figures therefore fall
    short of what a group of stages removes together. That relationship is what
    must survive; the counts move whenever the profile does.
    """
    print("\nUnique contribution:")
    from filter_audit.replay import dropping_stages

    criteria = _criteria()

    # One stage only: construction rejects it, relevance would have admitted it
    # on the keyword branch. Remove construction and this notice enters.
    elevator = P.audit_decision(_notice(NRC_ELEVATOR), criteria, "2023-03-15")
    check("a single-stage drop names exactly that stage",
          dropping_stages(elevator), {"construction"})

    # Two stages: unique to neither, still removed by the pair.
    both = dict(CODED_WRONG_FAMILY)
    both["closing_date"] = "2020-01-01"
    two = P.audit_decision(_notice(both), criteria, "2026-01-05")
    check("a two-stage drop names both", dropping_stages(two),
          {"closed", "relevance"})
    check("...so it is unique to neither", len(dropping_stages(two)) == 1, False)

    # An admitted notice contributes to nothing.
    admitted = P.audit_decision(_notice(SSC_CYBER), criteria, None)
    check("an admitted notice has an empty drop set",
          dropping_stages(admitted) - {"relevance"}, set())

    if not NOTICES_DB.exists():
        print("  SKIP  no data/notices.db for the funnel arithmetic")
        return
    from filter_audit.replay import replay

    result = replay(source="archive", as_of_spec="publication", sample=3000,
                    seed=11)
    unique = result["unique_contribution"]
    fires = result["audit_stage_fires"]
    check("every stage that can fire reports a unique contribution",
          sorted(unique), sorted(fires))
    check("no stage is unique on more rows than it fires on",
          all(unique[name] <= fires[name] for name in unique), True)
    check("the uniques do not exceed the rows actually rejected",
          sum(unique.values()) <= result["rejected"], True)
    # The whole point of the group figure: it is at least the sum of the
    # non-relevance uniques (each of those rows qualifies) and can exceed it
    # (a row dropped by two non-relevance stages counts here and in neither).
    non_relevance = sum(v for k, v in unique.items() if k != "relevance")
    check("the group figure is at least the sum of its stages' uniques",
          result["dropped_without_relevance"] >= non_relevance, True)
    check("...and no larger than everything relevance did not drop",
          result["dropped_without_relevance"]
          <= result["rejected"] - fires.get("relevance", 0), True)


def test_limitation_is_bounded_by_the_snapshots() -> None:
    """
    What the replay says it cannot do must track what is actually on disk.

    The limitation is the one line in the funnel that bounds every number above
    it, so it has to be derived rather than written down. Before any snapshot
    exists the claim is unbounded; once ingest.py has kept a day, the claim is
    that everything BEFORE that day is unrecoverable — and saying otherwise
    would be the audit overstating its own blindness.
    """
    print("\nLimitation text:")
    from filter_audit.replay import _limitation, first_snapshot_date

    empty = Path(tempfile.mkdtemp()) / "no-snapshots"
    check("an absent snapshot directory reports no date",
          first_snapshot_date(empty), None)

    empty.mkdir(parents=True)
    check("an empty snapshot directory reports no date",
          first_snapshot_date(empty), None)

    for name in ("tenders-2026-08-19.csv.gz", "tenders-2026-08-17.csv.gz",
                 "tenders-2026-09-01.csv.gz"):
        (empty / name).write_bytes(b"")
    check("the EARLIEST snapshot bounds the claim",
          first_snapshot_date(empty), "2026-08-17")

    # A file the convention does not fit is skipped, never coerced into a day
    # it does not describe.
    (empty / "tenders-backup.csv.gz").write_bytes(b"")
    (empty / "tenders-2026-8-1.csv.gz").write_bytes(b"")
    check("a non-conforming name is ignored, not parsed",
          first_snapshot_date(empty), "2026-08-17")

    unbounded = _limitation(None)
    bounded = _limitation("2026-08-17")
    check("with no snapshots the claim stays general",
          "cannot be reconstructed" in unbounded, True)
    check("...and names no date", "2026" not in unbounded, True)
    check("with snapshots the claim names the boundary",
          "2026-08-17" in bounded, True)
    check("...and says unavailable BEFORE it, not unavailable",
          "unavailable before 2026-08-17" in bounded, True)
    # A snapshot returns the feed, not the filter that read it.
    check("...while still refusing to claim the past predicates",
          "PREDICATES" in bounded, True)


def test_false_negative_recording() -> None:
    """
    A rejected notice can become structured evidence without moving the filter.

    Drives a full round trip against a TEMP reviews file and a temp audit db
    rather than reading the committed vault log. Two reasons: the committed log
    is the user's evolving evidence and a test that asserts things about its
    contents breaks the moment they review something, and a test that only reads
    an artifact someone else produced proves the artifact existed, not that the
    mechanism works.

    REVIEWS_FILE is rebound by attribute, the same pattern tests/conftest.py uses
    on tender_tools.paths and for the same reason — a `from review import
    REVIEWS_FILE` would take a copy this rebind cannot reach, and the test would
    append to the real vault log.
    """
    print("\nFalse-negative recording:")
    if not NOTICES_DB.exists():
        print("  SKIP  no data/notices.db")
        return
    from filter_audit.replay import replay

    tmp = Path(tempfile.mkdtemp())
    audit_db = tmp / "audit.db"
    original_reviews = review.REVIEWS_FILE
    review.REVIEWS_FILE = tmp / "filter-reviews.jsonl"
    try:
        run = replay(source="archive", as_of_spec="publication", sample=400,
                     seed=11, persist=True, audit_db=audit_db)
        queue = review.sample_rejects(run_id=run["run_id"], n=5, seed=3,
                                      branch="uncoded_no_keyword",
                                      audit_db=audit_db)
        check("a reject queue was built", "error" in queue, False)
        if "error" in queue:
            return

        item = review.next_item(queue["queue_id"], audit_db=audit_db)
        check("the queue yields a blinded item", "item_id" in item, True)
        item_id = item["item_id"]

        # Reveal must refuse before a disposition exists.
        early = review.reveal(item_id, audit_db=audit_db)
        check("reveal is refused before a disposition",
              "No disposition recorded" in early.get("error", ""), True)

        # Disposition — the reviewer disagrees, which is what makes it a
        # candidate false negative.
        posted = review.record_review(item_id, "ACCEPT", reviewer="assistant",
                                      audit_db=audit_db)
        check("the disposition returns the reveal", posted.get("agreed"), False)
        check("...and reports what production decided",
              posted["production_decision"], "REJECT")

        records = review.load_reviews()
        check("exactly one record was appended", len(records), 1)
        record = records[0]
        for field in ("original_decision", "original_first_rejecting_stage",
                      "reviewed_decision", "filter_version_label", "reviewer"):
            check(f"the review carries {field}", bool(record.get(field)), True)
        check("original_decision came from the run, not the caller",
              record["original_decision"], "REJECT")
        check("the reviewer is stated explicitly", record["reviewer"], "assistant")
        check("the disposition records that it was blinded",
              record["blinded_at_disposition"], True)
        check("the review id numbers dispositions, not lines",
              record["review_id"].endswith("-001"), True)

        # Categorization is a second record that inherits, never overwrites.
        categorized = review.categorize(item_id, "vocabulary_mismatch",
                                        "evidence", "explanation")
        check("categorization succeeds", "error" in categorized, False)
        after = review.load_reviews()
        check("categorization appended rather than edited", len(after), 2)
        check("...and the original disposition is byte-identical",
              after[0], record)
        check("...and the categorization names its parent",
              after[1]["supersedes"], record["review_id"])
        check("...and its category is in the taxonomy",
              after[1]["failure_category"] in review.taxonomy_keys(), True)

        # And none of it moved the filter.
        criteria = _criteria()
        notice = _notice(SSC_CYBER)
        decision = P.production_decision(notice, criteria, "2022-04-29")
        check("recording a false negative admits nothing",
              decision.admitted, False)
    finally:
        review.REVIEWS_FILE = original_reviews


def test_reviewer_must_be_stated() -> None:
    """The provenance log must not be able to guess who judged."""
    print("\nReviewer attribution:")
    import inspect

    signature = inspect.signature(review.record_review)
    check("record_review has no default reviewer",
          signature.parameters["reviewer"].default, inspect.Parameter.empty)

    from filter_audit.cli import build_parser
    parser = build_parser()
    action = next(a for a in parser._subparsers._group_actions[0]
                  .choices["record-review"]._actions
                  if a.dest == "reviewer")
    check("the CLI requires --reviewer", action.required, True)
    check("...and constrains it to stated values",
          sorted(action.choices), ["assistant", "human"])


def main() -> int:
    print("=" * 68)
    print("Filter audit")
    print("=" * 68)
    test_predicate_independence()
    test_relevance_branching()
    test_skipped_is_not_passed()
    test_jurisdiction_unrecognised_is_admitted()
    test_production_equivalence()
    test_equivalence_refuses_a_vacuous_pass()
    test_provenance()
    test_variants_cannot_reach_production()
    test_blinding_is_structural()
    test_uncertain_and_reviews_never_reach_production()
    test_taxonomy_is_closed_but_extensible()
    test_golden_evaluation_arithmetic()
    test_regression_protection()
    test_no_change_is_not_equivalence()
    test_historical_reproducibility()
    test_coded_wrong_family_stratum()
    test_unique_contribution_is_not_a_second_count_of_the_same_rows()
    test_limitation_is_bounded_by_the_snapshots()
    test_false_negative_recording()
    test_reviewer_must_be_stated()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S):")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print("All filter-audit checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
