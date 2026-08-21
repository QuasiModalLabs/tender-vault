"""
Build the initial golden set from real rows. Run once; the output is committed.

ORDER MATTERS AND IS ENFORCED HERE. The deterministic strata are drawn FIRST, at
a recorded seed, from the whole corpus - before any review exists. If the set
were assembled from reviewed rejects alone it would be the population the filter
is known to get wrong, and recall measured on it would be recall over a set
built to fail.

NOTHING IS FABRICATED. The four named cases are documented in the repository or
were found by replaying the corpus, and each one was verified against the live
predicates before being written here.
"""
from __future__ import annotations

import random
import sqlite3
from pathlib import Path

import pandas as pd
import yaml

import ingest

from . import predicates as P
from .golden import FROZEN_FIELDS, GOLDEN_FILE, row_sha256

PROJECT_ROOT = Path(__file__).parent.parent.parent
NOTICES_DB = PROJECT_ROOT / "data" / "notices.db"
FEED_CSV = PROJECT_ROOT / ".cache" / "tenders.csv"
DEFAULT_PROFILE = PROJECT_ROOT / "vault" / "profiles" / "my-company.md"

SEED = 20260820
STRATUM_N = 6      # per deterministic stratum


def _archive_row(conn, reference_number: str) -> dict:
    row = conn.execute("SELECT * FROM notices WHERE reference_number=?",
                       (reference_number,)).fetchone()
    if row is None:
        raise KeyError(reference_number)
    return {k: row[k] for k in FROZEN_FIELDS if k in row.keys()}


def _feed_row(df, cols, reference_number: str) -> dict:
    match = df[df[cols["tender_id"]].astype(str) == reference_number]
    if match.empty:
        raise KeyError(reference_number)
    row = match.iloc[0]

    def value(key):
        name = cols.get(key)
        if not name:
            return None
        raw = row.get(name)
        return None if pd.isna(raw) else str(raw)

    return {
        "reference_number": reference_number,
        "title": value("title"),
        "description": value("description"),
        "closing_date": value("closing_date"),
        "publication_date": None,
        "contracting_entity": value("contracting_entity"),
        "end_user": value("end_user"),
        "notice_type": value("notice_type"),
        "procurement_category": value("procurement_category"),
        "unspsc": value("unspsc"),
        "gsin": value("gsin"),
    }


def _entry(entry_id, frozen_row, source, klass, expected, as_of, rationale,
           provenance, failure_categories=None):
    return {
        "id": entry_id,
        "reference_number": frozen_row["reference_number"],
        "source": source,
        "class": klass,
        "expected": expected,
        "as_of": as_of,
        "failure_categories": failure_categories or [],
        "provenance": provenance,
        "rationale": rationale.strip(),
        "frozen_row": frozen_row,
        "frozen_row_sha256": row_sha256(frozen_row),
    }


