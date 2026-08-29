"""
The proactive-disclosure contracts DB: who won what, and what expires when.

Both commands read data/contracts.db, built by scripts/contracts_ingest.py. The
database is optional — it comes from a ~630MB download — so every entry point
here has to answer usefully when it is absent.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

import org_resolve

from . import paths
from .company_profile import _profile_expiry_min_value

def cmd_contracts_intel(args) -> dict:
    """
    Competitive intelligence from the proactive-disclosure contracts DB.

    Pure SQLite. Deliberately does NOT touch ChromaDB, so it responds in
    milliseconds regardless of embedding-model state. Aggregates per contract
    family (procurement id) using each family's highest recorded value, which
    approximates 'current value including amendments'.
    """
    import sqlite3

    db = paths.PROJECT_ROOT / "data" / "contracts.db"
    if not db.exists():
        return {"error": "Contracts DB not built. Run: python scripts/contracts_ingest.py"}

    con = sqlite3.connect(db)
    meta = dict(con.execute("SELECT key, value FROM meta").fetchall())
    like = f"%{args.query.lower()}%"

    # Same department identifier as every other signal tool. The contracts file
    # stores the CKAN slug, not a canonical key, so the key is translated
    # through the crosswalk and matched on the slug — an ID join, never a
    # comparison of display names.
    dept = org_resolve.resolve_department_arg(getattr(args, "department", None))
    where = ["(lower(description) LIKE ? OR lower(matched_terms) LIKE ?)"]
    params: list = [like, like]
    slugs = org_resolve.department_scope(dept)["contract_slugs"] if dept else []
    if dept:
        if not slugs:
            return {"query": args.query, "department": dept, "families": 0,
                    "note": f"{dept!r} discloses no contracts under a name of "
                            "its own, so this source cannot answer for it. That "
                            "is a real fact about the body, not a broken build."}
        where.append(f"owner_org IN ({','.join('?' * len(slugs))})")
        params += slugs

    families = con.execute(f"""
        SELECT family_id, vendor, vendor_norm, org, MAX(value) AS v,
               MAX(contract_date) AS d, MAX(period_end) AS pe, description
        FROM contracts
        WHERE {' AND '.join(where)}
        GROUP BY family_id
    """, params).fetchall()
    con.close()

    if not families:
        return {
            "query": args.query,
            "department": dept,
            "as_of": meta.get("ingest_date"),
            "families": 0,
            "note": "No matches. Try a broader term; matching is substring over descriptions.",
        }

    values = sorted(f[4] for f in families if f[4] and f[4] > 0)
    vendor_totals: dict[str, float] = {}
    org_counts: dict[str, int] = {}
    for _fam, _vendor, vnorm, org, v, *_rest in families:
        if vnorm:
            vendor_totals[vnorm] = vendor_totals.get(vnorm, 0) + (v or 0)
        if org:
            org_counts[org] = org_counts.get(org, 0) + 1

    recent = sorted(families, key=lambda f: f[5] or "", reverse=True)[:5]
    return {
        "query": args.query,
        "department": dept,
        "as_of": meta.get("ingest_date"),
        "window_years": meta.get("window_years"),
        "families": len(families),
        "total_value": round(sum(values), 0),
        "median_value": values[len(values) // 2] if values else 0,
        "top_vendors": [
            {"vendor": v, "total_value": round(t, 0)}
            for v, t in sorted(vendor_totals.items(), key=lambda x: -x[1])[:8]
        ],
        "top_departments": [
            {"org": o, "contract_families": n}
            for o, n in sorted(org_counts.items(), key=lambda x: -x[1])[:8]
        ],
        "recent_examples": [
            {"vendor": f[1], "org": f[3], "value": f[4],
             "contract_date": f[5], "period_end": f[6],
             "description": (f[7] or "")[:180]}
            for f in recent
        ],
        "caveats": "Unaudited data; vendor names lightly normalized (suffix/"
                   "punctuation only, not fuzzy-matched); families partially "
                   "outside the filter window may be incomplete.",
    }

def cmd_expiring_contracts(args) -> dict:
    """
    Surface contracts whose delivery period ends inside a window — the highest
    public signal for a future re-procurement.

    The thesis: a contract in our competency space that expires in 6-24 months
    is a near-certain future RFP. We already know the incumbent, the value, the
    department, and (from the description) roughly what the work is — 12-18
    months before the government formally asks for it. That head start is where
    proactive business development actually happens.

    This is deliberately NOT prediction. It's a lead list. It surfaces
    candidates; a human (with Claude's help via the opportunity-shaping lens)
    decides which are worth a proactive conversation.

    Window comes from --months-min / --months-max (default 6-24). Ordered by
    expiry soonest-first within the window, then by value, so the most urgent
    and most consequential surface at the top.

    Notes on signal quality, stated honestly:
    - Aggregates per family; a family's latest period_end is its true expiry
      (amendments extend contracts, so we take the MAX).
    - Standing offers / supply arrangements often have far-future or open end
      dates and are re-competed differently than project contracts; they'll
      appear but are weaker signals — the description usually reveals which.
    - Value is directional (see contracts-intel caveats).
    """
    import sqlite3
    from datetime import datetime, timedelta

    db = paths.PROJECT_ROOT / "data" / "contracts.db"
    if not db.exists():
        return {"error": "Contracts DB not built. Run: python scripts/contracts_ingest.py"}

    months_min = getattr(args, "months_min", None) or 6
    months_max = getattr(args, "months_max", None) or 24
    today = datetime.now()
    lo = (today + timedelta(days=30 * months_min)).strftime("%Y-%m-%d")
    hi = (today + timedelta(days=30 * months_max)).strftime("%Y-%m-%d")

    # Minimum value: CLI flag overrides profile; profile default is
    # expiry_min_value in the frontmatter (0 = no floor). This keeps the
    # threshold tunable per company profile, like every other filter.
    min_value = getattr(args, "min_value", None)
    if min_value is None:
        min_value = _profile_expiry_min_value()

    con = sqlite3.connect(db)
    meta = dict(con.execute("SELECT key, value FROM meta").fetchall())

    # Same department identifier as the other three signal tools, translated to
    # the CKAN slug the contracts file actually stores.
    dept = org_resolve.resolve_department_arg(getattr(args, "department", None))
    where = ["period_end != ''"]
    params: list = []
    if dept:
        slugs = org_resolve.department_scope(dept)["contract_slugs"]
        if not slugs:
            con.close()
            return {"department": dept, "expiring": 0,
                    "note": f"{dept!r} discloses no contracts under a name of "
                            "its own, so this source cannot answer for it."}
        where.append(f"owner_org IN ({','.join('?' * len(slugs))})")
        params += slugs

    # Per family: latest expiry, incumbent, department, value, description.
    # Filter families whose MAX(period_end) lands in the window AND whose
    # value clears the floor.
    rows = con.execute(f"""
        SELECT family_id,
               MAX(period_end)   AS expiry,
               vendor,
               vendor_norm,
               org,
               MAX(value)        AS v,
               MAX(contract_date) AS awarded,
               description
        FROM contracts
        WHERE {' AND '.join(where)}
        GROUP BY family_id
        HAVING expiry >= ? AND expiry <= ? AND v >= ?
        ORDER BY expiry ASC, v DESC
    """, params + [lo, hi, min_value]).fetchall()
    con.close()

    if not rows:
        return {
            "window": f"{months_min}-{months_max} months out ({lo} to {hi})",
            "min_value": min_value,
            "department": dept,
            "as_of": meta.get("ingest_date"),
            "expiring": 0,
            "note": "No contracts expiring in this window above the value floor. "
                    "Lower expiry_min_value in the profile (or --min-value), or "
                    "widen --months-max.",
        }

    def months_until(expiry: str) -> int:
        try:
            d = datetime.strptime(expiry, "%Y-%m-%d")
            return round((d - today).days / 30)
        except ValueError:
            return -1

    results = [
        {
            "incumbent": r[2],
            "incumbent_norm": r[3],
            "department": r[4],
            "expiry": r[1],
            "months_until_expiry": months_until(r[1]),
            "value": r[5],
            "awarded": r[6],
            "description": (r[7] or "")[:220],
        }
        for r in rows
    ]

    return {
        "window": f"{months_min}-{months_max} months out ({lo} to {hi})",
        "min_value": min_value,
        "department": dept,
        "as_of": meta.get("ingest_date"),
        "expiring": len(results),
        "contracts": results[:40],  # cap so the agent isn't flooded
        "truncated": len(results) > 40,
        "how_to_read": "Each row is a near-certain future re-procurement. The "
                       "incumbent is who you'd displace; the description hints at "
                       "the work. Not every row is worth pursuing — this is a lead "
                       "list for human judgment, not a forecast.",
    }


def _contract_rows_by_slug(slugs: list) -> dict[str, int]:
    """
    Rows each CKAN slug contributes to this extract, zeros included.

    Zeros are the point. A slug that contributes nothing has to say so rather
    than be missing from a dict, because a fold whose contribution is invisible
    is a fold the reader cannot check — pptc is folded into ircc and contributes
    0 rows here, and 'absent from the mapping' and 'present but empty' are
    different facts about the same department.
    """
    import sqlite3
    counts = {s: 0 for s in slugs}
    db = paths.PROJECT_ROOT / "data" / "contracts.db"
    if not slugs or not db.exists():
        return counts
    con = sqlite3.connect(db)
    try:
        for slug, n in con.execute(
            f"SELECT owner_org, COUNT(*) FROM contracts "
            f"WHERE owner_org IN ({','.join('?' * len(slugs))}) GROUP BY owner_org",
            slugs
        ):
            counts[slug] = n
    except sqlite3.OperationalError:
        pass
    finally:
        con.close()
    return counts
