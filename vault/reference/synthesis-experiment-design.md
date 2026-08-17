# Evidence-backed synthesis — proposed experiment

**Design only. Not implemented.** Follows [[case-pilot-postmortem]], which
stopped the predictive line and identified the untested link: nothing verifies
that a synthesised conclusion is faithful to the records underneath it.

## The question

> Given a frozen evidence bundle, does the system produce conclusions that are
> traceable, faithful, explicit about inference, explicit about contradictions,
> explicit about unknowns, and reproducible?

Note what this is not. It does not ask whether the conclusion is *correct* about
the future — that is the question that just failed, and it needs data that does
not exist. It asks whether the conclusion is *honest about its own evidence*,
which is checkable today, against records rather than against outcomes.

**Why this is worth measuring.** A dossier whose numbers cannot be trusted is
worse than no dossier, because it reads authoritative. The whole architecture
rests on Claude reading assembled evidence and reasoning in prose; the briefing
skill enforces style rules but nothing checks that a sentence about a contract
matches the contract.

**Why it can succeed where the last one could not.** Ground truth is a property
of the bundle, not of the world. No outcome window, no lead time, no base rate,
no waiting for CanadaBuys to accumulate years. Each bundle is its own answer key.

## The central construct: a claims table

For each frozen bundle, generate alongside it a set of **deterministic claims** —
statements computable exactly from the records, true or false by inspection, no
judgement involved.

```
claim_id
claim_type      value | date | count | identity | relation | absence
subject         the entity the claim is about
predicate       what is asserted
value           the exact figure, date or name
evidence_ids    the rows that establish it
derivation      the expression that computed it
```

Examples, all mechanically generated:

```
C-014  value     ECCC contract with THINKON, disclosed value       $35,932.58
C-021  date      that contract's period ends                       2027-01-31
C-033  count     notices in UNSPSC 43 for ECCC before T0           10
C-041  identity  audit a1b2 names ECCC by direct attribution       (method=direct)
C-052  relation  audit c3d4 names ECCC by INHERITED attribution    (method=inherited_citation)
C-060  absence   no plan rows for this org published before T0     (corpus absence)
```

The claims table is the answer key. It is generated from the same gate that built
the bundle, so it cannot disagree with it, and it is generated **before** any
model sees the bundle.

## Six measurable properties

Each is a check against the claims table, not a human rating.

### 1. Traceability

Every factual assertion in the output must carry an `evidence_id` or `claim_id`
that resolves. Measured as: proportion of assertions that cite; proportion of
citations that resolve to a real id; proportion that resolve to an id **actually
in this bundle** (a citation to a real row from a different case is the
interesting failure).

### 2. Faithfulness to deterministic facts

Where an assertion restates a claim, the value must match. Numbers, dates, vendor
names and counts are extracted from the output and compared to the claims table.
Failures are typed, because they are not equally serious:

- **contradiction** — states a different value than the claim (worst)
- **fabrication** — states a value with no claim behind it
- **distortion** — right value, wrong subject or wrong date attached
- **rounding** — `$35,932.58` rendered `$36k` (acceptable if the paper says so)

### 3. Explicit inference

Assertions not derivable from the claims table must be marked as inference. The
repository already carries the vocabulary — observed, derived, inferred — and
already applies it to audit attribution. Measured as: proportion of
non-derivable assertions that are marked; and the converse error, derivable
facts hedged as inference, which erodes trust in the parts that are solid.

### 4. Contradiction surfacing

Some bundles are **seeded with real contradictions already present in the
sources** — not fabricated ones. The corpus supplies them:

- `closing_date_conflict`, where a notice's structured field and its prose
  disagree (the repo already detects this)
- a contract whose `period_end` precedes its `period_start`, or the 630 rows
  with a blank end date and the ones ending in 2223
- an award notice and a proactive-disclosure row for the same procurement with
  different values
- a department named by both `end_user` and `contracting_entity` with different
  organizations

Measured as: recall over seeded contradictions. Silently picking one side is the
failure mode; the output must say the sources disagree.

### 5. Unknown surfacing

