# Analyst-blind evidence evaluation — protocol

> **STOPPED 2026-08-16. This protocol is a record, not a live method.** The pilot
> it describes ran once and could not answer its own question; see
> [[case-pilot-2026-08-16]] for the result and [[case-pilot-postmortem]] for what
> it did and did not establish. The predictive line is not being scaled and the
> evidence model is not being modified to rescue it. The successor work is
> [[synthesis-experiment-design]], which asks a different question.
>
> Kept unedited because the gating rules below are correct and reusable, and
> because a protocol rewritten after its results is no longer a protocol.

Frozen 2026-08-16, before any prediction was recorded. What follows is the
method. Results live in a separate dated file so the protocol cannot be edited
into agreement with them afterwards.

## The question

> Given only information publicly available at time T0, does adding public
> evidence improve the ability to identify a department × capability that will
> subsequently have a relevant procurement?

Not *does the graph predict procurement*. The department-year backtest
([[backtest-2026-08-15]]) already answered a version of that with a null result,
and said why: the target was "does this department tender anything relevant this
year", at a 68.6% base rate and 94% in the largest third of departments. There
was almost no room to be right in a way that counts.

This experiment changes the target rather than tuning the old one. The unit is
finer (`department × capability × date`), the output is a **decision** rather
than a score, and the comparison is between evidence conditions on the same
cases.

## Evidence conditions

| | Contents |
|---|---|
| **Ø** | department, capability and date only — no supplied evidence |
| **A** | tender and award notices |
| **B** | A + proactive contract disclosure |
| **C** | B + Departmental Plans and Results Reports |
| **D** | C + Auditor General records |

Strictly nested and asserted so: `A ⊆ B ⊆ C ⊆ D` by evidence id, checked per
case rather than assumed.

**Ø is not decoration.** The evaluator is a language model whose pretraining
post-dates every evaluation date here. No prompt can enforce a knowledge cutoff,
and claiming one would be false. Ø measures what the evaluator scores with
nothing supplied, which is the ceiling of what prior knowledge and base rates
buy it. Every other condition is read against it, and **the model-evaluated run
is labelled exploratory for this reason.** A human analyst-blind run on the same
bundles would be the stronger evidence, and the artifacts are written so one can
use them unchanged.

## Cases

10 positive and 10 negative, drawn from a frame of 14,268 cells (82 departments
× 6 capabilities × 29 monthly dates).

- **T0 range** 2023-04-01 → 2025-08-01. The corpus opens 2022-04-01, so this is
  the widest range giving every case 12 months of history behind it and a
  12-month outcome window ahead of it inside the corpus.
- **Outcome window** 12 months, half-open: `(T0, T0+12mo]`. A notice published
  exactly on T0 is evidence, never an outcome.
- **Eligibility** at least one condition-A item available at T0. Computed from
  pre-T0 data only; it cannot see the label. A cell with nothing to read makes
  condition A vacuous and the A-vs-B comparison meaningless.
- **Label** POSITIVE if any notice matching the capability, attributed to the
  department, is published in the window. Frozen before construction and not
  revisable on results. `opportunity_kind` and `notice_type` are preserved per
  case for later analysis but take no part in the label.

Base rate across eligible cells is **74.4%**, which is why the pilot is
balanced by matched sampling rather than drawn at random.

### Capability selection — deterministic and outcome-blind

Six UNSPSC segments, chosen by rule from notices published **before 2023-04-01**
— the first T0, so no case's outcome window can influence which capabilities
exist. Frozen in `vault/reference/capabilities.yaml`, hashed, and the hash
recorded on every case.

Floors: ≥50 notices, ≥10 departments, ≥100 disclosed contract rows; then the top
six by department count.

| segment | capability | notices | depts | contracts |
|---|---|---|---|---|
| 25 | Commercial, Military and Private Vehicles | 185 | 18 | 1,104 |
| 43 | Information Technology, Broadcasting and Telecommunications | 58 | 20 | 1,159 |
| 56 | Furniture and Furnishings | 159 | 23 | 410 |
| 72 | Building and Facility Construction and Maintenance | 354 | 18 | 2,267 |
| 80 | Management and Business Professionals and Administrative Services | 96 | 23 | 2,655 |
| 81 | Engineering and Research and Technology Based Services | 158 | 19 | 5,232 |

