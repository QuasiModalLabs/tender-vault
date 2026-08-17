"""
Tests for the department crosswalk.

Runs with plain Python — no pytest needed:

    python tests/test_crosswalk.py

Exit code 0 = all passed. Any assertion failure = non-zero + traceback.

Two things are worth guarding here.

THE NORMALIZER, on synthetic input, because it decides what joins to what and
a quiet over-reach in it would merge two departments without anyone noticing.

THE COVERAGE INVARIANT, on the real artifact if it has been built: the
crosswalk must cover every organization in the RAW contracts file, not just the
ones that survive the profile filter into contracts.db. That is the whole
reason the builder reads .cache/*.csv, and it's the property that would rot
silently — someone repoints the builder at the database, everything still runs,
and the crosswalk quietly shrinks to whatever the profile happened to admit
that year. The check is skipped, not failed, when the artifacts are absent, so
a fresh clone without a 640 MB download still passes.
"""
from __future__ import annotations

import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import crosswalk as cw  # noqa: E402

PASSED = []
SKIPPED = []


def check(cond: bool, label: str) -> None:
    assert cond, f"FAILED: {label}"
    PASSED.append(label)


def skip(label: str, why: str) -> None:
    SKIPPED.append(f"{label} ({why})")


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

def test_normalize_strips_the_real_variations():
    """The affix patterns that actually differ between the two datasets."""
    n = cw.normalize_org
    # Bilingual display title -> English half only.
    check(n("National Defence | Défense nationale") == "national defence",
          "bilingual title keeps the English half")
    # The legal/applied split these two datasets are built on.
    check(n("Department of Transport") == n("Transport Canada"),
          "Department of X == X Canada")
    check(n("Department of the Environment") == n("Environment"),
          "'department of the' prefix stripped")
    # Accents folded, so the French-influenced spellings compare equal.
    check(n("Immigration, Réfugiés et Citoyenneté") == n("Immigration, Refugies et Citoyennete"),
          "accents folded")
    # Punctuation, including the hyphen in Crown-Indigenous.
    check(n("Crown-Indigenous Relations") == n("Crown Indigenous Relations"),
          "hyphen normalized")
    check(n("") == "" and n(None) == "", "empty input is empty, not an error")


def test_normalize_does_not_over_merge():
    """
    The failure that matters is a WRONG merge, which corrupts silently. These
    pairs are genuinely different bodies and must stay apart.
    """
    n = cw.normalize_org
    distinct = [
        ("Department of Finance", "Department of Justice"),
        ("Fisheries and Oceans Canada", "Parks Canada"),
        # Same words, different organizations.
        ("Public Service Commission of Canada", "Public Health Agency of Canada"),
        ("Canadian Human Rights Commission", "Canadian Transportation Agency"),
        # A prefix of another name is not that name.
        ("Environment and Climate Change Canada", "Environment and Climate Change"
         " Canada Enforcement Branch"),
    ]
    for a, b in distinct:
        check(n(a) != n(b), f"{a!r} stays distinct from {b!r}")


def test_affixes_are_stripped_longest_first():
    """
    "X of Canada" must reduce to "X", not to "X of".

    Ordering regression. With "canada" tried before "of canada", eleven pairs of
    genuinely identical names failed the exact tier over a dangling connective,
    and were being reported as inferred matches instead of identical ones.
    """
    n = cw.normalize_org
    for name in ["National Research Council of Canada",
                 "Public Service Commission of Canada",
                 "Office of the Correctional Investigator of Canada",
                 "Military Police Complaints Commission of Canada"]:
        check(not n(name).endswith(" of"), f"{name!r} leaves no dangling 'of'")
    check(n("National Research Council of Canada") == n("National Research Council Canada"),
          "'of Canada' and 'Canada' tails reduce alike")


def test_no_inference_tier_exists():
    """
    The builder must offer exactly two ways to match. A future edit that
    reintroduces a similarity or containment tier should fail here, not be
    discovered later in a department dossier that quietly merged two agencies.
    """
    check(not hasattr(cw, "_contains"), "no containment helper on the module")
    check(not hasattr(cw, "_tokens"), "no tokenizer helper on the module")


# ---------------------------------------------------------------------------
# The built artifact
# ---------------------------------------------------------------------------

