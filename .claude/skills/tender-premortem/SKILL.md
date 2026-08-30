---
name: tender-premortem
description: One adversarial pass over a tender before committing to bid. Use when asked to pre-mortem, stress-test, challenge, or argue against a specific tender, or when a decision to bid is close. Not for surveying the corpus — that is the weekly briefing — and not for department research, which is the dossier.
---

# Tender pre-mortem

Two questions about one tender, asked as though the outcome were already known:

> **Assume we bid and lost.** What, visible today, explains it?
>
> **Assume we won and regretted it.** What, visible today, explains that?

This runs against a tender someone has already decided they like. That is the
point — it is a pass against enthusiasm, not a second opinion on a coin flip.
The briefing decides what is worth reading; this decides whether the thing we
already want is a mistake.

## Run the command first

```bash
python scripts/tender_tools pre-mortem <tender_id>
```

It assembles both lenses and scores nothing. Read `how_to_read` in its output
and then `not_checked` — that section is what the exercise could not see, and on
a thin notice it matters more than anything it could.

**Then read the notice itself with `get <tender_id>`.** The command quotes
sentences its probes matched; it does not understand them. A probe is a way of
not missing a sentence, never a substitute for reading the description, and
every judgment in what you write has to come from the text rather than from the
probe's `means` line.

**Promote it first if it isn't promoted.** A pre-mortem is for something under
serious consideration, which is what `watching/` means, and the write target
below is that file. If the tender is only in the corpus, say so and offer
`promote` rather than writing the analysis into the conversation and losing it.

## The two lenses stay apart

They answer different questions from different facts, and the same fact points
opposite ways under the two. A five-year option tail is a foothold under the
first lens and the whole risk under the second. A 70/30 technical/price split is
reassuring under the first and says nothing under the second.

**Never merge them into one verdict about "risk".** Write both, in order, and
let them disagree on the page. A tender that is hard to win and safe to hold is
a different decision from one that is easy to win and hard to live with, and a
single risk paragraph loses which one you have.

**Lost is checked first, because it can end the exercise.** A set-aside, or a
call-up against a vehicle `vehicles.md` records as not held, settles the tender
regardless of fit — say so and stop rather than working the regret lens on work
we cannot bid.

## What to write

Append to the tender's own file under `## My notes`, as
`### YYYY-MM-DD — Pre-mortem`. Never rewrite anything already there, never touch
the frontmatter, and don't create a separate document — the analysis belongs
next to the notice it is about, where it will be read again when the decision
comes back.

Say in one line at the top which lens produced the decisive facts, then:

**1. If we lost, this is why.** The specific facts, quoted. Who is already
named, what bar we don't clear, who holds the work now. Name the ones that are
*eligibility* separately from the ones that are *competition* — the first is a
door, the second is a race.

**2. If we won, this is what we'd regret.** What winning commits us to. The
term and its options, the shape of the work, the capability the profile says we
stretch. Quote the profile against the notice; the command hands you both.

**3. What would have to be true for this to be a good bid.** The most useful
paragraph, and the one that turns a pre-mortem into a decision. Name the
conditions — a partner for a product we don't own, a fact the package would have
to confirm, a clearance we'd need. If the list is short and reachable, that is
the case for bidding, arrived at honestly.

**4. What we couldn't check.** From `not_checked`, in words. The solicitation
package is the usual one and it is not a small gap: mandatory requirements, the
evaluation grid and the security schedule live there, and the notice is a
summary. Say it plainly rather than letting the analysis read as complete.

**5. Recommendation, in words, with the reason named.** *"No-bid, on incumbency
and product access — not on the clock"* is a recommendation. A rating is not.
State what would change it, in the same sentence where you can, because that is
what the file gets read for six months later.

## Rules

**No score, no rating, no risk level.** Not a number, not a colour, not
low/medium/high, not "3 of 5 concerns fired". This is the formula that was
deliberately deleted from this project, and a pre-mortem template is exactly
where it would return wearing a different hat. Present the facts and the
reasoning; the reader decides.

**Don't count the probes.** The command reports every probe, fired and absent,
and refuses to tally them for the same reason. A count reads as a summary and is
a severity score with the arithmetic hidden.

**No personas, and no steering committee.** An earlier version of this feature
put the skepticism in the mouths of invented reviewers — a CFO, a delivery lead,
a risk officer. It was cut because the personas changed the tone without
changing the reasoning: the same three concerns came back in three voices, which
reads as three findings and is one. Argue the case directly. If a concern is
worth raising it is worth raising as yours.

**`absent` is not `clear`.** A probe that did not fire says the phrase is not in
the notice text. The notice is a summary. "No clearance requirement was found in
the notice" is the true sentence; "no clearance required" is not, and the
difference is a bid.

**Never write a lobbying section, and never explain a loss by who was in the
room.** The command has no lobbying section on purpose and you must not add one
from `lobbying-signals` or a dossier. A pre-mortem asks why we lost, so a list
of firms that met the department would be read as the answer no matter what
caveat sat above it — and the data cannot support that claim about named real
companies and named real public servants. If asked directly, decline and say
why. This is the one rule here that is not about analysis quality.

**Incumbency is department-level, not requirement-level.** The command's
`scope_note` says so and it is easy to lose: the terms are the notice's own
matched competencies, which are broad because they are what admitted it to the
corpus. "IBM does a lot of IT work at DND" is what the section supports. "IBM
holds the contract this notice replaces" is not, unless the notice says so.

**Vendor names are lightly normalized.** Near-variants of one firm count
separately — `ADGA GROUP CONSULTANTS` and `ADGA GROUP COUNSULTANTS` are two rows
and one company. Don't report a vendor's total as exact, and don't conclude a
market is fragmented from a list that may be spelling.

**A null bid count is unknown, not one bid.** `number_of_bids` is populated on
roughly a quarter of rows and its absence is not obviously missing-at-random.
Quote `families_reporting` against `families_total` whenever you quote a median,
or leave the number out.

**Read the clearance level, not the word.** Protected A is not Secret, and an
SRCL appears on both. One notice in this vault was nearly skipped on the word
`SRCL` when its actual bar was Protected A, which the profile clears. Quote the
sentence and read the level out of it.

**A `closing_date_conflict` outranks everything here.** If the subject block
carries one, lead with it. Planning to the later date loses the bid whatever the
rest of the analysis says.

**Say when the pre-mortem found nothing.** A tender where both lenses come back
thin is a real result and worth recording as one — it is the evidence that the
enthusiasm was justified. Don't manufacture a concern to make the section look
worked. Two solid facts plus "nothing else in the notice argues against this"
beats five hedges.

**Write the durable lesson into the department node, not here.** If the exercise
turns up something still true after this notice closes — how the department
buys, a step we missed months earlier, an incumbent that keeps reappearing —
that belongs in `vault/agencies/<key>.md` under a dated `## Notes` heading, per
`vault/CLAUDE.md`. The pre-mortem is about this tender; the node is what
outlives it. Tell me in one line that you wrote it.
