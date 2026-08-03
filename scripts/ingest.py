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
import contextlib
import re
import shutil
import sqlite3
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
# Schema resolution — the guard, shared with the other ingest scripts
# ---------------------------------------------------------------------------
# Column names with fallbacks, resolved at runtime against the real header so a
# rename fails loudly with the actual column list instead of silently producing
# nothing. This exists because it didn't: the agency field read a column name
# that had never been in this file, `df.get(col, "")` returned the default, and
# every tender carried an empty agency for weeks without a single error.
TENDER_COLUMNS = {
    "tender_id": ["referenceNumber-numeroReference"],
    "title": ["title-titre-eng"],
    "description": ["tenderDescription-descriptionAppelOffres-eng"],
    "closing_date": ["tenderClosingDate-appelOffresDateCloture"],
    "contracting_entity": ["contractingEntityName-nomEntitContractante-eng"],
    "end_user": ["endUserEntitiesName-nomEntitesUtilisateurFinal-eng"],
}
# All of them: there is no tender worth indexing without any one of these.
TENDER_REQUIRED = list(TENDER_COLUMNS)


def resolve_columns(
    columns: list[str],
    candidates: dict[str, list[str]],
    required: list[str],
    source_label: str,
) -> dict[str, str | None]:
    """
    Map logical field names to real header names, or exit(2) with the real list.

    Shared by every ingest script. Matching is case-insensitive so a header
    recased upstream doesn't count as a rename. Keys not in `required` resolve
    to None and the caller decides what an absent column means.
    """
    lower = {c.lower().strip(): c for c in columns}
    resolved: dict[str, str | None] = {}
    for key, cands in candidates.items():
        found = None
        for cand in cands:
            if cand.lower().strip() in lower:
                found = lower[cand.lower().strip()]
                break
        resolved[key] = found
    missing = [k for k in required if resolved[k] is None]
    if missing:
        sys.stderr.write(
            f"Schema mismatch in {source_label}. Could not find columns for: {missing}\n"
            "Columns present in the file:\n"
            + "\n".join(f"  {c}" for c in columns)
            + f"\nUpdate the column candidates in {source_label}.\n"
        )
        sys.exit(2)
    return resolved


# ---------------------------------------------------------------------------
# Output safety — shared by every ingest script
# ---------------------------------------------------------------------------

def output_path(default_path: Path, explicit: Path | None,
                sampling_reason: str | None) -> Path:
    """
    Decide where an ingest writes, keeping sampling runs off the real database.

    A flag whose purpose is to spot-check the pipeline on a subset must not be
    able to replace the corpus the rest of the repo reads. `--max-audits 20`
    truncated a 364-row oag.db to 20; `--source <trimmed csv>` emptied a
    33,196-row contracts.db. Both "worked" — they just destroyed the real data
    on the way. Sampling now redirects to a sibling .sample path, and
    overwriting the committed database takes an explicit --db.
    """
    if explicit is not None:
        return Path(explicit)
    if not sampling_reason:
        return default_path
    sample = default_path.with_name(default_path.stem + ".sample" + default_path.suffix)
    print(
        f"SAMPLING RUN ({sampling_reason}).\n"
        f"  Writing to     {sample}\n"
        f"  Leaving intact {default_path}\n"
        f"  Pass --db {default_path} to overwrite the real one deliberately."
    )
    return sample


@contextlib.contextmanager
def staged_db(db_path: Path):
    """
    Yield a SQLite connection to a .part file, published over db_path on success.

    Every ingest used to unlink its output and then build in place, so anything
    that failed in between — a schema mismatch, a dropped connection, a bad row
    — left no database at all. Here nothing touches db_path until the new
    database is complete and committed; os.replace is atomic for files on
    Windows and POSIX alike.

    On failure the .part is removed as well, so a failed run leaves the tree
    exactly as it found it rather than a stray half-written file for the next
    person to wonder about.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = db_path.with_name(db_path.name + ".part")
    tmp_path.unlink(missing_ok=True)
    con = sqlite3.connect(tmp_path)
    try:
        yield con
        con.commit()
    except BaseException:
        con.close()
        tmp_path.unlink(missing_ok=True)
        raise
    con.close()
    tmp_path.replace(db_path)


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
        "plan_themes": fm.get("plan_themes", {}),
        "oag_themes": fm.get("oag_themes", {}),
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


def filter_tenders(df: pd.DataFrame, criteria: dict, cols: dict,
                   value_extractor=None) -> pd.DataFrame:
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
        df[cols["closing_date"]],
        errors="coerce",
        utc=True,
    ).dt.tz_localize(None)

    min_close = datetime.now() + timedelta(days=criteria["min_days_until_close"])
    df = df[df["_closing"] >= min_close]
    print(f"  After date filter (>={criteria['min_days_until_close']} days out): {len(df):,}")

    # Combined text for matching (title + description)
    df["_text"] = (
        df[cols["title"]].fillna("").astype(str) + " "
        + df[cols["description"]].fillna("").astype(str)
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
    if value_extractor is not estimate_value:
        print(f"  Extracting values via LLM for {len(df):,} tenders...")
    df["_value"] = df[cols["description"]].apply(value_extractor)
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

def _meta_str(value, limit: int) -> str:
    """NaN-safe string for ChromaDB metadata. float('nan') is truthy, so the
    obvious `str(x) or ''` yields the literal string 'nan'."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    return str(value).strip()[:limit]


