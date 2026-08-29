"""
Every filesystem location the tools read or write, and the single owner of them.

THIS MODULE IS THE ONE PLACE THESE NAMES ARE BOUND. Consumers must reach them
as `paths.VAULT`, never `from .paths import VAULT` — see the note in
tests/conftest.py. The test suite makes the real vault unreachable by rebinding
these attributes at import time, and a `from` import takes a private copy that
the rebind cannot reach. A module holding a stale copy writes into the user's
real vault with nothing failing to say so.
"""
from __future__ import annotations

from pathlib import Path

# .parent.parent.parent, not .parent.parent: this file is one level deeper
# than the old scripts/tender_tools.py it came out of.
PROJECT_ROOT = Path(__file__).parent.parent.parent
DB_PATH = PROJECT_ROOT / "chroma_db"
VAULT = PROJECT_ROOT / "vault"
PROFILE = VAULT / "profiles" / "my-company.md"
WATCHING = VAULT / "tenders" / "watching"
ARCHIVED = VAULT / "tenders" / "archived"
PARKED = VAULT / "tenders" / "parked"

# Committed, unlike chroma_db/ — which is what makes the corpus stamp
# comparable across machines. CI rebuilds the corpus on its runner and pushes
# only the digest, so the digest's stamp is the one observable fact about what
# another machine last built.
DIGESTS = VAULT / "digests"

# Department nodes, named by canonical key. The target of every [[key]] link a
# tender file writes, and the file whose backlinks answer "what are we looking
# at from this department".
#
# HAND-EDITABLE AND NEVER OVERWRITTEN. Deliberately not derived from the
# contracts ingest: that generator writes <key>-contracts.md, rewrites it every
# run, and only runs at all if someone took the optional ~630MB download. Graph
# connectivity cannot be a side effect of an optional step.
#
# CREATED ON PROMOTE, not generated in bulk. The registry carries ~100 keys and
# a vault holding 100 department stubs for the handful actually in play is a
# graph that hides its own signal.
AGENCIES = VAULT / "agencies"

# The CanadaBuys open-notice feed as downloaded. The dossier reads the raw feed
# rather than the profile-filtered corpus, so it needs the file itself.
_TENDERS_CSV = PROJECT_ROOT / ".cache" / "tenders.csv"

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
    container = Path(container) if container is not None else DB_PATH
    try:
        name = (container / POINTER_FILENAME).read_text(encoding="utf-8").strip()
    except OSError:
        return container
    candidate = container / name
    return candidate if candidate.exists() else container
