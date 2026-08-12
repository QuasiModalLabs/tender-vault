# Vehicles

Supply arrangements and standing offers. What we hold, what we don't, and what each one excludes.

Read this when a notice is a `qualification` or a `call_up`. A call-up against a vehicle we don't hold is not biddable regardless of fit — say so rather than assessing it.

**Status is the load-bearing field.** The same notice reads completely differently depending on whether we're on the vehicle.

**Record what each vehicle gated.** When a briefing finds notices blocked behind a vehicle we don't hold, add the count and date here. Over a few months that becomes the evidence for whether qualifying was worth it — which is a question no single week can answer.

> **The 48 / 50 / 53 denominators below are directional, and the series breaks after 2026-08-09.** Two things happened to them.
>
> First, all three were taken under an ingest cutoff that compared a wall-clock instant rather than a date, so notices closing at 14:00 on the boundary day survived a morning run and were dropped by an afternoon one. The same feed measured 53 one hour and 48 the next. Each denominator may be off by a few notices for a reason that has nothing to do with procurement.
>
> Second, from 2026-08-09 the ingest stopped deleting near-close notices — they enter tagged `imminent`. The corpus went from 53 to 70 on an unchanged feed. A jump of that size in a later entry is the filter change, not the market.
>
> **The numerators are unaffected by either.** Which notices a vehicle gates does not depend on the cutoff, so the SBIPS-versus-TBIPS comparison stands on its own. Read the counts, treat the denominators as approximate, and don't compare a ratio from before 2026-08-09 with one from after.

---

## SBIPS — EN537-05IT01/N

Solutions-Based Informatics Professional Services.

**Status:** not held.
**Refresh:** continuous. Notice `cb-20-75132174` closes 2028-07-04, so timing is not the constraint.

Outcome-based solution delivery rather than resource categories. The structural opposite of the body-shop work the profile excludes, which makes it the best fit of the three.

Promoted to `watching/` 2026-08-04.

**Gated:** 0 of 48 notices, week of 2026-08-04. The RFSA is the only notice in
the corpus mentioning SBIPS at all. Best fit, no observed traffic — the open
question against TBIPS's 7.

**Gated:** 0 of 50 notices, week of 2026-08-06 — on a re-ingested corpus, so
this *is* an independent second observation. `cb-20-75132174` remains the only
notice mentioning SBIPS at all. Two separate weeks of zero observed traffic
against TBIPS's seven is now a real comparison rather than one reading counted
twice.

_(An earlier 2026-08-06 note recorded 0 of 48 from the stale 2026-08-04 ingest
and correctly refused to count it. The ingest has since been re-run; the line
above replaces it.)_

**Gated:** 0 of 53 notices, week of 2026-08-09 — third independent ingest.
`cb-20-75132174` is still the only notice in the corpus mentioning SBIPS at all.

**Restated 2026-08-09 on the unfiltered corpus: 0 of 70.** Same day, wider
corpus, not a new week. Worth recording because it rules something out: the
three-week zero was not an artefact of the closing-date filter hiding SBIPS
call-ups. Seventeen previously invisible notices produced none.

