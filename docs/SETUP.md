# Setup

Full install, configuration, and troubleshooting. For what the project *is*, see the [README](../README.md).

## Before you start

- **Python 3.11.** That's what CI runs; nothing enforces a floor at install time.
- **Network on first run.** Every ingest fetches its source data live, and
  sentence-transformers downloads the `all-MiniLM-L6-v2` embedding model once
  (~90MB, cached in your user profile, not in the repo). Nothing here needs an
  API key — the only exception is `ingest.py --extract-values`.
- **Disk.** Roughly 150MB for a tenders-only install: ~90MB model, 30–80MB
  ChromaDB, plus cached source CSVs in `.cache/`. The optional contracts layer
  needs about **1GB more** — its ~630MB source CSV is cached to `.cache/` and
  kept, on top of the filtered database it produces. That cache is safe to
  delete afterwards.
- **Obsidian is optional.** The vault is plain markdown on disk and every tool
  reads it directly. Obsidian is a nice way to browse it, not a dependency.

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

**Edit your profile first.** Every layer filters and scores against it, so building before editing gives you a corpus and a set of scores aimed at somebody else's business.

```bash
# macOS/Linux
$EDITOR vault/profiles/my-company.md
# Windows PowerShell
notepad vault\profiles\my-company.md
```

**Read the comments in that file — they are the specification.** Every
frontmatter key is annotated in place with what it does, why the shipped value
was chosen, and what was deliberately excluded. There is no separate key-by-key
reference, on purpose: one would drift from the file the parser actually reads.

[`docs/PROFILE.md`](PROFILE.md) covers what the profile can't say about itself —
how the two-pole theme scoring works and how to tune it, how to pick UNSPSC
families, and which edits require a rebuild versus taking effect immediately.

Nothing derived is committed to this repo. The tender corpus, the contracts database, and the plans and audit scores are all products of your profile, so they have to be built locally. Only `data/crosswalk.db` ships, because it derives from `vault/crosswalk/org_aliases.yaml` — facts about the Government of Canada rather than about any one firm.

### 1. Tenders — required

```bash
python scripts/ingest.py
```

Downloads the current CanadaBuys open-notices CSV, applies the profile filter, and builds embeddings into a local ChromaDB store. About two minutes. The funnel counts print as it runs, so you can see how many notices each stage removed and tune accordingly — that output is the authority on corpus size, not any number written in the docs.

Optional flag:

```bash
python scripts/ingest.py --extract-values
```

Replaces the default regex value extraction with a per-tender model extraction pass. Requires `ANTHROPIC_API_KEY` and costs a few cents per ingest. Note that value data is sparse in this feed regardless — most notices publish no figure at all, and the value filter is disabled by default because of it.

### 2. Plans and audits — required for the signal tools

```bash
python scripts/plans_ingest.py    # ~1 min
python scripts/oag_ingest.py      # ~1 min
```

Both score prose locally with the embedding model at ingest — no API key, no model tokens.

**Re-running these under a different profile is a re-scoring, not a refresh.** The scores come from the theme example sentences in your profile, so the same source data produces different rankings for different firms. If you edit a theme block, rebuild.

Add `--show-extremes` to either to print the top and bottom scored records. It's a tuning aid: if the top of the list looks wrong, the theme poles need adjusting.

### 3. Contracts — optional, large

```bash
python scripts/contracts_ingest.py
```

Downloads the ~630MB proactive-disclosure contracts dataset and filters it to your profile's categories. Five to ten minutes. Skip it if you only want opportunity discovery; `contracts-intel`, `expiring-contracts` and the contracts section of the dossier won't work without it.

The source CSV is millions of rows. It's downloaded to `.cache/contracts.csv` first and then filtered chunk-by-chunk from disk, so only matching rows reach the database — but the full ~630MB file does land on disk and stays there. It's reused by any re-run within 24 hours and re-downloaded after that, so if you're tuning `contracts_categories`, do it in one sitting. Delete `.cache/contracts.csv` when you're done if you need the space.

The download is **resumable by design**. That Azure endpoint drops residential connections mid-transfer, and a plain download restarts from zero each time and may never finish. It streams to a `.part` file, resumes with an HTTP Range request from the bytes already written, and only renames to `.csv` once the byte count matches `Content-Length` — so an interrupted run can never leave a partial file that looks complete. If it drops, just run it again.

**Category matching.** Contracts describe work as standardized procurement categories, not free prose, so this filter reads a separate `contracts_categories` list in the profile (case-insensitive substring) rather than the tender competencies. The profile ships a commented reference catalog of the dataset's real category vocabulary, organized by sector, so adapting to a different company is a copy-paste.

**The recency window.** A contract is kept if `max(award_date, period_end)` falls within the last N years (`contracts_window_years`, default 3). This captures recent awards and still-active older contracts in one pass. A strict period-overlap filter was tried first, but the dataset skews heavily toward completed historical contracts, so "active today" left almost nothing — and a recently-awarded contract that has already ended is still exactly the competitive signal this layer exists to surface.

This ingest also generates the per-department summaries in `vault/intel/agencies/`. Those are derived from your filtered database and are not committed.

## Using it with Claude Code

```bash
cd tender-vault
claude
```

Claude Code picks up `vault/CLAUDE.md` automatically and calls `scripts/tender_tools.py` as a subprocess. Start with a natural-language request:

