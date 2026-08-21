"""
Ingest Canadian government tenders into ChromaDB, filtered by profile.

The filter is aggressive on purpose. The LLM Wiki pattern and hybrid agentic
retrieval both work better on a focused corpus than on a huge unfiltered one.
For a mid-size IT consulting firm, you might go from 2,800 active tenders
down to ~200-400 that actually match the profile.

Usage:
    python scripts/ingest.py
    python scripts/ingest.py --profile vault/profiles/my-company.md
    python scripts/ingest.py --force            # re-download even if cached
    python scripts/ingest.py --skip-unchanged   # no-op when the feed hasn't moved
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import re
import shutil
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import pandas as pd
import requests
import yaml


# The "open tender notices" file: active tenders only. There's also a
# "complete" file (tenderNoticeComplete-...) with every notice since 2022,
# but it's much larger and we filter to open tenders anyway.
TENDER_URL = "https://canadabuys.canada.ca/opendata/pub/openTenderNotice-ouvertAvisAppelOffres.csv"

# Canada Buys' WAF returns 403 for the default python-requests user agent.
# A browser-like UA is required for the download to succeed.
REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/csv,application/csv,*/*",
}
PROJECT_ROOT = Path(__file__).parent.parent
DEFAULT_PROFILE = PROJECT_ROOT / "vault" / "profiles" / "my-company.md"
DEFAULT_CACHE = PROJECT_ROOT / ".cache" / "tenders.csv"
DEFAULT_DB = PROJECT_ROOT / "chroma_db"


# ---------------------------------------------------------------------------
# Schema resolution — the guard, shared with the other ingest scripts
# ---------------------------------------------------------------------------
# Column names with fallbacks, resolved at runtime against the real header so a
# rename fails loudly with the actual column list instead of silently producing
# nothing. This exists because it didn't: the agency field read a column name
# that had never been in this file, `df.get(col, "")` returned the default, and
# every tender carried an empty agency for weeks without a single error.
TENDER_COLUMNS = {
    "tender_id": ["referenceNumber-numeroReference"],
    "title": ["title-titre-eng"],
    "description": ["tenderDescription-descriptionAppelOffres-eng"],
    "closing_date": ["tenderClosingDate-appelOffresDateCloture"],
    "contracting_entity": ["contractingEntityName-nomEntitContractante-eng"],
    "end_user": ["endUserEntitiesName-nomEntitesUtilisateurFinal-eng"],
    # The publisher's own classification. Preferred over guessing their
    # vocabulary from prose — see classify_notice and unspsc_relevance below.
    "notice_type": ["noticeType-avisType-eng"],
    "procurement_category": ["procurementCategory-categorieApprovisionnement"],
    "unspsc": ["unspsc"],
}
# The six the corpus cannot be built without. The three classification columns
# are deliberately NOT required: they are absent for whole source systems today
# (see UNCODED_SOURCE_SYSTEMS) and a feed that stopped publishing them should
# degrade to text matching with a loud funnel line, not hard-exit the ingest.
TENDER_REQUIRED = [
    "tender_id", "title", "description", "closing_date",
    "contracting_entity", "end_user",
]

# Source systems that file NO unspsc and NO noticeType, as of the 2026-08-04
# feed: MX 100 notices, PW 21, SSC 18 — 139 of 896. Recorded here because the
# gap is not random. SSC is the largest federal IT buyer and PW is PSPC; if
# either starts publishing codes that is a material improvement in this
# pipeline's precision, and the ingest funnel prints the live split every run so
# a change shows up on the next ingest rather than months later.
UNCODED_SOURCE_SYSTEMS = ("MX", "PW", "SSC")


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
    # supplier with a challenge window. "Directed Contract" is the same shape as
    # an ACAN — a sole-source already decided — and was landing in the
    # `solicitation` residual, which reads as open work.
    "request for information": "information",
    "advance contract award notice": "pre_awarded",
    "directed contract": "pre_awarded",
}

# All three qualification instruments put a supplier through a gate before any
# work is competed, but they do not open the same door, and one note for all
# three asserted a vehicle that a stage-1 ITQ does not have. Keyed on the same
# controlled-vocabulary literals as _NOTICE_KINDS, so this is a second lookup on
# a string the publisher filed — NOT a prose rule, and it adds no new way to be
# wrong that the notice type is not already wrong about.
_QUALIFICATION_NOTES = {
    "request for supply arrangement": (
        "A supply arrangement puts a supplier on a vehicle; it is not itself "
        "work. Work is competed later as call-ups against it, often with no "
        "public notice. Cross-reference vehicles.md."),
    "request for standing offer": (
        "A standing offer puts a supplier on a vehicle; it is not itself work. "
        "Work is drawn down later as call-ups against it, often with no public "
        "notice. Cross-reference vehicles.md."),
    "invitation to qualify": (
        "Qualifies a supplier through a gate; not itself work. Which gate is "
        "not in any structured field, and the two differ: an ITQ onto a vehicle "
        "or source list leads to call-ups over its life, while a stage-1 or "
        "phase-1 ITQ qualifies only for ONE named project's later stage — no "
        "vehicle, no call-ups, and nothing in vehicles.md to cross-reference. "
        "Read the notice to tell which."),
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


# ---------------------------------------------------------------------------
# Signals that are not in a structured field
# ---------------------------------------------------------------------------
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

    Imported lazily because crosswalk imports THIS module; a module-level import
    here would be circular.
    """
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return {}
    if not str(value).strip():
        return {}
    import crosswalk
    import org_resolve

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


