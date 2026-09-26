---
id: ref-007-coded-imputer-flags
status: PROPOSED
mode: flag_only
promotion: NOT EVALUATED
source_filter_version: fv-a698eee0
proposed_by: human
model: jev-1.13.0
proposed_change:
  kind: predicate_code
  target: scripts/filter_audit/predicates.py (coded branch, flag only) and scripts/ingest/filters.py
  variant: null
  summary: >
    Ask the imputer about coded notices whose filed codes reject, and record a
    flag when Jev's summed profile-family mass is at least 0.90. Flag only: the
    notice is not admitted and the corpus is unchanged. The uncoded branch is
    untouched.
failure_categories_addressed:
  - structured_field_error
supporting_examples: []
evaluation_results: []
promoted_to_production: null
---

# REF-007: flags on coded notices the codes reject

Follows REF-004 (the imputer), REF-005 (the families the profile does not list)
and REF-006 (the uncoded gate). This file records the rule, the label
vocabulary and the evidence before any gate code. It is committed before that
code, on its own.

## Problem

A coded notice is judged on its filed codes alone. REF-004's labelled
disagreements showed those codes are an ambiguous reference: only 11 of 30 got
a definite error attribution. When the codes reject a notice that Jev places
firmly in a profile family, nothing records it today.

## What this rule measures, and what it does not

**It does not measure publisher miscoding.** A flag says only that the filed
codes and Jev disagree strongly. The 20-flag sample below holds at least four
different things. Real miscodes are one of them, but the others are Jev being
wrong, correctly coded work outside the profile, and IT procurement vehicles.
A merged "worth reading" rate would add those piles together. It could come
back high on vehicles alone and promote a rule that also admits staff
augmentation.

So every flag is labelled into one of four kinds, defined here before any flag
exists or any label is recorded. Any promotion condition is stated per kind,
never on a merged positive rate.

## Label vocabulary

Defined before labelling, for the same reason `profile_gap` and `unsure` had to
exist before the REF-004 round. Apply them in this order and take the first
that fits:

1. **`vehicle`**: the notice qualifies suppliers onto, refreshes, or issues a
   call-up under a supply arrangement or standing offer for IT professional
   services (TBIPS, SBIPS, ProServices streams, PASS IT streams and the like),
   whatever it is coded. It comes first because the vault treats getting onto
   a vehicle as valuable in itself (CLAUDE.md, core loop step 4). So it is its
   own pile, not evidence for or against the codes.
2. **`jev_wrong`**: the principal purchase is not IT work. Jev's profile family
   does not describe it, whatever the filed codes say. Example from the
   sample: television signal and cable services placed in 8116.
3. **`miscoded`**: the principal purchase is IT work that a profile family
   describes, and the filed codes do not describe it. Examples: a GIS data hub
   filed under biological science services, and a GUPTA-to-.NET conversion
   filed under IT components.
4. **`out_of_scope`**: the filed codes describe the purchase acceptably, and
   the work is IT-adjacent but outside what the profile lists. Examples: IT
   audit filed as audit services, IT roles filed as temporary personnel (staff
   augmentation, which the vault calls a mismatch), and vendor training.

**Not provided: `unsure`, `no_description`, `profile_gap`.** The vocabulary is
the four kinds above, as decided. If a flag cannot be given one of them, it is
left unlabelled and reported as a count with the reason. It is not forced into
a kind. Adding a kind needs a commit to this file before the next label is
recorded.

**Labelled blind**, through `filter_audit.blinding`. Jev's choice and the mass
band are withheld until a disposition is recorded, and flags are mixed with an
equal number of non-flagged coded rejects. Otherwise a flag-only queue tells
the reviewer Jev's verdict.

## The rule

- **Population.** Coded notices past the closed, exclusion, construction and
  jurisdiction gates **whose filed codes reject**. A coded admit cannot be
  flagged, so asking Jev about it would be a wasted call. Coded admits still
  make no API call.
- **Flag** when Jev's summed probability across the four profile options
  (8111, 8116, 4323, 80101507) is at least **0.90**, compared at 0.01
  resolution (`predicates.mass_reaches`; see the REF-006 defect entry).
- **Flag only.** A flagged notice is not admitted. **The corpus is unchanged by
  this rule**, and a test asserts it: the admission mask is identical with and
  without coded imputations.
- **Uncoded branch unchanged.** t = 0.20 admits as today. Uncoded notices are
  imputed first, in their own call, so the flag backfill can never use up
  their time budget.
- **Once per notice.** The store is keyed on notice id, model and question
  hash. A notice open for weeks is flagged once. An amended notice is not
  re-flagged, and one amended out of flag range is not un-flagged. The record
  keeps the content hash it was flagged on.
- **The count is in the funnel on every run**, including zero, with a separate
  count of coded rejects not evaluated (no key, API error, time budget). A
  count is not a probability; it goes into provenance like REF-006's counts.

## Promotion: deliberately not set

**Status is NOT EVALUATED until at least 20 flags exist.** Until then the rule
is flag-only, with no default in either direction.

