"""
Attached documents — manually dropped, never fetched.

Named documents.py rather than attachments.py because scripts/attachments.py
already exists and is the extractor this module drives; two modules with one
name in the same import path is a trap even when Python resolves it.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import attachments

from . import paths
from .text import _slugify

# The RFP package lives on MERX or Ariba behind an account wall and this
# project deliberately does not scrape those platforms. A human pulls the
# files in a browser and drops them into the folder these commands manage.
# scripts/attachments.py holds the extraction and manifest logic; everything
# here is path resolution and the CLI/MCP shape.
#
# The folder is created on demand rather than on promote, and its existence is
# itself a signal: it marks the tenders that got real effort, which watching /
# parked / archived do not distinguish.


def _note_for_tender(tender_id: str) -> Path | None:
    """
    The note for a tender_id, wherever it currently lives.

    Deliberately searches all three lifecycle states, unlike the four inline
    `paths.WATCHING / f"{_slugify(id)}.md"` checks elsewhere in this file — those
    answer `in_watching`, which is a narrower question, and widening them would
    change what the corpus commands report. Attachments have to keep working
    after a tender is parked or archived, so they need this one instead.
    """
    name = f"{_slugify(tender_id)}.md"
    for directory in (paths.WATCHING, paths.PARKED, paths.ARCHIVED):
        candidate = directory / name
        if candidate.exists():
            return candidate
    return None


def _attachment_dir(note: Path) -> Path:
    """Beside the note, named for it: cb-342-92719341.md -> cb-342-92719341/."""
    return note.parent / note.stem


def _move_attachment_dir(source: Path, target: Path) -> tuple[str | None, dict | None]:
    """
    Move a tender's document folder to follow its note. Returns (moved, error).

    ORDER IS THE POINT, and the caller must run this BEFORE writing the note.
    A crash between the two leaves a note pointing at a folder that isn't there
    — wrong, but detectable by anyone who looks. The other order leaves a folder
    that no note references, which nothing will ever surface again.
    """
    src_dir = _attachment_dir(source)
    if not src_dir.is_dir():
        return None, None

    dst_dir = _attachment_dir(target)
    if dst_dir.exists():
        return None, {
            "error": f"Attachment folder already exists at the destination: "
                     f"{dst_dir}. Resolve it by hand; nothing was moved."
        }
    try:
        shutil.move(str(src_dir), str(dst_dir))
    except OSError as exc:
        # Report and stop. Moving the note anyway would produce exactly the
        # invisible orphan this ordering exists to prevent.
        return None, {"error": f"Could not move attachment folder: {exc}"}
    return str(dst_dir.relative_to(paths.PROJECT_ROOT)), None


def _reveal_in_file_manager(path: Path) -> None:
    """
    Best effort, and nothing more. Never raises, never blocks for long.

    Skipped entirely when stdout is not a terminal: over SSH, in CI, and on the
    MCP server — where stdout is the protocol channel and a file manager is
    meaningless — these calls variously fail, hang, or open something on the
    wrong machine. The absolute path printed by the caller is the actual
    contract with the user; this is a convenience on top of it.
    """
    if not sys.stdout.isatty():
        return
    try:
        if sys.platform == "win32":
            os.startfile(str(path))  # noqa: S606
        elif sys.platform == "darwin":
            subprocess.run(["open", str(path)], timeout=5, check=False)
        else:
            subprocess.run(["xdg-open", str(path)], timeout=5, check=False)
    except Exception:
        # Including timeouts: xdg-open under WSL can block until it is killed.
        pass


def cmd_attach(args) -> dict:
    """
    Create the document folder for a tender and print its absolute path.

    Nothing is downloaded. The user opens MERX or Ariba in a browser, gets past
    the account wall by hand, and drops the RFP package into this folder.
    """
    note = _note_for_tender(args.tender_id)
    if note is None:
        return {
            "error": f"No tender note for {args.tender_id} in watching/, "
                     f"parked/ or archived/. Promote it first."
        }

    # Validated here, not only in the parser. argparse enforces `choices` on the
    # CLI, but the MCP tool builds its own namespace and never touches it — so
    # without this an unrecognised platform would be written into the manifest
    # as provenance and read back later as though someone had recorded it.
    platform = getattr(args, "platform", None)
    if platform not in attachments.SOURCE_PLATFORMS:
        return {
            "error": f"Unknown source platform {platform!r}. "
                     f"Expected one of: {', '.join(attachments.SOURCE_PLATFORMS)}."
        }

    folder = _attachment_dir(note)
    created = not folder.exists()
    folder.mkdir(parents=True, exist_ok=True)
    (folder / attachments.EXTRACTED_DIRNAME).mkdir(exist_ok=True)

    manifest = attachments.read_manifest(folder)
    if not manifest:
        manifest = attachments.new_manifest(
            tender_id=args.tender_id,
            note_stem=note.stem,
            source_platform=platform,
        )
        attachments.write_manifest(folder, manifest)

    if not getattr(args, "no_reveal", False):
        _reveal_in_file_manager(folder)

    return {
        # ABSOLUTE, unlike promote's relative_to(paths.PROJECT_ROOT). This path exists
        # to be pasted into a file manager or a shell, so it has to stand alone.
        "attachment_folder": str(folder.resolve()),
        "created": created,
        "tender_id": args.tender_id,
        "note": str(note.relative_to(paths.PROJECT_ROOT)),
        "source_platform": manifest.get("source_platform"),
        "next": "Drop the files from MERX/Ariba into that folder, then run "
                "list-attachments to extract them.",
    }


def cmd_list_attachments(args) -> dict:
    """
    List a tender's documents, extracting anything new or changed first.

    This is where the directory is diffed against the manifest — there is no
    watcher and no daemon, so detection happens on the next call.
    """
    note = _note_for_tender(args.tender_id)
    if note is None:
        return {"error": f"No tender note for {args.tender_id}."}

    folder = _attachment_dir(note)
    if not folder.is_dir():
        return {
            "error": f"No attachment folder for {args.tender_id}. Create one "
                     f"with: attach {args.tender_id} --platform merx"
        }

    outcome = attachments.refresh(folder)
    manifest = outcome["manifest"]
    return {
        "tender_id": args.tender_id,
        "attachment_folder": str(folder.resolve()),
        "source_platform": manifest.get("source_platform"),
        "retrieved_at": manifest.get("retrieved_at"),
        "files": manifest.get("files", []),
        "added": outcome["added"],
        # An amended document re-dropped under the same name. Surfaced at the
        # top level because stale text that looks current is the failure mode
        # the hashes exist to catch.
        "changed": outcome["changed"],
        "removed": outcome["removed"],
        "warnings": outcome["warnings"],
    }


def cmd_read_attachment(args) -> dict:
    """
    Read a window of one document's extracted text.

    Paginated, never one blob: a 40-page RFP must not arrive as a single return
    value. Offsets and limits are in LINES of the extracted text.
    """
    note = _note_for_tender(args.tender_id)
    if note is None:
        return {"error": f"No tender note for {args.tender_id}."}

    folder = _attachment_dir(note)
    if not folder.is_dir():
        return {"error": f"No attachment folder for {args.tender_id}."}

    source = folder / args.filename
    if not source.is_file():
        return {"error": f"No such document: {args.filename}"}

    # Re-hash only the file being served. A full rescan is what
    # list-attachments is for; this is the narrow guarantee that a read never
    # returns text that no longer matches the document on disk.
    manifest = attachments.read_manifest(folder)
    record = attachments.find_record(manifest, args.filename)
    if record is None or record.get("sha256") != attachments.sha256_file(source):
        manifest = attachments.refresh(folder)["manifest"]
        record = attachments.find_record(manifest, args.filename)

    if record is None:
        return {"error": f"{args.filename} is not in the manifest."}

    status = record.get("extraction_status")
    if status != attachments.STATUS_EXTRACTED:
        return {
            "error": f"No extracted text for {args.filename} "
                     f"(extraction_status: {status}).",
            "extraction_status": status,
            "status_note": record.get("status_note"),
        }

    text_path = folder / record["extracted_path"]
    if not text_path.exists():
        return {"error": f"Extracted text missing for {args.filename}."}

    lines = text_path.read_text(encoding="utf-8").splitlines()
    offset = max(int(getattr(args, "offset", 0) or 0), 0)
    limit = min(max(int(getattr(args, "limit", 400) or 400), 1), 2000)
    window = lines[offset:offset + limit]

    return {
        "tender_id": args.tender_id,
        "filename": args.filename,
        "offset": offset,
        "limit": limit,
        "total_lines": len(lines),
        "lines_returned": len(window),
        "eof": offset + len(window) >= len(lines),
        "page_count": record.get("page_count"),
        "sha256": record.get("sha256"),
        "text": "\n".join(window),
    }
