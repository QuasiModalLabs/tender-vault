"""
Closing dates: the window a notice sits in, and the prose deadline that
contradicts the field.

Both derived, never stored — see closing_window's docstring for why.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Optional

import pandas as pd


# Past this horizon a closing date is a placeholder meaning "this arrangement
# has no real close", not a date. 2065-04-27, 2076-12-31 and 2100-12-31 all
# appear in the live feed on standing arrangements. Rendering them as closing
# dates makes a permanent vehicle look like an imminent deadline, and computing
# a days-until figure from one yields a meaningless five-digit integer.
#
# Defined here rather than in tender_tools because both modules need it and the
# import already runs that way (tender_tools imports parse_profile from here).
SENTINEL_HORIZON_YEARS = 10


def closing_window(closing_date, imminent_within_days: int, today=None):
    """
    Classify a closing date into a window. Returns `(window, days_until_close)`.

    DERIVED, NEVER STORED. Both outputs depend on what day it is, so freezing
    them into ChromaDB at ingest would make them wrong by up to a week before
    the next run. The corpus keeps `closing_date`; this is computed against it
    at query time, which also means the threshold can change with no re-ingest.

    The five windows, and why each exists:

    - `closed`    — the date has passed. Only reachable from a corpus older than
                    today, which is the normal case: a briefing written three
                    days after an ingest needs to see that something expired.
    - `imminent`  — inside the profile threshold. These used to be deleted at
                    ingest; they are here now so the reader decides.
    - `open`      — comfortably open.
    - `standing`  — past SENTINEL_HORIZON_YEARS. A placeholder, not a deadline,
                    so days_until_close is None rather than a five-digit number.
    - `unknown`   — no parseable date. Not the same as closed, and not the same
                    as standing.

    `days_until_close` is None for `standing` and `unknown` — the two cases where
    an integer would be a fabrication.
    """
    if today is None:
        today = datetime.now().date()
    elif hasattr(today, "date") and not isinstance(today, date):
        today = today.date()

    if closing_date is None or closing_date == "":
        return "unknown", None
    try:
        if isinstance(closing_date, str):
            parsed = datetime.strptime(closing_date[:10], "%Y-%m-%d").date()
        elif hasattr(closing_date, "date"):
            if pd.isna(closing_date):
                return "unknown", None
            parsed = closing_date.date()
        else:
            parsed = closing_date
    except (ValueError, TypeError):
        return "unknown", None

    if parsed.year - today.year > SENTINEL_HORIZON_YEARS:
        return "standing", None

    days = (parsed - today).days
    if days < 0:
        return "closed", days
    if days < imminent_within_days:
        return "imminent", days
    return "open", days


def body_date_conflict(description, closing_date) -> Optional[str]:
    """
    A submission date stated in the prose that is EARLIER than the field.

    Notices amended on a third-party portal routinely carry "disregard the
    Ariba posting deadline, bids are due <date>" while the structured closing
    date still shows the original. Believing the field costs the bid, and
    nothing about it fails loudly. Only an earlier body date is reported: a
    later one is usually an extension already reflected elsewhere, and flagging
    those would bury the case that matters.
    """
    if not isinstance(description, str) or not closing_date:
        return None
    matches = _BODY_DATE.findall(description)
    if not matches:
        return None
    try:
        closing = datetime.strptime(str(closing_date)[:10], "%Y-%m-%d")
    except ValueError:
        return None
    for raw in matches:
        for fmt in ("%d %B %Y", "%B %d, %Y", "%B %d %Y"):
            try:
                stated = datetime.strptime(raw.strip().replace(",", " ").replace("  ", " "),
                                           fmt.replace(",", ""))
            except ValueError:
                continue
            # One day of slack absorbs timezone wording, not a real conflict.
            if (closing - stated).days > 1:
                return stated.strftime("%Y-%m-%d")
            break
    return None


# Only dates introduced as a submission deadline. A bare date anywhere in the
# prose is usually an amendment log or a period of performance.
_BODY_DATE = re.compile(
    r"(?:submitted|received|due|submission)\s+(?:on|by|no later than)\s+"
    r"(\d{1,2}\s+[A-Z][a-z]+\s+\d{4}|[A-Z][a-z]+\s+\d{1,2},?\s+\d{4})",
    re.IGNORECASE,
)
