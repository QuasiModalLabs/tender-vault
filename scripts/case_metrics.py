"""
Reveal ground truth and score the blinded evaluation.

THE ONLY MODULE THAT OPENS truth.json, and it refuses to until predictions.jsonl
has been sealed with a hash. That ordering is the whole point of the protocol:
a prediction written after the answer was visible is not a prediction, and the
only way to prove the order is to fix the predictions first and check them
against their own digest afterwards.

WHAT THE NUMBERS CAN AND CANNOT CARRY. Twenty cases give roughly +/-22 points on
any single proportion, so nothing here is a finding about whether evidence helps.
It is a check that the machinery produces the right SHAPE of answer
reproducibly. Two guards make that explicit rather than leaving it to the
reader: no proportion is reported without its Wilson interval, and the
incremental comparison between conditions is a paired count of who changed their
mind, not a difference of two independent rates.

Z IS THE BASELINE THAT MATTERS. The evaluator's pretraining runs past every
evaluation date here. Z measures what it scores with no supplied evidence at
all, so anything A/B/C/D achieves is only interesting to the extent it exceeds
Z. Reporting A's precision without Z alongside it would be reporting the model's
memory as though it were the dossier's contribution.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CASES_DIR = PROJECT_ROOT / "data" / "cases"
CONDITIONS = ("Z", "A", "B", "C", "D")
CONDITION_LABELS = {
    "Z": "no supplied evidence",
    "A": "procurement",
    "B": "+ contracts",
    "C": "+ plans",
    "D": "+ audits",
}


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% interval for a proportion. Brackets the point estimate even at k=0."""
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, centre - half), min(1.0, centre + half))


def fmt_rate(k: int, n: int) -> str:
    if n == 0:
        return "n/a (0 cases)"
    lo, hi = wilson(k, n)
    return f"{k}/{n} = {k / n:.0%} (95% CI {lo:.0%}-{hi:.0%})"


def mcnemar_exact(b: int, c: int) -> float:
    """
    Two-sided exact binomial p for the discordant pairs of a paired comparison.

    Paired because both conditions judged the SAME cases: the cases that agree
    carry no information about which condition is better, and pooling them into
    two independent rates would understate the uncertainty.
    """
    n = b + c
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, i) for i in range(0, min(b, c) + 1)) / (2 ** n)
    return min(1.0, 2 * tail)


def load(require_seal: bool = True) -> tuple[dict, dict, list]:
    preds_path = CASES_DIR / "predictions.jsonl"
    seal_path = CASES_DIR / "predictions.sha256"
    if require_seal:
        if not seal_path.exists():
            raise SystemExit(
                "predictions.sha256 is missing. Seal the predictions before "
                "revealing ground truth:\n"
                "  python scripts/evaluate_cases.py --seal")
        recorded = seal_path.read_text(encoding="utf-8").split()[0]
        actual = hashlib.sha256(preds_path.read_bytes()).hexdigest()
        if recorded != actual:
            raise SystemExit(
                "predictions.jsonl has changed since it was sealed.\n"
                f"  sealed {recorded}\n  actual {actual}\n"
                "The run is void; a prediction file edited after sealing cannot "
                "be scored.")
    cases = {c["case_id"]: c for c in json.loads(
        (CASES_DIR / "cases.json").read_text(encoding="utf-8"))["cases"]}
    truth = {t["case_id"]: t for t in json.loads(
        (CASES_DIR / "truth.json").read_text(encoding="utf-8"))["truth"]}
    preds = [json.loads(line) for line in
             preds_path.read_text(encoding="utf-8").splitlines() if line.strip()]

    # Every prediction records the digest of the bundle it answered. If a bundle
    # has changed since — a re-run of `casebook.py pilot` with a different seed,
    # an edited renderer — then the prediction answers a document that no longer
    # exists and pairing it with today's ground truth is meaningless.
    drifted = []
    for p in preds:
        bundle = CASES_DIR / "bundles" / f"{p['case_id']}-{p['condition']}.md"
        if not bundle.exists():
            drifted.append(f"{bundle.name} is gone")
        elif hashlib.sha256(bundle.read_bytes()).hexdigest()[:16] != p["bundle_sha256"]:
            drifted.append(f"{bundle.name} changed since it was evaluated")
    if drifted:
        raise SystemExit("Bundles drifted from the predictions:\n  "
                         + "\n  ".join(drifted[:10]))
    # Last write wins, so a re-run of one case supersedes rather than duplicates.
    latest: dict = {}
    for p in preds:
        latest[(p["case_id"], p["condition"])] = p
    return cases, truth, list(latest.values())