def body_date_conflict(description, closing_date) -> Optional[str]:
    """
    A submission date stated in the prose that is EARLIER than the field.

    Notices amended on a third-party portal routinely carry "disregard the
    Ariba posting deadline, bids are due <date>" while the structured closing
    date still shows the original. Believing the field costs the bid, and
    nothing about it fails loudly. Only an earlier body date is reported: a
    later one is usually an extension already reflected elsewhere, and flagging
    those would bury the case that matters.
    """
    if not isinstance(description, str) or not closing_date:
        return None
    matches = _BODY_DATE.findall(description)
    if not matches:
        return None
    try:
        closing = datetime.strptime(str(closing_date)[:10], "%Y-%m-%d")
    except ValueError:
        return None
    for raw in matches:
        for fmt in ("%d %B %Y", "%B %d, %Y", "%B %d %Y"):
            try:
                stated = datetime.strptime(raw.strip().replace(",", " ").replace("  ", " "),
                                           fmt.replace(",", ""))
            except ValueError:
                continue
            # One day of slack absorbs timezone wording, not a real conflict.
            if (closing - stated).days > 1:
                return stated.strftime("%Y-%m-%d")
            break
    return None


# Only dates introduced as a submission deadline. A bare date anywhere in the
# prose is usually an amendment log or a period of performance.
_BODY_DATE = re.compile(
    r"(?:submitted|received|due|submission)\s+(?:on|by|no later than)\s+"
    r"(\d{1,2}\s+[A-Z][a-z]+\s+\d{4}|[A-Z][a-z]+\s+\d{1,2},?\s+\d{4})",
    re.IGNORECASE,
)


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
            result["kind_note"] = _QUALIFICATION_NOTES.get(nt, (
                "A supply arrangement, standing offer or invitation to qualify "
                "puts a supplier on a vehicle; it is not itself work. Work is "
                "competed later as call-ups against it, often with no public "
                "notice."))
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


def resolve_columns(
    columns: list[str],
    candidates: dict[str, list[str]],
    required: list[str],
    source_label: str,
) -> dict[str, str | None]:
    """
    Map logical field names to real header names, or exit(2) with the real list.

    Shared by every ingest script. Matching is case-insensitive so a header
    recased upstream doesn't count as a rename. Keys not in `required` resolve
    to None and the caller decides what an absent column means.
    """
    lower = {c.lower().strip(): c for c in columns}
    resolved: dict[str, str | None] = {}
    for key, cands in candidates.items():
        found = None
        for cand in cands:
            if cand.lower().strip() in lower:
                found = lower[cand.lower().strip()]
                break
        resolved[key] = found
    missing = [k for k in required if resolved[k] is None]
    if missing:
        sys.stderr.write(
            f"Schema mismatch in {source_label}. Could not find columns for: {missing}\n"
            "Columns present in the file:\n"
            + "\n".join(f"  {c}" for c in columns)
            + f"\nUpdate the column candidates in {source_label}.\n"
        )
        sys.exit(2)
    return resolved


# ---------------------------------------------------------------------------
# Output safety — shared by every ingest script
# ---------------------------------------------------------------------------

def output_path(default_path: Path, explicit: Path | None,
                sampling_reason: str | None) -> Path:
    """
    Decide where an ingest writes, keeping sampling runs off the real database.

    A flag whose purpose is to spot-check the pipeline on a subset must not be
    able to replace the corpus the rest of the repo reads. `--max-audits 20`
    truncated a 364-row oag.db to 20; `--source <trimmed csv>` emptied a
    33,196-row contracts.db. Both "worked" — they just destroyed the real data
    on the way. Sampling now redirects to a sibling .sample path, and
    overwriting the committed database takes an explicit --db.
    """
    if explicit is not None:
        return Path(explicit)
    if not sampling_reason:
        return default_path
    sample = default_path.with_name(default_path.stem + ".sample" + default_path.suffix)
    print(
        f"SAMPLING RUN ({sampling_reason}).\n"
        f"  Writing to     {sample}\n"
        f"  Leaving intact {default_path}\n"
        f"  Pass --db {default_path} to overwrite the real one deliberately."
    )
    return sample


