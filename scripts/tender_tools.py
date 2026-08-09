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
    python scripts/tender_tools.py list-corpus --window imminent
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

# The single definition of what kind of thing a notice is, shared with the
# ingest filter. Imported rather than reimplemented: when the dossier and the
# corpus disagree about whether an "RFP against Supply Arrangement" is work,
# one of them is lying to the reader, and it is not obvious which.
from ingest import classify_notice as _classify_notice  # noqa: E402
from ingest import entity_org_keys as _entity_org_keys  # noqa: E402



PROJECT_ROOT = Path(__file__).parent.parent
DB_PATH = PROJECT_ROOT / "chroma_db"
VAULT = PROJECT_ROOT / "vault"
PROFILE = VAULT / "profiles" / "my-company.md"
WATCHING = VAULT / "tenders" / "watching"
ARCHIVED = VAULT / "tenders" / "archived"
PARKED = VAULT / "tenders" / "parked"

# Department nodes, named by canonical key. The target of every [[key]] link a
# tender file writes, and the file whose backlinks answer "what are we looking
# at from this department".
#
# HAND-EDITABLE AND NEVER OVERWRITTEN. Deliberately not derived from the
# contracts ingest: that generator writes <key>-contracts.md, rewrites it every
# run, and only runs at all if someone took the optional ~630MB download. Graph
# connectivity cannot be a side effect of an optional step.
#
# CREATED ON PROMOTE, not generated in bulk. The registry carries ~100 keys and
# a vault holding 100 department stubs for the handful actually in play is a
# graph that hides its own signal.
AGENCIES = VAULT / "agencies"


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


def _yaml_list(raw: str) -> list[str]:
    """
    One inline frontmatter sequence as a list, for the no-dependency parse.

    cmd_list_watching reads frontmatter by hand rather than pulling in a YAML
    parser, so the `[a, b]` flow form is unpacked here. Quotes come off because
    department entries are written with them: a bare [[ircc]] is a nested
    sequence in YAML, not a string.
    """
    inner = raw.strip()
    if not inner.startswith("["):
        return [inner.strip('"')] if inner else []
    return [p.strip().strip('"') for p in inner[1:].rstrip("]").split(",") if p.strip()]


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
            # Derived now, not stored — see _window_fields. `imminent` means
            # inside the profile threshold; those notices used to be deleted at
            # ingest. `closed` means it expired since the corpus was built.
            **_window_fields(doc["metadata"]),
            # None, never 0. The ingest omits this key when no value was
            # extracted, and defaulting it to 0 is what made every tender in
            # the corpus read as a $0 contract.
            "estimated_value": doc["metadata"].get("estimated_value"),
            "matched_competencies": doc["metadata"].get("matched_competencies", ""),
            # What KIND of thing this is, before any judgement of fit. A
            # qualification is a vehicle, not work; see classify_notice.
            "opportunity_kind": doc["metadata"].get("opportunity_kind", "unknown"),
            "kind_basis": doc["metadata"].get("kind_basis", "unclassified"),
            "unspsc_families": doc["metadata"].get("unspsc_families", ""),
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
        # Alongside metadata rather than merged into it: metadata is what the
        # ingest wrote and is stable until the next one, these two are computed
        # for today and change under a corpus that hasn't moved.
        "derived": _window_fields(doc["metadata"]),
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


def cmd_list_corpus(args) -> dict:
    """
    Every notice in the corpus, ordered by closing date.

    The briefing is instructed to read the corpus end to end rather than search
    it, and until now there was no command that returned it — doing that meant
    reading ChromaDB directly, outside the tool layer that every other corpus
    operation goes through. `search` cannot substitute: it ranks against a query
    and returns n, which is the opposite of surveying what is open.

    Sorted by closing date with `standing` and `unknown` last, because a
    placeholder year sorted numerically puts a permanent supply arrangement at
    the bottom of the list and a missing date at the top of it.
    """
    load_collection()
    rows = []
    for doc in doc_index:
        meta = doc["metadata"]
        derived = _window_fields(meta)
        if args.window and derived["closing_window"] != args.window:
            continue
        rows.append({
            "tender_id": doc["id"],
            "title": meta.get("title", ""),
            "agency": _display_agency(meta),
            "closing_date": meta.get("closing_date", ""),
            **derived,
            "opportunity_kind": meta.get("opportunity_kind", "unknown"),
            "kind_basis": meta.get("kind_basis", "unclassified"),
            "matched_competencies": meta.get("matched_competencies", ""),
            "unspsc_families": meta.get("unspsc_families", ""),
            "in_watching": (WATCHING / f"{_slugify(doc['id'])}.md").exists(),
        })

    rank = {"closed": 0, "imminent": 1, "open": 2, "standing": 3, "unknown": 4}
    rows.sort(key=lambda r: (rank.get(r["closing_window"], 9), r["closing_date"]))

    counts: dict[str, int] = {}
    for r in rows:
        counts[r["closing_window"]] = counts.get(r["closing_window"], 0) + 1
    return {
        "count": len(rows),
        "by_window": counts,
        "imminent_within_days": _profile_imminence_threshold(),
        "filtered_to": args.window,
        "corpus": rows,
    }


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
            # Both department fields, so "which departments are we watching" and
            # "what did the registry fail to resolve" are answerable from the
            # tool rather than by grepping the vault. The second is the evidence
            # for whether the registry needs a Crown-corporation tier.
            "department": _yaml_list(fields.get("department", "")),
            "department_unresolved": _yaml_list(
                fields.get("department_unresolved", "")),
        })
    return {"watching": tenders}


