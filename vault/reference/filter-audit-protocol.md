# Filter audit — protocol

Frozen 2026-08-20, before the first review was recorded. What follows is the
method. Results live in the artifacts it names — `filter-reviews.jsonl`,
`filter-golden-set.yaml`, `filter-refinements/` — so this file cannot be edited
into agreement with them afterwards. Same rule [[case-protocol]] states about
itself.

## The question

> What did the filter reject, why, what relevant work are we missing, what change
> would recover it, and does that change actually improve recall without
> unacceptable precision loss?

`README.md` has named this gap for some time: the vault only knows about tenders
that survived the filter, so it can improve precision but is blind to recall.
This is the audit that samples what the filter rejected.

## Two decisions, never conflated

**Production decision** — short-circuit, exactly reproducing `filter_tenders`.
Reports `admitted` and `first_rejecting_stage`, and says nothing about gates it
never reached, because production never asked them.

**Audit decision** — every stage evaluated independently against the original
notice. A notice can be `production=REJECT, first_rejecting_stage=construction`
while the audit records `relevance=pass`. That gap is the entire point:
`PW-23-01030114` is rejected as construction, and only the audit can tell you
the keyword branch would have admitted an elevator contract.

Equivalence between the audit path and `ingest.py` is proven rather than
asserted — `verify-equivalence` runs both over the same rows and requires an
empty symmetric difference. Agreement on an empty admitted set is reported as
**vacuous**, never as a pass.

## The seven stages

`closed`, `exclusion`, `construction`, `jurisdiction`, `relevance`, `value`
(inactive), `identifiable`. The last two are modelled precisely because they are
invisible in production: `value` is dead twice over, and `identifiable` is
enforced in `_write_chroma` after the funnel has already counted the row.

Outcomes are `pass | drop | skipped | inactive`. **`skipped` is not `passed`** —
a reader must be able to tell "we did not ask" from "we asked and it passed".

## Relevance is never one boolean

`(has_codes AND family match) OR (no codes AND keyword match)`. The branches are
mutually exclusive on purpose; the OR form readmitted a vapour-cloud explosion
study because the phrase contains "cloud".

Two reject modes, and they imply different fixes:

| branch | archive count |
|---|---|
| coded, family matched (pass) | 1,843 |
| **coded, wrong family (reject)** | **21,471** |
| uncoded, keyword matched (pass) | 1,092 |
| **uncoded, no keyword (reject)** | **6,121** |

Widening the competency list cannot recover a single one of the 21,471. Recording
both as `relevance=false` would destroy the primary signal of the whole exercise.

## The clock

`--as-of publication` is the default: each notice is judged against its own
`publication_date`, which is stored and immutable, so two runs on two machines on
two days agree. A single present-day `as_of` is deterministic but degenerate on
an archive — it closes 30,484 of 30,527 rows at stage 1 and the funnel says
nothing.

Replay always recomputes classification from raw fields. `notices.db` is
append-only, so its stored `opportunity_kind` came from whichever classifier ran
on first insert; 59 rows currently disagree with the live one, 34 of them stored
as `construction`. Trusting the column would audit a classifier that no longer
exists. The drift is printed every run rather than papered over.

## Review is blinded, and blinding is structural

The reviewer must not see the rejection reason, the failing gate, or the stratum
until a disposition is recorded. Three mechanisms, none of them a convention:

1. The reviewer is handed a `BlindedNotice`, a type that does not carry those
   fields at all. A rendering bug cannot leak what the object lacks.
2. `reveal` checks the store for a disposition and refuses without one.
3. The first disposition is immutable. Corrections append with `supersedes` and
   `post_reveal: 1`, and only pre-reveal dispositions count toward agreement.

`sample-rejects` returns a queue id and a size and nothing else — echoing the
strata would tell the reviewer what a single-stratum queue contains.
`--force-unblind` exists for debugging and **records that it was used**, marking
the queue compromised.

**Two limits, stated rather than overclaimed.** An expert who sees UNSPSC
77101501 and knows the profile families can reconstruct "coded, wrong family"
unaided; this prevents anchoring on the machine's answer, not expertise. And a
queue drawn only from rejects still tells the reviewer the verdict was REJECT —
`--include-admitted` closes that gap and is off by default.

