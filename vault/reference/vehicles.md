# Vehicles

Supply arrangements and standing offers. What we hold, what we don't, and what each one excludes.

Read this when a notice is a `qualification` or a `call_up`. A call-up against a vehicle we don't hold is not biddable regardless of fit — say so rather than assessing it.

**Status is the load-bearing field.** The same notice reads completely differently depending on whether we're on the vehicle.

**Record what each vehicle gated.** When a briefing finds notices blocked behind a vehicle we don't hold, add the count and date here. Over a few months that becomes the evidence for whether qualifying was worth it — which is a question no single reading can answer.

> [!important] What counts as one observation, now that the ingest is daily
> Entries below are labelled "week of", written when the ingest ran weekly and the readings were already two and three days apart. The label was never the thing doing the work; **independence was**, and the criterion is whether the published feed moved between two readings.
>
> That is now a recorded fact rather than a judgement. Every ingest stamps `feed_sha256` — the content hash of the feed it read — into the corpus and into the digest frontmatter, and the daily job rebuilds *only* when that hash changes. So:
>
> - **Label new entries `ingest of YYYY-MM-DD (feed <first 8 of the hash>)`**, not "week of".
> - **Two readings that share a feed hash are one observation.** Recording the second as an independent zero would manufacture a measurement out of the scheduling, which is the same error as scoring two arms that received identical inputs. Re-reading an unchanged corpus produces `p = 1.000` by arithmetic, not by evidence.
> - A briefing written against an unchanged corpus may still be useful — dates move, notices close — but it contributes **no new count here**.
>
> The existing entries stand. They were four genuinely separate feeds, and the README's series argument rests on that rather than on the calendar.

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

**Gated:** 0 of 77 notices, ingest of 2026-08-17 — fifth independent observation, on a
feed that moved (71 → 77, fifteen arrivals since `digest-2026-08-10`).
`cb-20-75132174` is still the only notice in the corpus mentioning SBIPS at all. **Five
weeks of zero, against TBIPS's 7 → 7 → 9 → 15 → 16.**

> **Recorded retroactively on 2026-08-26, and that is a correction rather than a new
> reading.** `briefing-2026-08-17` states these counts were written to this file as
> *"ingest of 2026-08-17 (feed f64c5e3a)"*. They were not — `git log` on this file ends
> at the 2026-08-11 commit. The 2026-08-26 read re-derived every figure from the same
> ingest (identical `corpus_built_at`, identical membership) and they match, so the
> observation is sound; what failed was the filing. **The 2026-08-26 read is that same
> ingest and contributes no sixth observation.**

**Gated:** 0 of 66 notices, ingest of 2026-08-29 (feed `19edace6`). `cb-20-75132174` is still
the only notice open that mentions SBIPS at all. **Six recorded zeros, against TBIPS's
7 → 7 → 9 → 15 → 16 → 9.**

> **This series skipped an ingest that TBIPS recorded, and the gap cannot be closed.** The
> 2026-08-26 evening ingest is filed under TBIPS as its sixth independent observation (11 of
> 71); no SBIPS row was written for it. That build has since been superseded and deleted, so
> the count cannot be re-derived, and it is not being reconstructed from the TBIPS entry's
> denominator. The zeros above are the ones actually taken. What this costs is one
> observation of interval, not the argument: a filter or a feed that hid SBIPS traffic could
> only ever have hidden it, never invented it.

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

**Gated:** 16 of 77 notices, ingest of 2026-08-17 — **14 not second-gated**. Fifth
independent ingest, on a feed that moved 71 → 77. Four arrived — `cb-272-69359473`
(Courts Administration Service), `cb-221-29639929` and `cb-630-4686939` (GAC), and
`cb-702-62436471` (DND/MARPAC) — and `cb-303-67468850` returned from a reported close.
`cb-935-52253963` (Coast Guard) and `cb-189-58294946` (DND) left the corpus. The same two
Indigenous second gates as every previous week.

> **Recorded retroactively on 2026-08-26** — see the note in the SBIPS section above for
> why. Re-derived from the same ingest on 2026-08-26 and identical, so this is one
> observation filed late, not two.

**2026-08-26, re-read of the 2026-08-17 ingest: no new observation.** Same
`corpus_built_at` to the second, same 77 records, same 16. Nine days of calendar had
passed, so eleven of the sixteen had closed and five remained open — but **which notices a
vehicle gates does not change when nobody re-ingests**, and this is deliberately not
counted as a sixth week.

**Gated:** 9 of 66 notices, ingest of 2026-08-29 (feed `19edace6`) — **8 reachable**.
Seventh independent observation. The feed moved: `cb-40-97221487` (GAC, AI support) closed,
and two arrived — `cb-956-25478772` (DND, two junior network specialists at CFB Bagotville)
and `cb-692-67452484` (Transport Canada, UX/UI and Drupal application development). The one
not reachable is `cb-192-89046879` (DND, C4ISR): Secret clearance with two Top Secret seats,
controlled goods, citizens only, on-site Ottawa. Holding TBIPS would not reach it.

