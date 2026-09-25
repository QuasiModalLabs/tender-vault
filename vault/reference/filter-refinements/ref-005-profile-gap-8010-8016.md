---
id: ref-005-profile-gap-8010-8016
status: PROPOSED
source_filter_version: fv-c065f51c
proposed_by: human
model: null
proposed_change:
  kind: profile_config
  target: vault/profiles/my-company.md::unspsc_families
  variant: null
  summary: >
    Notices coded into families 8010 (other than 80101507) or 8016 are rejected
    on their codes at relevance and never reach keywords or any imputer. Scope
    how many that is, and which codes carry real IT work, before deciding
    whether any belong in unspsc_families.
failure_categories_addressed:
  - structured_field_error
supporting_examples: []
evaluation_results: []
promoted_to_production: null
---

# REF-005: the families the profile does not list

## Problem

`stage_relevance` decides a coded notice on its codes alone. If no code falls
in `unspsc_families`, the notice is dropped, and the keyword branch is never
consulted. The REF-004 disagreement reading found IT work coded into 8016 and
8010: `80161604` IT management services, `80101508` business intelligence
consulting, `80101604` project administration. It recorded these as
candidates for a `profile_gap` label.

This is separate from REF-004 and has nothing to do with Jev. It is a question
about the profile's family list.

## Evidence, 2026-09-24

Produced by `python scripts/family_imputer profile-gap`, from `data/notices.db`
and the REF-004 verdict cache. No API calls. The population is the 23,314
coded WS/cb archive notices.

- **1,127 notices** are rejected at relevance and carry at least one code in 8010 (other than `80101507`) or 8016.
  - 8010 only: 829. 8016 only: 256. Both: 42.
  - Sources: cb 1,005, WS 122.
- **1,115 of them pass the other active stages** (exclusion, construction, jurisdiction), evaluated through `filter_audit.predicates`. Of the 12 that don't, exclusion drops 6, construction 4 and jurisdiction 2.
- `closed` is not evaluated, because every archive notice would fail it.
- For scale: today's coded branch admits **1,843** of these 23,314. Listing both families outright would add about 61% to that.

**The gap codes are mostly not IT.** Top 15 codes (a notice can carry several):

| code | notices | Jev admits (top choice) | Jev mass ≥ 0.02 | description |
|---|---|---|---|---|
| 80101600 | 171 | 29 | 72 | Project management |
| 80101500 | 161 | 32 | 67 | Business and corporate management consultation |
| 80100000 | 111 | 18 | 37 | Management advisory services |
| 80160000 | 105 | 21 | 41 | Business administration services |
| 80101511 | 91 | 1 | 12 | Human resources consulting |
| 80101606 | 79 | 0 | 3 | Project monitoring and evaluation |
| **80161604** | **71** | **62** | **71** | **Information technology IT management services** |
| 80101706 | 70 | 16 | 28 | Professional procurement services |
| 80101508 | 67 | 24 | 35 | Business intelligence consulting |
| 80101604 | 48 | 4 | 11 | Project administration or planning |
| 80101504 | 44 | 1 | 9 | Strategic planning consultation |
| 80101510 | 30 | 5 | 11 | Risk management consultation |
| 80101513 | 29 | 8 | 14 | Process and procedures management consultation |
| 80161508 | 27 | 0 | 2 | Document destruction |
| 80161507 | 26 | 8 | 24 | Audio visual services |

**What Jev says about the 1,127.** It admits 207 by top choice, 394 by summed profile mass ≥ 0.02, and 454 at ≥ 0.01. Its most common top choices are:

| Jev top choice | notices |
|---|---|
| 8010 other | 470 |
| 80101507 | 131 |
| 8011 HR | 89 |
| 8111 | 60 |
| none of these | 57 |
| 8016 | 39 |

The production keyword matcher would fire on 271 of them. Production never asks it, because the notices are coded.

## The two sides

**For listing more of it.** One code behaves like the profile's existing
carve-out. `80161604` IT management services carries 71 gap notices, and Jev
puts 62 of them in a profile family by top choice (all 71 by mass). That is
the pattern that justified `80101507`: an island of real IT work inside a
family that is otherwise not IT.

**Against listing the families.** The profile already made this call for
8010. Its comment on `80101507` reads: *"Taking the L3 would drag in 19
notices of noise; taking the L4 takes the work."* The table above is that
argument at family scale. Project management, general management consulting,
HR consulting, procurement services, document destruction and AV services
dominate the counts, and Jev itself places most of them outside the profile
(470 in "8010 other").

## What this does not establish

- **Publisher codes are an ambiguous reference.** REF-004's labelled sample
  could assign a single kind to only 11 of 30 disagreements, so a code's
  description is not proof of what a notice buys.
- **Jev's admits here are agreement with a model, not ground truth.** They are
  recorded as evidence about where the IT work sits, not as a filter.
- **The population is WS/cb.** PW, SSC and MX file no codes and are
  unaffected by any family change.
- **Nothing here says what a change would do to the golden set.** A family or
  L4 addition would be a TESTING variant scored with `evaluate-refinement`
  before any decision.

## Status

PROPOSED, naming no variant. The profile is not edited. The next step, if
wanted, is to choose which codes to trial (whole families, or specific L4s such
as `80161604`), write that as a variant, and evaluate it against the golden set.
