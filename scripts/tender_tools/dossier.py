"""
The department dossier — convergence.

One query, four sources. This ASSEMBLES; it does not score. There is
deliberately no convergence number and no cross-signal ranking: weighting four
incommensurable signals into one figure buries the reasoning that makes the
dossier worth reading, and the reader is a model that can weigh them itself.
Nothing here may combine a value from one section with a value from another.
"""
from __future__ import annotations

import csv
import sqlite3
from collections import Counter
from datetime import datetime, timedelta

import crosswalk
import org_resolve

from ingest import classify_notice as _classify_notice

from . import paths
from .company_profile import (
    _SENTINEL_HORIZON_YEARS,
    _TENDER_COLS,
    _months_until,
    _profile_corpus_ids,
    _profile_expiry_min_value,
)
from .contracts import _contract_rows_by_slug
from .entities import _entity_attribution
# The lobbying section reuses the shaping helpers that back the
# lobbying-signals command, so the dossier and the standalone tool cannot
# drift on what a window is or on how 'not built' is reported.
from .signals import _lobbying_not_built, _lobbying_window


def _dossier_identity(dept: str, rows_by_slug: dict) -> dict:
    """
    Who this department is, and every caveat the registry records about the
    records folded into it.

    The notes are read from org_aliases.yaml verbatim and never restated here.
    A predecessor/successor/absorbed edge means this dossier is mixing one
    organization's records into another's, and the registry is where the
    explanation of that lives; duplicating it in code is how the two drift
    apart. Where the registry records no note, that is said plainly rather than
    papered over with generated prose.
    """
    import crosswalk
    aliases = crosswalk.load_aliases()
    entry = aliases.get(dept, {})
    resolver = org_resolve.default_resolver()

    folds = []
    for item in entry.get("ckan") or []:
        relation = item.get("relation") or "same"
        slug = item.get("slug")
        fold = {"slug": slug, "relation": relation,
                "contract_rows_in_extract": rows_by_slug.get(slug, 0)}
        if relation != "same":
            fold["note"] = item.get("note") or (
                f"The registry records this as a {relation} of {dept} but carries "
                "no note explaining it. Treat the fold as unexplained rather than "
                "assuming it is routine."
            )
            fold["note_source"] = ("registry" if item.get("note")
                                   else "none recorded in registry")
            if not fold["contract_rows_in_extract"]:
                fold["contribution"] = (
                    "Contributes 0 rows to this extract, so nothing in the "
                    "contracts section below comes from it. The caveat still "
                    "stands for any wider query — it is empty here, not absent "
                    "from the department.")
        folds.append(fold)

    return {
        "canonical_key": dept,
        "name": resolver.display_name(dept),
        "registry_note": entry.get("note"),
        "also_known_as": entry.get("observed_names") or [],
        "never_matches": entry.get("not") or [],
        "records_folded_in": folds,
    }


