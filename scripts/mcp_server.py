"""
MCP server exposing tender tools to Claude Desktop (and any MCP client).

Reuses all the logic from tender_tools.py. This file just adapts it to
the MCP protocol via FastMCP.

To use with Claude Desktop, add to your claude_desktop_config.json:

    {
      "mcpServers": {
        "tender-vault": {
          "command": "python",
          "args": ["/absolute/path/to/tender-vault/scripts/mcp_server.py"]
        }
      }
    }

On macOS the config file lives at:
    ~/Library/Application Support/Claude/claude_desktop_config.json

Restart Claude Desktop after editing. Look for the hammer icon in the input
box — it should show these tools as available.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

# Import the CLI tools module — we'll call its command functions directly
sys.path.insert(0, str(Path(__file__).parent))
import tender_tools  # noqa: E402

from mcp.server.fastmcp import FastMCP  # noqa: E402


mcp = FastMCP("tender-vault")


@mcp.tool()
def search(query: str, n: int = 10) -> dict:
    """
    Hybrid search (BM25 + semantic + RRF fusion) over the tender corpus.

    Use this when the user asks about tenders matching a topic, capability,
    or agency. Prefer short focused queries ("cloud migration federal")
    over long ones. The corpus has already been filtered to tenders that
    match the user's profile, so you don't need to include their
    competencies in the query.

    Args:
        query: Search terms.
        n: How many results to return (default 10, max 30).

    Returns:
        Dict with query, n, and a list of results. Each result has:
        tender_id, title, agency, closing_date, estimated_value,
        matched_competencies, score, in_watching, snippet.
    """
    args = SimpleNamespace(query=query, n=min(max(n, 1), 30))
    return tender_tools.cmd_search(args)


@mcp.tool()
def get_tender(tender_id: str) -> dict:
    """
    Fetch full details for one tender by ID.

    Call this after search when you need the full description to judge
    fit — search snippets are truncated and often miss key requirements
    (clearances, certifications, timelines).

    Args:
        tender_id: The tender ID, e.g. "EN578-123456".

    Returns:
        Dict with tender_id, metadata, document (full text), and in_watching.
    """
    args = SimpleNamespace(tender_id=tender_id)
    return tender_tools.cmd_get(args)


@mcp.tool()
def find_similar(tender_id: str, n: int = 5) -> dict:
    """
    Find tenders similar to a given one by semantic similarity.

    Useful when the user says "find more like this" or when you want to
    check if a promoted tender has close cousins still in the cold tier.

    Args:
        tender_id: The reference tender.
        n: How many similar tenders to return (default 5, max 20).

    Returns:
        Dict with target and a list of similar tenders (tender_id, title,
        agency, similarity).
    """
    args = SimpleNamespace(tender_id=tender_id, n=min(max(n, 1), 20))
    return tender_tools.cmd_similar(args)


@mcp.tool()
def list_watching() -> dict:
    """
    List tenders in the vault's watching folder — the promoted/hot tier.

    Call this near the start of any conversation about tenders. It tells
    you what the user is already tracking, so you can avoid duplicate
    recommendations and reference ongoing context.
    """
    return tender_tools.cmd_list_watching(SimpleNamespace())


@mcp.tool()
def promote(tender_id: str) -> dict:
    """
    Copy a tender from ChromaDB into vault/tenders/watching/ as a markdown file.

    Do NOT call this without the user's explicit confirmation. Always ask
    first: "Want me to promote W1234-567890 to watching?" The vault is
    the user's working memory — don't clutter it.

    Args:
        tender_id: The tender to promote.

    Returns:
        Dict with the path of the created file, or an error.
    """
    args = SimpleNamespace(tender_id=tender_id)
    return tender_tools.cmd_promote(args)


@mcp.tool()
def contracts_intel(query: str) -> dict:
    """
    Competitive intelligence from Canada's Proactive Publication of Contracts
    dataset: who actually WON contracts like this, from which departments, at
    what values.

    Use this when evaluating a promoted tender or discussing strategy:
    incumbents, typical contract values, and which departments buy in this
    space. Complements search (which covers open OPPORTUNITIES) with outcome
    data (AWARDED contracts, period-overlap window of recent years).

    Fast: pure SQLite, no model loading. Always relay the as_of date and the
    caveats to the user. The data is unaudited and vendor names are not
    normalized, so treat results as directional intelligence.

    Args:
        query: A keyword to match against contract descriptions
               (e.g. "cloud", "migration", "cybersecurity").

    Returns:
        Dict with families count, total/median values, top_vendors,
        top_departments, recent_examples, as_of, and caveats.
    """
    args = SimpleNamespace(query=query)
    return tender_tools.cmd_contracts_intel(args)


@mcp.tool()
def list_parked() -> dict:
    """
    List tenders in the parked folder, with their revisit triggers.

    Park is for tenders the user has decided not to pursue right now but
    might revisit if circumstances change (new hire, new partnership, the
    tender being reissued). Each parked tender has a "revisit_when"
    string saying what would unstick it.

    Useful when the user mentions a change that might match a trigger
    ("we just hired a cleared architect" → check parked/ for tenders
    waiting on exactly that).
    """
    return tender_tools.cmd_list_parked(SimpleNamespace())


@mcp.tool()
def park(filename: str, reason: str, revisit_when: str) -> dict:
    """
    Move a watching tender to parked/ — not pursuing now, might revisit later.

    Use this instead of archive when the decision is contingent. The
    revisit_when string should describe a concrete trigger event (not a
    vague "later"). Examples:
      - "after we hire a cleared architect"
      - "if reissued in 2027"
      - "if our partnership with X firm progresses"
      - "after we win our first federal contract"

    Requires user confirmation, like promote and archive.

    Args:
        filename: The .md filename in watching/ (not a path).
        reason: Why we're parking it now.
        revisit_when: What would make it worth re-evaluating.

    Returns:
        Dict with the parked path, or an error.
    """
    args = SimpleNamespace(filename=filename, reason=reason, revisit_when=revisit_when)
    return tender_tools.cmd_park(args)


@mcp.tool()
def archive(filename: str, reason: str) -> dict:
    """
    Move a tender to archived/ with a stated reason. Decision is final.

    Source can be either watching/ or parked/. Use park instead if the
    user might revisit the decision later — archive means done thinking
    about this one.

    Common reasons: "lost to competitor X", "closed before we could bid",
    "no-bid — clearance mismatch and no path to fix it".

    Requires user confirmation. The reason is appended to the file
    before moving.

    Args:
        filename: The .md filename (not a path). Found in watching/ or parked/.
        reason: Short explanation of why it's being archived.

    Returns:
        Dict with the archive path, the source folder, and the reason.
    """
    args = SimpleNamespace(filename=filename, reason=reason)
    return tender_tools.cmd_archive(args)


def _preload_in_background() -> None:
    """
    Start loading ChromaDB + the embedding model the moment the server starts.

    Without this, the first search/get/similar call pays the full cold-start
    cost (importing torch, loading the sentence-transformer, opening the DB) —
    60-90s on a typical machine, which exceeds Claude Desktop's tool-call
    timeout. Loading in a daemon thread at startup means the model warms up
    while the user is still typing their first message.

    The thread is a daemon so it never blocks server shutdown. If a tool call
    arrives before loading finishes, load_collection() is idempotent and the
    call simply waits for the same load (module-level state, single process).
    """
    import threading

    def _load():
        try:
            tender_tools.load_collection()
        except SystemExit:
            # ChromaDB not built yet (ingest never run) — tools will surface
            # the real error message when actually called.
            pass
        except Exception:
            # Same: don't crash the server at startup; let the tool call
            # report the failure with context.
            pass

    threading.Thread(target=_load, daemon=True).start()


if __name__ == "__main__":
    _preload_in_background()
    mcp.run()
