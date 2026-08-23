"""
Build the department name-resolution crosswalk between the contracts dataset
and the Departmental Plans dataset.

WHY THIS EXISTS. The four sources name the same department differently. The
contracts file says "Immigration, Refugees and Citizenship Canada | Immigration,
Refugies et Citoyennete Canada". The plans file says "Department of Citizenship
and Immigration" — the legal name from the enabling statute, which in several
cases has not matched the applied name for over a decade. A naive
`JOIN ... ON organization = org` returns nothing, silently, which is the worst
possible failure: the convergence query runs, returns an empty result, and reads
as "no signal" rather than "broken join".

BUILT FROM THE RAW CACHED CSVs, NOT THE DATABASES. This is the important
constraint. data/contracts.db holds only the organizations that survive the
profile's competency filter — a subset chosen for one company's interests in one
year. Widening the profile later would silently outgrow a crosswalk built from
it, and the newly-admitted departments would land in exactly the silent-empty-join
failure this file exists to prevent. So both universes are read from
.cache/*.csv: every organization that appears in the published data, whether or
not anyone currently cares about it.

A MATCH IS EITHER IDENTICAL OR ASSERTED. There are exactly two ways to pair:

  exact       normalized English names are identical
  alias       a curated entry in vault/crosswalk/org_aliases.yaml pins the pair
              by STABLE KEY (CKAN slug <-> organization_id), never by display
              name, because display names are the thing that keeps changing
  unmatched   everything else — recorded explicitly, on both sides

NO INFERENCE TIER, BY DESIGN. There was briefly a third tier that paired names
when one contained the other. It found 18 pairs and every one was correct,
which is the trap: 11 of them were identical names that a bug in the affix
ordering had made look different, and the remaining 7 leaned on as little as a
single shared token ("environment" finding "environment and climate change").
A rule that accepts those accepts a coincidence just as readily, and a wrong
department merge corrupts the intelligence silently while a missed one leaves a
gap you can see. All 7 are now curated pins with their basis written down.

So: an unmatched organization is a normal, visible outcome, and preferable to a
guessed one. Same reasoning as the vendor normalizer in contracts_ingest.py —
when in doubt, leave them apart and say so.

THE OUTPUT KEEPS BOTH SIDES WHOLE. Every organization from both files appears
in the output exactly once, matched or not. `SELECT ... WHERE confidence='none'`
is the review queue; it is not an error state.

Usage:
    python scripts/crosswalk.py                 # build (reuses the org-extract cache)
    python scripts/crosswalk.py --rescan        # re-read the 640 MB contracts CSV
    python scripts/crosswalk.py --report        # print the mapping and the gaps
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
import unicodedata
from datetime import datetime
from pathlib import Path

import pandas as pd
import yaml

sys.path.insert(0, str(Path(__file__).parent))
from ingest_common import output_path, resolve_columns, staged_db  # noqa: E402

PROJECT_ROOT = Path(__file__).parent.parent
CONTRACTS_CSV = PROJECT_ROOT / ".cache" / "contracts.csv"
PLANS_CSV = PROJECT_ROOT / ".cache" / "plans.csv"
# Distilling 640 MB down to ~99 rows takes minutes; the result is a few KB.
# Cache it so tuning the matcher doesn't mean re-reading the whole file, and
# make refreshing it an explicit --rescan rather than a staleness guess.
ORG_CACHE = PROJECT_ROOT / ".cache" / "contract_orgs.csv"
ALIAS_PATH = PROJECT_ROOT / "vault" / "crosswalk" / "org_aliases.yaml"
DB_PATH = PROJECT_ROOT / "data" / "crosswalk.db"
CHUNK_ROWS = 200_000

# The two OTHER sources that name departments. Neither is authoritative and
# neither is joined here yet — they are read only to collect the surface forms
# a future resolver will have to cope with. Both optional: absent means the
# observed-name pass is skipped, not that the build fails.
#
# oag.db is read for its audit TEXT (title, description), never for a derived
# department column — see oag_text_corpus() for why that distinction is the
# whole provenance story.
OAG_DB = PROJECT_ROOT / "data" / "oag.db"
TENDERS_CSV = PROJECT_ROOT / ".cache" / "tenders.csv"
TENDER_ENTITY_COLUMNS = {
    "contracting": ["contractingEntityName-nomEntitContractante-eng"],
    "end_user": ["endUserEntitiesName-nomEntitesUtilisateurFinal-eng"],
}

CONTRACT_COLUMNS = {
    "owner_org": ["owner_org"],
    "owner_org_title": ["owner_org_title"],
}
# Both are required here, unlike in contracts_ingest.py where owner_org is
# optional. A crosswalk keyed on a display title alone would defeat its own
# purpose, so an old snapshot without the slug column is a hard failure.
CONTRACT_REQUIRED = ["owner_org", "owner_org_title"]

PLAN_COLUMNS = {
    "org_id": ["organization_id"],
    "org": ["organization"],
    # Used only to decide which of an organization's names is the current one.
    # Optional: without it the canonical name is arbitrary but still correct.
    "year": ["fy_ef"],
}
PLAN_REQUIRED = ["org_id", "org"]


# ---------------------------------------------------------------------------
# Name normalization
# ---------------------------------------------------------------------------

# Stripped from either end. "canada" is the big one: the applied name appends it
# ("Transport Canada") where the legal name does not ("Department of Transport").
_TRAILING = [
    "canada", "of canada", "government of canada",
]
_LEADING = [
    "department of the", "department of", "department for the", "department for",
    "the department of", "office of the", "office of", "the",
]

# LONGEST AFFIX FIRST. Order is not cosmetic here. Listed as written above,
# "canada" was tried before "of canada", so "National Research Council of
# Canada" stripped to "national research council OF" — which no longer equals
# the plans side's "national research council". Eleven pairs of IDENTICAL names
# were being missed by the exact tier because of it.
_TRAILING.sort(key=lambda s: -len(s.split()))
_LEADING.sort(key=lambda s: -len(s.split()))


def normalize_org(name: str) -> str:
    """
    Reduce a department name to a comparable core.

    Handles, in order: the bilingual "English | Francais" split, accent folding,
    punctuation, the Department-of / Office-of prefixes, and the trailing
    "Canada" that the applied names carry and the legal names don't.

    Deliberately does NOT reorder or drop interior tokens. "Fisheries and
    Oceans" must not collapse to something that also matches "Oceans Protection",
    so token order and content are preserved and only affixes come off.
    """
    if not name:
        return ""
    # Bilingual display titles are "English | Francais"; keep the English half.
    s = str(name).split("|")[0]
    # Fold accents so "Refugies" and "Réfugiés" compare equal.
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = s.replace("&", " and ")
    s = re.sub(r"[^\w\s]", " ", s)      # punctuation, including the hyphen in
    s = re.sub(r"\s+", " ", s).strip()  # "Crown-Indigenous"

    # Affixes can stack ("The Department of the Environment"), so loop until
    # nothing more comes off rather than stripping a single pass.
    changed = True
    while changed:
        changed = False
        for pre in _LEADING:
            if s.startswith(pre + " "):
                s, changed = s[len(pre) + 1:].strip(), True
        for suf in _TRAILING:
            if s.endswith(" " + suf):
                s, changed = s[: -len(suf) - 1].strip(), True
    return s


# ---------------------------------------------------------------------------
# Observed names — how the OTHER two sources spell these departments
# ---------------------------------------------------------------------------

# A trailing parenthetical is an acronym gloss, not part of the name:
# "Department of National Defence (DND)". Stripped BEFORE normalization,
# because normalize_org turns punctuation into whitespace and would otherwise
# leave "dnd" behind as a real token that matches nothing.
_PAREN_TAIL = re.compile(r"\s*\([^()]*\)\s*$")


def observed_variants(raw: str) -> list[str]:
    """
    Split one raw source string into the department names it actually contains.

    The tender feed packs multiple end users into one field separated by "/"
    ("Department of Transport (TC) / Department of Fisheries and Oceans (DFO)"),
    and the same delimiter also separates the halves of a bilingual name. Both
    are handled the same way — split, clean, and let the attach step decide
    which fragments correspond to a known organization and which don't.

    Parenthetical tails are stripped repeatedly: "Canada Border Services Agency
    - (Administered Activities) (CBSA)" has two of them.
    """
    if not raw:
        return []
    out = []
    for part in str(raw).split("/"):
        s = part.strip()
        prev = None
        while s != prev:
            prev = s
            s = _PAREN_TAIL.sub("", s).strip()
        s = s.rstrip(" -–—,;")
        if s:
            out.append(s)
    return out


def oag_text_corpus() -> list[str]:
    """
    The OAG audit TEXT — title + description per record, normalized for search.

    This is the OAG source of truth for observed names, and it replaces reading
    oag.db's old `department` column. That column was written by oag_ingest.py
    substring-matching its own hardcoded list, so harvesting it as "observed"
    evidence was circular: the list produced the spelling and the harvest read
    it back as proof of the spelling. Five entries reached org_aliases.yaml that
    way. The audit text is what OAG actually published, and cannot be fabricated
    by anything on this side of the pipeline.
    """
    if not OAG_DB.exists():
        return []
    con = sqlite3.connect(OAG_DB)
    try:
        rows = con.execute("SELECT title, description FROM audits").fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        con.close()
    return [_search_form(f"{t or ''} . {d or ''}") for t, d in rows]


def _search_form(text: str) -> str:
    """
    Fold text for containment testing: case, accents, ampersands, punctuation.

    Deliberately NOT normalize_org — that strips "Department of" and a trailing
    "Canada", which on free prose reduces names to bare nouns ("health",
    "environment", "finance") and would match almost anything. Affixes stay on.
    """
    s = unicodedata.normalize("NFKD", str(text))
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = s.replace("&", " and ")
    s = re.sub(r"[^\w\s]", " ", s)
    return f" {re.sub(r'[ ]+', ' ', s).strip()} "


def attested_in_oag(name: str, corpus: list[str], longer: list[str] = ()) -> int:
    """
    How many OAG records name this organization, as a MAXIMAL mention.

    Plain containment is not enough, and the difference is the entire defect
    this replaced. "Global Affairs" is a substring of every "Global Affairs
    Canada" in the corpus, so containment would report it as attested and let a
    truncation masquerade as an observed spelling — which is precisely how the
    five circular entries survived review. An occurrence only counts when it is
    not part of a longer name the registry already knows, so a prefix has to
    stand on its own somewhere to earn the claim.

    `longer` is the registry's other names that this one is a strict prefix of.

    Ministerial titles are excluded on the same principle. "the Minister of
    Environment and Climate Change" is the portfolio holder, not the
    department, and it is the one place in this corpus where the bare form of a
    removed name genuinely occurs — so counting it would have re-attested a
    truncation on the strength of a construct that never names an organization.
    """
    needle = _search_form(name)
    if needle == " ":
        return 0
    extended = [_search_form(l) for l in longer]
    extended += [_search_form(f"Minister{s} of {name}") for s in ("", "s")]
    hits = 0
    for rec in corpus:
        if needle not in rec:
            continue
        # Every position the bare name sits at must also be covered by one of
        # its extensions for the mention to be non-maximal.
        bare = rec.count(needle[1:-1])
        covered = sum(rec.count(e[1:-1]) for e in extended)
        if bare > covered:
            hits += 1
    return hits


def _prefix_extensions(name: str, aliases: dict[str, dict]) -> list[str]:
    """Registry names that `name` is a strict leading sub-phrase of."""
    n = _search_form(name)
    out = []
    for entry in aliases.values():
        for other in [entry.get("name")] + list(entry.get("observed_names") or []):
            if not other:
                continue
            o = _search_form(other)
            if o != n and o.startswith(n[:-1] + " "):
                out.append(other)
    return out


def extract_observed_names() -> dict[str, dict]:
    """
    Every department-ish string the tender source actually contains, plus the
    declared OAG names that the audit text independently attests.

    SEEDED FROM REAL DATA ONLY. Nothing here is invented or extrapolated. The
    two sides are collected differently because they are shaped differently: the
    tender feed has an entity COLUMN whose values can be enumerated, while OAG
    publishes prose, where the only honest question is whether a given string
    appears in it. So OAG names are CONFIRMED here, not enumerated — discovery
    of ones the registry is missing is `discover_oag_org_names()`, which is a
    proposal for a human rather than an input to matching.
    """
    observed: dict[str, dict] = {}

    def record(raw: str, source: str, n: int = 1) -> None:
        for name in observed_variants(raw):
            entry = observed.setdefault(name, {"sources": set(), "count": 0})
            entry["sources"].add(source)
            entry["count"] += n

    corpus = oag_text_corpus()
    if corpus:
        aliases = load_aliases()
        for entry in aliases.values():
            for name in [entry.get("name")] + list(entry.get("observed_names") or []):
                if not name:
                    continue
                hits = attested_in_oag(name, corpus, _prefix_extensions(name, aliases))
                if hits:
                    record(name, "oag", hits)
    else:
        print(f"  (no {OAG_DB.name} audit text; skipping OAG names)")

    if TENDERS_CSV.exists():
        df = pd.read_csv(TENDERS_CSV, low_memory=False)
        cols = resolve_columns(list(df.columns), TENDER_ENTITY_COLUMNS,
                               [], "scripts/crosswalk.py")
        for key, col in cols.items():
            if not col:
                continue
            for value, n in df[col].dropna().astype(str).value_counts().items():
                record(value, f"tender_{key}", int(n))
    else:
        print(f"  (no {TENDERS_CSV.name}; skipping tender names)")

    return observed


# ---------------------------------------------------------------------------
# Attestation — provenance that survives the sources it was taken from
# ---------------------------------------------------------------------------
# org_aliases.yaml is committed and hand-curated. Its observed_names were
# checked against real data, and the check that enforced that re-derived the
# evidence on every run from `.cache/tenders.csv` — a gitignored snapshot of
# CURRENTLY OPEN notices, re-downloaded by every ingest.
#
# So the evidence expired. A small agency with no open solicitation this week
# simply is not in the feed: on 2026-08-09 the Transportation Safety Board and
# Polar Knowledge Canada were both absent, and the check reported two correct,
# legally-attested aliases as invented. It also never ran in CI, because the
# workflow runs the suite before ingest.py has created the CSV, and the check
# skipped itself when the file was missing.
#
# The fix is to stop re-deriving and start RECORDING. Attestation is written
# once, when the evidence is seen, and committed alongside the registry. A name
# absent from today's feed is then inconclusive — its record stands and its
# last_seen simply does not advance — which is the honest reading, because the
# feed never claimed to be a census of federal organizations.
#
# Kept in its own file rather than as a block inside org_aliases.yaml for the
# same reason vault/agencies/ and vault/intel/agencies/ are separate: one is
# hand-written and full of curated prose, the other is machine-owned and
# rewritten wholesale. Rewriting the curated file to stamp a date would mean a
# YAML round-trip through its comments, and those comments are the reasoning.

ATTESTATION_PATH = PROJECT_ROOT / "vault" / "crosswalk" / "attestation.yaml"

ATTESTATION_HEADER = """\
# Provenance for the observed_names in org_aliases.yaml.
#
# GENERATED — do not hand-edit. Rewritten wholesale by:
#     python scripts/crosswalk.py --attest
#
# Each record says where a name was seen and when. It is committed so the claim
# outlives the source it came from: `.cache/tenders.csv` holds only the notices
# open on the day it was downloaded, so an organization with nothing open right
# now vanishes from it. That is not evidence the name was invented, and this
# file exists so nothing mistakes it for evidence.
#
# `last_seen` not advancing on a run means the name was not observed that day.
# Only a name that has NEVER been observed, and so has no record here at all, is
# an error — see tests/test_crosswalk.py.
"""


# The bootstrap. These two were attested before this file existed, and the
# evidence has since expired, so there is no run of --attest that can rediscover
# them — the only honest options are to record the verification that did happen
# or to delete two correct aliases.
#
# What happened: commit 60234be (2026-08-03) rewrote the provenance check to
# compare every declared observed_name against the UNION of oag.db and
# tenders.csv, and removed the five it could not substantiate. Both names below
# were in org_aliases.yaml at that commit and both survived it. That is a real
# verification against real data, and the commit is the citation.
#
# Neither is guesswork on the merits either. "Canadian Transportation Accident
# Investigation and Safety Board" is the TSB's legal name in the Act that
# created it; "Polar Knowledge Canada" is the applied name of the Canadian High
# Arctic Research Station. Both are small enough to have no open solicitation in
# a given week, which is exactly how they fell out of a feed of open notices.
#
# Superseded the moment either name is observed live: build_attestation layers
# the file over this, and live evidence over both.
PRIOR_ATTESTATION: dict[str, dict] = {
    "Canadian Transportation Accident Investigation and Safety Board": {
        "sources": ["tender_contracting"],
        "count": 1,
        "first_seen": "2026-08-03",
        "last_seen": "2026-08-03",
        "note": "Verified by the union check in commit 60234be; absent from the "
                "feed since. Legal name of the Transportation Safety Board.",
    },
    "Polar Knowledge Canada": {
        "sources": ["tender_contracting"],
        "count": 1,
        "first_seen": "2026-08-03",
        "last_seen": "2026-08-03",
        "note": "Verified by the union check in commit 60234be; absent from the "
                "feed since. Applied name of the Canadian High Arctic Research "
                "Station.",
    },
}


def load_attestation() -> dict[str, dict]:
    """Recorded provenance, name -> record. Empty when the file is absent."""
    if not ATTESTATION_PATH.exists():
        return {}
    doc = yaml.safe_load(ATTESTATION_PATH.read_text(encoding="utf-8")) or {}
    return doc.get("names") or {}


def build_attestation(aliases: dict[str, dict],
                      observed: dict[str, dict],
                      today: str | None = None) -> tuple[dict, dict]:
    """
    Merge today's live evidence into the recorded provenance.

    Returns `(records, summary)`. ADDITIVE BY DESIGN: a name observed today has
    its record refreshed and its source set widened; a name not observed today
    keeps whatever was recorded before, untouched. Nothing is ever dropped here,
    because "absent from this download" and "never existed" are different
    claims and only the tests get to act on the difference.
    """
    today = today or datetime.now().strftime("%Y-%m-%d")
    # The recorded file wins over the bootstrap, and live evidence wins over
    # both — so the seed decays out of relevance instead of pinning anything.
    previous = {**PRIOR_ATTESTATION, **load_attestation()}
    records: dict[str, dict] = {}
    confirmed, carried, missing = [], [], []

    for key, entry in sorted(aliases.items()):
        for name in entry.get("observed_names") or []:
            live = observed.get(name)
            prior = previous.get(name) or {}
            if live:
                sources = sorted(set(live["sources"]) | set(prior.get("sources") or []))
                records[name] = {
                    "key": key,
                    "sources": sources,
                    "count": int(live["count"]),
                    "first_seen": prior.get("first_seen", today),
                    "last_seen": today,
                }
                confirmed.append(name)
            elif prior:
                # Carried forward verbatim, including its stale last_seen. That
                # date is the useful part: it says how long it has been since
                # anyone saw this string, which is a fact worth keeping visible.
                records[name] = {**prior, "key": key}
                carried.append(name)
            else:
                missing.append(f"{key}: {name}")

    return records, {"confirmed": confirmed, "carried": carried, "missing": missing}


def write_attestation(records: dict[str, dict]) -> None:
    """Rewrite the attestation file. Machine-owned, so a full rewrite is safe."""
    ATTESTATION_PATH.parent.mkdir(parents=True, exist_ok=True)
    body = yaml.safe_dump(
        {"names": {k: records[k] for k in sorted(records)}},
        sort_keys=False, allow_unicode=True, default_flow_style=False, width=100,
    )
    # newline="\n" for the reason .gitattributes gives: without it Python writes
    # CRLF on Windows, git stores LF, and every --attest run leaves the file
    # showing as modified with an empty diff. A generator that dirties its own
    # output on a no-op run teaches you to ignore its output.
    ATTESTATION_PATH.write_text(
        ATTESTATION_HEADER + "\n" + body, encoding="utf-8", newline="\n")


def cmd_attest() -> int:
    """Re-derive provenance from live sources and record it. Returns an exit code."""
    aliases = load_aliases()
    have_oag, have_csv = OAG_DB.exists(), TENDERS_CSV.exists()
    print(f"Sources: {OAG_DB.name} {'present' if have_oag else 'ABSENT'}, "
          f"{TENDERS_CSV.name} {'present' if have_csv else 'ABSENT'}")
    if not (have_oag or have_csv):
        sys.stderr.write(
            "Neither source is available, so nothing can be attested.\n"
            "Build them with scripts/oag_ingest.py and scripts/ingest.py.\n"
        )
        return 2

    observed = extract_observed_names()
    records, summary = build_attestation(aliases, observed)

    print(f"  confirmed today:  {len(summary['confirmed'])}")
    print(f"  carried forward:  {len(summary['carried'])}")
    for name in summary["carried"]:
        prior = records[name]
        print(f"      {name!r} — not in today's sources; last seen "
              f"{prior.get('last_seen', 'unknown')}")
    if summary["missing"]:
        # The only real error: no live evidence AND nothing ever recorded.
        print(f"  NEVER ATTESTED:   {len(summary['missing'])}")
        for item in summary["missing"]:
            print(f"      {item}")
        sys.stderr.write(
            "\nThese observed_names have no evidence in any source and no prior\n"
            "record. Either they were never seen, or they were added by hand.\n"
            "Remove them from org_aliases.yaml, or attest them from a source\n"
            "that contains them.\n"
        )
        write_attestation(records)
        return 1

    write_attestation(records)
    print(f"\nWrote {len(records)} records to "
          f"{ATTESTATION_PATH.relative_to(PROJECT_ROOT)}")
    return 0


def attach_observed(rows: list[dict], observed: dict[str, dict],
                    aliases: dict) -> tuple[list[dict], list[dict]]:
    """
    Bind each observed string to the one organization whose known names it
    matches, by NORMALIZED EQUALITY and nothing weaker.

    Same rule as the crosswalk proper: identical or nothing. A string that
    normalizes to something no organization answers to is not force-fitted to
    the nearest candidate — it goes to the review queue, which is the actual
    deliverable of this pass. Ambiguous strings (matching two organizations)
    also go there; those are the collisions the `not:` exclusions exist for.

    Returns (unattached, ambiguous), and mutates rows to carry observed_names.
    """
    # A row's registry entry, reached by slug or — for a plans-only
    # organization — by canonical key.
    by_key = {k: e for k, e in aliases.items()}

    def entry_for(r: dict) -> dict:
        return by_key.get(r.get("canonical_key") or "") or {}

    # Every name each crosswalk row answers to, normalized.
    keys: dict[str, list[int]] = {}
    for i, r in enumerate(rows):
        entry = entry_for(r)
        names = [r.get("contract_org_title"), r.get("contract_org_en"),
                 r.get("plan_organization"), entry.get("name")]
        if r.get("plan_organization_former"):
            names += [x.strip() for x in r["plan_organization_former"].split(";")]
        # Curated observed_names are authoritative match keys too.
        names += entry.get("observed_names") or []
        for n in names:
            if not n:
                continue
            norm = normalize_org(n)
            if norm and i not in keys.setdefault(norm, []):
                keys[norm].append(i)
        r["observed_names"] = []

    # Strings an entry explicitly refuses, so a near-neighbour can never land
    # on it. Applies to every slug the entry owns — pptc inherits IRCC's
    # exclusions because it inherits IRCC's plan name.
    excluded: dict[int, set[str]] = {}
    for i, r in enumerate(rows):
        excluded[i] = {normalize_org(x) for x in (entry_for(r).get("not") or [])}

    unattached, ambiguous = [], []
    for name, meta in sorted(observed.items()):
        norm = normalize_org(name)
        cands = [i for i in keys.get(norm, []) if norm not in excluded.get(i, set())]
        # A plan org's name is shared by every contract org pinned to it, so an
        # absorbed or successor pin answers to it too — "Department of
        # Citizenship and Immigration" reached both IRCC and Passport Canada.
        # The `same` row is the canonical holder of that name; the others hold
        # it only by inheritance. This is a tie-break between rows already
        # proven to match, not an inference about which one is nearest.
        if len(cands) > 1:
            primary = [i for i in cands if rows[i].get("relation") == "same"]
            if len(primary) == 1:
                cands = primary
        info = {"name": name, "norm": norm,
                "sources": sorted(meta["sources"]), "count": meta["count"]}
        if len(cands) == 1:
            rows[cands[0]]["observed_names"].append(name)
        elif len(cands) > 1:
            info["candidates"] = [rows[i].get("contract_org_en")
                                  or rows[i].get("plan_organization") for i in cands]
            ambiguous.append(info)
        else:
            unattached.append(info)

    for r in rows:
        r["observed_names"] = "; ".join(sorted(set(r["observed_names"]))) or None
    return unattached, ambiguous


# Organization-shaped proper-noun runs: capitalised words, optionally joined by
# lowercase connectives, ending on a word that names a kind of body. Anchoring
# on the tail is what keeps it from sweeping up ordinary Title Case prose —
# "Federal Sustainable Development Strategy" has no body word and is not
# proposed, while "Canadian Coast Guard" is.
_ORG_TAIL = (
    r"Canada|Agency|Board|Commission|Council|Service|Services|Secretariat|"
    r"Establishment|Corporation|Department|Office|Centre|Forces|Guard|Police|"
    r"Authority|Tribunal|Bureau|Institute"
)
_ORG_RUN = re.compile(
    r"\b((?:[A-Z][\w‑-]+[ ,]{0,2}(?:of |the |and |for )?){1,7}(?:" + _ORG_TAIL + r"))\b"
)

# Constructs that are organization-SHAPED but never an audited department: the
# parliamentary machinery an OAG record is addressed to, the auditor itself, and
# the collective nouns for government at large. "Committee" is absent from
# _ORG_TAIL for the same reason — the 231 "Standing Committee on X" mentions are
# who the report was TABLED WITH, and attributing an audit to them would repeat,
# in a new place, the mistake this whole change exists to remove.
_NOT_AN_ORG = re.compile(
    r"\bcommittee\b|\bparliament\b|\bsenate\b|\bhouse of commons\b|\bhoc\b|"
    r"\bauditor general\b|\bgovernment of canada\b|\bpublic accounts of canada\b|"
    r"^the office$|\bcrown corporation\b|\bprofessional services\b",
    re.I,
)
# Audit titles are verb phrases — "Supplying the Canadian Armed Forces",
# "Protecting Canada's Food System" — so a run that opens on a participle is a
# title fragment that happens to end on a body word, not an organization.
_TITLE_FRAGMENT = re.compile(r"^(?:The\s+)?\w+ing\b", re.I)


def discover_oag_org_names(aliases: dict[str, dict]) -> list[dict]:
    """
    Organization-shaped strings in the OAG audit text that resolve to NOTHING.

    The replacement for harvesting oag.db's `department` column. This is a
    PROPOSAL PASS, not an input to matching: it reports what a curator might
    want to add, and nothing it finds affects a join until a human writes it
    into org_aliases.yaml. That asymmetry is the point — the old path let the
    ingest script's own output become evidence for the registry that the ingest
    script then resolved against.

    Only non-resolving strings are proposed, so it cannot re-derive a name the
    registry already asserts, which is what made the old loop circular.
    """
    if not OAG_DB.exists():
        return []
    con = sqlite3.connect(OAG_DB)
    try:
        records = con.execute("SELECT title, description FROM audits").fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        con.close()

    known = {normalize_org(n)
             for e in aliases.values()
             for n in [e.get("name")] + list(e.get("observed_names") or []) if n}
    found: dict[str, int] = {}
    for title, desc in records:
        for m in _ORG_RUN.finditer(f"{title or ''} . {desc or ''}"):
            name = re.sub(r"\s+", " ", m.group(1)).strip(" ,")
            if len(name.split()) < 2 or normalize_org(name) in known:
                continue
            if _NOT_AN_ORG.search(name) or _TITLE_FRAGMENT.match(name):
                continue
            found[name] = found.get(name, 0) + 1
    return [{"name": k, "count": v} for k, v in
            sorted(found.items(), key=lambda kv: (-kv[1], kv[0]))]


# ---------------------------------------------------------------------------
# Reading the two universes, from the raw caches
# ---------------------------------------------------------------------------

def extract_contract_orgs(rescan: bool = False) -> pd.DataFrame:
    """
    Every distinct owner_org in the raw contracts CSV, with its row count.

    Streamed in chunks over two columns only — the file is ~640 MB and there is
    no reason to materialize the rest of it to count departments.
    """
    if ORG_CACHE.exists() and not rescan:
        print(f"Using cached org extract ({ORG_CACHE.name}); --rescan to rebuild")
        return pd.read_csv(ORG_CACHE, keep_default_na=False)

    if not CONTRACTS_CSV.exists():
        sys.stderr.write(
            f"Missing {CONTRACTS_CSV}.\n"
            "Run: python scripts/contracts_ingest.py   (it populates the cache)\n"
        )
        sys.exit(2)

    print(f"Scanning {CONTRACTS_CSV.name} "
          f"({CONTRACTS_CSV.stat().st_size / 1e6:.0f} MB) for organizations...")

    header = pd.read_csv(CONTRACTS_CSV, nrows=0, encoding="utf-8")
    cols = resolve_columns(list(header.columns), CONTRACT_COLUMNS,
                           CONTRACT_REQUIRED, "scripts/crosswalk.py")

    counts: dict[str, int] = {}
    titles: dict[str, str] = {}
    seen = 0
    reader = pd.read_csv(
        CONTRACTS_CSV, usecols=[cols["owner_org"], cols["owner_org_title"]],
        chunksize=CHUNK_ROWS, low_memory=False, on_bad_lines="skip",
        encoding="utf-8", keep_default_na=False,
    )
    for chunk in reader:
        seen += len(chunk)
        for slug, title in zip(chunk[cols["owner_org"]],
                               chunk[cols["owner_org_title"]]):
            slug = str(slug).strip()
            if not slug:
                continue
            counts[slug] = counts.get(slug, 0) + 1
            # Keep the first non-empty title seen; titles drift over the years
            # (rebrands), and the slug is the identity that doesn't.
            if slug not in titles and str(title).strip():
                titles[slug] = str(title).strip()
        if seen % 1_000_000 < CHUNK_ROWS:
            print(f"  ...{seen:,} rows, {len(counts)} organizations so far")

    df = pd.DataFrame(
        [{"owner_org": s, "owner_org_title": titles.get(s, ""), "contract_rows": n}
         for s, n in sorted(counts.items(), key=lambda x: -x[1])]
    )
    ORG_CACHE.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(ORG_CACHE, index=False, encoding="utf-8")
    print(f"Scanned {seen:,} rows -> {len(df)} organizations, cached to {ORG_CACHE.name}")
    return df


def extract_plan_orgs() -> pd.DataFrame:
    """
    One row per organization_id in the raw plans CSV, carrying every name that
    id has ever been published under.

    A department can be renamed part-way through the series: id 237 appears as
    "Office of Infrastructure of Canada" in the early years and "Department of
    Housing, Infrastructure and Communities" in the later ones. Grouping on
    (id, name) makes that look like two organizations, and whichever name lost
    the tie then vanished from the crosswalk without a word. So the id is the
    identity, the name from the latest fiscal year is canonical, and the older
    names are kept — both to report and to match against, since a contract
    title may well still be using one of them.
    """
    if not PLANS_CSV.exists():
        sys.stderr.write(
            f"Missing {PLANS_CSV}.\n"
            "Run: python scripts/plans_ingest.py   (it populates the cache)\n"
        )
        sys.exit(2)

    df = pd.read_csv(PLANS_CSV, low_memory=False)
    cols = resolve_columns(list(df.columns), PLAN_COLUMNS, PLAN_REQUIRED,
                           "scripts/crosswalk.py")

    df = df[[c for c in (cols["org_id"], cols["org"], cols["year"]) if c]].copy()
    df = df.rename(columns={cols["org_id"]: "organization_id", cols["org"]: "organization"})
    df["organization_id"] = df["organization_id"].astype(int)
    # "2024-2025" -> 2024, for ordering names by recency.
    if cols["year"]:
        df["_year"] = (df[cols["year"]].astype(str).str.split("-").str[0]
                       .pipe(pd.to_numeric, errors="coerce").fillna(0).astype(int))
    else:
        df["_year"] = 0

    records = []
    for oid, grp in df.groupby("organization_id"):
        # Latest year first; the name in force most recently is the canonical one.
        ordered = (grp.groupby("organization")["_year"].max()
                   .sort_values(ascending=False).index.tolist())
        records.append({
            "organization_id": int(oid),
            "organization": ordered[0],
            "former_names": ordered[1:],
            "plan_rows": int(len(grp)),
        })

    out = pd.DataFrame(records).sort_values("plan_rows", ascending=False).reset_index(drop=True)
    renamed = sum(1 for r in records if r["former_names"])
    print(f"Plans: {len(out)} organizations over {len(df):,} program-year rows"
          + (f" ({renamed} renamed mid-series)" if renamed else ""))
    return out


def load_aliases() -> dict[str, dict]:
    """
    The organization registry, canonical key -> entry, both blocks merged.

    `curated:` and `mechanical:` are separate in the file so the entries
    carrying human judgment aren't buried among the stubs, but they are one key
    space. A key appearing in both is a curation error and exits rather than
    letting one block silently win.
    """
    if not ALIAS_PATH.exists():
        return {}
    doc = yaml.safe_load(ALIAS_PATH.read_text(encoding="utf-8")) or {}
    curated = doc.get("curated") or {}
    mechanical = doc.get("mechanical") or {}
    clash = sorted(set(curated) & set(mechanical))
    if clash:
        sys.stderr.write(
            f"Duplicate canonical keys across blocks in {ALIAS_PATH.name}: {clash}\n"
            "A key must name exactly one organization.\n"
        )
        sys.exit(2)
    return {**curated, **mechanical}


def slug_index(entries: dict[str, dict]) -> dict[str, dict]:
    """
    CKAN slug -> the entry that owns it, plus this slug's own relation and note.

    The rest of the module still works slug-first, because that is what the
    contracts data gives it. This index is the bridge from that world to the
    canonical keys, and it is where the per-slug relation gets resolved — the
    relation is read off the slug, never defaulted, so a slug added to a `ckan`
    list without one is a loud KeyError rather than a silent `same`.
    """
    out: dict[str, dict] = {}
    for key, entry in entries.items():
        for item in entry.get("ckan") or []:
            slug = item["slug"]
            if slug in out:
                sys.stderr.write(
                    f"CKAN slug {slug!r} claimed by both {out[slug]['key']!r} and "
                    f"{key!r} in {ALIAS_PATH.name}. A slug belongs to one "
                    "organization.\n"
                )
                sys.exit(2)
            out[slug] = {
                "key": key,
                "entry": entry,
                "relation": item["relation"],
                "slug_note": item.get("note"),
            }
    return out


def plans_only_index(entries: dict[str, dict]) -> dict[int, str]:
    """
    infobase id -> canonical key, for entries that own no CKAN slug.

    Without this an organization that files plans but publishes no contracts
    would carry a key in the registry and still come out of the build nameless,
    which is the exact gap the rekey exists to close.
    """
    return {
        int(e["infobase"]): key
        for key, e in entries.items()
        if not (e.get("ckan") or []) and e.get("infobase") is not None
    }


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

def build_crosswalk(contracts: pd.DataFrame, plans: pd.DataFrame,
                    aliases: dict) -> list[dict]:
    """
    Pair the two universes, keeping every organization from both sides.

    THE REGISTRY RESOLVES; THE NAME EXPLAINS. org_aliases.yaml now carries an
    infobase id for every organization, so the pairing itself comes from the
    registry rather than from string comparison. What the names do still
    decides how the pair is REPORTED:

      exact   the two datasets independently agree on the name. The registry
              only restates what a comparison would have found anyway.
      alias   the names differ and a human bridged them. This is the set worth
              auditing — "Global Affairs Canada" against "Department of Foreign
              Affairs, Trade and Development" is nobody's string match.

    That distinction is the honest one: it says which joins rest on evidence
    from the data and which rest on somebody's assertion.

    NOT MANDATORY. A slug absent from the registry still falls through to plain
    exact-name matching, so a department appearing in the contracts file before
    anyone adds an entry is matched if its name allows it, and reported
    unmatched if not. The registry is authoritative, not required.

    MANY-TO-ONE IS REAL, AND ONLY A HUMAN MAY ASSERT IT. Several contract orgs
    legitimately point at one plan org: Status of Women Canada was renamed to
    Women and Gender Equality, Passport Canada was folded into IRCC, SIRC was
    absorbed by NSIRA. Their old contracts are still in the file under the old
    slug and still belong to the successor's plans. A slug whose relation is
    anything other than `same` does not reserve its plan org, which leaves the
    successor's own name free to match normally. The fallback tier can never do
    this: inferring a corporate succession from a string is exactly the silent
    wrong merge this file avoids.
    """
    plans = plans.copy()
    # Every name the org has held, normalized — a contract title may still be
    # using a name the plans data has since retired.
    plans["norm_names"] = [
        [normalize_org(n) for n in [r.organization, *r.former_names]]
        for r in plans.itertuples()
    ]
    by_id = {int(r.organization_id): r for r in plans.itertuples()}

    rows: list[dict] = []
    claimed: set[int] = set()     # exclusive: blocks the exact tier
    referenced: set[int] = set()  # any mention at all, incl. non-exclusive aliases

    slugs = slug_index(aliases)

    def emit(c, plan_row, method: str, confidence: str,
             relation: str | None = None, reserve: bool = True) -> None:
        rows.append({
            "canonical_key": (slugs.get(c.owner_org) or {}).get("key"),
            "contract_owner_org": c.owner_org,
            "contract_org_title": c.owner_org_title,
            "contract_org_en": str(c.owner_org_title).split("|")[0].strip(),
            "contract_org_norm": normalize_org(c.owner_org_title),
            "plan_organization_id": int(plan_row.organization_id) if plan_row is not None else None,
            "plan_organization": plan_row.organization if plan_row is not None else None,
            "plan_organization_former": ("; ".join(plan_row.former_names) or None)
                                        if plan_row is not None else None,
            "match_method": method,
            "relation": relation,
            "confidence": confidence,
            "contract_rows": int(c.contract_rows),
            "plan_rows": int(plan_row.plan_rows) if plan_row is not None else None,
        })
        if plan_row is not None:
            referenced.add(int(plan_row.organization_id))
            if reserve:
                claimed.add(int(plan_row.organization_id))

    pending = list(contracts.itertuples())

    # Tier 1 of 2: the registry. Resolves by infobase id, reports by name.
    still = []
    for c in pending:
        ref = slugs.get(c.owner_org)
        pid = (ref or {}).get("entry", {}).get("infobase") if ref else None
        if pid is None:
            still.append(c)
            continue
        if int(pid) not in by_id:
            # An entry pointing at an id the data no longer has is a curation
            # bug, not a match. Say so loudly instead of falling through
            # silently, then let the name tier try.
            print(f"  WARNING: {ref['key']} (slug {c.owner_org}) declares "
                  f"infobase={pid}, which is not in the plans data")
            still.append(c)
            continue
        plan_row = by_id[int(pid)]
        # Did the two datasets already agree, or did a human have to bridge it?
        agrees = normalize_org(c.owner_org_title) in set(plan_row.norm_names)
        # `same` means one entity under two names, so it reserves. A
        # predecessor, successor or absorbed entity shares its counterpart's
        # plans without owning them.
        relation = ref["relation"]
        emit(c, plan_row, "exact" if agrees else "alias", "high",
             relation=relation, reserve=(relation == "same"))
    pending = still

    # Tier 2 of 2: identical normalized names, for slugs the registry does not
    # cover. Keeps a brand-new department joinable before anyone writes it down.
    norm_index: dict[str, list] = {}
    for r in plans.itertuples():
        for n in set(r.norm_names):
            norm_index.setdefault(n, []).append(r)
    still = []
    for c in pending:
        cands = [r for r in norm_index.get(normalize_org(c.owner_org_title), [])
                 if int(r.organization_id) not in claimed]
        if len(cands) == 1:
            emit(c, cands[0], "exact", "high", relation="same")
        else:
            still.append(c)
    pending = still

    # Anything still pending is unmatched. There is no third tier: a name that
    # is neither identical nor pinned goes to the review queue, not to a guess.
    for c in pending:
        emit(c, None, "unmatched_contract", "none")

    # ...and on the plans side. A department can publish plans without ever
    # appearing in this contracts file, and it still belongs in the crosswalk.
    # Tested against `referenced`, not `claimed`: an org reached only by a
    # predecessor alias has a counterpart and must not be reported as a gap.
    # These rows pick up their key from the registry's slug-less entries, so an
    # organization the contracts data has never heard of is still named.
    plans_only = plans_only_index(aliases)
    for r in plans.itertuples():
        if int(r.organization_id) in referenced:
            continue
        rows.append({
            "canonical_key": plans_only.get(int(r.organization_id)),
            "contract_owner_org": None, "contract_org_title": None,
            "contract_org_en": None, "contract_org_norm": None,
            "plan_organization_id": int(r.organization_id),
            "plan_organization": r.organization,
            "plan_organization_former": "; ".join(r.former_names) or None,
            "match_method": "unmatched_plan", "relation": None,
            "confidence": "none",
            "contract_rows": None, "plan_rows": int(r.plan_rows),
        })
    return rows


def build_db(rows: list[dict], db_path: Path = DB_PATH) -> None:
    """Staged write, same contract as the other ingests: the existing database
    survives untouched if anything here fails."""
    with staged_db(db_path) as con:
        con.execute("""
            CREATE TABLE org_crosswalk (
                -- The organization's identity: short, chosen, stable across
                -- rebrands. What a department dossier should be named.
                canonical_key TEXT,
                contract_owner_org TEXT,
                contract_org_title TEXT,
                contract_org_en TEXT,
                contract_org_norm TEXT,
                plan_organization_id INTEGER,
                plan_organization TEXT,
                -- Names this id was published under in earlier years, if any.
                plan_organization_former TEXT,
                -- Spellings seen in oag.db / tenders.csv that resolve here.
                -- Evidence of real-world usage, not a generated variant list.
                observed_names TEXT,
                match_method TEXT,
                -- same | predecessor | absorbed. Joining Passport Canada's
                -- contracts to IRCC's plans is correct, but the caller should
                -- know it's an absorption and not a rename.
                relation TEXT,
                confidence TEXT,
                contract_rows INTEGER,
                plan_rows INTEGER
            )
        """)
        con.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        con.executemany(
            "INSERT INTO org_crosswalk VALUES "
            "(:canonical_key,:contract_owner_org,:contract_org_title,:contract_org_en,"
            ":contract_org_norm,:plan_organization_id,:plan_organization,"
            ":plan_organization_former,:observed_names,:match_method,:relation,"
            ":confidence,:contract_rows,:plan_rows)", rows)
        con.execute("CREATE INDEX idx_key ON org_crosswalk(canonical_key)")
        con.execute("CREATE INDEX idx_owner_org ON org_crosswalk(contract_owner_org)")
        con.execute("CREATE INDEX idx_plan_id ON org_crosswalk(plan_organization_id)")
        con.execute("CREATE INDEX idx_conf ON org_crosswalk(confidence)")

        matched = sum(1 for r in rows if r["confidence"] != "none")
        for k, v in [
            ("ingest_date", datetime.now().strftime("%Y-%m-%d")),
            ("source_contracts", str(CONTRACTS_CSV)),
            ("source_plans", str(PLANS_CSV)),
            ("built_from", "raw cached CSVs, not the filtered databases"),
            ("contract_orgs", str(sum(1 for r in rows if r["contract_owner_org"]))),
            # DISTINCT, not a row count: four plan orgs are legitimately
            # referenced by two contract orgs each (predecessor/absorbed pins),
            # so counting rows here overstated the universe by exactly those.
            ("plan_orgs", str(len({r["plan_organization_id"] for r in rows
                                   if r["plan_organization_id"] is not None}))),
            ("matched_pairs", str(matched)),
            ("licence", "Open Government Licence - Canada"),
        ]:
            con.execute("INSERT INTO meta VALUES (?, ?)", (k, v))


def report(rows: list[dict]) -> None:
    """Print the mapping and, more usefully, everything that didn't map."""
    by_method: dict[str, list] = {}
    for r in rows:
        by_method.setdefault(r["match_method"], []).append(r)

    print("\n=== MATCHED ===")
    for method in ("alias", "exact"):
        got = by_method.get(method, [])
        if not got:
            continue
        print(f"\n-- {method} ({len(got)}) --")
        for r in sorted(got, key=lambda x: -(x["contract_rows"] or 0)):
            tag = "" if r["relation"] == "same" else f"  ({r['relation']})"
            print(f"  {r['contract_org_en'][:52]:<52} -> [{r['plan_organization_id']:>3}] "
                  f"{r['plan_organization'][:48]}{tag}")

    for method, label in (("unmatched_contract", "CONTRACTS side, no plans counterpart"),
                          ("unmatched_plan", "PLANS side, no contracts counterpart")):
        got = by_method.get(method, [])
        print(f"\n=== UNMATCHED: {label} ({len(got)}) ===")
        for r in sorted(got, key=lambda x: -((x["contract_rows"] or x["plan_rows"]) or 0)):
            name = r["contract_org_en"] or r["plan_organization"]
            key = r["contract_owner_org"] or f"id={r['plan_organization_id']}"
            n = r["contract_rows"] or r["plan_rows"] or 0
            print(f"  {name[:60]:<60} ({key}, {n:,} rows)")


