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

The split is deliberate: the search corpus is rebuilt from scratch every week and holds everything; the vault holds only the handful worth attention and never gets overwritten. Most tenders are noise. A few deserve persistent context. The storage matches the reality.

## Then I realised the tender feed is already too late

Here's the problem with the whole premise. A tender notice is the *end* of a process. Someone identified a need, got budget, wrote requirements, and published. If your first contact with a department is the notice, you're responding to a document shaped by other people's conversations.

So I went looking for what happens earlier. Three more public datasets, each answering a different question about a federal department:

| Question | Source | What it tells you |
|---|---|---|
| What's being asked for? | CanadaBuys tender notices | The live opportunity feed |
| Who actually won? | Proactive contract disclosure | Incumbents, market size, expiry dates |
| What do they intend to change? | Departmental Plans | Modernization plans, in the department's own words |
| What have they been caught failing at? | Auditor General audits | Independent public criticism — the thing that forces a procurement |

Read in order, those move steadily earlier in time: from *an RFP is open now*, back through *an RFP is predictably coming*, to *here are the conditions that will produce one*.

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

## Does the pre-RFP idea actually work?

The hypothesis was: a department that *says* it plans to modernize something will, later, buy that thing.

Testing it was the point at which this stopped feeling like architecture and started feeling like a tool. Claude found a stated modernization intent in a 2019 departmental plan, cross-referenced it against the contracts database, and surfaced the matching award two years later. The chain held.

That test also broke the first version of the feature. My initial pass had scored the *retrospective* explanation field — how last year's spending differed from plan. Claude's own critique of the output pointed out that the field had effectively died after 2019, so the tool was ranking six-year-old archaeology and, worse, scoring genuine IT modernizations *negatively*. Pivoting to the forward-looking planning field fixed it. Finding the live data resource — which runs through 2025-26, where the CSV I'd started from appeared frozen at 2021 — made it current rather than historical.

The audit layer validates the same way. Its top-ranked results are real and recognizable: *Modernizing Information Technology Systems*, *Combatting Cybercrime*, *Cybersecurity in the Cloud*. Shared Services Canada — the federal government's IT department — recurs again and again. That's not a bug in the ranking. That's the finding.

## The failure worth recording

I also tried to mine the contracts data for two things that would have been genuinely valuable, and both failed on the same wall.

The first was **re-compete churn**: departments that keep cycling through vendors on the same capability, which would mark them as winnable. The second was **process provocations**: reading how a department currently does something and proposing a better way, unprompted.

Neither is possible with this data, because federal contracts describe work as coarse procurement categories — "Information technology and telecommunications consultants" — and nothing else. A $585 million contract is described in fifty-seven characters. There is no process to interrogate and no capability to track churn on. The category is a spending bucket, not a requirement.

I'm recording that because it's a durable boundary, not a bug to fix later: **this dataset is good for *who won what, roughly* and useless for *what is actually happening*.** Every future idea gets tested against that line.

The provocation idea isn't dead, though — it just needed sources that carry intent and detail. Which is exactly what the plans and audit layers turned out to be.

## What's missing: convergence

Here's the honest state of the project.

There are now four signals about any given federal department. Each is useful alone. The real payoff is when they **converge** — when the Auditor General has flagged a department, *and* its own plan says it intends to modernize that same system, *and* the incumbent's contract expires in five months. That's about as strong a pre-RFP case as public data can produce, and any live tender from that department should be read in that light.

The intended shape is a department dossier: ask about IRCC and get back everything all four sources know, so a tender stops being an isolated notice and becomes *a tender from the department the AG flagged for processing backlogs, that plans to modernize case management, whose incumbent contract runs out in the spring.*

It isn't built yet, and I found out why before starting, which saved me a bad afternoon: **the four sources name departments differently.** The audits say "Immigration, Refugees and Citizenship." The contracts say "National Defence | Défense nationale." The plans say "Department of Citizenship and Immigration." A naive join returns nothing at all, silently.

So convergence needs a name-resolution layer underneath it first, and then the dossier on top — assembling the signals, not scoring them cleverly. The clever weighting is the over-build. Get the assembly working first.

## What I know is wrong with it

**It's slow.** The old pipeline answered in three seconds. A multi-step Claude session takes twenty to sixty. Fine for *what should I look at this week*. Not fine for anything with a UI.

**It's not reproducible.** Two runs of the same question won't follow identical reasoning. Acceptable for research, disqualifying for a product that needs to be auditable.

**It only works because the corpus is small.** With a deliberately narrow profile, a recent run went from 875 open tenders down to 10. At ten thousand, the markdown-file pattern strains badly. This is right-sized for one firm, not for a platform.

**Contract value extraction is crude by default.** The ingest grabs the first dollar figure in a description, which sometimes catches a bond amount or an insurance minimum instead. An optional flag replaces it with a model-based extraction pass, but that needs an API key, so the dumb version stays the default and the repo stays runnable with no credentials.

**The contracts data is directional, not exact.** It's unaudited, vendor names are only lightly normalized (near-variants may still count separately), reporting lags about a quarter, and contract amendments are aggregated per procurement family using the highest recorded value — which avoids double-counting but under-represents families that straddle the date window.

