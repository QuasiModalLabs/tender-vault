"""
MCP server exposing tender tools to Claude Desktop (and any MCP client).

Reuses all the logic from tender_tools.py. This file just adapts it to
the MCP protocol via the SDK's MCPServer (called FastMCP before mcp 2.0).

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

from mcp.server.mcpserver import MCPServer  # noqa: E402


mcp = MCPServer("tender-vault")


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
        matched_competencies, opportunity_kind, kind_basis, unspsc_families,
        score, in_watching, snippet.

        estimated_value is usually null — the feed publishes no value field, and
        null means unknown, not zero. opportunity_kind says what KIND of thing
        the notice is before any judgement of fit: 'qualification' is a vehicle
        rather than work, 'call_up' is work only bidders already on a vehicle
        can win, 'information' is an RFI with nothing to bid, and 'solicitation'
        is a residual meaning the shape is unresolved — read the description.
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
def contracts_intel(query: str, department: str = None) -> dict:
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
        department: Canonical key from org_aliases.yaml (pspc, ircc, dnd) or an
                    organization's registered name — the SAME identifier
                    oag_signals and program_signals take, which is what makes
                    convergence across the three a real join (optional).

    Returns:
        Dict with families count, total/median values, top_vendors,
        top_departments, recent_examples, as_of, and caveats.
    """
    args = SimpleNamespace(query=query, department=department)
    return tender_tools.cmd_contracts_intel(args)


@mcp.tool()
def expiring_contracts(months_min: int = 6, months_max: int = 24,
                       min_value: float = None, department: str = None) -> dict:
    """
    Contracts expiring in a window — near-certain future re-procurements.

    A contract in our competency space whose delivery period ends in 6-24
    months is a future RFP we can see coming, with the incumbent, department,
    and value known today. Use this for proactive business development: which
    big contracts are coming up for renewal, who holds them, worth building a
    relationship around before the tender drops.

    A lead list for human judgment, not a prediction. Value floor defaults to
    the profile's expiry_min_value.

    Args:
        months_min: Earliest expiry, months from now (default 6).
        months_max: Latest expiry, months from now (default 24).
        min_value: Minimum contract value; None reads the profile default.
        department: Canonical key from org_aliases.yaml or an organization's
                    registered name — the same identifier the other signal
                    tools take (optional).

    Returns:
        Dict with expiring count, window, min_value, and per-contract incumbent,
        department, value, expiry, months_until_expiry, description.
    """
    args = SimpleNamespace(months_min=months_min, months_max=months_max,
                           min_value=min_value, department=department)
    return tender_tools.cmd_expiring_contracts(args)


@mcp.tool()
def program_signals(department: str = None, min_score: float = None,
                    exclude_internal: bool = False, limit: int = 25) -> dict:
    """
    Programs signalling FORWARD-LOOKING modernization intent — the pre-RFP
    signal from Departmental Plans / Results Reports.

    Where contracts_intel says WHAT was bought and expiring_contracts says WHAT
    is up for renewal, this says WHAT a department PLANS to do: it ranks programs
    by intent_score, a semantic score over each program's planning_explanation
    (forward-looking — "investing to modernize the case management system",
    "migrating infrastructure to the cloud"), scored toward IT/modernization
    intent and away from routine-operations noise. A stated plan to modernize is
    a conversation to open before the RFP exists.

    Each program also carries pressure_score/variance_explanation as secondary
    context (retrospective operational strain); a program that BOTH struggled
    and plans to modernize is the strongest signal. multi_year/other_scored_years
    show the intent trail across years where present.

    Internal Services is INCLUDED by default — in this data departmental IT spend
    is booked there, so it's where the IT-modernization signal lives. When
    reading Internal Services rows, distinguish vendor-buildable IT modernization
    (real opportunity) from union-locked operational delivery (skip). Judge
    IT-relevance and credibility — a small firm isn't a prime on a $90M program.
    A lead list, not a forecast.

    Args:
        department: Canonical key from org_aliases.yaml (pspc, ircc, dnd) or an
                    organization's registered name. Exact — substrings are
                    refused. Call resolve_department first if unsure (optional).
        min_score: Only programs with intent_score at or above this (default:
                   no floor — real leads can score slightly negative).
        exclude_internal: Exclude Internal Services programs (default False —
                          included, since that's where departmental IT is booked).
        limit: How many to return (default 25).

    Returns:
        Dict of programs ranked by intent_score, each with planning_explanation,
        pressure_score/variance_explanation context, pct_over_plan, multi_year
        flag and other_scored_years trail; plus as_of and how_to_read.
    """
    args = SimpleNamespace(department=department, min_score=min_score,
                           exclude_internal=exclude_internal, limit=limit)
    return tender_tools.cmd_program_signals(args)


