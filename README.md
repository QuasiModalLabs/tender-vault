# Tender Vault

**An experiment in finding federal contracts before they exist.**

Canadian government procurement is public, enormous, and almost impossible to read. Every week a few hundred new tender notices appear on CanadaBuys. By the time you see one, the requirement is already written, the incumbent already knows the department, and you're a stranger filling in a response form against someone who has been in the building for three years.

This project started as a way to read the tender feed faster. It ended up somewhere more interesting: an attempt to move backwards through the procurement lifecycle until you arrive *before* the RFP — at the point where a department is still deciding what it wants.

Claude does the reasoning. An [Obsidian](https://obsidian.md) vault of plain markdown files is the memory.

---

## The thing I got wrong the first time

A few months ago I built the obvious version: a retrieval pipeline that matched a company profile against active tenders. Semantic search, keyword search, a scoring formula with hand-tuned weights for contract value and timeline and complexity, and one call to an LLM at the end to write a strategic summary.

It worked. It was also the wrong shape, and it took me a while to see why:

> The system was solving *retrieval* really well. But tender research isn't a retrieval problem — it's a reasoning problem sitting on top of retrieval.

Three symptoms of that mismatch:

**Every query started from zero.** I'd find a promising tender on Monday, think about it, and by Thursday the system had no idea we'd ever met. All the context lived in my head.

**The scoring weights were guesses.** I'd decided that value mattered 0.3 and timeline mattered 0.2, and I had no principled reason for either number. Meanwhile the actual disqualifying facts were things a formula can't see — *this one needs Secret clearance and we don't have it.*

**The intelligence arrived last.** The model got invoked once, at the end, to summarize a decision the formula had already made.

## The rewrite

So I inverted it. The retrieval layer shrank to about 300 lines of Python — search, fetch, find-similar, and a few file operations. Everything above that is Claude, working directly against a folder of markdown files.

The scoring formula is gone entirely. Claude reads the tender and says *in range, but requires clearance we don't have — skip.* No weights to tune.

Memory is now literal. Tenders I care about get promoted out of the search corpus into the vault as markdown files, moving through three states: **watching** (actively considering), **parked** (deferred, with the specific condition that would revive it), and **archived** (decided, with the reason appended). When I ask about a tender next week, Claude reads the file and sees the history.

That last part turned out to matter more than expected. Because every decision is a readable file, a year from now I can search the vault for "lost to incumbent" and find the pattern. That's a hard question to ask a database.

The split is deliberate: the search corpus is rebuilt from scratch on every ingest and holds everything; the vault holds only the handful worth attention and never gets overwritten. Most tenders are noise. A few deserve persistent context. The storage matches the reality.

### Then the files started linking to each other

Markdown files in a folder are a filesystem. Markdown files that reference each other by name are a graph, and Obsidian renders one for free.

A department gets two files, deliberately split. `vault/agencies/ircc.md` is the department *node* — created on first promote, hand-edited afterwards, never overwritten by anything. `vault/intel/agencies/ircc-contracts.md` is generated from the contracts database and rewritten on every ingest. Keeping them apart means the graph works whether or not you've built the optional contracts layer, and means nothing generated can land on top of something written by hand: the generator only writes over files carrying its own frontmatter marker.

A tender's `department` field links the node, so the node's backlinks are every tender, briefing and note touching that department. *What are we looking at from IRCC* becomes a question the vault answers by structure rather than by search.

The node is also the one file Claude writes to unprompted, appending dated entries under `## Notes`. An entry has to still be true after the tender that taught it closes — *how this department buys, which incumbents keep reappearing, what I decided and why.* Per-tender analysis stays in the tender file. In six months I'll remember the no-bid and not the reason for it, and the reason is the part worth keeping.

## Then I realised the tender feed is already too late

Here's the problem with the whole premise. A tender notice is the *end* of a process. Someone identified a need, got budget, wrote requirements, and published. If your first contact with a department is the notice, you're responding to a document shaped by other people's conversations.

So I went looking for what happens earlier. Three more public datasets, each answering a different question about a federal department:

| Question | Source | What it tells you |
|---|---|---|
| What's being asked for? | CanadaBuys tender notices | The live opportunity feed |
| Who actually won? | Proactive contract disclosure | Incumbents, market size, expiry dates |
| What do they intend to change? | Departmental Plans | Stated spending intent, where a department files the prose |
| What have they been caught failing at? | Auditor General audits | Independent public criticism — the thing that forces a procurement |

Read in order, those move steadily earlier in time: from *an RFP is open now*, back through *an RFP is predictably coming*, to *here are the conditions that will produce one*.

<details>
<summary><strong>Federal procurement vocabulary used below</strong></summary>

The rest of this README assumes these. None are this project's coinages —
they're the government's.

**Classification**

- **UNSPSC** — United Nations Standard Products and Services Code. The commodity
  taxonomy CanadaBuys files notices under, hierarchical L1–L4. This project's
  primary relevance filter, matched by prefix.
- **GSIN** — Goods and Services Identification Number. Canada's older, parallel
  taxonomy. PSPC publishes a GSIN↔UNSPSC mapping; this project reads only the
  UNSPSC side of that file and never joins on GSIN. See *The profile*, below.
- **`procurementCategory`** — a coarse CanadaBuys field (construction, goods,
  services) populated on 100% of notices. Used to drop construction.

**Instruments** — the eight `opportunity_kind` values, defined in full in
[`vault/reference/notice-kinds.md`](vault/reference/notice-kinds.md)

- **Supply arrangement / standing offer** — a pre-qualified vehicle. Being on one
  is a precondition for bidding certain work, not a contract in itself.
- **Qualification notice** — a posting that puts suppliers onto a vehicle. Buys
  nothing today; often the most valuable thing in the corpus for a firm with no
  federal past performance.
- **Call-up** — work competed among suppliers already on a vehicle. Only holders
  can bid.
- **Results notice** — announces who already qualified. Nothing to bid; the
  shortlist is closed.
- **Source list** — a pre-qualified supplier list, common in construction.
- **RFI / ACAN** — a request for information (nothing to bid, but a requirement
  is coming), and an Advance Contract Award Notice (an award already intended
  for a named supplier).

**Vehicles and bodies**

- **TBIPS / SBIPS** — Task-Based and Solution-Based Informatics Professional
  Services. The two federal IT-services supply arrangements, and the words the
  government actually uses where a vendor would write "IT consulting".
- **PSPC** — Public Services and Procurement Canada, the central purchasing
  department. **SSC** — Shared Services Canada, the government's IT department
  and largest federal IT buyer. **OAG** — Office of the Auditor General.
- **MX, PW, SSC** (as source systems) — prefixes on a notice's reference number
  identifying the publishing system. These three file no UNSPSC codes at all.
- **CKAN** — the open-source data portal software behind open.canada.ca. The
  audit layer pulls through its API.

</details>

The last one is the most useful and the least obvious. When the Auditor General publicly reports that a department's systems are failing, that department is going to spend money. It's the most citable pre-RFP signal there is, because it's an independent authority saying the quiet part in public.

```
 OPPORTUNITY         OUTCOME            INTENT            SCRUTINY
what's asked        who won          what's planned    what's flagged
     │                  │                  │                  │
CanadaBuys       Proactive contract   Departmental      Auditor General
tender feed        disclosure            Plans              audits
     │                  │                  │                  │
     ▼                  ▼                  ▼                  ▼
filter to your    filter, dedupe,    score for         score for
profile, embed    aggregate by       modernization     IT relevance,
for search        procurement        intent            tag department
     │                  │                  │                  │
     └────────┬─────────┴─────────┬────────┴─────────┬────────┘
              ▼                   ▼                  ▼
     ┌──────────────────────────────────────────────────┐
     │  Claude — reads the vault, calls the tools,      │
     │  and does the actual thinking                    │
     └──────────────────────────────────────────────────┘
```

Two storage choices worth explaining, because they're the same decision made twice in opposite directions.

Tender notices are **prose** — long descriptions of work, where "cloud migration" and "infrastructure modernization" should match each other. That's what semantic search is for, so those go into a vector database.

Contract awards are **records** — vendor, department, value, date. The questions are arithmetic: *who are the top five vendors by total value since 2022?* Semantic search answers that badly; SQL answers it exactly. So those go into SQLite.

Plans and audits sit in between: prose with structure. Their text gets scored once, at ingest, by pointing the embedding model at two poles — pull *toward* modernization language, push *away* from routine-operations language — and the resulting score lands in SQLite for cheap ranked queries. It costs zero model tokens and runs in about a minute.

## The corpus reports its own age

Research that spans weeks needs to know how old its evidence is, so every `list-corpus` and `get` carries a `provenance` block rather than leaving that to inference.

There are **two stamps, because they answer different questions.** `feed_downloaded_at` is how old the data is; `corpus_built_at` is when it was last processed. A rebuild off an unchanged feed moves the second and not the first — so if corpus membership changes across that, it's a filter or profile effect rather than new notices. A third stamp, `feed_sha256`, answers the question neither date can: *which* feed, by content. That one is comparable across machines — download dates are not, since two machines holding identical bytes disagree on when they fetched them — so the block reports which of the two it compared on. It carries the same stamps from the newest committed digest, which makes a local corpus running behind the daily CI run say so out loud, with the fix (`git pull`, then re-ingest).

Stamps rather than file timestamps, because ChromaDB rewrites its segment files whenever anything *loads* the collection: `chroma_db/` mtimes report when the corpus was last queried, not when it was built. Read them and you're describing your own read.

The `state` field keeps three cases apart that would otherwise look identical — `stamped`, `unstamped` (predates stamping; a rebuild will date it), and `no_feed_at_build` (built with no cached feed, so its data can't be dated and a rebuild alone won't fix it).

**Closing windows are computed per query, not stored.** `closing_window` is derived from `closing_date` against the profile's `imminent_within_days`, so it's correct on the day you ask rather than the day of the ingest: `imminent`, `open`, `closed`, `standing`, `unknown`. Notices closing in two days are *labelled*, never dropped — whether a short fuse is disqualifying is the reader's call, for the same reason the scoring formula is gone. `standing` catches the sentinel years the feed uses for arrangements with no real close, so nothing ever reports a fifty-year countdown.

## Does the pre-RFP idea actually work?

The hypothesis was that a department which says it plans to modernize something will, later, buy that thing.

Testing it was the point where this stopped feeling like architecture and started feeling like a tool. Claude found a stated modernization intent in a 2019 departmental plan, checked it against the contracts database, and surfaced the matching award two years later. The chain held.

The same test broke the first version of the feature. My initial pass scored the retrospective explanation field, which describes how last year's spending differed from plan. Claude's critique of the output pointed out that the field had effectively died after 2019, so the tool was ranking six-year-old archaeology and scoring genuine IT modernizations negatively. Pivoting to the forward-looking planning field fixed that. Finding the live data resource, which runs through 2025-26 where the CSV I started from appeared frozen at 2021, made it current rather than historical.

The answer to the heading is more qualified than I expected. `planning_explanation` is not a statement of intent. It is an optional note explaining why planned spending moved between years, and sixteen of ninety-four organizations file none at all. Those sixteen include National Defence, Global Affairs, the RCMP, CBSA and Shared Services Canada. Ranking on that field returned nothing for the largest IT buyers in government, and reported it in language that sounded like an absence of intent rather than an absence of prose. Where departments do file it, one sentence is often pasted across an entire program inventory. There are 384 such groups government-wide, so six scored programs can be one signal counted six times.

The audit layer holds up better. Its top-ranked results are recognizable: *Modernizing Information Technology Systems*, *Combatting Cybercrime*, *Cybersecurity in the Cloud*. Shared Services Canada, the federal government's IT department, recurs throughout.

## Then I measured it, and it didn't hold

Everything above this line is the case for convergence made from examples. One chain that held, a top-ranked list that looks right. That is how you convince yourself of something, not how you check it, so I built the backtest before building anything else on top: `python scripts/backtest.py`.

The setup. For each federal department and each fiscal year, take only what was public six months before the year opened — audits, stated plans, incumbent contracts running down — and ask whether it predicts that the department published a relevant IT tender during that year. The target is written into [`scripts/backtest.py`](scripts/backtest.py)'s docstring and frozen before any feature exists, `as_of(T)` is the only way feature code touches data, and the leak tests in `tests/test_backtest.py` assert nothing dated after T ever comes back through it.

This needed history the repo didn't have, and it turned out to be sitting in plain sight: CanadaBuys publishes **per-fiscal-year archives of every notice back to 2009**, on the same host and with the same 67 columns as the live feed, plus award notices that link to them on `solicitationNumber` — 60% of them match, against 4 in 1.3 million for the route through the contracts CSV. `scripts/notices_ingest.py` reads them into an append-only table. That is the first thing in this project with a real past tense.

**The base rate is the whole story, and I nearly missed it.** 88% of department-years already contain an IT award. Asked as *will this department buy IT next year*, the answer is yes and no evidence is required. Even on the tightened target the pooled base rate is 68.6%, and in the largest third of departments it is 94% — there is almost no room left to be right in a way that counts. The unit had to be checked too: department × calendar half-year swings 27.7%–68.1% across eight years, which is unusable, and department × *fiscal* year holds inside 12pp. Fiscal-year-end concentration and a quarter of reporting lag, not noise.

And then the finding, which is a null:

| | uncontrolled | inside a size stratum |
|---|---|---|
| `audit_it` | 1.46× | 1.06× (large), 1.41× on n=6 (medium) |
| `expiring_awards` | 1.37× | 0.98× (large), 1.00× (medium) |
| `plan_intent` | 1.19× | 1.06× (large), 1.26× on n=10 (medium) |

**Zero of fifteen feature-by-stratum comparisons survive.** Not one lift above 1.0 whose interval excludes its own stratum's base rate. The Auditor General audits large departments; large departments tender constantly; `audit_it` was reading the second fact through the first. Control for how much a department buys and every signal collapses.

The fitted score does beat chance at a ten-department alert budget — 27 of 30 across three held-out years against 20.7 expected. It should. Ranking organizations by how much IT they buy predicts whether they will buy IT again, and 5 to 8 of each top ten are simply the biggest buyers. That is worth knowing and it is not the pre-RFP thesis.

What it doesn't establish, and I want to be precise here rather than generous to myself: this tested *whether a department tenders anything relevant at all in a year*. That is a coarse question with a 68.6% base rate, and the dossier's actual use has always been *what* is coming and *why now* — a specific system, a named incumbent, a date. Nothing here speaks to that, because three fiscal years of department-years cannot. So the honest reading is not "the evidence is worthless"; it is **"convergence at the department level is not a prediction, and I was one graph away from building as though it were."** The next experiment needs a finer target, and it needs more years than CanadaBuys has published since 2022.

The one design rule I held to throughout: the score lives in the backtest module and nowhere else. `vault/CLAUDE.md`, the dossier and the briefing skill are untouched, and no MCP tool returns a number. A score is allowed there because it is the hypothesis under test — the thing being measured — and every weight in it is fitted out of fold. A weight I chose by hand would be the formula I deleted at the start of this project, wearing a lab coat.

## The failure worth recording

I also tried to mine the contracts data for two things that would have been genuinely valuable, and both failed on the same wall.

The first was **re-compete churn**: departments that keep cycling through vendors on the same capability, which would mark them as winnable. The second was **process provocations**: reading how a department currently does something and proposing a better way, unprompted.

Neither is possible with this data, because federal contracts describe work as coarse procurement categories — "Information technology and telecommunications consultants" — and nothing else. A $585 million contract is described in fifty-seven characters. There is no process to interrogate and no capability to track churn on.

I'm recording that because it's a durable boundary, not a bug to fix later: **this dataset is good for *who won what, roughly* and useless for *what is actually happening*.** Every future idea gets tested against that line.

The provocation idea isn't dead, though — it just needed sources that carry intent and detail. Which is exactly what the plans and audit layers turned out to be.

## Convergence

There are four signals about any given federal department. Each is useful alone. The real payoff is when they **converge** — when the Auditor General has flagged a department, *and* its own plan says it intends to modernize that same system, *and* the incumbent's contract expires in five months. That's about as strong a pre-RFP case as public data can produce, and any live tender from that department should be read in that light.

The shape is a department dossier: `dossier ircc` returns everything all four sources know, so a tender stops being an isolated notice and becomes *a tender from the department the AG flagged for processing backlogs, that plans to modernize case management, whose incumbent contract runs out in the spring.*

<details>
<summary><strong>Running your first dossier</strong></summary>

In Claude Code you just ask — *"give me the full dossier on IRCC"* — and Claude
calls the tool. `dossier` is a subcommand of `scripts/tender_tools/`, not
something you type into the chat. To see the raw JSON yourself:

```bash
python scripts/tender_tools dossier ircc
```

Each of the four sections needs its own layer built, and the dossier renders
with whatever you have:

| Section | Needs | On a fresh clone |
|---|---|---|
| `identity` | `data/crosswalk.db` — **ships with the repo** | Works immediately |
| `audits` | `python scripts/oag_ingest.py` | Empty until built |
| `plans` | `python scripts/plans_ingest.py` | Empty until built |
| `contracts` | `python scripts/contracts_ingest.py` (~630MB) | Empty until built |
| `tenders` | `python scripts/ingest.py` | Empty until built |

Every section carries a `state` field, and the states distinguish *no data* from
*no signal* — `attributed` versus `no_audits_found` is the difference between a
layer you haven't built and a department the Auditor General has never examined.
That distinction is the reason there's no score.

Read `identity` first. On IRCC it reports that the contracts filed under the
pre-2015 `cic` slug — 2,327 rows — are folded in, and that Passport Canada was
absorbed in 2013 and contributes nothing to this extract. Those are the joins
that a naive name match silently gets wrong.

See [`vault/reference/dossier.md`](vault/reference/dossier.md) for how to read
the sections and what each `state` means.

</details>

Before building it I found out why a naive version wouldn't work, which saved me a bad afternoon: **the four sources name departments differently.** The audits say "Immigration, Refugees and Citizenship Canada." The contracts say "National Defence | Défense nationale." The plans say "Department of Citizenship and Immigration." A naive join returns nothing at all, silently.

So convergence needed a name-resolution layer underneath it first, and then the dossier on top — assembling the signals, not scoring them cleverly. I deleted a scoring formula at the start of this project and the dossier still has no score in it: it presents four sections and Claude judges. A number would have hidden the reasoning that makes the thing worth reading.

That layer is `vault/crosswalk/org_aliases.yaml`: one canonical key per organization, and all four signal tools take it, so the same `pspc` works in every one. The audits resolve against it too — a department on an audit is a registry key, not a string an extractor guessed.

**The most valuable dossier has no tender in it.** A department with an audit finding, a stated plan, an expiring incumbent and no open notice is the pre-RFP position the whole project exists to find — the work is coming and nobody has been asked yet. So the tenders section is optional by construction and the dossier renders fully without it.

Two findings about the audits cost me a day. An audit rarely has one department: half of those that name any name several, and ArriveCAN audited CBSA, PHAC and PSPC together, so attribution had to become a join table. Keeping the first match threw away half the signal.

The second is that most of the OAG corpus audits nobody. Of 364 records, about 110 examine a federal department. The rest are committee briefing packages, the Auditor General's own quarterly financials, Crown corporation examinations and territorial audits. Counting those as unattributed invented a 62% coverage gap that was never real. The figure worth quoting is 91% of the federal audits.

## What four independent ingests produced

The first thing this thing told me that I didn't already believe came out of counting the same number four ingests running.

Two federal IT supply arrangements matter here. **SBIPS** sells outcomes — solution delivery priced as a result, the structural opposite of the body-shop work the profile excludes. **TBIPS** sells resource categories and levels, which is precisely the work we'd rather not do. On fit, it isn't close: SBIPS is the one to want.

Every weekly briefing records how many notices each vehicle *gated* — listings we couldn't bid because we don't hold the arrangement — including the zeros, because a zero you verified and a zero you never checked read identically three months later.

| Ingest | Corpus | SBIPS gated | TBIPS gated | TBIPS reachable |
|---|---|---|---|---|
| 2026-08-04 | 48 | 0 | 7 | 5 |
| 2026-08-06 | 50 | 0 | 7 | 5 |
| 2026-08-09 | 53 | 0 | 9 | 7 |
| 2026-08-11 | 71 | 0 | 15 | 13 |

Four independent ingests — note the dates, which are two and three days apart, not seven. What makes them independent is that the published feed moved between them, and since the switch to a daily cadence that is recorded rather than assumed: each ingest stamps the feed's content hash, and two readings sharing a hash are one reading counted twice. The vehicle that fits the profile best produces no observable call-up traffic at all; the vehicle that fits worst gates everything, and its share is growing. That's the argument for qualifying on TBIPS *despite* preferring SBIPS, and no single reading could have made it — one is an anecdote, four is a series. The denominators move with the corpus and with one filter change partway through; the SBIPS zero is unaffected by both, since a filter that hid near-close notices could only ever have hidden traffic, not invented it.

Two refinements came out of the counting, and both cut the headline number down.

**Gated and reachable are different questions.** Two of the notices carry a second gate — an Indigenous Tier 1 restriction and a Voluntary Indigenous Set-Aside — that holding TBIPS wouldn't reach. Reported as one number, the count overstates what qualifying buys.

**"Holding TBIPS" isn't one thing.** Call-ups open to holders qualified in a specific **tier, region and resource category**. Four notices in the corpus state theirs explicitly:

| Notice | Tier | Region |
|---|---|---|
| `cb-303-67468850` (Transport Canada) | Tier 2 | National Capital Region |
| `cb-935-52253963` (Canadian Coast Guard) | Tier 1 | National Capital Region |
| `cb-998-30821848` (ISC) | Tier 1, Indigenous holders | National Capital Region |
| `cb-40-97221487` (GAC) | Tier 1 | National Capital Region |

Every call-up that names a region names the NCR, and three of four are Tier 1. That's a direction rather than a decision — eleven of the fifteen gated notices state no tier at all, so this is four observations, not a distribution — and the reachable count gets re-derived against the tier once that choice is made, because a reachable figure without its tier isn't interpretable.

What every one of these counts excludes: call-ups competed quietly among holders with no public notice, which is most of them. Zero observed is not zero occurring.

The sharpest statement of what a vehicle is actually worth came from the notices themselves. One DND call-up spells the rule out: uninvited arrangement holders may request an invitation up to five business days before closing and will normally receive one, while unqualified bidders "will have to qualify for Supply Arrangement # TBIPS SA EN578-170432 before they are given an opportunity to bid," and "Canada will not extend RFP # WS5819275303 to provide additional time for Bidders to qualify." **Holding the arrangement is admission, not advantage.**

## The weekly briefing

The recurring output is a briefing written to `vault/briefings/`, not printed to a terminal — it gets read in Obsidian, where the tables and the Mermaid closing-date timeline render. It's defined as a [skill](.claude/skills/tender-briefing/SKILL.md): six sections, ordered by what needs a decision rather than by what scored highest. *Act now* (anything imminent, and every date conflict), *closing soon, no action*, *worth bidding*, *vehicles*, *skip* grouped by failure class, and *pre-RFP signals*.

The presentation rules turned out to carry design weight, because a template is where a deleted formula tries to come back:

**Colour by instrument state, never by fit.** "Gated behind TBIPS" is a fact about the notice; "strong match" is a judgment, and rendering a judgment as a colour is a rating scale wearing a costume. There's no `success` callout in the palette for exactly that reason — it would imply a verdict the reader hasn't reached yet.

**Report the zero.** An empty section says why it's empty; uncovered ground gets named rather than left as a silent gap. Most of the value in a series of briefings is in the counts that didn't change.

**Correct the reference files.** They're written from past observation and aren't regenerated by any ingest, so when live data disagrees with a recorded count, the file gets fixed in place. The failure worth designing against is the line that's *true but incomplete* — TBIPS gating 7 notices was correct, and still overstated what qualifying reached until the set-asides were separated out. Record what a number excludes, not only what it counts.

## What I know is wrong with it

**It's slow.** The old pipeline answered in three seconds. A multi-step Claude session takes twenty to sixty. Fine for *what should I look at this week*. Not fine for anything with a UI.

**It's not reproducible.** Two runs of the same question won't follow identical reasoning. Acceptable for research, disqualifying for a product that needs to be auditable.

**It only works because the corpus is small.** The 2026-08-11 run went from 912 open tenders down to 71. At ten thousand, the markdown-file pattern strains badly. This is right-sized for one firm, not for a platform.

> **On every number in this file.** These are measurements from a specific day's
> feed, recorded to show orders of magnitude and where the filter loses things —
> they are not invariants, and they drift as the feed does. The funnel that
> `python scripts/ingest.py` prints on each run is the authority on your corpus,
> not anything written here.

**There is no contract value.** The feed publishes no value field, and the regex that used to invent one has been retired. Measured on the 2026-08-04 feed: only 94 of 896 descriptions contain a dollar figure at all, and the first one is usually not the price — the single most common extraction was $10,000,000, off construction source lists reading "estimated value of $10 million and below", which is a ceiling on a qualification vehicle. The resulting field (median $10M, max $5B) described nothing. `estimated_value` is now omitted rather than stored as `0.0`, so unknown stops rendering as free. `--extract-values` reads the descriptions with a model instead, and needs an API key.

**Four things are read out of prose, and prose rules are the fragile ones.** Results notices (postings that only announce who already qualified), call-ups whose notice type was filed as a plain RFP, provincial/territorial notices, and descriptions stating a submission deadline earlier than the closing-date field. Each is precision-first and each reports its evidence in `kind_basis`, because they are not equally strong: of the three call-ups the structured field missed, only one cites an arrangement number — the other two are recoverable only from the word "TBIPS" in the title. The arrangement numbers are a curated list, not a pattern; matching the PSPC number *format* relabelled a wharf reconstruction and a building demolition as IT call-ups, because solicitation numbers and supply-arrangement numbers are shaped identically.

**Relevance leans on the publisher, and the publisher has gaps.** Tenders are filtered on their UNSPSC commodity codes where CanadaBuys files them, because guessing a procurement officer's vocabulary is how a boiling-liquid-expanding-vapour-explosion study ends up in a cloud search — "vapour cloud". But three source systems file no codes at all (MX, PW and SSC — 37 of 431 notices post-filter on the 2026-08-04 feed), and one of them is Shared Services Canada, the largest federal IT buyer. Those fall back to keyword matching, and the ingest funnel prints the split every run so the gap stays visible.

**The contracts data is directional, not exact.** It's unaudited, vendor names are only lightly normalized (near-variants may still count separately), reporting lags about a quarter, and contract amendments are aggregated per procurement family using the highest recorded value — which avoids double-counting but under-represents families that straddle the date window.

**The Auditor General's own deep links are dead.** 214 of the 364 audit records carry an `oag-bvg.gc.ca` URL that now serves an error page under an HTTP 200, so nothing about it fails loudly. The dossier links to the CKAN dataset instead and keeps the original URL as citation text.

**Promoted tenders can drift.** Promoting copies the description into a markdown file. If CanadaBuys amends the notice afterwards, the copy doesn't know. Amendments are rare in practice.

**The tender documents themselves are off-limits to the code.** The notice is public; the RFP package it points at is not. Those live on commercial platforms — Ariba, MERX — behind account walls that exist to know who took the document, and getting past one programmatically means holding a credential and behaving like a browser. This project deliberately doesn't scrape them, and there's no fetcher anywhere in it to quietly grow into one. This paragraph is the whole record of that decision; the diagnostic that established it was a throwaway and wasn't kept.

**Attachments are a manual drop, and that is the same decision rather than a reversal of it.** `attach <tender_id>` creates a folder beside the tender note and prints its path. A human opens MERX or Ariba in a browser, signs in, downloads the package, and drops the files there; `list-attachments` extracts the text and `read-attachment` pages through it. Nothing in that path touches the platform — the code reads a directory. The extracted text stays out of git and out of the ChromaDB corpus, because it's third-party content from a commercial platform and the extraction is that same content in another encoding. The corpus exclusion is also structural: it's rebuilt from scratch on every ingest with no survival exemptions, and a document that outlived a rebuild would be the first thing to break that.

## The profile

Everything filters through one file: `vault/profiles/my-company.md`. Its frontmatter drives the ingest:

```yaml
# value_min / value_max are commented out — the feed has no value field
unspsc_families: ['8111', '8116', '4323', '80101507']
competencies: [informatics, information technology, TBIPS, software, cloud, SaaS, ...]
exclude: [janitorial, landscaping, catering, food service]
imminent_within_days: 10   # labels near-close notices; excludes nothing
```

That snippet is four keys out of a dozen; the frontmatter also carries the contracts filter and the theme example sentences that drive the plans and audit scoring. **The full specification is the profile's own inline comments** — every key is annotated in place with what it does and why the shipped value was chosen. [`docs/PROFILE.md`](docs/PROFILE.md) covers what that file can't say about itself: the two-pole scoring mechanics, how to tune the poles, and which edits force a rebuild.

The profile shipped here is a representative IT-consulting firm rather than a real client, which keeps the repo self-contained — swap in your own and everything downstream retargets.

`unspsc_families` is the primary filter and matches UNSPSC codes by prefix, so `8111` catches every `8111xxxx`. The list is hand-checked and committed on purpose. `scripts/unspsc_discover.py` regenerates candidates against PSPC's reference file — downloaded to `.cache/` on first run, and carrying the L1–L4 hierarchy plus a Construction/Goods/Services type per code — but only the UNSPSC side of that file is ever read. Its GSIN linkage is unusable for this: PSPC's own caveat says the mappings were assessed at higher levels and carried through indiscriminately, and it shows, with telecom cable laying and highway paving both landing on "Foundation work, including pile driving".

Construction is dropped on `procurementCategory`, the one classification field populated on 100% of notices across every source system. That alone removes 78 notices, including the Defence Construction source lists and a fishermen's-wharf reconstruction that a keyword filter kept surfacing.

`competencies` is the fallback for notices with no commodity code, matched on whole words so "aws" matches Amazon Web Services but not "flaws". Worth knowing before you tune it: on the live feed `AWS`, `Azure`, `DevOps`, `cybersecurity` and `data engineering` match **zero** notices between them. The government writes *informatics*, *TBIPS*, *information technology*. The full funnel — date, exclusions, construction, UNSPSC coverage, relevance — prints on every ingest, so you can tune against the live distribution rather than guessing.

## Running it

Python 3.11. The first run downloads a ~90MB embedding model; no API key is needed.

```bash
git clone https://github.com/QuasiModalLabs/tender-vault.git
cd tender-vault
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

$EDITOR vault/profiles/my-company.md   # the filter reads from this — read its comments
python scripts/ingest.py               # tender corpus, ~2 min
python scripts/plans_ingest.py         # departmental-plan signal, ~1 min
python scripts/oag_ingest.py           # Auditor General signal, ~1 min
```

To re-run the backtest rather than take my word for the null, add the notice
history and point it at them. The archives are static for closed fiscal years,
so this is a download rather than a re-scoring:

```bash
python scripts/notices_ingest.py       # ~340MB of archives -> data/notices.db
python scripts/backtest.py --write     # report to vault/reference/
python tests/test_backtest.py          # the leak tests, which are the point
```

Then either open Claude Code in the repo root and ask *"any good federal IT tenders for us this week?"*, or wire it into Claude Desktop as an MCP server.

**Nothing derived is committed.** Every layer is filtered and scored against your profile, so the same source data produces different results for different firms — which makes a shipped database actively misleading rather than a convenience. Only `data/crosswalk.db` ships, because it derives from `org_aliases.yaml` and is a fact about the Government of Canada rather than about any one firm. The venv is optional for the commands above but required for the MCP path.

The contracts layer is a further ~630MB download (`python scripts/contracts_ingest.py`) and is optional if you only want opportunity discovery — but `contracts-intel`, `expiring-contracts` and the contracts section of the dossier stay empty without it.

**Full install, MCP configuration, and troubleshooting: [`docs/SETUP.md`](docs/SETUP.md).**

Fresh tender data arrives on its own — a GitHub Action re-runs the tender ingest every morning and commits a markdown digest with a *new since* section diffed against the previous run. It only rebuilds when the published feed actually moved, which it checks with a conditional request rather than a clock — so outside the unconditional Monday rebuild, a digest existing for a date means the feed moved that day. A month of digests is its own artifact: you can see the corpus shift.

The contracts and plans refresh workflows are manual-dispatch only. They rebuilt databases that are no longer committed, so on a schedule they would run and commit nothing — rebuild those locally when you want fresh data.

## Files worth reading, in order

1. [`vault/CLAUDE.md`](vault/CLAUDE.md) — the agent's instructions. The most important design document in the repo; everything else is plumbing.
2. [`vault/profiles/my-company.md`](vault/profiles/my-company.md) — how user context is stored, and the key-by-key filter spec in its own comments. [`docs/PROFILE.md`](docs/PROFILE.md) is the companion on tuning it.
3. [`vault/reference/vehicles.md`](vault/reference/vehicles.md) — the gating series above, as it was actually recorded: dated observations, what each count excludes, and an open question left as a question rather than rounded into a number.
4. [`.claude/skills/tender-briefing/SKILL.md`](.claude/skills/tender-briefing/SKILL.md) — the briefing skill. The presentation layer is where "don't produce a score" has to be enforced concretely, so this is more design document than template.
5. [`scripts/tender_tools/`](scripts/tender_tools) — the retrieval layer, and the clean line between retrieval and reasoning. Start at [`cli.py`](scripts/tender_tools/cli.py) for the command surface; [`paths.py`](scripts/tender_tools/paths.py) and [`corpus.py`](scripts/tender_tools/corpus.py) own the only mutable state in the package, and their headers say why nothing else may copy it.
6. [`scripts/attachments.py`](scripts/attachments.py) — reading the RFP package a human downloaded by hand, and the module docstring is most of why it's here. A dispatch table rather than a chain of special cases, so a new format is one function; a per-file hash, because MERX posts addenda mid-solicitation and stale text that looks current is worse than no text; and the argument about spreadsheets, which was never that they're unreadable but that flattening a grid detaches a figure from its line item. The extractor that eventually read them keeps every value addressable instead.
7. [`vault/crosswalk/org_aliases.yaml`](vault/crosswalk/org_aliases.yaml) — the department registry. Ninety-odd hand-checked assertions about what the Government of Canada calls itself. Its `observed_names` are the ones seen in real source data, and `vault/crosswalk/attestation.yaml` records where and when each was seen — written by `python scripts/crosswalk.py --attest`, and committed because the evidence itself expires. The tender feed carries only notices open on the day it was downloaded, so an agency with nothing open drops out of it; without a durable record, a correct alias starts looking invented.
8. [`scripts/org_resolve.py`](scripts/org_resolve.py) — resolving organizations named in free text against that registry, and the one department identifier every signal tool takes.
9. [`scripts/plans_ingest.py`](scripts/plans_ingest.py) — the two-pole scoring technique, with the docstring explaining why the forward-looking field beats the retrospective one.
10. [`scripts/contracts_ingest.py`](scripts/contracts_ingest.py) — streaming filter over millions of rows into SQLite; the design notes are in the module docstring.
11. [`scripts/oag_ingest.py`](scripts/oag_ingest.py) — the audit pull, relevance scoring, and department attribution.
12. [`scripts/backtest.py`](scripts/backtest.py) — the experiment that checked the premise, and the module docstring is most of why it's here: the frozen target predicate, a per-feature table of why each input was knowable at the date it's read as of, and the argument for why a score is permitted in that one file and nowhere else. It also carries the record of the one time the predicate changed, before any result existed, and why that run was discarded rather than patched.
13. [`.github/workflows/ingest.yml`](.github/workflows/ingest.yml) — how data stays fresh without me remembering.

## What comes next

- **A department-level tender index.** The dossier reads the open-notice feed directly and resolves entity names per query, which is fine at the ~900 notices the feed carried in August 2026 and won't be at ten thousand. The attribution belongs at ingest, next to where the audits already write theirs.
- **A pre-mortem command.** For any tender under serious consideration: *assume we bid and lost, or won and regretted it — walk backwards and tell me why.* One adversarial pass against my own enthusiasm before committing. This is the surviving core of a multi-persona "steering committee" feature I cut mid-build; the personas changed tone without changing reasoning, but the skepticism they were reaching for is real.
- **A profile refinement loop.** Quarterly, read across everything watched, parked, and archived, and propose profile edits based on revealed preference. One structural catch to design around: the vault only knows about tenders that survived the filter, so it can improve precision but is blind to recall. It has to be paired with an audit that samples what the filter *rejected*.
- **The tier decision, and the count that follows it.** The gating series says qualify on TBIPS; it doesn't yet say at which tier, region and resource categories. Once that's chosen, the reachable count gets re-derived against it — and the series keeps running either way, since the value of a verified zero is that it accumulates.
- **Similarity drift.** Flag a new tender that closely resembles one archived as a loss.
- **Win/loss pattern mining**, once the archive is deep enough to say things like *we lose every tender that requires active SOC work*.

None of these need new infrastructure. That's mostly what the markdown-first design bought.

---

## Sources and licence

- [CanadaBuys tender notices](https://canadabuys.canada.ca/en/tender-opportunities) — active federal opportunities
- [CanadaBuys tender notice archives](https://open.canada.ca/data/en/dataset/6abd20d4-7a1c-4b38-baa2-9525d0bb2fd2) and [award notices](https://open.canada.ca/data/en/dataset/a1acb126-9ce8-40a9-b889-5da2b1dd20cb) — per-fiscal-year history back to 2009, publication-dated; what the backtest runs on
- [Proactive Publication of Contracts](https://open.canada.ca/data/en/dataset/d8f85d91-7dec-4fd1-8055-483b77225d8b) — awarded federal contracts over $10K
- [GC InfoBase Departmental Plans / Results](https://open.canada.ca/data/en/dataset/b15ee8d7-2ac0-4656-8330-6c60d085cda8) — planned spending and forward-looking planning prose
- [Office of the Auditor General](https://open.canada.ca/data/en/organization/oag-bvg) — performance audits, via the open.canada.ca CKAN API

Contains information licensed under the [Open Government Licence – Canada](https://open.canada.ca/en/open-government-licence-canada). Code is MIT.

The derived contracts database is committed as a static file, so you can browse it with no server at all by pointing [Datasette Lite](https://lite.datasette.io/) at its raw GitHub URL.
