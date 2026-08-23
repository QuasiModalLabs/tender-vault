"""
What KIND of thing a notice is — instrument shape, from the publisher's own
fields, with prose as the fallback of last resort.
"""
from __future__ import annotations

import re

import pandas as pd


# ---------------------------------------------------------------------------
# Instrument shape — what KIND of thing a notice is
# ---------------------------------------------------------------------------
# Read from the publisher's structured noticeType field rather than matched out
# of the title. "RFSA" in a title is a convention; noticeType is a controlled
# vocabulary, and the two disagree — of 14 TBIPS-titled notices in the feed, 7
# are typed "RFP against Supply Arrangement", 5 plain "Request for Proposal"
# and 2 "Request for Supply Arrangement".
#
# Matching is EXACT after casefolding, never substring. Substring matching is
# what made this wrong before: "RFP against Supply Arrangement" contains the
# words "supply arrangement", so a substring test labelled all 54 of them
# `qualification` — the exact inverse of the truth, because a call-up against a
# vehicle is the real work that the vehicle exists to award.
_NOTICE_KINDS = {
    # Qualify a supplier onto a vehicle. Not work; an invitation to compete
    # later, often via call-ups that never get a public notice.
    "request for supply arrangement": "qualification",
    "request for standing offer": "qualification",
    "invitation to qualify": "qualification",
    # Work competed among suppliers already on a vehicle.
    "rfp against supply arrangement": "call_up",
    # Not biddable. Market research, or an award already intended for a named
    # supplier with a challenge window.
    "request for information": "information",
    "advance contract award notice": "pre_awarded",
}

# procurementCategory is the only classification field populated on 100% of
# notices across every source system, so it carries the goods/services/
# construction split when noticeType cannot.
_CATEGORY_CONSTRUCTION = "*CNST"
_CATEGORY_GOODS = "*GD"
_CATEGORY_SERVICES = ("*SRV", "*SRVTGD")


def parse_categories(raw) -> set[str]:
    """
    Split the multi-valued procurementCategory field into its codes.

    Newline-delimited, one code per line, each with a leading '*'. A single
    notice legitimately carries several ('*SRV\\n*GD' — services and goods).
    """
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return set()
    return {p.strip() for p in str(raw).split("\n") if p.strip()}


def parse_unspsc_codes(raw) -> set[str]:
    """
    Split the multi-valued unspsc field into bare 8-digit commodity codes.

    Same shape as procurementCategory: newline-delimited, '*'-prefixed. One
    notice can carry a great many — the widest row in the feed lists 286 codes.
    """
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return set()
    codes = set()
    for part in str(raw).split("\n"):
        code = part.strip().lstrip("*").strip()
        if len(code) == 8 and code.isdigit():
            codes.add(code)
    return codes


# Everything below reads title+description prose, and each is deliberately
# precision-first: a rule that fires on nothing is recoverable, a rule that
# relabels real work is not. Counts on the 2026-08-04 feed are recorded next to
# each so a future drift in volume is visible rather than silent.

# A posting whose entire content is "here are the suppliers who already
# qualified". Not a solicitation, not a qualification, not work — there is
# nothing to bid and never will be. The EHRP one names Accenture, Deloitte, EY
# and Telus Health as the qualified integrators; the shortlist is closed.
#
# Phrases only, and only ones that say "this notice buys nothing" outright.
# "list of qualified suppliers" is NOT here despite looking ideal: it appears on
# 8 notices, most of them ordinary RFSAs describing the list they will produce.
_RESULTS_NOTICE_PHRASES = (
    "no solicitation document",        # 1 — EHRP
    "this is a notice only",           # 1 — EHRP
    "results notification",            # 1 — EHRP
    "itq result",                      # 1 — DRS
    "following the itq",               # 1 — DRS
    "have been selected as qualified",  # 1 — EHRP
)

# KNOWN supply-arrangement numbers, matched as exact whole tokens.
#
# Not a pattern. The obvious rule — \b[A-Z]{2}\d{3}-\d{5,6}\b — was tried and
# is wrong: PSPC solicitation numbers share the format exactly, so it matched
# EE517-261427 (a fishermen's wharf reconstruction), EQ754-270127 (building
# demolition) and EQ754-251469 (a lift-bridge security gate) and relabelled
# three construction projects as IT call-ups. The format identifies PSPC, not
# a vehicle.
#
# Each entry below was observed in the feed within an explicit "supply
# arrangement" context, with the count from the 2026-08-04 run. Add to this by
# checking the same way, not by loosening it back to a pattern.
_KNOWN_SUPPLY_ARRANGEMENTS = {
    "EN578-170432": "TBIPS — Task-Based Informatics Professional Services (11)",
    "EN537-05IT01": "SBIPS — Solution-Based Informatics Professional Services",
    "EN578-172870": "EN578 series professional services (2)",
    "EN578-201407": "EN578 series professional services (2)",
    "EN578-232335": "EN578 series professional services (1)",
    "EN578-150229": "EN578 series professional services (1)",
}
_SA_NUMBER = re.compile(
    r"\b(" + "|".join(re.escape(k) for k in _KNOWN_SUPPLY_ARRANGEMENTS) + r")\b",
    re.IGNORECASE,
)

# The vehicle by NAME rather than by number. Needed because the number is not
# always there: of the three call-ups the notice-type field missed, only ONE
# (an NRC help-desk requirement) cites EN578-170432 in its body. The two DND
# ones carry no arrangement number and never say "supply arrangement" — the
# only signal they are call-ups is the word TBIPS in the title. A name is
# weaker evidence than a number, and kind_basis says which one fired.
_VEHICLE_TOKENS = re.compile(r"\b(tbips|sbips)\b", re.IGNORECASE)