> Any good federal IT tenders for us this week?

**You talk in English; the commands are Claude's, not yours.** Names like
`dossier`, `contracts-intel`, `expiring-contracts` and `oag-signals` are
subcommands of `scripts/tender_tools.py` that Claude invokes on your behalf —
they aren't slash commands and you don't type them into the chat. So when the
README shows `dossier ircc`, the way you get it is to ask:

> Give me the full dossier on IRCC.

You can also run them directly if you want raw JSON, which is useful for
debugging a layer that seems to be returning nothing:

```bash
python scripts/tender_tools.py dossier ircc
python scripts/tender_tools.py --help
```

The `tender-briefing` skill in `.claude/skills/` is picked up automatically too. Ask for the weekly briefing and it writes to `vault/briefings/` rather than printing. It's a Claude Code feature — it does not apply to the Claude Desktop path below.

## Using it with Claude Desktop (MCP)

### Find the right config file

This varies by install, and guessing wastes time:

- **macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows (direct installer):** `%APPDATA%\Claude\claude_desktop_config.json`
- **Windows (Microsoft Store version):** the app sandboxes its config elsewhere. Don't guess — open **Settings → Developer → Edit Config**, which opens the file the app actually reads.

### Add the server

Point `command` at the **virtual environment's Python**, not system Python, or the server won't find its dependencies. Use absolute paths.

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

Note that this path is absolute. Renaming or moving the repo, or deleting and recreating `.venv` at a different location, silently stops the server loading.

### Restart properly

Quitting means killing the tray or background process, not just closing the window: on Windows, right-click the system tray icon and choose Quit, or end it in Task Manager. The config is only read at startup.

### Verify

In a chat, open the `+` menu → **Connectors**. `tender-vault` should be listed with a toggle.

Do **not** use "Add custom connector" — that dialog is for remote MCP servers with URLs, not local ones.

Test with:

> Resolve the department name "Department of Citizenship and Immigration".

That calls `resolve_department`, which reads the committed `data/crosswalk.db`,
so it returns a real answer before you have built anything. Expect the canonical
key `ircc` — which is the crosswalk earning its place, since that's the name the
Departmental Plans use for the department the audits call Immigration, Refugees
and Citizenship Canada.

Pass a canonical key or a full registered name. Bare acronyms are **refused on
purpose** — `IRCC` returns `resolved: null`, because substring matching is how
one department's name lands on another. A null here is a correct answer about
your query, not a broken server.

Once `ingest.py` has run, confirm the corpus is wired up too:

> Search the tender corpus for cloud migration.

Avoid *"list the tenders in my watching folder"* as a first check. `vault/tenders/`
isn't committed, so on a fresh clone the correct answer is an empty list — which
looks exactly like a server that failed to load.

### Two behaviours to expect

**First call is slow.** The first ChromaDB-backed call of a session takes 30–90 seconds while the embedding model loads. Everything after that is fast.

**Claude Desktop doesn't auto-read `CLAUDE.md`** the way Claude Code does. Paste its contents into a Claude Project's instructions and do tender work inside that project.

## Automatic refresh

`.github/workflows/weekly-ingest.yml` runs every Monday:

1. Downloads the latest CanadaBuys CSV
2. Re-runs the filter pipeline
3. Writes a digest to `vault/digests/YYYY-MM-DD.md`, including a **New this week** section diffed against the previous run's corpus snapshot (`vault/digests/corpus-latest.txt`)
4. Commits the digest and the updated snapshot — not `chroma_db/`

Because the digest is generated on the Actions runner, it can reference tenders your local corpus hasn't seen. After pulling a new digest, re-run `python scripts/ingest.py` to sync locally.

The contracts and plans refresh workflows are present but disabled. They rebuilt databases that are no longer committed, so on a schedule they would run and commit nothing. Rebuild those locally when you want fresh data.

## What isn't committed, and why

| Path | Reason |
|---|---|
| `chroma_db/` | 30–80MB binary, rebuilt in two minutes |
| `data/contracts.db` | Filtered to one profile's categories |
| `data/plans.db`, `data/oag.db` | Scored from one profile's theme examples |
| `vault/intel/agencies/` | Derived from the filtered contracts database |
| `vault/tenders/`, `vault/briefings/`, `vault/searches/` | Working state — what's actually being bid on |
| `.cache/` | Source CSVs, re-downloaded on ingest |

`data/crosswalk.db` **is** committed. It derives from `org_aliases.yaml`, which is a hand-curated set of assertions about what the Government of Canada calls itself — the same file every user needs, and 53KB.

## Tests

Plain Python, no pytest. Exit code 0 means passed.

```bash
python tests/test_lifecycle.py
python tests/test_notice_classification.py
python tests/test_dossier.py
python tests/test_crosswalk.py
python tests/test_org_resolve.py
python tests/test_oag_attribution.py
python tests/test_end_user_multivalue.py
```

**These run on a fresh clone.** The suites that check built databases skip
cleanly when the data isn't there, so you don't need to ingest anything first —
though the skips mean a green run before ingest is weaker evidence than a green
run after.

The lifecycle tests cover promote/park/archive, the code most likely to quietly corrupt vault files during a refactor. The classification tests lock the notice-kind directions and assert that construction survives the prose rules. The dossier tests guard the two ways the convergence view degrades: growing a composite score, and blurring a distinction one of the four sources took real work to establish.
