"""
Tools for Claude Code to work with the tender corpus.

This is the 'wrap' layer — Claude decides what to search for, when to fetch
full details, when to promote, etc. These tools just execute cleanly and
return JSON.

Design principle: each command is a single verb. Each prints JSON to stdout.
Errors go to stderr and non-zero exit codes. Claude can chain these freely.

Usage:
    python scripts/tender_tools search "cloud migration federal"
    python scripts/tender_tools get W1234-567890
    python scripts/tender_tools similar W1234-567890
    python scripts/tender_tools list-corpus --window imminent
    python scripts/tender_tools list-watching
    python scripts/tender_tools list-parked
    python scripts/tender_tools contracts-intel "cloud"
    python scripts/tender_tools promote W1234-567890
    python scripts/tender_tools park some-file.md "no clearance" "after hiring cleared architect"
    python scripts/tender_tools archive some-file.md "lost to competitor"
    python scripts/tender_tools attach cb-342-92719341 --platform merx
    python scripts/tender_tools list-attachments cb-342-92719341
    python scripts/tender_tools read-attachment cb-342-92719341 RFP-W2187-SPO.pdf --limit 40
"""
from __future__ import annotations

import argparse
import json
import sys

import attachments

from .contracts import cmd_contracts_intel, cmd_expiring_contracts
from .documents import cmd_attach, cmd_list_attachments, cmd_read_attachment
from .dossier import cmd_department_dossier
from .lifecycle import (
    cmd_archive,
    cmd_list_corpus,
    cmd_list_parked,
    cmd_list_watching,
    cmd_park,
    cmd_promote,
)
from .search import cmd_get, cmd_search, cmd_similar
from .signals import (
    cmd_lobbying_signals,
    cmd_oag_signals,
    cmd_program_signals,
    cmd_registrations_signals,
    cmd_resolve_department,
)

