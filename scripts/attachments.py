"""
Manually-dropped tender documents: extraction, manifest, drift detection.

THIS MODULE NEVER FETCHES ANYTHING. The RFP packages it reads are hosted on
Ariba and MERX behind account walls, and this project deliberately does not
scrape those platforms. A human pulls the package in a browser, past the
account wall, and drops the files into the tender's folder. Everything here
reads a directory that is already on disk. Adding a downloader to this file
would reverse a decision recorded in the README, not extend this one.

The extracted text stays out of git (`vault/tenders/` is ignored wholesale) and
out of the ChromaDB corpus. It is third-party content from a commercial
platform, and the extraction is that same content in another encoding.

WHAT GOES IN THE FOLDER

    vault/tenders/watching/cb-342-92719341/
        _index.md               this manifest
        RFP-W2187-SPO.pdf       dropped by hand
        .extracted/
            RFP-W2187-SPO.txt   what Claude actually reads

ADDING A FORMAT IS ONE FUNCTION AND ONE ROW

`EXTRACTORS` is a dispatch table keyed by lowercased file extension. No format
is special-cased anywhere in this module — `.pdf` is one row like any other.
Registering a new one means writing a function that returns an `ExtractResult`,
adding a row, and adding a line to requirements.txt. It needs no manifest schema
change, and it does not ask the user to re-drop files already in the folder: a
file sitting at `unsupported_type` is picked up on the next refresh once its
extension has an extractor (see `_needs_extraction`).

`.docx` IS THE OBVIOUS NEXT ONE. `.xlsx` IS NOT — and they should not be added
in the same pass. Flattening an evaluation grid or a pricing table to a stream
of text loses the row/column structure that gives every cell its meaning, and
the result reads as confident prose that can be badly wrong about which figure
belongs to which line item. A spreadsheet extractor needs its own design pass
about what shape it should even produce. A Word SOW has none of that problem.

WHAT `page_count: null` IS FOR

Formats with no meaningful pagination report `null` rather than inventing a
number. That is what lets the manifest schema stay fixed as formats are added.

ONE LIMITATION, STATED OUT LOUD: upgrading an extractor does not by itself force
re-extraction. The refresh triggers on the file's hash and on whether an
extractor exists at all, not on the extractor's version — keeping the dispatch
table a plain function table was worth more than automatic version tracking. To
re-extract everything after an extractor upgrade, delete the folder's
`.extracted/` directory; the next refresh rebuilds it.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

import yaml


MANIFEST_NAME = "_index.md"
EXTRACTED_DIRNAME = ".extracted"

# The per-file enum. Three values, fixed: a file is readable, or an extractor
# looked and found no text, or nothing here can read it.
STATUS_EXTRACTED = "extracted"
STATUS_NO_TEXT_LAYER = "no_text_layer"
STATUS_UNSUPPORTED = "unsupported_type"

SOURCE_PLATFORMS = ("merx", "ariba", "other")

# Below this many non-whitespace characters, an extractor is reporting that the
# document has no text layer rather than that the document is short. Scaled per
# page with a floor, because "empty" for a 40-page scan is a much larger number
# than "empty" for a one-page cover letter.
_MIN_CHARS_PER_PAGE = 10
_MIN_CHARS_FLOOR = 50


@dataclass(frozen=True)
class ExtractResult:
    """What every extractor returns. `page_count` is None where meaningless."""
    text: str
    page_count: int | None
    extractor: str  # "name version", recorded in the manifest


# ---------------------------------------------------------------------------
# Extractors
# ---------------------------------------------------------------------------


def _extract_pdf(path: Path) -> ExtractResult:
    """
    pdfplumber. Imported lazily so this module loads without the dependency —
    a missing library is reported per-file and self-heals once it is installed,
    rather than taking down every attachment command with an ImportError.
    """
    import pdfplumber  # noqa: PLC0415 — lazy on purpose, see docstring

    pages: list[str] = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            pages.append(page.extract_text() or "")

    version = getattr(pdfplumber, "__version__", "unknown")
    return ExtractResult(
        text="\n\n".join(pages),
        page_count=len(pages),
        extractor=f"pdfplumber {version}",
    )


# The dispatch table. One row per format; nothing below reads it by name.
EXTRACTORS: dict[str, Callable[[Path], ExtractResult]] = {
    ".pdf": _extract_pdf,
}


# ---------------------------------------------------------------------------
# Hashing and manifest I/O
# ---------------------------------------------------------------------------


def sha256_file(path: Path) -> str:
    """Streamed, because an RFP package runs to tens of megabytes."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _scalar(value) -> str:
    """
    One YAML scalar, house style: hand-built, never yaml.dump.

    Strings go through json.dumps rather than the quote-swapping used for
    display titles elsewhere in the project (`title.replace('"', "'")`). That
    trick is fine for prose nobody looks up again; these strings include
    FILENAMES, which are keys — they have to come back byte-for-byte, and JSON
    string syntax is a subset of YAML 1.2 double-quoted syntax, so escaping is
    handled and round-trips exactly.

    `ensure_ascii=False` because this file is read in Obsidian: an accented
    filename or an em dash should appear as itself, not as `\\u2014`. The file
    is written UTF-8, which YAML requires anyway.
    """
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    return json.dumps(str(value), ensure_ascii=False)


