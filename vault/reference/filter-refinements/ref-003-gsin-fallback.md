---
id: ref-003-gsin-fallback
status: PROPOSED
source_filter_version: fv-c065f51c
proposed_by: human
model: null
proposed_change:
  kind: predicate_code
  target: scripts/filter_audit/predicates.py::stage_relevance
  variant: null
  summary: >
    Add a GSIN fallback tier between UNSPSC and keywords, read directly from the
    notice's own gsin field - the ladder scripts/backtest.py already uses.
failure_categories_addressed:
  - structured_field_error
  - missing_structured_field
supporting_examples:
  - gold-fintrac-industry-day
evaluation_results: []
promoted_to_production: null
---

# REF-003 — the third relevance tier backtest.py already has

## Problem

`gold-fintrac-industry-day` is rejected on the CODED branch: the publisher filed
83110000 (telecom) and 80161502 (management support), neither in the profile
families. Because codes are present, the keyword branch is never consulted, so
no amount of competency tuning can recover it. REF-002's fix cannot reach this
class of miss at all.

## Observation

`scripts/backtest.py` already runs a three-tier ladder over the same corpus:
UNSPSC family, then `IT_GSIN_PREFIXES` read directly off the notice's `gsin`
field, then keywords. `scripts/ingest.py` has only two tiers. The archive
carries `gsin` on every row, so the tier is available and costs no new data.

## Explicitly NOT the crosswalk

This reads the notice's own GSIN field directly, exactly as `backtest.py` does.
It does **not** import the PSPC GSIN↔UNSPSC crosswalk to bridge the two code
systems — that is a standing prohibition, and `unspsc_discover.py` documents why
the linkage is unusable (telecom cable laying and highway paving both map to
GSIN 5153).

## Status

PROPOSED, and deliberately naming no variant. There is no code, so
`evaluate-refinement` refuses it — which is the intended behaviour, not a gap.
Someone has to decide whether a third tier is worth its precision cost before
writing it, and the first question to answer is what GSIN prefixes the 21,471
coded-wrong-family rejects actually carry.