# ONE department identifier across all four signal tools. Inconsistency here
# would be its own bug: the entire point of these tools is cross-referencing a
# department between them, and that fails if each takes a different spelling.
_DEPT_HELP = (
    "Canonical key from vault/crosswalk/org_aliases.yaml (e.g. pspc, ircc, dnd) "
    "or an organization's registered name. Exact after normalization — "
    "substrings are refused, so one department's name cannot land on another. "
    "Use `resolve-department` to check a string first."
)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("search", help="Hybrid search the full corpus")
    s.add_argument("query")
    s.add_argument("--n", type=int, default=10)
    s.set_defaults(func=cmd_search)

    g = sub.add_parser("get", help="Full details for one tender")
    g.add_argument("tender_id")
    g.set_defaults(func=cmd_get)

    sim = sub.add_parser("similar", help="Find tenders similar to a given one")
    sim.add_argument("tender_id")
    sim.add_argument("--n", type=int, default=5)
    sim.set_defaults(func=cmd_similar)

    lc = sub.add_parser(
        "list-corpus",
        help="Every notice in the corpus by closing date — for reading it end to end",
    )
    lc.add_argument(
        "--window",
        choices=["closed", "imminent", "open", "standing", "unknown"],
        help="Only notices in this closing window (default: all)",
    )
    lc.set_defaults(func=cmd_list_corpus)

    lw = sub.add_parser("list-watching", help="List promoted tenders")
    lw.set_defaults(func=cmd_list_watching)

    lp = sub.add_parser("list-parked", help="List parked tenders with revisit triggers")
    lp.set_defaults(func=cmd_list_parked)

    ci = sub.add_parser(
        "contracts-intel",
        help="Who won similar contracts: vendors, departments, values (SQLite, instant)",
    )
    ci.add_argument("query")
    ci.add_argument("--department", help=_DEPT_HELP)
    ci.set_defaults(func=cmd_contracts_intel)

    ec = sub.add_parser(
        "expiring-contracts",
        help="Contracts expiring in a window — near-certain future re-procurements",
    )
    ec.add_argument("--months-min", type=int, default=6,
                    help="Earliest expiry, months from now (default 6)")
    ec.add_argument("--months-max", type=int, default=24,
                    help="Latest expiry, months from now (default 24)")
    ec.add_argument("--min-value", type=float, default=None,
                    help="Minimum contract value (overrides profile's "
                         "expiry_min_value; default reads from profile)")
    ec.add_argument("--department", help=_DEPT_HELP)
    ec.set_defaults(func=cmd_expiring_contracts)

    ps = sub.add_parser(
        "program-signals",
        help="Programs showing operational pressure (pre-RFP intent signal)",
    )
    ps.add_argument("--department", help=_DEPT_HELP)
    ps.add_argument("--min-score", type=float, default=None,
                    help="Only programs with intent_score at or above this "
                         "(default: no floor, since real leads can score slightly negative)")
    ps.add_argument("--exclude-internal", action="store_true",
                    help="Exclude Internal Services programs (included by default)")
    ps.add_argument("--limit", type=int, default=25, help="How many to return (default 25)")
    ps.set_defaults(func=cmd_program_signals)

    og = sub.add_parser(
        "oag-signals",
        help="OAG audits touching IT/systems — independent-scrutiny pre-RFP signal",
    )
    og.add_argument("--department", help=_DEPT_HELP)
    og.add_argument("--vendor",
                    help="Audits INTO a named supplier (e.g. GCStrategies, "
                         "McKinsey). Independent of --department: these audits "
                         "span too many departments to attach to one, but an "
                         "audit of a firm you bid against is intelligence anyway")
    og.add_argument("--direct-only", action="store_true",
                    help="Only departments named in the audit itself, dropping "
                         "those a briefing package inherited from a hearing")
    og.add_argument("--min-score", type=float, default=None,
                    help="Only audits with it_score at or above this")
    og.add_argument("--doc-type", choices=["performance_audit", "committee_hearing",
                                           "special_examination", "financial_audit"],
                    help="Restrict to one document type")
    og.add_argument("--since", type=int, default=None, help="Only audits from this year onward")
    og.add_argument("--limit", type=int, default=20, help="How many to return (default 20)")
    og.set_defaults(func=cmd_oag_signals)

    lb = sub.add_parser(
        "lobbying-signals",
        help="Who has been meeting a department, on what subject — the "
             "earliest pre-RFP signal. Presence, never influence",
    )
    lb.add_argument("--department", help=_DEPT_HELP)
    lb.add_argument("--subject",
                    help="One filed subject matter, e.g. 'Government "
                         "Procurement'. See --list-subjects for the list")
    lb.add_argument("--client", help="Client organization name contains this")
    lb.add_argument("--vendor",
                    help="Client matched the way contracts.db matches vendors, "
                         "so an incumbent and a lobbying client compare equal")
    lb.add_argument("--since", help="Only communications on or after YYYY-MM-DD")
    lb.add_argument("--list-subjects", action="store_true",
                    help="Print the filed subject matters and their counts, "
                         "then stop")
    lb.add_argument("--limit", type=int, default=25,
                    help="How many to return (default 25)")
    lb.set_defaults(func=cmd_lobbying_signals)

    rg = sub.add_parser(
        "registrations-signals",
        help="Who was REGISTERED to lobby a department, as of a date. "
             "--as-of is required and has no default",
    )
    rg.add_argument("--as-of", dest="as_of", required=True,
                    help="REQUIRED, YYYY-MM-DD, or 'today' for current state. "
                         "There is no default: this database stores every "
                         "registration version because 53%% of amended "
                         "registrations change which departments they name, "
                         "and a default meaning 'latest' would answer a "
                         "time-ordered question with present-tense data")
    rg.add_argument("--department", help=_DEPT_HELP)
    rg.add_argument("--client", help="Client organization name contains this")
    rg.add_argument("--vendor",
                    help="Client matched the way contracts.db matches vendors")
    rg.add_argument("--limit", type=int, default=25,
                    help="How many to return (default 25)")
    rg.set_defaults(func=cmd_registrations_signals)

    rd = sub.add_parser(
        "resolve-department",
        help="What a department string resolves to — check before querying",
    )
    rd.add_argument("name")
    rd.set_defaults(func=cmd_resolve_department)

    do = sub.add_parser(
        "dossier",
        help="Everything all four sources know about one department — the "
             "convergence view. Assembles; does not score",
    )
    do.add_argument("department", help=_DEPT_HELP)
    do.add_argument("--months-min", type=int, default=6,
                    help="Expiry window opens this many months out (default 6)")
    do.add_argument("--months-max", type=int, default=24,
                    help="Expiry window closes this many months out (default 24)")
    do.add_argument("--min-value", type=float, default=None,
                    help="Value floor for the expiry timeline (default: "
                         "expiry_min_value from the company profile)")
    do.add_argument("--limit", type=int, default=10,
                    help="Rows per section (default 10)")
    do.set_defaults(func=cmd_department_dossier)

    pr = sub.add_parser("promote", help="Copy a tender into vault/tenders/watching/")
    pr.add_argument("tender_id")
    pr.set_defaults(func=cmd_promote)

    pk = sub.add_parser(
        "park",
        help="Move a watching tender to parked/ (not pursuing now, might revisit)",
    )
    pk.add_argument("filename")
    pk.add_argument("reason", help="Why we're parking it")
    pk.add_argument(
        "revisit_when",
        help="What event would make this worth re-evaluating",
    )
    pk.set_defaults(func=cmd_park)

    ar = sub.add_parser(
        "archive",
        help="Move a tender to archived/ (final). Source can be watching/ or parked/.",
    )
    ar.add_argument("filename")
    ar.add_argument("reason")
    ar.set_defaults(func=cmd_archive)

    at = sub.add_parser(
        "attach",
        help="Create the document folder for a tender (you drop the files in)",
    )
    at.add_argument("tender_id")
    at.add_argument(
        "--platform",
        choices=attachments.SOURCE_PLATFORMS,
        required=True,
        help="Where you pulled the package from, recorded as provenance",
    )
    at.add_argument(
        "--no-reveal",
        action="store_true",
        help="Don't try to open a file manager",
    )
    at.set_defaults(func=cmd_attach)

    la = sub.add_parser(
        "list-attachments",
        help="List a tender's dropped documents, extracting new/changed ones",
    )
    la.add_argument("tender_id")
    la.set_defaults(func=cmd_list_attachments)

    ra = sub.add_parser(
        "read-attachment",
        help="Read a window of one document's extracted text",
    )
    ra.add_argument("tender_id")
    ra.add_argument("filename", help="The dropped filename, e.g. RFP-W2187-SPO.pdf")
    ra.add_argument("--offset", type=int, default=0, help="First line (0-based)")
    ra.add_argument("--limit", type=int, default=400, help="Lines to return (max 2000)")
    ra.set_defaults(func=cmd_read_attachment)

    return p


def main():
    parser = build_parser()
    args = parser.parse_args()
    result = args.func(args)
    print(json.dumps(result, indent=2, default=str))
    # Error responses → non-zero exit so Claude notices
    if isinstance(result, dict) and "error" in result:
        sys.exit(1)


