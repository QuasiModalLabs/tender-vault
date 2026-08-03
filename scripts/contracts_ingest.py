"""
Ingest the Government of Canada Proactive Publication of Contracts dataset
into a small local SQLite database, filtered to the user's profile.

Data: https://open.canada.ca/data/en/dataset/d8f85d91-7dec-4fd1-8055-483b77225d8b
Licence: Open Government Licence - Canada (redistribution permitted with attribution).

Design notes, honestly stated:

STREAMING. The full CSV is very large (millions of rows since 2017). We never
hold it in memory or store it on disk: it's read in chunks straight off the
HTTP response, each chunk filtered, and only survivors persisted. Disk cost is
the final SQLite file (typically tens of MB), not the source data.

WINDOWING. A contract is kept if EITHER its award date OR its delivery-period
end falls within the last N years: effective_date = max(award, period_end).
This captures two things at once: recent AWARDS (who won what lately, the market
picture) and still-ACTIVE older contracts (live incumbents). The dataset skews
heavily toward completed historical contracts, so a strict "still active today"
period-overlap filter leaves almost nothing — but a recently-awarded contract
that already ended is still exactly the competitive signal we want. N comes from
`contracts_window_years` in the profile frontmatter (default 3).

COMPETENCY MATCHING. Word-boundary regex, not bare substring. The tender
ingest's substring matching taught us that "aws" happily matches "flaws".

AMENDMENTS. The dataset records amendments as separate rows sharing a
procurement id. We store all matching rows tagged with a family id; the query
tool aggregates per family using each family's highest value. Caveat: if only
some rows of a family match the filter, the family is partially represented.
Documented, not hidden.

DATA QUALITY. The dataset is unaudited. Vendor names are inconsistent
("IBM Canada Ltd." vs "IBM CANADA LIMITED"); Tier-1 normalization strips
corporate suffixes and punctuation to collapse the obvious duplicates, but
does NOT fuzzy-match (which would risk merging genuinely distinct firms). The
raw name is kept for display, a normalized form is used for aggregation. Counts
remain directional intelligence, not accounting.

Usage:
    python scripts/contracts_ingest.py
    python scripts/contracts_ingest.py --source path/to/local.csv   # offline/test
    python scripts/contracts_ingest.py --no-intel                   # skip intel files
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).parent))
from ingest import REQUEST_HEADERS, parse_profile, resolve_columns  # noqa: E402

CONTRACTS_URL = (
    "https://open.canada.ca/data/dataset/d8f85d91-7dec-4fd1-8055-483b77225d8b/"
    "resource/fac950c0-00d5-4ec1-a4d3-9cbebf98a305/download/contracts.csv"
)
PROJECT_ROOT = Path(__file__).parent.parent
DEFAULT_PROFILE = PROJECT_ROOT / "vault" / "profiles" / "my-company.md"
DB_PATH = PROJECT_ROOT / "data" / "contracts.db"
INTEL_DIR = PROJECT_ROOT / "vault" / "intel" / "agencies"
CACHE_PATH = PROJECT_ROOT / ".cache" / "contracts.csv"
CHUNK_ROWS = 50_000

# Schema column names with fallbacks, resolved at runtime so a rename fails
# loudly with the real column list rather than silently producing nothing.
# resolve_columns itself lives in ingest.py — one guard, shared by all four
# ingest scripts.
COLUMN_CANDIDATES = {
    "vendor": ["vendor_name"],
    # Left as a fallback list on purpose: the display title when present, the
    # slug when it isn't.
    "org": ["owner_org_title", "owner_org"],
    # The CKAN slug, captured SEPARATELY. As a fallback behind owner_org_title
    # it was never once stored, because the title is always present. It's the
    # stable machine key ("casdo-ocena") while the title is a bilingual display
    # string ("Accessibility Standards Canada | Normes d'accessibilité Canada")
    # that changes with rebrands and translations.
    "owner_org": ["owner_org"],
    "contract_date": ["contract_date"],
    "period_start": ["contract_period_start"],
    "period_end": ["delivery_date", "contract_period_end"],
    "value": ["contract_value"],
    "description": ["description_en", "description"],
    "procurement_id": ["procurement_id"],
    "reference": ["reference_number"],
}


# owner_org is deliberately NOT required: an older CSV snapshot passed via
# --source predates the column and should still ingest rather than exit, the
# same way procurement_id and reference are treated.
REQUIRED_COLUMNS = ["vendor", "org", "contract_date", "value", "description"]


def build_matchers(terms: list[str]) -> list[tuple[str, str]]:
    """
    Case-insensitive SUBSTRING matchers on procurement category labels.

    Unlike the tender ingest (which word-boundary matches prose competencies),
    contracts describe work as standardized category phrases like "Information
    technology and telecommunications consultants". We match configured
    category fragments as substrings so one term catches every casing variant.
    """
    return [(term, term.lower()) for term in terms]


def download_to_cache(force: bool = False, max_retries: int = 6) -> Path:
    """
    Download the contracts CSV to .cache/ first, then filtering reads from disk.

    RESUMABLE + AUTO-RETRY. This file is ~630 MB from an Azure blob endpoint
    that drops residential connections mid-stream. A plain download restarts
    from zero on every drop and may never finish. Instead we:
      - stream into a .part file,
      - on any network error, retry with an HTTP Range request that resumes
        from the bytes already on disk (server supports byte ranges),
      - only rename .part -> .csv once the download is verifiably COMPLETE
        (byte count matches Content-Length), so a partial file can never be
        mistaken for a finished one.

    The last point matters: an earlier version left a .part behind on failure
    and a subsequent run filtered that partial fragment, silently producing a
    corrupt corpus. A .part is now never usable input.
    """
    if not force and CACHE_PATH.exists():
        age_h = (datetime.now().timestamp() - CACHE_PATH.stat().st_mtime) / 3600
        size_mb = CACHE_PATH.stat().st_size / 1e6
        if age_h < 24 and size_mb > 1:
            print(f"Using cached contracts.csv ({age_h:.1f}h old, {size_mb:.0f} MB)")
            return CACHE_PATH

    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CACHE_PATH.with_suffix(".csv.part")
    if force and tmp.exists():
        tmp.unlink()  # a forced fresh download starts clean

    print("Downloading contracts.csv from open.canada.ca (resumable)...")
    total = None

    for attempt in range(1, max_retries + 1):
        have = tmp.stat().st_size if tmp.exists() else 0
        headers = dict(REQUEST_HEADERS)
        mode = "wb"
        if have > 0:
            headers["Range"] = f"bytes={have}-"  # resume from where we stopped
            mode = "ab"
            print(f"\n  resuming from {have/1e6:.0f} MB (attempt {attempt})")

        try:
            with requests.get(CONTRACTS_URL, headers=headers, stream=True,
                              timeout=(30, 180)) as resp:
                # 206 = partial (resume accepted); 200 = full (server ignored Range)
                if have > 0 and resp.status_code == 200:
                    # Server won't resume; restart cleanly rather than corrupt.
                    have, mode = 0, "wb"
                    print("  server ignored resume; restarting from 0")
                resp.raise_for_status()

                if total is None:
                    clen = int(resp.headers.get("Content-Length", 0))
                    total = (clen + have) if resp.status_code == 206 else clen

                done = have
                last_pct = -1
                with open(tmp, mode) as f:
                    for chunk in resp.iter_content(chunk_size=1 << 20):
                        if not chunk:
                            continue
                        f.write(chunk)
                        done += len(chunk)
                        if total:
                            pct = int(done * 100 / total)
                            if pct != last_pct:
                                bar = "#" * (pct // 4) + "-" * (25 - pct // 4)
                                print(f"\r  [{bar}] {pct:3d}%  {done/1e6:6.0f} MB",
                                      end="", flush=True)
                                last_pct = pct
            # Stream ended without error. Verify completeness before accepting.
            final = tmp.stat().st_size
            if total and final < total:
                raise IOError(f"incomplete: {final} of {total} bytes")
            print()
            tmp.replace(CACHE_PATH)
            print(f"Saved {CACHE_PATH.stat().st_size/1e6:.0f} MB to {CACHE_PATH}")
            return CACHE_PATH

        except (requests.RequestException, IOError) as exc:
            wait = min(2 ** attempt, 30)
            print(f"\n  drop on attempt {attempt}/{max_retries} "
                  f"({type(exc).__name__}); retrying in {wait}s...")
            import time
            time.sleep(wait)

    raise SystemExit(
        f"Download failed after {max_retries} attempts. The partial file is kept "
        f"at {tmp} — re-run to resume from where it stopped."
    )


def open_source(source: str | None):
    """Return a pandas chunk reader over the local CSV (cached download or --source)."""
    path = source if source else str(download_to_cache())
    if not source:
        print("Filtering cached file in chunks...")
    return pd.read_csv(path, chunksize=CHUNK_ROWS, low_memory=False,
                       on_bad_lines="skip", encoding="utf-8")


def filter_chunk(chunk: pd.DataFrame, cols: dict, matchers,
                 cutoff: datetime) -> pd.DataFrame:
    """Apply competency filter and keep rows within the recency window."""
    desc = chunk[cols["description"]].fillna("").astype(str).str.lower()

    def matched_terms(text: str) -> str:
        return ",".join(term for term, needle in matchers if needle in text)

    terms = desc.map(matched_terms)
    keep = terms != ""
    if not keep.any():
        return pd.DataFrame()

    sub = chunk[keep].copy()
    sub["_matched"] = terms[keep]

    # effective date = max(award, period_end). See the long note below for why
    # this beats strict period-overlap: it captures recent awards (market shape)
    # AND old-but-active contracts (live incumbency) in one comparison.
    if cols["period_end"]:
        end = pd.to_datetime(sub[cols["period_end"]], errors="coerce")
    else:
        end = pd.Series(pd.NaT, index=sub.index)
    awarded = pd.to_datetime(sub[cols["contract_date"]], errors="coerce")
    effective = pd.concat([awarded, end], axis=1).max(axis=1)

    return sub[effective >= cutoff]


def normalize_vendor(name: str) -> str:
    """
    Tier-1 vendor normalization: deterministic cleanup, NOT fuzzy matching.

    Collapses the common duplicate pattern where the same company appears with
    different corporate suffixes / punctuation / bilingual tails, e.g.
    "TEKSYSTEMS CANADA CORP." and "TEKSYSTEMS CANADA CORP./SOCIÉTÉ" both become
    "TEKSYSTEMS". This catches the majority of obvious dupes without the
    false-merge risk of similarity-threshold clustering (which would wrongly
    merge genuinely distinct firms like "SI Systems" and "Systems Inc").

    Deliberately conservative: when in doubt it leaves names apart rather than
    merging them, because a wrong merge silently corrupts the intelligence while
    a missed merge only mildly fragments the long tail.
    """
    if not name:
        return ""
    s = name.upper()
    # Drop the French half of a bilingual "ENGLISH / FRANÇAIS" name
    s = re.split(r"\s*/\s*", s)[0]
    # Remove punctuation, collapse whitespace
    s = re.sub(r"[.,'\"()]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    # Strip trailing corporate suffixes (repeatedly, e.g. "CANADA LTD")
    suffixes = {
        "INC", "LTD", "LTEE", "LTÉE", "CORP", "CORPORATION", "ULC", "LP", "LLP",
        "SOCIETE", "SOCIÉTÉ", "CO", "COMPANY", "LIMITED", "INCORPORATED",
        "CANADA", "AND", "&",
    }
    parts = s.split(" ")
    while parts and parts[-1] in suffixes:
        parts.pop()
    result = " ".join(parts).strip()
    # Never normalize a name completely away
    return result or s


def to_records(sub: pd.DataFrame, cols: dict) -> list[tuple]:
    def _s(x, limit: int) -> str:
        """NaN-safe string conversion. float('nan') is truthy, so `or ''` fails."""
        if x is None or (isinstance(x, float) and pd.isna(x)):
            return ""
        return str(x)[:limit]

    records = []
    for idx, row in sub.iterrows():
        proc_id = _s(row.get(cols["procurement_id"]), 100) if cols["procurement_id"] else ""
        ref = _s(row.get(cols["reference"]), 100) if cols["reference"] else ""
        family = proc_id or ref or f"row-{idx}"
        try:
            raw_v = row.get(cols["value"])
            value = 0.0 if pd.isna(raw_v) else float(raw_v)
        except (TypeError, ValueError):
            value = 0.0
        records.append((
            family,
            ref,
            _s(row.get(cols["vendor"]), 200),
            normalize_vendor(_s(row.get(cols["vendor"]), 200)),
            _s(row.get(cols["org"]), 200),
            _s(row.get(cols["owner_org"]), 100) if cols["owner_org"] else "",
            _s(row.get(cols["description"]), 1000),
            _s(row.get(cols["contract_date"]), 10),
            _s(row.get(cols["period_start"]), 10) if cols["period_start"] else "",
            _s(row.get(cols["period_end"]), 10) if cols["period_end"] else "",
            value,
            row["_matched"],
        ))
    return records


def build_db(records_iter, window_years: int, source_note: str) -> int:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if DB_PATH.exists():
        DB_PATH.unlink()
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE contracts (
            family_id TEXT, reference TEXT, vendor TEXT, vendor_norm TEXT, org TEXT,
            owner_org TEXT, description TEXT, contract_date TEXT, period_start TEXT,
            period_end TEXT, value REAL, matched_terms TEXT
        )
    """)
    con.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    total = 0
    for records in records_iter:
        con.executemany("INSERT INTO contracts VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", records)
        total += len(records)
    con.execute("CREATE INDEX idx_vendor ON contracts(vendor_norm)")
    con.execute("CREATE INDEX idx_org ON contracts(org)")
    for k, v in [
        ("ingest_date", datetime.now().strftime("%Y-%m-%d")),
        ("window_years", str(window_years)),
        ("source", source_note),
        ("row_count", str(total)),
        ("licence", "Open Government Licence - Canada"),
    ]:
        con.execute("INSERT INTO meta VALUES (?, ?)", (k, v))
    con.commit()
    con.close()
    return total