@contextlib.contextmanager
def staged_db(db_path: Path):
    """
    Yield a SQLite connection to a .part file, published over db_path on success.

    Every ingest used to unlink its output and then build in place, so anything
    that failed in between — a schema mismatch, a dropped connection, a bad row
    — left no database at all. Here nothing touches db_path until the new
    database is complete and committed; os.replace is atomic for files on
    Windows and POSIX alike.

    On failure the .part is removed as well, so a failed run leaves the tree
    exactly as it found it rather than a stray half-written file for the next
    person to wonder about.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = db_path.with_name(db_path.name + ".part")
    tmp_path.unlink(missing_ok=True)
    con = sqlite3.connect(tmp_path)
    try:
        yield con
        con.commit()
    except BaseException:
        con.close()
        tmp_path.unlink(missing_ok=True)
        raise
    con.close()
    tmp_path.replace(db_path)


# ---------------------------------------------------------------------------
# Profile parsing — the frontmatter of the profile is the filter config
# ---------------------------------------------------------------------------

# Past this horizon a closing date is a placeholder meaning "this arrangement
# has no real close", not a date. 2065-04-27, 2076-12-31 and 2100-12-31 all
# appear in the live feed on standing arrangements. Rendering them as closing
# dates makes a permanent vehicle look like an imminent deadline, and computing
# a days-until figure from one yields a meaningless five-digit integer.
#
# Defined here rather than in tender_tools because both modules need it and the
# import already runs that way (tender_tools imports parse_profile from here).
SENTINEL_HORIZON_YEARS = 10


def closing_window(closing_date, imminent_within_days: int, today=None):
    """
    Classify a closing date into a window. Returns `(window, days_until_close)`.

    DERIVED, NEVER STORED. Both outputs depend on what day it is, so freezing
    them into ChromaDB at ingest would make them wrong by up to a week before
    the next run. The corpus keeps `closing_date`; this is computed against it
    at query time, which also means the threshold can change with no re-ingest.

    The five windows, and why each exists:

    - `closed`    — the date has passed. Only reachable from a corpus older than
                    today, which is the normal case: a briefing written three
                    days after an ingest needs to see that something expired.
    - `imminent`  — inside the profile threshold. These used to be deleted at
                    ingest; they are here now so the reader decides.
    - `open`      — comfortably open.
    - `standing`  — past SENTINEL_HORIZON_YEARS. A placeholder, not a deadline,
                    so days_until_close is None rather than a five-digit number.
    - `unknown`   — no parseable date. Not the same as closed, and not the same
                    as standing.

    `days_until_close` is None for `standing` and `unknown` — the two cases where
    an integer would be a fabrication.
    """
    if today is None:
        today = datetime.now().date()
    elif hasattr(today, "date") and not isinstance(today, date):
        today = today.date()

    if closing_date is None or closing_date == "":
        return "unknown", None
    try:
        if isinstance(closing_date, str):
            parsed = datetime.strptime(closing_date[:10], "%Y-%m-%d").date()
        elif hasattr(closing_date, "date"):
            if pd.isna(closing_date):
                return "unknown", None
            parsed = closing_date.date()
        else:
            parsed = closing_date
    except (ValueError, TypeError):
        return "unknown", None

    if parsed.year - today.year > SENTINEL_HORIZON_YEARS:
        return "standing", None

    days = (parsed - today).days
    if days < 0:
        return "closed", days
    if days < imminent_within_days:
        return "imminent", days
    return "open", days


def _imminence_threshold(fm: dict) -> int:
    """
    Days-until-close below which a notice is `imminent`.

    Accepts the old `min_days_until_close` key so a profile written before the
    rename keeps working, but says so once, loudly: the key did not just change
    name, it changed meaning. It used to delete those notices. Reading it
    silently would leave someone believing their corpus is still filtered.
    """
    if "imminent_within_days" in fm:
        return int(fm["imminent_within_days"])
    if "min_days_until_close" in fm:
        value = int(fm["min_days_until_close"])
        print(
            f"  NOTE: profile uses `min_days_until_close: {value}`, which is now "
            f"`imminent_within_days`.\n"
            f"        It no longer excludes anything — notices closing sooner "
            f"than {value} days now\n"
            f"        enter the corpus tagged `imminent` instead of being "
            f"dropped. Rename the key to silence this."
        )
        return value
    return 5


def parse_profile(profile_path: Path) -> dict:
    """
    Extract filter criteria from the YAML frontmatter of the profile markdown.

    We use the real YAML parser here (not a regex) because the profile supports
    lists and the user might format them differently over time.
    """
    content = profile_path.read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
    if not match:
        raise ValueError(f"No YAML frontmatter found in {profile_path}")

    fm = yaml.safe_load(match.group(1)) or {}
    return {
        # Absent by default: the profile ships these commented out because the
        # extractor behind them is unreliable. Left at 0 / 100M the value filter
        # deactivates itself in filter_tenders. See estimate_value.
        "value_min": int(fm.get("value_min", 0)),
        "value_max": int(fm.get("value_max", 100_000_000)),
        # UNSPSC family prefixes, hand-checked and committed. Discovered offline
        # with scripts/unspsc_discover.py; never joined at runtime.
        "unspsc_families": [str(f).strip() for f in fm.get("unspsc_families", [])],
        "competencies": [c.lower() for c in fm.get("competencies", [])],
        "exclude": [e.lower() for e in fm.get("exclude", [])],
        # Imminence threshold, NOT an exclusion. Renamed from
        # min_days_until_close, which described the behaviour this key used to
        # have: a hard drop of everything closing sooner. That drop removed
        # notices before the date-conflict detector ever read them, made the
        # briefing's "act now" section unreachable by construction, and deleted
        # a watched tender from the corpus in its final days. Now nothing is
        # excluded on it — it only decides what counts as `imminent`.
        "imminent_within_days": _imminence_threshold(fm),
        "contracts_window_years": int(fm.get("contracts_window_years", 3)),
        "contracts_categories": [c.lower() for c in fm.get("contracts_categories", [])],
        "expiry_min_value": float(fm.get("expiry_min_value", 0)),
        "plan_themes": fm.get("plan_themes", {}),
        "oag_themes": fm.get("oag_themes", {}),
    }


# ---------------------------------------------------------------------------
# Data download — revalidate rather than guess from a clock
# ---------------------------------------------------------------------------

def _validators_path(cache_path: Path) -> Path:
    """Where the publisher's cache validators for `cache_path` are recorded."""
    return cache_path.with_name(cache_path.name + ".http.json")


