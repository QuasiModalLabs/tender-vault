"""
Plumbing every ingest script shares: HTTP headers, schema resolution, and the
two output guards.

Lifted out of ingest.py because none of it is tender-specific and four other
scripts were importing it from there. That import is also what forced
`entity_org_keys` to `import crosswalk` lazily: crosswalk needs
`resolve_columns`/`output_path`/`staged_db`, so crosswalk imported ingest, so
ingest could not import crosswalk at module level. Nothing here imports
anything from the project, which is the property that breaks the cycle — keep
it that way.
"""
from __future__ import annotations

import contextlib
import sqlite3
import sys
from pathlib import Path


# Canada Buys' WAF returns 403 for the default python-requests user agent.
# A browser-like UA is required for the download to succeed.
REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/csv,application/csv,*/*",
}


def resolve_columns(
    columns: list[str],
    candidates: dict[str, list[str]],
    required: list[str],
    source_label: str,
) -> dict[str, str | None]:
    """
    Map logical field names to real header names, or exit(2) with the real list.

    Shared by every ingest script. Matching is case-insensitive so a header
    recased upstream doesn't count as a rename. Keys not in `required` resolve
    to None and the caller decides what an absent column means.
    """
    lower = {c.lower().strip(): c for c in columns}
    resolved: dict[str, str | None] = {}
    for key, cands in candidates.items():
        found = None
        for cand in cands:
            if cand.lower().strip() in lower:
                found = lower[cand.lower().strip()]
                break
        resolved[key] = found
    missing = [k for k in required if resolved[k] is None]
    if missing:
        sys.stderr.write(
            f"Schema mismatch in {source_label}. Could not find columns for: {missing}\n"
            "Columns present in the file:\n"
            + "\n".join(f"  {c}" for c in columns)
            + f"\nUpdate the column candidates in {source_label}.\n"
        )
        sys.exit(2)
    return resolved


# ---------------------------------------------------------------------------
# Output safety — shared by every ingest script
# ---------------------------------------------------------------------------

def output_path(default_path: Path, explicit: Path | None,
                sampling_reason: str | None) -> Path:
    """
    Decide where an ingest writes, keeping sampling runs off the real database.

    A flag whose purpose is to spot-check the pipeline on a subset must not be
    able to replace the corpus the rest of the repo reads. `--max-audits 20`
    truncated a 364-row oag.db to 20; `--source <trimmed csv>` emptied a
    33,196-row contracts.db. Both "worked" — they just destroyed the real data
    on the way. Sampling now redirects to a sibling .sample path, and
    overwriting the committed database takes an explicit --db.
    """
    if explicit is not None:
        return Path(explicit)
    if not sampling_reason:
        return default_path
    sample = default_path.with_name(default_path.stem + ".sample" + default_path.suffix)
    print(
        f"SAMPLING RUN ({sampling_reason}).\n"
        f"  Writing to     {sample}\n"
        f"  Leaving intact {default_path}\n"
        f"  Pass --db {default_path} to overwrite the real one deliberately."
    )
    return sample


@contextlib.contextmanager
def staged_db(db_path: Path):
    """
    Yield a SQLite connection to a .part file, published over db_path on success.

    Every ingest used to unlink its output and then build in place, so anything
    that failed in between — a schema mismatch, a dropped connection, a bad row
    — left no database at all. Here nothing touches db_path until the new
    database is complete and committed; os.replace is atomic for files on
    Windows and POSIX alike.

    On failure the .part is removed as well, so a failed run leaves the tree
    exactly as it found it rather than a stray half-written file for the next
    person to wonder about.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = db_path.with_name(db_path.name + ".part")
    tmp_path.unlink(missing_ok=True)
    con = sqlite3.connect(tmp_path)
    try:
        yield con
        con.commit()
    except BaseException:
        con.close()
        tmp_path.unlink(missing_ok=True)
        raise
    con.close()
    tmp_path.replace(db_path)