def _dossier_audits(dept: str, limit: int) -> dict:
    """
    What an independent authority has publicly found. Direct findings and
    bundle-attached edges are returned as two lists and are never interleaved:
    being named in the Auditor General's own finding and being cited inside a
    committee briefing package are different evidence at different strength,
    and a merged list silently promotes the weaker one.
    """
    import sqlite3
    db = paths.PROJECT_ROOT / "data" / "oag.db"
    if not db.exists():
        return {"error": "OAG DB not built. Run: python scripts/oag_ingest.py"}

    con = sqlite3.connect(db)
    try:
        meta = dict(con.execute("SELECT key, value FROM meta").fetchall())
        rows = con.execute("""
            SELECT a.oag_id, a.year, a.doc_type, a.title, a.description,
                   a.it_score, a.html_url, d.method, d.evidence, d.n_parent_reports
            FROM audits a
            JOIN audit_departments d ON d.oag_id = a.oag_id
            WHERE d.dept_key = ?
            ORDER BY a.year DESC, a.it_score DESC
        """, (dept,)).fetchall()

        # The other departments named directly in the same audits. An audit
        # naming six departments is a different thing from one naming only this
        # department, and that is invisible without it.
        co_named: dict[str, list[str]] = {}
        for oag_id, *_ in rows:
            co_named[oag_id] = [
                r[0] for r in con.execute(
                    "SELECT DISTINCT dept_key FROM audit_departments "
                    "WHERE oag_id = ? AND method = 'direct' AND dept_key != ? "
                    "ORDER BY dept_key", (oag_id, dept))
            ]
    finally:
        con.close()

    direct, bundled = [], []
    for (oag_id, year, doc_type, title, desc, it_score,
         html_url, method, evidence, n_parents) in rows:
        item = {
            "year": year,
            "doc_type": doc_type,
            "title": title,
            "description": (desc or "")[:500],
            "it_score": it_score,
            # Every oag_id is the CKAN dataset uuid, so a working link can
            # always be built. Needed because html_url is dead for the 214
            # oag-bvg.gc.ca rows — the AG restructured its site and the old
            # deep links 404. Those are carried as text, never as a link.
            "source_url": f"https://open.canada.ca/data/dataset/{oag_id}",
        }
        if html_url and "oag-bvg.gc.ca" in html_url:
            item["report_url_dead"] = html_url
            item["link_note"] = ("oag-bvg.gc.ca deep links no longer resolve; "
                                 "use source_url. Kept for citation only.")
        elif html_url:
            item["report_url"] = html_url

        if method == "direct":
            item["also_named_in_this_audit"] = co_named.get(oag_id, [])
            direct.append(item)
        else:
            item["via"] = method
            item["parent_reports_in_bundle"] = n_parents
            item["evidence"] = evidence
            bundled.append(item)

    federal = int(meta.get("federal_audits") or 0)
    resolved = int(meta.get("federal_audits_resolved") or 0)
    section = {
        "direct_findings": direct[:limit],
        "bundle_attached": bundled[:limit],
        "direct_count": len(direct),
        "bundle_attached_count": len(bundled),
        "corpus": {
            "audits_total": meta.get("audit_count"),
            "federal_audits": federal,
            "federal_audits_attributed": resolved,
            "unattributed": federal - resolved if federal else None,
        },
    }
    if not direct and not bundled:
        section["state"] = "no_audits_attributed"
        section["note"] = (
            f"No audit in this corpus is attributed to {dept!r}. That is not the "
            f"same as a clean record: of {federal} federal audits, {resolved} were "
            f"attributed to a department and {federal - resolved} were not — an "
            "audit that names no department, or names a body outside the federal "
            "executive, resolves to nobody. The residual is where a missed finding "
            "would be hiding."
        )
    else:
        section["state"] = "attributed"
    section["how_to_read"] = (
        "direct_findings are audits that name this department in the finding "
        "itself. bundle_attached are committee briefing packages that cite a "
        "report naming it — real scrutiny, weaker evidence, and "
        "parent_reports_in_bundle says how many reports the package covered "
        "(the more it covered, the less it is about this department "
        "specifically). Never read the two as one list."
    )
    return section


