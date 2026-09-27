---
id: ref-008-coded-imputed-admission
status: PROPOSED
mode: admit
source_filter_version: fv-dee8e8a8
proposed_by: human
model: jev-1.13.0
proposed_change:
  kind: predicate_code
  target: scripts/filter_audit/predicates.py::stage_relevance (coded branch) and scripts/ingest/filters.py
  variant: null
  summary: >
    Admit a coded notice whose filed codes reject when Jev's summed probability
    across the four profile options is at least 0.60. Uncoded gate unchanged at
    0.20. ref-007's flags are unchanged and remain flag-only and NOT EVALUATED.
failure_categories_addressed:
  - structured_field_error
supporting_examples: []
evaluation_results: []
promoted_to_production: null
---

# REF-008: admitting coded notices the codes reject, at mass 0.60

Follows REF-004 (the imputer), REF-005 (the families the profile does not
list), REF-006 (the uncoded gate) and REF-007 (flags on coded notices). This
file records the decision, the reason for its threshold and the expected
effect. It is committed on its own, before any code.

## Intent, recorded so it is not relitigated

**Missing a tender costs real money. Reading an extra notice costs Claude a few
seconds. Precision is traded away deliberately, in favour of fewer false
negatives.**

This is REF-006's design intent ("the imputer's job is to widen the funnel;
Claude's job is to narrow it"), now applied to the coded branch. A false
positive here is a notice in the briefing that turns out not to be worth
bidding on. That is an accepted cost, not a defect to be tuned away.

The one kind of false positive this rule is shaped to avoid is **staff
augmentation**, because the profile exists to steer away from it. Widening
into staff augmentation is not just more noise. It skews the corpus
systematically toward work the profile says we do not want.

## The rule

- **Admit a coded notice when its filed codes reject it and the summed
  probability across the four profile options (8111, 8116, 4323, 80101507) is
  at least `t = 0.60`**, compared at Jev's own 0.01 resolution
  (`predicates.mass_reaches`).
- **Only coded notices past the closed, exclusion, construction and
  jurisdiction gates whose codes reject.** A coded notice its codes admit is
  admitted on its codes, as today, and produces no call.
- **No new calls.** Since REF-007 the flagger already imputes exactly this
  population on every run. REF-008 reads the same answers.
- **Fallback: the codes decide.** If the imputer does not answer for a notice
  (no key, API error, drifted question, time budget), the notice is decided on
  its codes and rejected, exactly as before REF-008. The funnel counts these as
  not evaluated. The ingest never fails for this.
- **The uncoded gate is unchanged at 0.20.** Lowering it adds one notice: on
  the 2026-09-13 feed, 32 of the 48 uncoded notices at the gate sit at exactly
  0.00 and one sits between 0 and 0.20.

## Why 0.60: where the 8011 share breaks

The threshold was read off the archive in bands, not chosen as a round number.
Population: the 19,478 coded archive rejects (WS and cb) that pass exclusion,
construction and jurisdiction, from the REF-004 cache. Cache only, no calls;
the call ledgers read 23,390 and 33 before and after.

Categories were fixed before counting, in precedence order:
1. **vehicle:** `ingest.classify_notice` returns `qualification` or `call_up`.
   The TBIPS column is the subset where the classifier's TBIPS/SBIPS name or
   known arrangement-number patterns match.
2. **staff aug:** a filed code in 8011, human resources services (including
   801116, temporary personnel).
3. **gap:** a filed code in 8010 other than 80101507, or in 8016 (REF-005).
4. **other:** none of the three.

These are code-based stand-ins, not labels. Nobody has read these notices.

| mass band | n | vehicle | of which TBIPS | staff aug | gap | other | any 8011 code |
|---|---|---|---|---|---|---|---|
| 0.20–0.30 | 151 | 62 | 36 | 0 | 13 | 76 | 32 |
| 0.30–0.40 | 125 | 61 | 39 | 1 | 13 | 50 | 38 |
| 0.40–0.50 | 101 | 49 | 41 | 1 | 14 | 37 | 33 |
| 0.50–0.60 | 74 | 31 | 22 | 2 | 10 | 31 | **17 (23%)** |
| 0.60–0.70 | 59 | 20 | 8 | 2 | 9 | 28 | **5 (8%)** |
| 0.70–0.80 | 56 | 18 | 13 | 3 | 5 | 30 | 5 |
| 0.80–0.90 | 93 | 21 | 12 | 4 | 9 | 59 | 4 |
| 0.90+ | 297 | 128 | 86 | 11 | 51 | 107 | 18 |