@mcp.tool()
def oag_signals(department: str = None, min_score: float = None,
                doc_type: str = None, since: int = None, limit: int = 20,
                vendor: str = None, direct_only: bool = False) -> dict:
    """
    OAG performance audits touching IT/systems — the independent-scrutiny pre-RFP
    signal, and the third leg of the convergence.

    Contracts say what a department bought; expiring_contracts what's up for
    renewal; program_signals what they PLAN to modernize; this says what the
    Auditor General has PUBLICLY found them failing at. An OAG finding is the
    most citable pre-RFP opener ("the AG flagged your backlog in 2023") and the
    scrutiny that forces a department to procure a fix.

    CONVERGENCE is the intended use: get an audit's department, then call
    program_signals and expiring_contracts with THE SAME canonical key. All four
    signal tools take one identifier, so a key that works in one works in all.

    READING THE DEPARTMENTS. `departments` are named in the audit itself — half
    of all audits name more than one, and an audit of six departments is a
    finding against all six. `inherited_departments` appear on committee
    briefing packages, which name no department of their own and take them from
    the report the hearing was about; each carries reports_in_hearing, and a
    department reached through a five-report agenda is much weaker evidence than
    one named in the audit. Use direct_only to drop them entirely.

    An audit with neither carries no_department_because, which distinguishes
    "audited nobody federal" from "we could not tell" — most of this corpus
    correctly has no department, so an empty list is usually not a gap.

    Args:
        department: Canonical key from org_aliases.yaml (pspc, ircc, dnd) or an
                    organization's registered name. Exact — substrings are
                    refused, so "Immigration and Refugee Board" gets the
                    tribunal and never IRCC. Call resolve_department if unsure.
        min_score: Only audits with it_score at or above this (optional).
        doc_type: performance_audit | committee_hearing | special_examination |
                  financial_audit (optional).
        since: Only audits from this year onward (optional).
        limit: How many to return (default 20).
        vendor: Audits INTO a named supplier (GCStrategies, McKinsey).
                Independent of department — these span too many departments to
                attach to one, but an audit of a firm you bid against is
                competitive intelligence regardless.
        direct_only: Only departments named in the audit itself (default False).

    Returns:
        Dict of audits ranked by it_score, each with year, doc_type,
        departments, inherited_departments, title, description, report_url;
        plus as_of, per-population coverage, and how_to_read.
    """
    args = SimpleNamespace(department=department, min_score=min_score,
                           doc_type=doc_type, since=since, limit=limit,
                           vendor=vendor, direct_only=direct_only)
    return tender_tools.cmd_oag_signals(args)


@mcp.tool()
def resolve_department(name: str) -> dict:
    """
    What a department string resolves to, without running a query.

    Use this BEFORE the signal tools when a name is uncertain, so an empty
    result can be read correctly. "No audits for this department" and "that
    string is not a department" are very different answers, and every other tool
    would otherwise return the same empty list for both.

    Resolution is registry-only and exact after normalization, honouring each
    entry's `not:` exclusions — the same rule all four signal tools apply.
    Fragments are refused on purpose: substring matching is how one
    organization's name lands on another's dossier.

    Args:
        name: A canonical key or an organization name.

    Returns:
        The canonical key, display name, known aliases, the names it refuses,
        and which source datasets can answer for it — or `closest` suggestions
        when nothing resolves.
    """
    return tender_tools.cmd_resolve_department(SimpleNamespace(name=name))


