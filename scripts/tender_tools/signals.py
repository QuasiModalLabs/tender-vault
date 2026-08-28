"""
The pre-RFP signals: departmental plans, OAG audits, lobbying communications
and registrations, and the registry lookup.

Neither signal predicts a procurement. They surface departments under
operational or independent-scrutiny pressure, and the reader does the judging —
which is why nothing here returns a combined score.
"""
from __future__ import annotations

import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import org_resolve
from contracts_ingest import normalize_vendor

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


def _lobbying_window(meta: dict) -> dict:
    """
    What the database covers, so a zero can be read correctly.

    TWO CURRENCIES, NOT ONE, and the difference is load-bearing. `latest` is the
    last communication in the database. `subject_coverage_latest` is the last one
    that carries a subject, and it is the real end date of every subject-derived
    number the callers report - the Government Procurement counts, subjects_filed,
    and any --subject filter.

    They were nearly two years apart once. The Registry moved subjects into a
    second export at 2024-09-30, the ingest read only the first, and 65% of the
    windowed database had no subject while this block went on reporting full
    currency. Nothing raised, because nothing compared the two dates.

    So the comparison happens HERE, once, inside the block all three callers
    already emit - rather than in each caller, which is the arrangement that
    failed. `subject_coverage_state` is the field to read:

      current   subjects run to the last communication
      lagging   they stop earlier. `subject_coverage_latest` is the true end of
                every subject-derived count, and the shortfall is given in days
      unknown   the database predates the stamp. NOT a synonym for `current`:
                an absent stamp is a question nobody answered, and reading
                absence as agreement is the original bug in miniature

    The warning also goes into `note`, which is the one field every consumer
    already prints, so a reader who checks nothing else still cannot miss it.
    """
    latest = str(meta.get("latest_communication") or "")
    subject_latest = str(meta.get("subject_coverage_latest") or "")

    def _as_date(value: str):
        """The stamp as a date, or None when it is absent or unreadable."""
        try:
            return datetime.strptime(value, "%Y-%m-%d").date()
        except (ValueError, TypeError):
            return None

    subject_on = _as_date(subject_latest)
    latest_on = _as_date(latest)

    shortfall = None
    if subject_on is None:
        # Absent AND unreadable both land here, deliberately. Comparing the raw
        # strings instead would be shorter and wrong: 'not-a-date' sorts above
        # '2026-08-21', so a corrupt stamp would read as `current` - a silent
        # pass, which is the failure mode this whole block exists to prevent.
        # Unreadable is a question nobody can answer, so it fails to `unknown`.
        state = "unknown"
    elif latest_on is not None and subject_on < latest_on:
        state = "lagging"
        shortfall = (latest_on - subject_on).days
    else:
        state = "current"

    note = ("The database is windowed at ingest. A department with no rows "
            "may simply have had no reportable meetings inside the window.")
    if state == "lagging":
        gap = f", {shortfall:,} days earlier" if shortfall is not None else ""
        note += (
            f" SUBJECT COVERAGE STOPS AT {subject_latest}{gap} than the last "
            f"communication ({latest}). Every subject-derived count reported "
            f"alongside this window - procurement counts, subjects_filed, any "
            f"--subject filter - describes the period ending {subject_latest}, "
            f"NOT the window above. Re-run the lobbying ingest before quoting "
            f"any of them.")
    elif state == "unknown":
        note += (
            " Subject coverage is UNSTAMPED: this database was built before the "
            "stamp existed, so how far subjects actually run is unknown and no "
            "subject-derived count here can be dated. Re-run the lobbying "
            "ingest. Unknown is not the same as current.")

    window = {
        "communications_in_db": meta.get("communications"),
        "communications_published": meta.get("communications_published"),
        "window_years": meta.get("window_years") or "all",
        "earliest": meta.get("earliest_communication"),
        "latest": latest,
        # Always present, all three states, including `current`. An absent key
        # would make "subjects are up to date" indistinguishable from "nobody
        # measured", which is the distinction this whole block exists to keep.
        "subject_coverage_latest": subject_latest or None,
        "subject_coverage_state": state,
        "note": note,
    }
    if shortfall is not None:
        window["subject_coverage_shortfall_days"] = shortfall
    return window