def write_agency_intel(top_n: int = 10) -> None:
    """Generate vault/intel/agencies/<org>.md from the freshly built DB."""
    con = sqlite3.connect(DB_PATH)
    ingest_date = con.execute("SELECT value FROM meta WHERE key='ingest_date'").fetchone()[0]
    orgs = con.execute("""
        SELECT org, COUNT(DISTINCT family_id) AS n
        FROM contracts WHERE org != '' GROUP BY org ORDER BY n DESC LIMIT ?
    """, (top_n,)).fetchall()

    INTEL_DIR.mkdir(parents=True, exist_ok=True)
    for org, n_families in orgs:
        rows = con.execute("""
            SELECT vendor_norm, MAX(value) AS v FROM contracts
            WHERE org = ? GROUP BY family_id
        """, (org,)).fetchall()
        values = sorted(r[1] for r in rows if r[1] and r[1] > 0)
        median = values[len(values) // 2] if values else 0
        total_value = sum(values)
        vendor_totals: dict[str, float] = {}
        for vendor, v in rows:
            if vendor:
                vendor_totals[vendor] = vendor_totals.get(vendor, 0) + (v or 0)
        top_vendors = sorted(vendor_totals.items(), key=lambda x: -x[1])[:8]

        slug = re.sub(r"[-\s]+", "-", re.sub(r"[^\w\s-]", "", org.lower())).strip("-")[:60]
        lines = [
            "---",
            f'agency: "{org[:150]}"',
            f"generated: {ingest_date}",
            "source: proactive-disclosure-contracts",
            "---",
            "",
            f"# {org}",
            "",
            "Auto-generated from the Proactive Publication of Contracts dataset "
            f"(ingest {ingest_date}, recency window). Unaudited data; vendor names "
            "are lightly normalized (corporate suffixes and punctuation stripped, "
            "but not fuzzy-matched), so counts are directional.",
            "",
            f"- **Contract families in our competency space:** {n_families}",
            f"- **Total awarded value:** ${total_value:,.0f}",
            f"- **Median contract value:** ${median:,.0f}",
            "",
            "## Top vendors by value",
            "",
        ]
        lines += [f"- {v} = ${amt:,.0f}" for v, amt in top_vendors]
        lines.append("")
        (INTEL_DIR / f"{slug}.md").write_text("\n".join(lines), encoding="utf-8")
    con.close()
    print(f"Wrote agency intel for {len(orgs)} departments to {INTEL_DIR}")


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    parser.add_argument("--source", help="Local CSV path instead of downloading (testing)")
    parser.add_argument("--no-intel", action="store_true", help="Skip agency intel files")
    parser.add_argument("--force", action="store_true", help="Re-download even if cached")
    args = parser.parse_args()

    criteria = parse_profile(args.profile)
    window_years = criteria.get("contracts_window_years", 3)
    # Contracts describe work as procurement CATEGORIES, not prose, so they need
    # their own vocabulary. Fall back to competencies only if categories unset.
    categories = criteria.get("contracts_categories") or criteria["competencies"]
    if not categories:
        sys.stderr.write("Profile has no contracts_categories or competencies.\n")
        sys.exit(1)

    cutoff = datetime.now() - timedelta(days=365 * window_years)
    matchers = build_matchers(categories)
    print(f"Window: last {window_years} years (cutoff {cutoff:%Y-%m-%d})")
    print(f"Contract categories (substring): {categories}")

    if args.force and not args.source:
        download_to_cache(force=True)
    reader = open_source(args.source)
    state = {"cols": None, "seen": 0}

    def records_gen():
        for chunk in reader:
            if state["cols"] is None:
                state["cols"] = resolve_columns(
                    list(chunk.columns), COLUMN_CANDIDATES, REQUIRED_COLUMNS,
                    "scripts/contracts_ingest.py",
                )
                print(f"Resolved columns: {state['cols']}")
            state["seen"] += len(chunk)
            sub = filter_chunk(chunk, state["cols"], matchers, cutoff)
            if len(sub):
                yield to_records(sub, state["cols"])
            if state["seen"] % 500_000 < CHUNK_ROWS:
                print(f"  ...scanned {state['seen']:,} rows")

    source_note = args.source or CONTRACTS_URL
    kept = build_db(records_gen(), window_years, source_note)
    print(f"\nScanned {state['seen']:,} rows, kept {kept:,} matching contract rows")
    print(f"SQLite written to {DB_PATH} ({DB_PATH.stat().st_size / 1e6:.1f} MB)")

    if kept and not args.no_intel:
        write_agency_intel()

    print("\nAttribution: contains information licensed under the "
          "Open Government Licence - Canada.")


if __name__ == "__main__":
    main()
