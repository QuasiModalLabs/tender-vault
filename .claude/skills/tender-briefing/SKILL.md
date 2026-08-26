---
name: tender-briefing
description: Produce the weekly tender briefing as a vault file. Use when asked to review the corpus, walk through what's open, do a weekly read, or say which tenders are worth time. Not for single-tender questions or dossier queries.
---

# Tender briefing

Write to `vault/briefings/briefing-YYYY-MM-DD.md`, dated today. Never print the
briefing to the terminal — it gets read in Obsidian, and wide tables
truncate in a terminal.

Read `vault/profiles/my-company.md` and `vault/reference/vehicles.md`
first. The profile decides fit; the vehicles file decides eligibility.

Then read the corpus end to end with `list-corpus`, which returns every
notice ordered by closing window. Don't survey with `search` — it ranks
against a query and returns n, which answers a different question — and
don't read ChromaDB directly. Read the `provenance` block it returns
before anything else, and report it (see Rules).

`list-corpus` is the only corpus call. The one signal tool this briefing
uses is `lobbying-signals`, and only for departments already appearing in
sections 3 and 6 — never as a survey.

It takes the `agency` string from `list-corpus` directly: the parenthetical
acronym tail is stripped before matching, so
`Department of Employment and Social Development (ESDC)` resolves to
`esdc`. Two cases still need you. An agency naming **two** departments —
`Department of Transport (TC) / Department of Fisheries and Oceans (DFO)` —
is refused rather than guessed, because a filter has to mean one
department; run `resolve-department` to see both keys and pick the one the
notice is actually about. An agency that resolves to **nothing** is usually
a Crown corporation with no registry entry, which is not a reason to skip
the notice — see the `unrecognised` rule below.

Query **once per distinct department**, not once per notice; sections 3
and 6 together are a handful of departments most weeks.

## Structure

Open with an H1 naming the week, then two lines summarising what needs
action. Obsidian previews the first lines of a file in its list view —
that summary is how a reader scans months of briefings without opening
them.

**1. Act now**
Things with a clock: `closing_window: imminent`, and any
`closing_date_conflict`. A body deadline earlier than the field is the
costliest error in the corpus — lead with it and quote the notice.
Absence of a conflict key means none was detected, not that the date
was verified.

`closing_window` is derived per query from `closing_date` against the
profile's `imminent_within_days`, so it is correct on the day you write
rather than the day of the ingest. Get it from `list-corpus` or `get`;
it is not stored in the corpus. A `closed` window means the notice
expired since the ingest — say so, don't list it as open.

**Nothing goes in this section that doesn't need a decision this
week, and imminent is not the same as actionable.** Most weeks a dozen
or more notices are imminent and nearly all are call-ups against a
vehicle not held, product buys or staff augmentation. Those go to
section 5 under their failure class exactly as they would at any other
closing date — putting them here replaces an empty section with a
useless one. Section 1 takes imminent notices that would otherwise be
worth acting on, plus every conflict.

**Report the imminent count either way**, including when none of them
are actionable. A verified zero and an unexamined zero read the same
on the page, and only one of them is worth anything in three months.

**2. Closing soon, no action**
Closing within 14 days but *not* imminent, and not actionable. One
collapsed callout with a table inside. A reader who wants to confirm
nothing was missed can open it; nobody has to scroll past it.

**3. Worth bidding**
The short list. For each: what the work actually is, why it fits, and
the disqualifying facts stated plainly rather than implied —
clearance, set-asides, incumbency, Canadian content weighting. If a
tender stretches the profile, name which capability it stretches and
quote the profile against the notice.

One optional line per tender on **the buyer, never the notice**:

> ESDC has taken 41 communications on procurement subjects in the
> window, across 19 organizations.

**No vendor names and no office-holder names in this section.** Naming a
firm beside a specific notice invites the reader to connect the two, and
nothing in the data connects them: `Government Procurement` is a subject
code covering everything the department buys, so a named firm here is
category-level overlap dressed as context for this requirement. Vendor
names belong in section 6, where the claim is about the department rather
than about a solicitation.

It is context on the buyer, **not** a reason to bid or not bid. It may
not change this section's ordering or which tenders appear in it. Omit it
entirely when nothing informative comes back — a tender with no lobbying
context reads normally, and there is no "none found" line.

**4. Vehicles**
Cross-reference `vault/reference/vehicles.md`. Report qualification
notices open now, and how many notices this week were gated behind a
vehicle not held. Record the gating count even when it's zero — a
verified zero is what makes the comparison meaningful in three months.

