# Analyst-blind evidence pilot — result, 2026-08-16

Method frozen in [[case-protocol]] before any prediction was recorded.
20 cases (10 positive, 10 negative), 5 evidence conditions, 100 blinded
evaluations. Predictions sealed at sha256 `247bfcb2e18255e9…` before
`truth.json` was opened.

**This is a methodology pilot, not a finding about procurement.** n=20 gives
roughly ±22 points on any proportion. What it can establish is whether the
machinery works and whether the question is answerable in this shape.

## Headline

**The pilot cannot answer whether A/B/C/D differ, and the numbers below must not
be read as evidence that they are equivalent.**

Two structural defects make the comparison undefined, not merely underpowered:
conditions C and D supply byte-identical evidence to both cases in every
department-matched pair, and the evaluator answers INVESTIGATE 16–18 times out
of 20 regardless of what it is shown. An experiment in which two arms cannot
differ, judged by a rater that does not vary, produces 50% by arithmetic. That
is a property of the setup, not a measurement of the sources.

The table is reported for completeness and as a record of what the machinery
produced. **It is not a result about evidence value.** The retraction is the
finding; see the two sections after it for why.

| condition | evidence | accuracy | precision | recall |
|---|---|---|---|---|
| Ø | none supplied | 50% (30–70) | n/a | 0% (0–28) |
| A | procurement | 50% (30–70) | 50% (28–72) | 80% (49–94) |
| B | + contracts | 50% (30–70) | 50% (29–71) | 90% (60–98) |
| C | + plans | 45% (26–66) | 47% (26–69) | 80% (49–94) |
| D | + audits | 45% (26–66) | 47% (26–69) | 80% (49–94) |

Paired McNemar on the same cases: Ø→A fixed 8 / broke 8, A→B fixed 2 / broke 2,
B→C fixed 2 / broke 3, C→D fixed 1 / broke 1. Every p = 1.000, and against a
permuted-label control observed accuracy sits on the shuffled median in every
condition.

**Read those p-values as uninformative, not as null results.** A p of 1.000
between two arms that received identical inputs is what identical inputs
produce. Reporting it as "contracts did not help" would be inventing a
measurement out of a design defect.

## Why: the evaluator says INVESTIGATE almost regardless

| condition | Ø | A | B | C | D |
|---|---|---|---|---|---|
| INVESTIGATE | **0/20** | 16/20 | 18/20 | 17/20 | 17/20 |

On a balanced set, a near-constant answer scores 50% by construction. The
decision carries almost no information, so no amount of added evidence can move
the metric — the ceiling was set by the response distribution, not by the
sources.

The three cases declined under D were two positives and one negative. It is not
declining selectively; it is declining rarely.

Stated confidence tells the same story. On cases that did procure, D produced
[25, 30, 35, 45, 45, 55, 58, 62, 65, 68]; on cases that did not,
[25, 35, 35, 45, 45, 45, 50, 58, 70, 80]. The distributions are
indistinguishable. **Confidence is not meaningful merely because a number was
emitted**, and here it is not.

## What the pilot establishes that is worth keeping

**1. The Ø arm found no detectable pretraining contamination.** With department,
capability and date but no evidence, the evaluator declined all 20 at confidence
5. It does not appear to be recalling which departments ran competitions. This
was the single largest threat to the design and it did not materialise — though
absence of evidence at n=20 is weak, and a human run would settle it properly.

**2. The matched controls did their job.** Prior 12-month activity by label:

```
POSITIVE  [2, 4, 10, 14, 26, 48, 63, 224, 228, 346]
NEGATIVE  [2, 3, 12, 14, 16, 48, 63, 228, 229, 238]
```

Near-identical. "Large departments procure more" is genuinely unavailable as a
shortcut, which is what the pairing was for.

**3. Temporal reconstruction holds.** 100 bundles pass a leak audit checking
every availability stamp against T0 and asserting the suppressed columns absent
from the rendered text. 243 forward-looking dates — closing dates, contract
period ends, next-year planned spending — are correctly kept as published
forecasts rather than treated as leaks. All 100 predictions carry a bundle
digest matching the file on disk.

**4. Provenance survives end to end.** Every evidence item retains source, row
key, source URL, publication date, the availability rule that admitted it, and
its epistemic class. `A ⊆ B ⊆ C ⊆ D` is asserted by evidence id per case.

## The structural finding, which matters more than the null

**Conditions B, C and D supply byte-identical evidence to both cases in every
`department_exact` pair. Only A differs.**

| tier | pairs | A differs | B adds differ | C | D |
|---|---|---|---|---|---|
| department_exact | 5 | 5/5 | **0/5** | **0/5** | **0/5** |
| activity_decile | 5 | 5/5 | 5/5 | 5/5 | 5/5 |

This is not weak signal. It is the absence of an input. Two reasons:

