"""
The pre-RFP signals: departmental plans, OAG audits, and the registry lookup.

Neither signal predicts a procurement. They surface departments under
operational or independent-scrutiny pressure, and the reader does the judging —
which is why nothing here returns a combined score.
"""
from __future__ import annotations

import sqlite3

import org_resolve

from . import paths

def cmd_program_signals(args) -> dict:
    """
    Surface government programs showing operational pressure — the pre-RFP intent
    signal from Departmental Plans / Results.

    Ranks by single-year pressure_score (semantic strain in the department's own
    words, minus accounting noise), because the variance_explanation field is
    populated sparsely across years — most programs are explained in only some
    of their years — so requiring a multi-year chronic pattern threw away the
    real single-year signals (named IT modernizations, backlogs, failed
    recruitment) and kept boilerplate that happened to repeat.

    BUT chronic strain is the stronger signal in principle (a multi-year pattern
    is what draws the OAG audits, committee grillings, and media pressure that
    force a department to procure a fix). So where a program DOES have multiple
    scored years, we surface its year-by-year trail and a `multi_year` flag, so
    Claude can SEE chronicity when the data supports it — without requiring it.
    True chronic-pattern detection wants the OAG / committee data (which
    explicitly documents "this has failed for N years"); that's a documented
    future source, and the natural home for the "impending scrutiny" signal.

    Read the variance prose and judge IT-relevance — programs aren't
    capability-tagged, so an IT modernization can live inside any department.
    A lead list, not a forecast.

    Filters:
      --department SUBSTR : restrict to matching departments
      --min-score FLOAT   : only programs above this pressure_score (default 0)
      --include-internal  : include Internal Services (excluded by default —
                            back-office strain isn't program-delivery opportunity)
      --limit N           : how many to return (default 25)
    """
    import sqlite3

    db = paths.PROJECT_ROOT / "data" / "plans.db"
    if not db.exists():
        return {"error": "Plans DB not built. Run: python scripts/plans_ingest.py"}

    con = sqlite3.connect(db)
    meta = dict(con.execute("SELECT key, value FROM meta").fetchall())

    # No score floor by default: real IT leads can score slightly negative
    # (a great lead phrased as a budget variance sits between the poles), so a
    # floor at 0 hid genuine signal. Rank by score, let --limit control volume;
    # --min-score is available to filter when wanted.
    min_score = getattr(args, "min_score", None)
    # Internal Services now INCLUDED by default: in this dataset departmental IT
    # spend is booked under Internal Services, so excluding it hid the exact
    # IT-modernization programs an IT firm wants. Use --exclude-internal to drop
    # it. Claude should still distinguish vendor-addressable IT modernization
    # from union-locked operational delivery when reading Internal Services rows.
    exclude_internal = getattr(args, "exclude_internal", False)

    where = ["intent_score IS NOT NULL"]
    params: list = []
    if min_score is not None:
        where.append("intent_score >= ?")
        params.append(min_score)
    # The plans file keys on the Infobase organization_id, which is stable
    # across rebrands — the reason the crosswalk carries it. Filtering on that
    # id rather than the organization name means a department that was renamed
    # mid-series still returns its whole history.
    dept = org_resolve.resolve_department_arg(getattr(args, "department", None))
    if dept:
        plan_ids = org_resolve.department_scope(dept)["plan_ids"]
        if not plan_ids:
            con.close()
            return {"department": dept, "programs": 0,
                    "note": f"{dept!r} files no departmental plans, so this "
                            "dataset cannot answer for it. Some organizations "
                            "publish contracts but no plans; that is a fact "
                            "about them, not a build failure."}
        where.append(f"organization_id IN ({','.join('?' * len(plan_ids))})")
        params += plan_ids
    if exclude_internal:
        where.append("lower(program_name) NOT LIKE '%internal service%'")

    limit = getattr(args, "limit", None) or 25
    rows = con.execute(f"""
        SELECT year, organization, program_name, core_responsibility,
               intent_score, planning_explanation,
               pressure_score, variance_explanation,
               planned_spending, actual_spending
        FROM programs
        WHERE {' AND '.join(where)}
        ORDER BY intent_score DESC
        LIMIT ?
    """, params + [limit]).fetchall()

    if not rows:
        # "Nothing above the floor" and "nothing scored at all" are different
        # answers and this used to give the first for both. Ranking is on
        # intent_score, which requires planning_explanation — and 16 of 94
        # organizations, including DND, GAC, RCMP, CBSA and SSC, file none in
        # any year. For them no floor is low enough, and saying so is the
        # difference between a tunable filter and a structural absence.
        scope_where, scope_params = "", []
        if dept:
            plan_ids = org_resolve.department_scope(dept)["plan_ids"]
            scope_where = f" WHERE organization_id IN ({','.join('?' * len(plan_ids))})"
            scope_params = plan_ids
        total, n_intent, n_pressure = con.execute(
            f"SELECT COUNT(*), SUM(intent_score IS NOT NULL), "
            f"SUM(pressure_score IS NOT NULL) FROM programs{scope_where}",
            scope_params).fetchone()
        con.close()

        if not n_intent:
            note = (f"No intent-scored programs{f' for {dept!r}' if dept else ''}: "
                    f"{total} program rows on file, 0 carrying a "
                    f"planning_explanation to score, {n_pressure or 0} carrying a "
                    "variance_explanation and scored for retrospective strain "
                    "instead. Lowering --min-score cannot help — there is nothing "
                    "on this axis to filter. The `dossier` command reads the "
                    "strain rows and the program inventory for exactly this case.")
        else:
            note = (f"{n_intent} intent-scored programs exist"
                    f"{f' for {dept!r}' if dept else ''} but none clear the "
                    f"current floor of {min_score}. Lower --min-score.")
        return {
            "as_of": meta.get("ingest_date"),
            "programs": 0,
            "intent_scored_rows": n_intent or 0,
            "pressure_scored_rows": n_pressure or 0,
            "note": note,
        }

    results = []
    for (year, org, prog, core, iscore, pe, pscore, ve, planned, actual) in rows:
        over = None
        if planned and actual and planned > 0:
            over = round((actual - planned) / planned * 100, 1)

        # Pull the program's intent-scored trail across years (context).
        trail = con.execute("""
            SELECT year, intent_score, planning_explanation
            FROM programs
            WHERE organization = ? AND program_name = ?
                  AND intent_score IS NOT NULL
            ORDER BY year
        """, (org, prog)).fetchall()

        other_years = []
        for (yr, sc, tpe) in trail:
            if yr == year:
                continue
            other_years.append({
                "year": yr, "intent_score": sc,
                "planning_explanation": (tpe or "")[:300],
            })

        results.append({
            "year": year,
            "department": org,
            "program": prog,
            "core_responsibility": core,
            "intent_score": iscore,
            "planning_explanation": pe,        # forward-looking: what they PLAN
            "pressure_score": pscore,          # retrospective strain (context)
            "variance_explanation": ve,
            "pct_over_plan": over,
            "multi_year": len(trail) > 1,
            "other_scored_years": other_years,
        })

    con.close()
    return {
        "as_of": meta.get("ingest_date"),
        "intent_scored_total": meta.get("intent_scored"),
        "returned": len(results),
        "filters": {"department": dept, "min_score": min_score,
                    "internal_services": "excluded" if exclude_internal else "included"},
        "programs": results,
        "how_to_read": "Ranked by intent_score — FORWARD-LOOKING modernization "
                       "intent from each program's planning_explanation (what the "
                       "department PLANS to spend on: modernize, replace, migrate, "
                       "build), scored toward IT/modernization language and away "
                       "from routine-operations noise. This is the pre-RFP signal: "
                       "a stated plan to modernize a system is a conversation to "
                       "open before the RFP. pressure_score/variance_explanation "
                       "are secondary context (retrospective strain); a program "
                       "that BOTH struggled and plans to modernize is strongest. "
                       "Internal Services is included by default (that's where "
                       "departmental IT is booked) — but distinguish vendor-buildable "
                       "IT modernization from union-locked operational delivery. "
                       "Judge IT-relevance and credibility (a 25-person firm isn't "
                       "a prime on a $90M program). A lead list, not a forecast.",
    }