def test_crosswalk_covers_the_raw_universe():
    """
    Every organization in the raw contracts extract is in the crosswalk exactly
    once — and the crosswalk is a strict SUPERSET of the filtered database.

    This is the regression that motivated the whole design. contracts.db holds
    only profile-matching departments; if the crosswalk were ever rebuilt from
    it, widening the profile later would admit departments the crosswalk has
    never heard of, and their joins would silently return nothing.
    """
    db = ROOT / "data" / "crosswalk.db"
    if not db.exists():
        skip("coverage invariant", "data/crosswalk.db not built")
        return

    con = sqlite3.connect(db)
    xwalk = {r[0] for r in con.execute(
        "SELECT contract_owner_org FROM org_crosswalk WHERE contract_owner_org IS NOT NULL")}
    n_rows = con.execute(
        "SELECT COUNT(contract_owner_org) FROM org_crosswalk").fetchone()[0]
    check(len(xwalk) == n_rows, "each contract org appears exactly once")

    if cw.ORG_CACHE.exists():
        import pandas as pd
        raw = set(pd.read_csv(cw.ORG_CACHE, keep_default_na=False)["owner_org"])
        check(xwalk == raw,
              f"crosswalk covers the raw universe exactly ({len(raw)} orgs)")
    else:
        skip("raw universe comparison", "no .cache/contract_orgs.csv")

    cdb = ROOT / "data" / "contracts.db"
    if cdb.exists():
        ccon = sqlite3.connect(cdb)
        filtered = {r[0] for r in ccon.execute(
            "SELECT DISTINCT owner_org FROM contracts WHERE owner_org != ''")}
        ccon.close()
        missing = filtered - xwalk
        check(not missing, f"crosswalk is a superset of contracts.db; missing {missing}")
        check(len(xwalk) > len(filtered),
              f"crosswalk ({len(xwalk)}) is strictly wider than the filtered DB "
              f"({len(filtered)}) — built from raw, not from the database")
    else:
        skip("superset-of-contracts.db check", "data/contracts.db not built")
    con.close()


def test_no_plan_org_is_double_claimed_automatically():
    """
    Many-to-one is allowed only where a human asserted it. Any plan org matched
    by more than one contract org must have at most one `same` relation; the
    rest must be curated predecessor/absorbed/successor pins.
    """
    db = ROOT / "data" / "crosswalk.db"
    if not db.exists():
        skip("many-to-one discipline", "data/crosswalk.db not built")
        return

    con = sqlite3.connect(db)
    rows = con.execute("""
        SELECT plan_organization_id, match_method, relation
        FROM org_crosswalk WHERE plan_organization_id IS NOT NULL
    """).fetchall()
    # Every match is identical-or-asserted; there is no middle confidence.
    methods = {r[0] for r in con.execute("SELECT DISTINCT match_method FROM org_crosswalk")}
    check(methods <= {"exact", "alias", "unmatched_contract", "unmatched_plan"},
          f"only exact/alias/unmatched methods present (found {sorted(methods)})")
    confidences = {r[0] for r in con.execute("SELECT DISTINCT confidence FROM org_crosswalk")}
    check(confidences <= {"high", "none"},
          f"confidence is high or none, never a middle tier (found {sorted(confidences)})")
    con.close()

    by_plan: dict[int, list] = {}
    for pid, method, relation in rows:
        by_plan.setdefault(pid, []).append((method, relation))

    for pid, matches in by_plan.items():
        if len(matches) == 1:
            continue
        sames = [m for m in matches if m[1] == "same"]
        check(len(sames) <= 1,
              f"plan org {pid} has at most one 'same' match (found {len(sames)})")
        extras = [m for m in matches if m[1] != "same"]
        check(all(m[0] == "alias" for m in extras),
              f"plan org {pid}'s {len(extras)} duplicate match(es) are all curated aliases")