- **8011 is 27% of the 0.20–0.60 bands (120 of 451) and 6% at 0.60 and above
  (32 of 505).** The break falls between 0.50–0.60 (23%) and 0.60–0.70 (8%).
- **Nearly all of those 8011 notices are also vehicles**: TBIPS-style call-ups
  filed as temporary personnel. That is why the staff-aug column, counted after
  vehicles, is nearly empty.
- **The 8011 test undercounts staff augmentation.** A single-role requirement
  filed under 8111 or 8016 is not caught. The true share is higher in every
  band, and nothing here says by how much.
- **Vehicles are about 40% of every band**, so raising t does not screen them
  out. The vault treats getting onto a vehicle as valuable (CLAUDE.md, core
  loop step 4), so they are admitted, not excluded.

At 0.60 and above, the admitted set's composition is vehicle 37% (TBIPS-named
24%), staff aug 4%, gap 15% and other 44%, with any 8011 code at 6%.

## Expected effect

| | today (CI, 2026-09-26) | with REF-008 |
|---|---|---|
| corpus | 94 | **about 109** |
| standing admits over codes | 0 | **about +15** (rate 0.0259 × 590 coded rejects) |
| daily inflow | none | **about 0.44/day** (0.0259 × 16.8 new coded rejects/day) |
| API calls | the flagger's | unchanged |

**About 9 of the +15 are REF-007's current flags.** Every flag has mass of at
least 0.90, which is at least 0.60, so every flag is also admitted.

**The first CI run lands the full +15.** The flagger's 2026-09-26 backfill put
the 590 coded rejects in the gate cache, so that run is served from the cache.

## Caveats on every number above

- **Everything below 0.90 is an archive rate with nothing observed against
  it.** At 0.90 the archive predicts 1.52% and CI observed 9 of 590 (1.53%).
  That is the only calibration point.
- **The last archive rate carried to the feed was off by 2×.** REF-006
  predicted 8 uncoded admits and 15 were observed. Read +15 and 0.44/day as
  order of magnitude.
- **The archive is WS and cb only**, and archive text differs from feed text.
- **The category shares are archive shares** from code-based stand-ins, not
  labels.
- **The first weeks of real numbers supersede everything here.** The funnel
  and the committed digest record the observed counts from the first run.

## REF-007 stays open, flag-only and NOT EVALUATED

REF-008 does not promote, close or replace REF-007. The two answer different
questions:
- **REF-007 asks why the filed codes and Jev disagree strongly.** Its flags
  exist to be read and labelled into kinds: `vehicle`, `jev_wrong`, `miscoded`,
  `out_of_scope`. That tells us whether and how the filed codes are wrong.
- **REF-008 asks whether a notice should be read at all**, and answers on the
  model's confidence alone.

Admitting on confidence cannot answer REF-007's question. A notice admitted at
0.97 might be a miscode, Jev being wrong, or a vehicle, and admission says
nothing about which. So the flag stream, the store, the label vocabulary, the
n ≥ 20 minimum and the promotion rule are all unchanged. REF-007's promotion,
if it ever comes, would be a decision about the codes, and REF-008 does not
pre-empt it.

**What does change for REF-007, stated rather than hidden:**
- **Every flag is now also admitted**, so flagged notices appear in briefings.
  A labeller who has read the briefing may recognise a notice on the blind
  sheet. The controls (coded rejects not in the store) mostly sit below 0.60
  and are not admitted, so appearing in the corpus now leans toward "flag".
  This weakens REF-007's blinding. It does not remove it: labels remain
  content judgements about the notice, and the choice and band stay withheld.
- **The weakness is recorded, not designed away.** Redrawing REF-007's
  controls from the 0.60–0.90 band would put the controls in the corpus
  alongside the flags. It was rejected, because it would make the control set
  a function of the rule under test. That circularity is a subtler problem than
  the one it fixes. A documented weakness beats a circular design, and no
  labels exist yet, so nothing already recorded is affected.
- **The flagger's answers now feed two consumers.** REF-007's "Structure"
  said they go "to predicates.coded_flag and nowhere else". From REF-008 they
  also go to `stage_relevance`, for admission. `coded_flag` itself is unchanged
  and still decides nothing.
- **REF-007's "corpus unchanged" test is replaced, deliberately.** It asserted
  that the corpus is identical with the flagger on. That is now false by
  design. It becomes two tests:
  - Recording flags changes no admission: the corpus is identical with flag
    recording on and off, given the same imputations.
  - REF-008 admits exactly the coded rejects at or above 0.60.

## The probability still never reaches anything Claude reads

