"""
Ingest CanadaBuys' HISTORICAL tender and award notice archives into an
append-only SQLite table, so the corpus gains a past.

Data:     https://canadabuys.canada.ca/opendata/pub/{FY}-TenderNotice-AvisAppelOffres.csv
          https://canadabuys.canada.ca/opendata/pub/{FY}-awardNotice-avisAttribution.csv
          plus the two pre-CanadaBuys legacy archives (2009-2022 / 2012-2022)
Licence:  Open Government Licence - Canada.

WHY THIS EXISTS. `ingest.py` reads openTenderNotice — notices open on the day it
ran. It is a snapshot, it is destroyed and rebuilt on every ingest, and no row in
it carries a date saying when it was first seen. So the repo could describe what
is open today and could not answer a single question about the past: the digests
in vault/digests/ are the only longitudinal record and they cover a few weeks.

Moving that ingest to a daily cadence does not change this. It sharpens the
digest chain from weekly to daily resolution, which is worth having, but a
snapshot of what is open still cannot reach back before the day it first ran —
and `first_seen` here remains a fact about when THIS PROJECT looked, never about
when the public could see a row. casebook.py suppresses it for that reason and a
finer-grained stamp does not make it admissible.

The fiscal-year archives fix that, and cheaply. Same host, same WAF (a bare
python-requests UA gets a 403 — REQUEST_HEADERS is required), and the SAME 67
COLUMNS as the live feed, so ingest.py's schema resolution and classifiers apply
unchanged. What they add is `publicationDate`, a real per-record first-seen
stamp, and `tenderStatus` values of Expired/Cancelled instead of the live feed's
uniform Open.

THIS TABLE STORES FACTS, NOT A FILTERED CORPUS. Every federal notice is written,
relevant or not, with the publisher's own classification fields preserved
alongside the resolved organization keys. Two reasons. First, a base rate cannot
be computed from a table that has already dropped the negatives. Second, the
selection predicate belongs in backtest.py where it is frozen and auditable, not
smeared across an ingest — see that module's docstring for the predicate itself.

APPEND-ONLY, DELIBERATELY, and this is the one place this repo does not rebuild
from scratch. `ingest.py` drops its collection every run because it wants a
clean snapshot of the present. This table wants the opposite: it is the
historical record, a notice that has left the archives should not leave this
table, and a re-run must be a no-op on rows it has already seen. Rows are keyed
on (reference_number, amendment_number) and inserted with INSERT OR IGNORE;
`first_seen` records when THIS TABLE first observed a row, which is not
`publication_date` and must never be used in its place — publication_date is the
government's fact, first_seen is ours.

Usage:
    python scripts/notices_ingest.py                      # all fiscal years
    python scripts/notices_ingest.py --fiscal-years 2023-2024,2024-2025
    python scripts/notices_ingest.py --include-legacy     # + the 2009-2022 archives
    python scripts/notices_ingest.py --awards-only
    python scripts/notices_ingest.py --source path/to/local.csv --fiscal-years 2023-2024
"""
from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))

from ingest import (  # noqa: E402  — sys.path is set immediately above
    REQUEST_HEADERS,
    classify_notice,
    entity_org_keys,
    parse_unspsc_codes,
    resolve_columns,
)

PROJECT_ROOT = Path(__file__).parent.parent
DEFAULT_DB = PROJECT_ROOT / "data" / "notices.db"
DEFAULT_CACHE = PROJECT_ROOT / ".cache" / "notices"

_BASE = "https://canadabuys.canada.ca/opendata/pub"

# The fiscal years CanadaBuys publishes as discrete archives. 2022-2023 is the
# first, and it is a partial year: CanadaBuys went live 2022-08-08, so anything
# earlier in that fiscal year is in the legacy file instead. Recorded here
# because a base rate computed over 2022-2023 as though it were a full year is
# wrong by four months of missing notices, and nothing in the file says so.
FISCAL_YEARS = [
    "2022-2023", "2023-2024", "2024-2025", "2025-2026", "2026-2027",
]
PARTIAL_FISCAL_YEARS = {
    "2022-2023": "CanadaBuys launched 2022-08-08; Apr-Jul 2022 is in the legacy archive",
}

