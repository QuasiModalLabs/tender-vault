---
id: ref-001-no-keywords-in-urls
status: TESTING
source_filter_version: fv-c065f51c
proposed_by: human
model: null
proposed_change:
  kind: predicate_code
  target: scripts/filter_audit/predicates.py::stage_relevance
  variant: v-url-stripped-keywords
  summary: Strip URLs from title+description before keyword matching.
failure_categories_addressed:
- semantic_context_mismatch
supporting_examples:
- gold-canadapost-cloud-in-url
evaluation_results:
- date: '2026-08-20'
  variant: v-url-stripped-keywords
  base_precision: 0.8571428571428571
  base_recall: 0.75
  candidate_precision: 1.0
  candidate_recall: 0.75
  regressions: 0
  recovered_false_negatives: 0
promoted_to_production: null
---

# REF-001 — stop matching competencies inside URLs

## Problem

`MX-443978767209` (Canada Post, "Request for Supply Arrangement_Electrical
Services_2024_Wave-2") is in the corpus because the competency `cloud` matched
inside the Ariba portal hostname `portal.us.bn.cloud.ariba.com`, and nowhere
else in the notice. It is an electrical-services arrangement.

This was not discovered by this system. `vault/briefings/briefing-2026-08-17.md`
flagged it on two consecutive briefings and stated the fix:

> The fix is not to prune `cloud` … it is to stop matching keywords inside URLs.
> That is a change to `scripts/ingest.py`; flagging rather than making it.

The briefing declined to make the change because there was no way to tell
whether it would cost more than it bought. That is the gap this package closes.

## Why not the obvious alternative

Pruning `cloud` from the profile would fix this notice and lose the nine real
cloud notices the term earns its place on. The briefing said so; the golden set
now proves it, because `gold-relevant-*` entries would regress.

## Proposed change

Strip URLs and bare dotted hostnames from title+description before
`matched_competencies` runs. Nothing else changes: the term list is untouched,
UNSPSC handling is untouched, and a notice that says "cloud" in prose still
matches.

## What this does not address

Nothing about the two known false negatives. A URL fix cannot recover
`gold-ssc-cyber-security-itq` (orthographic variant) or
`gold-fintrac-industry-day` (wrong publisher codes). Those are REF-002 and an
unwritten third proposal respectively.

## Evaluation 2026-08-20

```
Filter comparison - same golden set, same entries
  base       current                    precision 0.857 (0.487-0.974)    recall 0.750 (0.409-0.929)
  candidate  v-url-stripped-keywords    precision 1.000 (0.610-1.000)    recall 0.750 (0.409-0.929)
                                        TP 6 FP 0 TN 8 FN 2   (base TP 6 FP 1 TN 7 FN 2)

  newly admitted                        0
  newly rejected                        1
  recovered historical false negatives  0
  REGRESSIONS                           0
    still wrong gold-ssc-cyber-security-itq       expected ADMIT, got REJECT
    still wrong gold-fintrac-industry-day         expected ADMIT, got REJECT

  No regressions. 0 known false negatives recovered. Whether that is worth the precision change is a human decision, recorded in the refinement file - not computed here.

  No composite score is produced. Precision and recall are two numbers, not one, and 'admits more' is not 'is better'.
```