**Promoted tenders can drift.** Promoting copies the description into a markdown file. If CanadaBuys amends the notice afterwards, the copy doesn't know. Amendments are rare in practice.

**The tender documents themselves are off-limits.** They're hosted on commercial platforms — Ariba, MERX — behind account walls. This project deliberately doesn't scrape them. `scripts/probe_attachments.py` is the throwaway diagnostic that established that, kept in the repo because the negative result is part of the record.

## The profile

Everything filters through one file: `vault/profiles/my-company.md`. Its frontmatter drives the ingest:

```yaml
value_min: 250000
value_max: 5000000
competencies: [cloud, AWS, Azure, IT modernization, cybersecurity, DevOps]
exclude: [janitorial, landscaping, catering, food service]
min_days_until_close: 10
```

The profile shipped here is a representative IT-consulting firm rather than a real client, which keeps the repo self-contained — swap in your own and everything downstream retargets.

Competency matching is on whole words, not substrings, so "aws" matches Amazon Web Services but not "flaws" or "withdrawals". The full funnel — date, exclusions, competency, value range — prints on every ingest, so you can tune against the live distribution rather than guessing.

## Running it

```bash
git clone https://github.com/QuasiModalLabs/tender-vault.git
cd tender-vault
pip install -r requirements.txt

$EDITOR vault/profiles/my-company.md   # the filter reads from this
python scripts/ingest.py               # builds the tender corpus, ~2 min
```

Then either open Claude Code in the repo root and ask *"any good federal IT tenders for us this week?"*, or wire it into Claude Desktop as an MCP server.

The plans and audit databases ship prebuilt, so a fresh clone can query them immediately. The contracts layer is a one-time ~630MB download and is optional if you only want opportunity discovery.

**Full install, MCP configuration, and troubleshooting: [`docs/SETUP.md`](docs/SETUP.md).**

Fresh data arrives on its own — a GitHub Action re-runs the tender ingest every Monday and commits a markdown digest with a *new this week* section diffed against the previous run. The contracts and plans layers refresh quarterly. A month of digests is its own artifact: you can see the corpus shift.

## Files worth reading, in order

1. [`vault/CLAUDE.md`](vault/CLAUDE.md) — the agent's instructions. The most important design document in the repo; everything else is plumbing.
2. [`vault/profiles/my-company.md`](vault/profiles/my-company.md) — how user context is stored.
3. [`scripts/tender_tools.py`](scripts/tender_tools.py) — the retrieval layer, and the clean line between retrieval and reasoning.
4. [`scripts/plans_ingest.py`](scripts/plans_ingest.py) — the two-pole scoring technique, with the docstring explaining why the forward-looking field beats the retrospective one.
5. [`scripts/contracts_ingest.py`](scripts/contracts_ingest.py) — streaming filter over millions of rows into SQLite; the design notes are in the module docstring.
6. [`scripts/oag_ingest.py`](scripts/oag_ingest.py) — the audit pull, relevance scoring, and department tagging.
7. [`.github/workflows/weekly-ingest.yml`](.github/workflows/weekly-ingest.yml) — how data stays fresh without me remembering.

## What comes next

- **The department dossier**, on top of a name-resolution layer. The most important remaining piece — it's what reconnects all this intelligence back to actually spotting a bid.
- **A pre-mortem command.** For any tender under serious consideration: *assume we bid and lost, or won and regretted it — walk backwards and tell me why.* One adversarial pass against my own enthusiasm before committing. This is the surviving core of a multi-persona "steering committee" feature I cut mid-build; the personas changed tone without changing reasoning, but the skepticism they were reaching for is real.
- **A profile refinement loop.** Quarterly, read across everything watched, parked, and archived, and propose profile edits based on revealed preference. One structural catch to design around: the vault only knows about tenders that survived the filter, so it can improve precision but is blind to recall. It has to be paired with an audit that samples what the filter *rejected*.
- **Similarity drift.** Flag a new tender that closely resembles one archived as a loss.
- **Win/loss pattern mining**, once the archive is deep enough to say things like *we lose every tender that requires active SOC work*.

None of these need new infrastructure. That's mostly what the markdown-first design bought.

---

## Sources and licence

- [CanadaBuys tender notices](https://canadabuys.canada.ca/en/tender-opportunities) — active federal opportunities
- [Proactive Publication of Contracts](https://open.canada.ca/data/en/dataset/d8f85d91-7dec-4fd1-8055-483b77225d8b) — awarded federal contracts over $10K
- [GC InfoBase Departmental Plans / Results](https://open.canada.ca/data/en/dataset/b15ee8d7-2ac0-4656-8330-6c60d085cda8) — planned spending and forward-looking planning prose
- [Office of the Auditor General](https://open.canada.ca/data/en/organization/oag-bvg) — performance audits, via the open.canada.ca CKAN API

Contains information licensed under the [Open Government Licence – Canada](https://open.canada.ca/en/open-government-licence-canada). Code is MIT.

The derived contracts database is committed as a static file, so you can browse it with no server at all by pointing [Datasette Lite](https://lite.datasette.io/) at its raw GitHub URL.