# The three entity_source values in prose. Keyed rather than formatted so an
# unrecognised source raises here instead of being quietly rendered as a bare
# key — the set is fixed and test_dossier locks it.
_SOURCE_PROSE = {
    "end_user": "named as end user",
    "contracting_entity_end_user_unstated": "contracting entity; end user unstated",
    "contracting_entity_end_user_names_others":
        "contracting entity; end user is another department",
}


def _attribution_note(attribution: dict[str, dict], unresolved: list[str]) -> str:
    """
    Spell the attribution out in the BODY, not only in frontmatter.

    Someone reading this file in three weeks should not have to go and check a
    metadata field to learn that its department is a buyer of record rather than
    the stated customer, or that a registry miss means "not a department" rather
    than "not federal". Both are easy to reconstruct wrongly and expensive to
    reconstruct wrongly.
    """
    weak = [k for k, a in attribution.items() if a["entity_source"] != "end_user"]
    named = ", ".join(f'"{u}"' for u in unresolved) or "either entity field"

    if not attribution:
        return (
            f"\n> **Attribution note:** The organization registry did not resolve "
            f"{named}. The registry indexes *departments and agencies*, so a miss "
            "means **not a department**, not **not federal** — federal Crown "
            "corporations such as CDIC, BDC and Canada Post have no entry in it. "
            "See [[dossier]].\n"
        )

    lines = []
    if weak:
        lines.append(
            ", ".join(f"[[{k}]]" for k in weak)
            + (" is " if len(weak) == 1 else " are ")
            + "the contracting entity here, not a stated end user. Federal IT is "
            "routinely bought by SSC or PSPC on behalf of the department that "
            "actually needs the work, so this is weaker evidence than an end-user "
            "attribution. See [[dossier]]."
        )
    if unresolved:
        lines.append(
            f"The registry also did not resolve {named} — a miss there means "
            "*not a department*, not *not federal*."
        )
    if not lines:
        return ""
    lines[0] = f"**Attribution note:** {lines[0]}"
    return "\n" + "\n>\n".join(f"> {line}" for line in lines) + "\n"