# Field order in each file record. Fixed here so the manifest reads the same
# way every time it is rewritten and a diff shows only what actually changed.
_FILE_FIELDS = (
    "filename",
    "extension",
    "sha256",
    "size_bytes",
    "extraction_status",
    "extractor",
    "page_count",
    "char_count",
    "extracted_path",
    "first_seen",
    "last_changed",
    "superseded_sha256",
    "note",
)


def render_manifest(data: dict) -> str:
    """
    The manifest as markdown: authoritative YAML frontmatter, then a body for
    whoever opens the folder in Obsidian. The body is regenerated wholesale on
    every write; nothing reads it back.
    """
    lines = ["---"]
    for key in ("tender_id", "note", "source_platform", "retrieved_at",
                "manifest_updated_at"):
        if key in data:
            lines.append(f"{key}: {_scalar(data[key])}")

    lines.append("files:")
    for record in data.get("files", []):
        first = True
        for field in _FILE_FIELDS:
            if field not in record:
                continue
            prefix = "  - " if first else "    "
            lines.append(f"{prefix}{field}: {_scalar(record[field])}")
            first = False
        if first:  # a record with no recognised fields would break the sequence
            lines.append("  - {}")
    if not data.get("files"):
        lines[-1] = "files: []"
    lines.append("---")

    body = [
        "",
        f"# Documents — {data.get('tender_id', '')}",
        "",
        f"Tender note: {data.get('note', '')}",
        "",
        "Dropped by hand from "
        f"{data.get('source_platform', 'an unrecorded source')}. "
        "Nothing here was fetched by this project — see the module docstring in "
        "`scripts/attachments.py`.",
        "",
    ]
    files = data.get("files", [])
    if files:
        body += [
            "| File | Status | Pages | Extracted |",
            "| --- | --- | --- | --- |",
        ]
        for record in files:
            pages = record.get("page_count")
            body.append(
                f"| {record.get('filename', '')} "
                f"| {record.get('extraction_status', '')} "
                f"| {'—' if pages is None else pages} "
                f"| {record.get('extracted_path') or '—'} |"
            )
    else:
        body.append("_No documents dropped yet._")
    body.append("")
    return "\n".join(lines + body)


def write_manifest(folder: Path, data: dict) -> Path:
    """LF explicitly, as every markdown writer in this project does."""
    path = folder / MANIFEST_NAME
    path.write_text(render_manifest(data), encoding="utf-8", newline="\n")
    return path


