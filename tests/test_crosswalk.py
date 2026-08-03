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


def test_observed_names_are_never_invented():
    """
    THE seeding rule: every observed_name must appear verbatim in the OAG audit
    text or tenders.csv. This is the check that keeps the list evidence rather
    than guesswork — a plausible variant nobody has ever published is exactly
    what must not be in here, and it is the easiest thing to add by accident.
    """
    entries = cw.load_aliases()
    declared = {n for e in entries.values() for n in (e.get("observed_names") or [])}
    if not declared:
        skip("observed_names provenance", "none declared")
        return
    if not (cw.OAG_DB.exists() or cw.TENDERS_CSV.exists()):
        skip("observed_names provenance", "no source data to verify against")
        return

    real = set(cw.extract_observed_names())
    invented = sorted(declared - real)
    check(not invented,
          f"every observed_name appears in the source data; invented: {invented}")


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
