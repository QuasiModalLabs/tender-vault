# Notice kinds

What kind of thing a notice is, read before assessing fit. Most of what looks like a bad match is a good requirement in the wrong instrument.

One definition — `classify_notice` in `scripts/ingest.py` — used by both the dossier and the corpus.

## The eight values

**`qualification`** — RFSA, standing offer, or invitation to qualify. Puts a supplier on a vehicle; buys nothing. Work is competed later as call-ups, often with no public notice. Never present one as a tender to price, and never dismiss one either: for a firm with no federal past performance, getting onto the vehicle is what makes the call-ups reachable at all. Cross-reference `vehicles.md`.

**`call_up`** — work competed among suppliers already on a vehicle. The opposite of a qualification. Only holders can bid, so say so when we don't hold it.

**`results_notice`** — announces who already qualified. No solicitation document, nothing to bid, and the shortlist named in it is closed. Never present one as an opportunity. Checked before notice type, because a results notice inherits the type of the competition it reports — one filed as "Request for Proposal" would otherwise read as open work.

**`information`** — an RFI. Nothing to bid. The value is that a requirement is coming and nobody has been asked yet; treat it like the pre-RFP position the dossier exists to find.

**`pre_awarded`** — an ACAN. An award already intended for a named supplier, contestable only inside the posting window.

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

Construction is decided from `*CNST` before any prose rule runs, for the same reason.
