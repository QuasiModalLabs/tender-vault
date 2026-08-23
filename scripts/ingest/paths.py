"""
Where the tender ingest reads from and writes to.

THE ONLY OWNER of these paths. Read them as `paths.DEFAULT_DB` at call time
rather than taking a `from .paths import DEFAULT_DB` copy — a copy is a second
binding that a test rebinding the original cannot reach.
"""
from __future__ import annotations

from pathlib import Path


# The "open tender notices" file: active tenders only. There's also a
# "complete" file (tenderNoticeComplete-...) with every notice since 2022,
# but it's much larger and we filter to open tenders anyway.
TENDER_URL = "https://canadabuys.canada.ca/opendata/pub/openTenderNotice-ouvertAvisAppelOffres.csv"

# THREE parents, not two. This module sits one level deeper than the ingest.py
# it came out of (scripts/ingest/paths.py, not scripts/ingest.py), and the
# old two-parent arithmetic resolves to scripts/ from here — which silently
# points DEFAULT_CACHE and DEFAULT_DB at scripts/.cache and scripts/chroma_db.
PROJECT_ROOT = Path(__file__).parent.parent.parent
DEFAULT_PROFILE = PROJECT_ROOT / "vault" / "profiles" / "my-company.md"
DEFAULT_CACHE = PROJECT_ROOT / ".cache" / "tenders.csv"
DEFAULT_DB = PROJECT_ROOT / "chroma_db"