def build_chroma(df: pd.DataFrame, db_path: Path, cols: dict) -> None:
    """Embed filtered tenders and write to a persistent ChromaDB collection."""
    # Imported here, not at module level: this is the only function that needs
    # ChromaDB, and contracts_ingest.py imports this module purely for its
    # profile parser and HTTP headers.
    import chromadb
    from chromadb.utils import embedding_functions

    # We want a clean snapshot, not accumulated cruft — but the old corpus is
    # moved ASIDE rather than deleted, so a failure part-way through the build
    # doesn't leave us with nothing.
    #
    # Why aside rather than the usual build-to-temp-and-rename: ChromaDB holds
    # OS-level handles on its directory for the life of the client, so renaming
    # a freshly built temp directory into place fails on Windows with
    # PermissionError (verified). Renaming the OLD directory works, because
    # nothing has it open yet.
    retired = None
    if db_path.exists():
        retired = db_path.with_name(db_path.name + ".old")
        if retired.exists():
            shutil.rmtree(retired)
        db_path.rename(retired)

    try:
        _write_chroma(df, db_path, cols)
    except BaseException:
        if retired is not None:
            # Best effort: clear whatever partial exists and put the old corpus
            # back. ChromaDB may still hold handles on a partial build, in which
            # case the cleanup fails and we hand the user the two paths instead
            # of pretending we recovered.
            shutil.rmtree(db_path, ignore_errors=True)
            if not db_path.exists():
                retired.rename(db_path)
                sys.stderr.write(
                    f"\nIngest failed. Your previous corpus has been restored:\n"
                    f"  {db_path}\n"
                )
            else:
                sys.stderr.write(
                    f"\nIngest failed. Your previous corpus was NOT deleted:\n"
                    f"  {retired}\n"
                    f"An incomplete build is at {db_path} and is still held open by\n"
                    f"ChromaDB, so this process cannot swap the old one back itself.\n"
                    f"To restore:  rm -rf {db_path} && mv {retired} {db_path}\n"
                )
        raise
    if retired is not None:
        shutil.rmtree(retired, ignore_errors=True)


def _write_chroma(df: pd.DataFrame, db_path: Path, cols: dict) -> None:
    """Embed and write. Split out so build_chroma owns the rollback logic."""
    import chromadb
    from chromadb.utils import embedding_functions

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
        tender_id = str(row.get(cols["tender_id"], ""))
        if not tender_id or tender_id == "nan":
            continue

        title = str(row.get(cols["title"], ""))[:300]
        desc = str(row.get(cols["description"], ""))[:2000]
        # We embed title + description — weighting title higher by repeating it
        document = f"{title}\n{title}\n\n{desc}"

        metadata = {
            "tender_id": tender_id,
            "title": title[:200],
            # TWO fields, deliberately not merged and not collapsed to one.
            # Federal IT is routinely bought by a central authority (SSC, PSPC)
            # on behalf of the department that actually needs the work, so the
            # contracting entity is frequently NOT the customer. End user is the
            # demand signal; contracting entity is the fallback — it's always
            # populated, while end user is blank on roughly half the rows.
            "contracting_entity": _meta_str(row.get(cols["contracting_entity"]), 500),
            # Multi-valued and slash-delimited: one tender can legitimately name
            # several departments ("Department of National Defence (DND) /
            # Department of Transport (TC) / ..."). Stored VERBATIM, all values
            # kept. Do not re-join on commas — entity names contain commas
            # ("Foreign Affairs, Trade And Development (Department Of)"), which
            # would make the field unsplittable downstream.
            "end_user_entity": _meta_str(row.get(cols["end_user"]), 500),
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
    print(f"  Competencies: {criteria['competencies']}")
    print(f"  Value range: ${criteria['value_min']:,} – ${criteria['value_max']:,}")
    print(f"  Exclusions: {criteria['exclude']}")

    value_extractor = make_llm_value_extractor() if args.extract_values else None
    if args.extract_values:
        print("  Value extraction: LLM (Anthropic API)")

    df = download_tenders(args.cache, force=args.force)
    cols = resolve_columns(
        list(df.columns), TENDER_COLUMNS, TENDER_REQUIRED, "scripts/ingest.py"
    )
    df = filter_tenders(df, criteria, cols, value_extractor=value_extractor)

    if len(df) == 0:
        print("\nNo tenders passed the filter. Loosen your criteria.", file=sys.stderr)
        sys.exit(1)

    build_chroma(df, db_path, cols)
    print("\nDone. Claude Code can now search this corpus via scripts/tender_tools.py")


if __name__ == "__main__":
    main()
