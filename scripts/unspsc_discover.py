"""
Offline discovery of UNSPSC commodity families for the profile's filter list.

This script is NOT part of the ingest path and nothing imports it. You run it by
hand, read the table, and paste a hand-checked `unspsc_families` list into
vault/profiles/my-company.md. That separation is deliberate: the list of
families that describe your competencies is a judgement about your business, and
it belongs in committed config where you can see it change in a diff — not in a
join computed fresh on every ingest.

Source: PSPC's GSIN/NIBS-to-UNSPSC linkage file, Open Government Licence.
https://donnees-data.tpsgc-pwgsc.gc.ca/ba2/aev-bas/nibsunspsc-gsinunspsc.csv

ONLY the UNSPSC side of that file is read: code, description, HierarchyLevel
(L1-L4) and CommodityType (Construction/Goods/Services), which all describe the
UNSPSC code. The GSIN columns are dropped on load and never joined on. PSPC's
own caveat is that the linkages were assessed at higher levels and carried
through indiscriminately, and the file shows it — telecom cable laying and
highway paving both map to GSIN 5153 "Foundation work, including pile driving".
Rolling a tender up through GSIN would put paving in your cloud results.

Usage:
    python scripts/unspsc_discover.py                     # families seen in the live feed
    python scripts/unspsc_discover.py --segment 43 81 80  # drill into segments
    python scripts/unspsc_discover.py --level L3          # roll up at L3 instead of L2
    python scripts/unspsc_discover.py --grep cloud        # families whose description matches
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).parent))
from ingest import (  # noqa: E402  (path shim above must run first)
    DEFAULT_CACHE,
    PROJECT_ROOT,
    TENDER_COLUMNS,
    TENDER_REQUIRED,
    parse_unspsc_codes,
)
from ingest_common import REQUEST_HEADERS, resolve_columns  # noqa: E402

UNSPSC_REF_URL = (
    "https://donnees-data.tpsgc-pwgsc.gc.ca/ba2/aev-bas/nibsunspsc-gsinunspsc.csv"
)
DEFAULT_REF_CACHE = PROJECT_ROOT / ".cache" / "unspsc_reference.csv"

# The UNSPSC side only. Everything else in that file is the GSIN linkage, which
# this script deliberately never reads — see the module docstring.
REF_COLUMNS = {
    "code": ["UNSPSC-Code"],
    "description": ["UNSPSC-Description-eng"],
    "level": ["HierarchyLevel-NiveauHierarchique"],
    "commodity_type": ["CommodityType-TypeProduit-eng"],
}


def download_reference(cache_path: Path, force: bool = False) -> pd.DataFrame:
    """Fetch the PSPC reference file, cached on disk. Refreshed rarely by PSPC."""
    if cache_path.exists() and not force:
        print(f"Using cached reference: {cache_path}")
    else:
        print(f"Downloading {UNSPSC_REF_URL} ...")
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        response = requests.get(UNSPSC_REF_URL, headers=REQUEST_HEADERS, timeout=180)
        response.raise_for_status()
        cache_path.write_bytes(response.content)
        print(f"  Cached to {cache_path} ({len(response.content):,} bytes)")

    df = pd.read_csv(cache_path, dtype=str, low_memory=False, encoding="utf-8-sig")
    cols = resolve_columns(
        list(df.columns), REF_COLUMNS, list(REF_COLUMNS), "scripts/unspsc_discover.py"
    )
    # Drop the GSIN side here, at the door, so nothing downstream can reach it.
    ref = df[[cols[k] for k in REF_COLUMNS]].copy()
    ref.columns = list(REF_COLUMNS)
    ref["code"] = ref["code"].astype(str).str.strip().str.lstrip("*")
    ref = ref[ref["code"].str.fullmatch(r"\d{8}")]

    # One UNSPSC code appears once per GSIN it was linked to, so the same code
    # repeats. Collapse to one row per code and report any code whose
    # CommodityType is inconsistent across those duplicate rows — that would
    # make "drop Construction" ambiguous, and you should know before trusting it.
    conflicts = (
        ref.groupby("code")["commodity_type"].nunique().pipe(lambda s: s[s > 1]).index
    )
    if len(conflicts):
        print(
            f"\nWARNING: {len(conflicts):,} UNSPSC codes carry more than one "
            f"CommodityType across their GSIN linkage rows, e.g. "
            f"{sorted(conflicts)[:5]}. First value wins below.",
            file=sys.stderr,
        )
    return ref.drop_duplicates(subset="code", keep="first").set_index("code")


def load_feed_codes() -> tuple[dict[str, set[str]], dict[str, list[str]], int]:
    """
    Map each 8-digit UNSPSC code seen in the live tender feed to the notices
    carrying it, plus a few sample titles. Returns (code -> ids, code -> titles,
    total notices).
    """
    if not DEFAULT_CACHE.exists():
        sys.exit(
            f"No cached tender feed at {DEFAULT_CACHE}.\n"
            "Run: python scripts/ingest"
        )
    df = pd.read_csv(DEFAULT_CACHE, low_memory=False)
    cols = resolve_columns(
        list(df.columns), TENDER_COLUMNS, TENDER_REQUIRED, "scripts/unspsc_discover.py"
    )
    by_code: dict[str, set[str]] = defaultdict(set)
    titles: dict[str, list[str]] = defaultdict(list)
    for _, row in df.iterrows():
        tid = str(row.get(cols["tender_id"], ""))
        title = str(row.get(cols["title"], ""))
        for code in parse_unspsc_codes(row.get(cols["unspsc"]) if cols["unspsc"] else None):
            by_code[code].add(tid)
            if len(titles[code]) < 3:
                titles[code].append(title)
    return by_code, titles, len(df)


def roll_up(code: str, level: str) -> str:
    """Truncate an 8-digit commodity code to its family/class prefix, zero-padded."""
    width = {"L1": 2, "L2": 4, "L3": 6, "L4": 8}[level]
    return code[:width].ljust(8, "0")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--level", choices=["L1", "L2", "L3", "L4"], default="L2",
                        help="Hierarchy level to roll up to (default L2 — a dozen "
                             "families is hand-checkable, hundreds of L4 codes is not)")
    parser.add_argument("--segment", nargs="*", default=None,
                        help="Only show these 2-digit UNSPSC segments, e.g. 43 81 80")
    parser.add_argument("--grep", default=None,
                        help="Only show families whose description matches this substring")
    parser.add_argument("--commodity-type", nargs="*", default=None,
                        help="Only show these CommodityTypes, e.g. Services Goods")
    parser.add_argument("--min-notices", type=int, default=1,
                        help="Hide families appearing on fewer notices than this")
    parser.add_argument("--ref-cache", type=Path, default=DEFAULT_REF_CACHE)
    parser.add_argument("--force", action="store_true", help="Re-download the reference file")
    args = parser.parse_args()

    ref = download_reference(args.ref_cache, force=args.force)
    by_code, titles, total_notices = load_feed_codes()

    coded = set().union(*by_code.values()) if by_code else set()
    print(f"\nLive feed: {total_notices:,} notices, {len(coded):,} carrying a UNSPSC code, "
          f"{len(by_code):,} distinct codes.")
    unknown = [c for c in by_code if c not in ref.index]
    if unknown:
        print(f"  {len(unknown):,} codes in the feed are absent from the PSPC "
              f"reference file, e.g. {sorted(unknown)[:4]}")

    # Aggregate feed notices up to the requested level.
    fam_ids: dict[str, set[str]] = defaultdict(set)
    fam_titles: dict[str, list[str]] = defaultdict(list)
    for code, ids in by_code.items():
        fam = roll_up(code, args.level)
        fam_ids[fam] |= ids
        for t in titles[code]:
            if len(fam_titles[fam]) < 3:
                fam_titles[fam].append(t)

    rows = []
    for fam, ids in fam_ids.items():
        # Prefer the reference file's own name for the rolled-up family code;
        # fall back to a member code's description when PSPC doesn't list the
        # rollup itself (the file is not complete at every level).
        if fam in ref.index:
            desc = ref.loc[fam, "description"]
            ctype = ref.loc[fam, "commodity_type"]
        else:
            members = [c for c in by_code if roll_up(c, args.level) == fam and c in ref.index]
            desc = (ref.loc[members[0], "description"] + " (rollup unnamed in reference)"
                    if members else "(not in reference file)")
            ctype = ref.loc[members[0], "commodity_type"] if members else "?"
        rows.append({"family": fam, "description": desc, "commodity_type": ctype,
                     "notices": len(ids), "samples": fam_titles[fam]})

    if args.segment:
        keep = {s.zfill(2) for s in args.segment}
        rows = [r for r in rows if r["family"][:2] in keep]
    if args.grep:
        rows = [r for r in rows if args.grep.lower() in str(r["description"]).lower()]
    if args.commodity_type:
        keep_ct = {c.lower() for c in args.commodity_type}
        rows = [r for r in rows if str(r["commodity_type"]).lower() in keep_ct]
    rows = [r for r in rows if r["notices"] >= args.min_notices]
    rows.sort(key=lambda r: -r["notices"])

    print(f"\n{args.level} families, most-used first "
          f"({len(rows)} shown):\n")
    print(f"  {'FAMILY':<10} {'TYPE':<13} {'NOTICES':>7}  DESCRIPTION")
    print(f"  {'-'*10} {'-'*13} {'-'*7}  {'-'*52}")
    for r in rows:
        print(f"  {r['family']:<10} {str(r['commodity_type']):<13} {r['notices']:>7}  "
              f"{str(r['description'])[:52]}")

    print("\nSample titles per family (sanity-check before committing):\n")
    for r in rows[:25]:
        print(f"  {r['family']}  {str(r['description'])[:46]}")
        for t in r["samples"]:
            print(f"      - {t[:74]}")

    print("\n" + "=" * 78)
    print("Paste the families you want into vault/profiles/my-company.md under")
    print("`unspsc_families:`. Prefixes match, so 8111 catches every 8111xxxx code.")
    print("=" * 78)
    width = {"L1": 2, "L2": 4, "L3": 6, "L4": 8}[args.level]
    print("unspsc_families:")
    for r in rows:
        prefix = r["family"][:width]
        print(f"  - '{prefix}'  # {str(r['description'])[:50]} ({r['notices']} notices)")


if __name__ == "__main__":
    main()