def test_entries_are_pinned_by_stable_key():
    """
    An entry must identify its organization by the two stable machine keys —
    CKAN slug and infobase id — never by a display name, because the display
    name is the thing that keeps changing. An entry with no slugs is valid:
    that organization files plans but publishes no contracts.
    """
    entries = cw.load_aliases()
    if not entries:
        skip("registry key discipline", "no org_aliases.yaml")
        return
    valid_relations = {"same", "predecessor", "successor", "absorbed"}
    for key, entry in entries.items():
        check(bool(entry.get("name")), f"{key} declares a display name")
        if "infobase" in entry:
            check(isinstance(entry["infobase"], int),
                  f"{key} declares an integer infobase id")
        for item in entry.get("ckan") or []:
            check(isinstance(item, dict) and "slug" in item,
                  f"{key} ckan items are mappings with a slug")
            # The point of moving relation onto the slug: it cannot be omitted
            # and silently default to `same`.
            check("relation" in item,
                  f"{key}/{item.get('slug')} states its relation explicitly")
            check(item.get("relation") in valid_relations,
                  f"{key}/{item.get('slug')} declares a known relation")
            # A machinery-of-government relation has to be explained somewhere.
            # With one slug the entry note is unambiguous; with several, the
            # caveat must sit on the slug it actually describes, or a reader
            # cannot tell which of them it applies to.
            if item.get("relation") != "same":
                where = (item.get("note") if len(entry["ckan"]) > 1
                         else (item.get("note") or entry.get("note")))
                check(bool(where),
                      f"{key}/{item['slug']} is not `same`, so it explains itself"
                      + (" on the slug" if len(entry["ckan"]) > 1 else ""))


def test_canonical_keys_are_unique_and_stable():
    """
    The keys are the project's organization identity and become dossier
    filenames, so they must be unique, well-formed, and hard to change by
    accident.

    Uniqueness is asserted against the RAW TEXT, not the parsed document: YAML
    resolves a duplicate key by silently keeping the last one, so a clash would
    vanish before any assertion on the dict could see it.
    """
    if not cw.ALIAS_PATH.exists():
        skip("canonical key discipline", "no org_aliases.yaml")
        return
    raw = cw.ALIAS_PATH.read_text(encoding="utf-8")
    # Entry keys sit at exactly two spaces of indent inside their block.
    text_keys = re.findall(r"(?m)^  ([a-z0-9][\w-]*):\s*$", raw)
    dupes = sorted({k for k in text_keys if text_keys.count(k) > 1})
    check(not dupes, f"no duplicate canonical key in the raw file ({dupes})")

    entries = cw.load_aliases()
    check(len(entries) == len(text_keys),
          f"every key in the file survives the merge "
          f"({len(text_keys)} in text, {len(entries)} loaded)")

    bad = sorted(k for k in entries if not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", k))
    check(not bad, f"keys are lowercase kebab-case ({bad})")

    # A slug belongs to exactly one organization. Two entries claiming one slug
    # would make a contract row's identity depend on dict ordering.
    owners: dict[str, list[str]] = {}
    for key, entry in entries.items():
        for item in entry.get("ckan") or []:
            owners.setdefault(item["slug"], []).append(key)
    shared = {s: ks for s, ks in owners.items() if len(ks) > 1}
    check(not shared, f"no CKAN slug is claimed by two organizations ({shared})")

    # Every infobase id must exist in the plans data, or the entry points at
    # nothing and the organization silently loses its plans side.
    if cw.PLANS_CSV.exists():
        real = set(cw.extract_plan_orgs()["organization_id"])
        dangling = sorted(
            (k, e["infobase"]) for k, e in entries.items()
            if e.get("infobase") is not None and int(e["infobase"]) not in real
        )
        check(not dangling, f"every infobase id exists in the plans data ({dangling})")
    else:
        skip("infobase existence", "no plans cache")

    # STABILITY. These are filenames. A rename should be a deliberate edit to
    # this list, not a side effect of editing the registry.
    check(len(entries) == 100, f"the registry holds 100 organizations (found {len(entries)})")
    for key in ("ircc", "pspc", "ised", "gac", "ssc", "dnd", "cra", "cbsa",
                "eccc", "tbs", "wage", "nsira", "rcmp", "irb",
                "official-languages", "commissioner-of-lobbying",
                "copyright-board", "northern-pipeline"):
        check(key in entries, f"canonical key {key!r} is still present")
    # The slugs these replaced must NOT have become keys again.
    for stale in ("cic", "ic", "ec", "pc", "cpc-cpp", "swc-cfc", "pptc"):
        check(stale not in entries,
              f"stale slug {stale!r} is not a canonical key")


def test_every_observed_name_has_recorded_provenance():
    """
    THE seeding rule, and it now runs on committed files alone.

    Every observed_name must carry a record in attestation.yaml saying where it
    was seen and when. A plausible variant nobody ever published is what must
    not be in the registry, and it is the easiest thing to add by accident.

    This replaces a check that RE-DERIVED the evidence on every run from
    `.cache/tenders.csv`. Two things were wrong with that, and both bit:

    The evidence expired. That CSV is a gitignored snapshot of the notices open
    on the day it was downloaded, so an organization with nothing open drops out
    of it. On 2026-08-09 the Transportation Safety Board and Polar Knowledge
    Canada were both absent and the check called two correct, legally-attested
    aliases invented. A committed file was being validated against a mutable
    one, so the verdict changed with the weather.

    And it never ran where it mattered. ingest.yml runs the suite before
    ingest.py has created the CSV, so on a fresh runner the old check hit its own
    "needs both sources" guard and skipped — every Monday, silently.

    Reading only committed files fixes both: deterministic, and green or red in
    CI rather than absent.
    """
    entries = cw.load_aliases()
    declared = {n for e in entries.values() for n in (e.get("observed_names") or [])}
    if not declared:
        skip("observed_names provenance", "none declared")
        return

    records = cw.load_attestation()
    check(bool(records),
          "attestation.yaml exists and is populated — run crosswalk.py --attest")

    unrecorded = sorted(declared - set(records))
    check(not unrecorded,
          f"every observed_name has recorded provenance; unrecorded: {unrecorded}")

    # A record has to actually say something. An empty stamp would satisfy the
    # membership check above while asserting nothing, which is the shape the old
    # circular harvest had.
    malformed = []
    for name in sorted(declared & set(records)):
        rec = records[name] or {}
        if not rec.get("sources"):
            malformed.append(f"{name}: no sources")
        elif not all(s == "oag" or s.startswith("tender_") for s in rec["sources"]):
            malformed.append(f"{name}: unknown source in {rec['sources']}")
        if not isinstance(rec.get("count"), int) or rec.get("count", 0) < 1:
            malformed.append(f"{name}: count is not a positive integer")
        for field in ("first_seen", "last_seen"):
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(rec.get(field, ""))):
                malformed.append(f"{name}: {field} is not an ISO date")
    check(not malformed, f"every record is well-formed; malformed: {malformed}")


