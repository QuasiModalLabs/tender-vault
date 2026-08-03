"""
Ingest Office of the Auditor General (OAG) performance-audit metadata and score
each audit for IT-relevance, so the departments an independent authority has
flagged become a pre-RFP signal.

Source: open.canada.ca CKAN Action API, organization 'oag-bvg'. Read-only GET,
no API key. Licence: Open Government Licence - Canada.

WHY THIS EXISTS — the third signal, and the strongest for credibility. Contracts
say WHAT a department bought; plans say what they INTEND to modernize; OAG says
what an independent watchdog has PUBLICLY found them failing at. For a pre-RFP
conversation "the AG flagged your processing backlog in 2023" is far more citable
than a department's own planning prose. And OAG findings are exactly the
"impending scrutiny" that, in the chronic-strain thesis, forces a department to
procure a fix.

CONVERGENCE IS THE POINT. This is built to JOIN, not stand alone. Each audit is
tagged with the department it examined (best-effort, from title/description), so
oag-signals can be cross-referenced against program-signals (plans) and
contracts-intel/expiring-contracts. The real signal is CONVERGENCE: when OAG,
plans, and an expiring contract all point at the same department+capability, that
is the strongest possible pre-RFP case — and it feeds back into ranking live
tenders from that department higher.

Scoring reuses the two-pole semantic theming proven in plans_ingest: score the
audit's title+description toward an IT/systems theme, away from a non-IT-audit
theme (financial statements, environmental, benefits-administration). Runs
locally at ingest, zero Claude tokens.

Usage:
    python scripts/oag_ingest.py
    python scripts/oag_ingest.py --show-extremes
    python scripts/oag_ingest.py --max-audits 500   # cap the API pull
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))
from ingest import REQUEST_HEADERS, parse_profile  # noqa: E402
# Reuse the proven scoring helpers from plans_ingest.
from plans_ingest import theme_vector, _strip_md, EMBED_MODEL  # noqa: E402

API = "https://open.canada.ca/data/api/3/action/package_search"
ORG = "oag-bvg"
PROJECT_ROOT = Path(__file__).parent.parent
DEFAULT_PROFILE = PROJECT_ROOT / "vault" / "profiles" / "my-company.md"
DB_PATH = PROJECT_ROOT / "data" / "oag.db"

# Only these look like actual performance/audit reports (not briefing packages,
# financial-statement audits, or committee hearing materials). We keep audits
# AND committee briefing packages, because the latter are the "scrutiny
# materialized" signal — but tag which is which.
def classify_doc(title: str, notes: str) -> str:
    t = (title or "").lower()
    if "briefing package" in t or "hearing before" in t:
        return "committee_hearing"      # scrutiny materialized (PACP/OGGO/etc.)
    if "reports of the auditor general" in t or "report of the commissioner" in t:
        return "performance_audit"
    if "special examination" in t:
        return "special_examination"
    if "financial audit" in t or "financial statements" in t:
        return "financial_audit"
    return "other"


# IT/systems theme vs non-IT-audit theme. Defined by examples (profile can
# override via oag_themes); the model generalizes to unlisted phrasings.
DEFAULT_IT_AUDIT = [
    "audit of the department's aging IT systems and technology modernization",
    "failures in a case management or processing system causing backlogs",
    "problems delivering a digital service or online application platform",
    "weaknesses in cyber security of government networks and systems",
    "delays and cost overruns in a major IT or software project",
    "modernizing legacy technology infrastructure to meet service demand",
]
DEFAULT_NON_IT_AUDIT = [
    "audit of financial statements and public accounts",
    "environmental and climate change protection programs",
    "administration of benefits, grants and transfer payments",
    "physical infrastructure such as bridges, buildings and procurement of goods",
]

# Best-effort department extraction. OAG audit titles are usually the TOPIC, not
# the department, so the department is more often in the notes. We match against
# a list of known federal orgs (reused loosely) plus a regex fallback.
KNOWN_DEPTS = [
    "Immigration, Refugees and Citizenship", "Correctional Service",
    "National Defence", "Shared Services Canada", "Canada Revenue Agency",
    "Employment and Social Development", "Global Affairs", "Public Services and Procurement",
    "Royal Canadian Mounted Police", "Indigenous Services", "Crown-Indigenous",
    "Fisheries and Oceans", "Transport Canada", "Health Canada",
    "Public Health Agency", "Veterans Affairs", "Natural Resources",
    "Environment and Climate Change", "Agriculture and Agri-Food",
    "Innovation, Science and Economic Development", "Statistics Canada",
    "Canada Border Services", "Treasury Board",
]


def extract_dept(title: str, notes: str) -> str:
    hay = f"{title}\n{notes}"
    for d in KNOWN_DEPTS:
        if d.lower() in hay.lower():
            return d
    # regex fallback: "audit of X at the Department of Y"
    m = re.search(r"(Department of [A-Z][A-Za-z ,]+?)(?:[\.,]| to | and its)", hay)
    if m:
        return m.group(1).strip()
    return ""


def fetch_oag(max_audits: int) -> list[dict]:
    """Page through the CKAN API for all oag-bvg datasets."""
    out, start, rows = [], 0, 100
    while len(out) < max_audits:
        params = {"q": f"organization:{ORG}", "rows": rows, "start": start}
        r = requests.get(API, params=params, headers=REQUEST_HEADERS, timeout=60)
        r.raise_for_status()
        result = r.json()["result"]
        batch = result["results"]
        if not batch:
            break
        out.extend(batch)
        total = result["count"]
        print(f"  fetched {len(out)}/{total}")
        start += rows
        if start >= total:
            break
        time.sleep(0.3)  # be polite to the API
    return out[:max_audits]


def year_from_title(title: str) -> int | None:
    m = re.search(r"\b(20\d{2})\b", title or "")
    return int(m.group(1)) if m else None


def build_db(records: list[dict], scores: list, source_note: str) -> int:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if DB_PATH.exists():
        DB_PATH.unlink()
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE audits (
            oag_id TEXT, year INTEGER, doc_type TEXT,
            title TEXT, description TEXT, department TEXT,
            it_score REAL, html_url TEXT, pdf_url TEXT
        )
    """)
    con.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    rows = []
    for pkg, score in zip(records, scores):
        title = pkg.get("title", "")
        notes = _strip_md(pkg.get("notes", "") or "")
        html_url = pdf_url = ""
        for res in pkg.get("resources", []):
            fmt = (res.get("format") or "").upper()
            if fmt == "HTML" and not html_url:
                html_url = res.get("url", "")
            elif fmt == "PDF" and not pdf_url:
                pdf_url = res.get("url", "")
        rows.append((
            pkg.get("id", ""),
            year_from_title(title),
            classify_doc(title, notes),
            title, notes,
            extract_dept(title, notes),
            score, html_url, pdf_url,
        ))
    con.executemany("INSERT INTO audits VALUES (?,?,?,?,?,?,?,?,?)", rows)
    con.execute("CREATE INDEX idx_dept ON audits(department)")
    con.execute("CREATE INDEX idx_itscore ON audits(it_score)")
    con.execute("CREATE INDEX idx_type ON audits(doc_type)")
    for k, v in [
        ("ingest_date", datetime.now().strftime("%Y-%m-%d")),
        ("source", source_note),
        ("audit_count", str(len(rows))),
        ("licence", "Open Government Licence - Canada"),
    ]:
        con.execute("INSERT INTO meta VALUES (?, ?)", (k, v))
    con.commit()
    con.close()
    return len(rows)


