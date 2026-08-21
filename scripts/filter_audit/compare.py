"""
Two filter versions on one golden set, and the tradeoff between them.

"BETTER" IS NOT COMPUTED HERE, and that is the design. This module reports what
changed in both directions and stops. No F1, no weighted score, no verdict line
that says one version wins - the repo prohibits composite scoring for tenders
and departments, and a refinement gate is exactly where such a score would come
back wearing a lab coat.

The one judgement it does make is negative and mechanical: if a candidate breaks
entries the base got right, that is a REGRESSION and `--gate` fails. Recovering
a known false negative never offsets it automatically. `v-admit-everything`
exists in variants.py to prove the point - it recovers every false negative in
the set and is worse by every other measure.

TWO REFUSALS, both about not manufacturing a measurement:

  Different stage manifests are not compared. If a stage was added, removed,
  reordered or deactivated, the two funnels answer different questions and a
  confusion matrix spanning them is arithmetic across two definitions of
  admission.

  Identical results are reported as "no entry changed", never as "the versions
  are equivalent" with a rate attached. Arms that received identical inputs
  agree by arithmetic, and dressing that up as a finding invents a measurement
  out of a design defect.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from .golden import evaluate, load
from .version import comparable, filter_version


def compare(base_variant: Optional[str] = None,
            candidate_variant: Optional[str] = None,
            profile_path: Optional[Path] = None,
            golden_path: Optional[Path] = None) -> dict:
    """
    Score both versions on the same entries and diff them entry by entry.

    `None` for either variant means the current predicates and profile.
    """
    base = evaluate(base_variant, profile_path, golden_path)
    candidate = evaluate(candidate_variant, profile_path, golden_path)
    if "error" in base:
        return base
    if "error" in candidate:
        return candidate

    version = filter_version(profile_path)
    ok, reason = comparable(version, version)

    base_by_id = {e["id"]: e for e in base["per_entry"]}
    entries_by_id = {e["id"]: e for e in load(golden_path).get("entries", [])}

    newly_admitted, newly_rejected = [], []
    regressions, recovered, still_wrong = [], [], []

    for entry in candidate["per_entry"]:
        before = base_by_id.get(entry["id"])
        if not before:
            continue
        if before["got"] != entry["got"]:
            record = {"id": entry["id"],
                      "reference_number": entry["reference_number"],
                      "class": entry["class"], "expected": entry["expected"],
                      "was": before["got"], "now": entry["got"]}
            (newly_admitted if entry["got"] == "ADMIT" else newly_rejected).append(record)

        if before.get("correct") is True and entry.get("correct") is False:
            regressions.append({
                "id": entry["id"], "class": entry["class"],
                "expected": entry["expected"], "now": entry["got"],
                "why": (entry["first_rejecting_stage"] or "admitted")})
        elif before.get("correct") is False and entry.get("correct") is True:
            source = entries_by_id.get(entry["id"], {})
            recovered.append({
                "id": entry["id"], "class": entry["class"],
                "failure_categories": source.get("failure_categories", [])})
        elif entry.get("correct") is False:
            still_wrong.append({"id": entry["id"], "class": entry["class"],
                                "expected": entry["expected"], "got": entry["got"]})

    changed = bool(newly_admitted or newly_rejected)
    recovered_fn = [r for r in recovered if r["class"] == "known_false_negative"]

    return {
        "comparable": ok,
        "comparable_reason": reason,
        "base": {"filter": base["filter"], "matrix": base["matrix"],
                 "precision": base["precision"], "precision_ci": base["precision_ci"],
                 "recall": base["recall"], "recall_ci": base["recall_ci"]},
        "candidate": {"filter": candidate["filter"], "matrix": candidate["matrix"],
                      "precision": candidate["precision"],
                      "precision_ci": candidate["precision_ci"],
                      "recall": candidate["recall"],
                      "recall_ci": candidate["recall_ci"]},
        "changed": changed,
        "newly_admitted": newly_admitted,
        "newly_rejected": newly_rejected,
        "regressions": regressions,
        "recovered": recovered,
        "recovered_historical_false_negatives": recovered_fn,
        "still_wrong": still_wrong,
        "gate_pass": ok and not regressions and changed,
        "gate_reason": _gate_reason(ok, regressions, changed, recovered_fn),
    }


def _gate_reason(ok, regressions, changed, recovered_fn) -> str:
    if not ok:
        return ("Not comparable - the stage manifest differs. Nothing was "
                "scored across the two.")
    if regressions:
        return (f"REGRESSIONS: {len(regressions)} entr"
                f"{'y' if len(regressions) == 1 else 'ies'} the base got right, "
                f"the candidate gets wrong. Recovering a false negative does not "
                f"offset this; the tradeoff is for a human to weigh.")
    if not changed:
        return ("No entry changed. The two versions produced identical results "
                "on this set - which is not the same as being equivalent, and no "
                "rate is reported for it. The set may simply not exercise the "
                "difference.")
    return (f"No regressions. {len(recovered_fn)} known false negative"
            f"{'' if len(recovered_fn) == 1 else 's'} recovered. "
            f"Whether that is worth the precision change is a human decision, "
            f"recorded in the refinement file - not computed here.")


def render(result: dict) -> str:
    if "error" in result:
        return f"ERROR: {result['error']}"

    def rate(block, key):
        value, interval = block[key], block[f"{key}_ci"]
        if value is None:
            return "n/a"
        return f"{value:.3f} ({interval[0]:.3f}-{interval[1]:.3f})"

    base, cand = result["base"], result["candidate"]
    lines = [
        "Filter comparison - same golden set, same entries",
        f"  base       {base['filter']:<26} precision {rate(base,'precision'):<22} "
        f"recall {rate(base,'recall')}",
        f"  candidate  {cand['filter']:<26} precision {rate(cand,'precision'):<22} "
        f"recall {rate(cand,'recall')}",
        f"             {'':<26} TP {cand['matrix']['TP']} FP {cand['matrix']['FP']} "
        f"TN {cand['matrix']['TN']} FN {cand['matrix']['FN']}"
        f"   (base TP {base['matrix']['TP']} FP {base['matrix']['FP']} "
        f"TN {base['matrix']['TN']} FN {base['matrix']['FN']})",
        "",
        f"  newly admitted                        {len(result['newly_admitted'])}",
        f"  newly rejected                        {len(result['newly_rejected'])}",
        f"  recovered historical false negatives  "
        f"{len(result['recovered_historical_false_negatives'])}",
        f"  REGRESSIONS                           {len(result['regressions'])}",
    ]
    for item in result["recovered_historical_false_negatives"]:
        lines.append(f"    recovered  {item['id']}  "
                     f"{'/'.join(item['failure_categories']) or '-'}")
    for item in result["regressions"]:
        lines.append(f"    REGRESSED  {item['id']:<34} expected "
                     f"{item['expected']}, now {item['now']} ({item['why']})")
    for item in result["still_wrong"]:
        lines.append(f"    still wrong {item['id']:<33} expected "
                     f"{item['expected']}, got {item['got']}")
    lines.append("")
    lines.append(f"  {result['gate_reason']}")
    lines.append("")
    lines.append("  No composite score is produced. Precision and recall are two "
                 "numbers, not one, and 'admits more' is not 'is better'.")
    return "\n".join(lines)