LEGACY_TENDER_URL = (
    f"{_BASE}/2009-2022-tenderNoticeHistorical-AvisAppelOffresHistorique.csv"
)
LEGACY_AWARD_URL = (
    f"{_BASE}/2012-2022-awardNoticeHistorical-avisAttributionHistorique.csv"
)


def tender_url(fiscal_year: str) -> str:
    return f"{_BASE}/{fiscal_year}-TenderNotice-AvisAppelOffres.csv"


def award_url(fiscal_year: str) -> str:
    return f"{_BASE}/{fiscal_year}-awardNotice-avisAttribution.csv"


# ---------------------------------------------------------------------------
# Schema resolution
# ---------------------------------------------------------------------------
# Same guard as every other ingest here: resolve against the real header and
# exit(2) with the actual column list rather than silently reading a default.
# These are the live feed's names — the archives use them identically, which is
# the whole reason this module is short.
NOTICE_COLUMNS = {
    "reference_number": ["referenceNumber-numeroReference"],
    "amendment_number": ["amendmentNumber-numeroModification"],
    "solicitation_number": ["solicitationNumber-numeroSollicitation"],
    "publication_date": ["publicationDate-datePublication"],
    "title": ["title-titre-eng"],
    "description": ["tenderDescription-descriptionAppelOffres-eng"],
    "closing_date": ["tenderClosingDate-appelOffresDateCloture"],
    "status": ["tenderStatus-appelOffresStatut-eng"],
    "contracting_entity": ["contractingEntityName-nomEntitContractante-eng"],
    "end_user": ["endUserEntitiesName-nomEntitesUtilisateurFinal-eng"],
    "notice_type": ["noticeType-avisType-eng"],
    "procurement_category": ["procurementCategory-categorieApprovisionnement"],
    "unspsc": ["unspsc"],
    "gsin": ["gsin-nibs"],
}
# Only what the table cannot be keyed or dated without. The classification
# columns stay optional for the same reason ingest.py leaves them optional: they
# are absent for whole source systems, and a feed that stopped publishing them
# should degrade loudly rather than hard-exit.
NOTICE_REQUIRED = ["reference_number", "publication_date", "title"]

# The award file is a WIDER shape than the tender file — 80 columns — and the
# extra ones matter more than they look. `contract_end_date` is an expiry date
# that arrives attached to a notice, which makes it a far better source for an
# "incumbent contract expiring" feature than contracts.csv: it is stated on the
# award itself rather than reconstructed from a disclosure row, and it carries
# the solicitation number that links it back to the notice that produced it.
# `procurement_method` is the competitive/directed split on the notice side.
#
# It shares solicitation_number with the tender file, which is the documented
# join key between the two (PSPC's supporting documentation says so outright),
# and unlike contracts.csv's procurement_id that key actually matches.
AWARD_COLUMNS = {
    "reference_number": ["referenceNumber-numeroReference"],
    "amendment_number": ["amendmentNumber-numeroModification"],
    "solicitation_number": ["solicitationNumber-numeroSollicitation"],
    "contract_number": ["contractNumber-numeroContrat"],
    "publication_date": ["publicationDate-datePublication"],
    "title": ["title-titre-eng"],
    "description": ["awardDescription-descriptionAttribution-eng"],
    "status": ["awardStatus-attributionStatut-eng"],
    "contracting_entity": ["contractingEntityName-nomEntitContractante-eng"],
    "end_user": ["endUserEntitiesName-nomEntitesUtilisateurFinal-eng"],
    "procurement_category": ["procurementCategory-categorieApprovisionnement"],
    "notice_type": ["noticeType-avisType-eng"],
    "procurement_method": ["procurementMethod-methodeApprovisionnement-eng"],
    "instrument_type": ["instrumentType-typeInstrument-eng"],
    "amendment_type": ["amendmentType-typeModification-eng"],
    "unspsc": ["unspsc"],
    "gsin": ["gsin-nibs"],
    "vendor": ["supplierLegalName-nomLegalFournisseur-eng"],
    "award_date": ["contractAwardDate-dateAttributionContrat"],
    "contract_start_date": ["contractStartDate-contratDateDebut"],
    "contract_end_date": ["contractEndDate-dateFinContrat"],
    "contract_value": ["totalContractValue-valeurTotaleContrat"],
    "contract_amount": ["contractAmount-montantContrat"],
}
AWARD_REQUIRED = ["reference_number", "publication_date"]


