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
from .corpus import build_chroma, corpus_identity, stored_identity
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
    parser.add_argument("--force", action="store_true",
                        help="Re-download CSV unconditionally, ignoring both the "
                             "publisher's validators and the cache")
    parser.add_argument(
        "--skip-unchanged", action="store_true",
        help="Exit 0 without rebuilding when the feed and the profile are both "
             "byte-identical to what the existing corpus was built from. For "
             "scheduled runs. Does NOT notice a change to this script, so run "
             "without it after a code change.",
    )
    parser.add_argument(
        "--status-file", type=Path, default=None,
        help="Write `rebuilt` or `unchanged` here. Lets a scheduled job decide "
             "whether there is anything to digest without parsing stdout, and "
             "without this script knowing what a CI runner is.",
    )
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

    def record_status(status: str) -> None:
        """Report the outcome, if anyone asked to be told."""
        if args.status_file is not None:
            args.status_file.parent.mkdir(parents=True, exist_ok=True)
            args.status_file.write_text(status + "\n", encoding="utf-8", newline="\n")

    df = download_tenders(args.cache, force=args.force)

    identity = corpus_identity(args.cache, args.profile)
    if args.skip_unchanged:
        previous = stored_identity(db_path)
        # Both hashes must be present on both sides. A missing hash is "nothing
        # to compare", and treating that as agreement is how a corpus built
        # before this flag existed would be declared current forever.
        keys = ("feed_sha256", "profile_sha256")
        comparable = all(previous.get(k) and identity.get(k) for k in keys)
        if comparable and all(previous[k] == identity[k] for k in keys):
            print(f"\nUnchanged: the feed and profile are byte-identical to the "
                  f"corpus already at {db_path}.\n"
                  f"  feed    {identity['feed_sha256'][:12]}\n"
                  f"  profile {identity['profile_sha256'][:12]}\n"
                  f"Nothing rebuilt, and no build stamp moved. This is a "
                  f"successful run.")
            record_status("unchanged")
            return
        if not comparable:
            print("\n--skip-unchanged: no comparable identity on the existing "
                  "corpus, so rebuilding. (Expected once, on the first run "
                  "after this flag was added.)")

    cols = resolve_columns(
        list(df.columns), TENDER_COLUMNS, TENDER_REQUIRED, "scripts/ingest"
    )
    df = filter_tenders(df, criteria, cols, value_extractor=value_extractor)

    if len(df) == 0:
        print("\nNo tenders passed the filter. Loosen your criteria.", file=sys.stderr)
        sys.exit(1)

    build_chroma(df, db_path, cols, feed_path=args.cache, identity=identity)
    record_status("rebuilt")
    print("\nDone. Claude Code can now search this corpus via scripts/tender_tools")
