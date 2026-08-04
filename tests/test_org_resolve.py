"""
Tests for the free-text organization resolver.

Runs with plain Python — no pytest needed:

    python tests/test_org_resolve.py

Exit code 0 = all passed. Any assertion failure = non-zero + traceback.

Every case here is a REAL failure observed in the OAG corpus while replacing
oag_ingest.py's KNOWN_DEPTS list and its regex fallback, not a hypothetical.
The old extractor scored 140 of 364 records with 4 junk values; the traps below
are what a naive replacement gets wrong, and each one silently attributes a
finding to a department that was never audited.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import org_resolve as orx  # noqa: E402

PASSED = FAILED = SKIPPED = 0


def check(cond: bool, label: str) -> None:
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  ok    {label}")
    else:
        FAILED += 1
        print(f"  FAIL  {label}")


def skip(label: str, why: str) -> None:
    global SKIPPED
    SKIPPED += 1
    print(f"  skip  {label} ({why})")


def scanned(resolver, text: str) -> set[str]:
    return set(resolver.scan(text))


def test_registry_has_no_ambiguous_phrase(resolver):
    """
    Two entries claiming one phrase is a registry defect, not a runtime choice.

    The resolver refuses to guess between them, so this must be empty or every
    scan touching that phrase quietly drops an organization.
    """
    check(not resolver.ambiguous_phrases(),
          f"no phrase is claimed by two entries ({resolver.ambiguous_phrases()})")


def test_bare_nouns_are_never_organizations(resolver):
    """
    Why this module does not reuse crosswalk.normalize_org.

    normalize_org strips "Department of" and a trailing "Canada" so two clean
    name FIELDS compare equal. Applied to prose it reduces canonical names to
    "health", "environment", "finance", "industry" — words an audit uses
    constantly. Scanning on those matched a Senate committee's title as
    Environment and Climate Change Canada 71 times.
    """
    cases = [
        ("environment", "the environment and natural resources of the region"),
        ("health", "Stanton Territorial Hospital reported on health outcomes"),
        ("finance", "the Standing Senate Committee on National Finance met"),
        ("industry", "Industrial and Technological Benefits for industry"),
    ]
    for noun, text in cases:
        check(not scanned(resolver, text),
              f"bare {noun!r} in prose resolves to nothing")


def test_parliamentary_committees_are_not_departments(resolver):
    """
    A committee is who a report was TABLED WITH, never who was audited.

    The sharpest case is real: the Standing Committee on Canadian Heritage heard
    Report 2 on rural connectivity, which audited ISED and the CRTC. Attributing
    that audit to Canadian Heritage invents a finding against a department the
    report does not mention.
    """
    cases = [
        "Briefing Package for the hearing before the Standing Committee on "
        "Fisheries and Oceans",
        "Standing Senate Committee on Energy, the Environment and Natural Resources",
        "Standing Committee on National Defence",
        "Standing Committee on Indigenous and Northern Affairs",
    ]
    for text in cases:
        check(not scanned(resolver, text),
              f"committee name yields no department: {text[:58]!r}")

    check(scanned(resolver, "Standing Committee on Canadian Heritage on Report 2, "
                            "Connectivity in Rural and Remote Areas") == set(),
          "the Canadian Heritage committee does not attribute to Canadian Heritage")

    # Stripping the committee must not eat the report's own subject after it.
    check(scanned(resolver, "hearing before the Standing Committee on Public Accounts "
                            "on Report 1, ArriveCAN, with Canada Border Services Agency")
          == {"cbsa"},
          "the subject after a committee name still resolves")


def test_the_auditor_is_not_the_audited(resolver):
    """
    The OAG publishes every record in the corpus and is named in 254 of 364.

    It is who wrote the audit, never who was audited — but `resolve()` still
    answers for it, because looking a name up is not attributing a finding.
    """
    check(not scanned(resolver, "Office of the Auditor General of Canada"),
          "the publisher is not scanned out of its own reports")
    check(not scanned(resolver, "2024 Reports of the Auditor General of Canada"),
          "an OAG report title attributes nothing to the OAG")
    check(resolver.resolve("Office of the Auditor General of Canada") == "auditor-general",
          "resolve() still identifies the OAG by name")


def test_prefix_collisions_never_fall_through_to_the_neighbour(resolver):
    """
    The `not:` lists, which nothing exercised before this module.

    Each of these opens with the COMPLETE and correct name of a different
    organization, so checking a refusal against the phrase that matched is
    useless — the phrase that matched is the short one. The longer string has to
    win the span first. Resolving to nothing is the correct outcome when the
    registry has no name for the longer body; resolving to its neighbour is not.
    """
    cases = [
        ("Royal Canadian Mounted Police External Review Committee", "rcmp"),
        ("Civilian Review and Complaints Commission for the "
         "Royal Canadian Mounted Police", "rcmp"),
    ]
    for text, must_not in cases:
        got = scanned(resolver, text)
        check(must_not not in got,
              f"{text[:52]!r} does not resolve to {must_not!r} (got {sorted(got)})")

    check(scanned(resolver, "the Royal Canadian Mounted Police investigated") == {"rcmp"},
          "the force itself still resolves when named alone")


def test_immigration_collision_is_two_institutions(resolver):
    """IRCC is the department; the IRB is the independent tribunal."""
    check(scanned(resolver, "the Immigration and Refugee Board of Canada heard it")
          == {"irb"}, "the tribunal resolves to irb")
    check(scanned(resolver, "Immigration, Refugees and Citizenship Canada processed")
          == {"ircc"}, "the department resolves to ircc")
    check(resolver.resolve("Immigration and Refugee Board") == "irb",
          "resolve() keeps the tribunal off ircc")


def test_multi_department_audits_yield_every_named_department(resolver):
    """
    Half the resolved federal audits name more than one department, so returning
    the first match — what the old list did — discarded the rest.
    """
    arrivecan = ("This audit focuses on whether the Canada Border Services Agency, "
                 "Public Health Agency of Canada, and Public Services and Procurement "
                 "Canada managed all aspects of the ArriveCAN application")
    check(scanned(resolver, arrivecan) == {"cbsa", "phac", "pspc"},
          "ArriveCAN resolves to all three departments")

    identity = ("This audit focused on whether the Treasury Board of Canada Secretariat, "
                "with the support of Shared Services Canada, Employment and Social "
                "Development Canada, and Innovation, Science and Economic Development "
                "Canada")
    check(scanned(resolver, identity) == {"tbs", "ssc", "esdc", "ised"},
          "Digital Validation of Identity resolves to all four")


def test_evidence_is_returned_with_every_hit(resolver):
    """An edge that cannot say which string produced it is not reviewable."""
    hits = resolver.scan("Fisheries and Oceans Canada and Parks Canada")
    check(hits.get("dfo") == "fisheries and oceans canada",
          f"dfo carries its matched phrase ({hits.get('dfo')!r})")
    check(hits.get("parks-canada") == "parks canada",
          f"parks-canada carries its matched phrase ({hits.get('parks-canada')!r})")


def test_resolve_takes_a_key_or_a_registered_name_and_nothing_else(resolver):
    """
    The shared identifier rule for every tool that filters on a department.

    Substring matching is exactly what the `not:` lists exist to prevent, so a
    fragment must fail rather than land on whichever entry happens to contain it.
    """
    check(resolver.resolve("pspc") == "pspc", "a canonical key resolves to itself")
    check(resolver.resolve("Public Services and Procurement Canada") == "pspc",
          "a registered name resolves to its key")
    check(resolver.resolve("Fisheries and Oceans Canada") == "dfo",
          "a mechanical entry resolves by name")

    for junk in ("Public Services", "Fisheries", "Immigration", "not-a-dept"):
        check(resolver.resolve(junk) is None,
              f"{junk!r} does not substring-match its way to a department")


def test_scan_and_resolve_treat_bare_nouns_differently_on_purpose(resolver):
    """
    The asymmetry that looks like a bug and is the whole design.

    normalize_org strips a trailing "Canada", so "Health" is EXACTLY EQUAL to
    "Health Canada" after normalization, and `resolve("Health")` therefore
    returns health-canada. Ten registry names reduce to a single token that way,
    and none of them collides — each maps to exactly one key.

    That is safe for `resolve()` because the caller has handed over a COMPLETE
    identifier and the comparison is equality. It is unsafe for `scan()`, where
    the same word is one token inside a sentence and containment would fire on
    "improving health outcomes". So scan() indexes surface forms and enforces a
    two-token floor, and resolve() uses the project normalizer. Both directions
    are asserted here so neither is "fixed" into the other.
    """
    for noun, key in (("Health", "health-canada"), ("Transport", "transport-canada"),
                      ("Parks", "parks-canada"), ("Statistics", "statcan")):
        check(resolver.resolve(noun) == key,
              f"resolve({noun!r}) -> {key} (complete identifier, exact equality)")
        check(not scanned(resolver, f"a report on {noun.lower()} outcomes in Canada"),
              f"scan() refuses bare {noun.lower()!r} inside prose")


def test_observed_names_reach_their_key(resolver):
    """The four names added from the OAG discovery pass, in the forms OAG uses."""
    cases = [
        ("whether the Canadian Armed Forces recruited and trained members", "dnd"),
        ("the Canadian Coast Guard fleet renewal", "dfo"),
        ("Correctional Service Canada's programs respond to diversity", "csc"),
        ("Correctional Services Canada reported", "csc"),
        ("whether Infrastructure Canada designed a Climate Lens approach",
         "housing-infrastructure"),
    ]
    for text, key in cases:
        check(key in scanned(resolver, text), f"{text[:52]!r} -> {key}")


def main() -> int:
    resolver = orx.OrgResolver()
    if not resolver.aliases:
        print("no org_aliases.yaml; nothing to test")
        return 0

    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(f"\n{name}")
            fn(resolver)

    print(f"\n{PASSED} passed, {FAILED} failed, {SKIPPED} skipped")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
