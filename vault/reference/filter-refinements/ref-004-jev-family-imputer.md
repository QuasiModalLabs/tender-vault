---
id: ref-004-jev-family-imputer
status: PROPOSED
source_filter_version: fv-c065f51c
proposed_by: human
model: jev-1.13.0
proposed_change:
  kind: imputer
  target: scripts/filter_audit/predicates.py::stage_relevance (uncoded branch)
  variant: null
  summary: >
    Impute a UNSPSC family with Jev for notices that file no codes, so the
    profile's own unspsc_families can decide them instead of keywords alone.
    Phase 1 measures the imputer against the production keyword branch on
    coded notices; it changes nothing in production.
failure_categories_addressed:
  - vocabulary_mismatch
  - missing_structured_field
supporting_examples: []
evaluation_results: []
promoted_to_production: null
---

# REF-004: a code for the notices that file none

## Problem

PW, SSC and MX file no UNSPSC at all: 4,976 + 1,279 + 949 archive notices. They
can only reach the keyword branch of `stage_relevance`, so their admission
depends on whether the prose happens to contain a competency. REF-002 showed
how narrow that is: one spelling (`cyber security` vs `cybersecurity`) decided
the largest federal IT buyer's notices.

## What Jev does here, and what it does not

Jev reads the English title and description and names the commodity family the
notice most resembles. **It imputes a code; it does not judge fit.** The
question names no company, competency or preference. Whether an imputed family
is one we buy is decided afterwards by the profile's `unspsc_families`, exactly
as for a publisher's code. The profile remains the only place fit is decided.

## Design (scripts/family_imputer)

- **Options (35), frozen in the question hash:**
  - **4 profile options:** 8111, 8116, 4323, and `80101507` kept as an L4.
  - **18 sibling families:** every other L2 family in segments 43, 80 and 81. 8010 appears as "8010 other than 80101507".
  - **12 segment options:** the top segments by coded-notice volume, which are 72, 25, 41, 56, 78, 46, 86, 24, 76, 40, 30 and 77.
  - **`none of these`** for the tail.
- **UNSPSC side only.** The PSPC reference file is the GSIN↔UNSPSC linkage file. It is read through a three-column UNSPSC whitelist, so the GSIN columns never load. The crosswalk prohibition stands.
- **Blind.** The imputer's input type has two fields: title and description. Codes are read only after both methods have answered.
- **Pinned model** `jev-1.13.0`. A response from any other version is refused.
- **Frozen question**, `QUESTION_SHA256 = c5725ac4e364c103e429025214c7d4b1cb718f1d89cca5f750be59f17a055158`. Any drift, including an edit to the profile families, refuses to run.
- **Cache** keyed on notice id, content hash, model version and question hash.
- **Kept out of the product.** Nothing in tender_tools, mcp_server, ingest or predicates imports the package, which the tests check. Imputed fields are listed in `WITHHELD_UNTIL_DISPOSED`. No Jev number appears in a briefing or dossier.

## Pre-registered decision rule

Committed before any imputation call, and not revised afterwards. The run
refuses unless this section, as first committed, names question
`c5725ac4e364c103e429025214c7d4b1cb718f1d89cca5f750be59f17a055158` and still
matches the working tree byte for byte.

**Population.** Every coded notice in data/notices.db (23,314; all WS or cb),
imputed blind to its codes by `jev-1.13.0`.

**Definitions (admit class).**
- *Publisher admits*: `ingest.matches_unspsc_families(codes, unspsc_families)`
  is non-empty. This is production's own coded-branch test.
- *Jev admits*: Jev's highest-probability option is a profile option.
- *Keyword admits*: `ingest.matched_competencies` fires on the full
  title + description. This is production's own uncoded-branch test, run on
  the same notices, blind to their codes.
- *Recall*: of publisher admits, the share a method admits. *Precision*: of a
  method's admits, the share the publisher admits.

**Rule (point estimates; Wilson 95% intervals reported beside them):**

- **PROCEED to phase 2** only if all three hold:
  - Jev recall ≥ keyword recall + 0.10
  - Jev precision ≥ keyword precision − 0.05
  - Jev recall ≥ 0.60
- **KILL** if either holds:
  - Jev recall ≤ keyword recall
  - Jev precision < keyword precision − 0.15
- **Otherwise INCONCLUSIVE.** The decision returns to the human, with no default either way.
- **NOT EVALUATED** if any coded notice lacks a verdict, or a recall or precision is undefined.

The absolute floor stops a weak comparator from carrying the rule. Keyword
recall of 0.30 against Jev at 0.40 passes "+10 pts" while still missing six
admits in ten.

**Read every number with these two caveats beside it:**

1. **Population.** Agreement measured on WS and cb notices is evidence about
   the model. It is not a measurement on the PW, SSC and MX notices the imputer
   would actually serve.
2. **The keyword comparator's handicap.** `matched_competencies` never runs on
   WS or cb notices in production, because they are decided on their codes. A
   large keyword deficit here is therefore partly an artefact of the
   evaluation population, not evidence about PW, SSC and MX prose. This does
   not change the rule; it changes how a big gap is read.