def _ensure_agency_nodes(attribution: dict[str, dict]) -> list[str]:
    """
    Create a department node for every key this tender links, if absent.

    CREATE-IF-MISSING, NEVER OVERWRITE. An existing node is left exactly as it
    is — it is the one file in this pair a person is expected to write in, and
    a promote that rewrote it would eat notes accumulated across every tender
    that department has ever appeared in. The check is `exists()`, not a marker
    check, because there is nothing here worth clobbering a stranger's file for.

    Returns the keys actually created, so promote can report them rather than
    leaving new vault files to be noticed later.
    """
    created: list[str] = []
    if not attribution:
        return created

    AGENCIES.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d")

    for key in attribution:
        target = AGENCIES / f"{key}.md"
        if target.exists():
            continue

        # display_name falls back to the key itself for anything the registry
        # cannot name, which is the right failure: a node named after a key that
        # resolves to nothing is still a working link target.
        try:
            title = org_resolve.default_resolver().display_name(key) or key
        except Exception:
            title = key

        target.write_text(
            f"""---
canonical_key: {key}
agency: "{title[:150].replace('"', "'")}"
type: department
created: {stamp}
created_by: tender_tools.promote
---

# {title}

Department node — **hand-editable, and nothing regenerates it.** Created on the
first promote of a tender attributed to `{key}`; never rewritten afterwards.

The backlinks on this file are every tender, briefing and note in the vault that
links [[{key}]]. That list is the reason this file exists.

Awarded-contract intelligence is a separate, generated file: [[{key}-contracts]],
written by `scripts/contracts_ingest.py` and rewritten on every run. It is not
committed and will not exist until that ingest has been run, so treat a dead
link there as "not built yet" rather than "no contracts".

## Notes

What we've learned about dealing with this department: how they buy, who keeps
winning, which constraints recur, and what we decided and why. Both of us write
here — Claude appends dated entries as things come up, per `vault/CLAUDE.md`.
Append-only, newest at the bottom; nothing above is edited or removed.
""",
            encoding="utf-8",
            newline="\n",
        )
        created.append(key)

    return created


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
    families = meta.get("unspsc_families", "")
    family_list = [f.strip() for f in families.split(",") if f.strip()]

    # The ingest omits estimated_value entirely when nothing was extracted, so
    # "not stated" survives into the vault instead of being written down as $0
    # and later read back as a fact about the contract.
    value = meta.get("estimated_value")
    value_yaml = "null" if value is None else f"{value}"
    value_prose = "Not stated" if value is None else f"${value:,.0f}"
    kind = meta.get("opportunity_kind", "unknown")

    # Departments as WIKILINKS on the CANONICAL KEY, never the display string.
    # The key is the identity the rest of the project already uses, so linking on
    # it is what lets the vault graph connect a tender to its department — and,
    # through that department's backlinks, to every other tender touching it.
    # Resolved through _entity_attribution, shared with the dossier, so a tender
    # file and a dossier can never disagree about who a notice is for.
    end_user_raw = str(meta.get("end_user_entity") or "").strip()
    contracting_raw = str(meta.get("contracting_entity") or "").strip()
    attribution = _entity_attribution(end_user_raw, contracting_raw)

    # Quoted, because a bare [[ircc]] is a nested YAML sequence rather than a
    # string. Always a list, even for one department: a sometimes-scalar field
    # means every reader needs a type check and one of them will forget.
    dept_yaml = ", ".join(f'"[[{key}]]"' for key in attribution)
    source_yaml = ", ".join(a["entity_source"] for a in attribution.values())

    # Entity strings the registry did not resolve, kept as EVIDENCE rather than
    # dropped. Recorded even when the other field DID resolve, because the
    # question this list answers — should the registry grow a Crown-corporation
    # tier — deserves to be settled by counts rather than by someone noticing.
    unresolved: list[str] = []
    for raw in (end_user_raw, contracting_raw):
        if raw and raw not in unresolved and not _entity_keys(raw):
            unresolved.append(raw)
    unresolved_yaml = (
        "\ndepartment_unresolved: ["
        + ", ".join(f'"{u.replace(chr(34), chr(39))}"' for u in unresolved)
        + "]"
    ) if unresolved else ""

    dept_prose = ", ".join(
        f"[[{key}]] ({_SOURCE_PROSE[a['entity_source']]})"
        for key, a in attribution.items()
    ) or "None resolved — see the attribution note below."

    content = f"""---
tender_id: {doc['id']}
title: "{meta.get('title', '').replace('"', "'")}"
agency: "{_display_agency(meta).replace('"', "'")}"
department: [{dept_yaml}]
entity_source: [{source_yaml}]{unresolved_yaml}
closing_date: {meta.get('closing_date', '')}
estimated_value: {value_yaml}
matched_competencies: [{', '.join(matched_list)}]
unspsc_families: [{', '.join(family_list)}]
opportunity_kind: {kind}
kind_basis: {meta.get('kind_basis', 'unclassified')}
status: watching
promoted_at: {datetime.now().strftime('%Y-%m-%d')}
---

# {meta.get('title', 'Untitled')}

**Agency:** {_display_agency(meta) or 'Unknown'}
**Departments:** {dept_prose}
**Closes:** {meta.get('closing_date', 'Unknown')}
**Estimated value:** {value_prose}
**Instrument:** {kind} (per {meta.get('kind_basis', 'unclassified')})
**Matched on:** {', '.join(matched_list) if matched_list else 'none'}
**UNSPSC families:** {', '.join(family_list) if family_list else 'none'}
{_attribution_note(attribution, unresolved)}
## Description

{doc['document']}

## My notes

<!-- Claude can append analysis here under "## Fit assessment" -->
"""
    target.write_text(content, encoding="utf-8", newline="\n")

    # After the tender is on disk: the tender file is the artifact being asked
    # for, and the nodes exist to serve it. Reported rather than silent — these
    # are new vault files, and a tool that creates files without saying so is a
    # tool you have to audit afterwards.
    created = _ensure_agency_nodes(attribution)

    result = {"promoted": str(target.relative_to(PROJECT_ROOT))}
    if created:
        result["agency_nodes_created"] = [
            str((AGENCIES / f"{key}.md").relative_to(PROJECT_ROOT)) for key in created
        ]
    return result


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
    target.write_text(content, encoding="utf-8", newline="\n")
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
    target.write_text(content, encoding="utf-8", newline="\n")
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