def show_extremes(n: int = 10) -> None:
    con = sqlite3.connect(DB_PATH)
    print("\n=== HIGHEST it_score (should read as IT/systems/digital audits) ===")
    for title, sc, dept, dt in con.execute("""
        SELECT title, it_score, department, doc_type FROM audits
        ORDER BY it_score DESC LIMIT ?""", (n,)):
        print(f"[{sc:+.3f}] ({dt}) {dept or '?'}\n    {title[:150]}\n")
    print("=== LOWEST it_score (should read as financial/environmental/benefits) ===")
    for title, sc, dept, dt in con.execute("""
        SELECT title, it_score, department, doc_type FROM audits
        ORDER BY it_score ASC LIMIT ?""", (n,)):
        print(f"[{sc:+.3f}] ({dt}) {dept or '?'}\n    {title[:150]}\n")
    con.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    ap.add_argument("--max-audits", type=int, default=400)
    ap.add_argument("--show-extremes", action="store_true")
    args = ap.parse_args()

    criteria = parse_profile(args.profile)
    themes = criteria.get("oag_themes") or {}
    it_ex = themes.get("it_audit") or DEFAULT_IT_AUDIT
    nonit_ex = themes.get("non_it_audit") or DEFAULT_NON_IT_AUDIT
    print(f"IT-audit theme: {len(it_ex)} examples | non-IT: {len(nonit_ex)} examples")

    print(f"Fetching OAG datasets from CKAN API (org={ORG})...")
    records = fetch_oag(args.max_audits)
    print(f"Fetched {len(records)} datasets")

    # Score title + description (two-pole: IT audit minus non-IT audit)
    from sentence_transformers import SentenceTransformer
    print(f"Loading embedding model ({EMBED_MODEL})...")
    model = SentenceTransformer(EMBED_MODEL)
    it_vec = theme_vector(model, it_ex)
    nonit_vec = theme_vector(model, nonit_ex)

    texts = [_strip_md(f"{p.get('title','')}. {p.get('notes','') or ''}") for p in records]
    print(f"Embedding {len(texts)} audit title+descriptions...")
    embs = model.encode(texts, normalize_embeddings=True, show_progress_bar=False,
                        batch_size=64)
    scores = [round(float(e @ it_vec - e @ nonit_vec), 4) for e in embs]

    n = build_db(records, scores, API)
    print(f"\nWrote {n} audits to {DB_PATH} ({DB_PATH.stat().st_size/1e6:.1f} MB)")
    if args.show_extremes:
        show_extremes()
    print("\nAttribution: contains information licensed under the "
          "Open Government Licence - Canada.")


if __name__ == "__main__":
    main()
