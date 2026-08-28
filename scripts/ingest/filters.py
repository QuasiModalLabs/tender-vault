"""
The core of "Option 3 hybrid": filter aggressively up front, so retrieval runs
over a focused corpus.
"""
from __future__ import annotations

import re
from datetime import datetime

import pandas as pd

from .classify import parse_unspsc_codes
from .classify import classify_notice
from .dates import body_date_conflict, closing_window
from .jurisdiction import classify_jurisdiction
from .schema import UNCODED_SOURCE_SYSTEMS
from .value import estimate_value


def matched_competencies(text: str, competencies: list[str]) -> list[str]:
    # Word-boundary matching, not bare substring: "aws" must not match inside
    # "flaws", "withdrawals", or the French "travaux". The old substring version
    # inflated the corpus with archaeology and bridge tenders that merely
    # contained the letters a-w-s, and surfaced a $3.75M exhibits contract as a
    # top "AWS" result in the digest. Multi-word competencies like
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


def matches_unspsc_families(codes: set[str], families: list[str]) -> list[str]:
    """
    Return the profile families a notice's UNSPSC codes fall under.

    Prefix match against the hand-checked family list: '8111' catches every
    8111xxxx code. Families are committed config, discovered offline with
    scripts/unspsc_discover.py — this never touches the PSPC reference file at
    runtime, and never the GSIN linkage at all.
    """
    if not families:
        return []
    hits = {fam for code in codes for fam in families if code.startswith(fam)}
    return sorted(hits)


def _source_system(tender_id) -> str:
    """The publishing system's prefix on a reference number: MX, PW, SSC, WS, cb."""
    m = re.match(r"^([A-Za-z]+)", str(tender_id or ""))
    return m.group(1) if m else "?"