**Not headline:** overall set agreement across the 35 options, with its
majority-class baseline, is a diagnostic. It is dominated by the non-IT
segments, where a constant answer scores well.

## Cost structure, and the candidate variant B

Added 2026-09-24, after the pilot and before the full run. This section is
outside the pre-registered rule and changes nothing in it.

**Fixed overhead is measured, not fitted.** `python scripts/family_imputer
measure-overhead` sends the frozen question over the run's state shape with
empty title and description. It returned **3,471 input tokens** on
`jev-1.13.0`, question `c5725ac4..`.

The 200 pilot calls averaged 3,869.0 input tokens (median 3,665.5, p99 6,755,
max 8,449, sum 773,808). So **about 90% of every call (3,471 / 3,869) is the
question**: the instructions plus the 35 option criteria. The notice itself
contributes about 398 tokens on average. Price per docs.typesafe.ai/models
(read 2026-09-23): $0.042 per million input tokens, output free. The whole
coded set is about 90.2M input tokens, about $3.79.

**The question stays frozen for this run.** Trimming the option descriptions
would change `QUESTION_SHA256`. Every cached verdict would be keyed out of
reach, and the rule above would no longer describe the question that was run.

**Candidate variant B: trimmed option descriptions.** It is recorded here and
not started. It is to be proposed only if this run returns INCONCLUSIVE. It
would be a new question with a new recorded hash, a new pre-registered rule,
and a fresh run, compared against this run as a separate measurement. Nothing
cached under `c5725ac4..` carries over to it. `proposed_change.variant` stays
null for this run.

## Phase 1 measurement, 2026-09-24

Produced by `python scripts/family_imputer report`, over verdicts cached under
question `c5725ac4..` and model `jev-1.13.0`. Coverage: 23,314 of 23,314 coded
notices, 0 missing, 0 errors in the run.

**Headline, admit class (1,843 publisher admits):**

| | recall | precision |
|---|---|---|
| Jev (top choice) | 0.796 (0.777–0.814), 1467/1843 | 0.703 (0.683–0.722), 1467/2087 |
| Keyword branch (production) † | 0.565 (0.543–0.588), 1042/1843 | 0.469 (0.448–0.489), 1042/2224 |
| Majority baseline (never admit) | 0.000 | undefined |

**Population:** every coded notice is WS or cb. These numbers are evidence
about the model, not a measurement on the PW, SSC and MX notices the imputer
would serve.
† **Keyword handicap:** `matched_competencies` never runs on WS/cb notices in
production, so part of the 23-point gap is an artefact of this population.

**Pre-registered rule: PROCEED.** Every condition holds with margin:

| Condition | Required | Measured |
|---|---|---|
| Recall gain over keywords | ≥ +0.10 | +0.231 |
| Precision change vs keywords | ≥ −0.05 | +0.234 |
| Jev recall | ≥ 0.60 | 0.796 |

It holds within each source system separately:

| Source | Jev recall | Jev precision | Keyword recall | Keyword precision |
|---|---|---|---|---|
| WS (n = 10,193) | 0.749 | 0.713 | 0.442 | 0.467 |
| cb (n = 13,121) | 0.813 | 0.700 | 0.610 | 0.469 |

**Diagnostics (not headline):**
- Set agreement is 0.649, against 0.291 for always answering "none of these".
- Strict agreement on single-label notices is 0.638.
- Jev confuses 8111 and 80101507 in both directions (126 and 121 notices). Both are profile options, so this does not move the admit decision.
- 81 notices the publisher coded "8010 other" went to 80101507, which counts against precision.
- The admit rate follows P(profile options): 0.012 in the lowest decile, 0.800 in the top one.
- 242 publisher admits sit in the 0.0–0.1 bucket. That is where the recall loss is concentrated.

**What these numbers exclude or assume:**
- Precision is measured against publisher codes, which contain miscodes. The 30 sampled disagreements in `data/family_imputer/report.json` are the first look at how many of Jev's "errors" are publisher errors. They are unlabelled as of this entry.
- 22 notices whose codes are all unmappable are excluded from set agreement only.
- 1 notice was truncated.

**Tokens and cost, from the call ledger:**
- 23,342 calls, 90,111,081 input tokens: **$3.78** at $0.042 per million input (docs.typesafe.ai/models, read 2026-09-23). Output is free.
- The pre-run estimate was $3.79.
- 12.4M output tokens were returned and are unbilled under current pricing.
- An aborted pilot on 2026-09-24 sent an unknown number of calls, and only 25 are in the ledger. All were rejected 401 and unbilled (fixed in 37731b4).

## Phase 2 analysis, 2026-09-24 (post hoc, not pre-registered)

Produced by `python scripts/family_imputer sweep` and `misses`, from the cache
only. The call ledger shows 23,342 calls before and after. Recall is against
publisher codes. The phase 1 population caveat and keyword handicap apply
throughout.

**Why this was run.** A false negative is a tender never seen; a false positive
is seconds of reading. Recall is the number that matters, and phase 1's rule
weighed the two alike.