def confusion(preds: list, truth: dict, condition: str, tier: str | None = None,
              cases: dict | None = None) -> dict:
    tp = fp = tn = fn = unparsed = 0
    for p in preds:
        if p["condition"] != condition:
            continue
        if tier and cases and cases[p["case_id"]]["match_tier"] != tier:
            continue
        actual = truth[p["case_id"]]["ground_truth"] == "POSITIVE"
        if p["decision"] == "INVESTIGATE":
            tp, fp = tp + (1 if actual else 0), fp + (0 if actual else 1)
        elif p["decision"] == "DO_NOT_INVESTIGATE":
            fn, tn = fn + (1 if actual else 0), tn + (0 if actual else 1)
        else:
            unparsed += 1
    return {"tp": tp, "fp": fp, "tn": tn, "fn": fn, "unparsed": unparsed}


def report(cases: dict, truth: dict, preds: list) -> None:
    n_cases = len(cases)
    n_pos = sum(1 for t in truth.values() if t["ground_truth"] == "POSITIVE")
    print("=" * 74)
    print("BLINDED EVALUATION - PILOT RESULT")
    print("=" * 74)
    print(f"\n{n_cases} cases ({n_pos} positive, {n_cases - n_pos} negative), "
          f"{len(preds)} predictions across {len({p['condition'] for p in preds})} conditions.")
    print(f"Model: {sorted({p['model'] for p in preds})}")
    print("\nEXPLORATORY. The evaluator is a language model whose pretraining "
          "post-dates\nevery evaluation date in this pilot. Contamination cannot be "
          "ruled out, so read\nA/B/C/D against Z rather than in absolute terms, and "
          "read nothing as significant\nat n=20.")

    print("\n" + "-" * 74)
    print("IDENTIFICATION, BY CONDITION")
    print("-" * 74)
    print(f"{'cond':<5} {'evidence':<22} {'precision':>22} {'recall':>22}")
    rows = {}
    for cond in CONDITIONS:
        c = confusion(preds, truth, cond)
        rows[cond] = c
        prec_n = c["tp"] + c["fp"]
        rec_n = c["tp"] + c["fn"]
        print(f"{cond:<5} {CONDITION_LABELS[cond]:<22} "
              f"{fmt_rate(c['tp'], prec_n):>22} {fmt_rate(c['tp'], rec_n):>22}")
    print()
    print(f"{'cond':<5} {'accuracy':>22} {'FPR':>22} {'FNR':>22}  unparsed")
    for cond in CONDITIONS:
        c = rows[cond]
        n = c["tp"] + c["fp"] + c["tn"] + c["fn"]
        print(f"{cond:<5} {fmt_rate(c['tp'] + c['tn'], n):>22} "
              f"{fmt_rate(c['fp'], c['fp'] + c['tn']):>22} "
              f"{fmt_rate(c['fn'], c['fn'] + c['tp']):>22}  {c['unparsed']}")

    print("\n" + "-" * 74)
    print("INCREMENTAL EFFECT OF EACH ADDED SOURCE (paired, same cases)")
    print("-" * 74)
    print("b = cases the added evidence fixed, c = cases it broke. "
          "p is exact McNemar.\n")
    by_case = defaultdict(dict)
    for p in preds:
        by_case[p["case_id"]][p["condition"]] = p
    for lo, hi in (("Z", "A"), ("A", "B"), ("B", "C"), ("C", "D")):
        b = c = same = 0
        for cid, conds in by_case.items():
            if lo not in conds or hi not in conds:
                continue
            actual = truth[cid]["ground_truth"] == "POSITIVE"
            want = "INVESTIGATE" if actual else "DO_NOT_INVESTIGATE"
            lo_ok, hi_ok = conds[lo]["decision"] == want, conds[hi]["decision"] == want
            if lo_ok == hi_ok:
                same += 1
            elif hi_ok:
                b += 1
            else:
                c += 1
        print(f"  {lo} -> {hi}: fixed {b}, broke {c}, unchanged {same}   "
              f"p = {mcnemar_exact(b, c):.3f}")

    print("\n" + "-" * 74)
    print("BY MATCHING TIER")
    print("-" * 74)
    print("department_exact holds the department constant, so plans and audits are")
    print("IDENTICAL within a pair and conditions C and D cannot discriminate there")
    print("by construction. activity_decile is the weaker control where they can.\n")
    for tier in ("department_exact", "activity_decile"):
        print(f"  {tier}")
        for cond in CONDITIONS:
            c = confusion(preds, truth, cond, tier, cases)
            n = c["tp"] + c["fp"] + c["tn"] + c["fn"]
            print(f"    {cond}: accuracy {fmt_rate(c['tp'] + c['tn'], n)}")

    print("\n" + "-" * 74)
    print("CONFIDENCE CALIBRATION")
    print("-" * 74)
    print("Raw pairs, not a curve. Twenty cases cannot support a calibration plot and")
    print("a binned one here would be decoration.\n")
    for cond in CONDITIONS:
        pairs = [(p["confidence"], truth[p["case_id"]]["ground_truth"] == "POSITIVE")
                 for p in preds if p["condition"] == cond and p["confidence"] is not None]
        if not pairs:
            continue
        inv = [c for c, _ in pairs]
        pos_conf = [c for c, a in pairs if a]
        neg_conf = [c for c, a in pairs if not a]
        print(f"  {cond}: stated confidence {min(inv)}-{max(inv)}, "
              f"median {sorted(inv)[len(inv) // 2]}")
        print(f"     on cases that DID procure:     "
              f"{sorted(pos_conf) if pos_conf else 'none'}")
        print(f"     on cases that did NOT procure: "
              f"{sorted(neg_conf) if neg_conf else 'none'}")

    print("\n" + "-" * 74)
    print("LEAD TIME ON TRUE POSITIVES")
    print("-" * 74)
    for cond in CONDITIONS:
        leads = [truth[p["case_id"]]["lead_time_days"] for p in preds
                 if p["condition"] == cond and p["decision"] == "INVESTIGATE"
                 and truth[p["case_id"]]["ground_truth"] == "POSITIVE"
                 and truth[p["case_id"]]["lead_time_days"] is not None]
        if not leads:
            print(f"  {cond}: no true positives")
            continue
        s = sorted(leads)
        print(f"  {cond}: n={len(s)}  median {s[len(s) // 2]}d  "
              f"min {s[0]}d  max {s[-1]}d")

    print("\n" + "-" * 74)
    print("LABEL-SHUFFLE CONTROL")
    print("-" * 74)
    print("Accuracy against permuted labels. Anything much above chance here means")
    print("bundle SHAPE carries the label and the design is broken.\n")
    import random
    for cond in CONDITIONS:
        obs = [(p["case_id"], p["decision"]) for p in preds if p["condition"] == cond]
        if not obs:
            continue
        labels = [truth[c]["ground_truth"] == "POSITIVE" for c, _ in obs]
        rng = random.Random(1234)
        accs = []
        for _ in range(2000):
            shuffled = labels[:]
            rng.shuffle(shuffled)
            accs.append(sum(1 for (_, d), a in zip(obs, shuffled)
                            if (d == "INVESTIGATE") == a) / len(obs))
        accs.sort()
        real = confusion(preds, truth, cond)
        n = sum(real[k] for k in ("tp", "fp", "tn", "fn"))
        actual_acc = (real["tp"] + real["tn"]) / n if n else 0
        print(f"  {cond}: observed {actual_acc:.0%}, shuffled median "
              f"{accs[len(accs) // 2]:.0%} (95th pct {accs[int(0.95 * len(accs))]:.0%})")

    print("\n" + "=" * 74)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--no-seal-check", action="store_true",
                    help="Score without a sealed prediction file. For debugging "
                         "the report only; a run scored this way is not evidence.")
    args = ap.parse_args()
    cases, truth, preds = load(require_seal=not args.no_seal_check)
    if args.no_seal_check:
        print("!! SEAL CHECK SKIPPED - this output is not a valid experimental result\n")
    report(cases, truth, preds)
    return 0


if __name__ == "__main__":
    sys.exit(main())
