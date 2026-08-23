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
    python scripts/contracts_ingest.py --min-share 0.25             # more agency intel
"""
from __future__ import annotations

import argparse
import itertools
import re
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).parent))
from crosswalk import load_aliases  # noqa: E402
from ingest import parse_profile  # noqa: E402
from ingest_common import (  # noqa: E402
    REQUEST_HEADERS, output_path, resolve_columns, staged_db,
)

CONTRACTS_URL = (
    "https://open.canada.ca/data/dataset/d8f85d91-7dec-4fd1-8055-483b77225d8b/"
    "resource/fac950c0-00d5-4ec1-a4d3-9cbebf98a305/download/contracts.csv"
)
PROJECT_ROOT = Path(__file__).parent.parent
DEFAULT_PROFILE = PROJECT_ROOT / "vault" / "profiles" / "my-company.md"
DB_PATH = PROJECT_ROOT / "data" / "contracts.db"
INTEL_DIR = PROJECT_ROOT / "vault" / "intel" / "agencies"
CROSSWALK_DB = PROJECT_ROOT / "data" / "crosswalk.db"
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


def build_db(records_iter, window_years: int, source_note: str,
             db_path: Path = DB_PATH) -> int:
    # Staged write: nothing touches db_path until the new database is complete.
    # The previous version unlinked the real DB here and then pulled from
    # records_iter — which is where resolve_columns used to run — so a schema
    # mismatch deleted a good 33,196-row database on its way to exiting 2.
    with staged_db(db_path) as con:
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
    return total


# The frontmatter line that marks a file in INTEL_DIR as owned by THIS
# generator. Pruning refuses to touch anything without it, so a file written by
# hand into that directory is never deleted by an ingest.
GENERATED_MARKER = "source: proactive-disclosure-contracts"

# Which departments get an intel file: those holding at least this PERCENTAGE
# of all filtered contract families.
#
# A FLOOR, NOT A RANK, because a rank cannot grow with the data. `top_n = 20`
# returns exactly twenty departments forever, whatever the dataset does; it
# happened to cut at 233 families, a number nobody chose. A floor lets the set
# expand as departments' contracting grows, which is the behaviour asked for.
#
# A SHARE, NOT AN ABSOLUTE COUNT, because family counts are already filtered to
# the `contracts_categories` in vault/profiles/my-company.md. An absolute floor
# silently changes meaning every time that profile is edited — broaden the
# categories and 100 families stops meaning what it did. A share renormalizes
# against whatever the profile currently admits.
#
# 0.5% = 35 departments = 92.4% of filtered volume on the 2026-08-03 extract.
# Chosen to reproduce the ≥100-family cutoff exactly while being drift-proof;
# for reference 1.0% would have given 19 departments, not 35.
#
# CHEAP TO RAISE. write_agency_intel queries every bucket before filtering, so
# this gates file writes and nothing else — no extra queries, no re-download,
# and the files are ~4KB each in a gitignored directory. It also creates no
# obligation on the node side: generated files reference their node in plain
# text, so widening this never manufactures an unresolved link.
#
# WHAT IT DOES COST is churn. The contracts window is rolling
# (`contracts_window_years`), so departments cross the floor in both
# directions, and _reconcile_intel_dir spares any file whose key is still live
# rather than deleting it. Those keep accumulating; they are stamped
# `stale_since` so a stale file can be found later rather than only noticed at
# run time.
DEFAULT_INTEL_MIN_SHARE = 0.5

# Filename suffix separating what this generator owns from what it does not.
#
# THE NAMESPACE SPLIT. A department has two files and they have different
# owners: vault/agencies/<key>.md is the hand-editable department node, created
# on first promote and never overwritten by anything; this generator writes
# vault/intel/agencies/<key>-contracts.md and rewrites it on every run.
#
# Before the split, both roles wanted the same name. The generated file won it,
# which made [[pspc]] resolve to a file that write_text clobbers each run, in a
# directory documented as never-hand-edit, produced by an OPTIONAL ~630MB
# download. A vault whose graph hub can only exist after that download is a
# vault whose graph is empty for anyone who skipped it.
#
# The suffix is what tells the two apart on disk, so pruning keys off it too.
INTEL_SUFFIX = "-contracts"


def _intel_filename(stem: str) -> str:
    """The on-disk name of one generated intel file. One definition."""
    return f"{stem}{INTEL_SUFFIX}.md"


def _intel_key(path: Path) -> str | None:
    """
    The canonical key a generated filename claims, or None if it claims none.

    None means the name is not in current form — either legacy bare-key naming
    (`dnd.md`) or a display slug for an organization the registry cannot name.
    Both are prunable; a key in current form is not.
    """
    if not path.stem.endswith(INTEL_SUFFIX):
        return None
    return path.stem[: -len(INTEL_SUFFIX)] or None


def _display_slug(text: str) -> str:
    """
    The legacy slug of a bilingual display title.

    Retained ONLY for organizations the registry cannot name. Everything it can
    name is filed under the canonical key instead, which is the whole point of
    org_aliases.yaml: `cic` is pre-2015 Citizenship and Immigration, and keying
    intel on the dataset's own strings meant reading a file named for a
    department that stopped existing in 2015.
    """
    return re.sub(r"[-\s]+", "-", re.sub(r"[^\w\s-]", "", text.lower())).strip("-")[:60]


def _slug_to_key() -> dict[str, str]:
    """
    CKAN slug -> canonical key, read once from the crosswalk.

    The same table org_resolve.department_scope reads in the other direction.
    The slug is preferred over the display title because it is the STABLE
    machine key ("cic") while the title is a bilingual display string that moves
    with rebrands and translations. Empty when the crosswalk has not been built,
    which is not an error — the title fallback still resolves most of them.
    """
    if not CROSSWALK_DB.exists():
        return {}
    con = sqlite3.connect(CROSSWALK_DB)
    try:
        return {slug: key for key, slug in con.execute(
            "SELECT canonical_key, contract_owner_org FROM org_crosswalk "
            "WHERE contract_owner_org IS NOT NULL AND contract_owner_org != ''"
        )}
    except sqlite3.OperationalError:
        return {}
    finally:
        con.close()


def _canonical_key(org_title: str, owner_org: str, slug_map: dict[str, str]):
    """
    The registry key for one contracts-dataset organization, or None.

    None is a REAL ANSWER, not a failure. The contracts dataset carries bodies a
    registry of departments and agencies has no key for, and inventing one would
    put a department that does not exist into the vault graph.
    """
    if owner_org and owner_org in slug_map:
        return slug_map[owner_org]
    import org_resolve
    try:
        return org_resolve.default_resolver().resolve(org_title)
    except org_resolve.AmbiguousOrganization:
        # A registry defect, not something to guess through. Fall back to the
        # display slug so the file still gets written and the defect stays
        # visible in the filename.
        return None


def _mark_stale(path: Path, ingest_date: str) -> bool:
    """
    Stamp a kept-but-unrefreshed intel file `stale_since` in its frontmatter.

    IN THE FILE, NOT ONLY ON STDOUT. A run prints what it kept, and that line
    scrolls away; the question "is what I am reading current?" gets asked weeks
    later by someone who never saw the run. `generated:` alone cannot answer it,
    because a file refreshed today and a file last refreshed in March both carry
    a date and nothing distinguishes which is which.

    FIRST DETECTION WINS. If the stamp is already there it is left alone, so the
    field records how long a file has been stale rather than resetting to the
    most recent run. A refreshed file is rewritten wholesale from the template,
    which carries no stale fields, so the stamp clears itself.

    Returns True if it stamped, False if the file was already marked.
    """
    text = path.read_text(encoding="utf-8")
    if re.search(r"^stale_since:", text, re.M):
        return False

    # Insert directly after `generated:`, so the two dates read together: when
    # the figures are from, and when they stopped being refreshed.
    stamped, n = re.subn(
        r"^(generated: .*)$",
        rf"\1\nstale_since: {ingest_date}",
        text, count=1, flags=re.M,
    )
    if not n:
        return False
    path.write_text(stamped, encoding="utf-8", newline="\n")
    return True


def _reconcile_intel_dir(
    written: set[str], canonical_keys: set[str], ingest_date: str
) -> None:
    """
    Prune superseded generated files; stamp the ones kept but not refreshed.

    FOUR CONDITIONS, all required, because deleting a file out of the vault is
    the one operation here that cannot be undone by re-running the ingest:

      1. a direct child of INTEL_DIR ending in .md;
      2. carrying this generator's frontmatter marker — a hand-written file in
         that directory is never ours to delete;
      3. NOT named `<canonical-key>-contracts`. This is what protects a
         department that merely dropped out of this run's top N: those files are
         named in current form and are spared by construction. Any other name is
         superseded naming, a name no future run will ever write again;
      4. not something this run just wrote.

    CONDITION 3 IS KEYED OFF THE SUFFIX, and it has to be. The old rule spared a
    stem that WAS a canonical key, which was correct while the generator wrote
    bare `<key>.md` — and became wrong in both directions the moment it started
    writing `<key>-contracts.md`:

      - `dnd-contracts` is not itself a key, so a department dropping out of the
        top N would have been deleted. That is precisely the loss the old
        condition 3 existed to prevent, inverted by a rename.
      - `dnd.md` from before the rename IS a key, so it would have been spared
        forever — an orphan no run rewrites and no run removes, sitting on the
        name [[dnd]] now belongs to vault/agencies/.

    Reading the key back out of the filename fixes both: the first is spared
    because its prefix is a key, the second is pruned because it carries no
    suffix at all. `_intel_key` is the single definition of that reading.

    Files for organizations the registry cannot name are written under a display
    slug, so their prefix is not a key and they are pruned on dropping out. That
    is unchanged behaviour and intended — an unnamed body has no stable identity
    to keep a stale file under.

    The split also retired a genuine ambiguity this function used to carry:
    `justice-canada` was both a plausible display slug and a real canonical key,
    indistinguishable under bare-key naming and resolved conservatively by
    sparing it. The suffix now separates the two cases outright —
    `justice-canada-contracts` is the generated file, `justice-canada` is the
    department node in vault/agencies/ and not this function's business at all.
    """
    pruned, kept = [], []
    for path in sorted(INTEL_DIR.glob("*.md")):
        if path.name in written:
            continue
        if not path.is_file():
            continue
        if GENERATED_MARKER not in path.read_text(encoding="utf-8", errors="replace"):
            continue                                    # not ours
        if _intel_key(path) in canonical_keys:
            # Dropped below the floor, not orphaned. Kept — but the reader has
            # to be able to tell that from the file itself.
            _mark_stale(path, ingest_date)
            kept.append(path.name)
            continue
        path.unlink()
        pruned.append(path.name)

    # Printed every time. Silent deletion of vault files is exactly the kind of
    # thing that should never have to be discovered afterwards.
    if pruned:
        print(f"Pruned {len(pruned)} superseded intel file(s) "
              f"(not named <canonical-key>{INTEL_SUFFIX}):")
        for name in pruned:
            print(f"  - {name}")
    if kept:
        print(f"Kept {len(kept)} file(s) for departments below this run's floor, "
              f"stamped stale_since {ingest_date}:")
        for name in kept:
            print(f"  - {name}")


def write_agency_intel(
    min_share: float = DEFAULT_INTEL_MIN_SHARE, db_path: Path = DB_PATH
) -> None:
    """
    Generate vault/intel/agencies/<canonical-key>-contracts.md from the DB.

    NAMED BY CANONICAL KEY so this file sits next to the department it describes
    in the graph: [[ircc]] is the department node, [[ircc-contracts]] is what the
    contracts dataset knows about it. Under the old display-slug naming it
    matched neither and sat isolated while the relationship was perfectly real.

    THE SUFFIX IS NOT DECORATION. It keeps this generator off [[ircc]] itself.
    Everything written here is rewritten on every run, lives in a never-hand-edit
    directory, and only exists at all if someone ran the optional ~630MB
    contracts ingest — three properties that disqualify it from being the node a
    tender links to. See INTEL_SUFFIX.

    GROUPED BY KEY for the same reason the name is: the fold puts `cic` and
    `pptc` onto `ircc`, and two organizations mapping to one filename would
    otherwise mean the second silently overwrote the first. The fold is recorded
    in the frontmatter, because a fold whose contribution is invisible is a fold
    the reader cannot check.

    REFUSES TO OVERWRITE WHAT IT DOES NOT OWN. write_text is destructive and
    takes no view on what it lands on, so the marker is checked first. Without
    that check the suffix alone is only a convention, and one hand-written
    `<key>-contracts.md` — or one run predating the split — is enough to lose
    somebody's notes with no error and no trace.
    """
    con = sqlite3.connect(db_path)
    ingest_date = con.execute("SELECT value FROM meta WHERE key='ingest_date'").fetchone()[0]
    slug_map = _slug_to_key()

    # Bucket every organization in the extract by canonical key. Unresolved ones
    # keep their own bucket under the display slug rather than being merged into
    # an "unknown" pile that no reader could take apart again.
    buckets: dict[str, dict] = {}
    for org, owner_org in con.execute(
        "SELECT DISTINCT org, owner_org FROM contracts WHERE org != ''"
    ):
        key = _canonical_key(org, owner_org or "", slug_map)
        bucket = buckets.setdefault(key or f"\x00{_display_slug(org)}", {
            "key": key, "orgs": [], "slugs": []})
        bucket["orgs"].append(org)
        if owner_org and owner_org not in bucket["slugs"]:
            bucket["slugs"].append(owner_org)

    # COUNT(DISTINCT family_id) across the whole bucket, not summed per org: a
    # contract family spanning two folded slugs is one family, not two.
    for bucket in buckets.values():
        placeholders = ",".join("?" * len(bucket["orgs"]))
        bucket["rows"] = con.execute(
            f"SELECT vendor_norm, MAX(value) AS v FROM contracts "
            f"WHERE org IN ({placeholders}) GROUP BY family_id", bucket["orgs"],
        ).fetchall()

    # The floor is a share of what the profile currently admits, computed after
    # every bucket is counted — so it renormalizes when the profile changes
    # rather than silently meaning something different.
    total_families = sum(len(b["rows"]) for b in buckets.values())
    cutoff = total_families * min_share / 100
    ranked = sorted(
        (b for b in buckets.values() if len(b["rows"]) >= cutoff),
        key=lambda b: -len(b["rows"]),
    )
    print(f"Intel floor: {min_share}% of {total_families:,} filtered families "
          f"= {cutoff:,.0f}; {len(ranked)} of {len(buckets)} departments qualify")

    import org_resolve
    resolver = org_resolve.default_resolver()

    INTEL_DIR.mkdir(parents=True, exist_ok=True)
    written: set[str] = set()
    refused: list[str] = []
    for bucket in ranked:
        key, rows = bucket["key"], bucket["rows"]
        n_families = len(rows)
        values = sorted(r[1] for r in rows if r[1] and r[1] > 0)
        median = values[len(values) // 2] if values else 0
        total_value = sum(values)
        vendor_totals: dict[str, float] = {}
        for vendor, v in rows:
            if vendor:
                vendor_totals[vendor] = vendor_totals.get(vendor, 0) + (v or 0)
        top_vendors = sorted(vendor_totals.items(), key=lambda x: -x[1])[:8]

        primary = bucket["orgs"][0]
        stem = key or _display_slug(primary)
        title = resolver.display_name(key) if key else primary

        lines = [
            "---",
            f"canonical_key: {key or 'null'}",
            f'agency: "{title[:150]}"',
            f"ckan_slugs: [{', '.join(bucket['slugs'])}]",
            "contributing_org_titles: ["
            + ", ".join(f'"{o[:150]}"' for o in bucket["orgs"]) + "]",
            f"generated: {ingest_date}",
            GENERATED_MARKER,
            "---",
            "",
            f"# {title}",
            "",
            # Unconditional, and dead on purpose for departments nobody has
            # promoted a tender to yet: the link resolves the moment promote
            # creates the node, and a graph set to hideUnresolved never draws
            # it meanwhile. Where the node does exist this adds no edge — it
            # already links [[key-contracts]] and the graph is undirected — so
            # this is a convenience link, not the thing holding the pair
            # together. Emitting it either way keeps output a function of the
            # database alone, which a state-dependent version would not.
            *([f"Department node: [[{key}]] — where notes on "
               "this department belong. This file is rewritten on every "
               "ingest, so anything written here is lost.", ""] if key else []),
            "Auto-generated from the Proactive Publication of Contracts dataset "
            f"(ingest {ingest_date}, recency window). Unaudited data; vendor names "
            "are lightly normalized (corporate suffixes and punctuation stripped, "
            "but not fuzzy-matched), so counts are directional.",
            "",
            # ONE LINK, ONE PATH, and the asymmetry is the point. The profile
            # link stays: contracts_categories decides what these files contain,
            # so "change the competency list — what does that affect?" has to be
            # answerable from my-company's backlinks. That it lands on all 35 is
            # not a reason to drop it; the star it makes is a display problem,
            # fixed with a graph filter (-file:my-company), not by deleting a
            # real dependency. The dossier pointer is the other case — generic
            # "how to read this" on every file, discriminating nothing. It keeps
            # its links where they mean something: _attribution_note in
            # tender_tools.py emits [[dossier]] only when attribution is weak.
            f"Filtered to the competencies in [[my-company]]. For how this reads "
            f"against the other three signals on a department, see "
            f"`vault/reference/dossier.md`.",
            "",
            f"- **Contract families in our competency space:** {n_families}",
            f"- **Total awarded value:** ${total_value:,.0f}",
            f"- **Median contract value:** ${median:,.0f}",
            "",
        ]
        if len(bucket["orgs"]) > 1:
            lines += [
                "**Folded from more than one dataset organization**, so the "
                "figures above span all of them: "
                + "; ".join(bucket["orgs"]) + ". Read the relation notes in "
                "`vault/crosswalk/org_aliases.yaml` before quoting a number "
                "across a predecessor or an absorbed body.",
                "",
            ]
        if not key:
            lines += [
                "**No canonical key.** The organization registry has no entry for "
                "this body, so this file is named by its display string and no "
                "tender will link to it. A registry miss means *not a department* "
                "— not *not federal*; federal Crown corporations have no entry in "
                "a registry of departments and agencies. See "
                "`vault/reference/dossier.md`.",
                "",
            ]
        lines += ["## Top vendors by value", ""]
        lines += [f"- {v} = ${amt:,.0f}" for v, amt in top_vendors]
        lines.append("")

        # Marker check before write_text, never after: the point is to not
        # destroy the file, and reading it back afterwards is too late.
        target = INTEL_DIR / _intel_filename(stem)
        if target.exists():
            existing = target.read_text(encoding="utf-8", errors="replace")
            if GENERATED_MARKER not in existing:
                refused.append(target.name)
                continue

        target.write_text("\n".join(lines), encoding="utf-8", newline="\n")
        written.add(target.name)
    con.close()

    _reconcile_intel_dir(written, set(load_aliases()), ingest_date)
    print(f"Wrote agency intel for {len(written)} departments to {INTEL_DIR}")

    # Loud, not silent. A refusal means a department has no current intel and
    # the reason is a name collision someone has to resolve by hand — exactly
    # the kind of thing that must not be discovered three weeks later.
    if refused:
        print(f"REFUSED to overwrite {len(refused)} file(s) in {INTEL_DIR} "
              f"lacking this generator's marker ({GENERATED_MARKER!r}):")
        for name in refused:
            print(f"  - {name}")
        print("  Move or delete them by hand if the generated version should win.")


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    parser.add_argument("--source", help="Local CSV path instead of downloading (testing)")
    parser.add_argument("--no-intel", action="store_true", help="Skip agency intel files")
    parser.add_argument(
        "--min-share", type=float, default=DEFAULT_INTEL_MIN_SHARE,
        help=f"Percentage of all filtered contract families a department must "
             f"hold to get an intel file (default {DEFAULT_INTEL_MIN_SHARE}%%, "
             f"about 35 of 88 departments). A share rather than a count, so the "
             f"floor renormalizes when the profile's contracts_categories "
             f"change. Every bucket is queried regardless, so lowering this "
             f"costs writes and nothing else.",
    )
    parser.add_argument("--force", action="store_true", help="Re-download even if cached")
    parser.add_argument(
        "--db", type=Path, default=None,
        help=f"Output DB path (default {DB_PATH}). --source redirects output to a "
             ".sample path unless this is given, so testing on a trimmed CSV "
             "cannot replace the committed database.",
    )
    args = parser.parse_args()

    db_path = output_path(
        DB_PATH, args.db,
        f"--source reads {args.source} instead of the full dataset"
        if args.source else None,
    )
    is_sample = db_path != DB_PATH

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

    # Resolve the schema off the FIRST chunk, before build_db opens anything.
    # This used to happen lazily inside the generator, i.e. after build_db had
    # already deleted the existing database — so a schema mismatch exited 2 and
    # took the good data with it. Fail before writing, not during.
    try:
        first_chunk = next(reader)
    except StopIteration:
        sys.stderr.write(f"Source has no rows: {args.source or CONTRACTS_URL}\n")
        sys.exit(2)
    cols = resolve_columns(
        list(first_chunk.columns), COLUMN_CANDIDATES, REQUIRED_COLUMNS,
        "scripts/contracts_ingest.py",
    )
    print(f"Resolved columns: {cols}")

    state = {"seen": 0}

    def records_gen():
        for chunk in itertools.chain([first_chunk], reader):
            state["seen"] += len(chunk)
            sub = filter_chunk(chunk, cols, matchers, cutoff)
            if len(sub):
                yield to_records(sub, cols)
            if state["seen"] % 500_000 < CHUNK_ROWS:
                print(f"  ...scanned {state['seen']:,} rows")

    source_note = args.source or CONTRACTS_URL
    kept = build_db(records_gen(), window_years, source_note, db_path)
    print(f"\nScanned {state['seen']:,} rows, kept {kept:,} matching contract rows")
    print(f"SQLite written to {db_path} ({db_path.stat().st_size / 1e6:.1f} MB)")

    if kept and not args.no_intel:
        if is_sample:
            # The directory is derived, but that is not a licence to rewrite it
            # from a subset: a sampling run would publish partial figures under
            # the same filenames the full run uses, and would prune the
            # departments the sample happened to miss. Same failure as
            # clobbering the DB — just in markdown, and now with deletions.
            print("Skipping agency intel: sampling run must not rewrite vault/intel/.")
        else:
            write_agency_intel(min_share=args.min_share, db_path=db_path)

    print("\nAttribution: contains information licensed under the "
          "Open Government Licence - Canada.")


if __name__ == "__main__":
    main()