def classify_notice(notice_type, procurement_category, text=None) -> dict:
    """
    Classify a notice by instrument shape, from the publisher's own fields.

    `text` is optional title+description prose. Without it this reads only the
    structured fields and returns the same answers it always did; with it, two
    further shapes become reachable that no structured field expresses —
    results notices and call-ups that were typed as plain RFPs.

    THE SINGLE DEFINITION. Imported by tender_tools so the dossier and the
    ingest filter cannot drift into disagreeing about what `qualification`
    means. Returns `kind`, plus `kind_basis` naming the field that decided it,
    plus a `kind_note` for the kinds whose consequence isn't self-evident.

    Determinability is not uniform, and the basis field says so rather than
    implying a confidence the data doesn't support:

      qualification  fully determinable from noticeType.
      call_up        authoritative when typed "RFP against Supply
                     Arrangement". Otherwise recovered from `text` by the
                     arrangement number, or failing that by the vehicle name —
                     see kind_basis, which distinguishes the three.
      results_notice detectable only from prose, and only by phrases that say
                     outright that the notice buys nothing.
      product        `*GD` alone is a reliable goods buy, but a SaaS or COTS
                     purchase is routinely filed `*SRV` (both the Justice
                     OFOVC case-management SaaS and the ESDC applicant
                     tracking system are), so this UNDER-reports. A
                     `solicitation` may still turn out to be a product buy.
      solicitation   the residual: an open competition of unresolved shape.
                     Not a positive finding.
    """
    blob = str(text or "").lower()

    # Checked BEFORE the notice type, and only this one is. A results notice
    # inherits the type of the competition it reports on — the EHRP one is
    # typed "Request for Proposal" — so deferring to that field would present a
    # closed shortlist as an open solicitation. The phrases are narrow enough
    # to carry that precedence.
    if blob and any(p in blob for p in _RESULTS_NOTICE_PHRASES):
        return {
            "opportunity_kind": "results_notice",
            "kind_basis": "prose_results_phrase",
            "kind_note": (
                "Announces who already qualified. No solicitation document, "
                "nothing to bid, and the shortlist named in it is closed."),
        }

    nt = str(notice_type or "").strip().lower()
    kind = _NOTICE_KINDS.get(nt)
    if kind:
        result = {"opportunity_kind": kind, "kind_basis": "notice_type"}
        if kind == "qualification":
            result["kind_note"] = (
                "A supply arrangement, standing offer or invitation to qualify "
                "puts a supplier on a vehicle; it is not itself work. Work is "
                "competed later as call-ups against it, often with no public "
                "notice.")
        elif kind == "call_up":
            result["kind_note"] = (
                "A call-up competed among suppliers already on a vehicle. This "
                "IS work — but only bidders already holding the arrangement can "
                "win it.")
        elif kind == "information":
            result["kind_note"] = (
                "Market research, not a solicitation. Nothing to bid; the value "
                "is knowing the requirement is coming.")
        elif kind == "pre_awarded":
            result["kind_note"] = (
                "An award already intended for a named supplier. Biddable only "
                "by challenging the sole-source within the posting window.")
        return result

    cats = parse_categories(procurement_category)

    # Construction is settled by the publisher's category and nothing in the
    # prose may override it. Checked BEFORE the vehicle rules below because a
    # construction notice that happens to cite a procurement number must stay
    # construction — that ordering is load-bearing, not incidental.
    if cats and cats <= {_CATEGORY_CONSTRUCTION}:
        return {"opportunity_kind": "construction", "kind_basis": "procurement_category"}

    # The notice type said nothing decisive. Before falling back to the
    # goods/services split, see whether the prose names a vehicle — a call-up
    # typed as a plain "Request for Proposal" is otherwise indistinguishable
    # from open work, and the difference is whether we can bid at all.
    if blob:
        sa_numbers = {m.upper() for m in _SA_NUMBER.findall(str(text or ""))}
        if sa_numbers:
            sa = sorted(sa_numbers)[0]
            return {
                "opportunity_kind": "call_up",
                "kind_basis": "prose_arrangement_number",
                "kind_note": (
                    f"Cites supply arrangement {sa} "
                    f"({_KNOWN_SUPPLY_ARRANGEMENTS.get(sa, 'known vehicle')}). "
                    f"Work, but restricted to suppliers already holding that "
                    f"arrangement."),
            }
        vehicle = _VEHICLE_TOKENS.search(blob)
        if vehicle:
            return {
                "opportunity_kind": "call_up",
                "kind_basis": "prose_vehicle_name",
                "kind_note": (
                    f"Names the {vehicle.group(1).upper()} vehicle but files no "
                    f"arrangement number. Weaker evidence than a number — "
                    f"confirm against the notice before treating it as open."),
            }

    if _CATEGORY_GOODS in cats and not (cats & set(_CATEGORY_SERVICES)):
        return {
            "opportunity_kind": "product",
            "kind_basis": "procurement_category",
            "kind_note": ("Categorised goods-only: a purchase, not an engagement."),
        }
    if cats & set(_CATEGORY_SERVICES):
        return {
            "opportunity_kind": "solicitation",
            "kind_basis": "procurement_category_residual",
            "kind_note": ("Shape unresolved. Categorised as services, but a SaaS "
                          "or COTS purchase is routinely filed this way, and an "
                          "untyped call-up looks identical. Read the description."),
        }
    return {
        "opportunity_kind": "unknown",
        "kind_basis": "unclassified",
        "kind_note": ("Neither a notice type nor a usable procurement category "
                      "was filed. Shape unknown — read the description."),
    }