**Admit on probability mass.** A notice is admitted if the summed probability
on the four profile options is at least t. Full curve: `data/family_imputer/sweep.csv`.

| t | recall | precision | admits |
|---|---|---|---|
| top choice (phase 1) | 0.796 | 0.703 | 2,087 |
| 0.05 | 0.887 (0.872–0.901) | 0.539 | 3,033 |
| 0.03 | 0.8996 (1658/1843), just under 0.90 | 0.513 | 3,235 |
| **0.02** | **0.909 (0.895–0.922)** | **0.489 (0.473–0.506)** | **3,425** |
| 0.01 | 0.925 (0.912–0.936) | 0.450 | 3,790 |

- **Recall 0.90 is reachable on the full set at t = 0.02.** Against the top-choice rule, that is 209 more IT notices for 1,338 more admits, about 6.4 extra reads per notice recovered.
- **The split-half check says 0.02 is fragile.** Choosing t on a seeded half A picks 0.01, not 0.02, because 0.02 falls short of 0.90 on that half. At 0.01, half B's recall is 0.941 (P 0.452). The halves differ by about 3 points at a fixed t, which is the resolution this analysis actually has.
- **Jev's probabilities are rounded to 0.01.** Summed mass moves in steps of 0.01, so any t in (0, 0.01] selects the same notices. **t = 0.01 is the floor, and recall 0.925 is the ceiling of the threshold approach.**

**The misses have no mass on any profile option.** Of the 208 publisher admits missed at t = 0.05:

| Profile mass | Notices |
|---|---|
| 0.00 exactly | 139 |
| 0.01 | 28 |
| 0.02–0.04 | 41 |

The 139 at exactly 0.00 are unrecoverable by any threshold. Of the 376 phase 1 top-choice misses, 139 sit at 0.00 and 168 carry 0.05 or more.

**Union with the keyword branch.** A notice is admitted if the mass rule fires or `matched_competencies` fires. The union reaches 0.901 at t = 0.04 (precision 0.435, 3,817 admits) and 0.931 at t = 0.01 (precision 0.392, 4,378 admits). The two methods mostly fail on the same notices:

| t | found by both | Jev only | keywords only | neither |
|---|---|---|---|---|
| 0.05 | 1,021 | 614 | 21 | 187 |
| 0.01 | 1,030 | 674 | 12 | 127 |

**The 187 found by neither at t = 0.05.** These are 121 cb and 66 WS notices. 127 of them have profile mass 0.00. The profile codes they carry are 117 in 8111, 76 in 4323, 54 in 8116 and 10 in 80101507, counted per code. Six codes account for 75 notice-code occurrences: 81112000 ×23, 81162308 ×17, 81110000 ×10, 80101507 ×10, 81111809 ×8 and 43230000 ×7.

The shapes below come from reading titles only, not full descriptions, so they are a reading, not a finding:

1. **A profile code filed as a generic service code.**
   - 81162308 appears on first aid, nursing simulation, driver instruction and fall protection.
   - 81162305 appears on psychological risk assessments at correctional institutions (6 notices).
   - 81111809 appears on dust collectors, a roof chiller and a fire panel.
   - 43232605, 43232504 and 81111705 appear on floating docks, a tractor GPS and field cabins.
   - Jev names the actual purchase. These look like publisher miscodes.
2. **Data and statistics filed under 81112000.** Examples are national surveys, immunization coverage, lake sediment and magnetotelluric data. Jev chose 8113 Statistics or 8115 Earth science. They are plausibly not IT work.
3. **IT-adjacent, with the principal purchase outside IT.** Examples are vendor training (Cisco MDS, Nutanix, Fortinet, ArcGIS, an ML/AI workshop), which Jev put in 86 Education. Building automation control system maintenance went to 72, security system replacements to 46, and scanners and label printers to 4321 Computer Equipment. Whether we want these is a fit question for the profile, not an imputation error.
4. **Not procurement at all.** Examples are "DO NOT USE", "Cancelled", "created in error", RFI and consultation summaries, and Innovative Solutions Canada calls filing 10–59 codes. One ECCC RFI files 398 codes.
5. **Consulting and administrative roles filed under 80101507, 81111819 or 81162310.** Examples are project managers, QA specialists, procurement and financial specialists. Jev chose 8010 or 8011.

On this reading, groups 1, 2 and 4 are the publisher's code being wrong, uninformative or not describing a purchase, rather than the model missing IT work. The 30-notice disagreement labels are what would test that reading; none have been recorded yet.

**Recall weighted by bid disposition: not computed.**
- Only 1 of the 23,314 coded notices joins to a vault disposition (an archived tender).
- The two watching tenders are newer than `notices.db` (last publication 2026-08-13).
- The one other human-disposition store, `filter-reviews.jsonl`, has 1 record, and it judges relevance, not bid.
- One case cannot support a weighted recall, so there are no weights and no number.

## Status

PROPOSED, and naming no variant. Phase 1 is an evaluation of an imputer, not a
filter variant, so `evaluate-refinement` has nothing to score and refuses. That
is intended. A phase 2 variant would be written only if the rule above says
PROCEED, or the human decides an INCONCLUSIVE result is worth it.
