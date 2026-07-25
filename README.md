# Tender Vault

An experiment in building an agentic tender research assistant over the Canadian government tender corpus, using Claude as the orchestrator and an Obsidian vault as persistent memory.

## What this is

A few months ago I built a RAG pipeline for matching my company's capabilities to active Canadian government tenders. Hybrid search, metadata scoring, the whole pattern. It worked — but every query started from scratch, the ranking was wholly formula-driven, and the "strategic analysis" was a one-shot LLM call at the end. Eventually I realised the core problem with that shape:

> The system was solving *retrieval* really well. But tender research isn't a retrieval problem — it's a reasoning problem on top of retrieval.

This project is the rewrite. The retrieval layer is smaller (a Python module, ~300 lines). The reasoning layer is Claude itself, operating on an Obsidian vault. Most of the interesting design is in two files: `vault/CLAUDE.md` (the agent's instructions) and `scripts/tender_tools.py` (the tools it calls).

## Architecture

```
          ┌─────────────────────────┐
          │   Canada Buys CSV       │
          │   (open notices, ~850)  │
          └────────────┬────────────┘
                       │  python scripts/ingest.py
                       │  (weekly via GitHub Actions)
                       ▼
          ┌─────────────────────────┐
          │   Filter by profile     │  ← aggressive, profile-driven
          │   → ChromaDB (local)    │
          └────────────┬────────────┘
                       │
                       ▼
    ┌──────────────────┴──────────────────────────┐
    │                                             │
    │   Claude (Claude Code OR Claude Desktop)    │
    │                                             │
    │   reads: vault/CLAUDE.md (instructions)     │
    │          vault/profiles/my-company.md       │
    │          vault/tenders/watching/*.md        │
    │                                             │
    │   calls:  search, get_tender, find_similar, │
    │           list_watching, list_parked,       │
    │           promote, park, archive,           │
    │           contracts_intel                   │
    │                                             │
    └─────────────────────────────────────────────┘
```

**Option 3 hybrid storage.** Tenders live in two tiers. The ChromaDB corpus is the cold store — the full filtered set of opportunities. The vault (`vault/tenders/`) is the hot store, with three lifecycle states: `watching/` (actively considering), `parked/` (deferred but might revisit, with concrete trigger conditions), and `archived/` (decision was final). The hot tier accumulates notes, Claude's analyses, and the audit trail. The cold tier gets rebuilt from scratch by the weekly ingest.

The split matters because it matches how tender research actually works: most tenders are noise, a handful are worth attention, and the handful deserve persistent context.

## What makes this different from the RAG pipeline it replaces

**Retrieval → reasoning.** In the old system, the LLM got invoked once at the end to write a SWOT. In this one, Claude drives the loop: searches broadly, reads results, refines, cross-references against what's already in `watching/`, reads the full tender before recommending. Same retrieval tools underneath, different caller.

**Persistent state.** The old system started from zero on every query. This one remembers — literally, as markdown files. When I ask "any updates on the DND tender?" next week, Claude reads `tenders/watching/dnd-cloud-modernization.md` and sees the history.

**Debuggability through markdown.** Every promoted tender is a readable file. Every archived one has a reason appended. A year from now I can grep the vault for "lost to competitor" and see patterns. That's hard to do with rows in a database.

**The scoring formula is gone.** The old system had a MetadataScorer class with hand-tuned weights for value, timeline, complexity. It worked, but the weights were guesses. Now Claude does this reasoning directly: "this tender is in range but requires Secret clearance, which we don't have — skip." No formula to tune.

## Setup

```bash
git clone <this-repo>
cd tender-vault
pip install -r requirements.txt

# Edit your profile first — the ingest filter reads from its frontmatter
$EDITOR vault/profiles/my-company.md

# First ingest (downloads the open-notices CSV, builds embeddings, ~2 min)
python scripts/ingest.py
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

**Period-overlap windowing, not award date.** A contract is kept if its *delivery period* overlaps the last N years (`contracts_window_years` in the profile, default 3). Incumbency depends on when a contract is active, not when it was signed. A five-year contract awarded four years ago is today's incumbent, and an award-date filter would silently delete it, producing a confident "no recent incumbents" that is simply false. Where end dates are missing, it falls back to award date rather than dropping the row.

**Streaming ingest.** The source CSV is millions of rows. It's filtered chunk-by-chunk straight off the HTTP response; only matching rows are persisted. The full dataset never touches disk.

Known limitations, stated rather than hidden: the data is unaudited, vendor names are not normalized (so "IBM Canada Ltd." and "IBM CANADA LIMITED" count separately), and reporting lags by a quarter. Amendments share a procurement id and are aggregated per family using the highest recorded value, which avoids double-counting but means a family whose rows straddle the filter window is partially represented. Treat the output as directional intelligence.

The derived database (`data/contracts.db`) is committed to this repo and refreshed quarterly by GitHub Actions, along with auto-generated per-department summaries in `vault/intel/agencies/`. Since the SQLite file is a static asset, you can explore it in a browser with no server at all by pointing [Datasette Lite](https://lite.datasette.io/) at its raw GitHub URL.

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

The funnel — date, exclusions, competency match, value range — is printed on every ingest so you can tune criteria against the live distribution. With the default (deliberately narrow) profile, a recent run went 852 open tenders → 22. Literal substring matching on competencies is the big reducer; broaden the terms in the profile to widen the corpus.

## Files worth reading, in order

1. [`vault/CLAUDE.md`](vault/CLAUDE.md) — the agent's instructions. The most important design document in the repo.
2. [`vault/profiles/my-company.md`](vault/profiles/my-company.md) — how user context is stored.
3. [`scripts/tender_tools.py`](scripts/tender_tools.py) — the retrieval layer. Clean separation between retrieval (this file) and reasoning (Claude).
4. [`scripts/mcp_server.py`](scripts/mcp_server.py) — thin MCP wrapper around the same functions.
5. [`scripts/ingest.py`](scripts/ingest.py) — the filtering pipeline.
6. [`.github/workflows/weekly-ingest.yml`](.github/workflows/weekly-ingest.yml) — how fresh data flows in without me having to remember.

## Data sources and licence

- [CanadaBuys open tender notices](https://canadabuys.canada.ca/en/tender-opportunities) — active federal opportunities.
- [Proactive Publication of Contracts](https://open.canada.ca/data/en/dataset/d8f85d91-7dec-4fd1-8055-483b77225d8b) — awarded federal contracts over $10K.

Contains information licensed under the [Open Government Licence – Canada](https://open.canada.ca/en/open-government-licence-canada).

Note on attachments: tender documents themselves are hosted on third-party commercial platforms (Ariba, MERX) behind account walls. This project deliberately does not scrape them. `scripts/probe_attachments.py` is the throwaway diagnostic that established this, kept in the repo because the negative result is part of the design record.

## What I might build next

- **Pre-mortem command.** For any tender under serious consideration: "assume we bid and lost, or won and regretted it — walk backwards and tell me why." One skeptical reasoning pass, defined in `CLAUDE.md`, that applies adversarial pressure to my own enthusiasm before committing. This is the surviving core of a multi-persona "steering committee" feature that was cut mid-build: personas change tone, not reasoning, but the skepticism they were reaching for is real and doesn't need a cast of characters.
- **Profile refinement loop.** Quarterly, Claude reads across `watching/`, `parked/`, and `archived/` and proposes edits to the profile based on revealed preferences: which competencies actually led to promotions, which archive reasons keep repeating, which terms have matched nothing anyone cared about. The user approves or rejects; ingest re-runs with the refined filter. One structural caveat this must design around: the vault only knows about tenders that survived the filter, so it can tune precision but is blind to recall. The loop therefore pairs with a periodic audit that samples tenders the filter *rejected* and checks them for blind spots — precision from the vault, recall from the audit.
- **Similarity drift tracking.** When a new tender closely matches one archived as a loss, flag it — probably shouldn't pursue again without a plan for what's different.
- **Win/loss pattern mining.** Once `archived/` has enough entries, Claude can read across them to surface patterns ("we lose on every tender requiring active SOC work").
- ~~**LLM-based field extraction.**~~ Done — `ingest.py --extract-values` runs a per-tender LLM extraction pass with regex fallback.

These are all natural extensions of the markdown-first design. None require new infrastructure.