SCHEMA = """
CREATE TABLE IF NOT EXISTS notices (
    reference_number     TEXT NOT NULL,
    amendment_number     TEXT NOT NULL DEFAULT '',
    solicitation_number  TEXT,
    publication_date     TEXT,          -- the government's fact: when published
    first_seen           TEXT,          -- ours: when this table first saw the row
    fiscal_year          TEXT,
    title                TEXT,
    description          TEXT,
    closing_date         TEXT,
    status               TEXT,
    contracting_entity   TEXT,
    end_user             TEXT,
    notice_type          TEXT,
    procurement_category TEXT,
    unspsc               TEXT,
    gsin                 TEXT,
    -- Derived at ingest, from the SAME classifiers ingest.py uses, so the
    -- historical table and the live corpus cannot drift into disagreeing.
    opportunity_kind     TEXT,
    kind_basis           TEXT,
    org_keys             TEXT,          -- comma-separated canonical keys
    org_key_source       TEXT,          -- end_user | contracting_entity | none
    PRIMARY KEY (reference_number, amendment_number)
);
CREATE INDEX IF NOT EXISTS idx_notices_pub  ON notices(publication_date);
CREATE INDEX IF NOT EXISTS idx_notices_org  ON notices(org_keys);
CREATE INDEX IF NOT EXISTS idx_notices_soln ON notices(solicitation_number);

CREATE TABLE IF NOT EXISTS awards (
    reference_number     TEXT NOT NULL,
    amendment_number     TEXT NOT NULL DEFAULT '',
    solicitation_number  TEXT,
    contract_number      TEXT,
    publication_date     TEXT,
    first_seen           TEXT,
    fiscal_year          TEXT,
    title                TEXT,
    description          TEXT,
    status               TEXT,
    contracting_entity   TEXT,
    end_user             TEXT,
    procurement_category TEXT,
    notice_type          TEXT,
    procurement_method   TEXT,
    instrument_type      TEXT,
    amendment_type       TEXT,
    unspsc               TEXT,
    gsin                 TEXT,
    vendor               TEXT,
    award_date           TEXT,
    contract_start_date  TEXT,
    contract_end_date    TEXT,
    contract_value       REAL,
    contract_amount      REAL,
    org_keys             TEXT,
    org_key_source       TEXT,
    PRIMARY KEY (reference_number, amendment_number)
);
CREATE INDEX IF NOT EXISTS idx_awards_end ON awards(contract_end_date);
CREATE INDEX IF NOT EXISTS idx_awards_pub  ON awards(publication_date);
CREATE INDEX IF NOT EXISTS idx_awards_soln ON awards(solicitation_number);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

-- One row per archive file ever ingested. The append-only table cannot say
-- which years it covers by inspection: a fiscal year with no federal IT notices
-- and a fiscal year never downloaded look identical in `notices`. This is the
-- difference, and backtest.py refuses to build a cohort for a year absent here.
CREATE TABLE IF NOT EXISTS sources (
    fiscal_year   TEXT NOT NULL,
    kind          TEXT NOT NULL,        -- tender | award
    url           TEXT,
    downloaded_at TEXT,
    ingested_at   TEXT,
    rows_read     INTEGER,
    rows_new      INTEGER,
    partial_note  TEXT,
    PRIMARY KEY (fiscal_year, kind)
);
"""


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