def filter_tenders(df: pd.DataFrame, criteria: dict, cols: dict,
                   value_extractor=None) -> pd.DataFrame:
    """
    Apply profile filters. Prints a funnel so you can tune the profile.

    Relevance is decided by the publisher's UNSPSC classification where there is
    one, and falls back to keyword matching where there isn't. The fallback is
    not a nicety: three source systems file no UNSPSC at all (MX, PW, SSC — 139
    of 896 notices on the 2026-08-04 feed), and one of them is Shared Services
    Canada, the largest federal IT buyer. Gating on UNSPSC would silently drop
    every SSC notice. Every run prints the live split so a fourth uncoded
    system, or SSC starting to publish codes, shows up here rather than months on.

    value_extractor: callable(description) -> Optional[float], or None. None
    means no value is extracted and none is stored — see estimate_value.
    """
    print(f"\nStarting with {len(df):,} tenders")

    # Parse closing dates (timezone-naive for simplicity)
    df = df.copy()
    df["_closing"] = pd.to_datetime(
        df[cols["closing_date"]],
        errors="coerce",
        utc=True,
    ).dt.tz_localize(None)

    # Drop only what is dead. Everything still open enters and is tagged; the
    # reader decides whether a near-close notice is worth acting on.
    #
    # TWO BUGS LIVED IN THE THREE LINES THIS REPLACES, and both were invisible:
    #
    # 1. It dropped everything closing within `min_days_until_close`, which ran
    #    BEFORE body_date_conflict below — so a notice closing in eight days was
    #    never read for a conflicting prose deadline, and a tender in watching/
    #    vanished from the corpus in its final days. It also made the briefing's
    #    7-day "act now" section unreachable against a 10-day cutoff.
    #
    # 2. It compared `datetime.now() + timedelta(...)` against a full timestamp,
    #    so the cutoff was a wall-clock INSTANT, not a date. Notices closing at
    #    14:00 on the boundary day survived a 09:00 ingest and were dropped by a
    #    15:00 one — same feed, same day, 53 notices versus 48. Corpus size was
    #    not reproducible within a day, which quietly put noise into every
    #    week-over-week diff and every gating denominator recorded from them.
    #
    # Hence `.dt.normalize()` on both sides: "ten days out" is a count of days,
    # never a time of day.
    today = pd.Timestamp(datetime.now().date())
    closing_day = df["_closing"].dt.normalize()

    # NaT is not "closed". Comparison against NaT is False either way, so the
    # old expression dropped undated notices as a side effect of the arithmetic
    # rather than as a decision. There are none in the current feed; that is a
    # property of today's data, not a guarantee. Keep them and say so.
    undated = closing_day.isna()
    before = len(df)
    df = df[undated | (closing_day >= today)]
    closed = before - len(df)
    if closed:
        print(f"  After dropping closed notices: {len(df):,}  ({closed:,} already closed)")
    if int(undated.sum()):
        print(f"    {int(undated.sum()):,} notice(s) have no parseable closing date — "
              f"kept, tagged unknown")

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

    # --- Instrument shape, from the publisher's structured fields -----------
    # Classify BEFORE filtering on relevance, so the funnel can report the mix
    # and so construction can be dropped on the publisher's own category rather
    # than on words in a title.
    nt_col = cols.get("notice_type")
    cat_col = cols.get("procurement_category")
    df["_kind"] = [
        classify_notice(
            row[nt_col] if nt_col else None,
            row[cat_col] if cat_col else None,
            row["_text"],
        )
        for _, row in df.iterrows()
    ]
    df["_opportunity_kind"] = df["_kind"].apply(lambda k: k["opportunity_kind"])

    # Jurisdiction, from the organization registry. Not a keyword list — the
    # registry is the authority on what is a federal organization, and a miss
    # is reported as unrecognised rather than assumed non-federal.
    df["_jurisdiction"] = [
        classify_jurisdiction(
            row[cols["contracting_entity"]], row[cols["end_user"]]
        )
        for _, row in df.iterrows()
    ]

    # A submission deadline in the prose that is earlier than the closing date
    # field. Rare and costly: believing the field loses the bid.
    df["_date_conflict"] = [
        body_date_conflict(
            row[cols["description"]],
            row["_closing"].strftime("%Y-%m-%d") if pd.notna(row["_closing"]) else None,
        )
        for _, row in df.iterrows()
    ]

    # Construction is dropped outright. procurementCategory is populated on 100%
    # of notices across every source system, which makes this the one filter
    # here that never falls back to guessing.
    before = len(df)
    df = df[df["_opportunity_kind"] != "construction"]
    if before != len(df):
        print(f"  After construction drop (*CNST): {len(df):,}  "
              f"({before - len(df):,} dropped)")

    # Provincial and territorial notices are dropped; `unrecognised` is NOT,
    # because federal Crown corporations land there and one of them is the best
    # tender in the corpus.
    before = len(df)
    juris = df["_jurisdiction"].apply(lambda j: j["jurisdiction"])
    dropped_names = sorted({
        str(v) for v in df.loc[juris == "non_federal", cols["contracting_entity"]]
    })
    df = df[juris != "non_federal"]
    if before != len(df):
        print(f"  After non-federal drop: {len(df):,}  "
              f"({before - len(df):,} dropped — {'; '.join(dropped_names)[:70]})")

    # --- Relevance: publisher classification, else keywords -----------------
    unspsc_col = cols.get("unspsc")
    families = criteria["unspsc_families"]
    df["_unspsc"] = (df[unspsc_col].apply(parse_unspsc_codes) if unspsc_col
                     else [set() for _ in range(len(df))])
    df["_unspsc_families"] = df["_unspsc"].apply(
        lambda codes: matches_unspsc_families(codes, families)
    )
    df["_matched"] = df["_text"].apply(
        lambda t: matched_competencies(t, criteria["competencies"])
    )

    has_codes = df["_unspsc"].apply(bool)
    coded_n, uncoded_n = int(has_codes.sum()), int((~has_codes).sum())
    print(f"  UNSPSC present for {coded_n:,}/{len(df):,}; "
          f"{uncoded_n:,} classified via procurementCategory + keywords")
    if uncoded_n:
        uncoded_systems = (
            df.loc[~has_codes, cols["tender_id"]].apply(_source_system)
            .value_counts().to_dict()
        )
        known = ", ".join(f"{k} {v}" for k, v in sorted(uncoded_systems.items()))
        print(f"    uncoded source systems: {known}")
        surprises = set(uncoded_systems) - set(UNCODED_SOURCE_SYSTEMS)
        if surprises:
            print(f"    NOTE: {sorted(surprises)} newly uncoded — not in "
                  f"UNCODED_SOURCE_SYSTEMS. Worth a look.")
        for expected in UNCODED_SOURCE_SYSTEMS:
            if expected not in uncoded_systems:
                print(f"    NOTE: {expected} now files UNSPSC codes. Its notices "
                      f"are classified by the publisher rather than by keyword.")

    # Publisher first, keywords only where the publisher classified nothing.
    #
    # NOT an OR across everything. When a notice carries UNSPSC codes, the
    # publisher has already said what commodity it is, and a word match that
    # overrides them is exactly the guessing this filter exists to stop: on the
    # 2026-08-04 feed the OR form readmitted a boiling-liquid-expanding-vapour-
    # explosion study (coded 77101501, environmental) because the phrase
    # "vapour cloud" contains "cloud", plus an elevator modernization and an
    # advertising RFSA. Codes present and not ours means not ours.
    #
    # Where no codes were filed there is nothing to defer to, so keywords carry
    # those notices — 37 of 431, entirely from MX, PW and SSC.
    if families or criteria["competencies"]:
        by_family = df["_unspsc_families"].apply(bool)
        by_keyword = df["_matched"].apply(bool)
        relevant = (has_codes & by_family) | (~has_codes & by_keyword)
        df = df[relevant]
        kept_coded = int((has_codes & relevant).sum())
        kept_uncoded = int((~has_codes & relevant).sum())
        print(f"  After relevance filter: {len(df):,}  "
              f"({kept_coded:,} by UNSPSC family, {kept_uncoded:,} by keyword "
              f"where no codes were filed)")

    # --- Value: retired from the default path -------------------------------
    # No step is printed when nothing is filtered. A funnel line that always
    # passes everything reads as a step that decided something, and this one
    # decided nothing for months while every stored value was 0.0.
    if value_extractor is None:
        df["_value"] = None
        present = int(df[cols["description"]].apply(estimate_value).notna().sum())
        print(f"  Value present in {present:,}/{len(df):,}; filter inactive "
              f"(unreliable — see estimate_value; --extract-values to enable)")
    else:
        print(f"  Extracting values for {len(df):,} tenders...")
        df["_value"] = df[cols["description"]].apply(value_extractor)
        if criteria["value_min"] > 0 or criteria["value_max"] < 100_000_000:
            in_range = df["_value"].isna() | (
                (df["_value"] >= criteria["value_min"])
                & (df["_value"] <= criteria["value_max"])
            )
            df = df[in_range]
            print(f"  After value filter (${criteria['value_min']:,}-"
                  f"${criteria['value_max']:,}): {len(df):,}")

    kinds = df["_opportunity_kind"].value_counts().to_dict()
    print(f"\nFinal: {len(df):,} tenders")
    print("  by instrument shape: "
          + ", ".join(f"{k} {v}" for k, v in sorted(kinds.items(), key=lambda x: -x[1])))

    # How each call-up was recovered. Reported separately because the three
    # bases are not equally strong and the weakest one carries the most rows.
    bases = df.loc[df["_opportunity_kind"] == "call_up", "_kind"].apply(
        lambda k: k["kind_basis"]).value_counts().to_dict()
    if bases:
        print("  call-ups by evidence: "
              + ", ".join(f"{k} {v}" for k, v in sorted(bases.items())))

    juris = df["_jurisdiction"].apply(lambda j: j["jurisdiction"]).value_counts().to_dict()
    print("  by jurisdiction: "
          + ", ".join(f"{k} {v}" for k, v in sorted(juris.items(), key=lambda x: -x[1])))

    # A SNAPSHOT, and labelled as one. The window is derived at query time from
    # closing_date, so this line describes the corpus on the day it was built
    # and drifts afterwards. Printed because the imminent count is the number
    # that used to be silently deleted, and it should be visible every run.
    threshold = criteria["imminent_within_days"]
    windows = df["_closing"].apply(
        lambda c: closing_window(c, threshold)[0]
    ).value_counts().to_dict()
    order = ["imminent", "open", "standing", "unknown", "closed"]
    print(f"  closing window at ingest (<{threshold} days = imminent): "
          + ", ".join(f"{k} {windows[k]}" for k in order if k in windows))

    conflicts = df["_date_conflict"].notna().sum()
    if conflicts:
        print(f"  DATE CONFLICTS: {conflicts} notice(s) state a submission "
              f"deadline in the description EARLIER than the closing_date field")
        for _, row in df[df["_date_conflict"].notna()].iterrows():
            print(f"    body says {row['_date_conflict']}, field says "
                  f"{row['_closing'].strftime('%Y-%m-%d')} — "
                  f"{str(row[cols['title']])[:52]}")
    return df
