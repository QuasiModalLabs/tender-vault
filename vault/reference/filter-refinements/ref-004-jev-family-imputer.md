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

## Status

PROPOSED, and naming no variant. Phase 1 is an evaluation of an imputer, not a
filter variant, so `evaluate-refinement` has nothing to score and refuses. That
is intended. A phase 2 variant would be written only if the rule above says
PROCEED, or the human decides an INCONCLUSIVE result is worth it.