# ---------------------------------------------------------------------------
# The department dossier — convergence
# ---------------------------------------------------------------------------
# One query, four sources. This ASSEMBLES; it does not score. There is
# deliberately no convergence number and no cross-signal ranking: weighting four
# incommensurable signals into one figure buries the reasoning that makes the
# dossier worth reading, and the reader is a model that can weigh them itself.
# Nothing here may combine a value from one section with a value from another.

_TENDERS_CSV = PROJECT_ROOT / ".cache" / "tenders.csv"

# The CanadaBuys open-notice feed. The dossier reads the raw feed rather than
# the profile-filtered corpus, so it keeps its own column map; ingest.py reads
# most of these into ChromaDB but not the notice URL.
_TENDER_COLS = {
    "tender_id": "referenceNumber-numeroReference",
    "title": "title-titre-eng",
    "closing": "tenderClosingDate-appelOffresDateCloture",
    "contracting": "contractingEntityName-nomEntitContractante-eng",
    "end_user": "endUserEntitiesName-nomEntitesUtilisateurFinal-eng",
    "notice_type": "noticeType-avisType-eng",
    "procurement_category": "procurementCategory-categorieApprovisionnement",
    # Read for classification only, never rendered — the dossier lists notices,
    # it doesn't reproduce them. Without it classify_notice cannot see the
    # prose signals, and the dossier would call a TBIPS call-up open work.
    "description": "tenderDescription-descriptionAppelOffres-eng",
    "url": "noticeURL-URLavis-eng",
}

# Single definition, in ingest.py, imported here. It used to be declared in both
# modules with the same value and the same comment — which is fine until one of
# them is tuned and the corpus and the dossier start disagreeing about what a
# placeholder date is.
from ingest import SENTINEL_HORIZON_YEARS as _SENTINEL_HORIZON_YEARS  # noqa: E402


def _profile_expiry_min_value() -> float:
    """Value floor for expiring contracts, from the company profile frontmatter."""
    try:
        import sys as _sys
        _sys.path.insert(0, str(Path(__file__).parent))
        from ingest import parse_profile
        return parse_profile(PROFILE).get("expiry_min_value", 0)
    except Exception:
        return 0


def _profile_imminence_threshold() -> int:
    """
    Days-until-close below which a notice is `imminent`, from the profile.

    Read at call time, mirroring _profile_expiry_min_value: the window is
    derived per query rather than stored, so retuning the threshold takes effect
    on the next command with no re-ingest.
    """
    try:
        import sys as _sys
        _sys.path.insert(0, str(Path(__file__).parent))
        from ingest import parse_profile
        return int(parse_profile(PROFILE).get("imminent_within_days", 5))
    except Exception:
        return 5


def _window_fields(meta: dict) -> dict:
    """
    `closing_window` and `days_until_close` for a corpus notice, computed now.

    Deliberately not stored in ChromaDB. Both values depend on today's date, and
    a corpus is read for up to a week after it is built — a stored `imminent`
    would still say `imminent` after the notice had closed. Computing here means
    a briefing written three days after an ingest sees the truth on the day it
    is written, including notices that expired in between.
    """
    from ingest import closing_window
    window, days = closing_window(
        meta.get("closing_date", ""), _profile_imminence_threshold()
    )
    return {"closing_window": window, "days_until_close": days}


def _months_until(date_str: str, today: datetime) -> int | None:
    try:
        return round((datetime.strptime(date_str, "%Y-%m-%d") - today).days / 30)
    except (ValueError, TypeError):
        return None


def _entity_keys(value: str) -> dict[str, str]:
    """
    Canonical keys named by one tender entity field, each with the verbatim
    phrase that produced it.

    Delegates to ingest.entity_org_keys, which is the single definition — the
    ingest filter uses the same resolution to decide whether a notice is
    federal, and two copies of that would eventually disagree about which
    organizations exist. The matched phrase rides along because an attribution
    that cannot say which string produced it is not reviewable, which is the
    rule OrgResolver.scan already follows for audits.
    """
    return _entity_org_keys(value)