_(This is the first entry to carry a feed hash. The 2026-08-28 read could not be filed at all
— it had no hash to establish independence by, and said so in the briefing rather than
recording on a weaker basis. Note the hash comes from `corpus-identity.json` in the live
build, because the `provenance` block on `list-corpus` reported it as absent at the time.
**That reporting gap is now fixed** — the block assigned the local hash only when the newest
digest also carried one, so a corpus with a perfectly good hash reported none whenever the
digest predated hashing. It now reports each side's hash whenever that side has one, and
`basis` still falls back to dates when there is nothing to compare against. Future entries can
take the hash straight from the provenance block.)_

**Work behind the gate that we would want, second read running.** `cb-803-76594845` (IRB case
management) and now `cb-692-67452484` (Transport Canada) are both delivery work described as
an outcome. Against that, `cb-956-25478772` is seat-hire — two junior resources — which is the
usual shape and a fair reminder that most of what this gate blocks is still not work we want.

### The tier table, as of the 2026-08-17 ingest

| Notice | Tier | Region |
|---|---|---|
| `cb-303-67468850` (Transport Canada) | Tier 2 | NCR |
| `cb-935-52253963` (Coast Guard) | Tier 1 | NCR |
| `cb-998-30821848` (ISC) | Tier 1, Indigenous holders | NCR |
| `cb-40-97221487` (GAC) | Tier 1 | NCR |
| `cb-272-69359473` (CAS) | Tier 1 | NCR |
| `cb-702-62436471` (DND/MARPAC) | Tier 1 | **not stated** — template placeholder |
| `cb-692-67452484` (Transport Canada) | Tier 1 ($0–$3.75M) | NCR — **arrangement number cited**, 2026-08-29 |
| `cb-937-38464611` (Public Safety) | **none stated** | — |

**Five of seven name a tier, four name the NCR, and one names neither.** The exception is
load-bearing: `cb-937-38464611` states the requirement is *"available to all bidders who
hold a valid TBIPS SA for all the streams and categories for the requirement"* — no tier,
no region. So a non-Tier-1 qualification is not worthless, and the Tier 1 / NCR reading is
a **direction with a documented exception, not a rule**. `cb-702-62436471` reads "Tier 1
under the region / metropolitan area", an unfilled placeholder: a tier observation and
**not** a region observation.

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

**Gated:** 11 of 71 notices, ingest of 2026-08-26 — **10 not second-gated**. Sixth
independent ingest, feed downloaded the same evening. Down from 16 of 77, and **the fall
is the calendar, not the market**: eleven of the 2026-08-17 cohort closed between the two
feeds. Only one second gate survives — `cb-94-51127631` (NRC, Voluntary Indigenous
Set-Aside).

_(Correction to the 2026-08-09 and 2026-08-17 entries: `cb-477-31226224` was carried as
Indigenous-second-gated. It is not. Its only occurrence of "Indigenous" is the buyer's own
name — Indigenous Services Canada. Read against the notice on 2026-08-26.)_

**Five of the eleven are GAC** — `cb-221-29639929` (M365/Azure platform), `cb-180-40241040`
(classified solutions modernization), `cb-40-97221487` (AI support), `cb-630-4686939`
(data/analytics/AI foundations), `cb-335-46131928` (endpoint management). One department is
running nearly half the observed TBIPS traffic in this corpus. That is the clearest picture
yet of what holding the arrangement would actually reach — and it is concentrated in one
buyer, which is a risk as much as an argument.

### 2026-08-26: the "nothing worth bidding anyway" argument no longer holds

Every read from 2026-08-04 to 2026-08-17 concluded that the gated notices were
resource-category postings nobody would want on their own terms, and used that to argue
the gate cost little. **`cb-803-76594845` (IRB Digital Case Management, closes
2026-09-11) breaks that.** It asks for the transition of the IRB case management
ecosystem "toward a modernized, cloud-aligned **microservices architecture** and operating
model" — analysis, architecture, design, development, integration, testing and knowledge
transfer, phased. That is the profile's second core capability nearly verbatim, it is
outcome-shaped rather than seat-shaped, and it sits behind the gate.

**So the running count of *notices worth bidding if the vehicle did not block them* is no
longer zero every week. It is one, once.** Two cautions kept with it: the vehicle claim
rests on the word TBIPS in the **title** and the body cites no arrangement number, so it
needs confirming; and one observation is not a trend.

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

**Gated:** 0 of 77 notices, ingest of 2026-08-17. Fifth verified zero, recorded
retroactively on 2026-08-26.

**Gated:** 0 of 71 notices, ingest of 2026-08-26. Sixth verified zero, independent ingest.

**Gated:** 0 of 71 notices, ingest of 2026-08-26 (feed downloaded 20:25 the same
evening). Sixth verified zero, and a genuinely independent one.

**Read the RFSA before qualifying.** `cb-8448-42897985` states that part of this
method of supply is set aside under the Procurement Strategy for Indigenous
Business. Check which streams that covers before treating ProServices as cheap
and unconditional — the TBIPS count taught the same lesson at a cost of two
notices.

