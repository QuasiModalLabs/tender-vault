"""
Reading the company profile at query time, and the closing-window vocabulary.

Every threshold here is read per call rather than cached at import: editing
vault/profiles/my-company.md takes effect on the next query, which is what
docs/PROFILE.md promises.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

# Single definition, in ingest.py, imported here. It used to be declared in both
# modules with the same value and the same comment — which is fine until one of
# them is tuned and the corpus and the dossier start disagreeing about what a
# placeholder date is.
from ingest import SENTINEL_HORIZON_YEARS as _SENTINEL_HORIZON_YEARS

from . import paths


def _profile_expiry_min_value() -> float:
    """Value floor for expiring contracts, from the company profile frontmatter."""
    try:
        import sys as _sys
        _sys.path.insert(0, str(Path(__file__).parent))
        from ingest import parse_profile
        return parse_profile(paths.PROFILE).get("expiry_min_value", 0)
    except Exception:
        return 0


def _profile_imminence_threshold() -> int:
    """
    Days-until-close below which a notice is `imminent`, from the profile.

    Read at call time, mirroring _profile_expiry_min_value: the window is
    derived per query rather than stored, so retuning the threshold takes effect
    on the next command with no re-ingest.
    """
    try:
        import sys as _sys
        _sys.path.insert(0, str(Path(__file__).parent))
        from ingest import parse_profile
        return int(parse_profile(paths.PROFILE).get("imminent_within_days", 5))
    except Exception:
        return 5


def _window_fields(meta: dict) -> dict:
    """
    `closing_window` and `days_until_close` for a corpus notice, computed now.

    Deliberately not stored in ChromaDB. Both values depend on today's date, and
    a corpus is read for days after it is built — a stored `imminent`
    would still say `imminent` after the notice had closed. Computing here means
    a briefing written three days after an ingest sees the truth on the day it
    is written, including notices that expired in between.
    """
    from ingest import closing_window
    window, days = closing_window(
        meta.get("closing_date", ""), _profile_imminence_threshold()
    )
    return {"closing_window": window, "days_until_close": days}


def _months_until(date_str: str, today: datetime) -> int | None:
    try:
        return round((datetime.strptime(date_str, "%Y-%m-%d") - today).days / 30)
    except (ValueError, TypeError):
        return None



# The CanadaBuys open-notice feed. The dossier reads the raw feed rather than
# the profile-filtered corpus, so it keeps its own column map; ingest.py reads
# most of these into ChromaDB but not the notice URL.
_TENDER_COLS = {
    "tender_id": "referenceNumber-numeroReference",
    "title": "title-titre-eng",
    "closing": "tenderClosingDate-appelOffresDateCloture",
    "contracting": "contractingEntityName-nomEntitContractante-eng",
    "end_user": "endUserEntitiesName-nomEntitesUtilisateurFinal-eng",
    "notice_type": "noticeType-avisType-eng",
    "procurement_category": "procurementCategory-categorieApprovisionnement",
    # Read for classification only, never rendered — the dossier lists notices,
    # it doesn't reproduce them. Without it classify_notice cannot see the
    # prose signals, and the dossier would call a TBIPS call-up open work.
    "description": "tenderDescription-descriptionAppelOffres-eng",
    "url": "noticeURL-URLavis-eng",
}


def _profile_corpus_ids() -> set[str]:
    """
    Tender ids in the profile-filtered ChromaDB corpus, read straight out of
    Chroma's SQLite rather than through the client, so the dossier never pays
    for the embedding model just to answer "is this one we already track?".
    """
    import sqlite3
    db = paths.active_db() / "chroma.sqlite3"
    if not db.exists():
        return set()
    con = sqlite3.connect(db)
    try:
        return {
            r[0] for r in con.execute(
                "SELECT string_value FROM embedding_metadata WHERE key = 'tender_id'"
            ) if r[0]
        }
    except sqlite3.OperationalError:
        return set()
    finally:
        con.close()