def read_manifest(folder: Path) -> dict:
    """
    Parse the frontmatter with PyYAML, unlike the hand-rolled regex reader used
    for tender notes and digests.

    Those readers avoid safe_load for a specific reason (`_digest_frontmatter`
    in tender_tools.py): an unquoted `2026-08-09T14:27:11` loads as a datetime
    and then silently compares unequal to the string stamp it is checked
    against. That hazard is absent here because `_scalar` quotes every string it
    writes, so timestamps and hashes come back as `str`. And the regex reader
    could not do this job anyway — `files:` is a nested sequence of mappings,
    which `line.partition(":")` cannot represent.
    """
    path = folder / MANIFEST_NAME
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---", text, re.DOTALL)
    if not match:
        return {}
    loaded = yaml.safe_load(match.group(1)) or {}
    if not isinstance(loaded, dict):
        return {}
    loaded.setdefault("files", [])
    if not isinstance(loaded["files"], list):
        loaded["files"] = []
    return loaded


def new_manifest(tender_id: str, note_stem: str, source_platform: str) -> dict:
    return {
        "tender_id": tender_id,
        "note": f"[[{note_stem}]]",
        "source_platform": source_platform,
        "retrieved_at": datetime.now().strftime("%Y-%m-%d"),
        "manifest_updated_at": datetime.now().isoformat(timespec="seconds"),
        "files": [],
    }


# ---------------------------------------------------------------------------
# Refresh: diff the directory against the manifest
# ---------------------------------------------------------------------------


def dropped_files(folder: Path) -> list[Path]:
    """
    Everything a human put in the folder. Skips the manifest, `.extracted/`,
    and dotfiles — an OS writing `.DS_Store` into the folder is not a document.
    """
    out = []
    for child in sorted(folder.iterdir()):
        if child.is_dir() or child.name == MANIFEST_NAME:
            continue
        if child.name.startswith("."):
            continue
        out.append(child)
    return out


def _needs_extraction(record: dict | None, sha: str, ext: str, folder: Path) -> bool:
    """
    The whole re-extraction rule, in one place.

    Deliberately not a chain of special cases: a file is extracted again when
    what we recorded no longer describes what is on disk, or when this process
    can now read something it previously could not.
    """
    if record is None:
        return True                                  # never seen
    if record.get("sha256") != sha:
        return True                                  # amended document
    if record.get("extraction_status") == STATUS_UNSUPPORTED and ext in EXTRACTORS:
        return True                                  # an extractor arrived
    if record.get("extraction_status") == STATUS_EXTRACTED:
        rel = record.get("extracted_path")
        if not rel or not (folder / rel).exists():
            return True                              # text deleted by hand
    return False


def _classify(text: str, page_count: int | None) -> tuple[str, int]:
    """
    Distinguish 'the extractor found no text layer' from 'this document is
    short'. Returns (status, char_count); char_count goes into the manifest so
    the threshold's verdict is auditable instead of being a bare label.
    """
    chars = len(re.sub(r"\s", "", text))
    pages = page_count if page_count and page_count > 0 else 1
    threshold = max(_MIN_CHARS_FLOOR, _MIN_CHARS_PER_PAGE * pages)
    if chars < threshold:
        return STATUS_NO_TEXT_LAYER, chars
    return STATUS_EXTRACTED, chars


def _extracted_name(path: Path, claimed: set[str]) -> str:
    """
    `RFP-W2187-SPO.pdf` -> `RFP-W2187-SPO.txt`, unless that name is already
    spoken for. Two documents with the same stem and different extensions would
    otherwise overwrite each other's text silently.
    """
    candidate = f"{path.stem}.txt"
    if candidate in claimed:
        candidate = f"{path.name}.txt"
    claimed.add(candidate)
    return candidate


