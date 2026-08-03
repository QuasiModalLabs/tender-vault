# Tender Vault

An experiment in building an agentic research assistant over Canadian federal procurement data — using Claude as the orchestrator and an Obsidian vault as persistent memory. It pairs *opportunity* discovery (open tenders worth pursuing) with a stack of *pre-RFP intelligence* — who wins this work, who's planning to modernize, and who the Auditor General has flagged — so a tender stops being an isolated notice and becomes a lead with context.

## What this is

A few months ago I built a RAG pipeline for matching my company's capabilities to active Canadian government tenders. Hybrid search, metadata scoring, the whole pattern. It worked — but every query started from scratch, the ranking was wholly formula-driven, and the "strategic analysis" was a one-shot LLM call at the end. Eventually I realised the core problem with that shape:

> The system was solving *retrieval* really well. But tender research isn't a retrieval problem — it's a reasoning problem on top of retrieval.

This project is the rewrite, and then some. The retrieval layer is smaller (a Python module, ~300 lines) and the reasoning layer is Claude itself, operating on an Obsidian vault. Then it kept growing data sources, each answering a different question about a federal department:

- **Tender notices** — what work is being *asked for* right now (the live opportunity feed).
- **Proactive-disclosure contracts** — who actually *won* past work, at what scale (incumbents, market shape, expiring contracts).
- **Departmental Plans** — what a department *intends* to modernize, in its own forward-looking words (pre-RFP intent).
- **Auditor General audits** — what an independent authority has *publicly found* a department failing at (the most citable pre-RFP signal, and the scrutiny that forces a procurement).