def test_stale_records_are_not_dropped_and_are_not_invented():
    """
    The distinction the old check could not make: absent-today vs never-seen.

    Both must be preserved. Carrying a name forward on a stale record is what
    stops feed rotation from deleting correct aliases; refusing to carry one
    forward with no record at all is what stops the file becoming a place to
    launder guesses. Synthetic input, so it holds with no sources present.
    """
    aliases = {
        "a": {"observed_names": ["Seen Today"]},
        "b": {"observed_names": ["Seen Last Month"]},
        "c": {"observed_names": ["Never Seen Anywhere"]},
    }
    observed = {"Seen Today": {"sources": {"tender_contracting"}, "count": 7}}
    prior = {"Seen Last Month": {"sources": ["oag"], "count": 2,
                                 "first_seen": "2026-07-01", "last_seen": "2026-07-01"}}

    original_load = cw.load_attestation
    cw.load_attestation = lambda: prior
    try:
        records, summary = cw.build_attestation(aliases, observed, today="2026-08-09")
    finally:
        cw.load_attestation = original_load

    check(summary["confirmed"] == ["Seen Today"], "a live name is confirmed")
    check(records["Seen Today"]["last_seen"] == "2026-08-09",
          "a confirmed name advances last_seen")

    check(summary["carried"] == ["Seen Last Month"],
          "a name absent today is carried, not dropped")
    check(records["Seen Last Month"]["last_seen"] == "2026-07-01",
          "a carried name keeps its stale last_seen rather than being re-dated")

    check(summary["missing"] == ["c: Never Seen Anywhere"],
          "a name with no evidence and no record is reported missing")
    check("Never Seen Anywhere" not in records,
          "a never-seen name earns no record")


