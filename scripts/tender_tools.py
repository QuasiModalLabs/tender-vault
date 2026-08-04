"""
Tools for Claude Code to work with the tender corpus.

This is the 'wrap' layer — Claude decides what to search for, when to fetch
full details, when to promote, etc. These tools just execute cleanly and
return JSON.

Design principle: each command is a single verb. Each prints JSON to stdout.
Errors go to stderr and non-zero exit codes. Claude can chain these freely.

Usage:
    python scripts/tender_tools.py search "cloud migration federal"
    python scripts/tender_tools.py get W1234-567890
    python scripts/tender_tools.py similar W1234-567890
    python scripts/tender_tools.py list-watching
    python scripts/tender_tools.py list-parked
    python scripts/tender_tools.py contracts-intel "cloud"
    python scripts/tender_tools.py promote W1234-567890
    python scripts/tender_tools.py park some-file.md "no clearance" "after hiring cleared architect"
    python scripts/tender_tools.py archive some-file.md "lost to competitor"
"""
from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import org_resolve  # noqa: E402



PROJECT_ROOT = Path(__file__).parent.parent
DB_PATH = PROJECT_ROOT / "chroma_db"
VAULT = PROJECT_ROOT / "vault"
PROFILE = VAULT / "profiles" / "my-company.md"
WATCHING = VAULT / "tenders" / "watching"
ARCHIVED = VAULT / "tenders" / "archived"
PARKED = VAULT / "tenders" / "parked"


# ---------------------------------------------------------------------------
# Lightweight BM25 — paired with ChromaDB's vector search for hybrid retrieval
# ---------------------------------------------------------------------------
# We keep this simple and in-process. At a few hundred tenders it's instant.

