"""
Tests for the tender lifecycle: promote → park → archive.

This is the code most likely to silently corrupt data during a future
refactor (it moves and rewrites the user's files), so it gets a test even
though the rest of the project doesn't.

Runs with plain Python — no pytest needed:

    python tests/test_lifecycle.py

Exit code 0 = all passed. Any assertion failure = non-zero + traceback.

Strategy: we don't touch ChromaDB. We seed tender_tools' module state with
a fake document index and a sentinel collection object so load_collection()
short-circuits, then point the vault paths at a temp directory.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import tender_tools as tt  # noqa: E402


FAKE_ID = "TEST-0001"
FAKE_DOC = {
    "id": FAKE_ID,
    "metadata": {
        "tender_id": FAKE_ID,
        "title": "Test Tender for Lifecycle",
        # Two separate fields, as ingest.build_chroma writes them. The display
        # layer picks end user first and falls back to the contracting entity.
        "contracting_entity": "Shared Services Canada",
        "end_user_entity": "Test Agency",
        "closing_date": "2099-01-01",
        "estimated_value": 500000.0,
        "matched_competencies": "cloud,DevOps",
    },
    "document": "Test Tender for Lifecycle\n\nA fake tender description.",
}


def setup_temp_vault() -> Path:
    """Point tender_tools at a temp vault and seed a fake corpus."""
    root = Path(tempfile.mkdtemp(prefix="tender-vault-test-"))
    tt.PROJECT_ROOT = root
    tt.VAULT = root / "vault"
    tt.WATCHING = tt.VAULT / "tenders" / "watching"
    tt.PARKED = tt.VAULT / "tenders" / "parked"
    tt.ARCHIVED = tt.VAULT / "tenders" / "archived"
    tt.WATCHING.mkdir(parents=True)

    # Short-circuit ChromaDB loading: non-None sentinel + seeded index
    tt._collection = object()
    tt.doc_index = [FAKE_DOC]
    return root


def test_promote():
    result = tt.cmd_promote(SimpleNamespace(tender_id=FAKE_ID))
    assert "promoted" in result, f"promote failed: {result}"
    files = list(tt.WATCHING.glob("*.md"))
    assert len(files) == 1, f"expected 1 watching file, got {len(files)}"
    content = files[0].read_text(encoding="utf-8")
    assert f"tender_id: {FAKE_ID}" in content, "frontmatter missing tender_id"
    assert "status: watching" in content, "frontmatter missing status"
    return files[0].name


def test_promote_writes_department_links(filename: str):
    """
    The department link, the attribution, and the registry miss — in the file.

    FAKE_DOC is deliberately the awkward case rather than the easy one: its end
    user ("Test Agency") resolves to nothing and its contracting entity
    ("Shared Services Canada") resolves to ssc. So a correct promote has to link
    ssc, say out loud that ssc is the buyer of record rather than the customer,
    and keep the unresolved string instead of dropping it. An easy case where
    both fields name the same recognised department would assert none of that.
    """
    content = (tt.WATCHING / filename).read_text(encoding="utf-8")

    # Quoted, because a bare [[ssc]] is a nested YAML sequence, not a string.
    assert 'department: ["[[ssc]]"]' in content, (
        "department is not a quoted wikilink list on the canonical key"
    )
    assert "entity_source: [contracting_entity_end_user_unstated]" in content, (
        "entity_source must run parallel to department and say how it was reached"
    )
    assert 'department_unresolved: ["Test Agency"]' in content, (
        "an unresolved entity string is evidence and must survive the promote"
    )
    # The attribution has to be legible without going and reading frontmatter.
    assert "**Departments:** [[ssc]] (contracting entity; end user unstated)" in content, (
        "body does not name the department or how it was attributed"
    )
    assert "**Attribution note:**" in content and "not a stated end user" in content, (
        "body does not spell out that the attribution is via the contracting entity"
    )
    assert "*not a department*, not *not federal*" in content, (
        "body does not distinguish a registry miss from a non-federal finding"
    )


def test_promote_duplicate_rejected():
    result = tt.cmd_promote(SimpleNamespace(tender_id=FAKE_ID))
    assert "error" in result, "second promote of same tender should error"


def test_park(filename: str):
    result = tt.cmd_park(SimpleNamespace(
        filename=filename,
        reason="test reason",
        revisit_when="after test trigger",
    ))
    assert "parked" in result, f"park failed: {result}"
    assert not (tt.WATCHING / filename).exists(), "file still in watching after park"
    parked_file = tt.PARKED / filename
    assert parked_file.exists(), "file not in parked after park"
    content = parked_file.read_text(encoding="utf-8")
    assert "**Reason:** test reason" in content, "park reason not appended"
    assert "**Revisit when:** after test trigger" in content, "revisit trigger not appended"


def test_park_missing_file_rejected():
    result = tt.cmd_park(SimpleNamespace(
        filename="does-not-exist.md", reason="x", revisit_when="y",
    ))
    assert "error" in result, "parking a nonexistent file should error"


def test_list_parked(filename: str):
    result = tt.cmd_list_parked(SimpleNamespace())
    assert len(result["parked"]) == 1, f"expected 1 parked, got {result}"
    entry = result["parked"][0]
    assert entry["filename"] == filename
    assert entry["tender_id"] == FAKE_ID
    assert entry["revisit_when"] == "after test trigger", (
        f"revisit trigger not extracted: {entry}"
    )


def test_archive_from_parked(filename: str):
    result = tt.cmd_archive(SimpleNamespace(
        filename=filename, reason="test close-out",
    ))
    assert "archived" in result, f"archive failed: {result}"
    assert result["from"] == "parked", f"expected source 'parked', got {result}"
    assert not (tt.PARKED / filename).exists(), "file still in parked after archive"
    archived_file = tt.ARCHIVED / filename
    assert archived_file.exists(), "file not in archived"
    content = archived_file.read_text(encoding="utf-8")
    assert "test close-out" in content, "archive reason not appended"
    assert "(from parked)" in content, "archive source annotation missing"
    # The full history should be readable in one file: park then archive
    assert content.index("## Parked") < content.index("## Archived"), (
        "lifecycle annotations out of order"
    )


def test_archive_from_watching():
    """Separate path: archive directly from watching without parking first."""
    tt.cmd_promote(SimpleNamespace(tender_id=FAKE_ID))
    filename = next(tt.WATCHING.glob("*.md")).name
    result = tt.cmd_archive(SimpleNamespace(filename=filename, reason="direct kill"))
    assert result.get("from") == "watching", f"expected source 'watching': {result}"


def main():
    setup_temp_vault()
    filename = test_promote()
    test_promote_writes_department_links(filename)
    test_promote_duplicate_rejected()
    test_park(filename)
    test_park_missing_file_rejected()
    test_list_parked(filename)
    test_archive_from_parked(filename)
    # Re-promote is possible after archive (file no longer in watching)
    test_archive_from_watching()
    print("All lifecycle tests passed.")


if __name__ == "__main__":
    main()