def _lobbying_not_built() -> dict:
    """
    The "no database" response, written to be ACTED ON rather than relayed.

    This is the one signal layer whose source cannot be fetched by anything in
    this project, so the error a reader gets has to carry the whole remedy: the
    two official URLs, where the file goes, and the command. An error that only
    says "not built" gets repeated to the user verbatim and leaves them to go
    find all three.

    `state` is deliberately not "no data". A caller that cannot distinguish an
    unbuilt layer from an empty one will eventually report that no lobbying
    happened, which is false — the registry is published and updated weekly.
    """
    return {
        "error": "Lobbying DB not built.",
        "state": "source_archive_not_acquired",
        "why": "lobbycanada.gc.ca returns 403 to automated clients "
               "(Cloudflare challenge), so the archive is downloaded by hand. "
               "This is NOT an absence of data — the registry is published, "
               "current and updated weekly.",
        "download": {
            "communications (required)":
                "https://lobbycanada.gc.ca/media/mqbbmaqk/"
                "communications_ocl_cal.zip",
            "registrations (optional, not yet ingested)":
                "https://lobbycanada.gc.ca/media/zwcjycef/"
                "registrations_enregistrements_ocl_cal.zip",
        },
        "save_to": "data/source/lobbying/communications_ocl_cal.zip",
        "then_run": "python scripts/lobbying_ingest.py",
        "tell_the_user": (
            "Give them the communications URL and the save path, say in one "
            "line that Cloudflare blocks automated download, and offer to run "
            "the ingest once the file is in place. Do not report this as 'no "
            "lobbying data available'."
        ),
    }


def _produced_by(command: str, db: Path, meta: dict) -> dict:
    """
    Name the path that produced a number, so a reader can tell a tool answer
    from something typed by hand.

    Every figure in a briefing is supposed to be reproducible by re-running one
    command. That guarantee is invisible in the output, which is how it gets
    broken: when this command was too slow to finish, its numbers were replaced
    with equivalent hand-written SQL against the same database and nothing on
    the page recorded the difference. The figures happened to be right; nobody
    reading the file could have known either way, and the next substitution
    might not be.

    So the answer carries its own attribution. A number quoted with a
    `produced_by` is one somebody else can re-derive; a number without one has
    no standing, and the briefing skill treats it as unciteable.
    """
    return {
        "command": command,
        "database": str(db.relative_to(paths.PROJECT_ROOT)),
        "source_sha256": (meta.get("source_sha256") or "")[:12],
        "ingest_date": meta.get("ingest_date"),
        "rule": (
            "Quote this beside any figure taken from this result. If this "
            "command cannot complete, the briefing SAYS SO and the section "
            "goes without the number — it is never backfilled from an ad-hoc "
            "query, however obviously correct that query looks."
        ),
    }


def _lobbying_rows(con, where: list, params: list, limit: int) -> tuple[list, dict, dict]:
    """
    Fetch matching communications and their two child tables in three queries.

    Per-communication follow-ups would be a query per row; the office holders
    and subjects come back in one pass each, keyed by comlog_id.
    """
    rows = con.execute(f"""
        SELECT c.comlog_id, c.comm_date, c.client_name, c.client_norm,
               c.reg_type_label, c.registrant_first, c.registrant_last,
               c.posted_date, c.client_num
        FROM communications c
        WHERE {' AND '.join(where)}
        ORDER BY c.comm_date DESC
        LIMIT ?
    """, params + [limit]).fetchall()
    if not rows:
        return [], {}, {}

    marks = ",".join("?" * len(rows))
    ids = [r[0] for r in rows]
    dpohs: dict[int, list] = {}
    for comlog, last, first, title, institution, kind, key in con.execute(
        f"SELECT comlog_id, dpoh_last, dpoh_first, dpoh_title, institution, "
        f"institution_kind, dept_key FROM communication_dpohs "
        f"WHERE comlog_id IN ({marks}) ORDER BY institution, dpoh_last", ids
    ):
        entry = {"name": " ".join(x for x in (first, last) if x),
                 "title": title, "institution": institution, "kind": kind}
        if key:
            entry["department"] = key
        dpohs.setdefault(comlog, []).append(entry)

    subjects: dict[int, list] = {}
    for comlog, subject, other in con.execute(
        f"SELECT comlog_id, subject, other_subject FROM communication_subjects "
        f"WHERE comlog_id IN ({marks}) ORDER BY subject", ids
    ):
        subjects.setdefault(comlog, []).append(
            f"{subject}: {other}" if other else subject)
    return rows, dpohs, subjects