def refresh(folder: Path) -> dict:
    """
    Diff the directory against the manifest, extract what changed, rewrite it.

    Returns the manifest plus what happened this call: `added`, `changed`
    (an amended document — the reason the hashes are here at all), `removed`,
    and `warnings` for conditions the per-file enum cannot express, such as a
    registered extractor whose library is not installed.
    """
    manifest = read_manifest(folder)
    if not manifest:
        manifest = {"files": []}

    previous = {r.get("filename"): r for r in manifest.get("files", [])}
    extracted_dir = folder / EXTRACTED_DIRNAME
    today = datetime.now().strftime("%Y-%m-%d")

    records: list[dict] = []
    added: list[str] = []
    changed: list[str] = []
    warnings: list[str] = []
    claimed: set[str] = set()

    for path in dropped_files(folder):
        ext = path.suffix.lower()
        sha = sha256_file(path)
        old = previous.get(path.name)

        record = {
            "filename": path.name,
            "extension": ext,
            "sha256": sha,
            "size_bytes": path.stat().st_size,
            "first_seen": (old or {}).get("first_seen", today),
            "last_changed": (old or {}).get("last_changed", today),
        }

        if old is None:
            added.append(path.name)
        elif old.get("sha256") != sha:
            # The case the hashing exists for. MERX posts addenda mid-
            # solicitation under the same filename, and extracted text that
            # looks current but describes the superseded document is worse than
            # having no text at all.
            changed.append(path.name)
            record["last_changed"] = today
            record["superseded_sha256"] = old.get("sha256")
        elif old.get("superseded_sha256"):
            record["superseded_sha256"] = old["superseded_sha256"]

        if not _needs_extraction(old, sha, ext, folder):
            for field in ("extraction_status", "extractor", "page_count",
                          "char_count", "extracted_path", "note"):
                if field in old:
                    record[field] = old[field]
            records.append(record)
            continue

        extractor = EXTRACTORS.get(ext)
        if extractor is None:
            # Listed, hashed and sized — visible even though unreadable. A
            # dropped pricing spreadsheet should not become invisible.
            record["extraction_status"] = STATUS_UNSUPPORTED
            record["extractor"] = None
            record["page_count"] = None
            record["extracted_path"] = None
            records.append(record)
            continue

        try:
            result = extractor(path)
        except ImportError as exc:
            # The extension is registered but its library is not installed.
            # Recorded as unsupported so `_needs_extraction` retries it once the
            # dependency appears, and surfaced as a warning so it does not read
            # as a fact about the document.
            warnings.append(
                f"{path.name}: no extractor available for {ext} in this "
                f"environment ({exc}). Install it and run this again."
            )
            record["extraction_status"] = STATUS_UNSUPPORTED
            record["extractor"] = None
            record["page_count"] = None
            record["extracted_path"] = None
            records.append(record)
            continue
        except Exception as exc:  # a malformed or encrypted document
            warnings.append(f"{path.name}: extraction failed ({exc}).")
            record["extraction_status"] = STATUS_UNSUPPORTED
            record["extractor"] = None
            record["page_count"] = None
            record["extracted_path"] = None
            records.append(record)
            continue

        status, char_count = _classify(result.text, result.page_count)
        record["extraction_status"] = status
        record["extractor"] = result.extractor
        record["page_count"] = result.page_count
        record["char_count"] = char_count

        if status == STATUS_NO_TEXT_LAYER:
            # No zero-byte .txt. A file that exists and is empty looks like a
            # successful extraction of an empty document, and the next reader
            # concludes the RFP said nothing.
            record["extracted_path"] = None
            record["note"] = (
                "No text layer — almost certainly a scan. Not OCR'd; v1 reports "
                "this rather than solving it."
            )
        else:
            extracted_dir.mkdir(parents=True, exist_ok=True)
            name = _extracted_name(path, claimed)
            (extracted_dir / name).write_text(
                result.text, encoding="utf-8", newline="\n"
            )
            record["extracted_path"] = f"{EXTRACTED_DIRNAME}/{name}"

        records.append(record)

    on_disk = {r["filename"] for r in records}
    removed = sorted(set(previous) - on_disk)

    manifest["files"] = records
    manifest["manifest_updated_at"] = datetime.now().isoformat(timespec="seconds")
    write_manifest(folder, manifest)

    return {
        "manifest": manifest,
        "added": added,
        "changed": changed,
        "removed": removed,
        "warnings": warnings,
    }


def find_record(manifest: dict, filename: str) -> dict | None:
    for record in manifest.get("files", []):
        if record.get("filename") == filename:
            return record
    return None