---

## FINTRAC Supervision Modernization Program ITQ — `WS5627542574-Doc5782536172`

Not a supply arrangement. A qualification gate for **one programme**, establishing a list
of Qualified Suppliers for the subsequent phases of the SMP procurement. It is in this
file because this file is where eligibility is decided, and this is an eligibility
decision.

**Status:** not qualified. **Closes 2026-09-10** — and unlike TBIPS, SBIPS and
ProServices, **this one expires.** The three standing arrangements are in continuous
refresh, which is why their decision keeps not being made; there is no such slack here.

**Eligibility:** participation limited to Canadian Suppliers as defined in the
solicitation documents. **No supply arrangement is required.** That is the whole point of
it for a firm with no federal past performance — every other route into this corpus runs
through a vehicle we do not hold.

**Gates:** 0 notices in the corpus today, and every subsequent SMP phase.

**The unknown is the qualification criteria**, which live in the Ariba package and are not
in the corpus. ITQs routinely demand corporate experience references at a scale a
25-person firm may not reach. Pull the package before committing effort.

_(Added 2026-08-26. `briefing-2026-08-17` reported adding this heading and did not; see
the retroactive-filing note in the SBIPS section.)_

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
>
> **Gated: 0 of 71, ingest of 2026-08-26** — the first zero since traffic appeared. All
> three licence renewals (Alteryx, Adobe, Dynatrace) closed between the feeds. Three
> observations stand on the record; the vehicle is simply quiet this week. Nothing
> reopens.
>
> **Gated: 3 of 77, ingest of 2026-08-17** — recorded retroactively on 2026-08-26.
> `SSC-26-00034461:T` (Dynatrace for ESDC, SLSA EN578-232335/196/SMS) joins the Adobe and
> Alteryx renewals. All three are quantity lists of licences selectively tendered to named
> SA holders and Class-1 resellers. **Third observation of traffic through a declined
> vehicle, and all three are exactly the resale work its own terms describe.** All three
> had closed by 2026-08-26. This confirms the decline; it does not reopen it.

**SaaS Method of Supply** (`EN578-191593`) — we would be the SaaS vendor. We are not one.

> **Gated: 1 of 71, ingest of 2026-08-26 — first observed traffic through this vehicle.**
> `cb-700-5070164` (Elections Canada, closes 2026-09-11): a SaaS digital-recruiting
> platform for Protected B information, issued against the SaaS SA, open to **all** GC
> SaaS SA holders. It names its incumbent — VidCruiter Inc., $99,913.28.
>
> **This confirms the decline rather than reopening it.** The one call-up seen through the
> vehicle is a subscription to somebody else's product, which is what its own terms
> describe. Recorded because a declined vehicle with observed traffic is a stronger record
> than one with none.

**Subscription Agent Services** — magazine subscriptions.

**IM/IT Research and Advisory Services — `EN578-260567`.** First seen 2026-08-26.
`cb-772-71669904` (ATSSC, closes 2026-09-07) is open only to holders qualified under this
SA for the NCR, and names the three invited: **Forrester Research, Info-Tech Research
Group, Gartner Canada.** The requirement is an electronic self-service platform of
articles, reports, templates, blueprints and white papers.

This is analyst-subscription resale, not IT services — the same shape as the SLSA, and
declined for the same reason. **Gated: 1 of 71, ingest of 2026-08-26.** Recorded so the
next read does not re-derive it from scratch.

**CRA Professional Services Supply Chain — `cb-394-26364368`, closes 2026-10-02.** Not a
PSPC arrangement: the CRA's own suite of contracts, five streams (Application Development;
IT Oversight/Project Management; Cyber Protection; SAP ERP; Administrative and Non-IT).
Top five ranked bidders win contracts, then every requirement is competed among them.
April 2027 – March 2029 plus three option years.

**Declined on the notice's own eligibility terms**, not on preference: *"Bidders must be
able to supply consultants for every category and level after award."* Every category and
level across five streams, including SAP ERP and non-IT administrative staff. A 25-person
firm cannot certify that. The instrument is also as-and-when consultant supply — the
body-shop shape the profile excludes.

> **Worth keeping for the number in it.** Total invoiced under the PSSC, quoted from the
> notice: **2023 $61,283,633.32 → 2024 $41,421,783.98 → 2025 $15,606,254.92.** A 75% fall
> in two years, which the notice attributes to the Budget 2023 reductions in discretionary
> spending on consulting and professional services. That is the federal IT-services
> consulting market contracting, stated by a buyer in its own tender — context for every
> bid/no-bid decision in this file, and the strongest argument yet for preferring
> outcome-shaped work (SBIPS, CDIC) over seat-shaped work (TBIPS, PSSC).

**SSC Technical Integration Services for Conferencing and AV** (`cb-181-76348100`,
ongoing to 2030-01-27) — categories are installers, control-system programmers
and AV technical support. AV integration, not IT modernization. Seen 2026-08-04.
