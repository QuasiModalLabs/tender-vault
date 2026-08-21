"""
The blinded review surface, and the two mechanisms that make it structural.

THE REQUIREMENT. A reviewer must not see the rejection reason, the failing gate,
or the stratum an item was sampled from until a disposition has been recorded
for that item. Afterwards they see all of it - seeing whether you agreed is the
useful part - but never before.

WHY IT IS NOT A RENDERING RULE. "Remember not to print these fields" survives
exactly until someone adds a fourth call site. So:

  1. The reviewer is handed a DIFFERENT TYPE. `BlindedNotice` does not carry
     first_rejecting_stage, predicate results, evidence, stratum or sampling
     strategy - they are absent from the object, not private on it. A rendering
     bug cannot leak a field that is not there, and `blind()` is one-way: there
     is no route back to the decision from a BlindedNotice.

  2. `reveal()` CHECKS THE STORE, not the caller's good intentions. It refuses
     until a disposition row exists for the item.

  3. The first disposition is IMMUTABLE. Otherwise the protocol is gameable:
     record a throwaway, peek, rewrite. Reviews are append-only; a correction is
     a new record marked post_reveal, and only pre-reveal dispositions count
     toward agreement statistics.

WHAT BLINDING DOES NOT DO, stated rather than overclaimed. An expert who sees
UNSPSC 77101501 and knows the profile families can reconstruct "coded, wrong
family" unaided. This prevents anchoring on the machine's answer; it cannot
prevent expertise, and it does not try to. Separately, a queue drawn only from
rejects still tells the reviewer the verdict was REJECT even with the reason,
gate and stratum withheld - `--include-admitted` closes that gap and is off by
default because the workflow was scoped to reject sampling.
"""
from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Optional

# The fields a reviewer must never see before disposing of an item. Named here
# so the test can assert them absent from BlindedNotice by introspection rather
# than by reading rendered output - a test that greps stdout only proves the one
# code path it exercised.
WITHHELD_UNTIL_DISPOSED = (
    "production_admitted",
    "first_rejecting_stage",
    "audit_admitted",
    "audit_stage_results",
    "family_result",
    "keyword_result",
    "matched_families",
    "matched_keywords",
    "has_codes",
    "stratum",
    "sampling_strategy",
)


@dataclass(frozen=True)
class BlindedNotice:
    """
    Everything a reviewer may see, and the only thing the review surface renders.

    What is NOT here is the point. The raw publisher fields stay, because the
    reviewer is judging the notice and needs them; what is withheld is the
    filter's verdict ABOUT the notice.
    """
    item_id: str          # opaque; carries no stratum and no position meaning
    reference_number: str
    title: str
    description: str
    contracting_entity: str
    end_user: str
    notice_type: str
    procurement_category: str
    unspsc: str
    gsin: str
    publication_date: str
    closing_date: str

    def to_dict(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self)}


def blind(item_id: str, notice_row) -> BlindedNotice:
    """
    Project a raw notice row into what the reviewer may see.

    ONE-WAY. Nothing on the returned object references the audit decision, so a
    caller holding a BlindedNotice cannot reach the verdict even by accident.
    """
    get = (notice_row.get if isinstance(notice_row, dict)
           else lambda k: notice_row[k] if k in set(notice_row.keys()) else None)

    def text(key) -> str:
        value = get(key)
        return "" if value is None else str(value)

    return BlindedNotice(
        item_id=item_id,
        reference_number=text("reference_number"),
        title=text("title"),
        description=text("description"),
        contracting_entity=text("contracting_entity"),
        end_user=text("end_user"),
        notice_type=text("notice_type"),
        procurement_category=text("procurement_category"),
        unspsc=text("unspsc"),
        gsin=text("gsin"),
        publication_date=text("publication_date"),
        closing_date=text("closing_date"),
    )


def assert_blinded(payload: dict) -> None:
    """
    Refuse to emit a review-surface payload that carries a withheld field.

    Belt to `blind()`'s braces: the type makes leakage impossible for anything
    constructed properly, and this catches a hand-assembled dict that was not.
    Raises rather than filtering, because silently stripping a field would hide
    the bug that put it there.
    """
    leaked = sorted(set(payload) & set(WITHHELD_UNTIL_DISPOSED))
    if leaked:
        raise AssertionError(
            f"Review surface would have disclosed {leaked} before a disposition "
            f"was recorded. Blinding is a property of this package - see "
            f"filter_audit/blinding.py. Route the payload through blind().")


class RevealRefused(Exception):
    """Raised when reveal is attempted before a disposition exists."""


def reveal_payload(decision_row, queue_row, disposition: Optional[dict]) -> dict:
    """
    The full verdict, and the comparison the reviewer actually wants.

    Refuses unless a disposition exists. The refusal is checked against the
    STORE rather than against a flag the caller passes, so a caller cannot
    assert its way past it.
    """
    if not disposition:
        raise RevealRefused(
            "No disposition recorded for this item. Blinding is enforced here, "
            "not by convention - record a decision first with `record-review`.")

    reviewed = disposition.get("reviewed_decision")
    production = "ACCEPT" if decision_row["production_admitted"] else "REJECT"
    return {
        "item_id": queue_row["item_id"],
        "reference_number": decision_row["notice_id"],
        "your_decision": reviewed,
        "production_decision": production,
        "agreed": reviewed == production,
        "first_rejecting_stage": decision_row["first_rejecting_stage"],
        "audit_admitted": bool(decision_row["audit_admitted"]),
        "relevance": {
            "has_codes": bool(decision_row["has_codes"]),
            "family_result": decision_row["family_result"],
            "keyword_result": decision_row["keyword_result"],
            "matched_families": decision_row["matched_families"],
            "matched_keywords": decision_row["matched_keywords"],
        },
        "stratum": queue_row["stratum"],
        "audit_stage_results": decision_row["audit_stage_results"],
        "next_step": (
            "Attach a failure category with `categorize` if you disagreed. That "
            "step is deliberately post-reveal: naming WHY the filter erred "
            "requires knowing what it did, and forcing it while blinded would "
            "produce guesses."),
    }
