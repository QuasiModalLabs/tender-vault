"""
The tender ingest — the public surface.

`from ingest import parse_profile` and every other name below resolve exactly
as they did when this was one 1400-line module, which is what four sibling
ingest scripts, tender_tools and five test suites depend on.

WHAT THIS FILE RE-EXPORTS AND WHY IT IS SAFE HERE: functions, compiled regexes
and frozen lookup tables, none of which anything rebinds. The paths are the
exception and are deliberately absent from that argument — `PROJECT_ROOT`,
`DEFAULT_CACHE`, `DEFAULT_DB` and `DEFAULT_PROFILE` are re-exported below
because unspsc_discover.py and cli.py already read them as `ingest.PROJECT_ROOT`
and nothing in the tree rebinds them (verified: no assignment to an `ingest.*`
attribute exists in scripts/ or tests/). If a test ever needs to move one, move
it on `ingest.paths` and read it as `paths.DEFAULT_DB` at call time — a second
binding here would go on holding the old value. See tender_tools/__init__.py,
which had to make the same call and answered it the other way because its
paths ARE rebound by tests/conftest.py.

The plumbing shared with the other ingest scripts is NOT here: it lives in
scripts/ingest_common.py, which imports nothing of ours. REQUEST_HEADERS,
resolve_columns, output_path and staged_db are re-exported below only so the
old `from ingest import ...` spellings keep working; new code should import
them from ingest_common directly.
"""
from __future__ import annotations

import sys
from pathlib import Path

# scripts/ on the path so the flat sibling modules (crosswalk, org_resolve,
# ingest_common) import the same way they did when this was one file there.
sys.path.insert(0, str(Path(__file__).parent.parent))

from ingest_common import (  # noqa: E402,F401
    REQUEST_HEADERS,
    output_path,
    resolve_columns,
    staged_db,
)

from . import paths  # noqa: E402,F401  (the path owner — read via paths.X)
from .classify import (  # noqa: E402,F401
    _QUALIFICATION_NOTES,
    kind_manifest,
    _CATEGORY_CONSTRUCTION,
    _CATEGORY_GOODS,
    _CATEGORY_SERVICES,
    _KNOWN_SUPPLY_ARRANGEMENTS,
    _NOTICE_KINDS,
    _RESULTS_NOTICE_PHRASES,
    _SA_NUMBER,
    _VEHICLE_TOKENS,
    classify_notice,
    parse_categories,
    parse_unspsc_codes,
)
from .cli import main  # noqa: E402,F401
from .company_profile import _imminence_threshold, parse_profile  # noqa: E402,F401
from .corpus import (  # noqa: E402,F401
    IDENTITY_FILENAME,
    _chunk_document,
    _feed_mtime_iso,
    _meta_str,
    _write_chroma,
    build_chroma,
    corpus_identity,
    stored_identity,
)
from .dates import (  # noqa: E402,F401
    _BODY_DATE,
    SENTINEL_HORIZON_YEARS,
    body_date_conflict,
    closing_window,
)
from .feed import _snapshot_feed, download_tenders  # noqa: E402,F401
from .filters import (  # noqa: E402,F401
    _source_system,
    contains_excluded,
    filter_tenders,
    matched_competencies,
    matches_unspsc_families,
)
from .jurisdiction import (  # noqa: E402,F401
    _JURISDICTION_PREFIXES,
    _PROVINCIAL_JURISDICTIONS,
    classify_jurisdiction,
    entity_org_keys,
)
from .paths import (  # noqa: E402,F401
    DEFAULT_CACHE,
    DEFAULT_DB,
    DEFAULT_PROFILE,
    PROJECT_ROOT,
    TENDER_URL,
)
from .schema import (  # noqa: E402,F401
    TENDER_COLUMNS,
    TENDER_REQUIRED,
    UNCODED_SOURCE_SYSTEMS,
)
from .value import (  # noqa: E402,F401
    _VALUE_MULTIPLIERS,
    _VALUE_PATTERN,
    estimate_value,
    make_llm_value_extractor,
)