class BM25:
    def __init__(self, corpus: list[str], k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self.corpus_tokens = [self._tokenize(doc) for doc in corpus]
        self.corpus_size = len(corpus)
        self.avgdl = (
            sum(len(t) for t in self.corpus_tokens) / self.corpus_size
            if self.corpus_size else 0
        )
        self._build_index()

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        return re.findall(r"\b\w+\b", text.lower())

    def _build_index(self):
        self.doc_freqs = [Counter(tokens) for tokens in self.corpus_tokens]
        self.doc_lens = [len(tokens) for tokens in self.corpus_tokens]
        df: Counter = Counter()
        for tokens in self.corpus_tokens:
            df.update(set(tokens))
        self.idf = {
            term: math.log((self.corpus_size - freq + 0.5) / (freq + 0.5) + 1)
            for term, freq in df.items()
        }

    def search(self, query: str, top_k: int = 20) -> list[tuple[int, float]]:
        query_tokens = self._tokenize(query)
        scores = []
        for idx in range(self.corpus_size):
            doc_freqs = self.doc_freqs[idx]
            doc_len = self.doc_lens[idx]
            score = 0.0
            for term in query_tokens:
                if term not in doc_freqs:
                    continue
                freq = doc_freqs[term]
                idf = self.idf.get(term, 0)
                numerator = freq * (self.k1 + 1)
                denominator = freq + self.k1 * (1 - self.b + self.b * doc_len / self.avgdl)
                score += idf * numerator / denominator
            if score > 0:
                scores.append((idx, score))
        scores.sort(key=lambda x: x[1], reverse=True)
        return scores[:top_k]


# ---------------------------------------------------------------------------
# ChromaDB access
# ---------------------------------------------------------------------------

_collection = None
_bm25 = None
doc_index: list[dict] = []  # Parallel to BM25 corpus, for ID lookup
_load_lock = __import__("threading").Lock()


def load_collection():
    """
    Lazy-load ChromaDB and build BM25 index. Thread-safe: the MCP server
    preloads in a background thread at startup, and a tool call may arrive
    mid-load — the lock makes the second caller wait for the first load
    instead of racing it.
    """
    global _collection, _bm25, doc_index
    if _collection is not None:
        return _collection

    with _load_lock:
        if _collection is not None:  # Loaded while we waited for the lock
            return _collection
        return _do_load()


def _do_load():
    global _collection, _bm25, doc_index

    # Imported here rather than at module level so that SQLite-only commands
    # (contracts-intel) and vault-only commands (list-watching, park, archive)
    # run instantly without paying the ChromaDB/torch import cost.
    import chromadb
    from chromadb.utils import embedding_functions

    if not DB_PATH.exists():
        sys.stderr.write(
            f"ChromaDB not found at {DB_PATH}.\n"
            f"Run: python scripts/ingest.py\n"
        )
        sys.exit(2)

    client = chromadb.PersistentClient(path=str(DB_PATH))
    embedder = embedding_functions.SentenceTransformerEmbeddingFunction(
        model_name="all-MiniLM-L6-v2"
    )
    _collection = client.get_collection("tenders", embedding_function=embedder)

    # Pull everything once to build BM25 (this is fine at our scale)
    all_data = _collection.get()
    doc_index = [
        {"id": tid, "metadata": meta, "document": doc}
        for tid, meta, doc in zip(
            all_data["ids"], all_data["metadatas"], all_data["documents"]
        )
    ]
    _bm25 = BM25([d["document"] for d in doc_index])
    return _collection


def _display_agency(meta: dict) -> str:
    """
    Which department to SHOW for a tender. End user is the department that
    needs the work and is what we care about; the contracting entity is the
    fallback for the ~half of rows where end user is blank. This is a display
    convenience only — the two fields are stored separately in ChromaDB and
    anything matching on department should read them separately.
    """
    return meta.get("end_user_entity") or meta.get("contracting_entity") or ""


# ---------------------------------------------------------------------------
# Reciprocal Rank Fusion — combine BM25 + semantic without tuning weights
# ---------------------------------------------------------------------------

def _rrf_fuse(
    semantic: list[tuple[str, float]],
    keyword: list[tuple[str, float]],
    k: int = 60,
) -> list[tuple[str, float]]:
    """
    Standard RRF. We score by rank position rather than the raw scores, which
    dodges the problem of BM25 and cosine similarity being on different scales.
    """
    scores: dict[str, float] = {}
    for rank, (doc_id, _) in enumerate(semantic, start=1):
        scores[doc_id] = scores.get(doc_id, 0) + 1 / (k + rank)
    for rank, (doc_id, _) in enumerate(keyword, start=1):
        scores[doc_id] = scores.get(doc_id, 0) + 1 / (k + rank)
    return sorted(scores.items(), key=lambda x: x[1], reverse=True)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_search(args) -> dict:
    """Hybrid search. Returns list of {tender_id, title, score, snippet}."""
    load_collection()
    n_pool = max(args.n * 3, 30)  # Pull more for fusion, then trim

    # Semantic side — ChromaDB
    semantic_results = _collection.query(query_texts=[args.query], n_results=n_pool)
    semantic: list[tuple[str, float]] = []
    if semantic_results["ids"] and semantic_results["ids"][0]:
        for doc_id, distance in zip(
            semantic_results["ids"][0],
            semantic_results.get("distances", [[]])[0] or [0] * len(semantic_results["ids"][0]),
        ):
            # Convert cosine distance to a similarity-ish score
            semantic.append((doc_id, 1 / (1 + distance)))

    # Keyword side — BM25
    bm25_hits = _bm25.search(args.query, top_k=n_pool)
    keyword = [(doc_index[idx]["id"], score) for idx, score in bm25_hits]

    # Fuse
    fused = _rrf_fuse(semantic, keyword)[:args.n]

    # Build response
    id_to_doc = {d["id"]: d for d in doc_index}
    results = []
    for doc_id, fused_score in fused:
        doc = id_to_doc.get(doc_id)
        if not doc:
            continue
        snippet = doc["document"][:300].replace("\n", " ").strip() + "..."
        results.append({
            "tender_id": doc_id,
            "title": doc["metadata"].get("title", ""),
            "agency": _display_agency(doc["metadata"]),
            "closing_date": doc["metadata"].get("closing_date", ""),
            "estimated_value": doc["metadata"].get("estimated_value", 0),
            "matched_competencies": doc["metadata"].get("matched_competencies", ""),
            "score": round(fused_score, 4),
            "in_watching": (WATCHING / f"{_slugify(doc_id)}.md").exists(),
            "snippet": snippet,
        })
    return {"query": args.query, "n": len(results), "results": results}


def cmd_get(args) -> dict:
    """Full details for one tender."""
    load_collection()
    id_to_doc = {d["id"]: d for d in doc_index}
    doc = id_to_doc.get(args.tender_id)
    if not doc:
        return {"error": f"Tender {args.tender_id} not found in corpus"}
    return {
        "tender_id": doc["id"],
        "metadata": doc["metadata"],
        "document": doc["document"],
        "in_watching": (WATCHING / f"{_slugify(doc['id'])}.md").exists(),
    }


def cmd_similar(args) -> dict:
    """Find tenders similar to a given one (by its embedding)."""
    load_collection()
    # Query using the target tender's document text as the query
    id_to_doc = {d["id"]: d for d in doc_index}
    target = id_to_doc.get(args.tender_id)
    if not target:
        return {"error": f"Tender {args.tender_id} not found"}

    results = _collection.query(
        query_texts=[target["document"][:1000]],
        n_results=args.n + 1,  # +1 because the target itself will match
    )
    similar = []
    if results["ids"] and results["ids"][0]:
        for doc_id, distance in zip(
            results["ids"][0],
            results.get("distances", [[]])[0] or [0] * len(results["ids"][0]),
        ):
            if doc_id == args.tender_id:
                continue  # Skip self-match
            doc = id_to_doc.get(doc_id)
            if not doc:
                continue
            similar.append({
                "tender_id": doc_id,
                "title": doc["metadata"].get("title", ""),
                "agency": _display_agency(doc["metadata"]),
                "similarity": round(1 / (1 + distance), 4),
            })
    return {"target": args.tender_id, "similar": similar[:args.n]}


def cmd_list_watching(args) -> dict:
    """List all tenders in the watching folder with basic metadata."""
    if not WATCHING.exists():
        return {"watching": []}
    files = sorted(WATCHING.glob("*.md"))
    tenders = []
    for f in files:
        content = f.read_text(encoding="utf-8")
        # Extract a few fields from frontmatter without a full YAML parse
        fm_match = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
        fields = {}
        if fm_match:
            for line in fm_match.group(1).split("\n"):
                if ":" in line:
                    k, _, v = line.partition(":")
                    fields[k.strip()] = v.strip().strip('"')
        tenders.append({
            "filename": f.name,
            "tender_id": fields.get("tender_id", ""),
            "title": fields.get("title", ""),
            "closing_date": fields.get("closing_date", ""),
            "status": fields.get("status", ""),
        })
    return {"watching": tenders}


def cmd_promote(args) -> dict:
    """Copy a tender from ChromaDB into vault/tenders/watching/ as markdown."""
    load_collection()
    id_to_doc = {d["id"]: d for d in doc_index}
    doc = id_to_doc.get(args.tender_id)
    if not doc:
        return {"error": f"Tender {args.tender_id} not found"}

    meta = doc["metadata"]
    filename = f"{_slugify(doc['id'])}.md"
    target = WATCHING / filename
    if target.exists():
        return {"error": f"Already promoted: {filename}"}

    WATCHING.mkdir(parents=True, exist_ok=True)

    # Build the markdown file with YAML frontmatter
    matched = meta.get("matched_competencies", "")
    matched_list = [m.strip() for m in matched.split(",") if m.strip()]

    content = f"""---
tender_id: {doc['id']}
title: "{meta.get('title', '').replace('"', "'")}"
agency: "{_display_agency(meta).replace('"', "'")}"
closing_date: {meta.get('closing_date', '')}
estimated_value: {meta.get('estimated_value', 0)}
matched_competencies: [{', '.join(matched_list)}]
status: watching
promoted_at: {datetime.now().strftime('%Y-%m-%d')}
---

# {meta.get('title', 'Untitled')}

**Agency:** {_display_agency(meta) or 'Unknown'}
**Closes:** {meta.get('closing_date', 'Unknown')}
**Estimated value:** ${meta.get('estimated_value', 0):,.0f}
**Matched on:** {', '.join(matched_list) if matched_list else 'none'}

## Description

{doc['document']}

## My notes

<!-- Claude can append analysis here under "## Fit assessment" -->
"""
    target.write_text(content, encoding="utf-8")
    return {"promoted": str(target.relative_to(PROJECT_ROOT))}


def cmd_archive(args) -> dict:
    """
    Move a tender to archived/ with a reason. Source can be watching/ or parked/.

    Archived = decision is final. Use park instead if you might revisit.
    """
    # Look in watching first, then parked
    for source_dir in (WATCHING, PARKED):
        candidate = source_dir / args.filename
        if candidate.exists():
            source = candidate
            break
    else:
        return {
            "error": f"Not found in watching/ or parked/: {args.filename}",
        }

    ARCHIVED.mkdir(parents=True, exist_ok=True)
    target = ARCHIVED / args.filename

    # Append the archive reason to the file before moving
    content = source.read_text(encoding="utf-8")
    stamp = datetime.now().strftime("%Y-%m-%d")
    from_dir = source.parent.name
    content += f"\n\n## Archived {stamp} (from {from_dir})\n\n{args.reason}\n"
    target.write_text(content, encoding="utf-8")
    source.unlink()
    return {
        "archived": str(target.relative_to(PROJECT_ROOT)),
        "from": from_dir,
        "reason": args.reason,
    }


def cmd_park(args) -> dict:
    """
    Move a watching tender to parked/ with a reason and a revisit trigger.

    Park is for "not pursuing now but the situation might change." The trigger
    is a freeform string describing what would make this worth re-evaluating
    (e.g. "after we hire a cleared architect", "if reissued in 2027").
    Distinct from archive, which is for permanent close-out.
    """
    source = WATCHING / args.filename
    if not source.exists():
        return {"error": f"Not in watching/: {args.filename}"}

    PARKED.mkdir(parents=True, exist_ok=True)
    target = PARKED / args.filename

    content = source.read_text(encoding="utf-8")
    stamp = datetime.now().strftime("%Y-%m-%d")
    content += (
        f"\n\n## Parked {stamp}\n\n"
        f"**Reason:** {args.reason}\n\n"
        f"**Revisit when:** {args.revisit_when}\n"
    )
    target.write_text(content, encoding="utf-8")
    source.unlink()
    return {
        "parked": str(target.relative_to(PROJECT_ROOT)),
        "reason": args.reason,
        "revisit_when": args.revisit_when,
    }


def cmd_contracts_intel(args) -> dict:
    """
    Competitive intelligence from the proactive-disclosure contracts DB.

    Pure SQLite. Deliberately does NOT touch ChromaDB, so it responds in
    milliseconds regardless of embedding-model state. Aggregates per contract
    family (procurement id) using each family's highest recorded value, which
    approximates 'current value including amendments'.
    """
    import sqlite3

    db = PROJECT_ROOT / "data" / "contracts.db"
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
                    "note": f"{dept!r} publishes no contracts under its own slug, "
                            "so this dataset cannot answer for it. That is a real "
                            "fact about the organization, not a build failure."}
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

    db = PROJECT_ROOT / "data" / "plans.db"
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
        con.close()
        return {
            "as_of": meta.get("ingest_date"),
            "programs": 0,
            "note": "No programs above the score floor. Lower --min-score.",
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

    db = PROJECT_ROOT / "data" / "oag.db"
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

    db = PROJECT_ROOT / "data" / "contracts.db"
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
        try:
            import sys as _sys
            _sys.path.insert(0, str(Path(__file__).parent))
            from ingest import parse_profile
            criteria = parse_profile(PROFILE)
            min_value = criteria.get("expiry_min_value", 0)
        except Exception:
            min_value = 0

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
                    "note": f"{dept!r} publishes no contracts under its own slug, "
                            "so this dataset cannot answer for it."}
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