def _subject_coverage(con, matched_where: list, matched_params: list) -> dict:
    """
    The date range a subject-filtered answer ACTUALLY describes, read off the
    data rather than off the window parameter.

    `window.latest` is the newest communication in the database. It is not
    necessarily the newest one that carries a subject code, and when those two
    dates diverge every --subject filter answers about the coded period while
    appearing to answer about the window.

    That happened. On the archive acquired 2026-08-24 the two were twenty-three
    months apart — subjects stopped at 2024-09-30 while communications ran to
    2026-08-21, leaving 72,341 rows (65% of the windowed database) invisible to
    any subject filter, and a briefing quoted the resulting counts as current.
    The cause was an ingest that read Communication_SubjectMattersExport.csv and
    not Communication_SubjectMatterDetailsExport.csv, which carries the same
    vocabulary for every communication after that date; lobbying_ingest.py now
    reads both, and a healthy build reports `state: complete`.

    This block stays regardless, because the fix and the guard answer different
    questions. The fix made today's data whole. The guard is what will say so
    the next time the Office changes the export and nobody notices for a month.

    That is the same failure the corpus provenance block exists to prevent: a
    number that is correct about its rows and wrong about its period, presented
    with a date that belongs to something else. So this is derived the same way
    — measured from the rows, reported next to the parameter it contradicts,
    and carrying a `state` a caller can branch on rather than prose it has to
    parse.

    `coded_*` describes the subject vocabulary across the whole database.
    `matched_*` describes the specific result being returned. They are separate
    because a department can be quiet inside a period that is fully coded, and
    that is a real finding, where a period that is not coded at all is not.
    """
    coded_earliest, coded_latest, coded_n = con.execute(
        "SELECT MIN(c.comm_date), MAX(c.comm_date), COUNT(DISTINCT c.comlog_id) "
        "FROM communications c JOIN communication_subjects s "
        "ON s.comlog_id = c.comlog_id").fetchone()
    db_earliest, db_latest, db_n = con.execute(
        "SELECT MIN(comm_date), MAX(comm_date), COUNT(*) FROM communications"
    ).fetchone()
    matched_earliest, matched_latest = con.execute(
        f"SELECT MIN(c.comm_date), MAX(c.comm_date) FROM communications c "
        f"WHERE {' AND '.join(matched_where)}", matched_params).fetchone()

    uncoded = (db_n or 0) - (coded_n or 0)
    truncated = bool(coded_latest and db_latest and coded_latest < db_latest)

    cov = {
        "effective_earliest": coded_earliest,
        "effective_latest": coded_latest,
        "matched_earliest": matched_earliest,
        "matched_latest": matched_latest,
        "communications_with_subject": coded_n,
        "communications_without_subject": uncoded,
        "state": "truncated" if truncated else "complete",
    }
    if truncated:
        pct = round(100 * uncoded / db_n) if db_n else 0
        cov["reading"] = (
            f"SUBJECT CODES STOP AT {coded_latest}. This result describes "
            f"{coded_earliest} to {coded_latest}, NOT the window's "
            f"{db_earliest} to {db_latest}. The archive carries no subject row "
            f"for any communication after {coded_latest}, so {uncoded:,} "
            f"communications ({pct}% of the database) are invisible to any "
            f"--subject filter. A count from this result is a count for the "
            f"coded period only — quote that period beside it, and never read "
            f"it as current. This is missing CODING, not missing activity. "
            f"Check first whether the ingest is reading every subject member of "
            f"the archive (this exact symptom was once "
            f"Communication_SubjectMatterDetailsExport.csv going unread, not a "
            f"defect at the Office); run without --subject meanwhile, to see "
            f"whether the department is still being met."
        )
    else:
        cov["reading"] = (
            f"Subject coding reaches {coded_latest}, the newest communication "
            f"in the database, so this result describes the full window."
        )
    return cov


