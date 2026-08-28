# Predictive pilot — postmortem

Written 2026-08-16, after [[case-pilot-2026-08-16]]. The predictive line of work
is **stopped**: not scaled, and the evidence model is not being modified to make
B/C/D discriminate. Modifying it to rescue the experiment would be fitting the
data model to the result we wanted.

## 1. Hypotheses the pilot successfully tested

These were genuinely put at risk and survived.

**H1 — Historical snapshots can be reconstructed from this corpus.** Yes, for
four of five sources, on dates the publisher itself supplies. 100 bundles pass an
audit that checks every availability stamp against T0 and asserts the
current-state columns absent from rendered text. 243 forward-looking dates
(closing dates, contract period ends, next-year planned spending) are correctly
retained as published forecasts rather than mistaken for leaks.

**H2 — Contract disclosure dates are recoverable.** Yes, at 99.3%. `contract_date`
is the signing date and leaks by 70 days at the median, 1,020 at the 95th
percentile. Two independent statements of the disclosure quarter —
`reporting_period` (blank on 20.9%) and the quarter in `reference_number`
(98.6%) — agree 99.3% of the time where both exist, and their union covers
99.3% of 1,313,348 rows. 6,972 rows with neither were excluded, not estimated.

**H3 — Departmental Plan and Results Report fields need separate gates.**
Confirmed as fact, not conjecture: `intent_score` is non-null exactly where
`planning_explanation` is, `pressure_score` exactly where
`variance_explanation` is, in all six years. The two halves are ~20 months apart.
`backtest.py`'s `Evidence.plans()` admits both on the plan's date.

**H4 — Blinding survives the pipeline.** Ground truth stayed in a file the
harness never opened; case ids were assigned after shuffling and carry no
ordering or parity signal; predictions were hashed before `truth.json` was
opened; all 100 predictions carry bundle digests matching disk.

**H5 — Matched controls neutralise the department-size shortcut.** Prior
12-month activity by label came out near-identical
(`POSITIVE [2,4,10,14,26,48,63,224,228,346]` vs
`NEGATIVE [2,3,12,14,16,48,63,228,229,238]`). The trivial shortcut was
genuinely unavailable.

**H6 — Pretraining contamination is not obviously driving answers.** With
department, capability and date but no evidence, the evaluator declined all 20
at confidence 5. It does not appear to be recalling which departments ran
competitions. Weak at n=20, but it was the largest threat to the design and it
did not materialise.

## 2. Hypotheses the pilot could not test

**The entire research question.** Whether adding contracts, plans and audits
improves identification of a department × capability was never put at risk,
because for the strongest control tier the added conditions were not different
inputs. Nothing about evidence value was measured in either direction.

Also untested, for the same reason: whether lead time improves with more
evidence; whether confidence becomes better calibrated with more evidence;
whether any source has incremental value over `prior_year_hit`.

## 3. Evaluator failures versus data-model failures

Keeping these apart matters, because one is fixable by changing how we ask and
the other is not fixable without different data.

### Evaluator failures — the instrument, not the evidence

- **Response bias.** INVESTIGATE on 16–18 of 20 once given any evidence, against
  0 of 20 with none. The decision is close to a constant, so it carries almost
  no information and caps any metric at the base rate.
- **Uncalibrated confidence.** Under D, positives drew
  `[25,30,35,45,45,55,58,62,65,68]` and negatives `[25,35,35,45,45,45,50,58,70,80]`.
  Indistinguishable. A number was emitted; nothing was measured.
- **Plausible cause, not excuse.** In the real frame 74.4% of eligible cells are
  positive, so "investigate" is usually right in the wild. The balanced pilot
  removes that advantage and exposes the bias. A binary yes/no is also the wrong
  question shape for a rater with a strong prior.

### Data-model failures — not fixable by prompting

- **Plans and audits do not resolve to a capability.** They attach to a
  department. Holding the department constant holds them constant, which is why
  C and D are byte-identical within all five department-matched pairs. Attaching
  them to a capability would require a classifier we deliberately did not build,
  because inventing that link manufactures the coverage the experiment existed to
  measure.
- **Contracts do not resolve to a capability either.** 1 of 20 cases has a single
  capability-matched contract row. `commodity_code` is 73.5% GSIN-form against
  8.4% UNSPSC.
