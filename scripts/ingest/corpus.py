"""
ChromaDB persistence — this is what Claude's tools read from.

The build is staged: the old corpus moves aside and is only deleted once the
new one is complete, so a failure part-way leaves you with the corpus you had.
"""
from __future__ import annotations

import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd


def _meta_str(value, limit: int) -> str:
    """NaN-safe string for ChromaDB metadata. float('nan') is truthy, so the
    obvious `str(x) or ''` yields the literal string 'nan'."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    return str(value).strip()[:limit]


def _feed_mtime_iso(feed_path: Optional[Path]) -> Optional[str]:
    """
    When the feed CSV this run read was downloaded, or None if there wasn't one.

    Separate from the build time on purpose. A rebuild off an unchanged cache
    moves `corpus_built_at` and leaves this alone, and that difference is the
    only way to tell "I have newer data" from "I re-ran the ingest" — which
    matters because only the first one changes what is in the corpus.

    Returns None rather than raising: --cache can point anywhere, and a test
    harness that drives build_chroma directly has no feed at all.
    """
    if feed_path is None or not feed_path.exists():
        return None
    return datetime.fromtimestamp(
        feed_path.stat().st_mtime).isoformat(timespec="seconds")


def build_chroma(df: pd.DataFrame, db_path: Path, cols: dict,
                 feed_path: Optional[Path] = None) -> None:
    """
    Embed filtered tenders and write to a persistent ChromaDB collection.

    `feed_path` is the CSV this run read, recorded as provenance. Optional and
    keyword-defaulted because tests drive this directly with no feed on disk;
    absent means the corpus is stamped with a build time and no feed date,
    which is a different fact from an unstamped corpus.
    """
    # Imported here, not at module level: this is the only function that needs
    # ChromaDB, and contracts_ingest.py imports this module purely for its
    # profile parser and HTTP headers.
    import chromadb
    from chromadb.utils import embedding_functions

    # We want a clean snapshot, not accumulated cruft — but the old corpus is
    # moved ASIDE rather than deleted, so a failure part-way through the build
    # doesn't leave us with nothing.
    #
    # Why aside rather than the usual build-to-temp-and-rename: ChromaDB holds
    # OS-level handles on its directory for the life of the client, so renaming
    # a freshly built temp directory into place fails on Windows with
    # PermissionError (verified). Renaming the OLD directory works, because
    # nothing has it open yet.
    retired = None
    if db_path.exists():
        retired = db_path.with_name(db_path.name + ".old")
        if retired.exists():
            shutil.rmtree(retired)
        db_path.rename(retired)

    try:
        _write_chroma(df, db_path, cols, feed_path)
    except BaseException:
        if retired is not None:
            # Best effort: clear whatever partial exists and put the old corpus
            # back. ChromaDB may still hold handles on a partial build, in which
            # case the cleanup fails and we hand the user the two paths instead
            # of pretending we recovered.
            shutil.rmtree(db_path, ignore_errors=True)
            if not db_path.exists():
                retired.rename(db_path)
                sys.stderr.write(
                    f"\nIngest failed. Your previous corpus has been restored:\n"
                    f"  {db_path}\n"
                )
            else:
                sys.stderr.write(
                    f"\nIngest failed. Your previous corpus was NOT deleted:\n"
                    f"  {retired}\n"
                    f"An incomplete build is at {db_path} and is still held open by\n"
                    f"ChromaDB, so this process cannot swap the old one back itself.\n"
                    f"To restore:  rm -rf {db_path} && mv {retired} {db_path}\n"
                )
        raise
    if retired is not None:
        shutil.rmtree(retired, ignore_errors=True)


def _write_chroma(df: pd.DataFrame, db_path: Path, cols: dict,
                  feed_path: Optional[Path] = None) -> None:
    """Embed and write. Split out so build_chroma owns the rollback logic."""
    import chromadb
    from chromadb.utils import embedding_functions

    client = chromadb.PersistentClient(path=str(db_path))
    embedder = embedding_functions.SentenceTransformerEmbeddingFunction(
        model_name="all-MiniLM-L6-v2"
    )

    # Provenance, written here because it cannot be recovered afterwards:
    # ChromaDB rewrites its segment files whenever anything LOADS the
    # collection, so chroma_db/ mtimes report when the corpus was last queried,
    # not when it was built. A briefing that reads them describes its own read.
    provenance = {
        "corpus_built_at": datetime.now().isoformat(timespec="seconds"),
    }
    # OMITTED when unknown, never None. Chroma raises
    # `TypeError: argument 'metadata': Cannot convert Python object to
    # MetadataValue` on a None value (verified, chromadb 1.5.9), so the obvious
    # inline `"feed_downloaded_at": _feed_mtime_iso(...)` crashes the whole
    # ingest the first time there is no cached feed. An absent key is the
    # signal — see the provenance states in tender_tools._corpus_provenance.
    feed_at = _feed_mtime_iso(feed_path)
    if feed_at is not None:
        provenance["feed_downloaded_at"] = feed_at

    collection = client.create_collection(
        name="tenders",
        embedding_function=embedder,
        metadata={"hnsw:space": "cosine", **provenance},
    )

    documents, metadatas, ids = [], [], []
    for _, row in df.iterrows():
        tender_id = str(row.get(cols["tender_id"], ""))
        if not tender_id or tender_id == "nan":
            continue

        title = str(row.get(cols["title"], ""))[:300]
        desc = str(row.get(cols["description"], ""))[:2000]
        # We embed title + description — weighting title higher by repeating it
        document = f"{title}\n{title}\n\n{desc}"

        metadata = {
            "tender_id": tender_id,
            "title": title[:200],
            # TWO fields, deliberately not merged and not collapsed to one.
            # Federal IT is routinely bought by a central authority (SSC, PSPC)
            # on behalf of the department that actually needs the work, so the
            # contracting entity is frequently NOT the customer. End user is the
            # demand signal; contracting entity is the fallback — it's always
            # populated, while end user is blank on roughly half the rows.
            "contracting_entity": _meta_str(row.get(cols["contracting_entity"]), 500),
            # Multi-valued and slash-delimited: one tender can legitimately name
            # several departments ("Department of National Defence (DND) /
            # Department of Transport (TC) / ..."). Stored VERBATIM, all values
            # kept. Do not re-join on commas — entity names contain commas
            # ("Foreign Affairs, Trade And Development (Department Of)"), which
            # would make the field unsplittable downstream.
            "end_user_entity": _meta_str(row.get(cols["end_user"]), 500),
            "closing_date": row["_closing"].strftime("%Y-%m-%d") if pd.notna(row["_closing"]) else "",
            "matched_competencies": ",".join(row.get("_matched", [])),
            # Instrument shape, from classify_notice — the same function the
            # dossier uses. `kind_basis` says which publisher field decided it,
            # because determinability is not uniform across the four shapes.
            "opportunity_kind": row["_kind"]["opportunity_kind"],
            "kind_basis": row["_kind"]["kind_basis"],
            # Which hand-checked UNSPSC families this notice fell under, empty
            # when it qualified on keywords alone (or carries no codes at all).
            "unspsc_families": ",".join(row.get("_unspsc_families") or []),
            # federal / unrecognised. `unrecognised` means the registry has no
            # entry, NOT that the notice is provincial — federal Crown
            # corporations land there. Non-federal never reaches this point.
            "jurisdiction": row["_jurisdiction"]["jurisdiction"],
        }
        if row["_jurisdiction"].get("org_keys"):
            metadata["org_keys"] = row["_jurisdiction"]["org_keys"]

        # Present ONLY when the prose contradicts the closing date. An absent
        # key means no conflict was found, not that the date was verified.
        if row.get("_date_conflict"):
            metadata["closing_date_conflict"] = row["_date_conflict"]
            metadata["closing_date_note"] = (
                f"The description states a submission deadline of "
                f"{row['_date_conflict']}, EARLIER than the closing_date field. "
                f"Confirm against the notice before planning to the later date.")

        # OMITTED, not zeroed, when no value was extracted. Chroma metadata
        # cannot hold None, and storing 0.0 made "nobody stated a value" render
        # as "this contract is worth nothing" on every one of 11 tenders.
        # Absent key -> consumers show "not stated".
        if pd.notna(row.get("_value")):
            metadata["estimated_value"] = float(row["_value"])

        documents.append(document)
        metadatas.append(metadata)
        ids.append(tender_id)

    # Batch insert (ChromaDB handles this fine up to several thousand at a time)
    batch = 200
    for i in range(0, len(documents), batch):
        collection.add(
            documents=documents[i:i + batch],
            metadatas=metadatas[i:i + batch],
            ids=ids[i:i + batch],
        )
        print(f"  Embedded {min(i + batch, len(documents)):,} / {len(documents):,}")

    print(f"\nChromaDB written to {db_path} ({collection.count():,} tenders)")
