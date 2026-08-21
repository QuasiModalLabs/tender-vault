---
id: ref-002-cyber-security-spacing
status: TESTING
source_filter_version: fv-c065f51c
proposed_by: human
model: null
proposed_change:
  kind: profile_config
  target: vault/profiles/my-company.md::competencies
  variant: v-cyber-security-spacing
  summary: Add the two-word "cyber security" alongside the one-word competency.
failure_categories_addressed:
- vocabulary_mismatch
supporting_examples:
- gold-ssc-cyber-security-itq
evaluation_results:
- date: '2026-08-20'
  variant: v-cyber-security-spacing
  base_precision: 0.8571428571428571
  base_recall: 0.75
  candidate_precision: 0.875
  candidate_recall: 0.875
  regressions: 0
  recovered_false_negatives: 1
promoted_to_production: null
---

# REF-002 — the profile picked the minority spelling

## Problem

`SSC-22-00019111:T`, "Invitation to Qualify for the Cyber Security Procurement
Vehicle", is rejected at `relevance`.

Shared Services Canada files no UNSPSC — it is one of the three uncoded source
systems — so its notices can only ever reach the keyword branch. The profile
carries the one-word `cybersecurity`, and `matched_competencies` matches on word
boundaries, so `\bcybersecurity\b` cannot reach "Cyber Security".

## Evidence

Counted over archive rows where `parse_unspsc_codes` returns empty — the same
definition the filter itself uses:

| spelling | uncoded notices |
|---|---|
| `cyber security` (two words) | 35 |
| `cybersecurity` (one word) | 12 |

The profile picked the minority spelling, and it did so on the largest federal
IT buyer. A narrower `unspsc='*'` query gives 8 rather than 12 because it misses
174 rows whose field is null or empty — which is why the count is recorded with
the query that produced it rather than as a bare number.

## Proposed change

Add `cyber security` to `competencies`. This is a profile edit, not a code
change, so it bumps `profile_sha256` and therefore the filter version label.

## The cost, to be measured rather than assumed

The profile's own comment is explicit that `cybersecurity` is intake vocabulary
and that the prose caps real capability at vulnerability assessment with no
active SOC work. Adding the two-word form surfaces more notices to triage, some
of which will be physical-security guard services rather than IT. That is a
triage cost, not a bid cost, and the golden set is where it gets counted.

## Evaluation 2026-08-20

```
Filter comparison - same golden set, same entries
  base       current                    precision 0.857 (0.487-0.974)    recall 0.750 (0.409-0.929)
  candidate  v-cyber-security-spacing   precision 0.875 (0.529-0.978)    recall 0.875 (0.529-0.978)
                                        TP 7 FP 1 TN 7 FN 1   (base TP 6 FP 1 TN 7 FN 2)

  newly admitted                        1
  newly rejected                        0
  recovered historical false negatives  1
  REGRESSIONS                           0
    recovered  gold-ssc-cyber-security-itq  vocabulary_mismatch
    still wrong gold-canadapost-cloud-in-url      expected REJECT, got ADMIT
    still wrong gold-fintrac-industry-day         expected ADMIT, got REJECT

  No regressions. 1 known false negative recovered. Whether that is worth the precision change is a human decision, recorded in the refinement file - not computed here.

  No composite score is produced. Precision and recall are two numbers, not one, and 'admits more' is not 'is better'.
```