def cmd_oag_signals(args) -> dict:
    """
    Surface OAG performance audits (and committee hearings) touching IT/systems,
    ranked by IT-relevance — the independent-scrutiny pre-RFP signal.

    Where contracts_intel says WHAT was bought, expiring_contracts says WHAT is
    up for renewal, and program-signals says what a department PLANS, this says
    what an independent authority has PUBLICLY found them failing at. "The AG
    flagged your processing backlog in 2023" is the most citable pre-RFP opener
    of all, and OAG/committee scrutiny is what forces a department to procure.

    CONVERGENCE is the point: filter by --department to cross-reference against
    program-signals (same dept planning to modernize?) and contracts-intel /
    expiring-contracts (who holds their IT work, expiring when?). When OAG +
    plans + an expiring contract point at the same department, that is the
    strongest pre-RFP case — and a live tender from that department should rank
    higher because of it.

    Read each audit and judge IT-relevance + whether the finding is a real
    opportunity. A lead list, not a forecast.

    Filters:
      --department SUBSTR : restrict to audits of matching departments
      --min-score FLOAT   : only audits with it_score at/above this (default: none)
      --doc-type TYPE     : performance_audit | committee_hearing | special_examination
      --since YYYY        : only audits from this year onward
      --limit N           : how many to return (default 20)
    """
    import sqlite3

    db = paths.PROJECT_ROOT / "data" / "oag.db"
    if not db.exists():
        return {"error": "OAG DB not built. Run: python scripts/oag_ingest.py"}

    con = sqlite3.connect(db)
    meta = dict(con.execute("SELECT key, value FROM meta").fetchall())

    where, params = ["a.it_score IS NOT NULL"], []
    # A canonical key from org_aliases.yaml, joined on — never a substring of a
    # display name. "Immigration and Refugee Board" is an independent tribunal
    # and must not answer to IRCC; that refusal is written into the registry and
    # only an exact identifier honours it.
    dept = org_resolve.resolve_department_arg(getattr(args, "department", None))
    if dept:
        where.append("EXISTS (SELECT 1 FROM audit_departments d "
                     "WHERE d.oag_id = a.oag_id AND d.dept_key = ?)")
        params.append(dept)
    # Direct findings only — drop departments a briefing package inherited from
    # a multi-report hearing, which are real but much weaker evidence.
    if getattr(args, "direct_only", False):
        where.append("EXISTS (SELECT 1 FROM audit_departments d "
                     "WHERE d.oag_id = a.oag_id AND d.method = 'direct')")
    vendor = getattr(args, "vendor", None)
    if vendor:
        # Deliberately independent of --department: an audit into a supplier is
        # competitive intelligence whichever departments it touches, and the two
        # that exist are unattributable to any single one.
        where.append("lower(a.vendor_focus) LIKE ?")
        params.append(f"%{vendor.lower()}%")
    min_score = getattr(args, "min_score", None)
    if min_score is not None:
        where.append("a.it_score >= ?")
        params.append(min_score)
    doc_type = getattr(args, "doc_type", None)
    if doc_type:
        where.append("a.doc_type = ?")
        params.append(doc_type)
    since = getattr(args, "since", None)
    if since:
        where.append("a.year >= ?")
        params.append(since)

    limit = getattr(args, "limit", None) or 20
    rows = con.execute(f"""
        SELECT a.oag_id, a.year, a.doc_type, a.title, a.description,
               a.it_score, a.html_url, a.pdf_url,
               a.attribution_status, a.vendor_focus
        FROM audits a
        WHERE {' AND '.join(where)}
        ORDER BY a.it_score DESC, a.year DESC
        LIMIT ?
    """, params + [limit]).fetchall()

    # Departments per audit, split by how they were reached. A dossier shows the
    # direct findings; inherited ones are the same audit seen through a hearing
    # agenda, and a department reached through a 5-report bundle is weaker
    # evidence than one named in the audit itself.
    edges: dict[str, dict] = {}
    if rows:
        marks = ",".join("?" * len(rows))
        for oag_id, key, method, evidence, n_parents in con.execute(
            f"SELECT oag_id, dept_key, method, evidence, n_parent_reports "
            f"FROM audit_departments WHERE oag_id IN ({marks}) "
            f"ORDER BY method, dept_key", [r[0] for r in rows]
        ):
            slot = edges.setdefault(oag_id, {"direct": [], "inherited": []})
            if method == "direct":
                slot["direct"].append(key)
            else:
                slot["inherited"].append(
                    {"department": key, "via": method,
                     "reports_in_hearing": n_parents, "evidence": evidence})
    con.close()

    if not rows:
        return {"as_of": meta.get("ingest_date"), "audits": 0,
                "note": "No audits matched. Loosen --min-score/--department/"
                        "--since, or drop --direct-only."}

    results = []
    for (oag_id, year, dt, title, desc, score, html, pdf, status, vendor_focus) in rows:
        got = edges.get(oag_id, {"direct": [], "inherited": []})
        entry = {
            "year": year,
            "doc_type": dt,
            "departments": got["direct"],
            "title": title,
            "description": (desc or "")[:500],
            "it_score": score,
            "report_url": html or pdf,
        }
        if got["inherited"]:
            entry["inherited_departments"] = got["inherited"]
        if not got["direct"] and not got["inherited"]:
            entry["no_department_because"] = status
        if vendor_focus:
            entry["vendor_focus"] = vendor_focus
        results.append(entry)

    return {
        "as_of": meta.get("ingest_date"),
        "audit_count_total": meta.get("audit_count"),
        "coverage": {
            "federal_audits": f"{meta.get('federal_audits_resolved')}"
                              f"/{meta.get('federal_audits')} attributed directly",
            "committee_briefings": f"{meta.get('briefings_resolved')}"
                                   f"/{meta.get('briefings')} inherited from a report",
            "note": "Reported separately on purpose. A federal audit that names "
                    "its own departments and a briefing package that inherits "
                    "them are different evidence at different strength, and most "
                    "of this corpus correctly has no federal department at all.",
        },
        "returned": len(results),
        "filters": {"department": dept, "min_score": min_score,
                    "doc_type": doc_type, "since": since,
                    "vendor": vendor, "direct_only": getattr(args, "direct_only", False)},
        "audits": results,
        "how_to_read": "Ranked by it_score — how strongly the audit reads as "
                       "IT/systems/digital (vs financial/environmental/benefits), "
                       "scored semantically. doc_type distinguishes a performance_"
                       "audit (the AG's finding) from a committee_hearing (the "
                       "scrutiny materialized before PACP/OGGO). The pre-RFP power "
                       "is CONVERGENCE: take the department, then check program-"
                       "signals (are they planning to modernize the same thing?) "
                       "and expiring-contracts (who holds it, expiring when?). When "
                       "OAG + plans + contract all align on one department, that's "
                       "your strongest lead — and any live tender from them should "
                       "rank higher. Read the report_url for citable specifics. "
                       "A lead list, not a forecast.",
    }