def _registrations_as_of_clause() -> str:
    """The one as-of predicate, imported from the ingest so there is a single
    definition of the half-open interval rather than a copy that drifts."""
    # TWO parents, not one. This module sits a level deeper than the
    # scripts/tender_tools.py it came out of, where one parent reached scripts/.
    # One parent from here is scripts/tender_tools/, where registrations_ingest
    # does not live, and the import fails at call time rather than at import.
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from registrations_ingest import as_of_clause
    return as_of_clause("v")


def cmd_lobbying_signals(args) -> dict:
    """
    Who has been in the room with a department, about what, and when — the
    earliest signal in the corpus.

    Where oag-signals says what an independent authority found a department
    failing at and program-signals says what it plans, this says who has been
    talking to it while the requirement was still being decided. A monthly
    communication report is filed under the Lobbying Act when a lobbyist has an
    arranged oral communication with a designated public office holder, and
    "Government Procurement" is one of the 54 subject matters they file under.

    THIS IS EVIDENCE OF PRESENCE, NOT OF INFLUENCE, and the distinction is not
    a disclaimer — it governs how the output may be used. Filing is what
    COMPLIANCE looks like: the firms here are the ones following the rules, and
    a meeting is not a finding about anybody. What the record supports is that a
    department has been hearing from a particular set of firms on a particular
    subject, with a citable date. Never write it up as anything stronger.

    Read it two ways. Down the client column it is competitive intelligence —
    who is cultivating the department you are about to bid into, and for how
    long. Down the department column it is a pre-RFP signal: a department taking
    procurement meetings on a capability, whose plan says it intends to
    modernize that capability and whose incumbent contract expires next year, is
    the convergence case the dossier exists to show.

    Filters:
      --department KEY  : meetings with that department's office holders
      --subject STR     : one of the filed subject matters (see --list-subjects)
      --client SUBSTR   : client organization name contains this
      --vendor NAME     : client matched the way contracts.db matches vendors,
                          so a contract holder and a lobbying client compare
                          equal without the caller normalizing by hand
      --since YYYY-MM-DD: only communications on or after this date
      --limit N         : how many to return (default 25)
    """
    import sqlite3

    db = paths.PROJECT_ROOT / "data" / "lobbying.db"
    if not db.exists():
        return _lobbying_not_built()

    con = sqlite3.connect(db)
    meta = dict(con.execute("SELECT key, value FROM meta").fetchall())

    if getattr(args, "list_subjects", False):
        subjects = [
            {"subject": s, "communications": n}
            for s, n in con.execute(
                "SELECT subject, COUNT(DISTINCT comlog_id) c "
                "FROM communication_subjects GROUP BY 1 ORDER BY c DESC")
        ]
        con.close()
        return {"as_of": meta.get("ingest_date"), "subjects": subjects,
                "note": "The filed subject matter is a controlled list chosen "
                        "by the filer. 'Government Procurement' is the direct "
                        "signal; 'Industry', 'Science and Technology' and "
                        "'Telecommunications' carry IT requirements too."}

    where, params = ["1=1"], []
    dept = org_resolve.resolve_department_arg(getattr(args, "department", None))
    if dept:
        # A canonical key, joined on — never a substring of the published
        # institution name. dept_key is set only where naming a department is a
        # true statement, so this filter cannot pick up the 31k parliamentary
        # rows or a Crown corporation that happens to share a word.
        where.append("EXISTS (SELECT 1 FROM communication_dpohs d "
                     "WHERE d.comlog_id = c.comlog_id AND d.dept_key = ?)")
        params.append(dept)
    subject = getattr(args, "subject", None)
    if subject:
        where.append("EXISTS (SELECT 1 FROM communication_subjects s "
                     "WHERE s.comlog_id = c.comlog_id AND lower(s.subject) = ?)")
        params.append(subject.lower())
    client = getattr(args, "client", None)
    if client:
        where.append("lower(c.client_name) LIKE ?")
        params.append(f"%{client.lower()}%")
    vendor = getattr(args, "vendor", None)
    if vendor:
        # The contracts-side normalizer, applied to the query rather than
        # re-implemented, so "HP Canada Co." and "HP CANADA LTD" reach the same
        # client_norm the ingest stored.
        where.append("c.client_norm = ?")
        params.append(normalize_vendor(vendor))
    since = getattr(args, "since", None)
    if since:
        where.append("c.comm_date >= ?")
        params.append(since)

    limit = getattr(args, "limit", None) or 25
    rows, dpohs, subjects = _lobbying_rows(con, where, params, limit)

    # Counted over everything that MATCHED, not over the page returned: "who
    # turns up most" is the question this data answers best, and answering it
    # from 25 rows would just describe the sort order.
    totals = con.execute(
        f"SELECT COUNT(*) FROM communications c WHERE {' AND '.join(where)}",
        params).fetchone()[0]
    top_clients = [
        {"client": name, "communications": n}
        for name, n in con.execute(
            f"SELECT c.client_name, COUNT(*) n FROM communications c "
            f"WHERE {' AND '.join(where)} AND c.client_name != '' "
            f"GROUP BY c.client_norm ORDER BY n DESC LIMIT 15", params)
    ]
    top_departments = [
        {"department": key, "communications": n}
        for key, n in con.execute(
            f"SELECT d.dept_key, COUNT(DISTINCT c.comlog_id) n "
            f"FROM communications c JOIN communication_dpohs d "
            f"ON d.comlog_id = c.comlog_id "
            f"WHERE {' AND '.join(where)} AND d.dept_key IS NOT NULL "
            f"GROUP BY 1 ORDER BY n DESC LIMIT 15", params)
    ]
    # Derived before the connection closes, and attached to every subject-filtered
    # answer including the empty one — an empty result is exactly where a missing
    # period is most likely to be misread as a quiet department.
    coverage = _subject_coverage(con, where, params) if subject else None
    con.close()

    if not rows:
        empty = {"as_of": meta.get("ingest_date"), "communications": 0,
                 "produced_by": _produced_by("lobbying-signals", db, meta),
                 "window": _lobbying_window(meta),
                 "note": "Nothing matched. Check --subject against "
                         "--list-subjects, and remember the database holds only "
                         f"the window described above ({meta.get('window_cutoff')} "
                         "onward) — an older meeting is not absent, it is out of "
                         "scope."}
        if coverage:
            empty["subject_coverage"] = coverage
        return empty

    results = []
    for (comlog, date, client_name, _norm, reg_type,
         reg_first, reg_last, posted, client_num) in rows:
        results.append({
            "date": date,
            "client": client_name,
            "lobbyist": " ".join(x for x in (reg_first, reg_last) if x),
            "registration": reg_type,
            "subjects": subjects.get(comlog, []),
            "office_holders": dpohs.get(comlog, []),
            "posted": posted,
            # The registry's own composite identifier, not a URL. The published
            # data dictionary defines the communication number as the client
            # number followed by the comlog id, and that is the string the
            # Registry of Lobbyists search takes. No deep link is offered
            # because the registry has none: its report views are reached
            # through a session-scoped search form, so any per-communication
            # URL built here would be invented rather than cited.
            "communication_number": f"{client_num}-{comlog}"
            if client_num is not None else str(comlog),
        })

    out = {
        "as_of": meta.get("ingest_date"),
        "produced_by": _produced_by("lobbying-signals", db, meta),
        "window": _lobbying_window(meta),
        "matched": totals,
        "returned": len(results),
        "filters": {"department": dept, "subject": subject, "client": client,
                    "vendor": vendor, "since": since},
        "top_clients": top_clients,
        "top_departments": top_departments,
        "communications": results,
        "how_to_read": (
            "Each row is one disclosed meeting: who met which office holders, "
            "on what date, filed under which subject matters. This is evidence "
            "of PRESENCE and nothing more — filing is what compliance with the "
            "Lobbying Act looks like, and no row here is a finding about "
            "anyone. Do not write it up as influence over a procurement. "
            "top_clients answers the question the page cannot: who turns up "
            "most across everything that matched. The registration type "
            "matters — consultant means a hired lobbyist and the client is who "
            "paid, in_house means the organization's own staff. Coverage is "
            "partial by law: only ARRANGED ORAL communications with designated "
            "office holders are reportable, so absence is not evidence that "
            "nobody was in the room. communication_number is the registry's "
            "own identifier, for looking a record up at "
            "lobbycanada.gc.ca — cite it rather than a URL. "
            "The pre-RFP read is convergence — take a "
            "department that is taking procurement meetings and check "
            "program-signals and expiring-contracts for the same capability."
        ),
    }
    if coverage:
        # Placed after `window` in reading order but asserted here so it cannot
        # be dropped by an edit to the literal above: a subject-filtered result
        # without its effective range is the defect this block exists to close.
        out["subject_coverage"] = coverage
        if coverage["state"] == "truncated":
            out["window"] = dict(out["window"])
            out["window"]["note"] = (
                "SUPERSEDED FOR THIS RESULT by subject_coverage — `latest` "
                f"below is the newest communication in the database "
                f"({coverage['effective_latest']} is the newest one carrying a "
                "subject code). A --subject filter cannot see past that date. "
                + out["window"].get("note", "")
            )
    return out