def download(url: str, cache_path: Path, force: bool = False) -> Path:
    """
    Fetch an archive to the cache. Archives for closed fiscal years never
    change, so a cached file is reused unconditionally rather than on an age
    check — the freshness question this repo cares about is asked of the live
    feed, not of a file covering 2023.
    """
    if cache_path.exists() and not force:
        mb = cache_path.stat().st_size / (1 << 20)
        print(f"    cached ({mb:.0f} MB) {cache_path.name}")
        return cache_path

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    print(f"    downloading {url.rsplit('/', 1)[-1]} ...", end="", flush=True)
    response = requests.get(url, headers=REQUEST_HEADERS, stream=True, timeout=600)
    response.raise_for_status()

    tmp = cache_path.with_suffix(cache_path.suffix + ".part")
    written = 0
    with open(tmp, "wb") as handle:
        for chunk in response.iter_content(chunk_size=1 << 20):
            handle.write(chunk)
            written += len(chunk)
    tmp.replace(cache_path)
    print(f" {written / (1 << 20):.0f} MB")
    return cache_path


# ---------------------------------------------------------------------------
# Row shaping
# ---------------------------------------------------------------------------

def _clean(value) -> str:
    return (value or "").strip()


def _iso_date(value) -> str:
    """
    Normalize a publication/closing stamp to a bare ISO date.

    The archives carry a mix of `2023-04-03` and `2023-04-03 14:00:00 EDT`.
    Truncating to ten characters is enough for every form seen and keeps the
    column comparable with a plain string inequality, which is what `as_of`
    does. A value that does not start with a four-digit year is returned empty
    rather than guessed at.
    """
    text = _clean(value)
    if len(text) >= 10 and text[:4].isdigit() and text[4] == "-":
        return text[:10]
    return ""


def _fiscal_year(iso_date: str) -> str:
    """Canadian federal fiscal year (April 1 - March 31) as `2023-2024`."""
    if len(iso_date) < 7:
        return ""
    year, month = int(iso_date[:4]), int(iso_date[5:7])
    start = year if month >= 4 else year - 1
    return f"{start}-{start + 1}"


def _org_attribution(end_user: str, contracting: str) -> tuple[str, str]:
    """
    Canonical keys for a notice, end user preferred over contracting entity.

    Same precedence the vault already documents for a tender's `department`
    field: the end user is the customer, the contracting entity is often SSC or
    PSPC buying on someone else's behalf. Returning the source alongside the
    keys keeps that distinction legible downstream instead of collapsing two
    different facts into one column.
    """
    keys = entity_org_keys(end_user)
    if keys:
        return ",".join(sorted(keys)), "end_user"
    keys = entity_org_keys(contracting)
    if keys:
        return ",".join(sorted(keys)), "contracting_entity"
    return "", "none"


def shape_notice(row: dict, cols: dict, seen_at: str) -> dict | None:
    """One archive row to one `notices` row, or None if it cannot be keyed."""
    ref = _clean(row.get(cols["reference_number"]))
    if not ref:
        return None

    published = _iso_date(row.get(cols["publication_date"]))
    title = _clean(row.get(cols["title"])) if cols.get("title") else ""
    description = _clean(row.get(cols["description"])) if cols.get("description") else ""
    notice_type = _clean(row.get(cols["notice_type"])) if cols.get("notice_type") else ""
    category = (_clean(row.get(cols["procurement_category"]))
                if cols.get("procurement_category") else "")

    kind = classify_notice(notice_type or None, category or None,
                           f"{title} {description}")
    end_user = _clean(row.get(cols["end_user"])) if cols.get("end_user") else ""
    contracting = (_clean(row.get(cols["contracting_entity"]))
                   if cols.get("contracting_entity") else "")
    org_keys, org_source = _org_attribution(end_user, contracting)

    return {
        "reference_number": ref,
        "amendment_number": (_clean(row.get(cols["amendment_number"]))
                             if cols.get("amendment_number") else ""),
        "solicitation_number": (_clean(row.get(cols["solicitation_number"]))
                                if cols.get("solicitation_number") else ""),
        "publication_date": published,
        "first_seen": seen_at,
        "fiscal_year": _fiscal_year(published),
        "title": title,
        "description": description,
        "closing_date": (_iso_date(row.get(cols["closing_date"]))
                         if cols.get("closing_date") else ""),
        "status": _clean(row.get(cols["status"])) if cols.get("status") else "",
        "contracting_entity": contracting,
        "end_user": end_user,
        "notice_type": notice_type,
        "procurement_category": category,
        "unspsc": _clean(row.get(cols["unspsc"])) if cols.get("unspsc") else "",
        "gsin": _clean(row.get(cols["gsin"])) if cols.get("gsin") else "",
        "opportunity_kind": kind["opportunity_kind"],
        "kind_basis": kind.get("kind_basis", ""),
        "org_keys": org_keys,
        "org_key_source": org_source,
    }


