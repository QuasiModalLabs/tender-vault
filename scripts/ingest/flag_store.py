"""
The ref-007 flag store: data/coded_flags.jsonl, committed, append-only.

WHAT A LINE HOLDS. Notice id, title, filed codes, Jev's choice, the mass BAND,
model, question hash, content hash, filter version and first-seen date. NOT
the mass: predicates.CodedFlag has no mass field, so this module never holds
one to write. A number of that shape in git, next to notices, is a fit score
in disguise - see ref-006's decision A and ref-007.

WHY COMMITTED, NOT UNDER .cache/. CI restores .cache/ from a rolling Actions
cache; one missed restore would reset a month of promotion evidence with no
error. The digest is committed for the same reason.

ONE WRITER. Only an ingest run with --record-flags writes here, and CI is the
one that passes it, committing the file with the digest. Two machines
appending to one committed file would meet in a rebase conflict.

A FAILED WRITE NEVER FAILS THE INGEST, AND IS NEVER SILENT. The caller
(ingest/cli.py::_record_flags) logs it and marks flag_store_status in
provenance and the digest; see ref-007.

ONCE PER NOTICE. Keyed on (notice_id, model, question_sha256). A notice open
for six weeks is flagged once; an amended notice is not re-flagged, and one
amended out of range is not un-flagged. The line keeps the content hash it was
flagged on.

APPEND-ONLY, ENFORCED HERE. The file is only ever opened in append mode, so
existing bytes are never rewritten; tests/test_family_imputer.py asserts the
old file is a byte prefix of the new one. The file is created even at zero
flags, so CI's `git add` always has a path to add.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import paths


def _key(record: dict) -> tuple:
    return (record["notice_id"], record["model"], record["question_sha256"])


def load(path: Path | None = None) -> list:
    """Every recorded flag, in the order recorded. Missing store is empty."""
    path = path or paths.CODED_FLAGS
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def existing_keys(path: Path | None = None) -> set:
    path = path or paths.CODED_FLAGS
    if not path.exists():
        return set()
    with path.open(encoding="utf-8") as fh:
        return {_key(json.loads(line)) for line in fh if line.strip()}


def to_record(flag, title: str, filter_version: str, seen: str) -> dict:
    """One line. Built from a CodedFlag, which carries a band and no mass."""
    return {
        "notice_id": flag.notice_id,
        "title": " ".join(str(title).split()),
        "filed_codes": list(flag.filed_codes),
        "jev_choice": flag.jev_choice,
        "mass_band": flag.mass_band,
        "model": flag.model,
        "question_sha256": flag.question_sha256,
        "content_sha256": flag.content_sha256,
        "filter_version": filter_version,
        "first_seen": seen,
    }


def append(records: list, path: Path | None = None) -> dict:
    """Append records not already present by key. Returns counts."""
    path = path or paths.CODED_FLAGS
    path.parent.mkdir(parents=True, exist_ok=True)
    seen = existing_keys(path)
    new = []
    for record in records:
        if _key(record) not in seen:
            seen.add(_key(record))
            new.append(record)
    # One write call, so a failure mid-append is unlikely to leave half a line.
    # If one ever is left, the next run's existing_keys cannot parse it and the
    # ingest marks `write failed: JSONDecodeError` in provenance and the digest
    # every day until it is fixed by hand - visible, never silent.
    with path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write("".join(json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n"
                         for record in new))
    return {"written": len(new), "already_recorded": len(records) - len(new)}