Bundles where a field is genuinely absent. **The distinction under test is the
one the pilot already had to enforce:** "no plan rows were published by T0" is a
statement about the corpus; "the department has no plans" is a statement about
the department, and does not follow. Measured as: proportion of absences reported
as absences, and count of absences converted into negative claims.

Adversarial probes belong here — an absent field that a plausible-sounding
sentence would fill.

### 6. Reproducibility

The same frozen bundle, run *k* times (k=5 proposed), by:

- **claim-set stability** — Jaccard overlap of cited claim ids across runs
- **assertion stability** — do the extracted values agree run to run
- **conclusion stability** — does the headline judgement hold

Prose will vary and should. Cited facts should not.

## Why most of this can be scored mechanically

The claims table makes 1, 2, 5 and 6 automatable: extract citations and figures
from the output, join to the table, count mismatches by type. Properties 3 and 4
need a rubric and a second reader, and the second reader should be a human on a
subset — this experiment is *about* whether model output can be trusted, so
scoring it entirely by model would be assuming the conclusion.

## Design

**Unit.** One frozen bundle, one synthesis task, one output.

**Bundles.** Reuse the existing machinery unchanged — the availability gate,
evidence records with provenance and epistemic class, content-hashed snapshots.
Add the claims-table generator. Bundles need not be historical: the gate can
target today. The T0 apparatus stays because it makes bundles *frozen*, which is
what reproducibility requires, not because anything is being predicted.

**Task.** The real one. `cmd_department_dossier` assembles four sources and
refuses to score; the briefing skill turns assembled evidence into prose. The
experiment evaluates the actual synthesis these produce, not a proxy.

**Sampling.** Stratify bundles by how hard they are to be honest about:

| stratum | what it probes |
|---|---|
| rich, consistent | baseline — can it stay faithful with plenty to say |
| sparse | does it hedge, or invent |
| contradictory | does it surface the disagreement |
| absence-heavy | does it distinguish corpus silence from departmental silence |
| inference-heavy | mostly `inherited_*` attribution — does it mark the uncertainty |

Roughly 6 per stratum for a pilot; 30 bundles, ~150 outputs at k=5.

**Blinding.** Less critical than before — there is no label to leak. But the
claims table must be generated before generation and sealed, so a bundle cannot
be adjusted to match what the model happened to say.

## What would count as success or failure

Thresholds frozen before the run. As first proposals, to be argued before
implementation:

- **contradiction rate 0.** Any assertion contradicting a deterministic claim is
  disqualifying, not a percentage to optimise. One wrong contract value in a
  dossier is the whole problem.
- **fabrication rate < 2%** of factual assertions.
- **traceability > 90%** of factual assertions carrying a resolving id.
- **absence-to-negative conversions: 0.**
- **contradiction recall > 80%** over seeded contradictions.
- **claim-set Jaccard > 0.8** across five runs.

A failure here is more actionable than the predictive null, because every failure
names a specific sentence, a specific claim, and a specific record.

## Explicitly out of scope

- **No prediction.** No outcome windows, no lead times, no forecasting.
- **No scoring or ranking.** The six properties are reported separately and never
  combined into a quality score. They are incommensurable, and a weighted total
  would be the deleted formula wearing a lab coat.
- **No modification of the evidence model to improve results.** The point is to
  measure the system as built.
- **The expiry hypothesis stays rejected** and does not re-enter as a claim type.

## Open questions before implementation

1. **Claim granularity.** One claim per contract row is thousands per bundle;
   one per aggregate loses the specificity that makes faithfulness checkable.
   Where to draw it needs deciding before generating anything.
2. **Assertion extraction.** Comparing output to claims requires parsing
   assertions out of prose. Structured-output-per-assertion is reliable but
   changes the task; free prose plus an extractor is the real task but adds an
   error source that must itself be validated.
3. **Rounding policy.** Is `$36k` for `$35,932.58` faithful? Probably yes with a
   stated tolerance — needs freezing.
4. **Who reads the subset.** Properties 3 and 4 need a human on some fraction,
   and that fraction should be decided in advance.