def find_prefix_collisions(rows: list[dict]) -> list[dict]:
    """
    Organizations whose names share a leading token run.

    This is where a resolver goes wrong quietly. "Immigration, Refugees and
    Citizenship" and "Immigration and Refugee Board" both begin with
    "immigration", and a truncated or partial string starting that way could
    land on either. The pairs found here are what the `not:` exclusions in
    org_aliases.yaml are written against — not hypothetical collisions, the
    ones actually present in these two universes.

    Compares every name each organization answers to, including its observed
    forms, since those are what a resolver will actually be handed.
    """
    named: list[tuple[int, str, list[str]]] = []
    for i, r in enumerate(rows):
        label = r.get("contract_org_en") or r.get("plan_organization")
        names = {r.get("contract_org_en"), r.get("plan_organization")}
        if r.get("plan_organization_former"):
            names |= {x.strip() for x in r["plan_organization_former"].split(";")}
        if r.get("observed_names"):
            names |= {x.strip() for x in r["observed_names"].split(";")}
        for n in filter(None, names):
            toks = normalize_org(n).split()
            if toks:
                named.append((i, label, toks))

    out, seen = [], set()
    for a_i, a_label, a_toks in named:
        for b_i, b_label, b_toks in named:
            if a_i >= b_i:
                continue
            # Two rows pinned to one plan org share that org's name by
            # inheritance. That's the many-to-one design working, not a clash.
            pa, pb = rows[a_i].get("plan_organization_id"), rows[b_i].get("plan_organization_id")
            if pa is not None and pa == pb:
                continue
            shared = 0
            for x, y in zip(a_toks, b_toks):
                if x != y:
                    break
                shared += 1
            if not shared:
                continue
            # A full-length equality is a match, not a collision; the crosswalk
            # would have joined them. Only PARTIAL leading overlap is a hazard.
            if shared == len(a_toks) == len(b_toks):
                continue
            key = (a_i, b_i, shared)
            if key in seen:
                continue
            seen.add(key)
            # One name is entirely the opening of the other: the sharpest form
            # of this hazard, because the shorter name is a complete, correct
            # name for a DIFFERENT organization.
            strict = shared in (len(a_toks), len(b_toks))
            out.append({
                "a": a_label, "a_name": " ".join(a_toks),
                "b": b_label, "b_name": " ".join(b_toks),
                "shared_tokens": shared,
                "prefix": " ".join(a_toks[:shared]),
                "strict_prefix": strict,
                # A single shared generic head ("public", "canada") is not a
                # real hazard; two or more tokens, or a strict prefix, is.
                "significant": strict or shared >= 2,
            })
    return sorted(out, key=lambda d: (not d["strict_prefix"], -d["shared_tokens"]))