def build() -> dict:
    criteria = ingest.parse_profile(DEFAULT_PROFILE)
    conn = sqlite3.connect(f"file:{NOTICES_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    feed = pd.read_csv(FEED_CSV, dtype=str, low_memory=False)
    fcols = ingest.resolve_columns(feed, ingest.TENDER_COLUMNS,
                                   ingest.TENDER_REQUIRED, "tenders")

    entries = []

    # --- deterministic strata, drawn BEFORE any review ---------------------
    # Rule, recorded so it can be re-run: over notices published in FY2023-2024
    # with a parseable closing date, evaluated at their own publication date,
    # take the first STRATUM_N admitted and the first STRATUM_N rejected in a
    # shuffle seeded with SEED. No human looked at these before they were drawn.
    rows = conn.execute(
        "SELECT * FROM notices WHERE fiscal_year='2023-2024' "
        "ORDER BY reference_number").fetchall()
    rng = random.Random(SEED)
    order = list(range(len(rows)))
    rng.shuffle(order)

    admitted_pool, rejected_pool = [], []
    for index in order:
        row = rows[index]
        notice = P.Notice.from_archive_row(row)
        as_of = notice.publication_date
        if not as_of:
            continue
        decision = P.production_decision(notice, criteria, as_of)
        if decision.admitted and len(admitted_pool) < STRATUM_N:
            admitted_pool.append((row, as_of))
        elif not decision.admitted and len(rejected_pool) < STRATUM_N:
            rejected_pool.append((row, as_of, decision.first_rejecting_stage))
        if len(admitted_pool) >= STRATUM_N and len(rejected_pool) >= STRATUM_N:
            break

    for position, (row, as_of) in enumerate(admitted_pool, 1):
        frozen = {k: row[k] for k in FROZEN_FIELDS if k in row.keys()}
        entries.append(_entry(
            f"gold-relevant-{position:02d}", frozen, "archive",
            "clearly_relevant", "ADMIT", as_of,
            "Drawn by the deterministic FY2023-2024 rule at seed 20260820, "
            "before any review existed. Admitted by the filter at its own "
            "publication date; retained as a regression anchor so a refinement "
            "that starts rejecting real IT work is visible.",
            {"added": "2026-08-20", "added_by": "deterministic_draw",
             "seed": SEED, "rule": "FY2023-2024, shuffled at seed, first admitted"}))

    for position, (row, as_of, stage) in enumerate(rejected_pool, 1):
        frozen = {k: row[k] for k in FROZEN_FIELDS if k in row.keys()}
        entries.append(_entry(
            f"gold-irrelevant-{position:02d}", frozen, "archive",
            "clearly_irrelevant", "REJECT", as_of,
            f"Drawn by the same deterministic rule and seed, before any review. "
            f"Rejected at `{stage}`. These are the entries a recall-chasing "
            f"refinement breaks first, which is exactly why they are here and "
            f"why they were not chosen by anyone.",
            {"added": "2026-08-20", "added_by": "deterministic_draw",
             "seed": SEED, "rule": "FY2023-2024, shuffled at seed, first rejected"}))

    # --- the four documented cases -----------------------------------------
    ssc = _archive_row(conn, "SSC-22-00019111:T")
    entries.append(_entry(
        "gold-ssc-cyber-security-itq", ssc, "archive",
        "known_false_negative", "ADMIT", "2022-04-29",
        """
Shared Services Canada is the largest federal IT buyer and files no UNSPSC, so
its notices can only ever reach the keyword branch. The profile carries the
one-word `cybersecurity`; the notice says "Cyber Security". matched_competencies
matches on word boundaries, so \\bcybersecurity\\b cannot reach it.

Measured over archive rows where parse_unspsc_codes returns empty - the same
definition the filter uses - 35 say "cyber security" and 12 say "cybersecurity".
The profile picked the minority spelling. A narrower unspsc='*' query gives 8
and misses 174 null/empty rows, which is why the count is recorded with the
query that produced it.

This entry must ADMIT. A variant that admits it by widening the keyword list to
anything is not a fix - see gold-canadapost-cloud-in-url for the cost.
""",
        # Cites the replay and the briefing, NOT a review. The judgement on this
        # notice was formed while building the audit - long before it could be
        # put through the blinded surface - so recording it as a blinded review
        # would claim more than happened. The evidence here is the measurement
        # and the documented case, which is stronger anyway.
        {"added": "2026-08-20", "added_by": "replay",
         "found_by": "predicate-independent replay of data/notices.db",
         "corroborated_by": "vault/briefings/briefing-2026-08-17.md"},
        ["vocabulary_mismatch"]))

    elevator = _archive_row(conn, "PW-23-01030114")
    entries.append(_entry(
        "gold-nrc-elevator-modernization", elevator, "archive",
        "clearly_irrelevant", "REJECT", "2023-03-13",
        """
The precision guard for every keyword-widening refinement, and the clearest
demonstration in the set of why predicates are evaluated independently.

Uncoded, so it reaches the keyword branch, and `modernization` HITS - relevance
evaluates to pass. Production never asks, because the construction stage rejects
it first. The audit records `construction: drop` and `relevance: pass` on the
same notice, which is information the production funnel structurally cannot
report.

Must REJECT. A refinement that reorders or weakens the construction stage will
admit an elevator contract and this entry is what catches it.
""",
        {"added": "2026-08-20", "added_by": "replay",
         "note": "Found by predicate-independent replay, 2026-08-20."}))

    # as_of is the date the behaviour was OBSERVED, not today. The briefing that
    # recorded both of these ran on 2026-08-17, when both notices were open.
    # Dating them today would reject cb-564-37642506 at stage 1 - it closed on
    # 2026-08-18 - and the entry would then test the closed predicate instead of
    # the relevance failure it exists to pin.
    observed_on = "2026-08-17"

    canada_post = _feed_row(feed, fcols, "MX-443978767209")
    entries.append(_entry(
        "gold-canadapost-cloud-in-url", canada_post, "feed",
        "known_false_positive", "REJECT", observed_on,
        """
Canada Post RFSA for Electrical Services, admitted because `cloud` matched
inside the Ariba portal hostname portal.us.bn.cloud.ariba.com and nowhere else.

Recorded in vault/briefings/briefing-2026-08-17.md, which flagged it on two
consecutive briefings and said the fix is NOT to prune `cloud` but to stop
matching keywords inside URLs - then declined to make the change for want of a
way to evaluate it. This entry is that way.

Frozen inline because it exists only in .cache/tenders.csv, which the next
download overwrites.
""",
        {"added": "2026-08-20", "added_by": "vault_briefing",
         "source_file": "vault/briefings/briefing-2026-08-17.md"},
        ["semantic_context_mismatch"]))

    fintrac = _feed_row(feed, fcols, "cb-564-37642506")
    entries.append(_entry(
        "gold-fintrac-industry-day", fintrac, "feed",
        "known_false_negative", "ADMIT", observed_on,
        """
FINTRAC "Industry Day Solution", flagged on three consecutive briefings and
found only via the FINTRAC dossier's feed scan.

Rejected on the CODED branch, which makes it a different failure from the SSC
case even though both are false negatives. The publisher filed 83110000
(telecom) and 80161502 (management support), neither in the profile families -
and because codes are present, the keyword branch is never consulted. Widening
the competency list cannot recover this notice. That distinction is why
family_result and keyword_result are recorded separately.

Frozen inline: feed-only, and the feed is overwritten on every download.
""",
        {"added": "2026-08-20", "added_by": "vault_briefing",
         "source_file": "vault/briefings/briefing-2026-08-17.md"},
        ["structured_field_error"]))

    conn.close()
    return {
        "schema_version": 1,
        "set_version": 1,
        "frozen": "2026-08-20",
        "deterministic_seed": SEED,
        "entries": entries,
    }


HEADER = """# Golden evaluation set for the tender filter.
#
# WHAT THIS IS FOR. Comparing two filter versions on exactly the same entries,
# so a refinement can be judged rather than believed. Precision and recall are
# reported as two separate observed numbers with intervals - never combined into
# one figure, because a single number is what lets "admits more" pass for
# "is better".
#
# EVERY ENTRY FREEZES ITS RAW ROW. Feed-sourced entries have no other home
# (.cache/tenders.csv is overwritten on every download) and archive-sourced ones
# need it too, because data/notices.db is gitignored and 120MB and this set must
# evaluate on a fresh clone. `verify-golden` compares frozen rows against the
# archive when it is present and REPORTS disagreement rather than resolving it.
#
# HOW IT WAS BUILT, in order. The clearly_relevant and clearly_irrelevant strata
# were drawn first, by a deterministic rule at seed 20260820, before any review
# existed - so the set is not merely the population the filter is known to fail.
# The four named cases were then added from the repository's own documented
# evidence and from predicate-independent replay. Nothing here is invented.
#
# EVERY ENTRY CARRIES ITS OWN `as_of`. Evaluating a 2022 notice against today's
# clock rejects it at stage 1 and measures nothing.
#
# Editing an entry changes what every past evaluation meant. Add entries; do not
# rewrite them.
"""


def write(path: Path = None) -> Path:
    path = Path(path or GOLDEN_FILE)
    data = build()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        HEADER + yaml.safe_dump(data, sort_keys=False, default_flow_style=False,
                                allow_unicode=True, width=88),
        encoding="utf-8", newline="\n")
    return path


if __name__ == "__main__":
    written = write()
    print(f"wrote {written}")
