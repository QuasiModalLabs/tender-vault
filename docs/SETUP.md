# Setup

Full install, configuration, and troubleshooting. For what the project *is*, see the [README](../README.md).

## Install

```bash
git clone https://github.com/QuasiModalLabs/tender-vault.git
cd tender-vault
pip install -r requirements.txt
```

A virtual environment is strongly recommended — the MCP configuration below depends on one.

```bash
python -m venv .venv
# Windows:  .venv\Scripts\activate
# macOS/Linux:  source .venv/bin/activate
pip install -r requirements.txt
```

## Build the data layers

**Edit your profile first.** The ingest filter reads from its frontmatter, so running the ingest before editing it will build a corpus against the shipped example profile.

```bash
$EDITOR vault/profiles/my-company.md
```

### 1. Tenders — required

```bash
python scripts/ingest.py
```

Downloads the current CanadaBuys open-notices CSV, applies the profile filter, and builds embeddings into a local ChromaDB store. Takes about two minutes. The funnel counts print as it runs, so you can see how many tenders each filter stage removed and tune accordingly.

Optional flag:

```bash
python scripts/ingest.py --extract-values
```

Replaces the default regex value extraction (first dollar amount in the description) with a per-tender model extraction pass. Requires `ANTHROPIC_API_KEY` and costs a few cents per ingest. The regex remains the default and the per-tender fallback, so the repo runs with zero credentials.

### 2. Contracts — optional, large

```bash
python scripts/contracts_ingest.py
```

Downloads the ~630MB proactive-disclosure contracts dataset once and filters it to your profile's categories. Five to ten minutes. Skip this if you only want opportunity discovery.

The source CSV is millions of rows and is filtered chunk-by-chunk straight off the HTTP response — only matching rows are ever persisted, and the full dataset never touches disk.

**Note on category matching.** Contracts describe work as standardized procurement categories, not free prose, so this filter reads a separate `contracts_categories` list in the profile (case-insensitive substring) rather than the tender competencies. The profile ships a commented reference catalog of the dataset's real category vocabulary, organized by sector, so adapting to a different company is a copy-paste.

**Note on the recency window.** A contract is kept if `max(award_date, period_end)` falls within the last N years (`contracts_window_years`, default 3). This captures recent awards (who is winning work now) and still-active older contracts (live incumbents) in one pass. A strict period-overlap filter was tried first, but the dataset skews heavily toward completed historical contracts, so "active today" left almost nothing — and a recently-awarded contract that has already ended is still exactly the competitive signal this layer exists to surface.

### 3. Plans and audits — optional, small, prebuilt

```bash
python scripts/plans_ingest.py    # ~1 min
python scripts/oag_ingest.py      # ~1 min
```

Both ship a committed, prebuilt database, so a fresh clone already has them. Re-run only to refresh. Both score prose with the local embedding model at ingest — no API key, no model tokens.

Add `--show-extremes` to either to print the top and bottom scored records. It's a tuning aid: if the top of the list looks wrong, the theme poles need adjusting.

## Using it with Claude Code

```bash
cd tender-vault
claude
```

Claude Code picks up `vault/CLAUDE.md` automatically and calls `scripts/tender_tools.py` as a subprocess. Start with a natural-language request:

> Any good federal IT tenders for us this week?

## Using it with Claude Desktop (MCP)

### Find the right config file

This varies by install, and guessing wastes time:

- **macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows (direct installer):** `%APPDATA%\Claude\claude_desktop_config.json`
- **Windows (Microsoft Store version):** the app sandboxes its config somewhere else entirely. Don't guess — open **Settings → Developer → Edit Config**, which opens the file the app actually reads.

### Add the server

Point `command` at the **virtual environment's Python**, not system Python — otherwise the server won't find its dependencies. Use absolute paths.

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

On macOS/Linux, `command` is `/path/to/tender-vault/.venv/bin/python`.

If the config file already has content, merge `mcpServers` in as a sibling key. Don't paste a second top-level `{}` object — the JSON must have exactly one root.

### Restart properly

Quitting means killing the tray or background process, not just closing the window: on Windows, right-click the system tray icon and choose Quit, or end it in Task Manager. The config is only read at startup.

### Verify

In a chat, open the `+` menu → **Connectors**. `tender-vault` should be listed with a toggle.

Do **not** use "Add custom connector" — that dialog is for remote MCP servers with URLs, not local ones.

Test with:

> List the tenders in my watching folder.

### Two behaviours to expect

**First call is slow.** The first ChromaDB-backed call of a session (search, get, find-similar) takes 30–90 seconds while the embedding model loads. Everything after that is fast.

**Claude Desktop doesn't auto-read `CLAUDE.md`** the way Claude Code does. Paste its contents into a Claude Project's instructions and do tender work inside that project.

## Automatic refresh

`.github/workflows/weekly-ingest.yml` runs every Monday:

1. Downloads the latest CanadaBuys CSV
2. Re-runs the filter pipeline
3. Writes a digest to `vault/digests/YYYY-MM-DD.md`, including a **New this week** section diffed against the previous run's corpus snapshot (`vault/digests/corpus-latest.txt`)
4. Commits the digest and the updated snapshot — but not `chroma_db/`

Because the digest is generated on the Actions runner, it can reference tenders your local corpus hasn't seen. After pulling a new digest, re-run `python scripts/ingest.py` to sync locally.

The contracts and plans layers refresh quarterly on their own workflows, along with auto-generated per-department summaries in `vault/intel/agencies/`. The audit layer has no refresh workflow yet — the OAG publishes a few dozen audits a year, irregularly, so `oag.db` will go stale without one.

**Why ChromaDB isn't committed:** it's a 30–80MB binary blob, and committing it weekly would bloat the repository's history for no benefit. Anyone cloning runs the ingest once. The SQLite databases *are* committed, because they're small, diffable enough, and mean a fresh clone can query immediately.

## Tests

```bash
python tests/test_lifecycle.py
```

Covers the promote/park/archive file-lifecycle logic — the code most likely to quietly corrupt vault files during a refactor.