def cmd_list_parked(args) -> dict:
    """List all parked tenders with their revisit triggers."""
    if not PARKED.exists():
        return {"parked": []}
    files = sorted(PARKED.glob("*.md"))
    tenders = []
    for f in files:
        content = f.read_text(encoding="utf-8")
        # Frontmatter — same lightweight parse as list-watching
        fm_match = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
        fields = {}
        if fm_match:
            for line in fm_match.group(1).split("\n"):
                if ":" in line:
                    k, _, v = line.partition(":")
                    fields[k.strip()] = v.strip().strip('"')
        # Pull the most recent "Revisit when:" line from the body so Claude can
        # see at a glance what would unstick this tender
        revisit = ""
        for match in re.finditer(r"\*\*Revisit when:\*\*\s*(.+)", content):
            revisit = match.group(1).strip()
        tenders.append({
            "filename": f.name,
            "tender_id": fields.get("tender_id", ""),
            "title": fields.get("title", ""),
            "closing_date": fields.get("closing_date", ""),
            "revisit_when": revisit,
        })
    return {"parked": tenders}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _slugify(text: str, max_len: int = 80) -> str:
    slug = re.sub(r"[^\w\s-]", "", text.lower())
    slug = re.sub(r"[-\s]+", "-", slug).strip("-")
    return slug[:max_len] or "untitled"


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------

