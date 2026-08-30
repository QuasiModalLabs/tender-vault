"""
Tests for the pre-mortem: the invariants, not the wording.

    python tests/test_premortem.py

Exit code 0 = all passed.

WHAT IS WORTH TESTING HERE is not that a probe matches a phrase — patterns are
tuned by reading real notices, and a test asserting one fires on a string it was
written against proves only that the string was copied twice. What is worth
testing is the set of properties a future refactor could quietly destroy while
every probe still fires:

  - nothing in the output ranks, scores or tallies anything;
  - a probe that did not fire is still reported, with its own state;
  - there is no lobbying section, and adding one has to break a test;
  - `estimated_value: null` in a vault note decodes to None, not "null";
  - a vehicle status is captured whole rather than to the end of its line.

The corpus is faked the way tests/test_lifecycle.py fakes it — a sentinel
collection object and a seeded doc index, so load_collection() short-circuits
and nothing here needs ChromaDB or the embedding model.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent))
import conftest  # noqa: E402  — MUST come first: redirects the vault on import
import tender_tools as tt  # noqa: E402
from tender_tools import premortem  # noqa: E402


CORPUS_ID = "TEST-PM-0001"

# Deliberately dense: it fires probes under BOTH lenses, so the disjointness and
# no-score checks run against a populated output rather than an empty one.
CORPUS_DOC = {
    "id": CORPUS_ID,
    "metadata": {
        "tender_id": CORPUS_ID,
        "title": "Application modernization services",
        "contracting_entity": "Shared Services Canada",
        "end_user_entity": "",
        "closing_date": "2099-01-01",
        "opportunity_kind": "call_up",
        "kind_basis": "prose_vehicle_name",
        "matched_competencies": "cloud,modernization",
    },
    "document": (
        "Application modernization services.\n"
        "This is a call-up against the TBIPS supply arrangement EN578-170432.\n"
        "The requirement is for 12 resources, 150 days per resource.\n"
        "Bidders must have completed similar projects and hold Secret clearance.\n"
        "Evaluation is on the highest combined rating of technical merit and price.\n"
    ),
}

# No probe vocabulary at all, so every probe must come back `absent`.
QUIET_ID = "TEST-PM-0002"
QUIET_DOC = {
    "id": QUIET_ID,
    "metadata": {
        "tender_id": QUIET_ID,
        "title": "Quiet notice",
        "contracting_entity": "Shared Services Canada",
        "end_user_entity": "",
        "closing_date": "2099-01-01",
        "matched_competencies": "",
    },
    "document": "A short description that says nothing about how it will be bought.",
}


def setup() -> Path:
    root = conftest.redirect_vault()
    tt.paths.WATCHING.mkdir(parents=True)
    tt.paths.PARKED.mkdir(parents=True)
    tt.paths.ARCHIVED.mkdir(parents=True)
    tt.corpus._collection = object()
    tt.corpus.doc_index = [CORPUS_DOC, QUIET_DOC]
    return root


def run(tender_id: str) -> dict:
    return tt.cmd_pre_mortem(SimpleNamespace(tender_id=tender_id))


def _walk(node, path="$"):
    """Every (path, key, value) in a nested result, for the structural checks."""
    if isinstance(node, dict):
        for k, v in node.items():
            yield f"{path}.{k}", k, v
            yield from _walk(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _walk(v, f"{path}[{i}]")


# --------------------------------------------------------------------------
# The architectural invariants. These are the tests that exist.

def test_nothing_scores_or_ranks():
    """
    No score, no rank, no severity, no tally — the dossier's rule, applied here.

    Checked as a property of the whole output rather than of one section,
    because the tempting place to add a number is a new section rather than an
    existing one. A count of fired probes is the specific failure this is
    watching for: it reads as a summary and is a severity score with the
    arithmetic hidden.
    """
    result = run(CORPUS_ID)
    banned = ("score", "rank", "rating", "severity", "weight", "confidence",
              "risk_level", "fired_count", "signals_fired", "total_signals")
    for path, key, _value in _walk(result):
        assert key not in banned, f"scoring key {key!r} appeared at {path}"

    # And no integer count of probe states anywhere, under any name.
    for lens in (premortem.LOST, premortem.REGRETTED):
        section = result[lens]
        for key, value in section.items():
            assert not (isinstance(value, int) and key != "n"), (
                f"{lens}.{key} is a bare integer; a per-lens count is a score"
            )


def test_no_lobbying_section():
    """
    Lobbying is omitted on purpose, and removing that omission must fail here.

    vault/CLAUDE.md forbids offering a meeting as the explanation for an award
    or a requirement. A pre-mortem asks "why did we lose", so a lobbying section
    would be read as the answer whatever caveat sat above it — the omission is a
    design decision, not an oversight, and an oversight is how it comes back.
    """
    result = run(CORPUS_ID)
    for path, key, value in _walk(result):
        assert "lobby" not in key.lower(), f"lobbying key at {path}"
        if isinstance(value, str):
            # The module's own explanation of the omission is allowed to say the
            # word; a data section is not.
            assert not (path.endswith(".vendor") and "lobby" in value.lower())


def test_both_lenses_present_and_probes_disjoint():
    """Every probe belongs to exactly one lens, and both lenses are populated."""
    result = run(CORPUS_ID)
    lost = result[premortem.LOST]["notice_signals"]
    regretted = result[premortem.REGRETTED]["notice_signals"]
    assert lost and regretted, "both lenses must carry probes"

    ids_lost = {p["probe"] for p in lost}
    ids_regretted = {p["probe"] for p in regretted}
    assert not ids_lost & ids_regretted, "a probe appears under both lenses"
    assert ids_lost | ids_regretted == {p["id"] for p in premortem._PROBES}


def test_absent_probes_are_reported():
    """
    A probe that did not fire is a finding, not silence.

    The whole point of reporting `absent` is that it is NOT a statement about
    the solicitation package, so the note that says so has to be present too —
    dropping it turns "the notice does not mention clearance" into "the work
    needs no clearance".
    """
    result = run(QUIET_ID)
    probes = (result[premortem.LOST]["notice_signals"]
              + result[premortem.REGRETTED]["notice_signals"])
    assert len(probes) == len(premortem._PROBES), "probes went missing"
    for probe in probes:
        assert probe["state"] == "absent", f"{probe['probe']} fired on a quiet notice"
        assert probe["quotes"] == []
        assert "absent_note" in probe, f"{probe['probe']} lost its absent_note"


def test_fired_probe_carries_its_quote():
    """A fired probe without the sentence that fired it is unreviewable."""
    result = run(CORPUS_ID)
    probes = (result[premortem.LOST]["notice_signals"]
              + result[premortem.REGRETTED]["notice_signals"])
    fired = [p for p in probes if p["state"] == "fired"]
    assert fired, "the seeded notice should fire something"
    for probe in fired:
        assert probe["quotes"], f"{probe['probe']} fired with no quote"
        assert probe["matched_patterns"], f"{probe['probe']} fired with no pattern"


def test_seat_based_pricing_catches_counted_bodies():
    """
    The regression this probe was widened for.

    It stayed silent on "15 resources in various TBIPS positions, part-time
    based (150 days per resource)" — the profile's central exclusion, stated
    about as plainly as a notice ever states it, matched by none of the supply
    arrangement rate-card vocabulary the probe started with.
    """
    result = run(CORPUS_ID)
    seat = next(p for p in result[premortem.REGRETTED]["notice_signals"]
                if p["probe"] == "seat_based_pricing")
    assert seat["state"] == "fired", "counted bodies no longer detected"


# --------------------------------------------------------------------------
# The two decoding bugs, frozen.

def test_null_estimated_value_from_a_vault_note_is_none():
    """
    `estimated_value: null` must decode to None, never the string "null".

    promote writes that line for a notice the feed gave no value for. Read back
    verbatim it is a four-character string: truthy, printed as `"null"`, and it
    defeats the one rule this field has — absent means unknown, never zero and
    never free. The corpus path returns a real None, so leaving this as text
    also made two readings of one tender disagree about the type of one key.
    """
    # Named for the slug of the id, because that is how _note_for_tender
    # resolves one — promote writes the same name.
    note = tt.paths.WATCHING / "vault-only-1.md"
    note.write_text(
        "---\n"
        "tender_id: VAULT-ONLY-1\n"
        'title: "A promoted tender"\n'
        "department: [\"[[ssc]]\"]\n"
        "closing_date: 2099-01-01\n"
        "estimated_value: null\n"
        "matched_competencies: [cloud]\n"
        "---\n\n"
        "## Description\n\nSome work.\n",
        encoding="utf-8", newline="\n",
    )
    result = run("VAULT-ONLY-1")
    assert result["subject"]["estimated_value"] is None, (
        f"got {result['subject']['estimated_value']!r}"
    )
    assert result["subject"]["read_from"].startswith("vault note")
    assert result["subject"]["departments"] == ["ssc"]


def test_vehicle_status_is_captured_whole():
    """
    A status is a statement, not a line.

    Line-anchored capture cut one entry at "and unlike TBIPS, SBIPS and", which
    reads as a complete status and is not one. The qualifier that got dropped —
    that this vehicle, alone among them, expires — is the part a reader needs.
    """
    reference = tt.paths.VAULT / "reference"
    reference.mkdir(parents=True, exist_ok=True)
    (reference / "vehicles.md").write_text(
        "# Vehicles\n\n"
        "## TBIPS — EN578-170432\n\n"
        "**Status:** not held.\n\n"
        "Some prose.\n\n"
        "## An expiring one\n\n"
        "**Status:** not qualified. **Closes 2026-09-10** — and unlike the\n"
        "others, **this one expires.**\n\n"
        "More prose.\n",
        encoding="utf-8", newline="\n",
    )
    vehicles = premortem._vehicle_statuses()
    assert vehicles["state"] == "read"
    by_name = {v["vehicle"]: v["status"] for v in vehicles["vehicles"]}
    assert by_name["TBIPS — EN578-170432"] == "not held."
    expiring = by_name["An expiring one"]
    assert expiring.endswith("**this one expires.**"), expiring
    assert "\n" not in expiring, "the status should be folded to one line"


def test_unknown_tender_errors_rather_than_inventing_one():
    result = run("NO-SUCH-TENDER")
    assert "error" in result
    assert "neither the corpus nor the vault" in result["error"]


def test_not_checked_always_names_the_package():
    """
    The solicitation package is unread by design, and saying so is not optional.

    It is where mandatory requirements, the evaluation grid and the security
    schedule live. A pre-mortem that omitted the gap would read as though they
    had been checked.
    """
    for tender_id in (CORPUS_ID, QUIET_ID):
        gaps = {g["gap"] for g in run(tender_id)["not_checked"]}
        assert gaps & {"solicitation_package", "solicitation_package_not_read_here"}
        assert "notice_text_only" in gaps


if __name__ == "__main__":
    setup()
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for test in tests:
        test()
        print(f"  ok  {test.__name__}")
    print(f"\n{len(tests)} passed")
