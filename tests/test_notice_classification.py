"""
Lock the DIRECTION of notice classification.

Runs with plain Python — no pytest needed:
    python tests/test_notice_classification.py

The failure mode this guards is inverted labels, not missing ones. The previous
implementation substring-matched "supply arrangement" against the notice type,
which meant:

  "RFP against Supply Arrangement"  -> qualification   WRONG, it is work
  "Invitation to Qualify"           -> work            WRONG, it is qualification

Both are backwards, and backwards is worse than absent here. A qualification
mislabelled as work sends you to write a proposal for a vehicle that awards
nothing; a call-up mislabelled as qualification hides the one notice on the
page that is actually buying something. Neither reads as broken — they read as
confident answers — which is exactly why they need a test that names them.

The assertions below are written as directions ("X must resolve to Y, and must
NOT resolve to Z") rather than as a snapshot of the current output, so a future
refactor that reintroduces substring matching fails here loudly.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from ingest import classify_notice  # noqa: E402
import tender_tools  # noqa: E402


FAILURES: list[str] = []


def check(label: str, got, want) -> None:
    if got == want:
        print(f"  PASS  {label}")
    else:
        FAILURES.append(f"{label}: expected {want!r}, got {got!r}")
        print(f"  FAIL  {label}: expected {want!r}, got {got!r}")


def test_the_two_inversions() -> None:
    """The two cases that were backwards. These are the point of this file."""
    print("\nThe two inversions:")

    call_up = classify_notice("RFP against Supply Arrangement", "*SRV")
    check("'RFP against Supply Arrangement' -> call_up (work, NOT qualification)",
          call_up["opportunity_kind"], "call_up")
    if call_up["opportunity_kind"] == "qualification":
        FAILURES.append(
            "REGRESSION: substring matching is back. 'RFP against Supply "
            "Arrangement' contains the words 'supply arrangement' but is the "
            "opposite of a qualification.")

    itq = classify_notice("Invitation to Qualify", "*SRV")
    check("'Invitation to Qualify' -> qualification (NOT work)",
          itq["opportunity_kind"], "qualification")


def test_qualification_family() -> None:
    print("\nQualification instruments:")
    for nt in ("Request for Supply Arrangement", "Request for Standing Offer",
               "Invitation to Qualify"):
        r = classify_notice(nt, "*SRV")
        check(f"{nt!r} -> qualification", r["opportunity_kind"], "qualification")
        if not r.get("kind_note"):
            FAILURES.append(f"{nt!r} carries no kind_note explaining the consequence")


def test_matching_is_exact_not_substring() -> None:
    """A notice type that merely CONTAINS a known phrase must not inherit it."""
    print("\nExact matching:")
    for nt, forbidden in [
        ("RFP against Supply Arrangement", "qualification"),
        ("Notice of a future Request for Standing Offer competition", "qualification"),
    ]:
        r = classify_notice(nt, "*SRV")
        got = r["opportunity_kind"]
        if got == forbidden:
            FAILURES.append(
                f"{nt!r} substring-matched into {forbidden!r}; matching must be "
                f"exact after casefolding")
            print(f"  FAIL  {nt!r} must not be {forbidden!r}")
        else:
            print(f"  PASS  {nt!r} is not {forbidden!r} (got {got!r})")


def test_category_fallback() -> None:
    """With no notice type, procurementCategory carries the split."""
    print("\nCategory fallback (no notice type — MX/PW/SSC file none):")
    check("construction only -> construction",
          classify_notice(None, "*CNST")["opportunity_kind"], "construction")
    check("goods only -> product",
          classify_notice(None, "*GD")["opportunity_kind"], "product")
    check("services -> solicitation (residual)",
          classify_notice(None, "*SRV")["opportunity_kind"], "solicitation")
    check("multi-valued '*GD\\n*SRV' -> solicitation, not product",
          classify_notice(None, "*GD\n*SRV")["opportunity_kind"], "solicitation")
    check("nothing at all -> unknown",
          classify_notice(None, None)["opportunity_kind"], "unknown")

    # The residual must not claim to be a positive finding.
    r = classify_notice(None, "*SRV")
    check("services residual is marked as residual in kind_basis",
          r["kind_basis"], "procurement_category_residual")


def test_notice_type_wins_over_category() -> None:
    """Instrument shape is decided before what is being bought."""
    print("\nPrecedence:")
    r = classify_notice("Request for Supply Arrangement", "*CNST")
    check("a construction RFSA is still a qualification",
          r["opportunity_kind"], "qualification")
    check("...and says the notice type decided it", r["kind_basis"], "notice_type")


def test_results_notices_are_not_solicitations() -> None:
    """A posting that announces who already qualified is not open work."""
    print("\nResults notices:")
    ehrp = ("This is a notice only. No solicitation document is associated with "
            "this notice. Its purpose is to announce the results of the EHRP ITQ.")
    # Typed 'Request for Proposal' in the real feed — the prose has to win.
    r = classify_notice("Request for Proposal", "*SRV", ehrp)
    check("EHRP results notice -> results_notice, not solicitation",
          r["opportunity_kind"], "results_notice")

    drs = "Digital Registry Solution (DRS) Project - List of Qualified Suppliers following The ITQ"
    check("DRS results notice -> results_notice",
          classify_notice(None, "*SRV", drs)["opportunity_kind"], "results_notice")

    # The phrase that looks perfect and isn't: ordinary RFSAs describe the list
    # they will produce. It must not fire on its own.
    rfsa = ("Supply arrangement for cables and wires. A list of qualified "
            "suppliers will be established under this arrangement.")
    got = classify_notice("Request for Supply Arrangement", "*GD", rfsa)
    check("an RFSA merely describing a future supplier list stays qualification",
          got["opportunity_kind"], "qualification")


def test_call_up_recovery_from_prose() -> None:
    """
    Recover call-ups the notice-type field missed — and be honest that the
    arrangement number alone does not get all of them.
    """
    print("\nCall-up recovery:")
    with_number = ("open only to Indigenous Supply Arrangement (SA) Holders who "
                   "pre-qualified against the TBIPS Supply Arrangement # EN578-170432.")
    r = classify_notice("Request for Proposal", "*SRV", with_number)
    check("cites EN578-170432 -> call_up", r["opportunity_kind"], "call_up")
    check("...and says the number decided it",
          r["kind_basis"], "prose_arrangement_number")

    # The two DND notices carry NO arrangement number and never say "supply
    # arrangement" — only the vehicle name in the title. The number rule alone
    # catches 1 of the 3 real cases; this is the other 2.
    name_only = ("TBIPS - Implementation and Deployment of Cloud Analytics "
                 "Solutions. DJDCP will require informatics professional services.")
    r = classify_notice("Request for Proposal", "*SRV", name_only)
    check("names TBIPS with no number -> call_up",
          r["opportunity_kind"], "call_up")
    check("...and says the weaker evidence decided it",
          r["kind_basis"], "prose_vehicle_name")


def test_construction_survives_prose_rules() -> None:
    """
    The regression that a format-based number rule caused, locked out.

    PSPC solicitation numbers share the shape of supply-arrangement numbers, so
    matching the FORMAT relabelled a wharf reconstruction, a building
    demolition and a lift-bridge gate as IT call-ups. Two defences: the
    arrangement list is exact, and construction is decided before any prose
    rule runs.
    """
    print("\nConstruction cannot be relabelled by prose:")
    wharf = ("EE517-261427 - RFP Reconstruction of the fishermen's wharf at "
             "Cap-aux-Meules in the Magdalen Islands")
    r = classify_notice("Request for Proposal", "*CNST", wharf)
    check("wharf with a PSPC-format number stays construction",
          r["opportunity_kind"], "construction")
    if r["opportunity_kind"] == "call_up":
        FAILURES.append(
            "REGRESSION: the arrangement-number rule is matching the PSPC "
            "number FORMAT again instead of the known-arrangement list.")

    # An unknown number in a services notice must not invent a vehicle.
    r = classify_notice("Request for Proposal", "*SRV", "Solicitation EQ754-270127 for services")
    check("an unknown PSPC number does not create a call_up",
          r["opportunity_kind"], "solicitation")


def test_jurisdiction_uses_the_registry() -> None:
    """Federal is decided by the registry; non-federal needs a positive signal."""
    print("\nJurisdiction:")
    from ingest import classify_jurisdiction

    fed = classify_jurisdiction("Department of National Defence (DND)", None)
    check("a registered department -> federal", fed["jurisdiction"], "federal")
    check("...decided by the registry", fed["jurisdiction_basis"], "org_registry")

    terr = classify_jurisdiction("Government of the Northwest Territories (GNWT)", None)
    check("GNWT -> non_federal", terr["jurisdiction"], "non_federal")

    # The distinction that keeps the best tender in the corpus: a federal Crown
    # corporation is absent from a registry of departments, and absent must not
    # read as provincial.
    crown = classify_jurisdiction("Canada Deposit Insurance Corporation (CDIC)", None)
    check("CDIC -> unrecognised, NOT non_federal",
          crown["jurisdiction"], "unrecognised")
    if crown["jurisdiction"] == "non_federal":
        FAILURES.append(
            "CDIC classified non_federal — a registry miss is being treated as "
            "evidence of provincial jurisdiction, and this drops federal Crown "
            "corporations including the strongest tender in the corpus.")


def test_body_date_conflict() -> None:
    """A prose deadline earlier than the field is reported; later is not."""
    print("\nClosing-date conflict:")
    from ingest import body_date_conflict

    esdc = ("AMENDMENT TO RESPONSE DEADLINE CLOSING TIME: Please disregard the "
            "Ariba Discovery posting response deadline. All required supporting "
            "documentation and bid submission must be submitted on August 07, "
            "2026 at 1400h (2:00 pm) EDT.")
    check("body date earlier than the field is reported",
          body_date_conflict(esdc, "2026-08-21"), "2026-08-07")
    check("no conflict when the field already matches",
          body_date_conflict(esdc, "2026-08-07"), None)
    check("a LATER body date is not reported (that is an extension)",
          body_date_conflict("bids must be submitted on 30 September 2026", "2026-08-21"),
          None)
    check("prose with no deadline yields nothing",
          body_date_conflict("This contract runs to March 2028.", "2026-08-21"), None)


def test_one_definition_not_two() -> None:
    """tender_tools must USE this function, not carry its own copy."""
    print("\nSingle definition:")
    if tender_tools._classify_notice is not classify_notice:
        FAILURES.append(
            "tender_tools._classify_notice is not ingest.classify_notice — the "
            "dossier and the ingest filter have drifted into two definitions")
        print("  FAIL  tender_tools does not import the shared classifier")
    else:
        print("  PASS  tender_tools imports the shared classifier")

    if hasattr(tender_tools, "_QUALIFICATION_NOTICES"):
        FAILURES.append(
            "tender_tools._QUALIFICATION_NOTICES still exists — the substring "
            "table that caused the inversion should be gone, not shadowed")
        print("  FAIL  the old substring table is still present")
    else:
        print("  PASS  the old substring table is gone")

    from ingest import entity_org_keys
    if tender_tools._entity_keys("Department of National Defence (DND)") != \
            entity_org_keys("Department of National Defence (DND)"):
        FAILURES.append(
            "tender_tools._entity_keys and ingest.entity_org_keys disagree — "
            "the federal check and the dossier attribution have forked")
        print("  FAIL  entity resolution has two behaviours")
    else:
        print("  PASS  entity resolution is one definition")


def main() -> int:
    print("=" * 72)
    print("Notice classification — direction tests")
    print("=" * 72)
    test_the_two_inversions()
    test_qualification_family()
    test_matching_is_exact_not_substring()
    test_category_fallback()
    test_notice_type_wins_over_category()
    test_results_notices_are_not_solicitations()
    test_call_up_recovery_from_prose()
    test_construction_survives_prose_rules()
    test_jurisdiction_uses_the_registry()
    test_body_date_conflict()
    test_one_definition_not_two()

    print("\n" + "=" * 72)
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S):")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("All notice-classification checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
