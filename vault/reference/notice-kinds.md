# Notice kinds

What kind of thing a notice is, read before assessing fit. Most of what looks like a bad match is a good requirement in the wrong instrument.

One definition — `classify_notice` in `scripts/ingest/classify.py` — used by both the dossier and the corpus.

## The eight values

**`qualification`** — RFSA, standing offer, or invitation to qualify. Puts a supplier through a gate; buys nothing. Never present one as a tender to price, and never dismiss one either: for a firm with no federal past performance, getting through the gate is what makes the work reachable at all.

**Three instruments share this value, and they do not open the same door.** A supply arrangement or standing offer puts you on a vehicle, and work is competed later as call-ups against it, often with no public notice — cross-reference `vehicles.md`. A **source list** is the same shape under a different name: a pre-qualified list, and the one ITQ that reached the corpus on the 2026-08-17 feed was exactly that (`Invitation to Qualify to Artificial Intelligence Source List`). But a **stage-1 or phase-1 ITQ qualifies you for ONE named project's later stage** — there is no vehicle, there are no call-ups, and there is nothing in `vehicles.md` to cross-reference. `Phase 1 - ITQ – EV385-261359 – CSC Sask Pen Roof Replacement` is that shape.

No structured field separates them, so the `kind_note` on an ITQ says so rather than guessing, and the notice has to be read. Every stage-1 ITQ on the 2026-08-17 feed was construction and dropped on relevance, so this has not yet reached a briefing — the first services ITQ filed for a single named project is when it does.

**`call_up`** — work competed among suppliers already on a vehicle. The opposite of a qualification. Only holders can bid, so say so when we don't hold it.

**`results_notice`** — announces who already qualified. No solicitation document, nothing to bid, and the shortlist named in it is closed. Never present one as an opportunity. Checked before notice type, because a results notice inherits the type of the competition it reports — one filed as "Request for Proposal" would otherwise read as open work.

**`information`** — an RFI. Nothing to bid. The value is that a requirement is coming and nobody has been asked yet; treat it like the pre-RFP position the dossier exists to find.

**`pre_awarded`** — an ACAN, or a notice typed `Directed Contract`. An award already intended for a named supplier, contestable only inside the posting window. Both notice types are filed by the publisher; the directed-contract literal was added after three of them were found landing in the `solicitation` residual, where a sole-source reads as open work. All three were `*CNST` on the 2026-08-17 feed and none reached the corpus — but note they are now stopped by relevance rather than by the construction filter, because a mapped notice type outranks the category. See "The order the rules run in", below.

**`product`** — categorised goods-only. A purchase, not an engagement.

**`solicitation`** — the residual, and **not a positive finding**. It means an open competition whose shape is unresolved. Two things routinely land here:

- SaaS and COTS purchases filed as services. Nothing structural separates them from a services engagement in this feed — both are `*SRV`, both type as Request for Proposal. Only reading the description catches them.
- Call-ups typed as a plain RFP. Several TBIPS notices carry no arrangement number and never say "supply arrangement."

Read the description before calling one of these a services engagement.

**`unknown`** — neither field was filed. Read the description.

## `kind_basis` — how strong the classification is

- **`notice_type`** — the publisher's controlled vocabulary. Reliable.
- **`prose_arrangement_number`** — a cited supply arrangement number (`EN578-170432` is TBIPS). Strong, but only present when the notice cites one.
- **`prose_vehicle_name`** — only the word TBIPS or SBIPS appearing in the text. Weaker. Worth confirming against the notice before acting.
- **`procurement_category_residual`** — only the goods/services split was available, so the shape is a guess at the category level. Never state a `solicitation` is a services engagement on the strength of this label alone.

## What the classifier deliberately doesn't do

Arrangement numbers are matched against a curated list of six observed in genuine supply-arrangement context, not against a format pattern. PSPC solicitation numbers share the same shape, and a format rule relabelled three construction projects as IT call-ups when it was tried.

Construction is decided from `*CNST` before any prose rule runs, for the same reason. It is **not** decided before the notice type, which is a different claim and the one that catches people out — see the next section.

## The order the rules run in, and what it costs

Five steps, and the order is load-bearing at every one:

1. **Results-notice prose.** Checked first, because a results notice inherits the notice type of the competition it reports.
2. **Notice type.** The publisher's controlled vocabulary.
3. **Construction category** (`*CNST`).
4. **Vehicle prose** — a cited arrangement number, then failing that a vehicle name.
5. **Goods/services residual.**

**Step 2 outranks step 3, and that has a consequence worth stating outright:** a construction-category notice that carries any mapped notice type is *not* classified `construction`. On the 2026-08-17 feed that was **38 of 162 `*CNST` notices**:

| notice type | n |
|---|---|
| Invitation to Qualify | 22 |
| RFP against Supply Arrangement | 4 |
| Request for Standing Offer | 4 |
| Request for Information | 3 |
| Directed Contract | 3 |
| Request for Supply Arrangement | 2 |

All 38 therefore skip the construction drop at ingest, and are removed by the **relevance filter** instead, on their `72xxxxxx` construction commodity codes. Zero of the 38 survived to the corpus, so nothing is leaking today — but the gate that stops them is relevance, not the construction filter, and a construction notice whose codes fell inside a profile family would reach the corpus under its instrument kind.

Adding a notice-type literal therefore moves notices *out* of `construction`, which is not obvious from the diff: mapping `Directed Contract` at step 2 took those three out of the construction bucket at step 3 and is why this count is 38 rather than 35.

The ordering is deliberate — instrument shape is decided before what is being bought, so that getting onto a vehicle is never hidden by the commodity — and it is pinned by `test_notice_type_wins_over_category`. The count above is what it excludes, measured on one feed; it drifts as the feed does.
