"""
Ingest Canadian government tenders into ChromaDB, filtered by profile.

The filter is aggressive on purpose. The LLM Wiki pattern and hybrid agentic
retrieval both work better on a focused corpus than on a huge unfiltered one.
For a mid-size IT consulting firm, you might go from 2,800 active tenders
down to ~200-400 that actually match the profile.

Usage:
    python scripts/ingest
    python scripts/ingest --profile vault/profiles/my-company.md
    python scripts/ingest --force  # re-download even if cached
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ingest_common import output_path, resolve_columns
from .company_profile import parse_profile
from .corpus import build_chroma
from .feed import download_tenders
from .filters import filter_tenders
from .paths import DEFAULT_CACHE, DEFAULT_DB, DEFAULT_PROFILE
from .schema import TENDER_COLUMNS, TENDER_REQUIRED
from .value import make_llm_value_extractor


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument(
        "--db", type=Path, default=None,
        help=f"Output corpus path (default {DEFAULT_DB}). Pointing --cache at a "
             "non-default CSV redirects output to a .sample path unless this is "
             "given, so a spot-check can't replace the real corpus.",
    )
    parser.add_argument("--force", action="store_true", help="Re-download CSV even if cached")
    parser.add_argument(
        "--extract-values",
        action="store_true",
        help="Use the Anthropic API for contract-value extraction instead of the "
             "first-dollar-amount regex. Requires ANTHROPIC_API_KEY. Costs a few "
             "cents per ingest; noticeably more accurate.",
    )
    args = parser.parse_args()

    if not args.profile.exists():
        print(f"Profile not found: {args.profile}", file=sys.stderr)
        sys.exit(1)

    db_path = output_path(
        DEFAULT_DB, args.db,
        f"--cache points at {args.cache}, not the default"
        if args.cache != DEFAULT_CACHE else None,
    )

    criteria = parse_profile(args.profile)
    print(f"Profile: {args.profile}")
    print(f"  UNSPSC families: {criteria['unspsc_families'] or '(none — keywords only)'}")
    print(f"  Competencies: {criteria['competencies']}")
    print(f"  Exclusions: {criteria['exclude']}")
    # Only announce a value range when one is actually going to be applied.
    if args.extract_values and (criteria["value_min"] > 0
                                or criteria["value_max"] < 100_000_000):
        print(f"  Value range: ${criteria['value_min']:,} – ${criteria['value_max']:,}")

    value_extractor = make_llm_value_extractor() if args.extract_values else None
    if args.extract_values:
        print("  Value extraction: LLM (Anthropic API)")

    df = download_tenders(args.cache, force=args.force)
    cols = resolve_columns(
        list(df.columns), TENDER_COLUMNS, TENDER_REQUIRED, "scripts/ingest"
    )
    df = filter_tenders(df, criteria, cols, value_extractor=value_extractor)

    if len(df) == 0:
        print("\nNo tenders passed the filter. Loosen your criteria.", file=sys.stderr)
        sys.exit(1)

    build_chroma(df, db_path, cols, feed_path=args.cache)
    print("\nDone. Claude Code can now search this corpus via scripts/tender_tools.py")