def cmd_resolve_department(args) -> dict:
    """
    What does this string mean, as a department identifier?

    Exists so a caller can check an identifier WITHOUT running a query and
    guessing from an empty result whether the department has no signal or the
    name was simply wrong. Those are very different answers and every other tool
    would otherwise conflate them.

    Resolution is registry-only and exact after normalization — the same rule
    all four signal tools use, including the `not:` exclusions, so
    "Immigration and Refugee Board" resolves to the tribunal and never to IRCC.
    """
    resolver = org_resolve.default_resolver()
    try:
        key = resolver.resolve(args.name)
    except org_resolve.AmbiguousOrganization as exc:
        return {"query": args.name, "resolved": None, "error": str(exc)}

    if not key:
        return {
            "query": args.name,
            "resolved": None,
            "closest": resolver.suggest(args.name),
            "note": "Not a known organization. Pass a canonical key from "
                    "vault/crosswalk/org_aliases.yaml or an organization's "
                    "registered name; fragments and substrings are refused on "
                    "purpose, because that is how one department's name lands "
                    "on another.",
        }

    entry = resolver.aliases[key]
    scope = org_resolve.department_scope(key)
    return {
        "query": args.name,
        "resolved": key,
        "name": resolver.display_name(key),
        "also_known_as": entry.get("observed_names") or [],
        "never_matches": entry.get("not") or [],
        "sources": {
            "oag": "audit_departments.dept_key",
            "plans": scope["plan_ids"] or "files no departmental plans",
            "contracts": scope["contract_slugs"] or "publishes no contracts",
        },
        "note": entry.get("note"),
    }