Four datasets, storage engines matched to their shapes (a vector DB for prose, SQLite for structured records), one agent reasoning across all of them. The throughline is a single question asked earlier and earlier in the procurement lifecycle: from "an RFP is open now" back through "an RFP is predictably coming" to "here are the conditions that will produce one." Most of the interesting design is in `vault/CLAUDE.md` (the agent's instructions) and `scripts/tender_tools.py` (the tools it calls).

## Architecture

```
  OPPORTUNITIES          OUTCOMES              INTENT                 SCRUTINY
  (what's asked)         (who won)          (what's planned)      (what's flagged)
 ┌──────────────┐    ┌──────────────┐    ┌──────────────┐    ┌──────────────┐
 │ Canada Buys  │    │  Proactive   │    │  GC InfoBase │    │  Auditor Gen │
 │ CSV (~850)   │    │  Contracts   │    │  Dept Plans  │    │  audits via  │
 │              │    │  (~1.3M rows)│    │  (CSV, live) │    │  CKAN API    │
 └──────┬───────┘    └──────┬───────┘    └──────┬───────┘    └──────┬───────┘
        │ ingest.py         │ contracts_        │ plans_            │ oag_
        │ (weekly)          │ ingest.py         │ ingest.py         │ ingest.py
        │                   │ (quarterly)       │ (quarterly)       │
        ▼                   ▼                   ▼                   ▼
 ┌──────────────┐    ┌──────────────┐    ┌──────────────┐    ┌──────────────┐
 │ profile      │    │ category +   │    │ two-pole     │    │ two-pole     │
 │ filter →     │    │ recency →    │    │ semantic     │    │ semantic     │
 │ ChromaDB     │    │ SQLite       │    │ scoring →    │    │ scoring →    │
 │ (local)      │    │ (committed)  │    │ SQLite       │    │ SQLite       │
 └──────┬───────┘    └──────┬───────┘    └──────┬───────┘    └──────┬───────┘
        │                   │                   │                   │
        └─────────┬─────────┴─────────┬─────────┴─────────┬─────────┘
                  ▼                   ▼                   ▼
    ┌────────────────────────────────────────────────────────────┐
    │   Claude (Claude Code OR Claude Desktop)                    │
    │                                                            │
    │   reads: vault/CLAUDE.md, profiles/my-company.md,          │
    │          tenders/watching/*.md, intel/agencies/*.md        │
    │                                                            │
    │   calls:  search, get_tender, find_similar, list_watching, │
    │           list_parked, promote, park, archive,             │
    │           contracts_intel, expiring_contracts,             │
    │           program_signals, oag_signals                     │
    └────────────────────────────────────────────────────────────┘
```

Storage engines match data shapes. Tender notices are prose, so they go into a **vector database** (ChromaDB) where semantic search earns its keep. Contract awards are structured records answering analytical questions ("top vendors by value"), so **SQLite** where SQL answers exactly. Plans and OAG audits are prose-with-structure: their signal fields are scored once at ingest by **two-pole semantic theming** (embed the text, score it toward a target theme and away from a noise theme, using the same local model as the tender corpus — zero Claude tokens), and the results land in **SQLite** for cheap ranked queries. Claude reasons across all four.

**Option 3 hybrid storage.** Tenders live in two tiers. The ChromaDB corpus is the cold store — the full filtered set of opportunities. The vault (`vault/tenders/`) is the hot store, with three lifecycle states: `watching/` (actively considering), `parked/` (deferred but might revisit, with concrete trigger conditions), and `archived/` (decision was final). The hot tier accumulates notes, Claude's analyses, and the audit trail. The cold tier gets rebuilt from scratch by the weekly ingest.

The split matters because it matches how tender research actually works: most tenders are noise, a handful are worth attention, and the handful deserve persistent context.

## What makes this different from the RAG pipeline it replaces

**Retrieval → reasoning.** In the old system, the LLM got invoked once at the end to write a SWOT. In this one, Claude drives the loop: searches broadly, reads results, refines, cross-references against what's already in `watching/`, reads the full tender before recommending. Same retrieval tools underneath, different caller.

**Persistent state.** The old system started from zero on every query. This one remembers — literally, as markdown files. When I ask "any updates on the DND tender?" next week, Claude reads `tenders/watching/dnd-cloud-modernization.md` and sees the history.

**Debuggability through markdown.** Every promoted tender is a readable file. Every archived one has a reason appended. A year from now I can grep the vault for "lost to competitor" and see patterns. That's hard to do with rows in a database.

**The scoring formula is gone.** The old system had a MetadataScorer class with hand-tuned weights for value, timeline, complexity. It worked, but the weights were guesses. Now Claude does this reasoning directly: "this tender is in range but requires Secret clearance, which we don't have — skip." No formula to tune.

**Outcomes, not just opportunities.** The original tool only saw what was being *asked for*. It now also ingests awarded-contract data, so Claude can answer "who's the incumbent here?" and "what does this department typically pay for this work?" before I decide whether to pursue a tender. Opportunity discovery and competitive intelligence in one loop — see [Outcome intelligence](#outcome-intelligence-who-actually-won) below.

## Setup

```bash
git clone <this-repo>
cd tender-vault
pip install -r requirements.txt

# Edit your profile first — the ingest filter reads from its frontmatter
$EDITOR vault/profiles/my-company.md

# First ingest (downloads the open-notices CSV, builds embeddings, ~2 min)
python scripts/ingest.py

# Optional: build the outcome-intelligence layer. Downloads a large (~630MB)
# contracts dataset once, filters it to your profile's categories, ~5-10 min.
# Skip this if you only want tender/opportunity discovery.
python scripts/contracts_ingest.py

# Optional: the pre-RFP intelligence layers. Both are small and both ship a
# committed, prebuilt DB — so a fresh clone already has them; you only re-run
# these to refresh. They score prose with the local embedding model at ingest.
python scripts/plans_ingest.py     # departmental-plans intent signal (~1 min)
python scripts/oag_ingest.py       # Auditor General scrutiny signal (~1 min)
```

After that, you have two ways to use it.

### Using Claude Code (terminal)

```bash
# In the repo root:
claude
```

Claude Code picks up `vault/CLAUDE.md` automatically and uses `scripts/tender_tools.py` as a subprocess. Start with a natural-language request: *"Any good federal IT tenders for us this week?"*

### Using Claude Desktop (via MCP)

**Find the right config file first** — this varies by install:

- **macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows (direct installer):** `%APPDATA%\Claude\claude_desktop_config.json`
- **Windows (Microsoft Store version):** the app sandboxes its config elsewhere. Don't guess the path — in Claude Desktop open **Settings → Developer → Edit Config**, which opens the file the app actually reads.

Add an `mcpServers` block. Point `command` at the **virtual environment's Python** (not system Python, or the server won't find its dependencies), with absolute paths:

```json
{
  "mcpServers": {
    "tender-vault": {
      "command": "C:\\code\\tender-vault\\.venv\\Scripts\\python.exe",
      "args": ["C:\\code\\tender-vault\\scripts\\mcp_server.py"]
    }
  }
}
```

(macOS/Linux: `command` is `/path/to/tender-vault/.venv/bin/python`.) If the config file already has content, merge `mcpServers` in as a sibling key — don't paste a second top-level `{}` object; the JSON must have exactly one root.

**Restart properly:** quitting means killing the tray/background process (Windows: system tray → right-click → Quit, or Task Manager), not just closing the window. The config is only read at startup.

**Verify:** in a chat, open the `+` menu → **Connectors** — `tender-vault` should be listed with a toggle. (Do **not** use "Add custom connector" — that dialog is for remote MCP servers with URLs, not local ones.) Test with *"list the tenders in my watching folder."*

Two behaviors to expect: the first ChromaDB-backed call of a session (search/get/similar) takes 30–90 seconds while the embedding model loads — subsequent calls are fast. And Claude Desktop doesn't auto-read `CLAUDE.md` the way Claude Code does — paste its contents into a Claude Project's instructions and do tender work in that project.

## Weekly auto-ingest via GitHub Actions

The `.github/workflows/weekly-ingest.yml` workflow runs every Monday. It:

1. Downloads the latest Canada Buys CSV
2. Re-runs the filter pipeline
3. Writes a markdown digest to `vault/digests/YYYY-MM-DD.md`, including a **"New this week"** section diffed against the previous run's corpus snapshot (`vault/digests/corpus-latest.txt`)
4. Commits the digest and updated snapshot (not `chroma_db/`)

Because the digest is generated on the Actions runner, it can reference tenders your local ChromaDB hasn't seen yet. After pulling a new digest, re-run `python scripts/ingest.py` to sync your local corpus.

There's a small test suite for the file-lifecycle logic (promote/park/archive) — the code most likely to corrupt vault files during a refactor: `python tests/test_lifecycle.py`.

ChromaDB is deliberately not committed — it's a 30-80MB binary blob and committing it weekly bloats the repo's history. Anyone cloning runs `python scripts/ingest.py` once locally.

The digest that *does* get committed is the portfolio artifact: a month of digests shows corpus trends at a glance.

## Outcome intelligence: who actually won

Tender notices tell you what's being *asked for*. They say nothing about who wins, at what price, or which departments actually buy in your space. That second half comes from a different dataset: Canada's [Proactive Publication of Contracts](https://open.canada.ca/data/en/dataset/d8f85d91-7dec-4fd1-8055-483b77225d8b), every federal contract over $10K.

```bash
python scripts/contracts_ingest.py          # quarterly; also runs via Actions
python scripts/tender_tools.py contracts-intel "cloud migration"
```

Three design decisions worth explaining:

**SQLite, not vectors.** Tender notices are prose, so embeddings earn their keep. Contract awards are records, and the questions are analytical ("top vendors by total value since 2022"). Semantic search answers that badly; SQL answers it exactly. Two retrieval systems, each matched to its data shape.

**Recency windowing on award date OR period end.** A contract is kept if `max(award_date, period_end)` falls within the last N years (`contracts_window_years` in the profile, default 3). This captures two signals at once: recent *awards* (who's winning work now — the market picture) and still-*active* older contracts (live incumbents). A strict period-overlap filter was tried first but the dataset skews heavily toward completed historical contracts, so "still active today" left almost nothing; a recently-awarded contract that has already ended is still exactly the competitive signal this layer exists to surface.

**Category matching, not prose keywords.** Contracts describe work as standardized procurement categories ("Information technology and telecommunications consultants"), not free prose, so the contracts filter matches a separate `contracts_categories` list in the profile (case-insensitive substring) rather than the tender competencies. The profile ships a commented reference catalog of the dataset's real category vocabulary, organized by sector, so adapting to a different company is a copy-paste.

**Streaming ingest.** The source CSV is millions of rows. It's filtered chunk-by-chunk straight off the HTTP response; only matching rows are persisted. The full dataset never touches disk.

Known limitations, stated rather than hidden: the data is unaudited, vendor names are lightly normalized (corporate suffixes and punctuation stripped to collapse obvious duplicates, but not fuzzy-matched — so near-variants may still count separately), and reporting lags by a quarter. Amendments share a procurement id and are aggregated per family using the highest recorded value, which avoids double-counting but means a family whose rows straddle the filter window is partially represented. Treat the output as directional intelligence.

The derived database (`data/contracts.db`) is committed to this repo and refreshed quarterly by GitHub Actions, along with auto-generated per-department summaries in `vault/intel/agencies/`. Since the SQLite file is a static asset, you can explore it in a browser with no server at all by pointing [Datasette Lite](https://lite.datasette.io/) at its raw GitHub URL.

## Pre-RFP intelligence: intent and scrutiny

Contracts tell you who *won* work. Two more sources tell you what's coming *before* the RFP exists — the point in the lifecycle where the requirement can still be shaped.

**Departmental Plans — stated intent.** Treasury Board's [GC InfoBase expenditures-by-program data](https://open.canada.ca/data/en/dataset/b15ee8d7-2ac0-4656-8330-6c60d085cda8) carries a forward-looking `planning_explanation` field where departments describe, in their own words, what they plan to spend on ("investment in 2025-26 for improvements to the case management system"). `plans_ingest.py` scores each program's planning prose by **two-pole semantic theming** — toward modernization intent, away from routine-operations noise — and `program-signals` surfaces the IT-modernization plans ranked by score.

```bash
python scripts/plans_ingest.py --show-extremes     # --show-extremes prints top/bottom scored, a tuning aid
python scripts/tender_tools.py program-signals --limit 15
```

The design story is worth knowing because it shows the reasoning-over-retrieval loop working: v1 scored the *retrospective* `variance_explanation` field, but a Claude Code test found it was surfacing 6-year-old archaeology (that field died after 2019) and scoring real IT modernizations *negative*. The fix — pivoting to the forward-looking `planning_explanation`, and later finding the *live* DataStore resource that runs through 2025-26 after the first CSV appeared frozen at 2021 — turned the feature from historical to current. Ranking is by intent score; where a program has multiple scored years, the trail is surfaced as context. Honest limitation: semantic averaging struggles when a modernization is buried in budget-variance phrasing, so it can score low — but Claude reads the full prose and compensates. The tool surfaces candidates; Claude judges. Auto-refreshes quarterly (`plans-refresh.yml`); `data/plans.db` is committed for prepopulation.

**Auditor General audits — independent scrutiny.** The [OAG's performance audits](https://open.canada.ca/data/en/organization/oag-bvg), pulled via the open.canada.ca CKAN API (read-only, no key), are the most citable pre-RFP signal: an independent authority publicly stating a department is failing at something. `oag_ingest.py` classifies each document (performance audit vs. committee hearing), scores IT-relevance by the same two-pole theming, tags the department, and builds `oag.db`; `oag-signals` surfaces the IT/systems/cyber audits.

```bash
python scripts/oag_ingest.py --show-extremes
python scripts/tender_tools.py oag-signals --department "shared services"
```

Top hits are real — "Modernizing Information Technology Systems," "Combatting Cybercrime," "Cybersecurity in the Cloud" — and Shared Services Canada recurs, a live insight: the federal IT department under sustained AG scrutiny on exactly this work. Two v2 improvements are noted in the roadmap (department extraction from the report body, since title/description alone leaves ~1/3 untagged; and a refresh workflow). The department tag is deliberate: it's the join key for the convergence view (see roadmap), where OAG + plans + an expiring contract pointing at one department is the strongest possible pre-RFP case.

## Tradeoffs I'm aware of

**Speed.** The old system answered in ~3 seconds. A multi-step Claude session takes 20-60 seconds. For "which tenders should I look at today?" that's fine. For anything UI-driven it wouldn't be.

**Determinism.** Two runs of the same query won't produce identical reasoning paths. That's okay for research, bad for a product that needs to be reproducible.

**Scale.** This works because the corpus is small after filtering (tens to low hundreds). At 10,000 the vault pattern starts to strain (too many files, context windows get tight). For a mid-size consulting firm it's the right size.

**Value extraction defaults to a crude regex.** By default `ingest.py` grabs the first dollar amount in the description — often right, sometimes it catches a bond amount or insurance minimum instead. Running `python scripts/ingest.py --extract-values` replaces this with an LLM extraction pass (Anthropic API, `ANTHROPIC_API_KEY` required, a few cents per ingest) that reads the description and pulls the actual contract value. The regex remains the default and the per-tender fallback so the repo stays runnable with zero credentials.

**Cold/hot tier drift.** Promoting a tender copies its description into a markdown file. If the description in Canada Buys changes later, the promoted file won't auto-update. In practice amendments are rare, but worth knowing.

## Corpus filtering

The profile (`vault/profiles/my-company.md`) has YAML frontmatter that drives `ingest.py`:

```yaml
value_min: 250000
value_max: 5000000
competencies: [cloud, AWS, Azure, IT modernization, cybersecurity, DevOps, data engineering]
exclude: [janitorial, landscaping, catering, food service]
min_days_until_close: 10
```

The funnel — date, exclusions, competency match, value range — is printed on every ingest so you can tune criteria against the live distribution. With the default (deliberately narrow) profile, a recent run went 875 open tenders → 10. Word-boundary competency matching is the big reducer (word-boundary rather than substring, so "aws" matches Amazon Web Services but not "flaws" or "withdrawals"); broaden the terms in the profile to widen the corpus.

## Files worth reading, in order

1. [`vault/CLAUDE.md`](vault/CLAUDE.md) — the agent's instructions. The most important design document in the repo.
2. [`vault/profiles/my-company.md`](vault/profiles/my-company.md) — how user context is stored.
3. [`scripts/tender_tools.py`](scripts/tender_tools.py) — the retrieval layer. Clean separation between retrieval (this file) and reasoning (Claude).
4. [`scripts/mcp_server.py`](scripts/mcp_server.py) — thin MCP wrapper around the same functions.
5. [`scripts/ingest.py`](scripts/ingest.py) — the tender filtering pipeline.
6. [`scripts/contracts_ingest.py`](scripts/contracts_ingest.py) — the outcome-intelligence pipeline: streaming filter into SQLite, with the design notes (category matching, recency windowing, vendor normalization) in the module docstring.
7. [`scripts/plans_ingest.py`](scripts/plans_ingest.py) — the intent-signal pipeline: two-pole semantic theming over departmental planning prose. The docstring explains the technique and why the planning field beats the variance field.
8. [`scripts/oag_ingest.py`](scripts/oag_ingest.py) — the scrutiny-signal pipeline: CKAN-API pull of Auditor General audits, IT-relevance scoring, department tagging for convergence.
9. [`.github/workflows/weekly-ingest.yml`](.github/workflows/weekly-ingest.yml) — how fresh data flows in without me having to remember.

## Data sources and licence

- [CanadaBuys open tender notices](https://canadabuys.canada.ca/en/tender-opportunities) — active federal opportunities.
- [Proactive Publication of Contracts](https://open.canada.ca/data/en/dataset/d8f85d91-7dec-4fd1-8055-483b77225d8b) — awarded federal contracts over $10K.
- [GC InfoBase Departmental Plans / Results](https://open.canada.ca/data/en/dataset/b15ee8d7-2ac0-4656-8330-6c60d085cda8) — per-program planned spending and forward-looking planning prose (live DataStore resource, through 2025-26).
- [Office of the Auditor General audits](https://open.canada.ca/data/en/organization/oag-bvg) — performance audits and committee-hearing materials, via the open.canada.ca CKAN API.

Contains information licensed under the [Open Government Licence – Canada](https://open.canada.ca/en/open-government-licence-canada).

Note on attachments: tender documents themselves are hosted on third-party commercial platforms (Ariba, MERX) behind account walls. This project deliberately does not scrape them. `scripts/probe_attachments.py` is the throwaway diagnostic that established this, kept in the repo because the negative result is part of the design record.

## Built since the two-source version

Three signal layers shipped after the original tenders + contracts system, each recorded honestly — what worked, what was cut, what's still rough.

- **Opportunity-shaping — partially built, and a lesson.** The goal: get in *before* the RFP, when the requirement can still be shaped, rather than reacting to procurements already decided. What shipped: `expiring-contracts`, which surfaces contracts expiring in a 6-24 month window (near-certain future re-procurements) with incumbent, department, value, and expiry — tunable via `expiry_min_value` in the profile. That part works and is a real lead list. What got cut, and why it's worth recording: I also tried mining the contracts data for *re-compete churn* (departments cycling through vendors on the same capability) and for *process-improvement provocations* (reading how work is done and proposing better). Both failed on the same wall — **the contracts dataset describes work as coarse procurement categories ("Information technology and telecommunications consultants"), never how the work is actually done.** A $585M contract is described in 57 characters. So there's no process to interrogate and no capability-level churn to detect; the category is a spending bucket, not a requirement. The durable lesson: this dataset is good for "who won what, roughly" (incumbents, expiries, market shape) and bad for "what specifically is happening." Test future ideas against that line. The provocation vision isn't dead — it just needed a data source that carries intent and detail, which is exactly what the next two layers provide.
- **Departmental Plans / Results intent signal — SHIPPED.** Built and deployed: `plans_ingest.py` scores each program's forward-looking `planning_explanation` by two-pole semantic theming (modernization intent vs. routine noise), `program-signals` surfaces IT-modernization intent ranked by score, cross-referenced against contracts. Validated in Claude Code — proved the core hypothesis empirically (a 2019 stated intent became a real 2021 contract). Pivoted from the retrospective `variance_explanation` (dead after 2019) to the forward-looking `planning_explanation` after Claude's own critique found the variance ranking surfaced stale archaeology. Then found the *live* GC InfoBase DataStore resource (2018 through 2025-26) after the original CSV appeared frozen at 2021 — the feature is current, not historical. Auto-refreshes quarterly via `plans-refresh.yml`, `plans.db` committed for prepopulation. Honest limitation recorded: semantic averaging struggles on mixed-content sentences (a modernization buried in budget-variance phrasing scores low), but Claude reading the full prose compensates — the tool surfaces candidates, Claude judges. (Details in the [Pre-RFP intelligence](#pre-rfp-intelligence-intent-and-scrutiny) section.)
- **OAG signals — SHIPPED (v1).** `oag_ingest.py` pulls Office of the Auditor General audits via the open.canada.ca CKAN API (no key), classifies performance-audit vs. committee-hearing, semantically scores IT-relevance, tags the department, builds `oag.db`; `oag-signals` surfaces IT/systems/cyber audits filterable by department/doc-type/year. Validated: top hits are real (SSC "Modernizing IT Systems", RCMP "Combatting Cybercrime", SSC cybersecurity/cloud/procurement) — and SSC appears repeatedly, a live insight (the federal IT department under sustained AG scrutiny). Two v2 improvements noted: (1) **department extraction from the report body** — title/description extraction leaves ~1/3 of audits with no department (`?`), which weakens the convergence join; the fix is fetching the report HTML and parsing the department from its standardized header. (2) No refresh workflow yet — OAG publishes a few dozen audits/year irregularly; `oag.db` will go stale without one. Both are natural to fold into the convergence work.

## What I might build next

- **The convergence view — the standout next task, and the thing that ties everything back to RFPs.** The project now has four signals on federal departments: contracts (who holds their IT work, expiring when), plans (what they intend to modernize), OAG (what an independent authority found them failing at), and the live tender feed (what's open now). Each is useful alone; the real payoff is *convergence* — when OAG + plans + an expiring contract all point at the same department+capability, that's the strongest possible pre-RFP case, and any live tender from that department should rank higher because of it. The intended shape is a "department dossier": given a department, pull together everything all four sources know, so a live tender stops being an isolated notice and becomes "a tender from IRCC — who the AG flagged for processing backlogs, who plans to modernize case management, whose incumbent contract expires in five months." That is the answer to "am I losing track of RFPs" — convergence is what reconnects the intelligence layer to actually spotting and winning the bid. **The hidden dependency, discovered before building (so it doesn't fail silently): the sources name departments differently** — OAG "Immigration, Refugees and Citizenship", contracts bilingual pipe-separated "... | Défense nationale", plans "Department of Citizenship and Immigration". A naive join returns nothing. So convergence must be built on a *department-normalization / alias layer first* (resolve all naming conventions to one key), then the dossier on top. Start simple — a dossier that assembles the signals, not a clever weighted-scoring engine (that's the over-build; design it once the assembly works). This deserves a fresh head: it's the most important integration piece in the system, and building the name-matching tired means building it twice.
- **Pre-mortem command.** For any tender under serious consideration: "assume we bid and lost, or won and regretted it — walk backwards and tell me why." One skeptical reasoning pass, defined in `CLAUDE.md`, that applies adversarial pressure to my own enthusiasm before committing. This is the surviving core of a multi-persona "steering committee" feature that was cut mid-build: personas change tone, not reasoning, but the skepticism they were reaching for is real and doesn't need a cast of characters.
- **Profile refinement loop.** Quarterly, Claude reads across `watching/`, `parked/`, and `archived/` and proposes edits to the profile based on revealed preferences: which competencies actually led to promotions, which archive reasons keep repeating, which terms have matched nothing anyone cared about. The user approves or rejects; ingest re-runs with the refined filter. One structural caveat this must design around: the vault only knows about tenders that survived the filter, so it can tune precision but is blind to recall. The loop therefore pairs with a periodic audit that samples tenders the filter *rejected* and checks them for blind spots — precision from the vault, recall from the audit.
- **Similarity drift tracking.** When a new tender closely matches one archived as a loss, flag it — probably shouldn't pursue again without a plan for what's different.
- **Win/loss pattern mining.** Once `archived/` has enough entries, Claude can read across them to surface patterns ("we lose on every tender requiring active SOC work").
- ~~**LLM-based field extraction.**~~ Done — `ingest.py --extract-values` runs a per-tender LLM extraction pass with regex fallback.

These are all natural extensions of the markdown-first design. None require new infrastructure.
