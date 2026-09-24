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

## Status

PROPOSED, and naming no variant. Phase 1 is an evaluation of an imputer, not a
filter variant, so `evaluate-refinement` has nothing to score and refuses. That
is intended. A phase 2 variant would be written only if the rule above says
PROCEED, or the human decides an INCONCLUSIVE result is worth it.
