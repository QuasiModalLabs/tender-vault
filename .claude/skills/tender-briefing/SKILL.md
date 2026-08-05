---
name: tender-briefing
description: Produce the weekly tender briefing as a vault file. Use when asked to review the corpus, walk through what's open, do a weekly read, or say which tenders are worth time. Not for single-tender questions or dossier queries.
---

# Tender briefing

Write to `vault/briefings/YYYY-MM-DD.md`, dated today. Never print the
briefing to the terminal — it gets read in Obsidian, and wide tables
truncate in a terminal.

Read `vault/profiles/my-company.md` and `vault/reference/vehicles.md`
first. The profile decides fit; the vehicles file decides eligibility.

## Sections, in this order

**1. Act now**
Anything closing within 14 days, and any `closing_date_conflict`.
A body deadline earlier than the field is the costliest error in the
corpus — lead with it and say which date the notice itself states.
Absence of a conflict key means none was detected, not that the date
was verified. Say so if it matters.

**2. Worth bidding**
The short list. For each: what the work actually is, why it fits, and
the disqualifying facts stated plainly rather than implied — clearance
requirements, set-asides, incumbency, Canadian content weighting.
If a tender is a stretch for the profile, say which capability it
stretches.

**3. Vehicles**
Cross-reference `vault/reference/vehicles.md`. Report:
- Qualification notices open now, and whether the vehicle is worth holding
- How many notices this week were gated behind a vehicle not held
That second number is the durable one. Record it in the briefing even
when it's zero.

**4. Skip**
Grouped by failure class, not listed flat. The classes that recur:
product or SaaS purchase, call-up against a vehicle not held, staff
augmentation, results notice with nothing to bid, out of domain,
non-federal. One line each; the grouping is what makes it scannable.

**5. Pre-RFP signals**
Closed RFIs and summary reports. Nothing to submit, but they name
requirements that haven't been competed yet. Say which department and
which program.

## Rules

**Read the descriptions.** Six product buys per week file as `*SRV`
and type as Request for Proposal. Nothing structural separates a SaaS
purchase from a services engagement in this feed — the classifier
can't and shouldn't try. Catching them is the job of reading.

**State the basis when it's weak.** `kind_basis:
procurement_category_residual` means solicitation is what's left over,
not a positive finding. `prose_vehicle_name` means the vehicle was
inferred from a title, not a cited arrangement number — flag those for
confirmation against the notice.

**`unrecognised` is not `non_federal`.** Federal Crown corporations —
CDIC, BDC, Canada Post — have no entry in a registry of departments.
A registry miss is not evidence of anything. Never treat unrecognised
jurisdiction as a reason to skip.

**Never rank by a composite score.** No weighted totals, no
convergence numbers, no ordering by a computed fit value. Present the
facts and the reasoning; the reader decides. This is the formula that
was deliberately deleted from this project and a briefing template is
exactly where it would return.

**Don't rank by value either.** `estimated_value` is unreliable —
present in a small minority of notices and often reading a ceiling or
a bond amount rather than a contract value. Say "not stated" rather
than implying zero.

**Link tenders as `[[tender-id]]`** so promoted files connect in the
graph.

**Say what you didn't cover.** If the corpus has uncoded source
systems, notices you couldn't classify, or a section with nothing in
it, state that rather than leaving a silent gap. An empty section
should say why it's empty.