def _load_validators(cache_path: Path) -> dict:
    """ETag / Last-Modified recorded alongside the cached feed. {} when absent."""
    try:
        loaded = json.loads(_validators_path(cache_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def download_tenders(cache_path: Path, force: bool = False) -> pd.DataFrame:
    """
    Download the tender CSV, revalidating against the PUBLISHER rather than a clock.

    CanadaBuys serves this file from Azure Blob Storage, which sends `ETag` and
    `Last-Modified`. So "has the feed moved" is a question the publisher will
    answer directly, for the cost of one conditional request — and the answer is
    a fact about the data instead of an inference from how long ago we asked.
    That matters more the more often this runs: on a daily cadence a clock-based
    cache either re-downloads 6.7MB to learn nothing, or withholds a feed that
    did move, depending on which side of the TTL the run lands.

    A 304 deliberately leaves the cached file's mtime alone, which is what makes
    `_feed_mtime_iso` mean "when the data last arrived" rather than "when we last
    asked". The 12-hour TTL survives as the fallback for a response that carries
    no validators at all.

    A failed request RAISES rather than falling back to the cache. Serving stale
    bytes under a fresh build stamp is the one outcome the provenance states
    downstream cannot represent.
    """
    validators = {} if force else _load_validators(cache_path)
    headers = dict(REQUEST_HEADERS)

    if not force and cache_path.exists():
        if validators.get("etag"):
            headers["If-None-Match"] = validators["etag"]
        elif validators.get("last_modified"):
            headers["If-Modified-Since"] = validators["last_modified"]
        else:
            age = datetime.now() - datetime.fromtimestamp(cache_path.stat().st_mtime)
            if age < timedelta(hours=12):
                print(f"Using cached CSV ({age.total_seconds() / 3600:.1f}h old; "
                      f"no validator recorded, so this is the 12h fallback)")
                return pd.read_csv(cache_path, low_memory=False)

    conditional = "If-None-Match" in headers or "If-Modified-Since" in headers
    print("Checking Canada Buys for a newer tender feed..." if conditional
          else "Downloading open tender notices from Canada Buys (may take a minute)...")
    response = requests.get(TENDER_URL, headers=headers, timeout=180)

    if response.status_code == 304:
        print("  304 Not Modified — the published feed is the one already cached.")
        return pd.read_csv(cache_path, low_memory=False)

    response.raise_for_status()
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_bytes(response.content)

    # Recorded only on a body we actually stored, so the validators can never
    # describe a file other than the one on disk.
    _validators_path(cache_path).write_text(
        json.dumps({"etag": response.headers.get("ETag", ""),
                    "last_modified": response.headers.get("Last-Modified", ""),
                    "fetched_at": datetime.now().isoformat(timespec="seconds")},
                   indent=2) + "\n",
        encoding="utf-8", newline="\n")
    print(f"  downloaded {len(response.content):,} bytes")
    return pd.read_csv(cache_path, low_memory=False)


# ---------------------------------------------------------------------------
# Corpus identity — what a build was made FROM, by content
# ---------------------------------------------------------------------------

def _sha256_file(path: Optional[Path]) -> Optional[str]:
    """Content hash of a file, or None when there is no file to hash."""
    if path is None or not Path(path).exists():
        return None
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def corpus_identity(feed_path: Optional[Path],
                    profile_path: Optional[Path]) -> dict[str, str]:
    """
    The inputs this corpus was built from, identified by CONTENT.

    Two hashes, because the two answer different questions and a rebuild that
    changes one is a different event from a rebuild that changes the other. A
    new `feed_sha256` is new notices; a new `profile_sha256` is a re-scoring of
    the same ones. `feed_downloaded_at` cannot separate them — it moves whenever
    bytes arrive, including when the bytes are identical — which is tolerable at
    one run a week and is a daily false alarm at one run a day.

    Keys are OMITTED when unknown, never None, for the reason `_write_chroma`
    documents: Chroma raises on a None metadata value. An absent key means "no
    hash to compare", which is a different finding from "the hashes differ" and
    must stay distinguishable downstream.

    Deliberately NOT a hash of this file. A change to the filter code alters the
    corpus without altering either input, so `--skip-unchanged` would sit on a
    stale build after a deploy. Run without the flag when the code changes; the
    flag is for the scheduled case, where the code is fixed and the feed is not.
    """
    out: dict[str, str] = {}
    feed = _sha256_file(feed_path)
    if feed is not None:
        out["feed_sha256"] = feed
    profile = _sha256_file(profile_path)
    if profile is not None:
        out["profile_sha256"] = profile
    return out


# The identity of the build that produced a corpus, kept INSIDE the corpus
# directory. Two reasons it is a file rather than a read of the collection
# metadata that carries the same values. First, opening a ChromaDB client takes
# OS-level handles on the directory for the life of the process, and
# `build_chroma` renames that directory — reading the corpus to decide whether
# to replace it would break replacing it, on Windows, exactly as the comment
# there records. Second, it needs no chromadb import and no dependency on
# Chroma's storage schema.
#
# The collection metadata remains the portable record: this file is local and
# gitignored with the rest of chroma_db/, and only ever an optimisation. When it
# is missing the answer is "rebuild", which is the safe direction.
IDENTITY_FILENAME = "corpus-identity.json"


def stored_identity(db_path: Path) -> dict[str, str]:
    """Identity of the corpus already at `db_path`. {} when there isn't one."""
    try:
        loaded = json.loads(
            (db_path / IDENTITY_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


# ---------------------------------------------------------------------------
# Filtering — the core of "Option 3 hybrid": filter aggressively up front
# ---------------------------------------------------------------------------

# Regex for dollar amounts like "$1.5M", "$500,000", "$2 million"
_VALUE_PATTERN = re.compile(
    r"\$\s*(\d{1,3}(?:,\d{3})*(?:\.\d+)?)\s*(K|M|B|thousand|million|billion)?",
    re.IGNORECASE,
)
_VALUE_MULTIPLIERS = {
    "K": 1_000, "thousand": 1_000,
    "M": 1_000_000, "million": 1_000_000,
    "B": 1_000_000_000, "billion": 1_000_000_000,
}


def estimate_value(description: str) -> Optional[float]:
    """
    Grab the first dollar figure in a description. NOT USED BY DEFAULT.

    Retired from the default ingest path because measurement showed it is wrong
    far more often than it is right. On the 2026-08-04 feed only 94 of 896
    descriptions contain a dollar figure at all, and the first figure is
    routinely not the contract value: the most common extraction is $10,000,000
    from construction source lists reading "with an estimated value of $10
    million and below" — a ceiling on a qualification vehicle, not a price. The
    resulting distribution (median $10M, max $5B, min $0) does not describe the
    tenders it was attached to.

    It survives only as the per-tender fallback for --extract-values, where a
    model read the description first and the regex catches an API failure.
    Nothing else should call it. When no extractor runs, estimated_value is
    OMITTED from the metadata rather than stored as 0.0 — a real zero and an
    unknown must not render identically.
    """
    if not isinstance(description, str):
        return None
    match = _VALUE_PATTERN.search(description)
    if not match:
        return None
    amount_str, unit = match.group(1), match.group(2)
    value = float(amount_str.replace(",", ""))
    if unit:
        value *= _VALUE_MULTIPLIERS.get(unit.upper() if len(unit) <= 1 else unit.lower(), 1)
    return value


# ---------------------------------------------------------------------------
# LLM value extraction (optional, --extract-values)
# ---------------------------------------------------------------------------
# The regex above grabs the FIRST dollar amount in the description, which is
# often the contract value but sometimes a bond amount, an insurance minimum,
# or an unrelated figure. The LLM pass reads the description and extracts the
# actual estimated contract value, or null if none is stated.
#
# Opt-in by design: the default ingest path needs zero credentials so the
# repo stays clonable-and-runnable. With the flag, set ANTHROPIC_API_KEY.
# Cost at ~300 tenders with a small model: a few cents per ingest.

def make_llm_value_extractor():
    """
    Return a callable(description) -> Optional[float] backed by the Anthropic
    API. Falls back to the regex extractor per-tender on any failure, so a
    flaky network degrades gracefully rather than killing the ingest.
    """
    import json
    import os

    import anthropic

    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit(
            "--extract-values requires the ANTHROPIC_API_KEY environment variable"
        )
    client = anthropic.Anthropic()

    def extract(description: str) -> Optional[float]:
        if not isinstance(description, str) or not description.strip():
            return None
        try:
            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=100,
                messages=[{
                    "role": "user",
                    "content": (
                        "Extract the estimated total contract value in CAD from "
                        "this government tender description. Ignore bond amounts, "
                        "insurance minimums, penalty figures, and per-unit prices. "
                        "Respond with ONLY a JSON object, no other text: "
                        '{"contract_value": <number or null>}\n\n'
                        f"Description:\n{description[:1500]}"
                    ),
                }],
            )
            text = response.content[0].text.strip()
            # Strip accidental code fences before parsing
            text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
            value = json.loads(text).get("contract_value")
            return float(value) if value is not None else None
        except Exception as exc:  # noqa: BLE001 — deliberate broad fallback
            print(f"    LLM extraction failed ({type(exc).__name__}), regex fallback")
            return estimate_value(description)

    return extract


def matched_competencies(text: str, competencies: list[str]) -> list[str]:
    # Word-boundary matching, not bare substring: "aws" must not match inside
    # "flaws", "withdrawals", or the French "travaux". The old substring version
    # inflated the corpus with archaeology and bridge tenders that merely
    # contained the letters a-w-s, and surfaced a $3.75M exhibits contract as a
    # top "AWS" result in the digest. Multi-word competencies like
    # "it modernization" still match as a phrase with boundaries at each end.
    text_lower = text.lower()
    matched = []
    for c in competencies:
        pattern = r"\b" + re.escape(c.lower()) + r"\b"
        if re.search(pattern, text_lower):
            matched.append(c)
    return matched


def contains_excluded(text: str, exclusions: list[str]) -> bool:
    text_lower = text.lower()
    return any(excl in text_lower for excl in exclusions)


def matches_unspsc_families(codes: set[str], families: list[str]) -> list[str]:
    """
    Return the profile families a notice's UNSPSC codes fall under.

    Prefix match against the hand-checked family list: '8111' catches every
    8111xxxx code. Families are committed config, discovered offline with
    scripts/unspsc_discover.py — this never touches the PSPC reference file at
    runtime, and never the GSIN linkage at all.
    """
    if not families:
        return []
    hits = {fam for code in codes for fam in families if code.startswith(fam)}
    return sorted(hits)


def _source_system(tender_id) -> str:
    """The publishing system's prefix on a reference number: MX, PW, SSC, WS, cb."""
    m = re.match(r"^([A-Za-z]+)", str(tender_id or ""))
    return m.group(1) if m else "?"


def filter_tenders(df: pd.DataFrame, criteria: dict, cols: dict,
                   value_extractor=None, as_of=None) -> pd.DataFrame:
    """
    Apply profile filters. Prints a funnel so you can tune the profile.

    THE STAGE DECISIONS LIVE IN filter_audit.predicates, not inline here. Each
    drop below calls the same stage function the audit replays, so the corpus
    and the audit cannot come to disagree about what the filter does — the same
    argument notices_ingest.py makes about sharing the classifiers. The
    narrowing sequence is unchanged, so every funnel count still means what it
    meant: a stage only ever sees the rows earlier stages let through.

    Imported lazily, because filter_audit.predicates imports THIS module and a
    module-level import here would be circular. Same reason entity_org_keys
    imports crosswalk lazily.

    as_of: the date "closed" is judged against. None means datetime.now().date(),
    which is what production has always done. It is an argument now because a
    predicate that reads a wall clock cannot be replayed, and because the
    equivalence check needs both paths on one clock — see
    filter_audit/equivalence.py.

    Relevance is decided by the publisher's UNSPSC classification where there is
    one, and falls back to keyword matching where there isn't. The fallback is
    not a nicety: three source systems file no UNSPSC at all (MX, PW, SSC — 139
    of 896 notices on the 2026-08-04 feed), and one of them is Shared Services
    Canada, the largest federal IT buyer. Gating on UNSPSC would silently drop
    every SSC notice. Every run prints the live split so a fourth uncoded
    system, or SSC starting to publish codes, shows up here rather than months on.

    value_extractor: callable(description) -> Optional[float], or None. None
    means no value is extracted and none is stored — see estimate_value.
    """
    from filter_audit import predicates as _pred

    print(f"\nStarting with {len(df):,} tenders")

    # Parse closing dates (timezone-naive for simplicity). THE definition lives
    # in filter_audit.predicates so the replay parses identically — pandas
    # infers a format from the first element and coerces the rest to NaT, so a
    # second implementation would disagree on any feed that is not
    # format-uniform, silently and in the "kept, tagged unknown" direction.
    df = df.copy()
    df["_closing"] = _pred.parse_closing_days(df[cols["closing_date"]])

    # Drop only what is dead. Everything still open enters and is tagged; the
    # reader decides whether a near-close notice is worth acting on.
    #
    # TWO BUGS LIVED IN THE THREE LINES THIS REPLACES, and both were invisible:
    #
    # 1. It dropped everything closing within `min_days_until_close`, which ran
    #    BEFORE body_date_conflict below — so a notice closing in eight days was
    #    never read for a conflicting prose deadline, and a tender in watching/
    #    vanished from the corpus in its final days. It also made the briefing's
    #    7-day "act now" section unreachable against a 10-day cutoff.
    #
    # 2. It compared `datetime.now() + timedelta(...)` against a full timestamp,
    #    so the cutoff was a wall-clock INSTANT, not a date. Notices closing at
    #    14:00 on the boundary day survived a 09:00 ingest and were dropped by a
    #    15:00 one — same feed, same day, 53 notices versus 48. Corpus size was
    #    not reproducible within a day, which quietly put noise into every
    #    week-over-week diff and every gating denominator recorded from them.
    #
    # Hence `.dt.normalize()` on both sides: "ten days out" is a count of days,
    # never a time of day.
    effective_as_of = as_of or datetime.now().date()
    closing_day = df["_closing"].dt.normalize()

    # NaT is not "closed". Comparison against NaT is False either way, so the
    # old expression dropped undated notices as a side effect of the arithmetic
    # rather than as a decision. There are none in the current feed; that is a
    # property of today's data, not a guarantee. Keep them and say so.
    #
    # Stage 1 in filter_audit.predicates.stage_closed.
    undated = closing_day.isna()
    before = len(df)
    keep = _pred.stage_mask(df, cols, criteria, _pred.stage_closed, effective_as_of)
    df = df[pd.Series(keep, index=df.index)]
    closed = before - len(df)
    if closed:
        print(f"  After dropping closed notices: {len(df):,}  ({closed:,} already closed)")
    if int(undated.sum()):
        print(f"    {int(undated.sum()):,} notice(s) have no parseable closing date — "
              f"kept, tagged unknown")

    # Combined text for matching (title + description)
    df["_text"] = (
        df[cols["title"]].fillna("").astype(str) + " "
        + df[cols["description"]].fillna("").astype(str)
    )

    # Exclusions first (cheap, removes obvious misfits).
    # Stage 2 in filter_audit.predicates.stage_exclusion.
    if criteria["exclude"]:
        keep = _pred.stage_mask(df, cols, criteria, _pred.stage_exclusion)
        df = df[pd.Series(keep, index=df.index)]
        print(f"  After exclusion filter: {len(df):,}")

    # --- Instrument shape, from the publisher's structured fields -----------
    # Classify BEFORE filtering on relevance, so the funnel can report the mix
    # and so construction can be dropped on the publisher's own category rather
    # than on words in a title.
    nt_col = cols.get("notice_type")
    cat_col = cols.get("procurement_category")
    df["_kind"] = [
        classify_notice(
            row[nt_col] if nt_col else None,
            row[cat_col] if cat_col else None,
            row["_text"],
        )
        for _, row in df.iterrows()
    ]
    df["_opportunity_kind"] = df["_kind"].apply(lambda k: k["opportunity_kind"])

    # Jurisdiction, from the organization registry. Not a keyword list — the
    # registry is the authority on what is a federal organization, and a miss
    # is reported as unrecognised rather than assumed non-federal.
    df["_jurisdiction"] = [
        classify_jurisdiction(
            row[cols["contracting_entity"]], row[cols["end_user"]]
        )
        for _, row in df.iterrows()
    ]

    # A submission deadline in the prose that is earlier than the closing date
    # field. Rare and costly: believing the field loses the bid.
    df["_date_conflict"] = [
        body_date_conflict(
            row[cols["description"]],
            row["_closing"].strftime("%Y-%m-%d") if pd.notna(row["_closing"]) else None,
        )
        for _, row in df.iterrows()
    ]

    # Construction is dropped outright. procurementCategory is populated on 100%
    # of notices across every source system, which makes this the one filter
    # here that never falls back to guessing.
    # Stage 3 in filter_audit.predicates.stage_construction.
    before = len(df)
    keep = _pred.stage_mask(df, cols, criteria, _pred.stage_construction)
    df = df[pd.Series(keep, index=df.index)]
    if before != len(df):
        print(f"  After construction drop (*CNST): {len(df):,}  "
              f"({before - len(df):,} dropped)")

    # Provincial and territorial notices are dropped; `unrecognised` is NOT,
    # because federal Crown corporations land there and one of them is the best
    # tender in the corpus.
    # Stage 4 in filter_audit.predicates.stage_jurisdiction.
    before = len(df)
    juris = df["_jurisdiction"].apply(lambda j: j["jurisdiction"])
    dropped_names = sorted({
        str(v) for v in df.loc[juris == "non_federal", cols["contracting_entity"]]
    })
    keep = _pred.stage_mask(df, cols, criteria, _pred.stage_jurisdiction)
    df = df[pd.Series(keep, index=df.index)]
    if before != len(df):
        print(f"  After non-federal drop: {len(df):,}  "
              f"({before - len(df):,} dropped — {'; '.join(dropped_names)[:70]})")

    # --- Relevance: publisher classification, else keywords -----------------
    unspsc_col = cols.get("unspsc")
    families = criteria["unspsc_families"]
    df["_unspsc"] = (df[unspsc_col].apply(parse_unspsc_codes) if unspsc_col
                     else [set() for _ in range(len(df))])
    df["_unspsc_families"] = df["_unspsc"].apply(
        lambda codes: matches_unspsc_families(codes, families)
    )
    df["_matched"] = df["_text"].apply(
        lambda t: matched_competencies(t, criteria["competencies"])
    )

    has_codes = df["_unspsc"].apply(bool)
    coded_n, uncoded_n = int(has_codes.sum()), int((~has_codes).sum())
    print(f"  UNSPSC present for {coded_n:,}/{len(df):,}; "
          f"{uncoded_n:,} classified via procurementCategory + keywords")
    if uncoded_n:
        uncoded_systems = (
            df.loc[~has_codes, cols["tender_id"]].apply(_source_system)
            .value_counts().to_dict()
        )
        known = ", ".join(f"{k} {v}" for k, v in sorted(uncoded_systems.items()))
        print(f"    uncoded source systems: {known}")
        surprises = set(uncoded_systems) - set(UNCODED_SOURCE_SYSTEMS)
        if surprises:
            print(f"    NOTE: {sorted(surprises)} newly uncoded — not in "
                  f"UNCODED_SOURCE_SYSTEMS. Worth a look.")
        for expected in UNCODED_SOURCE_SYSTEMS:
            if expected not in uncoded_systems:
                print(f"    NOTE: {expected} now files UNSPSC codes. Its notices "
                      f"are classified by the publisher rather than by keyword.")

    # Publisher first, keywords only where the publisher classified nothing.
    #
    # NOT an OR across everything. When a notice carries UNSPSC codes, the
    # publisher has already said what commodity it is, and a word match that
    # overrides them is exactly the guessing this filter exists to stop: on the
    # 2026-08-04 feed the OR form readmitted a boiling-liquid-expanding-vapour-
    # explosion study (coded 77101501, environmental) because the phrase
    # "vapour cloud" contains "cloud", plus an elevator modernization and an
    # advertising RFSA. Codes present and not ours means not ours.
    #
    # Where no codes were filed there is nothing to defer to, so keywords carry
    # those notices — 37 of 431, entirely from MX, PW and SSC.
    # Stage 5 in filter_audit.predicates.stage_relevance, which keeps the two
    # reject modes apart — "coded into a family we don't buy" and "uncoded and
    # no keyword fired" imply different fixes and split 21,471 / 6,121 on the
    # archive. This funnel line reports only the survivors; the audit reports
    # both failures.
    if families or criteria["competencies"]:
        keep = _pred.stage_mask(df, cols, criteria, _pred.stage_relevance)
        relevant = pd.Series(keep, index=df.index)
        df = df[relevant]
        kept_coded = int((has_codes & relevant).sum())
        kept_uncoded = int((~has_codes & relevant).sum())
        print(f"  After relevance filter: {len(df):,}  "
              f"({kept_coded:,} by UNSPSC family, {kept_uncoded:,} by keyword "
              f"where no codes were filed)")

    # --- Value: retired from the default path -------------------------------
    # No step is printed when nothing is filtered. A funnel line that always
    # passes everything reads as a step that decided something, and this one
    # decided nothing for months while every stored value was 0.0.
    if value_extractor is None:
        df["_value"] = None
        present = int(df[cols["description"]].apply(estimate_value).notna().sum())
        print(f"  Value present in {present:,}/{len(df):,}; filter inactive "
              f"(unreliable — see estimate_value; --extract-values to enable)")
    else:
        print(f"  Extracting values for {len(df):,} tenders...")
        df["_value"] = df[cols["description"]].apply(value_extractor)
        if criteria["value_min"] > 0 or criteria["value_max"] < 100_000_000:
            in_range = df["_value"].isna() | (
                (df["_value"] >= criteria["value_min"])
                & (df["_value"] <= criteria["value_max"])
            )
            df = df[in_range]
            print(f"  After value filter (${criteria['value_min']:,}-"
                  f"${criteria['value_max']:,}): {len(df):,}")

    kinds = df["_opportunity_kind"].value_counts().to_dict()
    print(f"\nFinal: {len(df):,} tenders")
    print("  by instrument shape: "
          + ", ".join(f"{k} {v}" for k, v in sorted(kinds.items(), key=lambda x: -x[1])))

    # How each call-up was recovered. Reported separately because the three
    # bases are not equally strong and the weakest one carries the most rows.
    bases = df.loc[df["_opportunity_kind"] == "call_up", "_kind"].apply(
        lambda k: k["kind_basis"]).value_counts().to_dict()
    if bases:
        print("  call-ups by evidence: "
              + ", ".join(f"{k} {v}" for k, v in sorted(bases.items())))

    juris = df["_jurisdiction"].apply(lambda j: j["jurisdiction"]).value_counts().to_dict()
    print("  by jurisdiction: "
          + ", ".join(f"{k} {v}" for k, v in sorted(juris.items(), key=lambda x: -x[1])))

    # A SNAPSHOT, and labelled as one. The window is derived at query time from
    # closing_date, so this line describes the corpus on the day it was built
    # and drifts afterwards. Printed because the imminent count is the number
    # that used to be silently deleted, and it should be visible every run.
    threshold = criteria["imminent_within_days"]
    windows = df["_closing"].apply(
        lambda c: closing_window(c, threshold)[0]
    ).value_counts().to_dict()
    order = ["imminent", "open", "standing", "unknown", "closed"]
    print(f"  closing window at ingest (<{threshold} days = imminent): "
          + ", ".join(f"{k} {windows[k]}" for k in order if k in windows))

    conflicts = df["_date_conflict"].notna().sum()
    if conflicts:
        print(f"  DATE CONFLICTS: {conflicts} notice(s) state a submission "
              f"deadline in the description EARLIER than the closing_date field")
        for _, row in df[df["_date_conflict"].notna()].iterrows():
            print(f"    body says {row['_date_conflict']}, field says "
                  f"{row['_closing'].strftime('%Y-%m-%d')} — "
                  f"{str(row[cols['title']])[:52]}")
    return df


# ---------------------------------------------------------------------------
# ChromaDB persistence — this is what Claude's tools read from
# ---------------------------------------------------------------------------

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

    Still a LOCAL fact, and that is its limit: two machines that downloaded the
    same bytes an hour apart carry different values here, so this date orders
    events on one machine and cannot compare two. `feed_sha256` is what does
    that — see corpus_identity. Kept because "when did this arrive" is a real
    question that a hash cannot answer, and because a 304 now leaves it alone,
    which makes it mean when the data last arrived rather than when we last asked.

    Returns None rather than raising: --cache can point anywhere, and a test
    harness that drives build_chroma directly has no feed at all.
    """
    if feed_path is None or not feed_path.exists():
        return None
    return datetime.fromtimestamp(
        feed_path.stat().st_mtime).isoformat(timespec="seconds")


def build_chroma(df: pd.DataFrame, db_path: Path, cols: dict,
                 feed_path: Optional[Path] = None,
                 identity: Optional[dict] = None) -> None:
    """
    Embed filtered tenders and write to a persistent ChromaDB collection.

    `feed_path` is the CSV this run read, recorded as provenance. Optional and
    keyword-defaulted because tests drive this directly with no feed on disk;
    absent means the corpus is stamped with a build time and no feed date,
    which is a different fact from an unstamped corpus.

    `identity` is `corpus_identity()` for this build — content hashes of the
    inputs. Also optional and for the same reason, and an absent hash stays
    absent rather than becoming a placeholder.
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
        _write_chroma(df, db_path, cols, feed_path, identity)
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

    # AFTER the build succeeds, never before. This file is what --skip-unchanged
    # trusts, so it must not be able to describe a corpus that was never
    # completed — an absent file costs a rebuild, a premature one costs a
    # silently stale corpus.
    (db_path / IDENTITY_FILENAME).write_text(
        json.dumps(identity or {}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8", newline="\n")

    if retired is not None:
        shutil.rmtree(retired, ignore_errors=True)


def _write_chroma(df: pd.DataFrame, db_path: Path, cols: dict,
                  feed_path: Optional[Path] = None,
                  identity: Optional[dict] = None) -> None:
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

    # The content hashes ride alongside the timestamps rather than replacing
    # them. A timestamp says when something happened here; a hash says which
    # bytes it happened to, and only the second one is comparable across two
    # machines that downloaded the same feed at different moments. Already
    # omit-when-unknown by construction — see corpus_identity.
    provenance.update(identity or {})

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


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument(
        "--db", type=Path, default=None,
        help=f"Output corpus path (default {DEFAULT_DB}). Pointing --cache at a "
             "non-default CSV redirects output to a .sample path unless this is "
             "given, so a spot-check can't replace the real corpus.",
    )
    parser.add_argument("--force", action="store_true",
                        help="Re-download CSV unconditionally, ignoring both the "
                             "publisher's validators and the cache")
    parser.add_argument(
        "--skip-unchanged", action="store_true",
        help="Exit 0 without rebuilding when the feed and the profile are both "
             "byte-identical to what the existing corpus was built from. For "
             "scheduled runs. Does NOT notice a change to this script, so run "
             "without it after a code change.",
    )
    parser.add_argument(
        "--status-file", type=Path, default=None,
        help="Write `rebuilt` or `unchanged` here. Lets a scheduled job decide "
             "whether there is anything to digest without parsing stdout, and "
             "without this script knowing what a CI runner is.",
    )
    parser.add_argument(
        "--extract-values",
        action="store_true",
        help="Use the Anthropic API for contract-value extraction instead of the "
             "first-dollar-amount regex. Requires ANTHROPIC_API_KEY. Costs a few "
             "cents per ingest; noticeably more accurate.",
    )
    args = parser.parse_args()

    if not args.profile.exists():
        print(f"Profile not found: {args.profile}", file=sys.stderr)
        sys.exit(1)

    db_path = output_path(
        DEFAULT_DB, args.db,
        f"--cache points at {args.cache}, not the default"
        if args.cache != DEFAULT_CACHE else None,
    )

    criteria = parse_profile(args.profile)
    print(f"Profile: {args.profile}")
    print(f"  UNSPSC families: {criteria['unspsc_families'] or '(none — keywords only)'}")
    print(f"  Competencies: {criteria['competencies']}")
    print(f"  Exclusions: {criteria['exclude']}")
    # Only announce a value range when one is actually going to be applied.
    if args.extract_values and (criteria["value_min"] > 0
                                or criteria["value_max"] < 100_000_000):
        print(f"  Value range: ${criteria['value_min']:,} – ${criteria['value_max']:,}")

    value_extractor = make_llm_value_extractor() if args.extract_values else None
    if args.extract_values:
        print("  Value extraction: LLM (Anthropic API)")

    def record_status(status: str) -> None:
        """Report the outcome, if anyone asked to be told."""
        if args.status_file is not None:
            args.status_file.parent.mkdir(parents=True, exist_ok=True)
            args.status_file.write_text(status + "\n", encoding="utf-8", newline="\n")

    df = download_tenders(args.cache, force=args.force)

    identity = corpus_identity(args.cache, args.profile)
    if args.skip_unchanged:
        previous = stored_identity(db_path)
        # Both hashes must be present on both sides. A missing hash is "nothing
        # to compare", and treating that as agreement is how a corpus built
        # before this flag existed would be declared current forever.
        keys = ("feed_sha256", "profile_sha256")
        comparable = all(previous.get(k) and identity.get(k) for k in keys)
        if comparable and all(previous[k] == identity[k] for k in keys):
            print(f"\nUnchanged: the feed and profile are byte-identical to the "
                  f"corpus already at {db_path}.\n"
                  f"  feed    {identity['feed_sha256'][:12]}\n"
                  f"  profile {identity['profile_sha256'][:12]}\n"
                  f"Nothing rebuilt, and no build stamp moved. This is a "
                  f"successful run.")
            record_status("unchanged")
            return
        if not comparable:
            print("\n--skip-unchanged: no comparable identity on the existing "
                  "corpus, so rebuilding. (Expected once, on the first run "
                  "after this flag was added.)")

    cols = resolve_columns(
        list(df.columns), TENDER_COLUMNS, TENDER_REQUIRED, "scripts/ingest.py"
    )
    df = filter_tenders(df, criteria, cols, value_extractor=value_extractor)

    if len(df) == 0:
        print("\nNo tenders passed the filter. Loosen your criteria.", file=sys.stderr)
        sys.exit(1)

    build_chroma(df, db_path, cols, feed_path=args.cache, identity=identity)
    record_status("rebuilt")
    print("\nDone. Claude Code can now search this corpus via scripts/tender_tools")


if __name__ == "__main__":
    main()
