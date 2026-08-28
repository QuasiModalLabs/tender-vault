"""
Resolving the departments named on a notice, and writing that attribution down.

The registry lookup lives in scripts/org_resolve.py; this is the layer that
decides which of a notice's two department fields to trust, and renders the
result as prose a human can check.
"""
from __future__ import annotations

from datetime import datetime

import org_resolve

from ingest import entity_org_keys as _entity_org_keys

from . import paths


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

    paths.AGENCIES.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d")

    for key in attribution:
        target = paths.AGENCIES / f"{key}.md"
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