def _dossier_plans(dept: str, plan_ids: list, limit: int) -> dict:
    """
    Forward-looking modernization intent. Intent and strain are returned as two
    separate blocks and are never merged into one ranked list: intent is a
    stated forward plan, strain is retrospective over-commitment, the magnitudes
    are not comparable, and blending them is exactly how variance-based ranking
    once scored real IT modernizations negative.
    """
    import sqlite3
    db = paths.PROJECT_ROOT / "data" / "plans.db"
    if not db.exists():
        return {"error": "Plans DB not built. Run: python scripts/plans_ingest.py"}

    if not plan_ids:
        return {
            "state": "files_no_plans",
            "note": f"{dept!r} files no departmental plans at all — it has no "
                    "Infobase organization id, so this dataset has nothing to "
                    "say about it. A handful of organizations publish contracts "
                    "but file no plans. That is a fact about the organization, "
                    "not missing data.",
        }

    con = sqlite3.connect(db)
    placeholders = ",".join("?" * len(plan_ids))
    try:
        meta = dict(con.execute("SELECT key, value FROM meta").fetchall())
        totals = con.execute(f"""
            SELECT COUNT(*),
                   SUM(intent_score IS NOT NULL),
                   SUM(pressure_score IS NOT NULL),
                   MIN(year), MAX(year)
            FROM programs WHERE organization_id IN ({placeholders})
        """, plan_ids).fetchone()
        total_rows, n_intent, n_pressure, first_year, last_year = totals

        def scored_block(score_col: str, prose_col: str) -> list:
            """Top programs for the most recent year that has any scored row."""
            year = con.execute(f"""
                SELECT MAX(year) FROM programs
                WHERE organization_id IN ({placeholders}) AND {score_col} IS NOT NULL
            """, plan_ids).fetchone()[0]
            if year is None:
                return []
            rows = con.execute(f"""
                SELECT program_name, core_responsibility, {score_col}, {prose_col},
                       planned_spending
                FROM programs
                WHERE organization_id IN ({placeholders})
                      AND year = ? AND {score_col} IS NOT NULL
                ORDER BY {score_col} DESC
            """, plan_ids + [year]).fetchall()

            # Boilerplate: one sentence filed against N programs is one signal,
            # not N. Departments routinely paste a single FTE explanation across
            # every program in the inventory (384 such groups government-wide),
            # and rendering it N times manufactures a pattern that isn't there.
            spread = Counter(r[3] for r in rows)
            out, seen = [], set()
            for (prog, core, score, prose, planned) in rows:
                shared = spread[prose]
                if shared > 1 and prose in seen:
                    continue
                seen.add(prose)
                item = {
                    "year": year, "program": prog, "core_responsibility": core,
                    score_col: score, prose_col: prose,
                    "planned_spending": planned,
                }
                if shared > 1:
                    item["shared_with"] = shared
                    item["boilerplate_note"] = (
                        f"This same sentence is filed against {shared} programs "
                        f"in {year}. One replicated sentence is one signal, not "
                        f"{shared} — shown once, with the programs listed."
                    )
                    item["programs_sharing_it"] = [
                        r[0] for r in rows if r[3] == prose
                    ]
                out.append(item)
            return out[:limit]

        intent = scored_block("intent_score", "planning_explanation")
        strain = scored_block("pressure_score", "variance_explanation") if not intent else []

        inventory = [
            {"program": r[0], "core_responsibility": r[1], "planned_spending": r[2]}
            for r in con.execute(f"""
                SELECT program_name, core_responsibility, planned_spending
                FROM programs
                WHERE organization_id IN ({placeholders}) AND year = ?
                ORDER BY planned_spending DESC
            """, plan_ids + [last_year]).fetchall()
        ][:limit]
    finally:
        con.close()

    section = {
        "as_of": meta.get("ingest_date"),
        "years_on_file": f"{first_year}-{last_year}" if first_year else None,
        "program_rows": total_rows,
        "intent_scored_rows": n_intent,
        "pressure_scored_rows": n_pressure,
        "intent": intent,
        "inventory": inventory,
        "inventory_note": (
            f"Every program on the books in {last_year}, ordered by planned "
            "spending — the only ranking available when nothing is scored. "
            "Presence and budget, not intent."
        ),
    }

    if intent:
        section["state"] = "intent_scored"
    elif n_pressure:
        section["state"] = "no_intent_prose"
        section["strain"] = strain
        section["strain_note"] = (
            "RETROSPECTIVE, and not a plan. These score variance_explanation — "
            "the department explaining why actual spending or headcount diverged "
            "from what it planned. Read as evidence of over-commitment already "
            "incurred, never as stated forward intent, and never ranked against "
            "intent_score: the two measure different things on different scales."
        )
        section["note"] = _NO_INTENT_PROSE_NOTE.format(dept=dept, rows=total_rows,
                                                       pressure=n_pressure)
    elif total_rows:
        section["state"] = "no_prose_at_all"
        section["note"] = (
            f"{dept!r} files {total_rows} program rows but populates neither "
            "planning_explanation nor variance_explanation in any year, so "
            "nothing is scored on either axis. The spending and FTE figures are "
            "filed; the prose the signal is built from is not. Nothing here is "
            "readable as intent or strain — the inventory below is all this "
            "source has."
        )
    else:
        section["state"] = "no_program_rows"
        section["note"] = (f"{dept!r} has an Infobase id but no program rows in "
                           "this extract.")
    return section


# Stated once, because it is a property of the source rather than of any one
# department, and a reader meeting it on SSC needs to know that immediately.
_NO_INTENT_PROSE_NOTE = (
    "{dept!r} files {rows} program rows but zero planning_explanation in any "
    "year, so no program is intent-scored. This is a filing pattern in the "
    "source, not something odd about this department: 16 of 94 organizations "
    "file none at all, including DND, GAC, RCMP, CBSA and SSC — together 11.7% "
    "of planned spending. The field is an optional note explaining why planned "
    "spending or FTE figures move between years, not a general statement of "
    "intent, and a department with steady figures has nothing to file. The "
    "narrative Departmental Plan, where modernization commitments are actually "
    "written down, is a separate publication that is not in this dataset. "
    "{pressure} rows do carry variance_explanation and are scored for strain "
    "below."
)