**The promotion number is not pre-registered here, and that is deliberate.**
It will be pre-registered in its own commit, after the human has read the
first ten flags and knows what the four piles actually look like. The reasons:

- **Time to n = 20 is two to five months.** Expected volume is about 9 flags
  from the backfill of the open feed, then about 8 a month. Both figures carry
  the archive-transfer caveat below, so they are uncertain by about 2×.
- **The composition of the piles is unknown.** A threshold set now would be an
  absolute number set against a pile nobody has read. That is the error the
  human rejected in REF-004 phase 1.
- **At n = 20 each label moves a pile's share by 5 points.** For example, 5 of
  20 is 0.25 with a Wilson 95% interval of 0.11 to 0.47. Whatever number is
  pre-registered must be stated knowing this resolution.
- **The unit may need to move.** Vehicle notices cluster: TBIPS tiers, streams
  and refreshes arrive as separate notices for one arrangement. The count
  reports distinct notices. The human's reading of the first ten decides
  whether the unit for the promotion rule becomes the solicitation series
  instead.

**0.90 was chosen from the same 23,314 notices the REF-004 sweep was fitted
on.** Before any promotion there must be a split-half check: 50 seeded splits
of the coded archive, reporting the half-B minus half-A spread in the flag rate
and in the REF-005 overlap share at 0.90. **Its limit, stated now:** at a fixed
threshold it measures sampling noise in those quantities. It cannot test the
choice of 0.90, because that choice was a human reading of the data, not a
procedure that can be re-run on a half. The labelled piles cannot be split-half
checked at n = 20 at all.

## Evidence, 2026-09-25 (cache only, no API calls)

Scratch scripts over `_scored_rows()` (the REF-004 cache, question
`c5725ac4..`, `jev-1.13.0`) and `filter_audit.predicates`. The call ledger read
23,390 before and after.

**Caveats that apply to every number in this section:**
- **Population.** The archive is WS and cb only. PW, SSC and MX file no codes,
  and the feed's coded notices are not archive notices.
- **`closed` cannot be evaluated** on the archive, because every notice would
  fail it. Gates 2–4 were evaluated.
- **Archive rates have failed to transfer before.** REF-006's archive-derived
  admit rate missed the live feed by about 2× (8 predicted, 15 observed).
- **The cost and volume figures are the right order of magnitude, and no more
  precise than that.**

### 1. How many coded notices the rule would flag

**297 of 23,314.** These are coded rejects that pass gates 2–4 with mass ≥ 0.90
(301 before gates 2–4).
- **Source:** cb 246, WS 51.
- **Scale:** 1.5% of the 19,478 gated rejects, against 1,843 coded admits today.
- **Jev's choice:** 80101507 ×113, 8111 ×107, 8116 ×42, 4323 ×35.

**Mass band** (the resolution the store keeps):

| band | flags |
|---|---|
| 0.90–0.95 | 71 |
| 0.95–0.99 | 99 |
| 0.99+ | 127 |

**Sensitivity.** The count is not flat around the threshold:

| mass ≥ | 0.80 | 0.85 | 0.90 | 0.95 | 0.99 |
|---|---|---|---|---|---|
| flags | 390 | 354 | **297** | 226 | 127 |

### 2. Cost

Tokens come from a least-squares fit on 23,362 measured verdicts:
`input_tokens = 3,464 + 0.245 × chars`.
- **Overhead:** the intercept matches the separately measured 3,471-token
  empty-notice overhead (REF-004).
- **Checked on real feed verdicts:** on the 33 in the gate cache, the fit's
  mean error is −4 tokens and its largest is 343.
- **Price:** $0.042 per million input tokens, output free. Read on
  docs.typesafe.ai/models on 2026-09-25, unchanged from REF-004.

| | notices | input tokens | cost |
|---|---|---|---|
| Backfill, open feed of 2026-09-13 | 594 coded rejects past gates 1–4 | 2.32M | $0.097 |
| Daily, new or amended content | 16.8/day | 66.6k/day | $0.0028/day, about $1.02/year |

- **The daily rate is a diff of two real feeds,** the 2026-08-28 snapshot and
  the 2026-09-13 feed. It found 269 notices with new content in 16 days: 234
  new ids and 35 with amended text. It undercounts notices that opened and
  closed inside the window.
- **Asking only code-rejects** excludes the feed's 67 coded admits.
- **None of the 594 is in the REF-004 cache under the same content hash,**
  because archive and feed text differ. So seeding the gate cache from it would
  save nothing.
- **Time.** Sequential gate calls ran at about 0.5 s each (33 calls in 15 s).
  So the backfill is about 5 minutes, inside the gate's 10-minute budget.
  Notices not reached are counted as not evaluated and are picked up on the
  next run from the cache.

### 3. Twenty flags (seed 20260923)

This is a reading of titles only, not descriptions, and it is not a labelling.
The kinds in the right-hand column are provisional and are not records.

