"""Retrieval: hybrid search, one tender by id, and nearest neighbours."""
from __future__ import annotations

from . import corpus, paths
from .company_profile import _window_fields
from .corpus import _rrf_fuse, load_collection
from .provenance import _corpus_provenance
from .text import _display_agency, _slugify

def cmd_search(args) -> dict:
    """Hybrid search. Returns list of {tender_id, title, score, snippet}."""
    load_collection()
    n_pool = max(args.n * 3, 30)  # Pull more for fusion, then trim

    # Semantic side — ChromaDB
    semantic_results = corpus._collection.query(query_texts=[args.query], n_results=n_pool)
    semantic: list[tuple[str, float]] = []
    if semantic_results["ids"] and semantic_results["ids"][0]:
        for doc_id, distance in zip(
            semantic_results["ids"][0],
            semantic_results.get("distances", [[]])[0] or [0] * len(semantic_results["ids"][0]),
        ):
            # Convert cosine distance to a similarity-ish score
            semantic.append((doc_id, 1 / (1 + distance)))

    # Keyword side — BM25
    bm25_hits = corpus._bm25.search(args.query, top_k=n_pool)
    keyword = [(corpus.doc_index[idx]["id"], score) for idx, score in bm25_hits]

    # Fuse
    fused = _rrf_fuse(semantic, keyword)[:args.n]

    # Build response
    id_to_doc = {d["id"]: d for d in corpus.doc_index}
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
            "in_watching": (paths.WATCHING / f"{_slugify(doc_id)}.md").exists(),
            "snippet": snippet,
        })
    return {"query": args.query, "n": len(results), "results": results}

def cmd_get(args) -> dict:
    """Full details for one tender."""
    load_collection()
    id_to_doc = {d["id"]: d for d in corpus.doc_index}
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
        # A third category again: not this notice's data, but how old the
        # corpus it came out of is. `derived` changes daily under a static
        # corpus; this says how static the corpus actually is.
        "provenance": _corpus_provenance(),
        "document": doc["document"],
        "in_watching": (paths.WATCHING / f"{_slugify(doc['id'])}.md").exists(),
    }


def cmd_similar(args) -> dict:
    """Find tenders similar to a given one (by its embedding)."""
    load_collection()
    # Query using the target tender's document text as the query
    id_to_doc = {d["id"]: d for d in corpus.doc_index}
    target = id_to_doc.get(args.tender_id)
    if not target:
        return {"error": f"Tender {args.tender_id} not found"}

    results = corpus._collection.query(
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