- **The code-system bridge is genuinely absent.** Zero of 7,152 pre-cutoff
  notices carry both a UNSPSC and a GSIN code. There is no evidence from which to
  derive a mapping, and PSPC's own crosswalk is rejected on the grounds
  `unspsc_discover.py` already records — it carried high-level linkages through
  indiscriminately, so telecom cable laying and highway paving share a GSIN.
  **No crosswalk will be imported to rescue this experiment.**

### A label failure, belonging to neither

Lead times `[4, 9, 51, 64, 68, 115, 116, 181, 303, 304]` — median 115 days, five
of ten under 90. "A relevant procurement eventually occurred" is not early
identification; a notice four days out was nearly public already. Dropping the
prior work's 6-month blackout to keep the negative-control definition clean cost
more than it saved.

## 4. What a valid predictive experiment would require

All four, not any one:

1. **Capability-resolvable evidence on the non-procurement sources.** Something
   that attaches a plan or an audit to a *kind of work*, sourced rather than
   inferred by us. Until that exists, C and D cannot be evaluated at this grain
   under any design.
2. **A frozen minimum lead time,** set before cases are drawn and justified by
   what the work actually needs — how long before a solicitation an analyst must
   know to act. Outcomes inside that threshold count as neither positive nor
   negative; they are excluded, not relabelled.
3. **An instrument that varies.** Either a rater whose responses spread across
   the range, or a question shape that forces discrimination — a forced choice
   within a matched pair ("which of these two") rather than an absolute
   judgement. A comparison is not a rating and stays inside the no-scoring rule.
4. **More corpus than exists.** CanadaBuys opens 2022-04-01. A 12-month window
   with a 6-month blackout and a year of prior history leaves under two years of
   usable evaluation dates. This constraint resolves with time and not otherwise.

Absent 1 and 2, a larger sample would produce tighter intervals around a
quantity that still is not the thing we care about.

## 5. Reframing the system

The predictive framing asked the graph to forecast. The system was never built
for that, and the two experiments that tested it both said so. What it is
actually good at is assembling scattered public records into something a person
can audit. That pipeline already exists in the repository; it has simply never
been named or tested as a pipeline.

```
public data → evidence graph → deterministic claims → model-assisted synthesis → auditable conclusion
```

| stage | what it is | what already implements it |
|---|---|---|
| **public data** | raw federal sources, retained as downloaded | `.cache/` (899MB), five ingest scripts, `notices.db` `sources` recording every fetch |
| **evidence graph** | records joined on identity that is *attested*, with provenance and epistemic class on every edge | `org_aliases.yaml` (93 keys), `crosswalk.db`, `audit_departments` (`direct` vs `inherited_*`), `casebook.py`'s availability gate and evidence records |
| **deterministic claims** | statements computable exactly from records, true or false by inspection, no judgement | `cmd_contracts_intel`, `expiring_contracts`, `program_signals`, `oag_signals` |
| **model-assisted synthesis** | Claude reading the assembled evidence and reasoning in prose | the `tender-briefing` skill, `cmd_department_dossier` (assembles, refuses to score) |
| **auditable conclusion** | a vault file a person can check back to source | `vault/briefings/`, `vault/intel/agencies/`, wikilinks, provenance stamps |

Two things the pilot built slot straight in and are worth keeping regardless of
what happens to prediction:

- **The availability gate** is the evidence graph's missing time dimension. It
  makes "what did we know on date D" answerable, which matters for auditing a
  past decision even when nothing is being forecast.
- **The bundle-plus-manifest format** — evidence records carrying source, row
  key, URL, publication date, admitting rule and epistemic class, with a
  content-hashed frozen snapshot — is the unit a synthesis experiment needs.

**The gap is the fourth arrow.** Nothing anywhere checks that a synthesised
conclusion is faithful to the deterministic claims underneath it. The briefing
skill enforces *style* rules (no composite score, colour by instrument state) but
nothing verifies that a sentence about a contract matches the contract. That is
both the untested link and the one the product's value actually rests on: a
dossier whose numbers cannot be trusted is worse than no dossier, because it
reads authoritative.

That gap is what the next experiment should measure. Design in
[[synthesis-experiment-design]].

## What stays rejected

**Incumbent expiry → same-capability recompete.** 1.00–1.05× against a
department × capability permutation null across four windows, p 0.07–0.50.
Contract end dates remain descriptive fact with their dates attached.

**Composite scoring, ranking, opportunity scores, predictive graph edges.**
Unchanged and not reopened by anything here.