- **Plans and audits attach to a department, not to a department × capability.**
  Holding the department constant holds them constant. No classifier was run to
  attribute a program or an audit to a capability, deliberately — inventing that
  link to fill the section would manufacture exactly the coverage the experiment
  exists to measure.
- **Contracts cannot be capability-matched in practice.** Only 1 of 20 cases has
  a single capability-matched contract row. Contract `commodity_code` is 73.5%
  GSIN-form against 8.4% UNSPSC, and **zero of 7,152 pre-cutoff notices carry
  both code systems**, so there is no evidence from which to derive a bridge.
  PSPC's own crosswalk is rejected on the grounds `unspsc_discover.py` already
  records. The other 19 cases receive department-level contract context only.

So the strong-control tier can only ever measure condition A. The weaker tier
lets B/C/D vary, but there they vary *by department*, confounded with department
identity. **At `department × capability` grain, these three sources carry no
capability-specific information in this corpus.** That is a fact about the data,
not about the evaluator, and it would hold for a human analyst reading the same
bundles.

## A defect in the label worth fixing before scaling

Lead times for the 10 positives: `[4, 9, 51, 64, 68, 115, 116, 181, 303, 304]` —
median 115 days, and **5 of 10 under 90 days**. A "future procurement" four days
after T0 is not early identification; it is a notice that was nearly public
already. The 12-month window with no blackout admits imminent procurements as
readily as genuinely forward ones.

The prior backtest used a 6-month blackout for this reason. Dropping it here was
a simplification made to keep the negative-control definition clean, and the
lead-time distribution shows it cost more than it saved.

## Pilot success criteria

| # | criterion | verdict |
|---|---|---|
| 1 | T0 snapshots reconstructable | **pass** |
| 2 | Future information cannot enter bundles | **pass** — audit clean, 17 tests |
| 3 | Cases constructed reproducibly | **pass** — seed + frame version + matcher hash |
| 4 | A/B/C/D/Ø genuinely distinct | **FAIL for department_exact**; pass for activity_decile |
| 5 | Multi-department notices handled | **pass** — one event_id across cells |
| 6 | Missing evidence represented honestly | **pass** — NONE with explicit non-claim |
| 7 | Ground truth hidden until after prediction | **pass** — sealed digest, verified |
| 8 | Reproducible from saved definitions | **pass** |

Criterion 4 fails, so **do not scale to 50–100 cases as designed.** The
methodology is sound and the plumbing is trustworthy; the experiment as
specified cannot answer the question for conditions C and D, because those
sources do not resolve to the unit of analysis.

## What would have to change first

> **Superseded.** These were written before the decision to stop. The predictive
> line is not being pursued and the evidence model is not being changed to make
> B/C/D discriminate; see [[case-pilot-postmortem]] for what the pilot did and
> did not establish, and [[synthesis-experiment-design]] for the successor. The
> list is kept because items 2 and 3 are preconditions for *any* future
> predictive experiment.

1. **Move the unit of analysis, or move the sources.** Either evaluate at
   department × *time* where plans and audits actually live, or find
   capability-resolvable evidence. The second is the more useful and the harder.
2. **Add a blackout** between T0 and the outcome window — the prior 6-month
   choice is the obvious starting point — so the label means "future" rather
   than "imminent".
3. **Break the response bias.** A binary INVESTIGATE decision from a model that
   answers INVESTIGATE 85% of the time is close to a constant. Forcing a ranking
   within matched pairs would extract more from the same judgement without
   introducing a score — the evaluator says *which of these two* rather than
   *yes or no*, which is a comparison rather than a rating.
4. **Run the human arm.** The bundles and protocol are built for it unchanged.
   With the Ø arm showing no contamination, the model proxy is defensible for
   plumbing, but the product thesis is about an analyst's decision.

## What this does not establish

That the evidence is worthless. It establishes that **at this grain, with this
label, and with a binary decision from this evaluator, the added sources cannot
be shown to help — and for two of them, cannot in principle differ within the
strongest control.** The dossier's real use has always been *what* is coming and
*why now*, at the level of a specific system with a named incumbent. Nothing
here speaks to that, and the same caveat the department-year backtest recorded
applies unchanged.

**The rejected expiry hypothesis stays rejected.** Contract end dates appeared in
these bundles as descriptive fact with their dates attached and nothing here
promotes them back to a signal.

## Reproducing

```
python scripts/casebook.py build-db
python scripts/casebook.py freeze
python scripts/casebook.py pilot
python tests/test_casebook.py
python scripts/evaluate_cases.py
python scripts/evaluate_cases.py --seal
python scripts/case_metrics.py
```

Seed 20260816, frame version 1, matcher hash `8a5fc1bb90b634d8`,
evaluator `claude-sonnet-5` via isolated CLI subprocesses, mean 25.1s each.