# ONE department identifier across all four signal tools. Inconsistency here
# would be its own bug: the entire point of these tools is cross-referencing a
# department between them, and that fails if each takes a different spelling.
_DEPT_HELP = (
    "Canonical key from vault/crosswalk/org_aliases.yaml (e.g. pspc, ircc, dnd) "
    "or an organization's registered name. Exact after normalization — "
    "substrings are refused, so one department's name cannot land on another. "
    "Use `resolve-department` to check a string first."
)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("search", help="Hybrid search the full corpus")
    s.add_argument("query")
    s.add_argument("--n", type=int, default=10)
    s.set_defaults(func=cmd_search)

    g = sub.add_parser("get", help="Full details for one tender")
    g.add_argument("tender_id")
    g.set_defaults(func=cmd_get)

    sim = sub.add_parser("similar", help="Find tenders similar to a given one")
    sim.add_argument("tender_id")
    sim.add_argument("--n", type=int, default=5)
    sim.set_defaults(func=cmd_similar)

    lw = sub.add_parser("list-watching", help="List promoted tenders")
    lw.set_defaults(func=cmd_list_watching)

    lp = sub.add_parser("list-parked", help="List parked tenders with revisit triggers")
    lp.set_defaults(func=cmd_list_parked)

    ci = sub.add_parser(
        "contracts-intel",
        help="Who won similar contracts: vendors, departments, values (SQLite, instant)",
    )
    ci.add_argument("query")
    ci.add_argument("--department", help=_DEPT_HELP)
    ci.set_defaults(func=cmd_contracts_intel)

    ec = sub.add_parser(
        "expiring-contracts",
        help="Contracts expiring in a window — near-certain future re-procurements",
    )
    ec.add_argument("--months-min", type=int, default=6,
                    help="Earliest expiry, months from now (default 6)")
    ec.add_argument("--months-max", type=int, default=24,
                    help="Latest expiry, months from now (default 24)")
    ec.add_argument("--min-value", type=float, default=None,
                    help="Minimum contract value (overrides profile's "
                         "expiry_min_value; default reads from profile)")
    ec.add_argument("--department", help=_DEPT_HELP)
    ec.set_defaults(func=cmd_expiring_contracts)

    ps = sub.add_parser(
        "program-signals",
        help="Programs showing operational pressure (pre-RFP intent signal)",
    )
    ps.add_argument("--department", help=_DEPT_HELP)
    ps.add_argument("--min-score", type=float, default=None,
                    help="Only programs with intent_score at or above this "
                         "(default: no floor, since real leads can score slightly negative)")
    ps.add_argument("--exclude-internal", action="store_true",
                    help="Exclude Internal Services programs (included by default)")
    ps.add_argument("--limit", type=int, default=25, help="How many to return (default 25)")
    ps.set_defaults(func=cmd_program_signals)

    og = sub.add_parser(
        "oag-signals",
        help="OAG audits touching IT/systems — independent-scrutiny pre-RFP signal",
    )
    og.add_argument("--department", help=_DEPT_HELP)
    og.add_argument("--vendor",
                    help="Audits INTO a named supplier (e.g. GCStrategies, "
                         "McKinsey). Independent of --department: these audits "
                         "span too many departments to attach to one, but an "
                         "audit of a firm you bid against is intelligence anyway")
    og.add_argument("--direct-only", action="store_true",
                    help="Only departments named in the audit itself, dropping "
                         "those a briefing package inherited from a hearing")
    og.add_argument("--min-score", type=float, default=None,
                    help="Only audits with it_score at or above this")
    og.add_argument("--doc-type", choices=["performance_audit", "committee_hearing",
                                           "special_examination", "financial_audit"],
                    help="Restrict to one document type")
    og.add_argument("--since", type=int, default=None, help="Only audits from this year onward")
    og.add_argument("--limit", type=int, default=20, help="How many to return (default 20)")
    og.set_defaults(func=cmd_oag_signals)

    rd = sub.add_parser(
        "resolve-department",
        help="What a department string resolves to — check before querying",
    )
    rd.add_argument("name")
    rd.set_defaults(func=cmd_resolve_department)

    pr = sub.add_parser("promote", help="Copy a tender into vault/tenders/watching/")
    pr.add_argument("tender_id")
    pr.set_defaults(func=cmd_promote)

    pk = sub.add_parser(
        "park",
        help="Move a watching tender to parked/ (not pursuing now, might revisit)",
    )
    pk.add_argument("filename")
    pk.add_argument("reason", help="Why we're parking it")
    pk.add_argument(
        "revisit_when",
        help="What event would make this worth re-evaluating",
    )
    pk.set_defaults(func=cmd_park)

    ar = sub.add_parser(
        "archive",
        help="Move a tender to archived/ (final). Source can be watching/ or parked/.",
    )
    ar.add_argument("filename")
    ar.add_argument("reason")
    ar.set_defaults(func=cmd_archive)

    return p


def main():
    parser = build_parser()
    args = parser.parse_args()
    result = args.func(args)
    print(json.dumps(result, indent=2, default=str))
    # Error responses → non-zero exit so Claude notices
    if isinstance(result, dict) and "error" in result:
        sys.exit(1)


if __name__ == "__main__":
    main()