For vehicles already declined in `vehicles.md`, reference rather than
restate: one line saying how many are open again and pointing at the
file. Don't re-argue a decision that's already recorded.

**5. Skip**
Grouped by failure class, not listed flat. The classes that recur:
product or SaaS purchase, call-up against a vehicle not held, staff
augmentation, results notice with nothing to bid, out of domain,
non-federal. One line each. Collapsed.

**6. Pre-RFP signals**
Closed RFIs and summary reports. Nothing to submit, but they name
requirements that haven't been competed yet. Say which department and
which program.

Also the department's **disclosed lobbying activity**, which is a pre-RFP
signal by definition — who has been in the room while a requirement was
still being decided. This section is department-level, so vendor names and
office-holder roles are legitimate here in a way they are not in section 3:

> **ESDC** — three digital-services signals in the corpus this week. In
> the same window the department logged 41 communications on procurement
> subjects, most frequently with the ADM, Digital Services Branch. The
> organizations appearing most often are TECHNATION (9), Oracle (6) and
> ThinkOn (4).
>
> This is disclosed contact, not influence: it says who has been in the
> room with the department, and nothing about any specific requirement.

**Say when two signals converge.** An RFI or summary report from a
department that is also taking procurement meetings is two independent
pre-RFP signals landing on one buyer, and that is the strongest thing this
section can report. Name it as convergence; don't compute anything from it.

**Say when the archive is missing.** If `lobbying-signals` returns
`state: source_archive_not_acquired`, the layer has no data because nobody
has downloaded it — give me the download line rather than dropping the
content silently. That is a missing file, never an absence of activity.

## Presentation

Use Obsidian callouts. They're native, need no plugins, and degrade to
plain blockquotes anywhere else.

```
> [!danger] Closes Friday, not the 21st
> The body states 2026-08-07 at 14:00 EDT.

> [!warning]- Weak basis — confirm against the notice
> Classified on "TBIPS" in a title, no arrangement number cited.
```

A trailing `-` collapses the callout by default. A trailing `+` leaves
it expanded but foldable.

| Callout | For |
|---|---|
| `danger` | date conflicts, anything `imminent` |
| `tip` | worth bidding |
| `warning` | stretches, weak `kind_basis`, confirm-before-acting |
| `failure` | gated behind a vehicle not held |
| `info` | recorded, no action needed |
| `quote` | text quoted from a notice |

**Colour by instrument state, never by fit.** "Gated behind TBIPS" is
a fact about the notice. "Strong match" is a judgment, and rendering a
judgment as a colour is a rating scale wearing a costume. There is no
`success` callout in that table for the same reason — it implies a
verdict the reader hasn't reached yet.

**Collapse by default:** section 2, section 5, the declined-vehicles
line, and any table over six rows. Expand by default: sections 1, 3
and 4.

**A closing-date timeline** is worth including when three or more
notices close within the briefing window. Dates are facts, so this
adds no judgment — and a `closing_date_conflict` renders as two bars
for one notice, which makes the anomaly visible rather than described.

````
```mermaid
gantt
    dateFormat YYYY-MM-DD
    axisFormat %b %d
    section Act now
    ESDC Canada.ca (body)  :crit, 2026-08-04, 2026-08-07
    ESDC Canada.ca (field) :done, 2026-08-04, 2026-08-21
    section Worth bidding
    CDIC app maintenance   :active, 2026-08-04, 2026-09-01
```
````

Use `crit` for date conflicts and the `imminent` window, `active` for
the worth-bidding set, `done` for everything else. Don't put the skip
list on it, and leave `standing` notices off entirely — a placeholder
year renders as a bar running off the chart.

**No emoji status markers.** They age badly and read as noise at
length.

## Rules

**Report corpus provenance, and never infer it from file timestamps.**
`list-corpus` returns a `provenance` block: `corpus_built_at` and
`feed_downloaded_at` as recorded by the ingest, the same two from the
newest digest, and a `state`. State them in the briefing. If this
machine's feed stamp is behind the digest's, say so and say plainly that
the briefing cannot see anything the newer corpus holds — the reader's
fix is `git pull` then `python scripts/ingest`.

`chroma_db/` mtimes are **not** evidence of anything: ChromaDB rewrites
its segment files on every read, so they report when you last queried.
A briefing that dates the corpus from them is describing its own read,
and one already did.