def _dossier_contracts(dept: str, slugs: list, rows_by_slug: dict,
                       months_min: int, months_max: int,
                       min_value: float, limit: int) -> dict:
    """
    Who holds the work and when it runs out. The expiry timeline is the most
    actionable field in the dossier: a contract in our competency space ending
    in 6-24 months is a near-certain re-procurement with the incumbent, the
    value and the scope already public.

    Both halves aggregate per contract family taking the family's highest
    recorded value and latest period_end, matching contracts-intel and
    expiring-contracts exactly, so the dossier can never disagree with the
    standalone tools about the same department.
    """
    import sqlite3
    from datetime import timedelta
    db = paths.PROJECT_ROOT / "data" / "contracts.db"
    if not db.exists():
        return {"error": "Contracts DB not built. Run: python scripts/contracts_ingest.py"}

    if not slugs:
        return {
            "state": "no_slug",
            "note": f"{dept!r} publishes no contracts under its own slug, so this "
                    "dataset cannot answer for it. Some organizations file plans "
                    "but disclose no contracts of their own. That is a fact about "
                    "the organization, not a build failure.",
        }

    today = datetime.now()
    lo = (today + timedelta(days=30 * months_min)).strftime("%Y-%m-%d")
    hi = (today + timedelta(days=30 * months_max)).strftime("%Y-%m-%d")
    placeholders = ",".join("?" * len(slugs))

    con = sqlite3.connect(db)
    try:
        meta = dict(con.execute("SELECT key, value FROM meta").fetchall())
        vendors = con.execute(f"""
            SELECT vendor_norm, COUNT(*) AS families, SUM(v) AS total
            FROM (SELECT family_id, vendor_norm, MAX(value) AS v
                  FROM contracts WHERE owner_org IN ({placeholders})
                  GROUP BY family_id, vendor_norm)
            WHERE vendor_norm IS NOT NULL
            GROUP BY vendor_norm
            ORDER BY total DESC
            LIMIT ?
        """, slugs + [limit]).fetchall()

        expiring = con.execute(f"""
            SELECT MAX(period_end) AS expiry, vendor, vendor_norm,
                   MAX(value) AS v, MAX(contract_date) AS awarded, description
            FROM contracts
            WHERE owner_org IN ({placeholders}) AND period_end != ''
            GROUP BY family_id
            HAVING expiry >= ? AND expiry <= ? AND v >= ?
            ORDER BY expiry ASC, v DESC
        """, slugs + [lo, hi, min_value]).fetchall()
    finally:
        con.close()

    rows_total = sum(rows_by_slug.values())
    if not rows_total:
        return {
            "state": "slug_but_no_rows",
            "slugs": slugs,
            "as_of": meta.get("ingest_date"),
            "note": f"{dept!r} owns {len(slugs)} CKAN slug(s) but has no rows in "
                    f"this extract, which covers the last "
                    f"{meta.get('window_years')} years filtered to IT and "
                    "telecom categories. The organization discloses contracts; "
                    "none of them are recent IT work.",
        }

    timeline = [
        {"incumbent": r[1], "incumbent_norm": r[2], "expiry": r[0],
         "months_until_expiry": _months_until(r[0], today),
         "value": r[3], "awarded": r[4], "description": (r[5] or "")[:220]}
        for r in expiring
    ]

    return {
        "state": "has_contracts",
        "as_of": meta.get("ingest_date"),
        "window_years": meta.get("window_years"),
        "slugs": slugs,
        "rows_by_slug": rows_by_slug,
        "top_vendors": [
            {"vendor": v, "contract_families": n, "total_value": round(t or 0)}
            for v, n, t in vendors
        ],
        "expiry_window": f"{months_min}-{months_max} months out ({lo} to {hi})",
        "expiry_min_value": min_value,
        "expiring_count": len(timeline),
        "expiry_timeline": timeline[:limit],
        "expiry_truncated": len(timeline) > limit,
        "caveats": "Unaudited; vendor names lightly normalized (suffix and "
                   "punctuation only, not fuzzy-matched), so one vendor may "
                   "appear under two spellings. Standing offers and supply "
                   "arrangements carry far-future end dates and are re-competed "
                   "differently — the description usually reveals which.",
    }