def cmd_registrations_signals(args) -> dict:
    """
    Who was REGISTERED to lobby a department, as of a given date.

    The standing declaration behind the meetings. `lobbying-signals` says a
    communication happened; this says who was on the record as working that
    department at a point in time, and which registration version says so.

    AS-OF IS MANDATORY AND HAS NO DEFAULT. Not an oversight — the whole reason
    this database stores versions is that 53% of amended registrations change
    their institution list, and a default meaning "latest" is how flattened
    behaviour returns through a different door. A caller who never thinks about
    time would silently get the present tense and build a time-ordered claim on
    it. Wanting current state is fine; saying so is the requirement. Pass
    --as-of today for that.

    EVERY ROW CARRIES ITS VERSION so a claim can be traced back to the exact
    registration version it rests on: `reg_id` is the version id, `reg_num`
    ends in the version sequence, and `effective`/`ends` are the window that
    made it match. A briefing line citing this data can name the version.

    A null `ends` means still in force, measured rather than assumed — every
    open-ended version in the archive is the latest-effective of its own chain.

    Filters:
      --as-of YYYY-MM-DD : REQUIRED. 'today' is accepted as an explicit request
                           for current state.
      --department KEY   : registrations naming that department
      --client SUBSTR    : client organization name contains this
      --vendor NAME      : client matched the way contracts.db matches vendors
      --limit N          : how many to return (default 25)
    """
    import sqlite3

    # The as-of contract is checked BEFORE the database, deliberately. A caller
    # who omitted it has made a malformed request, and telling them the
    # database is missing sends them to install something instead of fixing
    # their query — and worse, hides the requirement until the day the data
    # exists, which is the day a defaulted answer would start being believed.
    as_of = getattr(args, "as_of", None)
    if not as_of:
        return {
            "error": "--as-of is required and has no default.",
            "why": "This database stores every registration version because "
                   "53% of amended registrations change which departments they "
                   "name. A default meaning 'latest' would quietly answer a "
                   "time-ordered question with present-tense data.",
            "how": "Pass --as-of YYYY-MM-DD for a point in time, or "
                   "--as-of today if you genuinely want current state.",
        }
    if str(as_of).lower() in ("today", "now", "current"):
        as_of = datetime.now().strftime("%Y-%m-%d")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(as_of)):
        return {"error": f"--as-of must be YYYY-MM-DD or 'today', got {as_of!r}"}

    db = paths.PROJECT_ROOT / "data" / "registrations.db"
    if not db.exists():
        return {
            "error": "Registrations DB not built.",
            "state": "source_archive_not_acquired",
            "why": "lobbycanada.gc.ca returns 403 to automated clients "
                   "(Cloudflare challenge), so the archive is downloaded by "
                   "hand. This is NOT an absence of data.",
            "download": "https://lobbycanada.gc.ca/media/zwcjycef/"
                        "registrations_enregistrements_ocl_cal.zip",
            "save_to": "data/source/lobbying/"
                       "registrations_enregistrements_ocl_cal.zip",
            "then_run": "python scripts/registrations_ingest.py",
        }

    con = sqlite3.connect(db)
    meta = dict(con.execute("SELECT key, value FROM meta").fetchall())

    where = ["1=1"]
    params: dict = {"as_of": as_of}
    dept = org_resolve.resolve_department_arg(getattr(args, "department", None))
    if dept:
        where.append("EXISTS (SELECT 1 FROM registration_institutions i "
                     "WHERE i.reg_id = v.reg_id AND i.dept_key = :dept)")
        params["dept"] = dept
    client = getattr(args, "client", None)
    if client:
        where.append("lower(v.client_name) LIKE :client")
        params["client"] = f"%{client.lower()}%"
    vendor = getattr(args, "vendor", None)
    if vendor:
        where.append("v.client_norm = :vendor")
        params["vendor"] = normalize_vendor(vendor)

    limit = getattr(args, "limit", None) or 25
    params["limit"] = limit
    sql_where = " AND ".join(where) + " AND " + _registrations_as_of_clause()
    rows = con.execute(f"""
        SELECT v.reg_id, v.reg_num, v.version_seq, v.effective_date, v.end_date,
               v.client_name, v.reg_type_label, v.registrant_first,
               v.registrant_last, v.firm_name
        FROM registration_versions v
        WHERE {sql_where}
        ORDER BY v.effective_date DESC
        LIMIT :limit
    """, params).fetchall()

    total = con.execute(
        f"SELECT COUNT(*) FROM registration_versions v WHERE {sql_where}",
        params).fetchone()[0]

    departments = [
        {"department": key, "registrations": n}
        for key, n in con.execute(f"""
            SELECT i.dept_key, COUNT(DISTINCT v.reg_base) n
            FROM registration_versions v
            JOIN registration_institutions i ON i.reg_id = v.reg_id
            WHERE {sql_where} AND i.dept_key IS NOT NULL
            GROUP BY 1 ORDER BY n DESC LIMIT 15
        """, params)
    ]

    results = []
    for (reg_id, reg_num, seq, eff, end, client_name, reg_type,
         r_first, r_last, firm) in rows:
        insts = [
            r[0] for r in con.execute(
                "SELECT DISTINCT dept_key FROM registration_institutions "
                "WHERE reg_id = ? AND dept_key IS NOT NULL ORDER BY 1", (reg_id,))
        ]
        results.append({
            "client": client_name,
            "registration": reg_num,
            # The version this claim rests on. Anything time-ordered built from
            # this row must cite it — that is what makes the claim checkable.
            "version_id": reg_id,
            "version_seq": seq,
            "effective": eff,
            "ends": end,
            "still_in_force": end is None,
            "registration_type": reg_type,
            "registrant": " ".join(x for x in (r_first, r_last) if x),
            "firm": firm,
            "departments": insts,
        })
    con.close()

    return {
        "as_of": as_of,
        "ingest_date": meta.get("ingest_date"),
        "source_sha256": meta.get("source_sha256"),
        "matched_versions": total,
        "returned": len(results),
        "filters": {"department": dept, "client": client, "vendor": vendor},
        "departments": departments,
        "registrations": results,
        "corpus": {
            "versions": meta.get("versions"),
            "registrations": meta.get("registrations"),
            "open_ended_versions": meta.get("open_ended_versions"),
            "chain_breaks_midchain": meta.get("chain_breaks_midchain"),
        },
        "how_to_read": (
            f"Every row was in force on {as_of} — that date selected them, and "
            "a different date returns a different answer, which is the point. "
            "version_id is the registration version the row rests on; cite it "
            "for any claim about a point in time. A null `ends` means still in "
            "force. This is a DECLARATION of intent to lobby, not evidence any "
            "meeting occurred — pair it with lobbying-signals for that — and "
            "like all of this data it is presence, never influence."
        ),
    }