def _entity_attribution(end_user_value, contracting_value) -> dict[str, dict]:
    """
    Every canonical department a notice attributes to, and how each was reached.

    ONE definition, shared by the dossier's notice section and by promote. The
    dossier asks "does this notice reach the department I am looking at"; promote
    asks "which departments does this notice name" — the same resolution read from
    two directions, and two copies of it would eventually disagree about which
    field carried an attribution.

    END-USER KEYS FIRST, and the order is load-bearing rather than cosmetic: it is
    the order they are written into a promoted tender file, and the department that
    needs the work outranks the one that happens to be buying. A contracting entity
    only ever ADDS a department the end-user field did not already name; it never
    overwrites one, because that would downgrade a stated customer to a buyer of
    record.

    The `entity_source` values are the distinction the whole thing exists for.
    Federal IT is routinely bought by SSC or PSPC on behalf of the department that
    actually needs it, so 'they are buying this' and 'they are the buyer of record
    and nobody said who it is for' have to stay apart. The matched phrase rides
    along as `entity_evidence` because an attribution that cannot say which string
    produced it is not reviewable.
    """
    end_user = _entity_keys(end_user_value)
    contracting = _entity_keys(contracting_value)

    attribution: dict[str, dict] = {}
    for key, evidence in end_user.items():
        attribution[key] = {"entity_source": "end_user", "entity_evidence": evidence}

    # Whether the end-user field named ANYONE is a property of the notice, not of
    # the department being asked about — so it is decided once, here, rather than
    # per lookup.
    fallback = ("contracting_entity_end_user_unstated" if not end_user
                else "contracting_entity_end_user_names_others")
    for key, evidence in contracting.items():
        attribution.setdefault(
            key, {"entity_source": fallback, "entity_evidence": evidence}
        )
    return attribution


def _profile_corpus_ids() -> set[str]:
    """
    Tender ids in the profile-filtered ChromaDB corpus, read straight out of
    Chroma's SQLite rather than through the client, so the dossier never pays
    for the embedding model just to answer "is this one we already track?".
    """
    import sqlite3
    db = DB_PATH / "chroma.sqlite3"
    if not db.exists():
        return set()
    con = sqlite3.connect(db)
    try:
        return {
            r[0] for r in con.execute(
                "SELECT string_value FROM embedding_metadata WHERE key = 'tender_id'"
            ) if r[0]
        }
    except sqlite3.OperationalError:
        return set()
    finally:
        con.close()


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
    db = PROJECT_ROOT / "data" / "contracts.db"
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
    db = PROJECT_ROOT / "data" / "oag.db"
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
    db = PROJECT_ROOT / "data" / "plans.db"
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
    db = PROJECT_ROOT / "data" / "contracts.db"
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
    if not _TENDERS_CSV.exists():
        return {"state": "no_feed",
                "note": "No open-notice feed cached. Run: python scripts/ingest.py"}

    in_corpus = _profile_corpus_ids()
    today = datetime.now()
    horizon = today.replace(year=today.year + _SENTINEL_HORIZON_YEARS).strftime("%Y-%m-%d")
    today_str = today.strftime("%Y-%m-%d")

    notices, scanned, expired = [], 0, 0
    with open(_TENDERS_CSV, encoding="utf-8-sig", newline="") as fh:
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
        "tenders": _dossier_tenders(dept, limit),
        "how_to_read": (
            "Four independent sources on one department. Read them together and "
            "form your own judgement — there is no score here on purpose. What "
            "you are looking for is agreement between sections: an audit finding "
            "and a plan and an expiring incumbent that are all about the same "
            "system. An empty section is information too, and each one says "
            "which kind of empty it is — no data, or no signal. Check "
            "identity.records_folded_in before quoting any total: where a "
            "predecessor or absorbed organization has been folded in, the "
            "registry's note says what the number actually covers."
        ),
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

    lc = sub.add_parser(
        "list-corpus",
        help="Every notice in the corpus by closing date — for reading it end to end",
    )
    lc.add_argument(
        "--window",
        choices=["closed", "imminent", "open", "standing", "unknown"],
        help="Only notices in this closing window (default: all)",
    )
    lc.set_defaults(func=cmd_list_corpus)

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

    do = sub.add_parser(
        "dossier",
        help="Everything all four sources know about one department — the "
             "convergence view. Assembles; does not score",
    )
    do.add_argument("department", help=_DEPT_HELP)
    do.add_argument("--months-min", type=int, default=6,
                    help="Expiry window opens this many months out (default 6)")
    do.add_argument("--months-max", type=int, default=24,
                    help="Expiry window closes this many months out (default 24)")
    do.add_argument("--min-value", type=float, default=None,
                    help="Value floor for the expiry timeline (default: "
                         "expiry_min_value from the company profile)")
    do.add_argument("--limit", type=int, default=10,
                    help="Rows per section (default 10)")
    do.set_defaults(func=cmd_department_dossier)

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