@mcp.tool()
def department_dossier(department: str, months_min: int = 6, months_max: int = 24,
                       min_value: float = None, limit: int = 10) -> dict:
    """
    Everything all four sources know about one department, in one call.

    The convergence view, and the reason the other four tools share a department
    identifier. Each signal is useful alone; the payoff is when they line up —
    the Auditor General flagged a department, its own plan says it intends to
    modernize that system, and the incumbent's contract expires in five months.
    That is about as strong a pre-RFP case as public data produces.

    THIS TOOL DOES NOT SCORE. There is no convergence number, no weighting of
    the four signals into one figure, and no ranking of departments by it — by
    design. The four signals are incommensurable, any weighting would be
    invented, and a single number hides the reasoning that makes the dossier
    worth reading. Read the four sections and judge. Do not compute a score of
    your own from them either; say what converges and why, in words.

    TENDERS ARE NOT REQUIRED, and their absence is the most valuable output.
    A department with an audit finding, a stated plan, an expiring incumbent and
    NO open tender is the pre-RFP position to act on: the work is coming and
    nobody has been asked yet. An empty tenders section never weakens the case.

    READING THE SECTIONS.

    audits — direct_findings name the department in the finding itself.
    bundle_attached are committee briefing packages that cite a report naming
    it: real scrutiny, weaker evidence, with parent_reports_in_bundle saying how
    many reports the package covered. Never merge the two lists. Links: use
    source_url. Anything in report_url_dead is an oag-bvg.gc.ca deep link that
    no longer resolves — cite it, never present it as a working link.

    plans — `state` says which kind of answer this is. intent_scored is stated
    forward intent. no_intent_prose means the department files plans but no
    planning_explanation in any year, so nothing is intent-scored; 16 of 94
    organizations are in this state including DND, GAC, RCMP, CBSA and SSC, and
    a `strain` block appears instead, scored from retrospective variance prose.
    Strain is NOT intent and the two are never ranked against each other.
    no_prose_at_all means neither field is populated. files_no_plans means the
    organization files none — a fact about it, not missing data. A
    boilerplate_note marks one sentence filed against many programs: that is one
    signal, not many.

    contracts — top vendors by value and, critically, expiry_timeline. An
    incumbent contract ending in 6-24 months is the most actionable field here.

    tenders — entity_source says how each notice reached this department.
    'end_user' means it named them as the customer; anything else means they are
    the contracting entity, attributed via contracting entity with the end user
    unstated, which for SSC and PSPC often means buying for somebody else.
    A null closing_date with a date_note is a sentinel, not a deadline.

    opportunity_kind is the instrument, and reading it before assessing fit is
    the point: 'qualification' (RFSA, standing offer, invitation to qualify)
    puts a supplier on a vehicle and buys nothing; 'call_up' is an RFP against
    a supply arrangement, which IS work but only for suppliers already holding
    it; 'information' is an RFI; 'pre_awarded' is an ACAN already intended for
    a named supplier; 'product' is a goods-only purchase; 'solicitation' is the
    residual and NOT a positive finding — a SaaS purchase and an untyped
    call-up both land there. kind_basis says which field decided it.

    Always check identity.records_folded_in before quoting a total. Where a
    predecessor or absorbed organization is folded in, the registry's note says
    what the figure actually covers.

    Args:
        department: Canonical key from org_aliases.yaml (ssc, ircc, dnd) or an
                    organization's registered name. Exact after normalization —
                    substrings are refused. Call resolve_department if unsure.
        months_min: Expiry window opens this many months out (default 6).
        months_max: Expiry window closes this many months out (default 24).
        min_value: Value floor for the expiry timeline. Defaults to
                   expiry_min_value from the company profile.
        limit: Rows per section (default 10).

    Returns:
        identity, audits, plans, contracts and tenders, each with a `state`
        saying which kind of empty it is when it is empty.
    """
    args = SimpleNamespace(department=department, months_min=months_min,
                           months_max=months_max, min_value=min_value,
                           limit=limit)
    return tender_tools.cmd_department_dossier(args)


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