## Review has two phases

| phase | question | blinded |
|---|---|---|
| 1 — disposition | "Is this relevant work for us?" ACCEPT / REJECT / UNCERTAIN | **yes** |
| 2 — categorization | failure category, evidence, explanation | no |

Naming *why* the filter erred is a claim about what it did, so forcing it while
blinded would produce guesses. Judgment must be uninformed; analysis should be
informed.

`UNCERTAIN` is a review state and only a review state. `predicates.py` imports
nothing from the review layer, so an `ACCEPT` label physically cannot reach
admission.

## The golden set

Entries carry a class (`clearly_relevant`, `clearly_irrelevant`,
`known_false_negative`, `known_false_positive`, `edge_case`), an `expected`
verdict, and their own frozen `as_of`.

**The strata were drawn before any review existed**, by a deterministic rule at
seed 20260820. If the set were assembled only from reviewed rejects it would be
the population the filter is known to fail, and recall measured on it would be
recall over a set built to fail.

Every entry freezes its raw row, including archive-sourced ones. Feed-sourced
entries have no other home — `.cache/tenders.csv` is overwritten on every
download — and archive-sourced ones need it too, because `notices.db` is
gitignored and 120MB and the set must evaluate on a fresh clone. `verify-golden`
compares frozen rows against the archive when present and **reports**
disagreement rather than resolving it.

An `edge_case` may carry `expected: null`; those are excluded from the matrix and
reported separately, because scoring an entry nobody agreed on manufactures a
number.

## Evaluation

Precision and recall, each with a Wilson interval and `n`. **No composite score,
no F1, no verdict line that picks a winner.** At this set's size a rate to three
decimals is theatre; the per-entry table and the regression list are the parts
that mean something.

The one mechanical judgement is negative: a candidate that breaks entries the
base got right is REGRESSED and `--gate` fails. Recovering a known false negative
never offsets that automatically. `v-admit-everything` exists to prove the point
— it recovers every false negative in the set, reaches recall 1.000, and is
rejected on seven regressions.

Two refusals: versions with different stage manifests are not compared, and
identical results are reported as "no entry changed", never as a measured
equivalence.

## Proposal is not promotion

```
PROPOSED  -> TESTING -> ACCEPTED | REJECTED  -> (separate human commit) -> shipped
```

A `PROPOSED` refinement names no variant, so there is no code and
`evaluate-refinement` refuses it. Moving to `TESTING` requires a human to write
the variant. **Accepting still changes nothing**: `variants.py` is not imported by
`ingest.py` or `predicates.py`, so an accepted-but-unpromoted variant physically
cannot reach the corpus. Shipping is a separate commit that edits the profile or
the predicates and registers a new filter version.

No command in this package writes to `vault/profiles/my-company.md`.

Evaluation results are **appended as dated sections**, never merged into the
proposal's own prose.

## Versions

A triple, not one hash: `profile_sha256` (what we look for),
`predicates_sha256` (how we decide — the identity key), and
`stage_manifest_sha256` (the funnel's shape — the comparability key). The middle
one is coarse on purpose, so a comment edit bumps the version; the third absorbs
the cost by staying still unless a stage is added, removed, reordered or
deactivated.

`filter-versions.yaml` is append-only. Audit records reference its labels, and
rewriting an entry would retroactively change what a past run claims to have been.

## What this cannot do

`.cache/tenders.csv` is overwritten on every download, and the CanadaBuys open
feed is a snapshot of what was open the day it was read. **The exact set of
notices ChromaDB admitted on any past day is gone and no replay recovers it.**
Notices that opened and closed between two archive cuts may not appear at all.

`--as-of publication` answers a different, answerable question: given the archive
row and today's predicates, would this notice have passed on the day it was
published? Two things it therefore cannot tell you — which notices were in the
feed at all on a given day, and what the predicates were on that day, since
before this package existed no filter version was recorded.

The archive is also not the production population: `notices.db` holds every
federal notice for the fiscal years ingested, including notices never present in
the open feed. Every replay says so, and `compare` refuses to cross an archive
run with a feed run.

Reconstructing daily feed membership is a future capability, not a reason to
block this one.
