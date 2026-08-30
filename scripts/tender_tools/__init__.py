"""
Tools for Claude Code to work with the tender corpus — the public surface.

`import tender_tools` gets every command from here, which is what
scripts/mcp_server.py and the test suites do. The CLI lives in cli.py and the
usage text is that module's docstring.

WHAT THIS FILE MUST NOT RE-EXPORT: any name that is rebound from outside.
`VAULT`, `DIGESTS`, `DB_PATH`, `doc_index`, `_collection`, `_bm25` and their
neighbours are reachable as `tender_tools.paths.VAULT` and
`tender_tools.corpus.doc_index`, deliberately, and never as a bare
`tender_tools.VAULT`. A re-export here binds a second name to the same object,
and rebinding one leaves every module that reads the other holding the old
value — which for the vault paths means a test run writing into the user's real
vault with nothing failing. Functions and classes are immutable in practice and
re-export freely; state does not. See tests/conftest.py.
"""
from __future__ import annotations

import sys
from pathlib import Path

# scripts/ on the path so the flat sibling modules (attachments, org_resolve,
# ingest, crosswalk) import the same way they did when this was one file there.
sys.path.insert(0, str(Path(__file__).parent.parent))

import org_resolve  # noqa: E402,F401  (re-exported: test_dossier reads it)

from ingest import classify_notice as _classify_notice  # noqa: E402
from ingest import entity_org_keys as _entity_org_keys  # noqa: E402

from . import corpus, paths  # noqa: E402,F401  (the two mutable-state owners)
from .contracts import (  # noqa: E402
    cmd_contracts_intel,
    cmd_expiring_contracts,
)
from .corpus import BM25, load_collection  # noqa: E402
from .documents import (  # noqa: E402
    cmd_attach,
    cmd_list_attachments,
    cmd_read_attachment,
)
from .dossier import (  # noqa: E402
    cmd_department_dossier,
    _dossier_lobbying,
    _dossier_tenders,
)
from .entities import _entity_keys  # noqa: E402
from .lifecycle import (  # noqa: E402
    cmd_archive,
    cmd_list_corpus,
    cmd_list_parked,
    cmd_list_watching,
    cmd_park,
    cmd_promote,
)
from .cli import build_parser, main  # noqa: E402
from .company_profile import _SENTINEL_HORIZON_YEARS  # noqa: E402
from .provenance import _corpus_provenance, _digest_frontmatter  # noqa: E402
from .premortem import cmd_pre_mortem  # noqa: E402
from .search import cmd_get, cmd_search, cmd_similar  # noqa: E402
from .signals import (  # noqa: E402
    cmd_lobbying_signals,
    cmd_oag_signals,
    cmd_program_signals,
    cmd_registrations_signals,
    cmd_resolve_department,
)

__all__ = [
    "BM25", "load_collection", "build_parser", "main",
    "cmd_search", "cmd_get", "cmd_similar",
    "cmd_list_corpus", "cmd_list_watching", "cmd_list_parked",
    "cmd_promote", "cmd_park", "cmd_archive",
    "cmd_attach", "cmd_list_attachments", "cmd_read_attachment",
    "cmd_pre_mortem",
    "cmd_contracts_intel", "cmd_expiring_contracts",
    "cmd_program_signals", "cmd_oag_signals", "cmd_resolve_department",
    "cmd_lobbying_signals", "cmd_registrations_signals",
    "cmd_department_dossier",
    "corpus", "paths", "org_resolve",
]