The floors moved once, from a first attempt at 4-digit family grain that yielded
only 5 candidates across 3 segments. That change was made against **coverage
counts only** — no label had been computed and the frame did not exist. Recorded
because it moved, not because moving it was wrong.

### Negative controls

Every positive is paired with one negative at the **same T0**, under one of two
tiers, five pairs each:

- **department_exact** — same department, different capability. The strongest
  control available: department size, budget, procurement culture and calendar
  are held literally constant, so "large departments procure more" carries no
  information. **But plans and audits attach to a department, not to a
  department × capability, so C and D are identical within such a pair and
  cannot discriminate there by construction.**
- **activity_decile** — a different department in the same prior-activity decile
  (all notices in the 12 months before T0). Weaker control, but C and D differ.

Running both is deliberate: one tier shows what the strong control permits, the
other shows what the weak one reveals.

Diversity caps of 3 per department, 3 per capability and 4 per quarter stop the
draw collapsing onto one department or one period.

### Residual biases, documented rather than solved

- **Capability is not matched.** Pairs differ in capability by construction, so
  a capability that almost always procures is a learnable shortcut.
- **department_exact favours multi-capability departments**, which skew larger.
- **Eligibility excludes silent cells**, so nothing generalises to cold-start
  departments.
- **History depth varies** from 12 to 40 months across the T0 range.
- **Four departments are held out** — ircc, prairiescan, wage, nsira — whose
  crosswalk relation is absorbed, successor or predecessor. "Was this department
  procuring at T0" has no single answer for them.
- **April–July 2022 is backfilled.** CanadaBuys launched 2022-08-08; those rows
  assert a publication date rather than having been observed at one. Affects
  early history only, never a label.

## Temporal gating

| Source | Availability rule |
|---|---|
| notices, awards | `publication_date <= T0` — the government's own stamp |
| contracts | `quarter_end(max(reporting_period, reference)) + 30d <= T0` |
| plans, DP fields | `{Y}-03-01` — plan tabled ~March of Y |
| plans, DRR fields | `{Y+1}-12-01` — results report tabled ~Nov of Y+1 |
| audits | `date_published <= T0` — portal date, runs late, which is the safe direction |

**Contract disclosure dates.** `contract_date` is when a contract was signed,
not when it became public; proactive disclosure is quarterly with a 30-day
deadline, and the gap is 70 days at the median and 1,020 at the 95th percentile.
Two independent statements of the quarter exist — `reporting_period` (blank on
20.9%) and the quarter encoded in `reference_number` (98.6%). Union coverage is
**99.3%**, and where both are present they agree **99.3%** of the time. Rows
with neither are excluded, not dated by inference; 6,972 of 969,366 fell out.
Where the two disagree the **later** quarter wins: too late withholds evidence
and costs recall, too early invalidates the run.

**Plan halves.** One `programs` row carries a Departmental Plan and a
Departmental Results Report tabled about twenty months apart. The counts prove
it — `intent_score` is non-null exactly where `planning_explanation` is,
`pressure_score` exactly where `variance_explanation` is, in all six years. The
halves are gated separately, so a row can arrive half-visible.

### Suppressed entirely

- **`status`** — Expired or Cancelled on all 30,527 notices, Open on none,
  because it is state at ingest. At a 2023 T0 it is pure 2026 information.
- **`first_seen`** — the 2026-08-15 backfill stamp on every row. It records when
  this project ran, not when the public could see anything.
- **`intent_score`, `pressure_score`, `it_score`** — derived from a 2026 model
  against 2026-authored example sentences, and scores besides.
- **`observed_names`** — attested from the full-history corpus.

Asserted absent from the written bundles rather than merely omitted from the
queries.

### Two leaks found in the existing backtest gate

`scripts/backtest.py`'s `Evidence` has the right shape — per-accessor SQL bounds,
no connection handed out — and two of its five accessors leak:

1. `Evidence.contracts()` bounds on `contract_date`, showing contracts about 100
   days before disclosure at the median.
