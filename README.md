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
          │   (~2,800 active)       │
          └────────────┬────────────┘
                       │  python scripts/ingest.py
                       │  (weekly via GitHub Actions)
                       ▼
          ┌─────────────────────────┐
          │   Filter by profile     │  ← aggressive: 2,800 → ~200
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
    │           promote, park, archive            │
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

# First ingest (downloads ~80MB CSV, builds embeddings, takes ~2 min)
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

Edit your Claude Desktop config:
- **macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows:** `%APPDATA%\Claude\claude_desktop_config.json`

Add this (using the **absolute** path to your clone):

```json
{
  "mcpServers": {
    "tender-vault": {
      "command": "python",
      "args": ["/absolute/path/to/tender-vault/scripts/mcp_server.py"]
    }
  }
}
```

Quit Claude Desktop completely and reopen it. A hammer icon appears in the input box — click it to verify `search`, `get_tender`, `find_similar`, `list_watching`, `list_parked`, `promote`, `park`, `archive` are all there.

One caveat: Claude Desktop doesn't auto-read `CLAUDE.md` the way Claude Code does. When starting a conversation, either paste the contents of `vault/CLAUDE.md` into a project, or mention it: *"First, read vault/CLAUDE.md for how to work with this repo."*

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

## Tradeoffs I'm aware of

**Speed.** The old system answered in ~3 seconds. A multi-step Claude session takes 20-60 seconds. For "which tenders should I look at today?" that's fine. For anything UI-driven it wouldn't be.

**Determinism.** Two runs of the same query won't produce identical reasoning paths. That's okay for research, bad for a product that needs to be reproducible.

**Scale.** This works because the corpus is ~200 tenders after filtering. At 10,000 the vault pattern starts to strain (too many files, context windows get tight). For a mid-size consulting firm it's the right size.

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

Going from 2,800 active tenders to ~200 happens in four filters: date, exclusions, competency match, value range. The funnel is printed when you run ingest so you can tune the criteria against your actual distribution.

## Files worth reading, in order

1. [`vault/CLAUDE.md`](vault/CLAUDE.md) — the agent's instructions. The most important design document in the repo.
2. [`vault/profiles/my-company.md`](vault/profiles/my-company.md) — how user context is stored.
3. [`scripts/tender_tools.py`](scripts/tender_tools.py) — the retrieval layer. Clean separation between retrieval (this file) and reasoning (Claude).
4. [`scripts/mcp_server.py`](scripts/mcp_server.py) — thin MCP wrapper around the same functions.
5. [`scripts/ingest.py`](scripts/ingest.py) — the filtering pipeline.
6. [`.github/workflows/weekly-ingest.yml`](.github/workflows/weekly-ingest.yml) — how fresh data flows in without me having to remember.

## What I might build next

- **Similarity drift tracking.** When a new tender closely matches one archived as a loss, flag it — probably shouldn't pursue again without a plan for what's different.
- **Win/loss pattern mining.** Once `archived/` has enough entries, Claude can read across them to surface patterns ("we lose on every tender requiring active SOC work").
- ~~**LLM-based field extraction.**~~ Done — `ingest.py --extract-values` runs a per-tender LLM extraction pass with regex fallback.

These are all natural extensions of the markdown-first design. None require new infrastructure.
