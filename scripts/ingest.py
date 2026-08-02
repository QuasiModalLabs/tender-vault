"""
Ingest Canadian government tenders into ChromaDB, filtered by profile.

The filter is aggressive on purpose. The LLM Wiki pattern and hybrid agentic
retrieval both work better on a focused corpus than on a huge unfiltered one.
For a mid-size IT consulting firm, you might go from 2,800 active tenders
down to ~200-400 that actually match the profile.

Usage:
    python scripts/ingest.py
    python scripts/ingest.py --profile vault/profiles/my-company.md
    python scripts/ingest.py --force  # re-download even if cached
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import pandas as pd
import requests
import yaml


# The "open tender notices" file: active tenders only. There's also a
# "complete" file (tenderNoticeComplete-...) with every notice since 2022,
# but it's much larger and we filter to open tenders anyway.
TENDER_URL = "https://canadabuys.canada.ca/opendata/pub/openTenderNotice-ouvertAvisAppelOffres.csv"

# Canada Buys' WAF returns 403 for the default python-requests user agent.
# A browser-like UA is required for the download to succeed.
REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/csv,application/csv,*/*",
}
PROJECT_ROOT = Path(__file__).parent.parent
DEFAULT_PROFILE = PROJECT_ROOT / "vault" / "profiles" / "my-company.md"
DEFAULT_CACHE = PROJECT_ROOT / ".cache" / "tenders.csv"
DEFAULT_DB = PROJECT_ROOT / "chroma_db"


# ---------------------------------------------------------------------------
# Profile parsing — the frontmatter of the profile is the filter config
# ---------------------------------------------------------------------------

def parse_profile(profile_path: Path) -> dict:
    """
    Extract filter criteria from the YAML frontmatter of the profile markdown.

    We use the real YAML parser here (not a regex) because the profile supports
    lists and the user might format them differently over time.
    """
    content = profile_path.read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
    if not match:
        raise ValueError(f"No YAML frontmatter found in {profile_path}")

    fm = yaml.safe_load(match.group(1)) or {}
    return {
        "value_min": int(fm.get("value_min", 0)),
        "value_max": int(fm.get("value_max", 100_000_000)),
        "competencies": [c.lower() for c in fm.get("competencies", [])],
        "exclude": [e.lower() for e in fm.get("exclude", [])],
        "min_days_until_close": int(fm.get("min_days_until_close", 5)),
        "contracts_window_years": int(fm.get("contracts_window_years", 3)),
        "contracts_categories": [c.lower() for c in fm.get("contracts_categories", [])],
        "expiry_min_value": float(fm.get("expiry_min_value", 0)),
    }


# ---------------------------------------------------------------------------
# Data download — cache aggressively
# ---------------------------------------------------------------------------

def download_tenders(cache_path: Path, force: bool = False) -> pd.DataFrame:
    """Download tender CSV, cached to disk for 12 hours."""
    if not force and cache_path.exists():
        age = datetime.now() - datetime.fromtimestamp(cache_path.stat().st_mtime)
        if age < timedelta(hours=12):
            print(f"Using cached CSV ({age.total_seconds() / 3600:.1f}h old)")
            return pd.read_csv(cache_path, low_memory=False)

    print("Downloading open tender notices from Canada Buys (may take a minute)...")
    response = requests.get(TENDER_URL, headers=REQUEST_HEADERS, timeout=180)
    response.raise_for_status()
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_bytes(response.content)
    return pd.read_csv(cache_path, low_memory=False)


# ---------------------------------------------------------------------------
# Filtering — the core of "Option 3 hybrid": filter aggressively up front
# ---------------------------------------------------------------------------

# Regex for dollar amounts like "$1.5M", "$500,000", "$2 million"
_VALUE_PATTERN = re.compile(
    r"\$\s*(\d{1,3}(?:,\d{3})*(?:\.\d+)?)\s*(K|M|B|thousand|million|billion)?",
    re.IGNORECASE,
)
_VALUE_MULTIPLIERS = {
    "K": 1_000, "thousand": 1_000,
    "M": 1_000_000, "million": 1_000_000,
    "B": 1_000_000_000, "billion": 1_000_000_000,
}


def estimate_value(description: str) -> Optional[float]:
    """
    Extract a dollar value from tender description. Imperfect — many tenders
    don't state a value at all, some bury it deep. We just grab the first match.
    """
    if not isinstance(description, str):
        return None
    match = _VALUE_PATTERN.search(description)
    if not match:
        return None
    amount_str, unit = match.group(1), match.group(2)
    value = float(amount_str.replace(",", ""))
    if unit:
        value *= _VALUE_MULTIPLIERS.get(unit.upper() if len(unit) <= 1 else unit.lower(), 1)
    return value


# ---------------------------------------------------------------------------
# LLM value extraction (optional, --extract-values)
# ---------------------------------------------------------------------------
# The regex above grabs the FIRST dollar amount in the description, which is
# often the contract value but sometimes a bond amount, an insurance minimum,
# or an unrelated figure. The LLM pass reads the description and extracts the
# actual estimated contract value, or null if none is stated.
#
# Opt-in by design: the default ingest path needs zero credentials so the
# repo stays clonable-and-runnable. With the flag, set ANTHROPIC_API_KEY.
# Cost at ~300 tenders with a small model: a few cents per ingest.

def make_llm_value_extractor():
    """
    Return a callable(description) -> Optional[float] backed by the Anthropic
    API. Falls back to the regex extractor per-tender on any failure, so a
    flaky network degrades gracefully rather than killing the ingest.
    """
    import json
    import os

    import anthropic

    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit(
            "--extract-values requires the ANTHROPIC_API_KEY environment variable"
        )
    client = anthropic.Anthropic()

    def extract(description: str) -> Optional[float]:
        if not isinstance(description, str) or not description.strip():
            return None
        try:
            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=100,
                messages=[{
                    "role": "user",
                    "content": (
                        "Extract the estimated total contract value in CAD from "
                        "this government tender description. Ignore bond amounts, "
                        "insurance minimums, penalty figures, and per-unit prices. "
                        "Respond with ONLY a JSON object, no other text: "
                        '{"contract_value": <number or null>}\n\n'
                        f"Description:\n{description[:1500]}"
                    ),
                }],
            )
            text = response.content[0].text.strip()
            # Strip accidental code fences before parsing
            text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
            value = json.loads(text).get("contract_value")
            return float(value) if value is not None else None
        except Exception as exc:  # noqa: BLE001 — deliberate broad fallback
            print(f"    LLM extraction failed ({type(exc).__name__}), regex fallback")
            return estimate_value(description)

    return extract


def matched_competencies(text: str, competencies: list[str]) -> list[str]:
    # Word-boundary matching, not bare substring: "aws" must not match inside
    # "flaws", "withdrawals", or the French "travaux". The old substring version
    # inflated the corpus with archaeology and bridge tenders that merely
    # contained the letters a-w-s, and surfaced a $3.75M exhibits contract as a
    # top "AWS" result in the weekly digest. Multi-word competencies like
    # "it modernization" still match as a phrase with boundaries at each end.
    text_lower = text.lower()
    matched = []
    for c in competencies:
        pattern = r"\b" + re.escape(c.lower()) + r"\b"
        if re.search(pattern, text_lower):
            matched.append(c)
    return matched


def contains_excluded(text: str, exclusions: list[str]) -> bool:
    text_lower = text.lower()
    return any(excl in text_lower for excl in exclusions)


def filter_tenders(df: pd.DataFrame, criteria: dict, value_extractor=None) -> pd.DataFrame:
    """
    Apply profile filters. Prints funnel stats so you can tune the profile.

    value_extractor: callable(description) -> Optional[float]. Defaults to the
    regex extractor. Runs after the competency filter on purpose — with
    --extract-values that means a few hundred API calls, not 2,800.
    """
    if value_extractor is None:
        value_extractor = estimate_value
    print(f"\nStarting with {len(df):,} tenders")

    # Parse closing dates (timezone-naive for simplicity)
    df = df.copy()
    df["_closing"] = pd.to_datetime(
        df.get("tenderClosingDate-appelOffresDateCloture"),
        errors="coerce",
        utc=True,
    ).dt.tz_localize(None)

    min_close = datetime.now() + timedelta(days=criteria["min_days_until_close"])
    df = df[df["_closing"] >= min_close]
    print(f"  After date filter (>={criteria['min_days_until_close']} days out): {len(df):,}")

    # Combined text for matching (title + description)
    title_col = "title-titre-eng"
    desc_col = "tenderDescription-descriptionAppelOffres-eng"
    df["_text"] = (
        df.get(title_col, "").fillna("").astype(str) + " "
        + df.get(desc_col, "").fillna("").astype(str)
    )

    # Exclusions first (cheap, removes obvious misfits)
    if criteria["exclude"]:
        mask = ~df["_text"].apply(lambda t: contains_excluded(t, criteria["exclude"]))
        df = df[mask]
        print(f"  After exclusion filter: {len(df):,}")

    # Competency match — the big reducer
    if criteria["competencies"]:
        df["_matched"] = df["_text"].apply(
            lambda t: matched_competencies(t, criteria["competencies"])
        )
        df = df[df["_matched"].apply(len) > 0]
        print(f"  After competency filter: {len(df):,}")
    else:
        df["_matched"] = [[] for _ in range(len(df))]

    # Value filter — only drop tenders where we can read a value AND it's out of range
    if desc_col in df.columns:
        if value_extractor is not estimate_value:
            print(f"  Extracting values via LLM for {len(df):,} tenders...")
        df["_value"] = df[desc_col].apply(value_extractor)
    else:
        df["_value"] = None
    if criteria["value_min"] > 0 or criteria["value_max"] < 100_000_000:
        in_range = df["_value"].isna() | (
            (df["_value"] >= criteria["value_min"])
            & (df["_value"] <= criteria["value_max"])
        )
        df = df[in_range]
        print(f"  After value filter (${criteria['value_min']:,}-${criteria['value_max']:,}): {len(df):,}")

    print(f"\nFinal: {len(df):,} tenders")
    return df


# ---------------------------------------------------------------------------
# ChromaDB persistence — this is what Claude's tools read from
# ---------------------------------------------------------------------------

def build_chroma(df: pd.DataFrame, db_path: Path) -> None:
    """Embed filtered tenders and write to a persistent ChromaDB collection."""
    # Imported here, not at module level: this is the only function that needs
    # ChromaDB, and contracts_ingest.py imports this module purely for its
    # profile parser and HTTP headers.
    import chromadb
    from chromadb.utils import embedding_functions

    # Wipe the old DB — we want a clean snapshot, not accumulated cruft
    if db_path.exists():
        import shutil
        shutil.rmtree(db_path)

    client = chromadb.PersistentClient(path=str(db_path))
    embedder = embedding_functions.SentenceTransformerEmbeddingFunction(
        model_name="all-MiniLM-L6-v2"
    )
    collection = client.create_collection(
        name="tenders",
        embedding_function=embedder,
        metadata={"hnsw:space": "cosine"},
    )

    documents, metadatas, ids = [], [], []
    for _, row in df.iterrows():
        tender_id = str(row.get("referenceNumber-numeroReference", ""))
        if not tender_id or tender_id == "nan":
            continue

        title = str(row.get("title-titre-eng", ""))[:300]
        desc = str(row.get("tenderDescription-descriptionAppelOffres-eng", ""))[:2000]
        # We embed title + description — weighting title higher by repeating it
        document = f"{title}\n{title}\n\n{desc}"

        metadata = {
            "tender_id": tender_id,
            "title": title[:200],
            "agency": str(row.get("contracting-organization-name-eng-nom-organisation-contractante-ang", ""))[:200],
            "closing_date": row["_closing"].strftime("%Y-%m-%d") if pd.notna(row["_closing"]) else "",
            "estimated_value": float(row["_value"]) if pd.notna(row.get("_value")) else 0.0,
            "matched_competencies": ",".join(row.get("_matched", [])),
        }

        documents.append(document)
        metadatas.append(metadata)
        ids.append(tender_id)

    # Batch insert (ChromaDB handles this fine up to several thousand at a time)
    batch = 200
    for i in range(0, len(documents), batch):
        collection.add(
            documents=documents[i:i + batch],
            metadatas=metadatas[i:i + batch],
            ids=ids[i:i + batch],
        )
        print(f"  Embedded {min(i + batch, len(documents)):,} / {len(documents):,}")

    print(f"\nChromaDB written to {db_path} ({collection.count():,} tenders)")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
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

    criteria = parse_profile(args.profile)
    print(f"Profile: {args.profile}")
    print(f"  Competencies: {criteria['competencies']}")
    print(f"  Value range: ${criteria['value_min']:,} – ${criteria['value_max']:,}")
    print(f"  Exclusions: {criteria['exclude']}")

    value_extractor = make_llm_value_extractor() if args.extract_values else None
    if args.extract_values:
        print("  Value extraction: LLM (Anthropic API)")

    df = download_tenders(args.cache, force=args.force)
    df = filter_tenders(df, criteria, value_extractor=value_extractor)

    if len(df) == 0:
        print("\nNo tenders passed the filter. Loosen your criteria.", file=sys.stderr)
        sys.exit(1)

    build_chroma(df, args.db)
    print("\nDone. Claude Code can now search this corpus via scripts/tender_tools.py")


if __name__ == "__main__":
    main()
