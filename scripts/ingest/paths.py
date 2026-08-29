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

# ---------------------------------------------------------------------------
# The corpus is a CONTAINER holding one or more build directories and a
# one-line `CURRENT` file naming the live one.

POINTER_FILENAME = "CURRENT"


def active_db(container=None):
    """
    The directory holding the live corpus inside `container`.

    A rebuild writes a NEW build directory and repoints `CURRENT` at it. It
    never renames or deletes the directory a reader might have open, because
    Windows refuses both while another process holds a handle inside it — and
    something usually does. `scripts/mcp_server.py` keeps a ChromaDB client
    open for the life of a Claude Desktop session, and the rebuild this
    replaced moved `chroma_db/` aside as its first step, so on 2026-08-28 an
    ingest downloaded a fresh feed, embedded it, and then died with
    `PermissionError: [WinError 5]` against an untouched corpus and an advanced
    feed cache. Replacing a one-line pointer file is permitted no matter what
    else is open.

    Returns `container` itself when no pointer is present. That is the layout
    of every corpus built before this change, and of the collections the tests
    hand-build, so both keep working untouched.
    """
    container = Path(container) if container is not None else DEFAULT_DB
    try:
        name = (container / POINTER_FILENAME).read_text(encoding="utf-8").strip()
    except OSError:
        return container
    candidate = container / name
    return candidate if candidate.exists() else container