def _dossier_tenders(dept: str, limit: int) -> dict:
    """
    Open notices, if any — and the absence is the interesting case.

    A department with an audit finding, a stated plan, an expiring incumbent and
    NO open tender is the pre-RFP position this whole project exists to find:
    the work is coming and nobody has been asked yet. So this section is not
    required, carries no weight against the other three, and must read as a
    finding rather than as a failure when it is empty.

    Reads the full open-notice feed rather than the profile-filtered corpus,
    because "is this department in market right now" is a question about the
    department, not about our fit. Notices that ALSO cleared the profile filter
    are flagged, so the two questions stay separable.
    """
    import csv
    if not paths._TENDERS_CSV.exists():
        return {"state": "no_feed",
                "note": "No open-notice feed cached. Run: python scripts/ingest.py"}

    in_corpus = _profile_corpus_ids()
    today = datetime.now()
    horizon = today.replace(year=today.year + _SENTINEL_HORIZON_YEARS).strftime("%Y-%m-%d")
    today_str = today.strftime("%Y-%m-%d")

    notices, scanned, expired = [], 0, 0
    with open(paths._TENDERS_CSV, encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            scanned += 1
            # The distinction the whole section turns on — which field carried
            # the attribution — is decided by _entity_attribution, shared with
            # promote so a tender file and a dossier never disagree about it.
            attribution = _entity_attribution(
                row.get(_TENDER_COLS["end_user"]),
                row.get(_TENDER_COLS["contracting"]),
            )
            if dept not in attribution:
                continue
            source = attribution[dept]["entity_source"]
            evidence = attribution[dept]["entity_evidence"]
            end_user = [k for k, a in attribution.items()
                        if a["entity_source"] == "end_user"]

            closing = (row.get(_TENDER_COLS["closing"]) or "")[:10]
            item = {
                "tender_id": row.get(_TENDER_COLS["tender_id"]),
                "title": row.get(_TENDER_COLS["title"]),
                "notice_type": row.get(_TENDER_COLS["notice_type"]) or None,
                "entity_source": source,
                "entity_evidence": evidence,
                "in_profile_corpus": row.get(_TENDER_COLS["tender_id"]) in in_corpus,
                "url": (row.get(_TENDER_COLS["url"]) or "").strip() or None,
            }
            if not item["url"]:
                # Only 423 of 896 notices carry one, and they are the ones also
                # posted to a third-party site like MERX. A CanadaBuys-native
                # notice files no URL, so there is nothing to link to — say so
                # rather than let a null read as a broken field. No URL is
                # constructed: guessing one is how the dead oag-bvg.gc.ca links
                # in the audits section happened.
                item["url_note"] = ("No notice URL filed — search CanadaBuys by "
                                    "the reference number.")
            if source != "end_user":
                item["attribution_note"] = (
                    "Attributed via the contracting entity; the end user is "
                    "unstated." if source.endswith("unstated") else
                    "Attributed via the contracting entity, which is buying on "
                    "behalf of a different named end user.")
            if end_user and source != "end_user":
                item["end_user_named"] = sorted(end_user)

            # ONE definition, imported — not a second copy that agrees today.
            # The copy that used to live here substring-matched "supply
            # arrangement", which labelled all 54 "RFP against Supply
            # Arrangement" notices `qualification` when a call-up is the exact
            # opposite: real work, competed among vehicle holders. It also
            # labelled "Invitation to Qualify" as work. See classify_notice.
            item.update(_classify_notice(
                item["notice_type"],
                row.get(_TENDER_COLS["procurement_category"]),
                f"{row.get(_TENDER_COLS['title']) or ''} "
                f"{row.get(_TENDER_COLS['description']) or ''}",
            ))

            if not closing:
                item["closing_date"] = None
            elif closing > horizon:
                # Not a date. Standing arrangements park the field decades out.
                item["closing_date"] = None
                item["date_note"] = (
                    f"Filed as {closing} — a sentinel meaning the arrangement "
                    "has no real close, not a deadline.")
            elif closing < today_str:
                # The feed is 'open tender notices', but 55 of 896 rows carry a
                # closing date already past. Trust the date over the status.
                expired += 1
                continue
            else:
                item["closing_date"] = closing
                item["days_until_close"] = (
                    datetime.strptime(closing, "%Y-%m-%d") - today).days
            notices.append(item)

    notices.sort(key=lambda n: (n["closing_date"] is None, n["closing_date"] or ""))
    by_source = Counter(n["entity_source"] for n in notices)

    section = {
        "open_notices": len(notices),
        "notices": notices[:limit],
        "truncated": len(notices) > limit,
        "by_entity_source": dict(by_source),
        "in_profile_corpus": sum(1 for n in notices if n["in_profile_corpus"]),
        "feed_scanned": scanned,
        "dropped_already_closed": expired,
        # How old the feed is, on the same footing as the `as_of` the
        # contracts, plans and audits sections already carry — this section
        # was the only one reporting undated numbers.
        #
        # The mtime of the file just read, NOT the stamp in the corpus
        # metadata: this section deliberately bypasses ChromaDB and reads the
        # CSV directly, so it must report the provenance of what IT read. The
        # two can legitimately differ — a corpus built this morning off a feed
        # downloaded yesterday, then the feed re-downloaded since.
        "feed_downloaded_at": datetime.fromtimestamp(
            paths._TENDERS_CSV.stat().st_mtime).isoformat(timespec="seconds"),
    }
    if not notices:
        section["state"] = "none_open"
        section["note"] = (
            f"No open notice in the feed attributes to {dept!r}. This is a "
            "finding, not a gap — a department with an audit finding, a stated "
            "plan and an expiring incumbent but no open tender is the pre-RFP "
            "position worth acting on, because the work is coming and nobody "
            "has been asked yet. Read the other three sections on their own "
            "terms; nothing here weakens them."
        )
    else:
        section["state"] = "open_notices"
    section["how_to_read"] = (
        "entity_source says how the notice reached this department. 'end_user' "
        "means it named them as the customer. Anything else means they are the "
        "contracting entity — attributed via contracting entity, end user "
        "unstated — which for SSC and PSPC frequently means they are buying for "
        "somebody else entirely. in_profile_corpus marks the notices that also "
        "cleared our profile filter; the rest are the department being in "
        "market regardless of our fit."
    )
    return section


def _dossier_lobbying(dept: str, limit: int) -> dict:
    """
    Who has been in the room with this department, and on what subject.

    The earliest section, and the one most easily misread, so it returns two
    things rather than a list of meetings: WHO turns up (ranked by how many
    disclosed communications name this department) and WHAT they filed under.
    A dossier reader wants the pattern — this department has been hearing from
    these firms about procurement for two years — not twenty individual dates.

    Procurement-subject counts are reported separately from the total for the
    same reason the audit section splits direct from bundle-attached: a firm
    that meets a department about Health and a firm that meets it about
    Government Procurement are not the same signal, and one list merges them.
    """
    import sqlite3
    db = paths.PROJECT_ROOT / "data" / "lobbying.db"
    if not db.exists():
        return _lobbying_not_built()

    con = sqlite3.connect(db)
    try:
        meta = dict(con.execute("SELECT key, value FROM meta").fetchall())
        # Both counts are COUNT(DISTINCT comlog_id), and the second one has to
        # be: the join to communication_dpohs emits a row per office holder, so
        # a meeting with three SSC officials appears three times. A SUM(CASE)
        # over that counts the duplicates and reports a procurement subset
        # LARGER than the total it is a subset of.
        clients = con.execute("""
            SELECT c.client_name, COUNT(DISTINCT c.comlog_id) n,
                   MIN(c.comm_date), MAX(c.comm_date),
                   COUNT(DISTINCT CASE WHEN EXISTS (
                       SELECT 1 FROM communication_subjects s
                       WHERE s.comlog_id = c.comlog_id
                         AND s.subject = 'Government Procurement')
                       THEN c.comlog_id END) procurement
            FROM communications c
            JOIN communication_dpohs d ON d.comlog_id = c.comlog_id
            WHERE d.dept_key = ? AND c.client_name != ''
            GROUP BY c.client_norm
            ORDER BY n DESC
            LIMIT ?
        """, (dept, limit)).fetchall()
        subjects = con.execute("""
            SELECT s.subject, COUNT(DISTINCT s.comlog_id) n
            FROM communication_subjects s
            JOIN communication_dpohs d ON d.comlog_id = s.comlog_id
            WHERE d.dept_key = ?
            GROUP BY 1 ORDER BY n DESC LIMIT ?
        """, (dept, limit)).fetchall()
        total = con.execute(
            "SELECT COUNT(DISTINCT d.comlog_id) FROM communication_dpohs d "
            "WHERE d.dept_key = ?", (dept,)).fetchone()[0]
    finally:
        con.close()

    section = {
        "communications": total,
        "top_clients": [
            {"client": name, "communications": n, "first": first, "latest": last,
             "on_government_procurement": proc}
            for name, n, first, last, proc in clients
        ],
        "subjects_filed": [{"subject": s, "communications": n} for s, n in subjects],
        "window": _lobbying_window(meta),
    }
    if not total:
        section["state"] = "no_communications_in_window"
        section["note"] = (
            f"No disclosed communication in the window names {dept!r}. That is "
            "not evidence that nobody met them: only ARRANGED ORAL "
            "communications with designated public office holders are "
            "reportable, and the database is windowed at ingest."
        )
    else:
        section["state"] = "present"
    section["how_to_read"] = (
        "Evidence of PRESENCE, never of influence, and never a finding about "
        "any firm named — filing these reports is what compliance with the "
        "Lobbying Act looks like. Read it as: this department has been hearing "
        "from these organizations, on these subjects, over this period. "
        "on_government_procurement is the subset filed under that subject "
        "specifically, which is the one that bears directly on a coming "
        "requirement. Cross-read against the plans and contracts sections: a "
        "firm meeting a department about procurement while that department "
        "plans to modernize a system it already holds the contract for is an "
        "incumbent defending a renewal."
    )
    return section


def cmd_department_dossier(args) -> dict:
    """
    Everything all four sources know about one department, in one query.

    The convergence view. Each signal is useful alone; the payoff is when they
    line up — the Auditor General flagged a department, its own plan says it
    intends to modernize that system, and the incumbent's contract expires in
    five months. That is about as strong a pre-RFP case as public data can
    produce, and a live tender from that department should be read in its light.

    This tool ASSEMBLES AND PRESENTS. It does not score. There is no convergence
    number, no weighting of the four signals into one figure, and no ranking of
    departments by it — deliberately. The four signals are incommensurable, any
    weighting would be invented, and a single number would hide the reasoning
    that makes the dossier worth reading in the first place. Claude reads the
    four sections and judges. That is the architecture.

    Sections are ordered by their own native logic and never against each other:
    audits by year, plans by intent within the most recent scored year,
    contracts by soonest expiry, tenders by soonest close.

    TENDERS ARE NOT REQUIRED. The highest-value output of this tool is a
    department with an audit finding, a stated plan, an expiring incumbent and
    NO open tender.
    """
    dept = org_resolve.resolve_department_arg(
        getattr(args, "department", None), flag="dossier")
    if not dept:
        return {"error": "A department is required. Pass a canonical key from "
                         "vault/crosswalk/org_aliases.yaml or a registered name."}

    limit = getattr(args, "limit", None) or 10
    months_min = getattr(args, "months_min", None) or 6
    months_max = getattr(args, "months_max", None) or 24
    min_value = getattr(args, "min_value", None)
    if min_value is None:
        min_value = _profile_expiry_min_value()

    scope = org_resolve.department_scope(dept)
    rows_by_slug = _contract_rows_by_slug(scope["contract_slugs"])
    return {
        "department": dept,
        "generated": datetime.now().strftime("%Y-%m-%d"),
        "identity": _dossier_identity(dept, rows_by_slug),
        "audits": _dossier_audits(dept, limit),
        "plans": _dossier_plans(dept, scope["plan_ids"], limit),
        "contracts": _dossier_contracts(dept, scope["contract_slugs"], rows_by_slug,
                                        months_min, months_max, min_value, limit),
        "lobbying": _dossier_lobbying(dept, limit),
        "tenders": _dossier_tenders(dept, limit),
        "how_to_read": (
            "Five independent sources on one department. Read them together and "
            "form your own judgement — there is no score here on purpose. What "
            "you are looking for is agreement between sections: an audit finding "
            "and a plan and an expiring incumbent that are all about the same "
            "system. An empty section is information too, and each one says "
            "which kind of empty it is — no data, or no signal. The lobbying "
            "section is the exception to 'read them together': it is evidence "
            "of who was present, never of influence, and it may not be used to "
            "explain why any other section says what it says. Check "
            "identity.records_folded_in before quoting any total: where a "
            "predecessor or absorbed organization has been folded in, the "
            "registry's note says what the number actually covers."
        ),
    }

