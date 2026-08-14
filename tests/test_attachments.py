"""
Tests for manually-dropped tender documents: manifest, dispatch table, reads.

Runs with plain Python — no pytest needed:

    python tests/test_attachments.py

Exit code 0 = all passed. Any assertion failure = non-zero + traceback.

NO REAL PDF ANYWHERE IN HERE, and that is deliberate three times over. CI would
otherwise need pdfplumber installed to test anything; a binary fixture would
have to be committed; and we cannot commit a third-party tender document in the
first place — that is the whole premise of the feature.

So the suite registers its own extractors into `attachments.EXTRACTORS` and
exercises the machinery through those. That is not a workaround: the claim
under test is that adding a format is one function plus one row, and the only
honest way to test that claim is to add one.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent))
import conftest  # noqa: E402  — MUST come first: redirects the vault on import
import attachments as at  # noqa: E402
import tender_tools as tt  # noqa: E402


TENDER_ID = "TEST-ATT-0001"
NOTE_STEM = "test-att-0001"          # what _slugify(TENDER_ID) produces

NOTE = f"""---
tender_id: {TENDER_ID}
title: "Attachment fixture tender"
status: watching
---

# Attachment fixture tender

## My notes
"""


def _register_text_extractor(ext: str, version: str = "1.0") -> None:
    """
    The dispatch-table claim under test: one function, one row.

    Page count is faked as 1 so the no_text_layer threshold is a flat 50
    non-whitespace characters, which makes the boundary easy to write against.
    """
    def _extract(path: Path) -> at.ExtractResult:
        return at.ExtractResult(
            text=path.read_text(encoding="utf-8"),
            page_count=1,
            extractor=f"faketext {version}",
        )

    at.EXTRACTORS[ext] = _extract


def setup_temp_vault() -> Path:
    """A vault with one watching note. The redirect itself lives in conftest."""
    root = conftest.redirect_vault()
    tt.WATCHING.mkdir(parents=True)
    (tt.WATCHING / f"{NOTE_STEM}.md").write_text(NOTE, encoding="utf-8", newline="\n")

    _register_text_extractor(".txt")
    # .later is deliberately NOT registered yet — test_extractor_added_later
    # registers it to prove an already-dropped file is picked up with no re-drop.
    at.EXTRACTORS.pop(".later", None)
    return root


def _folder() -> Path:
    return tt.WATCHING / NOTE_STEM


def _drop(name: str, content: str) -> Path:
    path = _folder() / name
    path.write_text(content, encoding="utf-8", newline="\n")
    return path


def _list() -> dict:
    return tt.cmd_list_attachments(SimpleNamespace(tender_id=TENDER_ID))


def _record(result: dict, filename: str) -> dict:
    for rec in result["files"]:
        if rec["filename"] == filename:
            return rec
    raise AssertionError(f"{filename} not in manifest: {result['files']}")


# ---------------------------------------------------------------------------


def test_attach_creates_folder():
    result = tt.cmd_attach(SimpleNamespace(
        tender_id=TENDER_ID, platform="merx", no_reveal=True,
    ))
    assert "error" not in result, f"attach failed: {result}"
    assert result["created"] is True, "attach did not report creating the folder"

    folder = _folder()
    assert folder.is_dir(), "attachment folder not created"
    assert (folder / at.EXTRACTED_DIRNAME).is_dir(), ".extracted/ not created"
    assert (folder / at.MANIFEST_NAME).exists(), "_index.md not written"

    # The contract with the user: a path they can paste somewhere.
    assert Path(result["attachment_folder"]).is_absolute(), (
        f"attach must return an absolute path, got {result['attachment_folder']}"
    )
    assert result["source_platform"] == "merx", "provenance not recorded"


def test_attach_is_idempotent_and_keeps_provenance():
    """A second attach must not blow away a manifest that has files in it."""
    result = tt.cmd_attach(SimpleNamespace(
        tender_id=TENDER_ID, platform="ariba", no_reveal=True,
    ))
    assert result["created"] is False, "second attach reported creating the folder"
    assert result["source_platform"] == "merx", (
        "second attach overwrote the recorded source platform"
    )


def test_attach_unknown_tender_rejected():
    result = tt.cmd_attach(SimpleNamespace(
        tender_id="NO-SUCH-TENDER", platform="merx", no_reveal=True,
    ))
    assert "error" in result, "attach on a nonexistent tender should error"


def test_manifest_written_with_lf():
    raw = (_folder() / at.MANIFEST_NAME).read_bytes()
    assert b"\r\n" not in raw, "_index.md written with CRLF"


def test_extracts_a_dropped_file():
    _drop("sow.txt", "\n".join(f"Statement of work line {i}" for i in range(100)))
    result = _list()

    assert result["added"] == ["sow.txt"], f"new file not reported: {result['added']}"
    rec = _record(result, "sow.txt")
    assert rec["extraction_status"] == at.STATUS_EXTRACTED, rec
    assert rec["extractor"] == "faketext 1.0", rec
    assert rec["page_count"] == 1, rec
    assert len(rec["sha256"]) == 64, "sha256 not a full hex digest"
    assert rec["size_bytes"] > 0, rec

    text = _folder() / rec["extracted_path"]
    assert text.exists(), "no extracted text written"
    assert "Statement of work line 0" in text.read_text(encoding="utf-8")


def test_unsupported_type_still_listed():
    """A dropped pricing spreadsheet must be visible even though unreadable."""
    _drop("pricing.xlsx", "not really a spreadsheet, but nothing reads it")
    result = _list()

    rec = _record(result, "pricing.xlsx")
    assert rec["extraction_status"] == at.STATUS_UNSUPPORTED, rec
    assert rec["extractor"] is None, rec
    assert rec["extracted_path"] is None, "unsupported type must produce no text"
    # The point of listing it at all: identity and size are still recorded.
    assert len(rec["sha256"]) == 64, rec
    assert rec["size_bytes"] > 0, rec
    assert rec["extension"] == ".xlsx", rec


def test_no_text_layer_writes_no_file():
    """
    A scan yields nothing. Recording that is the job; writing a zero-byte .txt
    would look like a successful extraction of an empty document.
    """
    _drop("scanned.txt", "   \n  \n tiny \n")
    result = _list()

    rec = _record(result, "scanned.txt")
    assert rec["extraction_status"] == at.STATUS_NO_TEXT_LAYER, rec
    assert rec["extracted_path"] is None, rec
    assert rec["char_count"] < 50, f"fixture is not below the threshold: {rec}"
    assert not (_folder() / at.EXTRACTED_DIRNAME / "scanned.txt").exists(), (
        "a zero-byte .txt was written for a document with no text layer"
    )
    assert "scan" in (rec.get("note") or "").lower(), (
        "no_text_layer should say what the condition is"
    )


def test_unchanged_file_is_not_re_extracted():
    before = (_folder() / at.EXTRACTED_DIRNAME / "sow.txt").stat().st_mtime_ns
    result = _list()
    assert result["added"] == [] and result["changed"] == [], (
        f"a no-op refresh reported changes: {result}"
    )
    after = (_folder() / at.EXTRACTED_DIRNAME / "sow.txt").stat().st_mtime_ns
    assert before == after, "unchanged file was extracted again"


def test_amended_document_detected_and_re_extracted():
    """
    The case the hashing exists for: MERX posts an addendum mid-solicitation
    under the same filename. Stale text that looks current is the failure.
    """
    old_sha = _record(_list(), "sow.txt")["sha256"]

    _drop("sow.txt", "\n".join(f"AMENDED line {i}" for i in range(100)))
    result = _list()

    assert result["changed"] == ["sow.txt"], (
        f"amended document not reported as changed: {result['changed']}"
    )
    rec = _record(result, "sow.txt")
    assert rec["sha256"] != old_sha, "hash did not change"
    assert rec["superseded_sha256"] == old_sha, (
        "the superseded hash is what makes an amendment reconstructable later"
    )
    text = (_folder() / rec["extracted_path"]).read_text(encoding="utf-8")
    assert "AMENDED line 0" in text, "text was not re-extracted"
    assert "Statement of work" not in text, "stale text survived the amendment"


def test_extractor_added_later_needs_no_re_drop():
    """
    THE DISPATCH-TABLE REQUIREMENT. A file dropped before its format was
    supported must be picked up once an extractor is registered — one function
    and one row, no manifest migration, and the user does not re-drop anything.
    """
    _drop("annex.later", "\n".join(f"Annex clause {i}" for i in range(40)))

    first = _record(_list(), "annex.later")
    assert first["extraction_status"] == at.STATUS_UNSUPPORTED, first
    sha_before = first["sha256"]

    _register_text_extractor(".later", version="0.1")   # the whole registration

    second = _record(_list(), "annex.later")
    assert second["extraction_status"] == at.STATUS_EXTRACTED, (
        f"registering an extractor did not pick up the existing file: {second}"
    )
    assert second["sha256"] == sha_before, "the file was not touched, only read"
    assert second["extractor"] == "faketext 0.1", second
    assert (_folder() / second["extracted_path"]).exists(), "no text written"


def test_deleted_extracted_text_is_rebuilt():
    text_path = _folder() / _record(_list(), "sow.txt")["extracted_path"]
    text_path.unlink()
    rec = _record(_list(), "sow.txt")
    assert rec["extraction_status"] == at.STATUS_EXTRACTED, rec
    assert (_folder() / rec["extracted_path"]).exists(), (
        "extracted text deleted by hand was not rebuilt"
    )


def test_removed_file_drops_out_of_manifest():
    _drop("temporary.txt", "x" * 200)
    assert _record(_list(), "temporary.txt")

    (_folder() / "temporary.txt").unlink()
    result = _list()
    assert "temporary.txt" in result["removed"], f"removal not reported: {result}"
    assert all(r["filename"] != "temporary.txt" for r in result["files"]), (
        "a deleted file is still in the manifest"
    )


def test_filename_with_awkward_characters_round_trips():
    """
    Filenames are KEYS. The manifest quotes them as JSON strings precisely so
    spaces and punctuation survive the write/read cycle exactly.
    """
    name = "RFP W2187-SPO, Annex A & B (rev 2).txt"
    _drop(name, "\n".join(f"clause {i}" for i in range(30)))
    rec = _record(_list(), name)
    assert rec["filename"] == name, f"filename did not round-trip: {rec['filename']}"
    assert rec["extraction_status"] == at.STATUS_EXTRACTED, rec


def test_read_is_paginated():
    result = tt.cmd_read_attachment(SimpleNamespace(
        tender_id=TENDER_ID, filename="sow.txt", offset=0, limit=10,
    ))
    assert "error" not in result, result
    assert result["lines_returned"] == 10, result
    assert result["total_lines"] == 100, result
    assert result["eof"] is False, "a 10-line window of 100 lines is not eof"
    assert result["text"].startswith("AMENDED line 0"), result["text"][:60]
    assert "AMENDED line 10" not in result["text"], "window overran its limit"


def test_read_offset_and_eof():
    result = tt.cmd_read_attachment(SimpleNamespace(
        tender_id=TENDER_ID, filename="sow.txt", offset=90, limit=400,
    ))
    assert result["lines_returned"] == 10, result
    assert result["eof"] is True, "reading to the end did not report eof"
    assert result["text"].startswith("AMENDED line 90"), result["text"][:60]


def test_read_limit_is_clamped():
    result = tt.cmd_read_attachment(SimpleNamespace(
        tender_id=TENDER_ID, filename="sow.txt", offset=0, limit=999999,
    ))
    assert result["limit"] == 2000, f"limit not clamped: {result['limit']}"


def test_read_rejects_a_file_with_no_text():
    result = tt.cmd_read_attachment(SimpleNamespace(
        tender_id=TENDER_ID, filename="scanned.txt", offset=0, limit=10,
    ))
    assert "error" in result, "reading a no_text_layer file should error"
    assert result["extraction_status"] == at.STATUS_NO_TEXT_LAYER, result
    assert "text" not in result, "an unreadable file must not return empty text"


def test_read_picks_up_a_change_without_a_list_call():
    """
    A read re-hashes the one file it serves, so it can never hand back text
    that no longer matches the document on disk — even if nobody called
    list-attachments in between.
    """
    _drop("sow.txt", "\n".join(f"SECOND AMENDMENT line {i}" for i in range(20)))
    result = tt.cmd_read_attachment(SimpleNamespace(
        tender_id=TENDER_ID, filename="sow.txt", offset=0, limit=5,
    ))
    assert "error" not in result, result
    assert result["text"].startswith("SECOND AMENDMENT line 0"), result["text"][:60]
    assert result["total_lines"] == 20, result


def test_list_before_attach_errors():
    result = tt.cmd_list_attachments(SimpleNamespace(tender_id="NO-SUCH-TENDER"))
    assert "error" in result, "listing a nonexistent tender should error"


def main():
    setup_temp_vault()
    test_attach_creates_folder()
    test_attach_is_idempotent_and_keeps_provenance()
    test_attach_unknown_tender_rejected()
    test_manifest_written_with_lf()
    test_extracts_a_dropped_file()
    test_unsupported_type_still_listed()
    test_no_text_layer_writes_no_file()
    test_unchanged_file_is_not_re_extracted()
    test_amended_document_detected_and_re_extracted()
    test_extractor_added_later_needs_no_re_drop()
    test_deleted_extracted_text_is_rebuilt()
    test_removed_file_drops_out_of_manifest()
    test_filename_with_awkward_characters_round_trips()
    test_read_is_paginated()
    test_read_offset_and_eof()
    test_read_limit_is_clamped()
    test_read_rejects_a_file_with_no_text()
    test_read_picks_up_a_change_without_a_list_call()
    test_list_before_attach_errors()
    print("All attachment tests passed.")


if __name__ == "__main__":
    main()
