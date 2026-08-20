"""
The tender lifecycle: listing the corpus, and moving a notice through the vault.

promote → watching/, park → parked/, archive → archived/. These are the only
commands that write markdown into the vault, which is why paths.py is read by
attribute here and why tests/conftest.py exists.
"""
from __future__ import annotations

import re
from datetime import datetime

from . import corpus, paths
from .company_profile import _profile_imminence_threshold, _window_fields
from .corpus import load_collection
from .documents import _move_attachment_dir
from .entities import (
    _attribution_note,
    _ensure_agency_nodes,
    _entity_attribution,
    _entity_keys,
)
from .provenance import _corpus_provenance
from .text import _display_agency, _slugify, _yaml_list

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
    for doc in corpus.doc_index:
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
            "in_watching": (paths.WATCHING / f"{_slugify(doc['id'])}.md").exists(),
        })

    rank = {"closed": 0, "imminent": 1, "open": 2, "standing": 3, "unknown": 4}
    rows.sort(key=lambda r: (rank.get(r["closing_window"], 9), r["closing_date"]))

    counts: dict[str, int] = {}
    for r in rows:
        counts[r["closing_window"]] = counts.get(r["closing_window"], 0) + 1
    return {
        "count": len(rows),
        # First, because a survey of what is open is worth exactly as much as
        # the corpus it reads, and the reader cannot judge that from the rows.
        "provenance": _corpus_provenance(),
        "by_window": counts,
        "imminent_within_days": _profile_imminence_threshold(),
        "filtered_to": args.window,
        "corpus": rows,
    }

def cmd_list_watching(args) -> dict:
    """List all tenders in the watching folder with basic metadata."""
    if not paths.WATCHING.exists():
        return {"watching": []}
    files = sorted(paths.WATCHING.glob("*.md"))
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


def cmd_promote(args) -> dict:
    """Copy a tender from ChromaDB into vault/tenders/watching/ as markdown."""
    load_collection()
    id_to_doc = {d["id"]: d for d in corpus.doc_index}
    doc = id_to_doc.get(args.tender_id)
    if not doc:
        return {"error": f"Tender {args.tender_id} not found"}

    meta = doc["metadata"]
    filename = f"{_slugify(doc['id'])}.md"
    target = paths.WATCHING / filename
    if target.exists():
        return {"error": f"Already promoted: {filename}"}

    paths.WATCHING.mkdir(parents=True, exist_ok=True)

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

    result = {"promoted": str(target.relative_to(paths.PROJECT_ROOT))}
    if created:
        result["agency_nodes_created"] = [
            str((paths.AGENCIES / f"{key}.md").relative_to(paths.PROJECT_ROOT)) for key in created
        ]
    return result


def cmd_archive(args) -> dict:
    """
    Move a tender to archived/ with a reason. Source can be watching/ or parked/.

    Archived = decision is final. Use park instead if you might revisit.
    """
    # Look in watching first, then parked
    for source_dir in (paths.WATCHING, paths.PARKED):
        candidate = source_dir / args.filename
        if candidate.exists():
            source = candidate
            break
    else:
        return {
            "error": f"Not found in watching/ or parked/: {args.filename}",
        }

    paths.ARCHIVED.mkdir(parents=True, exist_ok=True)
    target = paths.ARCHIVED / args.filename

    # Documents move BEFORE the note does. See _move_attachment_dir.
    moved, error = _move_attachment_dir(source, target)
    if error:
        return error

    # Append the archive reason to the file before moving
    content = source.read_text(encoding="utf-8")
    stamp = datetime.now().strftime("%Y-%m-%d")
    from_dir = source.parent.name
    content += f"\n\n## Archived {stamp} (from {from_dir})\n\n{args.reason}\n"
    target.write_text(content, encoding="utf-8", newline="\n")
    source.unlink()
    result = {
        "archived": str(target.relative_to(paths.PROJECT_ROOT)),
        "from": from_dir,
        "reason": args.reason,
    }
    if moved:
        result["attachments_moved"] = moved
    return result


def cmd_park(args) -> dict:
    """
    Move a watching tender to parked/ with a reason and a revisit trigger.

    Park is for "not pursuing now but the situation might change." The trigger
    is a freeform string describing what would make this worth re-evaluating
    (e.g. "after we hire a cleared architect", "if reissued in 2027").
    Distinct from archive, which is for permanent close-out.
    """
    source = paths.WATCHING / args.filename
    if not source.exists():
        return {"error": f"Not in watching/: {args.filename}"}

    paths.PARKED.mkdir(parents=True, exist_ok=True)
    target = paths.PARKED / args.filename

    # Documents move BEFORE the note does. See _move_attachment_dir.
    moved, error = _move_attachment_dir(source, target)
    if error:
        return error

    content = source.read_text(encoding="utf-8")
    stamp = datetime.now().strftime("%Y-%m-%d")
    content += (
        f"\n\n## Parked {stamp}\n\n"
        f"**Reason:** {args.reason}\n\n"
        f"**Revisit when:** {args.revisit_when}\n"
    )
    target.write_text(content, encoding="utf-8", newline="\n")
    source.unlink()
    result = {
        "parked": str(target.relative_to(paths.PROJECT_ROOT)),
        "reason": args.reason,
        "revisit_when": args.revisit_when,
    }
    if moved:
        result["attachments_moved"] = moved
    return result


def cmd_list_parked(args) -> dict:
    """List all parked tenders with their revisit triggers."""
    if not paths.PARKED.exists():
        return {"parked": []}
    files = sorted(paths.PARKED.glob("*.md"))
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
