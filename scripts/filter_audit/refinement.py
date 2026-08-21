"""
Refinement proposals, and the human step between proposing one and shipping it.

THE SEPARATION IS THE POINT, and it is structural rather than procedural:

  PROPOSED   names no variant. There is no code, so there is nothing to
             evaluate, and `evaluate-refinement` refuses. An LLM may author a
             file in this state - that is the whole extent of what it can do.

  TESTING    a human has written the variant in variants.py and named it here.
             Only now can the proposal be scored.

  ACCEPTED   a human recorded the decision after reading the comparison.
             ACCEPTING STILL CHANGES NOTHING. variants.py is not imported by
             ingest.py or predicates.py, so an accepted-but-unpromoted variant
             physically cannot reach the corpus.

  REJECTED   kept, not deleted. A rejected refinement is evidence about the
             filter, and re-proposing it later should cost someone the trouble
             of reading why it failed.

Promotion is a separate human commit that edits the profile or predicates.py and
registers a new filter version. There is no command in this package that writes
to vault/profiles/my-company.md.

EVALUATION RESULTS ARE APPENDED, NEVER MERGED INTO THE PROPOSAL. Same rule
case-protocol.md states about itself: a protocol rewritten after its results is
no longer a protocol. The frontmatter records what was proposed; dated sections
below record what happened when it was tried.
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml

PROJECT_ROOT = Path(__file__).parent.parent.parent
REFINEMENTS_DIR = PROJECT_ROOT / "vault" / "reference" / "filter-refinements"

STATUSES = ("PROPOSED", "TESTING", "ACCEPTED", "REJECTED")


def _split_frontmatter(text: str):
    match = re.match(r"^---\n(.*?)\n---\n?(.*)$", text, re.DOTALL)
    if not match:
        return {}, text
    return yaml.safe_load(match.group(1)) or {}, match.group(2)


def load_all() -> list:
    if not REFINEMENTS_DIR.exists():
        return []
    out = []
    for path in sorted(REFINEMENTS_DIR.glob("*.md")):
        front, body = _split_frontmatter(path.read_text(encoding="utf-8"))
        front["_path"] = str(path.relative_to(PROJECT_ROOT))
        front["_body"] = body
        out.append(front)
    return out


def load(refinement_id: str) -> Optional[dict]:
    for item in load_all():
        if item.get("id") == refinement_id:
            return item
    return None


def validate() -> dict:
    """
    Structural checks over every proposal file.

    The one that matters: a proposal past PROPOSED must name a variant, because
    a status of TESTING or ACCEPTED without code is a claim nobody can check.
    """
    problems = []
    for item in load_all():
        rid = item.get("id", item["_path"])
        status = item.get("status")
        if status not in STATUSES:
            problems.append(f"{rid}: status {status!r} not in {STATUSES}")
        change = item.get("proposed_change") or {}
        variant = change.get("variant")
        if status in ("TESTING", "ACCEPTED") and not variant:
            problems.append(
                f"{rid}: status {status} but names no variant. A refinement past "
                f"PROPOSED must point at code that can be evaluated.")
        if status == "PROPOSED" and variant:
            problems.append(
                f"{rid}: status PROPOSED but names variant {variant!r}. If the "
                f"code exists, the status is TESTING.")
        if status == "ACCEPTED" and not item.get("evaluation_results"):
            problems.append(
                f"{rid}: ACCEPTED with no evaluation_results. Nothing may be "
                f"accepted on argument alone.")
    return {"checked": len(load_all()), "problems": problems,
            "ok": not problems}


def evaluate_refinement(refinement_id: str,
                        profile_path: Optional[Path] = None,
                        golden_path: Optional[Path] = None) -> dict:
    """
    Score a refinement against the version it was proposed from.

    Refuses a PROPOSED refinement, and the refusal is the mechanism rather than
    a lint: there is genuinely no code to run.
    """
    from .compare import compare

    item = load(refinement_id)
    if not item:
        known = ", ".join(sorted(i.get("id", "?") for i in load_all())) or "none"
        return {"error": f"No refinement {refinement_id!r}. Known: {known}"}
    change = item.get("proposed_change") or {}
    variant = change.get("variant")
    if not variant:
        return {"error": f"{refinement_id} is {item.get('status')} and names no "
                         f"variant. A proposal has no code behind it, so there "
                         f"is nothing to evaluate. Write the variant in "
                         f"scripts/filter_audit/variants.py and set status to "
                         f"TESTING."}
    result = compare(None, variant, profile_path, golden_path)
    result["refinement_id"] = refinement_id
    result["status"] = item.get("status")
    result["variant"] = variant
    result["promotion_note"] = (
        "This is an evaluation, not a promotion. Accepting is a human edit to "
        "the refinement file; shipping is a separate commit that changes the "
        "profile or predicates.py and registers a new filter version. Nothing "
        "in this package writes to either.")
    return result


def append_evaluation(refinement_id: str, comparison: dict) -> dict:
    """
    Record one evaluation as a dated section, leaving the proposal untouched.

    Appends to the body and adds a stub to `evaluation_results`. The proposal's
    own prose is never edited - what was proposed and what was measured are kept
    apart on purpose.
    """
    item = load(refinement_id)
    if not item:
        return {"error": f"No refinement {refinement_id!r}."}
    path = PROJECT_ROOT / item["_path"]
    front, body = _split_frontmatter(path.read_text(encoding="utf-8"))

    stamp = datetime.now().strftime("%Y-%m-%d")
    base, cand = comparison["base"], comparison["candidate"]
    results = list(front.get("evaluation_results") or [])
    results.append({
        "date": stamp,
        "variant": comparison.get("variant"),
        "base_precision": base["precision"], "base_recall": base["recall"],
        "candidate_precision": cand["precision"], "candidate_recall": cand["recall"],
        "regressions": len(comparison["regressions"]),
        "recovered_false_negatives":
            len(comparison["recovered_historical_false_negatives"]),
    })
    front["evaluation_results"] = results

    from .compare import render as render_comparison
    section = (f"\n\n## Evaluation {stamp}\n\n"
               f"```\n{render_comparison(comparison)}\n```\n")

    path.write_text(
        "---\n"
        + yaml.safe_dump(
            {k: v for k, v in front.items() if not k.startswith("_")},
            sort_keys=False, default_flow_style=False, allow_unicode=True)
        + "---\n" + body.rstrip() + section,
        encoding="utf-8", newline="\n")
    return {"refinement_id": refinement_id, "appended": stamp,
            "path": item["_path"]}
