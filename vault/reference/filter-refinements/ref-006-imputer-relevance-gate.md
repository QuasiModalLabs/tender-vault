---
id: ref-006-imputer-relevance-gate
status: PROPOSED
source_filter_version: fv-c065f51c
proposed_by: human
model: jev-1.13.0
proposed_change:
  kind: predicate_code
  target: scripts/filter_audit/predicates.py::stage_relevance (uncoded branch)
  variant: null
  summary: >
    Admit an uncoded notice when Jev's summed probability across the four
    profile options is at least 0.20. Coded notices are still judged on their
    codes. Keywords remain as the fallback when the imputer is unavailable.
failure_categories_addressed:
  - vocabulary_mismatch
  - missing_structured_field
supporting_examples: []
evaluation_results: []
promoted_to_production: null
---

# REF-006: the imputer as the relevance gate for uncoded notices

Follows REF-004, where the pre-registered rule returned PROCEED, and its phase 2
analysis. This file records the decisions made before the gate goes live. It
is committed before any gate code.

## Design intent, recorded so it is not relitigated

**The imputer's job is to widen the funnel. Claude's job is to narrow it.**
False positives are acceptable by design, because a human and Claude read the
whole briefing. **Precision is deliberately not optimised.** What is budgeted
is **corpus size**, not precision.

**Jev imputes a commodity family; it does not judge fit.** The profile's
`unspsc_families` remains the only place fit is decided. The four profile
options in the frozen question are exactly those families.

## The rule

- **Admit an uncoded notice when the summed probability across the four
  profile options (8111, 8116, 4323, 80101507) is at least `t = 0.20`.**
- This is not top choice and not top-2. Top-2 would admit a notice whose
  profile option sits second at 0.02, and reject one where two profile options
  hold 0.20 each behind a 0.25 non-profile option. Summed probability handles
  both.
- **Only the uncoded branch**, and only after the closed, exclusion,
  construction and jurisdiction gates. **Coded notices are judged on their
  codes and never produce an API call.**
- **Fallback:** if `TYPESAFE_API_KEY` is unset, the frozen question does not
  match the profile, or the API errors, the notice is decided by the keyword
  branch as before. The funnel says so, and the ingest never fails for this.
- **Cached by content hash**, so a notice open for six weeks is paid for once.

## Why t = 0.20: chosen on corpus size, not recall

Chosen by the human from the corpus-size table, per the design intent. The
recall and precision below are agreement with publisher codes, which REF-004's
labels showed to be an ambiguous reference. They are context, not the
criterion.

**Corpus budget, observed.** `python scripts/family_imputer observe-feed`
imputed the 48 uncoded notices in the 2026-09-13 feed that reach the relevance
gate. That was 48 calls, 193,753 input tokens, $0.0081.
- The breakdown is MX 24, PW 13, SSC 11.
- At t = 0.20 Jev admits **15 of 48**. The keyword branch admits 5, and 4 of
  those 5 are also Jev admits.
- The corpus becomes **82** (67 coded + 15), against **72** today.
- Across all thresholds the corpus ranges from 77 to 83. Masses are bimodal:
  12 notices at 0.90 or more, 32 at exactly 0.00, and only 4 between.

**The archive-based estimate was wrong, and is kept here as a warning.**
`python scripts/family_imputer budget` estimated uncoded admit rates from coded
archive notices and predicted 8 feed admits at t = 0.20, against 15 observed.
The uncoded feed is not like the coded archive: 11 of its 48 are SSC, the
largest federal IT buyer. The keyword rate happened to transfer (0.103 vs
0.104); Jev's did not (0.159 vs 0.31).

**The volume premise was also wrong.** The feed carried 901 notices in total,
of which 143 were uncoded and 48 reached the relevance gate. The figure of
"~970 uncoded notices daily" was not supported.

## Split-half check

From `python scripts/family_imputer budget`, over 50 seeded splits of the
23,314 coded archive notices:

- **At a fixed t there is no systematic optimism.** Half B minus half A
  averages about zero. At t = 0.20 the 95% spread is:

  | measure | 95% spread (B minus A) |
  |---|---|
  | recall | −0.022 to +0.035 |
  | precision | −0.032 to +0.022 |
  | admit rate | −0.0062 to +0.0077 |

  That spread is the noise any single-sample figure carries.
- **Picking t on the data to hit a recall target is where it bites.** Picking
  t on half A for recall 0.90 and scoring it on half B, half B misses the
  target in **38%** of splits (22% at a 0.85 target). The picked t wanders
  between 0.01 and 0.05.

Choosing t on corpus size avoids that selection. At t = 0.20 the archive gives
recall 0.843 and precision 0.616 against publisher codes.

## Decisions taken with the gate

- **A. The probability never reaches anything Claude reads.**
  - Chroma metadata records `relevance_basis` (`unspsc`, `imputed` or `keyword`) and, for imputed notices, `imputed_family` and `imputer_model`. It records no probability.
  - Probabilities and full distributions live only in the gate cache, `.cache/family_imputer_gate.db`, which is the audit trail.
  - A score of 0.91 next to one of 0.06 would be a fit score in disguise, which is exactly what the briefing rules exist to prevent.
  - This is enforced by an AST scan and a test on the metadata builder, not by discipline.
- **B. CI gets `TYPESAFE_API_KEY` as a repository secret**, so the digest CI
  commits is built on the same rule as a local ingest. The provenance block
  and the funnel record which relevance mode ran, with counts, so a
  keyword-fallback build is never mistaken for an imputed one.

## Known gaps, stated at the start

- **The CI golden-set gate (`evaluate-golden --gate`) replays predicates on
  frozen rows that carry no imputation.** Uncoded golden entries are therefore
  still judged on keywords there. The gate does not test the imputed path.
- **`--skip-unchanged` compares only the feed and profile hashes.** A change of
  imputer mode alone (for example, the key becoming available) does not
  trigger a rebuild.
- The corpus figures come from one feed snapshot (2026-09-13). Nothing about
  the 15 imputed admits has been labelled. They are Jev's judgment, and the
  briefing is where they get read.

## Status

PROPOSED: decisions recorded, gate not yet live. `promoted_to_production` is
set in the commit that turns the gate on.
