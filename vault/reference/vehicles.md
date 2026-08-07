# Vehicles

Supply arrangements and standing offers. What we hold, what we don't, and what each one excludes.

Read this when a notice is a `qualification` or a `call_up`. A call-up against a vehicle we don't hold is not biddable regardless of fit — say so rather than assessing it.

**Status is the load-bearing field.** The same notice reads completely differently depending on whether we're on the vehicle.

**Record what each vehicle gated.** When a briefing finds notices blocked behind a vehicle we don't hold, add the count and date here. Over a few months that becomes the evidence for whether qualifying was worth it — which is a question no single week can answer.

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

---

## Considered and declined

**PSPC AI Source List (ITQ)** — closes 2026-09-30. A pre-qualified source list for AI services. Real and current, but only worth it if we intend to become an AI shop.

**Software Licensing SLSA** — excluded by its own terms: explicitly not for IT professional services or cloud-based solutions such as software as a service. It is licence resale.

**SaaS Method of Supply** — we would be the SaaS vendor. We are not one.

**Subscription Agent Services** — magazine subscriptions.

**SSC Technical Integration Services for Conferencing and AV** (`cb-181-76348100`,
ongoing to 2030-01-27) — categories are installers, control-system programmers
and AV technical support. AV integration, not IT modernization. Seen 2026-08-04.