**A fresh `corpus_built_at` is not a fresh briefing.** A corpus rebuilt
off an unchanged `.cache/tenders.csv` has a new build stamp and the old
feed stamp; the data is exactly as old as it was. When the two digests
agree on the feed but not the build, any change in what's in the corpus
is a filter or profile effect — never report it as new notices.

**`unstamped` and `no_feed_at_build` are different findings.** The first
means the corpus predates stamping and a rebuild will date it. The
second means it was built with no cached feed, so its data cannot be
dated and a rebuild alone will not help. Don't collapse them.

**Wikilink only tenders that exist in the vault** — `watching/`,
`parked/` or `archived/`. Everything else is plain text with the ID.
Fifty unresolved links per briefing makes the graph unreadable and
hides what's actually being tracked.

**Read the descriptions.** Product buys file as `*SRV` and type as
Request for Proposal. Nothing structural separates a SaaS purchase
from a services engagement in this feed — the classifier can't and
shouldn't try. Catching them is the job of reading.

**State the basis when it's weak.** `kind_basis:
procurement_category_residual` means solicitation is what's left over,
not a positive finding. `prose_vehicle_name` means the vehicle was
inferred from a title, not a cited arrangement number — flag those for
confirmation.

**`unrecognised` is not `non_federal`.** Federal Crown corporations —
CDIC, BDC, Canada Post — have no entry in a registry of departments. A
registry miss is not evidence of anything. Never treat it as a reason
to skip.

**Never rank by a composite score.** No weighted totals, no
convergence numbers, no ordering by a computed fit value, and no
colour or icon standing in for one. Present the facts and the
reasoning; the reader decides. This is the formula that was
deliberately deleted from this project, and a briefing template is
exactly where it would return.

**A `standing` window is not a deadline.** Sentinel years — 2065, 2076,
2100 — are the feed's way of saying an arrangement has no real close.
`closing_window` reports them as `standing` with a null
`days_until_close`, so don't quote a countdown for one, don't sort them
into the tail of a date-ordered list, and don't describe one as closing
in fifty years.

**Don't rank by value either.** `estimated_value` is unreliable —
present in a small minority of notices and often reading a ceiling or
a bond amount rather than a contract value. Say "not stated" rather
than implying zero.

**Correct the reference files.** If something you observe contradicts
`vehicles.md` — a gating count that was overstated, a vehicle now
held, a set-aside that means qualifying wouldn't reach a notice — say
so and update the file. Don't work around it silently.

**Say what you didn't cover.** Uncoded source systems, notices with no
description, a section with nothing in it: state it rather than
leaving a silent gap. An empty section should say why it's empty.

**Lobbying is presence, never influence.** Filing a monthly communication
report is what *compliance* with the Lobbying Act looks like — the
organizations named are following the law, and the office holders named
are doing their jobs. Never offer a meeting as the explanation for an
award, a requirement's wording, or an audit finding. Not directly, not by
adjacency, not under a deadline. This binds harder at office-holder level
than at firm level, because the subject is then a named public servant.

**Never rank or colour by lobbying.** It is neither an instrument state
nor a score input, so it gets no callout type of its own and never orders
a section. Both rules above already say this — *never rank by a composite
score*, and *colour by instrument state, never by fit* — and a meeting
count is exactly the kind of number that looks like it wants to be a
ranking.

**Absence of lobbying is not evidence.** Only *arranged oral*
communications with *designated* office holders are reportable; a written
submission, an unarranged conversation, or a meeting below the DPOH
threshold leaves no record. The database is windowed at ingest as well,
three years by default — check the `window` block. Write "no reportable
communications in the window," never "nobody lobbied them."

**Never print a lobbying count over an unresolved person.** Office-holder
titles are filer-entered free text and drift badly: at SSC, 49 real people
appear as 101 distinct name+title combinations, so counting on name+title
undercounts by roughly half. Resolve on person name, not name+title, and
report the title variants rather than picking one — *"the Chief Technology
Officer (filed variously as CTO, Acting CTO and Deputy CTO)."* Names
themselves are sometimes transposed (`Dunne Brendan` where every other row
is First Last), so where a person can't be resolved confidently, name the
role and skip the number.

**Never infer a promotion or portfolio change from title sequence.** It
looks recoverable and isn't: one SSC official's eleven title variants have
heavily overlapping date ranges, with `CTO` appearing *before* `Acting
CTO` and both running concurrently with the full form. Different filers
write the same period differently. A role change needs a source that
publishes appointments.