def _float_or_none(value):
    text = _clean(value).replace(",", "").replace("$", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def shape_award(row: dict, cols: dict, seen_at: str) -> dict | None:
    ref = _clean(row.get(cols["reference_number"]))
    if not ref:
        return None
    published = _iso_date(row.get(cols["publication_date"]))
    end_user = _clean(row.get(cols["end_user"])) if cols.get("end_user") else ""
    contracting = (_clean(row.get(cols["contracting_entity"]))
                   if cols.get("contracting_entity") else "")
    org_keys, org_source = _org_attribution(end_user, contracting)
    text = lambda key: _clean(row.get(cols[key])) if cols.get(key) else ""  # noqa: E731
    date = lambda key: _iso_date(row.get(cols[key])) if cols.get(key) else ""  # noqa: E731
    return {
        "reference_number": ref,
        "amendment_number": text("amendment_number"),
        "solicitation_number": text("solicitation_number"),
        "contract_number": text("contract_number"),
        "publication_date": published,
        "first_seen": seen_at,
        "fiscal_year": _fiscal_year(published),
        "title": text("title"),
        "description": text("description"),
        "status": text("status"),
        "contracting_entity": contracting,
        "end_user": end_user,
        "procurement_category": text("procurement_category"),
        "notice_type": text("notice_type"),
        "procurement_method": text("procurement_method"),
        "instrument_type": text("instrument_type"),
        "amendment_type": text("amendment_type"),
        "unspsc": text("unspsc"),
        "gsin": text("gsin"),
        "vendor": text("vendor"),
        "award_date": date("award_date"),
        "contract_start_date": date("contract_start_date"),
        "contract_end_date": date("contract_end_date"),
        "contract_value": (_float_or_none(row.get(cols["contract_value"]))
                           if cols.get("contract_value") else None),
        "contract_amount": (_float_or_none(row.get(cols["contract_amount"]))
                            if cols.get("contract_amount") else None),
        "org_keys": org_keys,
        "org_key_source": org_source,
    }


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------

def ingest_file(conn: sqlite3.Connection, path: Path, table: str,
                fiscal_year: str, url: str) -> tuple[int, int]:
    """
    Stream one archive into `table`. Returns (rows_read, rows_new).

    INSERT OR IGNORE on the composite key is what makes a re-run a no-op. It
    also means a row already present is NOT updated — deliberate. The archives
    are supposed to be immutable for closed years; if a row's content changed
    upstream, silently overwriting it would erase the evidence that it did.
    Detecting that is a real question and this is not the module that answers
    it, so the safe behaviour is to keep what was first recorded.
    """
    candidates = NOTICE_COLUMNS if table == "notices" else AWARD_COLUMNS
    required = NOTICE_REQUIRED if table == "notices" else AWARD_REQUIRED
    shaper = shape_notice if table == "notices" else shape_award
    seen_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    with open(path, encoding="utf-8-sig", errors="replace", newline="") as handle:
        reader = csv.DictReader(handle)
        cols = resolve_columns(reader.fieldnames or [], candidates, required,
                               f"notices_ingest.py ({table}, {fiscal_year})")

        read = 0
        batch: list[dict] = []
        new = 0
        for raw in reader:
            read += 1
            shaped = shaper(raw, cols, seen_at)
            if shaped is None:
                continue
            batch.append(shaped)
            if len(batch) >= 5000:
                new += _flush(conn, table, batch)
                batch.clear()
                print(f"      {read:,} rows read, {new:,} new", end="\r", flush=True)
        if batch:
            new += _flush(conn, table, batch)

    print(f"      {read:,} rows read, {new:,} new{' ' * 20}")
    conn.execute(
        "INSERT OR REPLACE INTO sources "
        "(fiscal_year, kind, url, downloaded_at, ingested_at, rows_read, rows_new, "
        " partial_note) VALUES (?,?,?,?,?,?,?,?)",
        (fiscal_year,
         "tender" if table == "notices" else "award",
         url,
         datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%dT%H:%M:%S"),
         seen_at, read, new,
         PARTIAL_FISCAL_YEARS.get(fiscal_year, "")),
    )
    conn.commit()
    return read, new


def _flush(conn: sqlite3.Connection, table: str, batch: list[dict]) -> int:
    """Insert a batch, returning how many rows were actually new."""
    if not batch:
        return 0
    fields = list(batch[0].keys())
    placeholders = ",".join("?" * len(fields))
    sql = (f"INSERT OR IGNORE INTO {table} ({','.join(fields)}) "
           f"VALUES ({placeholders})")
    before = conn.total_changes
    conn.executemany(sql, [tuple(row[f] for f in fields) for row in batch])
    return conn.total_changes - before


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Ingest CanadaBuys historical tender and award archives.")
    parser.add_argument("--fiscal-years", default=",".join(FISCAL_YEARS),
                        help="comma-separated, e.g. 2023-2024,2024-2025")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--source", type=Path,
                        help="local CSV instead of downloading (offline/test); "
                             "requires exactly one --fiscal-years value")
    parser.add_argument("--include-legacy", action="store_true",
                        help="also ingest the 2009-2022 / 2012-2022 archives")
    parser.add_argument("--tenders-only", action="store_true")
    parser.add_argument("--awards-only", action="store_true")
    parser.add_argument("--force-download", action="store_true")
    args = parser.parse_args()

    years = [y.strip() for y in args.fiscal_years.split(",") if y.strip()]
    if args.source and len(years) != 1:
        parser.error("--source requires exactly one --fiscal-years value")

    do_tenders = not args.awards_only
    do_awards = not args.tenders_only

    args.db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(args.db)
    conn.executescript(SCHEMA)

    print(f"Ingesting into {args.db}")
    for year in years:
        note = PARTIAL_FISCAL_YEARS.get(year)
        print(f"\n  {year}" + (f"   [partial: {note}]" if note else ""))
        if do_tenders:
            path = (args.source if args.source
                    else download(tender_url(year),
                                  args.cache / f"{year}-tender.csv",
                                  args.force_download))
            ingest_file(conn, path, "notices", year, tender_url(year))
        if do_awards and not args.source:
            path = download(award_url(year), args.cache / f"{year}-award.csv",
                            args.force_download)
            ingest_file(conn, path, "awards", year, award_url(year))

    if args.include_legacy:
        print("\n  legacy archives (pre-CanadaBuys)")
        if do_tenders:
            path = download(LEGACY_TENDER_URL, args.cache / "legacy-tender.csv",
                            args.force_download)
            ingest_file(conn, path, "notices", "2009-2022", LEGACY_TENDER_URL)
        if do_awards:
            path = download(LEGACY_AWARD_URL, args.cache / "legacy-award.csv",
                            args.force_download)
            ingest_file(conn, path, "awards", "2012-2022", LEGACY_AWARD_URL)

    for key, value in [
        ("ingest_date", datetime.now().strftime("%Y-%m-%d")),
        ("licence", "Open Government Licence - Canada"),
        ("notice_count", conn.execute("SELECT COUNT(*) FROM notices").fetchone()[0]),
        ("award_count", conn.execute("SELECT COUNT(*) FROM awards").fetchone()[0]),
    ]:
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)",
                     (key, str(value)))
    conn.commit()

    notices = conn.execute("SELECT COUNT(*) FROM notices").fetchone()[0]
    awards = conn.execute("SELECT COUNT(*) FROM awards").fetchone()[0]
    attributed = conn.execute(
        "SELECT COUNT(*) FROM notices WHERE org_keys != ''").fetchone()[0]
    print(f"\n{notices:,} notices, {awards:,} awards in {args.db}")
    if notices:
        print(f"{attributed:,} notices ({100 * attributed / notices:.0f}%) "
              f"resolved to a canonical organization key")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