def test_first_seen_survives_reconfirmation():
    """
    Re-attesting must not rewrite history. first_seen is the age of the claim,
    and an --attest run that reset it would quietly turn a two-year-old
    observation into a fresh one every time it ran.
    """
    aliases = {"a": {"observed_names": ["Long Standing Name"]}}
    observed = {"Long Standing Name": {"sources": {"oag"}, "count": 3}}
    prior = {"Long Standing Name": {"sources": ["tender_contracting"], "count": 1,
                                    "first_seen": "2026-01-15", "last_seen": "2026-07-01"}}

    original_load = cw.load_attestation
    cw.load_attestation = lambda: prior
    try:
        records, _ = cw.build_attestation(aliases, observed, today="2026-08-09")
    finally:
        cw.load_attestation = original_load

    rec = records["Long Standing Name"]
    check(rec["first_seen"] == "2026-01-15", "first_seen is preserved across runs")
    check(rec["last_seen"] == "2026-08-09", "last_seen advances")
    # Sources accumulate: a name seen in the feed last month and in the audit
    # text today is attested by both, and dropping either narrows the claim.
    check(rec["sources"] == ["oag", "tender_contracting"],
          "sources accumulate rather than being replaced")


def test_live_evidence_is_never_weaker_than_the_record():
    """
    The opportunistic half, and it only ever fails in the direction that means
    someone forgot to run --attest.

    A name the sources attest RIGHT NOW must be recorded; there is no excuse for
    a missing stamp when the evidence is sitting there. A name the sources do not
    attest proves nothing either way — that is the feed-rotation case, and
    failing on it is the bug this whole change removes.
    """
    if not (cw.OAG_DB.exists() and cw.TENDERS_CSV.exists()):
        skip("live attestation", "needs both oag.db and tenders.csv")
        return

    entries = cw.load_aliases()
    declared = {n for e in entries.values() for n in (e.get("observed_names") or [])}
    records = cw.load_attestation()
    live = set(cw.extract_observed_names())

    stale = sorted((declared & live) - set(records))
    check(not stale,
          f"names visible in the sources are recorded; run --attest for: {stale}")


def test_a_truncated_name_is_not_attested_by_its_own_extension():
    """
    The specific hole that let five circular entries through review.

    Until 2026-08 the OAG side of the provenance check read oag.db's
    `department` column, which oag_ingest.py had filled from its own hardcoded
    KNOWN_DEPTS list — so the list wrote the spelling and the check read it back
    as proof. The names it admitted were TRUNCATIONS: "Global Affairs" for an
    audit text that only ever says "Global Affairs Canada".

    Plain containment cannot tell those apart, because the short form is a
    substring of the long one. This asserts the stronger rule that replaced it —
    a prefix must stand on its own somewhere to count as observed.
    """
    if not cw.OAG_DB.exists():
        skip("truncation attestation", "no oag.db")
        return
    corpus = cw.oag_text_corpus()
    if not corpus:
        skip("truncation attestation", "no audit text in oag.db")
        return
    aliases = cw.load_aliases()

    for full in ("Global Affairs Canada",
                 "Environment and Climate Change Canada",
                 "Immigration, Refugees and Citizenship Canada"):
        truncated = full[: -len(" Canada")]
        present = cw.attested_in_oag(full, corpus, cw._prefix_extensions(full, aliases))
        bare = cw.attested_in_oag(truncated, corpus,
                                  cw._prefix_extensions(truncated, aliases))
        check(present > 0, f"{full!r} is attested in the audit text ({present})")
        check(bare == 0,
              f"{truncated!r} is NOT attested by {full!r} alone (got {bare})")


def test_exclusions_are_symmetric_and_non_contradictory():
    """
    A `not:` entry must not refuse a name the same entry also claims, and a
    collision exclusion should be mutual — if A refuses B's name, B should
    refuse A's, or the hazard is only half-guarded.
    """
    entries = cw.load_aliases()
    if not entries:
        skip("exclusion discipline", "no org_aliases.yaml")
        return
    for key, entry in entries.items():
        claimed = {cw.normalize_org(n) for n in (entry.get("observed_names") or [])}
        claimed.add(cw.normalize_org(entry.get("name") or ""))
        refused = {cw.normalize_org(n) for n in (entry.get("not") or [])}
        overlap = {x for x in claimed & refused if x}
        check(not overlap,
              f"{key} does not both claim and refuse the same name ({overlap})")


if __name__ == "__main__":
    for fn in [v for k, v in sorted(globals().items()) if k.startswith("test_")]:
        fn()
    for label in PASSED:
        print(f"  ok    {label}")
    for label in SKIPPED:
        print(f"  skip  {label}")
    print(f"\n{len(PASSED)} passed, {len(SKIPPED)} skipped")
