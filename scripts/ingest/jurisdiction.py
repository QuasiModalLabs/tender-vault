"""
Federal, non-federal, or unrecognised — decided by the organization registry,
never by a name list.
"""
from __future__ import annotations

import pandas as pd

import crosswalk
import org_resolve


# Provinces and territories, for the NON-federal side only. The federal side is
# answered by the organization registry (entity_org_keys), never by a list —
# but the registry is a registry of federal bodies, so a miss means "not
# recognised", not "not federal": CDIC, BDC, Canada Post and Service Canada all
# miss it and are federal. Distinguishing a territorial government from an
# unregistered federal Crown corporation therefore needs a positive signal, and
# this is it.
_PROVINCIAL_JURISDICTIONS = (
    "alberta", "british columbia", "manitoba", "new brunswick",
    "newfoundland and labrador", "northwest territories", "nova scotia",
    "nunavut", "ontario", "prince edward island", "quebec", "québec",
    "saskatchewan", "yukon",
)
_JURISDICTION_PREFIXES = ("government of the ", "government of ", "province of ",
                          "territory of ")


def entity_org_keys(value) -> dict[str, str]:
    """
    Canonical registry keys named by one tender entity field, with the phrase
    that produced each.

    THE SINGLE DEFINITION, shared with tender_tools._entity_keys. Both steps are
    load-bearing: observed_variants splits the slash-delimited multi-department
    values and strips the parenthetical acronym tail, without which "Department
    of National Defence (DND)" resolves to nothing — raw-string resolution
    covers 47 of 896 rows, this covers 767.

    crosswalk and org_resolve are imported at module level. They could not be
    while the shared plumbing lived in ingest: crosswalk imports resolve_columns
    and staged_db, so importing crosswalk from here closed a cycle and the
    import had to happen inside the call. Both now come from ingest_common,
    which imports nothing of ours, so the cycle is gone rather than dodged.
    """
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return {}
    if not str(value).strip():
        return {}

    resolver = org_resolve.default_resolver()
    found: dict[str, str] = {}
    for variant in crosswalk.observed_variants(str(value)):
        try:
            key = resolver.resolve(variant)
        except org_resolve.AmbiguousOrganization:
            continue
        if key and key not in found:
            found[key] = variant
    return found


def classify_jurisdiction(contracting_entity, end_user_entity) -> dict:
    """
    Federal, non-federal, or unrecognised — and the difference matters.

    `federal` means the organization registry resolved one of the two entity
    fields. That is the authority, so no federal name list is maintained here.

    `non_federal` requires a POSITIVE provincial or territorial signal and no
    registry hit. The Government of the Northwest Territories publishes to this
    feed and its notices are not available to a federal bidder in the way the
    rest of the corpus is.

    `unrecognised` is the honest third answer, not a synonym for non-federal.
    CDIC, BDC, Canada Post and Service Canada all land here: federal, but absent
    from a registry of departments and agencies. Dropping on this value would
    drop the best tender in the corpus, so nothing does.
    """
    keys = {}
    for value in (end_user_entity, contracting_entity):
        keys.update(entity_org_keys(value))
    if keys:
        return {"jurisdiction": "federal", "jurisdiction_basis": "org_registry",
                "org_keys": ",".join(sorted(keys))}

    blob = " ".join(
        str(v).lower() for v in (contracting_entity, end_user_entity)
        if v is not None and not (isinstance(v, float) and pd.isna(v))
    )
    for prefix in _JURISDICTION_PREFIXES:
        for province in _PROVINCIAL_JURISDICTIONS:
            if prefix + province in blob:
                return {
                    "jurisdiction": "non_federal",
                    "jurisdiction_basis": "provincial_or_territorial_name",
                    "jurisdiction_note": (
                        f"Names a provincial/territorial government "
                        f"({prefix + province}) and resolves to no federal "
                        f"organization."),
                }
    return {
        "jurisdiction": "unrecognised",
        "jurisdiction_basis": "no_registry_match",
        "jurisdiction_note": (
            "Not in the federal organization registry and carries no "
            "provincial or territorial name. Federal Crown corporations sit "
            "here — this is not evidence the notice is non-federal."),
    }