def report_observed(unattached: list[dict], ambiguous: list[dict],
                    rows: list[dict], proposals: list[dict] | None = None) -> None:
    """The review queue: source strings that bind to no organization."""
    attached = sum(len(r["observed_names"].split(";")) for r in rows
                   if r.get("observed_names"))
    print(f"\n=== OBSERVED NAMES ===")
    print(f"  attached to an organization: {attached}")
    print(f"  ambiguous (>1 candidate):    {len(ambiguous)}")
    print(f"  UNATTACHED (review queue):   {len(unattached)}")

    if proposals:
        print(f"\n--- OAG discovery: org-shaped names that resolve to nothing "
              f"({len(proposals)}) ---")
        print("    Candidates for observed_names. Nothing here affects a join "
              "until\n    a human adds it to org_aliases.yaml — that is what "
              "keeps this pass\n    evidence rather than a feedback loop.")
        for p in proposals:
            print(f"    {p['count']:>4}x  {p['name'][:70]}")

    if ambiguous:
        print("\n--- ambiguous ---")
        for a in ambiguous:
            print(f"  {a['name']!r} -> {a['candidates']}")

    print(f"\n--- unattached, by source ({len(unattached)}) ---")
    for src in ("oag", "tender_contracting", "tender_end_user"):
        got = [u for u in unattached if src in u["sources"]]
        if not got:
            continue
        print(f"\n  [{src}] {len(got)}")
        for u in sorted(got, key=lambda x: -x["count"]):
            print(f"    {u['count']:>4}x  {u['name'][:62]:<62} norm={u['norm'][:34]!r}")


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--rescan", action="store_true",
                        help="Re-read the full contracts CSV instead of the org cache")
    parser.add_argument("--report", action="store_true",
                        help="Print the mapping and the unmatched review queue")
    parser.add_argument("--observed", action="store_true",
                        help="Print the observed-name review queue (OAG + tender strings "
                             "that bind to no organization)")
    parser.add_argument("--collisions", action="store_true",
                        help="Print organizations sharing a leading token run — the "
                             "pairs the not: exclusions are written against")
    parser.add_argument("--attest", action="store_true",
                        help="Re-derive observed_name provenance from the live sources "
                             "and record it in attestation.yaml, then exit")
    parser.add_argument("--db", type=Path, default=None, help=f"Output DB (default {DB_PATH})")
    args = parser.parse_args()

    # Before the crosswalk build, deliberately: attestation needs only the
    # registry and the two evidence sources, and the build ahead of it reads a
    # 640 MB contracts CSV. Making provenance cheap to re-record is most of what
    # decides whether it actually gets re-recorded.
    if args.attest:
        sys.exit(cmd_attest())

    db_path = output_path(DB_PATH, args.db, None)

    contracts = extract_contract_orgs(rescan=args.rescan)
    plans = extract_plan_orgs()
    aliases = load_aliases()
    print(f"Contracts: {len(contracts)} organizations")
    print(f"Registry:  {len(aliases)} canonical keys covering "
          f"{len(slug_index(aliases))} CKAN slugs, from {ALIAS_PATH.name}")

    rows = build_crosswalk(contracts, plans, aliases)

    print("Collecting observed names from OAG audit text and tenders.csv...")
    observed = extract_observed_names()
    unattached, ambiguous = attach_observed(rows, observed, aliases)
    print(f"Observed: {len(observed)} distinct strings, "
          f"{len(unattached)} unattached, {len(ambiguous)} ambiguous")

    build_db(rows, db_path)

    matched = sum(1 for r in rows if r["confidence"] != "none")
    un_c = sum(1 for r in rows if r["match_method"] == "unmatched_contract")
    un_p = sum(1 for r in rows if r["match_method"] == "unmatched_plan")
    print(f"\nWrote {len(rows)} crosswalk rows to {db_path}")
    print(f"  matched pairs:            {matched}")
    print(f"  unmatched (contracts):    {un_c}")
    print(f"  unmatched (plans):        {un_p}")

    if args.report:
        report(rows)
    if args.observed:
        report_observed(unattached, ambiguous, rows,
                        discover_oag_org_names(aliases))
    if args.collisions:
        found = find_prefix_collisions(rows)
        sig = [c for c in found if c["significant"]]
        print(f"\n=== LEADING-SUBSTRING COLLISIONS ===")
        print(f"  significant: {len(sig)}   (of {len(found)}; the rest share one "
              f"generic head token like 'public' or 'canada')")
        for c in sig:
            mark = "STRICT PREFIX" if c["strict_prefix"] else f"{c['shared_tokens']} tok"
            print(f"\n  [{mark}: {c['prefix']!r}]")
            print(f"      {c['a']}\n        {c['a_name']!r}")
            print(f"      {c['b']}\n        {c['b_name']!r}")

    print("\nAttribution: contains information licensed under the "
          "Open Government Licence - Canada.")


if __name__ == "__main__":
    main()