2. `Evidence.plans()` admits `year <= fiscal_year_of(T)` and selects
   `pressure_score`, a results-report field, 8–20 months before publication.

Both sit in the untracked experiment layer, not in production. The direction is
reassuring rather than alarming: a leaking gate inflates lift, and that run still
returned a null.

## Epistemic classes

- **Observed** — in the source data: notice → UNSPSC, contract → vendor, audit →
  department where the record names it directly (191 of 439 edges).
- **Derived** — deterministic transform: disclosure quarter → availability date,
  `period_end` → expiry window.
- **Inferred** — requires interpretation: audit attribution inherited by a
  committee briefing from its parent report (248 of 439 edges), carried with
  method, confidence and the matched phrase.

**No inferred relation was invented to increase coverage.** Plans and audits are
presented at department level and explicitly *not* attributed to a capability,
because running a text classifier to link a program to a capability would
manufacture the very coverage the experiment exists to measure.

**There is no GSIN bridge, and that is a finding.** Notices are classified in
UNSPSC; contract `commodity_code` is 73.5% GSIN-form and 8.4% UNSPSC. Two bridges
were considered and both refused. PSPC's own crosswalk is documented in
`unspsc_discover.py` as carrying high-level linkages through indiscriminately —
telecom cable laying and highway paving share a GSIN. Co-occurrence inside a
single notice was measured and **zero of 7,152 pre-cutoff notices carry both code
systems**. So contracts are capability-matched only through the UNSPSC minority,
and the GSIN majority is shown at department level flagged as unmatched.

## Blinding

- Ground truth lives in `truth.json`; the evaluation harness never opens it.
  A field the harness is asked not to read is not hidden; a file it never opens
  is.
- Case ids are assigned **after** shuffling, so no ordering or parity tracks the
  label.
- The evaluator runs as a fresh process per bundle, with the default system
  prompt **replaced** (not appended to), MCP servers cleared, every tool
  disallowed, and its working directory an empty temp folder. It has no
  mechanism to reach the databases.
- Predictions are appended to a JSONL file and hashed before `truth.json` is
  opened. `case_metrics.py` refuses to score an unsealed or modified file.

## Metrics

Precision, recall, FPR, FNR per condition, each with a Wilson interval.
Incremental effect is a **paired** McNemar count of who changed their mind, not
a difference of two independent rates, because both conditions judged the same
cases. Calibration is reported as raw (confidence, outcome) pairs — twenty cases
cannot support a curve. A label-shuffle control re-scores against permuted
labels; anything much above chance there means bundle shape carries the label.

**n=20 gives roughly ±22 points on any single proportion.** The pilot is a
methodology check, not a finding.

## Standing constraints

**No composite scoring.** No opportunity score, no ranking, no fitted weight, no
predictive graph edge. The evaluator emits a decision and a stated confidence;
confidence is an output under test, never an input. Nothing here is imported by
`tender_tools` or `mcp_server.py`, and no MCP tool exposes it.

**Contract expiry remains a rejected hypothesis.** Incumbent expiry → same-
capability recompete tested at 1.00–1.05× against a department × capability
permutation null across four windows, p 0.07–0.50. Contract end dates appear in
bundles as descriptive fact with their dates attached. They are not a signal, and
nothing may quietly promote them back into one.

**Missing evidence is not negative evidence.** An absent plan or audit renders
"NONE — no rows were published on or before T0", with an explicit note that this
records an absence in the corpus and is not a statement about the department.
Absence from CanadaBuys is never evidence a procurement did not happen.

## Reproducing

```
python scripts/contracts_ingest.py --no-term-filter --db data/contracts_full.db \
       --window-years 12 --no-intel
python scripts/casebook.py build-db
python scripts/casebook.py freeze          # writes capabilities.yaml + hash
python scripts/casebook.py pilot           # frame, sample, bundles, leak audit
python tests/test_casebook.py
python scripts/evaluate_cases.py
python scripts/evaluate_cases.py --seal    # hash predictions BEFORE revealing
python scripts/case_metrics.py
```

Seed 20260816, frame version 1. `capabilities.yaml` is committed; the cases,
bundles, truth and predictions are not, because committing them would put the
answer key in the repository.