| # | title | filed | Jev | provisional reading |
|---|---|---|---|---|
| 1 | EO Data Analytic System Prototype | 81141901 product R&D | 8111 | miscoded? |
| 2 | Satellite AIS data services | 81151605 satellite imaging | 8111 | unclear |
| 3 | TBIPS RFP, DEFENCEX Stream 1 for DND | 80161604 † | 80101507 | vehicle |
| 4 | ML engineering and scientific services | 92111700 military science | 8111 | miscoded? |
| 5 | Television signal service, CFB Trenton | 72100000, 72141116, 72151601 | 8116 | jev_wrong |
| 6 | RFP, Phoenix Operations | 80101504, 80101508 † | 8111 | miscoded? |
| 7 | Digital Enforcement: Technology and Tool Insights | 80111510 | 80101507 | unclear |
| 8 | TBIPS Tower Support System Administrators L2 | 80161604 † | 8111 | vehicle |
| 9 | NPP A.7 Programmer/Analyst | 80161604 † | 8111 | out_of_scope? |
| 10 | SSC TBIPS Tier 1 | 80161604 † | 80101507 | vehicle |
| 11 | TBIPS Operations Services | 80101706 † | 8111 | vehicle |
| 12 | Research for machine technical translation | 82110000 | 80101507 | unclear |
| 13 | System Administrator L1, CJWC | 80111600 temp personnel | 8111 | out_of_scope |
| 14 | RFI FDC Transaction Management System | 84121703 | 4323 | miscoded? |
| 15 | IM/IT Advisory Services, Justice | 80100000 † | 80101507 | miscoded? |
| 16 | IT and Systems Audit Services, PASS Stream 3 | 84111600 audit | 80101507 | out_of_scope or vehicle |
| 17 | TC personnel system GUPTA → .NET conversion | 43200000 | 8111 | miscoded |
| 18 | Cable or relay television services | 83111801 | 8116 | jev_wrong |
| 19 | GIS Hub for spatial datasets, maintenance | 81170000 | 8111 | miscoded |
| 20 | Royal Military College Net Testing | 92121700 | 80101507 | unclear |

† carries an 8010 or 8016 code outside `80101507` (REF-005).

Among all 297, 18 flags carry 8011 (staff and HR services). That is where the
staff-augmentation risk sits.

### 4. Overlap with REF-005

Would a flag be admitted anyway if a REF-005 change shipped?

| REF-005 variant | flags admitted | share |
|---|---|---|
| list 8010 and 8016 whole | 127 | 43% |
| add L4 80161604 only | 52 | 17.5% |

The other 170 carry no 8010 or 8016 code. They spread across 8011 (18),
8311/8312 telecom (14/13), 8114 (13), 8110 (11), 7215 (11), 8411 (11) and
4322 (10), counted per notice.

**So this rule mostly does not measure the REF-005 profile gap.** It also does
not cleanly measure miscoding (see above). It measures a strong disagreement
between codes and Jev, and the labels are what split that disagreement into
kinds.

## Structure

- **`predicates.coded_flag(notice, criteria, imputation)`** returns a
  `CodedFlag` or None. It is pure, and `CodedFlag` has **no mass field and no
  admit field**, only the band. The raw mass never leaves predicates on the
  flag path. `stage_relevance` is unchanged: it already ignores an imputation
  on a coded notice.
- **`filters.filter_tenders`** takes a separate `flagger`. Uncoded notices are
  imputed first through the existing `imputer`, exactly as before. Coded
  rejects past gates 1–4 go to the `flagger` afterwards. Coded imputations are
  never passed to `stage_mask`.
- **The store is `data/coded_flags.jsonl`, committed and append-only.** Each
  line holds: notice id, title, filed codes, Jev's choice (the top profile
  family; at mass ≥ 0.90 the overall top choice is necessarily a profile
  option), mass band, model, question hash, content hash, filter version and
  first-seen date.
  - **No raw mass goes into git.**
  - **Not under `.cache/`.** CI restores that from a rolling Actions cache, and
    a single missed restore would silently reset the evidence.
  - **One writer.** The ingest writes the store only with `--record-flags`,
    which CI passes, and CI commits it with the digest. Local runs print the
    count without writing, so two machines never append to the same file.
- **Kept away from Claude.** The store's name and the band field join the AST
  scan's forbidden tokens for tender_tools, mcp_server and the skills. The
  flag fields join `WITHHELD_UNTIL_DISPOSED`.
- **Deliberate change to an existing test.** "A coded notice never produces an
  API call" becomes "a coded notice whose codes admit never produces an API
  call".

## Known gaps, stated at the start

- **Golden set.** The CI gate (`evaluate-golden --gate`) replays frozen rows
  with no imputation, so it does not exercise the flag path. That costs
  nothing here, because flags change no admission.
- **`--skip-unchanged`.** An unchanged feed evaluates no flags that day, which
  is correct, since there is no new content.
- **Nothing here is labelled.** The provisional readings in item 3 are not
  records.

## Status

PROPOSED in lifecycle terms, `mode: flag_only`, promotion NOT EVALUATED.