- **Corpus metadata:** a REF-008 admit carries `relevance_basis:
  imputed_over_codes`, plus `imputed_family` and `imputer_model`. No mass and
  no band. The keys are the pinned `RELEVANCE_METADATA_KEYS`, unchanged. Only
  a new basis value is added.
- **What the basis value tells a reader:** the notice cleared a fixed
  threshold. That is the same kind of fact `imputed` already carries for the
  uncoded gate, and it is not a score.
- **The basis is provenance, never a quality signal.** The briefing names it
  so a reader can see how a notice arrived. A reader who starts treating
  `imputed_over_codes` as a confidence tier, whether reading it first, last or
  with more or less trust, has brought back the score the briefing rules exist
  to prevent. The briefing skill says so in these terms.
- **Threshold constant:** `CODED_IMPUTER_THRESHOLD = 0.60` lives in
  predicates. The AST scan's existing token `IMPUTER_THRESHOLD` already
  matches it as a substring, and a test asserts that.

## Funnel, provenance and digest

- **The relevance line splits four ways:** by UNSPSC family, **imputed over
  rejecting codes (REF-008)**, by imputed family where no UNSPSC was filed
  (REF-006), and by keyword.
- **A REF-008 line is printed on every run, zeros included:** `Coded imputed
  admits (ref-008): A of M coded rejects at mass >= 0.6; E evaluated, U not
  evaluated`.
- **The REF-007 line drops "none admitted"**, which becomes false. Flags
  still admit nothing themselves; the line says how many flagged notices
  REF-008 also admitted.
- **Provenance and the digest frontmatter add `relevance_coded_imputed`**
  (admits over codes) **and `relevance_coded_not_evaluated`**. Counts only.

## Known gaps, stated at the start

- **Golden set.** The CI golden-set gate (`evaluate-golden --gate`) replays
  frozen rows with no imputation, so coded golden entries are judged on their
  codes there. It does not test the REF-008 path. This is the same gap
  REF-006 recorded.
- **Time budget.** Coded rejects not reached in the flagger's time budget are
  rejected on their codes that run and are picked up from the cache on the
  next. The funnel counts them.
- **Staff augmentation is not measured directly.** The 8011 stand-in
  undercounts it, and only reading the admits says how much.

## Review: NOT EVALUATED until four weeks of CI runs

- **No revert threshold is set here.** Whether a threshold would be crossed
  depends on a pile nobody has read, which is the error REF-004 and REF-007
  refused.
- **What will be reported**, per kind and never as one merged rate: observed
  standing and daily admits against the expected +15 and 0.44/day, and the
  counts of admits that are vehicles, carry 8011, carry gap codes, or are
  other. The same code definitions as above apply, and they live in code
  before the first admit.
- **What that n can resolve:** about 12 new admits in four weeks at the
  expected rate. At that n, one notice moves a kind's share by about 8 points.
  It can detect a gross miss in volume, such as a repeat of REF-006's 2×, or
  8011 arriving at several times its archive share. It cannot resolve a
  difference of a few points.
- **Any revert or adjust rule for later admits is pre-registered in its own
  commit**, after the human has read the first ten REF-008 admits.

### When the four weeks come back inconclusive

That is the expected outcome, since about 12 admits can only reveal a gross
miss. So **inconclusive does not extend the window by default. Reading decides.**

- **The review date is four weeks after the first CI run that carries
  REF-008, and no later than 2026-10-30.** The run's date is added to this
  file when it happens.
- **By that date, the human reads the first ten REF-008 admits**, in order of
  first admission. Each is labelled with REF-007's four kinds (`vehicle`,
  `jev_wrong`, `miscoded`, `out_of_scope`), as defined in code in
  `flag_labels.FLAG_LABEL_DEFINITIONS`. A notice that fits none is recorded as
  unassigned, with a reason. It is not forced into a kind. The vocabulary
  exists before the reading, as REF-007 required for its own.
- **The decision is one of KEEP, ADJUST or REVERT.** It is recorded in this
  file with the per-kind counts and the reason. It is stated per kind, never
  on a merged "worth reading" rate. Ten `vehicle` admits and ten
  `out_of_scope` admits are different findings.
- **If fewer than ten admits exist by the review date**, the ones that exist
  are read and **one** extension of four weeks is allowed, recorded here with
  its reason. A second extension is not allowed. At the end of the extension
  the status moves to KEPT, ADJUSTED or REVERTED on whatever has been read.
- **The status never stays NOT EVALUATED past the second date.** An
  evaluation state with no forcing date drifts into permanent by default.
  REF-007 already runs that risk with zero flags read, and REF-008 is written
  so as not to share it.

## Status

PROPOSED, `mode: admit`, review NOT EVALUATED until the date above. Committed
before the code.
