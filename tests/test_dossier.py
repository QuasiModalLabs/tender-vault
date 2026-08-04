"""
Invariants for the department dossier, checked against the built databases.

Runs with plain Python — no pytest needed:

    python tests/test_dossier.py

Exit code 0 = all passed. Skips cleanly when the data hasn't been built, so a
fresh clone without a network fetch still passes.

WHAT THIS GUARDS. The dossier is the convergence view, and the two ways it can
quietly stop being worth reading are (a) growing a score, and (b) blurring a
distinction one of the four sources took real work to establish.

The scoring one is deliberate architecture: four incommensurable signals
weighted into one number would bury the reasoning the dossier exists to show,
and the reader is a model that can weigh them itself. test_no_convergence_score
is the assertion that keeps a helpful-looking total from appearing later.

The rest are the distinctions. Direct findings versus bundle-attached edges;
stated intent versus retrospective strain; no data versus no signal; a live
source link versus a dead one. Each was a real fact in the sources before it was
a field here, and each collapses into something misleading if merged.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import crosswalk as cw  # noqa: E402
import tender_tools as tt  # noqa: E402

PASSED = FAILED = SKIPPED = 0


def check(cond: bool, label: str) -> None:
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  ok    {label}")
    else:
        FAILED += 1
        print(f"  FAIL  {label}")


def skip(label: str) -> None:
    global SKIPPED
    SKIPPED += 1
    print(f"  skip  {label}")


def dossier(dept: str, **kw) -> dict:
    args = SimpleNamespace(department=dept, limit=kw.get("limit", 10),
                           months_min=6, months_max=24, min_value=None)
    return tt.cmd_department_dossier(args)


def walk(node, path=""):
    """Every (path, value) leaf in the result, for whole-document assertions."""
    if isinstance(node, dict):
        for k, v in node.items():
            yield from walk(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from walk(v, f"{path}[{i}]")
    else:
        yield path, node


# Counts keyed by a name from the data (slug, entity source), so the leaf key is
# whatever the corpus contains rather than a field the dossier chose. Checked as
# containers instead of by leaf name.
COUNTER_CONTAINERS = (".contracts.rows_by_slug", ".tenders.by_entity_source")

# Every numeric field the dossier is allowed to emit. A number outside this set
# is either a new source field (add it here deliberately) or the convergence
# score this project exists without.
ALLOWED_NUMERIC = {
    "it_score", "year", "parent_reports_in_bundle", "direct_count",
    "bundle_attached_count", "audits_total", "federal_audits",
    "federal_audits_attributed", "unattributed",
    "intent_score", "pressure_score", "planned_spending", "program_rows",
    "intent_scored_rows", "pressure_scored_rows", "shared_with",
    "contract_rows_in_extract", "contract_families", "total_value", "value",
    "months_until_expiry", "expiring_count", "expiry_min_value",
    "open_notices", "in_profile_corpus", "feed_scanned",
    "dropped_already_closed", "days_until_close",
}


def test_no_convergence_score():
    """
    THE architectural guarantee, and the formula that was deleted at the start
    of this project. The dossier assembles four signals; it must never weight
    them into one figure or rank departments by it. Any numeric leaf outside the
    per-source whitelist is a new score until proven otherwise.
    """
    d = dossier("ssc")
    numeric = {}
    for path, value in walk(d):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if path.startswith(COUNTER_CONTAINERS):
            continue
        numeric[path.rsplit(".", 1)[-1].split("[")[0]] = path
    strays = {k: v for k, v in numeric.items() if k not in ALLOWED_NUMERIC}
    check(not strays, f"no numeric field outside the per-source whitelist "
                      f"(strays: {strays or 'none'})")

    text = " ".join(str(v).lower() for _, v in walk(d) if isinstance(v, str))
    for word in ("convergence_score", "combined score", "overall score",
                 "total score", "weighted score"):
        check(word not in text, f"the dossier never speaks of a {word!r}")


def test_zero_tenders_still_renders():
    """
    The pre-RFP case, and the whole point of the tool: an audit finding, a plan,
    an expiring incumbent and NO open tender. That must be a first-class result,
    not a degraded one, so every other section stays fully populated and the
    empty one explains itself.
    """
    candidates = [k for k in sorted(cw.load_aliases())
                  if dossier(k, limit=3)["tenders"]["state"] == "none_open"]
    if not candidates:
        skip("no department currently has zero open tenders")
        return
    d = dossier(candidates[0], limit=3)
    check(d["tenders"]["open_notices"] == 0,
          f"{candidates[0]}: zero open notices")
    check(bool(d["tenders"].get("note")),
          "the empty tenders section says why it is empty")
    for section in ("audits", "plans", "contracts"):
        check("state" in d[section] or "error" in d[section],
              f"{section} still renders with a state alongside zero tenders")
    # The section must not editorialise the absence as a negative.
    note = d["tenders"]["note"].lower()
    check("finding, not a gap" in note,
          "absence of a tender reads as a finding, not a failure")


def test_audit_evidence_never_merged():
    """
    Being named in the Auditor General's own finding and being cited inside a
    committee briefing package are different evidence at different strength.
    A merged list silently promotes the weaker one, so they are two lists and
    only the bundle-attached side carries a parent report count.
    """
    d = dossier("ssc", limit=50)["audits"]
    if d.get("state") != "attributed":
        skip("ssc has no attributed audits in this build")
        return
    direct_titles = {a["title"] for a in d["direct_findings"]}
    bundle_titles = {a["title"] for a in d["bundle_attached"]}
    check(not (direct_titles & bundle_titles),
          "no audit appears in both direct_findings and bundle_attached")
    check(all("parent_reports_in_bundle" in a for a in d["bundle_attached"]),
          f"all {len(d['bundle_attached'])} bundle edges carry a parent report count")
    check(all("parent_reports_in_bundle" not in a for a in d["direct_findings"]),
          "no direct finding carries a parent report count")
    check(all(a.get("evidence") for a in d["bundle_attached"]),
          "every bundle edge names the string that produced it")
    check(all("via" in a for a in d["bundle_attached"]),
          "every bundle edge says which method attached it")


def test_dead_oag_links_are_never_presented_as_live():
    """
    oag-bvg.gc.ca deep links no longer resolve — the AG restructured its site and
    214 of 364 rows carry one. They stay citable, but a reader (or a model
    writing an email) must not be handed one as a working link.
    """
    seen_dead, misplaced, audits = 0, [], 0
    for key in ("ssc", "ircc", "cbsa", "esdc"):
        d = dossier(key, limit=50)["audits"]
        for path, value in walk(d):
            # Only values that ARE links matter. The prose warning about dead
            # links names the host on purpose and must not trip this.
            if not isinstance(value, str) or not value.startswith("http"):
                continue
            if "oag-bvg.gc.ca" not in value:
                continue
            seen_dead += 1
            field = path.rsplit(".", 1)[-1]
            if field != "report_url_dead":
                misplaced.append(f"{key}.{path}")
        for audit in d.get("direct_findings", []) + d.get("bundle_attached", []):
            audits += 1
            if not audit.get("source_url", "").startswith(
                    "https://open.canada.ca/data/dataset/"):
                misplaced.append(f"{key}: source_url not a live CKAN dataset link")

    if not seen_dead:
        skip("no oag-bvg.gc.ca links in this build")
        return
    check(not misplaced,
          f"all {seen_dead} dead oag-bvg.gc.ca links sit in report_url_dead, and "
          f"all {audits} audits carry a live source_url "
          f"(violations: {misplaced[:3] or 'none'})")


def test_relation_caveats_come_from_the_registry():
    """
    A predecessor/successor/absorbed edge means the dossier is mixing one
    organization's records into another's. The explanation lives in
    org_aliases.yaml and is quoted verbatim; restating it in code is how the two
    drift apart, and inventing one where the registry has none is worse.
    """
    aliases = cw.load_aliases()
    folded = [(k, item) for k, e in aliases.items()
              for item in (e.get("ckan") or [])
              if (item.get("relation") or "same") != "same"]
    check(bool(folded), f"{len(folded)} folded records exist to check")
    for key, item in folded:
        folds = {f["slug"]: f for f in dossier(key, limit=3)["identity"]["records_folded_in"]}
        fold = folds.get(item["slug"])
        check(fold is not None, f"{key}: the {item['slug']} fold is disclosed")
        if fold is None:
            continue
        check(fold["relation"] == item["relation"],
              f"{key}/{item['slug']}: relation is {item['relation']!r}")
        check("contract_rows_in_extract" in fold,
              f"{key}/{item['slug']}: the fold states its row contribution")
        if item.get("note"):
            check(fold["note"] == item["note"],
                  f"{key}/{item['slug']}: the registry note is quoted verbatim")
            check(fold["note_source"] == "registry",
                  f"{key}/{item['slug']}: the note is sourced to the registry")
        else:
            check(fold["note_source"] == "none recorded in registry",
                  f"{key}/{item['slug']}: absence of a note is stated, not filled in")


def test_ircc_carries_the_passport_caveat():
    """
    The specific case the registry was built around. A dossier for ircc folds in
    Passport Canada's contracts, and passport spending must never read as an
    IRCC-wide figure. In the current extract pptc contributes nothing, which is
    itself a fact the fold has to state rather than omit.
    """
    folds = {f["slug"]: f
             for f in dossier("ircc", limit=3)["identity"]["records_folded_in"]}
    check("pptc" in folds, "ircc discloses the pptc fold")
    if "pptc" not in folds:
        return
    fold = folds["pptc"]
    check(fold["relation"] == "absorbed", "pptc is folded in as 'absorbed'")
    check("one program inside a much larger department" in fold["note"],
          "the caveat says Passport is one program inside a larger department")
    if fold["contract_rows_in_extract"] == 0:
        check(bool(fold.get("contribution")),
              "a fold contributing zero rows says so explicitly")


def test_plans_distinguishes_no_data_from_no_signal():
    """
    Four different empty answers, and conflating them is how "no signal" gets
    read as "nothing to see". Five organizations file no departmental plans at
    all; sixteen file plans but no planning_explanation in any year — including
    the biggest IT buyers — and one of those files no prose of either kind.
    """
    expect = {
        "ssc": "no_intent_prose",          # 102 rows, 0 intent-scored
        "gac": "no_prose_at_all",          # 283 rows, neither field populated
        "national-gallery": "files_no_plans",   # no Infobase id at all
        "ircc": "intent_scored",           # the normal case
    }
    for key, state in expect.items():
        got = dossier(key, limit=3)["plans"].get("state")
        check(got == state, f"{key}: plans state is {state!r} (got {got!r})")

    ssc = dossier("ssc", limit=3)["plans"]
    check(ssc["program_rows"] > 0 and ssc["intent_scored_rows"] == 0,
          f"ssc files {ssc['program_rows']} rows with 0 intent-scored — "
          "the distinction the state exists to make")
    for org in ("DND", "GAC", "RCMP", "CBSA"):
        check(org in ssc["note"],
              f"the note names {org} so the reader sees a source-wide pattern")


def test_intent_and_strain_are_never_ranked_together():
    """
    Intent is a stated forward plan; strain is retrospective over-commitment.
    The scales are not comparable, and blending them is exactly how
    variance-based ranking once scored real IT modernizations negative. They are
    separate keys, and strain only ever appears when there is no intent to rank.
    """
    d = dossier("ssc", limit=10)["plans"]
    check("strain" in d and not d["intent"],
          "ssc: strain appears only because there is no intent to show")
    check(all("intent_score" not in p for p in d.get("strain", [])),
          "no strain row carries an intent score")
    check("RETROSPECTIVE" in d.get("strain_note", ""),
          "the strain block is labelled retrospective")

    ircc = dossier("ircc", limit=10)["plans"]
    check("strain" not in ircc,
          "ircc: no strain block, because intent exists and would be ranked against it")
    check(all("pressure_score" not in p for p in ircc["intent"]),
          "no intent row carries a pressure score")


def test_replicated_prose_is_flagged_not_repeated():
    """
    Departments routinely paste one sentence across every program in the
    inventory — 384 such groups government-wide. Rendering it once per program
    manufactures a pattern that isn't there, so it is shown once, counted, and
    the programs sharing it are named.
    """
    d = dossier("ssc", limit=10)["plans"]
    shared = [p for p in d.get("strain", []) if p.get("shared_with")]
    if not shared:
        skip("no replicated prose in ssc's scored rows in this build")
        return
    for row in shared:
        check(row["shared_with"] == len(row["programs_sharing_it"]),
              f"the {row['shared_with']}-way replicated sentence names all "
              f"{len(row['programs_sharing_it'])} programs sharing it")
        check(bool(row.get("boilerplate_note")),
              "replicated prose says it is one signal, not many")
    prose = [p["variance_explanation"] for p in d["strain"]]
    check(len(prose) == len(set(prose)),
          "no sentence is rendered twice in the strain block")


def test_every_tender_says_how_it_was_attributed():
    """
    Federal IT is routinely bought by SSC or PSPC for the department that
    actually needs it, so the contracting entity is frequently not the customer.
    An attribution that cannot say which field carried it — and on which
    string — is not reviewable, the rule the audit attribution already follows.
    """
    valid = {"end_user", "contracting_entity_end_user_unstated",
             "contracting_entity_end_user_names_others"}
    checked = 0
    bad_source, no_evidence, implied = [], [], []
    for key in ("ssc", "pspc", "dnd", "ircc"):
        for n in dossier(key, limit=25)["tenders"].get("notices", []):
            checked += 1
            ref = f"{key}/{n['tender_id']}"
            if n["entity_source"] not in valid:
                bad_source.append(ref)
            if not n["entity_evidence"]:
                no_evidence.append(ref)
            if n["entity_source"] != "end_user" and not n.get("attribution_note"):
                implied.append(ref)
    if not checked:
        skip("no attributed tenders in this feed")
        return
    check(not bad_source, f"all {checked} notices carry a known entity_source "
                          f"(bad: {bad_source[:3] or 'none'})")
    check(not no_evidence, f"all {checked} notices name the string that produced "
                           f"the attribution (bare: {no_evidence[:3] or 'none'})")
    check(not implied, "contracting-entity attribution is always spelled out, "
                       f"never implied (implied: {implied[:3] or 'none'})")


def test_sentinel_dates_are_not_rendered_as_deadlines():
    """
    Standing arrangements park the closing date decades out — 2065-04-27,
    2076-12-31, 2100-12-31 all appear live. Rendering one as a closing date
    makes a permanent vehicle look like an imminent deadline.
    """
    from datetime import datetime
    horizon = datetime.now().year + tt._SENTINEL_HORIZON_YEARS
    found, rendered, leaked, beyond = 0, 0, [], []
    for key in ("ssc", "pspc", "dnd"):
        for n in dossier(key, limit=40)["tenders"].get("notices", []):
            ref = f"{key}/{n['tender_id']}"
            if n.get("date_note"):
                found += 1
                if n["closing_date"] is not None or "days_until_close" in n:
                    leaked.append(ref)
            elif n["closing_date"]:
                rendered += 1
                if int(n["closing_date"][:4]) > horizon:
                    beyond.append(ref)
    if not found:
        skip("no sentinel dates in this feed")
        return
    check(not leaked, f"all {found} sentinels render as a null closing_date with "
                      f"no countdown (leaked: {leaked[:3] or 'none'})")
    check(not beyond, f"all {rendered} rendered closing dates are inside the "
                      f"{horizon} horizon (beyond: {beyond[:3] or 'none'})")


def test_qualification_notices_are_not_shown_as_work():
    """
    A supply arrangement or standing offer qualifies a supplier onto a vehicle.
    Work is competed later as call-ups against it, often with no public notice,
    so it must not render identically to an open piece of work.
    """
    kinds, quals, mislabelled, silent = set(), 0, [], []
    for key in ("ssc", "pspc", "dnd"):
        for n in dossier(key, limit=40)["tenders"].get("notices", []):
            kinds.add(n["opportunity_kind"])
            if any(q in (n.get("notice_type") or "").lower()
                   for q in tt._QUALIFICATION_NOTICES):
                quals += 1
                ref = f"{key}/{n['tender_id']}"
                if n["opportunity_kind"] != "qualification":
                    mislabelled.append(ref)
                if not n.get("kind_note"):
                    silent.append(ref)
    check(kinds <= {"work", "qualification"},
          f"opportunity_kind is only ever work or qualification (saw {kinds})")
    if not quals:
        skip("no supply arrangements or standing offers in this feed")
        return
    check(not mislabelled, f"all {quals} supply arrangements / standing offers "
                           f"are marked qualification "
                           f"(mislabelled: {mislabelled[:3] or 'none'})")
    check(not silent, f"all {quals} say work is competed later as call-ups "
                      f"(silent: {silent[:3] or 'none'})")


def test_every_registry_key_renders():
    """
    The dossier takes the same identifier as the other four tools, so every key
    in the registry has to produce a document — including the organizations with
    no plans, no contracts, or no audits at all.
    """
    keys = sorted(cw.load_aliases())
    failures, missing = [], []
    for key in keys:
        try:
            d = dossier(key, limit=1)
        except Exception as exc:  # noqa: BLE001 — the point is that none escape
            failures.append(f"{key}: {type(exc).__name__}: {exc}")
            continue
        for section in ("identity", "audits", "plans", "contracts", "tenders"):
            if section not in d:
                missing.append(f"{key}.{section}")
    check(not failures, f"all {len(keys)} registry keys render "
                        f"(failures: {failures[:3] or 'none'})")
    check(not missing, f"every dossier has all five sections "
                       f"(missing: {missing[:3] or 'none'})")


def test_a_fragment_never_resolves_to_a_department():
    """
    Substring matching is how one department's dossier lands on another's name.
    "Immigration and Refugee Board" is an independent tribunal and must reach
    its own key, never IRCC.
    """
    resolver = tt.org_resolve.default_resolver()
    check(resolver.resolve("Immigration and Refugee Board") != "ircc",
          "the tribunal never resolves to IRCC")
    check(resolver.resolve("Immigration and Refugee Board") == "irb",
          "the tribunal resolves to its own key")
    for fragment in ("Immigration", "Shared", "Defence"):
        check(resolver.resolve(fragment) is None,
              f"the fragment {fragment!r} resolves to nothing")


def main() -> int:
    required = [ROOT / "data" / "oag.db", ROOT / "data" / "plans.db",
                ROOT / "data" / "contracts.db", ROOT / "data" / "crosswalk.db"]
    absent = [p.name for p in required if not p.exists()]
    if absent:
        print(f"missing {', '.join(absent)}; run the ingest scripts — skipping")
        return 0

    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(f"\n{name}")
            fn()

    print(f"\n{PASSED} passed, {FAILED} failed, {SKIPPED} skipped")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
