"""
Filter audit, replay and refinement — making scripts/ingest.py's filter observable.

BLINDING IS A PROPERTY OF THIS PACKAGE, NOT A CONVENTION ITS CALLERS FOLLOW.
The review surface must not disclose the rejection reason, the failing gate, or
the stratum an item was sampled from until a disposition has been recorded for
it. That is enforced two ways and both are load-bearing: the reviewer is handed
a `BlindedNotice`, which does not carry those fields at all, and `reveal`
refuses to render until the store confirms a disposition exists.

This is why the audit lives here rather than as verbs on tender_tools. A rule
that three functions inside an 18-verb dispatcher have to remember is a rule
that survives exactly until someone adds a fourth. Same reasoning that kept
scoring inside the backtest harness.

TWO DECISIONS, NEVER CONFLATED. `production_decision` short-circuits and
reproduces scripts/ingest.py exactly, reporting the FIRST rejecting stage.
`audit_decision` evaluates every stage independently against the original
notice. A notice can be `production=REJECT, first_rejecting_stage=construction`
while the audit says `relevance=pass` — that gap is the entire point.

NOTHING HERE CHANGES WHAT PRODUCTION ADMITS. `variants.py` is not imported by
ingest.py or predicates.py, reviews are never read by a decision function, and
promotion of a refinement is a human commit.
"""
from __future__ import annotations

import sys
from pathlib import Path

# scripts/ on the path so the flat sibling modules (ingest, org_resolve,
# crosswalk) import the same way they do everywhere else in this repo.
sys.path.insert(0, str(Path(__file__).parent.parent))

from .predicates import (  # noqa: E402
    STAGES,
    AuditDecision,
    Notice,
    ProductionDecision,
    Stage,
    StageResult,
    audit_decision,
    production_decision,
)
from .version import filter_version  # noqa: E402

__all__ = [
    "Notice", "StageResult", "Stage", "STAGES",
    "ProductionDecision", "AuditDecision",
    "production_decision", "audit_decision", "filter_version",
]