**Gated:** 0 of 71 notices, week of 2026-08-11 — fourth independent ingest, on a
feed downloaded the same evening (912 raw notices against Sunday's 896).
`cb-20-75132174` is still the only notice in the corpus mentioning SBIPS at all.
Four separate weeks now, one of them on a corpus that grew.

**Four weeks of zero, against TBIPS's 7 → 7 → 9 → 15.** This is now a series
rather than a pair of readings, and it says something the individual weeks did
not: the vehicle that best fits the profile produces no observable call-up
traffic in this corpus, and the vehicle that fits worst gates everything. That is
the argument for qualifying on TBIPS *despite* preferring SBIPS — and it is an
argument from four weeks of a filtered IT-services corpus, not from the whole
feed. What this count excludes: call-ups competed among SBIPS holders with no
public notice, which is most of them. Zero observed is not zero occurring.

_(Counted "three weeks" until 2026-08-11. The TBIPS series is not directly
comparable across its whole length — 7 and 7 were taken under the old
closing-date filter, 9 was too, and 15 follows the fix. The **zero** is
comparable throughout, because a filter that hid near-close notices could only
ever have hidden SBIPS traffic, not invented it.)_

---

## TBIPS — EN578-170432

Task-Based Informatics Professional Services.

**Status:** not held.
**Refresh:** continuous. Notice `cb-364-13128756` closes 2028-07-04.

Priced by resource category and level. Not the work we want, but it is the gate on most federal IT call-ups — holding it converts unbiddable listings into biddable ones.

Note that several TBIPS call-ups cite no arrangement number and never say "supply arrangement." Their only signal is the vehicle name in the title, which is why `kind_basis: prose_vehicle_name` exists and why it's worth confirming against the notice.

Promoted to `watching/` 2026-08-04.

**Gated:** 7 of 48 notices, week of 2026-08-04 — 4 explicit call-ups plus 3
typed as plain RFPs. Mostly DND, NRC, ISC. Every call-up in the corpus that
week was a TBIPS call-up.

But **2 of the 7 carry a second gate**: `cb-998-30821848` (ISC) is open only to
Indigenous TBIPS SA Holders, and `cb-94-51127631` (NRC) is a Voluntary
Indigenous Set-Aside. Holding TBIPS would not reach either. Count reachable
separately from gated — 5, not 7 — or the number overstates what qualifying
buys.

**Gated:** 7 of 50 notices, week of 2026-08-06 — on a re-ingested corpus, and an
independent observation. Still **5 reachable**: `cb-998-30821848` (ISC, Indigenous
Tier 1 only) and `cb-94-51127631` (NRC, Voluntary Indigenous Set-Aside) carry the
same second gate as last week.

The total held at 7 but the composition changed: `cb-330-42994613` (DND/MARPAC)
closed out, and `cb-937-38464611` (Public Safety, Office of the Foreign Influence
Commissioner) arrived. All 7 are now typed `call_up` — last week 3 of the 7 were
plain RFPs identified from prose.

_(An earlier 2026-08-06 note recorded the same 7 from the stale 2026-08-04
ingest and correctly declined to tally it. The line above replaces it.)_

**Gated:** 9 of 53 notices, week of 2026-08-09 — **7 reachable**. First week the
count has grown rather than churned: all seven from 2026-08-06 are still open, and
two arrived — `cb-477-31226224` (ISC, MS Dynamics 365 / Power Platform: ERP
analysts, programmer/analysts, technology architect) and `cb-719-13916324` (DND,
one Level 2 programmer for the bilingual R2MR mental health mobile app).

The same two second-gated notices as the previous two weeks — `cb-998-30821848`
(ISC, Indigenous Tier 1, NCR) and `cb-94-51127631` (NRC, Voluntary Indigenous
Set-Aside) — so reachable is 7 of 9, not 9.

Both new arrivals are ordinary resource-category call-ups. They move the gate
arithmetic and nothing else; neither is work the profile would want on its own
terms. Worth keeping the two counts distinct as this series grows: *notices the
vehicle gates* and *notices worth bidding if it did not* are different numbers,
and so far the second one is zero every week.

**Restated 2026-08-09 on the unfiltered corpus: 14 of 70, 12 reachable.** Not a
new week — the same day re-read after the ingest stopped deleting near-close
notices. Five call-ups had been invisible: `cb-303-67468850` (Transport Canada),
`cb-935-52253963` (Canadian Coast Guard), `cb-189-58294946` and `cb-786-4578560`
(DND), and `cb-330-42994613` (DND/MARPAC — recorded on 2026-08-06 as having
closed out, which it had not; it closes 2026-08-15). Supersede the 9-of-53 line
above with this one when comparing; do not count both.

**Gated:** 15 of 71 notices, week of 2026-08-11 — **13 not second-gated**. Fourth
independent ingest, on a feed downloaded the same evening. Compare this against
the 14-of-70 restatement above, not against the 9-of-53 line, which was taken
under the old closing-date filter.

One arrival: `cb-40-97221487` (GAC, "TBIPS AI Support Services", closes
2026-08-28) — up to three years, business analysts, data scientists and platform
resources. The same two Indigenous second gates as every previous week
(`cb-998-30821848`, `cb-94-51127631`), so reachable is 13 of 15.

_(A briefing earlier on 2026-08-11 re-read the 2026-08-09 rows and correctly
declined to tally them as a new observation. The line above is from a genuinely
new ingest and replaces nothing.)_

### Open question: "holding TBIPS" is not one thing

The five newly visible call-ups make a distinction the earlier counts could not.
They do not open to TBIPS holders generally — they open to holders qualified in a
specific **tier, region and resource category**:

- `cb-303-67468850` — Tier 2, National Capital Region, I.5 Information Management
  Architect Level 3.
- `cb-935-52253963` — Tier 1, National Capital Region, five categories.
- `cb-998-30821848` — Indigenous holders, Tier 1, NCR.

So "12 reachable" overstates what qualifying buys, in exactly the way the
original 7 overstated it before the Indigenous set-asides were separated out.
The number we actually want is *notices reachable at the tier and categories we
would qualify for*, and it cannot be computed until that choice is made.

**Recorded as an open question, not a figure**, because inventing a number here
would repeat the mistake this file exists to catch. When the tier decision is
made, re-derive the count against it and note which tier the count assumes —
a reachable figure without its tier is not interpretable.

**2026-08-11: the evidence now points somewhere.** `cb-40-97221487` (GAC) states
its gate explicitly and is the fourth call-up in this corpus to do so, which
makes a table possible where before there were scattered notes:

| Notice | Tier | Region |
|---|---|---|
| `cb-303-67468850` (Transport Canada) | Tier 2 | National Capital Region |
| `cb-935-52253963` (Canadian Coast Guard) | Tier 1 | National Capital Region |
| `cb-998-30821848` (ISC) | Tier 1, Indigenous holders | National Capital Region |
| `cb-40-97221487` (GAC) | Tier 1 | National Capital Region |

**Every call-up that names a region names the NCR; three of four are Tier 1.**
Still not a decision, and deliberately not converted into a reachable count. What
it does establish is a direction: a qualification that is not Tier 1 / NCR would
reach less of the observed traffic than the headline count implies. What it
excludes, as always: call-ups competed among holders with no public notice, which
is most of them — and eleven of the fifteen gated notices state no tier at all,
so this table is four observations out of fifteen, not a distribution.

**What the gate looks like from the buyer's side.** `WS5819275303-Doc5819275371`
(DND, 2026-08-19) is the one call-up in the corpus that states the rule
explicitly: uninvited SA holders may request an invitation up to five business
days before closing and will normally receive one, but unqualified bidders "will
have to qualify for Supply Arrangement # TBIPS SA EN578-170432 before they are
given an opportunity to bid," and "Canada will not extend RFP # WS5819275303 to
provide additional time for Bidders to qualify." Holding the SA is admission,
not advantage. Quote it when the qualification cost comes up for decision.

---

## ProServices

Professional services for lower-value requirements.

**Status:** not held.
**Refresh:** continuous. Notice `cb-8448-42897985` closes 2028-07-04.

Reasonable third. Cheap to hold.

**Gated:** 0 of 48 notices, week of 2026-08-04 — recorded as a zero, not as an
absence of checking.

**Gated:** 0 of 50 notices, week of 2026-08-06, on a re-ingested corpus.

**Gated:** 0 of 53 notices, week of 2026-08-09. Third verified zero. Restated on
the unfiltered corpus the same day: **0 of 70**.

**Gated:** 0 of 71 notices, week of 2026-08-11. Fourth verified zero, independent
ingest.

**Read the RFSA before qualifying.** `cb-8448-42897985` states that part of this
method of supply is set aside under the Procurement Strategy for Indigenous
Business. Check which streams that covers before treating ProServices as cheap
and unconditional — the TBIPS count taught the same lesson at a cost of two
notices.

---

## Considered and declined

**PSPC AI Source List (ITQ)** — closes 2026-09-30. A pre-qualified source list for AI services. Real and current, but only worth it if we intend to become an AI shop.

**Software Licensing SLSA** — excluded by its own terms: explicitly not for IT professional services or cloud-based solutions such as software as a service. It is licence resale.

> **Gated:** 1 of 53 notices, week of 2026-08-09 — the first observed traffic
> through any declined vehicle. `SSC-26-00034429:T`, Adobe licence renewal for the
> RCMP, issued pursuant to SLSA EN578-232335/071/SMS, selective tendering to ten
> named SA holders and Class-1 resellers. The requirement is a quantity list of
> Adobe seats.
>
> This *confirms* the decline rather than reopening it: the one call-up we have
> seen come through the vehicle is exactly the resale work its own terms describe.
> Recorded because a declined vehicle with observed traffic is a stronger record
> than a declined vehicle with none — the counts for the vehicles we hold-or-not
> only mean something if the declined ones are counted the same way.
>
> **Restated the same day on the unfiltered corpus: 2 of 70.**
> `SSC-26-00034425:T` — Alteryx Designer for DND, SLSA EN578-232335/065/SMS, six
> subscriptions over twelve months — was inside the old cutoff and invisible.
> Two observations, both quantity lists of licences, both selective-tendered to
> named SA holders and Class-1 resellers. The decline is now evidenced twice.
>
> **Gated: 2 of 71, week of 2026-08-11** — the same two notices, both still open.
> No new traffic through the vehicle on a fresh feed.

**SaaS Method of Supply** — we would be the SaaS vendor. We are not one.

**Subscription Agent Services** — magazine subscriptions.

**SSC Technical Integration Services for Conferencing and AV** (`cb-181-76348100`,
ongoing to 2030-01-27) — categories are installers, control-system programmers
and AV technical support. AV integration, not IT modernization. Seen 2026-08-04.
