"""
The pre-mortem: assume this tender already went wrong, and assemble what explains it.

Two questions, never merged, because they pull in opposite directions and the
same fact answers them differently. `bid_and_lost` asks what stops us winning:
eligibility gates, an incumbent, a named shortlist, a bar we don't clear.
`won_and_regretted` asks what makes winning bad: seat-based pricing, a product
we'd have to front, a multi-year tail on work outside what we deliver. A 70/30
technical/price split is reassuring under the first and says nothing under the
second; a five-year option tail is attractive under the first and is the whole
risk under the second. Averaging them would destroy exactly the distinction the
exercise exists to draw.

THIS ASSEMBLES; IT DOES NOT SCORE. Same rule as the dossier, and it binds harder
here: a pre-mortem is where a number would be most tempting and most corrosive,
because "risk: 7/10" is a verdict that ends the reasoning it was supposed to
start. There is no severity, no ranking, and no ordering by anything but the
notice's own structure. A tally of fired probes is a score with the arithmetic
hidden, so that is not reported either.

THE TOOL READS THE NOTICE; IT DOES NOT INTERPRET THE PROFILE. Probes match the
notice text and quote the sentence they matched. The profile's own limits are
quoted verbatim beside them, section by section, and the two are never compared
in code. That keeps the judgment where the architecture puts it, and it means a
swapped profile retargets the output without touching this file — the probes
are about what federal solicitations say, not about who is reading them.

A PROBE THAT DOES NOT FIRE IS REPORTED, and as its own state. `absent` means the
phrase is not in the notice text, which is a summary; `fired` means it is.
Neither is a finding about the solicitation package, which is where mandatory
requirements, evaluation grids and security schedules actually live and which
this project deliberately cannot fetch. Collapsing `absent` into silence is how
a notice with no clearance language reads as work with no clearance requirement.

FALSE POSITIVES ARE THE CHEAP FAILURE AND ARE PREFERRED. Every fired probe
carries the sentence that fired it, so a match on the wrong sense — the
"vapour cloud" error the relevance filter is built around — costs a reader one
sentence to dismiss. A missed gate costs a bid. Patterns are therefore broad,
and no probe here may drop a notice or change any other section.

LOBBYING IS DELIBERATELY ABSENT, and its absence is the load-bearing decision in
this file. vault/CLAUDE.md forbids offering a meeting as the explanation for an
award or a requirement, and warns that adjacency to sources that DO support
inference is the trap. A pre-mortem is that trap in its purest form: the
question is literally "why did we lose", and a section listing who had been in
the room would be read as the answer whatever caveat sat above it. There is no
flag to turn it on. Run `lobbying-signals` or `dossier` when the department is
the question; those present it as presence, which is all it is.
"""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime

import org_resolve

from . import corpus, paths
from .company_profile import _window_fields
from .corpus import load_collection
from .documents import _attachment_dir, _note_for_tender
from .entities import _entity_attribution
from .provenance import _corpus_provenance
from .text import _display_agency

# The two lenses. Strings rather than an enum because they are keys in JSON a
# model reads, and the value it sees should say what it means.
LOST = "bid_and_lost"
REGRETTED = "won_and_regretted"


# Each probe is data, not a branch. `means` is the whole reason a probe is worth
# reporting — a fired pattern with no statement of what it does and does not
# establish is a keyword hit dressed as an insight, and a reader cannot tell the
# two apart. Every `means` names what the match would have to be confirmed
# against, because none of these is confirmable from a notice summary alone.
_PROBES: tuple[dict, ...] = (
    {
        "id": "named_suppliers",
        "lens": LOST,
        "looking_for": "A shortlist that already exists",
        "patterns": [
            r"\bACAN\b", r"advance contract award", r"invited suppliers?",
            r"selected suppliers?", r"statements? of capabilit",
            r"pre-?qualified suppliers?", r"selective competitive",
        ],
        "means": "The competition may be running among firms that were in the "
                 "conversation before this notice existed. Read who is named and "
                 "when they qualified — bidding from outside a list the buyer "
                 "will not update is a different proposition from an open field.",
    },
    {
        "id": "set_aside",
        "lens": LOST,
        "looking_for": "Work reserved for a defined group of suppliers",
        "patterns": [
            r"set[- ]aside", r"Procurement Strategy for Indigenous Business",
            r"\bPSIB\b", r"Indigenous business", r"reserved for",
        ],
        "means": "A set-aside is not a fit question. No amount of capability "
                 "reaches work reserved for suppliers we are not among, so this "
                 "is checked before anything else is worth reading.",
    },
    {
        "id": "vehicle_named",
        "lens": LOST,
        "looking_for": "A supply arrangement or standing offer named in the prose",
        "patterns": [
            r"\bTBIPS\b", r"\bSBIPS\b", r"\bProServices\b",
            r"supply arrangement", r"standing offer", r"\bRFSA\b",
            r"call-?up", r"EN578-\d+", r"EN537-[\w/]+",
        ],
        "means": "Only holders of a vehicle can bid work competed against it. "
                 "The vehicles section carries what vault/reference/vehicles.md "
                 "records as held; that file, not this probe, is the authority "
                 "on eligibility.",
    },
    {
        "id": "mandatory_experience",
        "lens": LOST,
        "looking_for": "A past-performance or corporate-experience bar",
        "patterns": [
            r"years? of experience", r"past performance", r"corporate experience",
            r"similar (?:projects?|contracts?|engagements?)",
            r"mandatory (?:requirements?|criteria)",
            r"must have (?:completed|delivered)", r"reference projects?",
        ],
        "means": "The commonest way a technically capable bid scores zero. The "
                 "notice rarely states the threshold and the package does. Where "
                 "the profile records no federal past performance, this is the "
                 "probe that decides whether that gap is fatal here.",
    },
    {
        "id": "evaluation_basis",
        "lens": LOST,
        "looking_for": "How the bid will actually be scored",
        "patterns": [
            r"lowest (?:priced?|cost)", r"highest combined rating",
            r"technical merit", r"best value", r"points? (?:will be )?awarded",
            r"\d{1,3}\s?%\s*(?:for )?(?:technical|price)",
        ],
        "means": "A price-weighted evaluation and a merit-weighted one are "
                 "different competitions against different fields. Quote the "
                 "split rather than assuming it: where the notice gives one it "
                 "is usually accurate, and it is usually the only part of the "
                 "evaluation visible before the package is downloaded.",
    },
    {
        "id": "seat_based_pricing",
        "lens": REGRETTED,
        "looking_for": "Work priced by the person rather than by the outcome",
        # The counted-body forms were added after this probe stayed silent on
        # "15 resources in various TBIPS positions, part-time based (150 days
        # per resource)" — which is the profile's central exclusion stated about
        # as plainly as a notice ever states it. `resource categor` and
        # `person-days` are the vocabulary of a supply arrangement's rate card;
        # a notice buying seats directly counts them instead.
        "patterns": [
            r"resource categor", r"\blevel [123]\b", r"per diem", r"day rate",
            r"hourly rate", r"task authoriz", r"person-?days?",
            r"staff augmentation", r"consultant categor",
            r"\d+\s+resources?\b", r"per resource", r"\bdays? per\b",
            r"resource\s+(?:positions?|levels?)", r"\bbody shop\b",
        ],
        "means": "The shape the profile most often excludes outright. It is a "
                 "regret risk rather than a loss risk: bids like this are "
                 "winnable, and winning one commits the firm to selling seats "
                 "for the term.",
    },
    {
        "id": "clearance_level",
        "lens": REGRETTED,
        "looking_for": "The security level the work is carried out at",
        "patterns": [
            r"\bsecret\b", r"top secret", r"\bNATO\b", r"\bSRCL\b",
            r"security clearance", r"\bProtected [ABC]\b", r"reliability status",
            r"personnel security", r"facility security",
        ],
        "means": "READ THE LEVEL, NOT THE WORD. Protected A or B is not Secret, "
                 "and an SRCL appears on both — one notice in this vault was "
                 "nearly skipped on the word SRCL when its actual bar was "
                 "Protected A. The quoted sentence carries the level; the "
                 "profile's own line on clearance is quoted beside it.",
    },
    {
        "id": "product_supply",
        "lens": REGRETTED,
        "looking_for": "A product the bidder is expected to already have",
        "patterns": [
            r"commercially available", r"\bCOTS\b", r"off-?the-?shelf",
            r"software licen[cs]e", r"subscription", r"\bSaaS\b",
            r"supply and deliver", r"vendor'?s (?:product|solution|platform)",
        ],
        "means": "Winning work that wants a product we do not own means fronting "
                 "somebody else's, which is a partnership decision taken on the "
                 "bid clock rather than a delivery decision taken afterwards. "
                 "Distinguish a product the buyer licences directly from a "
                 "platform the bidder must bring.",
    },
    {
        "id": "term_and_options",
        "lens": REGRETTED,
        "looking_for": "How long winning commits us for",
        "patterns": [
            r"option periods?", r"irrevocable", r"extend the contract",
            r"initial (?:one|two|three|1|2|3)[- ]year",
            r"\b\d\s*\+\s*\d\b", r"ongoing (?:support|maintenance)",
        ],
        "means": "The tail is the part that gets regretted, not the first year. "
                 "An option tail on work inside the profile is a foothold; the "
                 "same tail on work outside it is what is still there in year "
                 "four.",
    },
    {
        "id": "language_obligation",
        "lens": REGRETTED,
        "looking_for": "A bilingual delivery obligation",
        "patterns": [
            r"bilingual", r"official languages", r"in French and English",
            r"French-language",
        ],
        "means": "The profile treats bilingual capability as an asset rather "
                 "than a constant, so whether this is mandatory or merely rated "
                 "changes what it costs. The notice usually does not say which; "
                 "the package does.",
    },
)


def _sentences(text: str) -> list[str]:
    """
    The notice broken into quotable units.

    Splits on sentence punctuation AND on newlines, because feed descriptions
    are half prose and half pasted requirement lists where a bullet never ends
    in a period. A probe that could only quote a sentence would report nothing
    for the notices that state their requirements as a list — which is most of
    the ones worth pre-mortemming.
    """
    parts = re.split(r"(?<=[.!?])\s+|\n+", text or "")
    return [p.strip() for p in parts if p and p.strip()]


def _probe_notice(text: str) -> list[dict]:
    """
    Every probe against the notice text, fired or not, with what matched.

    Returns all of them in declaration order. That order is this file's and is
    never a ranking: sorting fired probes to the top would be a severity
    ordering invented here, and the absent ones are findings in their own right.
    """
    sentences = _sentences(text)
    out = []
    for probe in _PROBES:
        quotes: list[str] = []
        matched_patterns: list[str] = []
        for pattern in probe["patterns"]:
            rx = re.compile(pattern, re.IGNORECASE)
            hit = False
            for sentence in sentences:
                if rx.search(sentence):
                    hit = True
                    # Quoted whole, capped only where a pasted requirement block
                    # arrives as one 2,000-character "sentence".
                    quote = sentence if len(sentence) <= 400 else sentence[:400] + "…"
                    if quote not in quotes:
                        quotes.append(quote)
            if hit:
                matched_patterns.append(pattern)
        entry = {
            "probe": probe["id"],
            "lens": probe["lens"],
            "looking_for": probe["looking_for"],
            "state": "fired" if quotes else "absent",
            "matched_patterns": matched_patterns,
            "quotes": quotes[:6],
            "means": probe["means"],
        }
        if not quotes:
            entry["absent_note"] = (
                "Not present in the notice text. The notice is a summary, so "
                "this establishes nothing about the solicitation package, which "
                "is where the requirement would be written."
            )
        out.append(entry)
    return out


_VEHICLE_HEADING = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)
# To the end of the PARAGRAPH, not the end of the line. Three of the four
# entries state their status in one line and the fourth runs on into the
# qualifier that matters most — a line-anchored capture cut it at "and unlike
# TBIPS, SBIPS and", which reads as a complete status and is not one.
_VEHICLE_STATUS = re.compile(
    r"^\*\*Status:\*\*[^\S\n]*(.+?)(?=\n[^\S\n]*\n|\Z)",
    re.MULTILINE | re.DOTALL,
)


def _vehicle_statuses() -> dict:
    """
    What vault/reference/vehicles.md records as held, per vehicle.

    Parsed rather than restated. That file is hand-maintained and is the stated
    authority on eligibility; a second copy of "we are not on TBIPS" in Python
    is a copy that keeps working after the real one changes.

    Reports the status line VERBATIM rather than reducing it to a boolean. The
    entries carry qualifiers — a date, a condition, a refresh window — that a
    `held: false` throws away, and the reader needs the sentence not the flag.
    """
    path = paths.VAULT / "reference" / "vehicles.md"
    if not path.exists():
        return {
            "state": "not_found",
            "path": str(path),
            "note": "vehicles.md is missing, so nothing here can say whether a "
                    "named vehicle is held. That is a missing file, not an "
                    "absence of vehicles.",
        }
    text = path.read_text(encoding="utf-8")
    vehicles = []
    headings = list(_VEHICLE_HEADING.finditer(text))
    for i, heading in enumerate(headings):
        end = headings[i + 1].start() if i + 1 < len(headings) else len(text)
        status = _VEHICLE_STATUS.search(text[heading.start():end])
        if status:
            vehicles.append({
                "vehicle": heading.group(1).strip(),
                # Newlines folded: the status is one statement in the file
                # even where it is wrapped across source lines.
                "status": " ".join(status.group(1).split()),
            })
    return {
        "state": "read",
        "source": "vault/reference/vehicles.md",
        "vehicles": vehicles,
        "note": "Status lines quoted verbatim. A call-up against a vehicle not "
                "held is not biddable regardless of fit.",
    }


_PROFILE_SECTIONS = (
    "What we're looking for",
    "What we're NOT looking for",
    "Constraints Claude should respect",
    "Core capabilities",
)


def _profile_limits() -> dict:
    """
    The profile's own prose limits, quoted verbatim and never interpreted here.

    Four headings out of the profile body, returned as text. Nothing in this
    file compares them against a probe result: that comparison is the judgment,
    and the judgment is the reader's. A heading the profile does not carry is
    reported as absent rather than skipped, so a swapped profile says which
    sections this pre-mortem could not quote instead of quietly narrowing.
    """
    if not paths.PROFILE.exists():
        return {"state": "not_found", "path": str(paths.PROFILE)}
    text = paths.PROFILE.read_text(encoding="utf-8")
    # Body only. The frontmatter is config, already applied upstream by the
    # ingest; quoting it here would put tuning knobs in front of a bid decision.
    body = re.split(r"^---\s*$", text, maxsplit=2, flags=re.MULTILINE)[-1]
    found: dict[str, str] = {}
    missing: list[str] = []
    for name in _PROFILE_SECTIONS:
        rx = re.compile(
            r"^##\s+" + re.escape(name) + r"\s*$(.*?)(?=^##\s|\Z)",
            re.MULTILINE | re.DOTALL,
        )
        match = rx.search(body)
        if match:
            found[name] = match.group(1).strip()
        else:
            missing.append(name)
    return {
        "state": "read",
        "source": "vault/profiles/my-company.md",
        "sections": found,
        "sections_absent": missing,
        "note": "Quoted, not applied. Hold these beside the notice_signals "
                "above and judge; this tool never compares the two.",
    }


# From the column notes in scripts/contracts_ingest.py, which is where this
# grouping is established and where it should be corrected if it is wrong. The
# individual code letters are NOT expanded: no authority for that mapping ships
# with this repo, and inventing one inside a bid decision is how a directed
# award gets read as a competition.
_COMPETITIVE_CODES = {"TC", "TN", "OB"}
_DIRECTED_CODES = {"ST", "AC"}


def _incumbency(dept_keys: list[str], terms: list[str]) -> dict:
    """
    Who already holds work of this kind at this department, and how contested it was.

    The loss question the contracts data can actually answer. Three things come
    out of it and they are reported separately: who the money goes to, whether
    the department competes this work or directs it, and how many bidders showed
    up when it did compete.

    NULLS ARE UNKNOWN, NEVER ZERO, and `number_of_bids` is the field that makes
    this matter. It is populated on roughly a quarter of rows, its absence is
    not obviously missing-at-random, and a null read as "one bid" turns a
    reporting gap into a story about an uncontested field. Every count here
    carries the denominator it came from.
    """
    db = paths.PROJECT_ROOT / "data" / "contracts.db"
    if not db.exists():
        return {
            "state": "not_built",
            "note": "Contracts DB not built, so nothing here can say who the "
                    "incumbent is. Run: python scripts/contracts_ingest.py "
                    "(~630MB download). This is a missing build, not an absence "
                    "of incumbents.",
        }
    if not dept_keys:
        return {
            "state": "no_department",
            "note": "No department resolved for this notice, so the contracts "
                    "data cannot be scoped to it. A registry miss is not "
                    "evidence — see the jurisdiction note in vault/CLAUDE.md.",
        }

    slugs: list[str] = []
    for key in dept_keys:
        slugs += org_resolve.department_scope(key)["contract_slugs"]
    slugs = sorted(set(slugs))
    if not slugs:
        return {
            "state": "department_discloses_nothing",
            "departments": dept_keys,
            "note": "These departments disclose no contracts under a name of "
                    "their own, so this source cannot answer for them. That is "
                    "a real fact about the bodies, not a broken build.",
        }

    con = sqlite3.connect(db)
    try:
        meta = dict(con.execute("SELECT key, value FROM meta").fetchall())
        where = [f"owner_org IN ({','.join('?' * len(slugs))})"]
        params: list = list(slugs)
        if terms:
            clause = " OR ".join(
                ["lower(description) LIKE ?"] * len(terms)
                + ["lower(matched_terms) LIKE ?"] * len(terms)
            )
            where.append(f"({clause})")
            params += [f"%{t.lower()}%" for t in terms] * 2
        rows = con.execute(f"""
            SELECT family_id, vendor_norm, MAX(value) AS v, MAX(period_end) AS pe,
                   MAX(contract_date) AS cd, solicitation_procedure,
                   MAX(number_of_bids) AS nb, description
              FROM contracts
             WHERE {' AND '.join(where)}
          GROUP BY family_id
        """, params).fetchall()
    finally:
        con.close()

    if not rows:
        return {
            "state": "no_matching_contracts",
            "departments": dept_keys,
            "terms": terms,
            "as_of": meta.get("ingest_date"),
            "note": "No disclosed contract in the window matches these "
                    "departments and terms. Absence here is weak evidence: "
                    "matching is substring over descriptions, and a $585M "
                    "contract can be described in fifty-seven characters.",
        }

    vendor_totals: dict[str, float] = {}
    for _fam, vnorm, value, *_rest in rows:
        if vnorm:
            vendor_totals[vnorm] = vendor_totals.get(vnorm, 0) + (value or 0)

    procedures: dict[str, int] = {}
    for row in rows:
        code = (row[5] or "").strip().upper() or "not_stated"
        procedures[code] = procedures.get(code, 0) + 1

    bids = [row[6] for row in rows if row[6] is not None]
    bids_sorted = sorted(bids)

    return {
        "state": "read",
        "departments": dept_keys,
        "terms": terms,
        "as_of": meta.get("ingest_date"),
        "window_years": meta.get("window_years"),
        "contract_families": len(rows),
        "scope_note": "DEPARTMENT-LEVEL, NOT REQUIREMENT-LEVEL. The terms are "
                      "the notice's own matched competencies, which are broad by "
                      "construction — they are what admitted it to the corpus. So "
                      "this is who does work of this general kind at these "
                      "departments, not who holds the contract this notice would "
                      "replace. Reading it as the latter names an incumbent "
                      "nobody has identified.",
        "top_vendors_by_value": [
            {"vendor": vendor, "total_value": round(total, 0)}
            for vendor, total in sorted(vendor_totals.items(), key=lambda kv: -kv[1])[:8]
        ],
        "procedure": {
            "codes": procedures,
            "competed": sum(n for c, n in procedures.items() if c in _COMPETITIVE_CODES),
            "directed": sum(n for c, n in procedures.items() if c in _DIRECTED_CODES),
            "not_stated": procedures.get("not_stated", 0),
            "grouping_source": "scripts/contracts_ingest.py — TC/TN/OB competitive, "
                               "ST/AC not. The individual codes are the "
                               "publisher's and are not expanded here.",
        },
        "bids": {
            "families_reporting": len(bids),
            "families_total": len(rows),
            "median": bids_sorted[len(bids_sorted) // 2] if bids_sorted else None,
            "max": max(bids) if bids else None,
            "note": "A null number_of_bids is UNKNOWN, not one bid. The field is "
                    "populated on roughly a quarter of rows and its absence is "
                    "not obviously missing-at-random, so the median describes "
                    "the families that reported and nothing else.",
        },
        "recent": [
            {"vendor": row[1], "value": row[2], "contract_date": row[4],
             "period_end": row[3], "description": (row[7] or "")[:180]}
            for row in sorted(rows, key=lambda r: r[4] or "", reverse=True)[:6]
        ],
        "caveats": "Unaudited; vendor names lightly normalized (suffix and "
                   "punctuation only, not fuzzy-matched), so near-variants of "
                   "one firm may count separately and understate a single "
                   "incumbent.",
    }


_FM = re.compile(r"^---\n(.*?)\n---", re.DOTALL)
_DECISION_SECTION = re.compile(
    r"^##\s+(Archived|Parked)\b[^\n]*\n(.*?)(?=^##\s|\Z)",
    re.MULTILINE | re.DOTALL,
)


def _frontmatter(content: str) -> dict:
    """
    The same lightweight frontmatter parse list-watching uses, no YAML dependency.

    `null` IS DECODED TO None RATHER THAN KEPT AS THE STRING. promote writes
    `estimated_value: null` for a notice the feed gave no value for, and the
    text of that line read back verbatim is a four-character string that is
    truthy, prints as `"null"`, and defeats the one rule this field has —
    absent means unknown, never zero and never free. The corpus path returns a
    real None here, so leaving the vault path as text also made two sources of
    the same tender disagree about the type of the same key.
    """
    match = _FM.match(content)
    if not match:
        return {}
    fields = {}
    for line in match.group(1).split("\n"):
        if ":" in line:
            key, _, value = line.partition(":")
            cleaned = value.strip().strip('"')
            fields[key.strip()] = None if cleaned in ("null", "~", "") else cleaned
    return fields


def _prior_decisions(dept_keys: list[str], tender_id: str) -> dict:
    """
    What we decided last time, in our own words — the backwards half of the exercise.

    Reads the reasons `park` and `archive` wrote into the vault. This is the one
    section that is about us rather than about the notice, and it is why a
    pre-mortem beats a checklist: a checklist asks whether a risk is present,
    and this asks what happened the last four times it was.

    SCOPED TO THE DEPARTMENTS THIS NOTICE ATTRIBUTES TO, and to nothing else.
    Scoping on competency overlap instead would return every archived notice in
    the vault, because the corpus is filtered on those competencies to begin
    with — the filter guarantees the overlap, so it carries no information.

    Recency is not authority. Entries come back newest first because a reader
    has to start somewhere, and a decision taken against a different market can
    be the wrong one to repeat; the dates are reported so that can be judged.
    """
    out: dict = {
        "state": "read",
        "departments": dept_keys,
        "watching": [],
        "parked": [],
        "archived": [],
    }
    buckets = (("watching", paths.WATCHING), ("parked", paths.PARKED),
               ("archived", paths.ARCHIVED))
    any_notes = False
    for label, directory in buckets:
        if not directory.exists():
            continue
        for path in sorted(directory.glob("*.md")):
            any_notes = True
            content = path.read_text(encoding="utf-8")
            fields = _frontmatter(content)
            if fields.get("tender_id") == tender_id:
                continue
            departments = re.findall(r"\[\[([^\]]+)\]\]", fields.get("department") or "")
            if dept_keys and not set(departments) & set(dept_keys):
                continue
            entry = {
                "filename": path.name,
                "tender_id": fields.get("tender_id", ""),
                "title": fields.get("title", ""),
                "closing_date": fields.get("closing_date", ""),
                "departments": departments,
            }
            recorded = [
                {"decision": m.group(1).lower(), "text": m.group(2).strip()[:800]}
                for m in _DECISION_SECTION.finditer(content)
            ]
            if recorded:
                entry["recorded"] = recorded
            out[label].append(entry)

    for label, _ in buckets:
        out[label].sort(key=lambda e: e.get("closing_date") or "", reverse=True)

    if not any_notes:
        out["note"] = (
            "The vault holds no tender notes at all yet, so there is no prior "
            "decision to walk backwards from. This section gets useful after the "
            "first few archives — it is the only one here that improves with use."
        )
    elif not any(out[label] for label, _ in buckets):
        out["note"] = (
            "Nothing else in the vault is attributed to these departments — "
            "this tender itself is excluded, so a lone note on the subject "
            "reads as empty here. That is a gap in our own history, not a "
            "fact about the department."
        )
    return out


def _not_checked(tender_id: str, incumbency: dict, source: str) -> list[dict]:
    """
    What this pre-mortem did not look at, stated rather than left as a silent gap.

    The most important section on a thin notice, and the one a reader is least
    able to reconstruct. Everything here is a real limit of the assembly rather
    than a hedge about confidence: the solicitation package is unread because
    this project does not fetch it, and a pre-mortem that omitted to say so
    would read as though the mandatory requirements had been checked.
    """
    gaps: list[dict] = []
    note = _note_for_tender(tender_id)
    if note is None:
        gaps.append({
            "gap": "solicitation_package",
            "detail": "This tender has no note in the vault, so it has no "
                      "document folder either. Mandatory requirements, the "
                      "evaluation grid and the security schedule live in the "
                      "package, not in the notice — nothing below has seen them.",
            "fix": f"python scripts/tender_tools promote {tender_id}, then attach.",
        })
    else:
        folder = _attachment_dir(note)
        files = sorted(
            p.name for p in folder.iterdir()
            if p.is_file() and p.suffix.lower() != ".json"
        ) if folder.is_dir() else []
        if files:
            gaps.append({
                "gap": "solicitation_package_not_read_here",
                "detail": f"{len(files)} document(s) are attached and this command "
                          "does not read them. Every probe below ran against the "
                          "notice text only.",
                "files": files,
                "fix": f"python scripts/tender_tools read-attachment {tender_id} <filename>",
            })
        else:
            gaps.append({
                "gap": "solicitation_package",
                "detail": "No documents have been dropped for this tender. The "
                          "notice is a summary; the requirements that decide a bid "
                          "are in the package, which is downloaded by hand from the "
                          "platform and never by this project.",
                "fix": f"python scripts/tender_tools attach {tender_id} --platform merx",
            })
    if incumbency.get("state") == "not_built":
        gaps.append({
            "gap": "incumbency",
            "detail": "The contracts database is not built, so the commonest "
                      "single reason a bid loses — somebody already has the work "
                      "— could not be checked at all.",
            "fix": "python scripts/contracts_ingest.py",
        })
    gaps.append({
        "gap": "notice_text_only",
        "detail": f"Every probe read the notice as held in the {source}. Feed "
                  "descriptions are truncated and amended notices are "
                  "re-published on the platform, so an absent probe is evidence "
                  "about this text and about nothing else.",
    })
    return gaps


def cmd_pre_mortem(args) -> dict:
    """
    Assume this tender already went wrong, and assemble what would explain it.

    One adversarial pass before committing, run against a notice somebody has
    already decided they like. It asks two questions separately — bid and lost,
    won and regretted — because the facts that answer them are different facts
    and the same fact often points opposite ways under the two.

    Reads the corpus first and the vault note second, and says which. A promoted
    tender outlives its corpus entry, since notices leave on closing and the one
    most worth pre-mortemming is often the one closest to its date.

    ASSEMBLES; DOES NOT SCORE. No risk rating, no probe tally, no ordering by
    anything but this file's declaration order. The module docstring carries why
    a count of fired probes is a score with the arithmetic hidden, and why there
    is no lobbying section.
    """
    tender_id = args.tender_id

    meta: dict = {}
    text = ""
    doc = None
    try:
        load_collection()
        doc = {d["id"]: d for d in corpus.doc_index}.get(tender_id)
    except Exception:
        # A missing or unbuilt corpus is not fatal here: a promoted note carries
        # enough to run the whole exercise, and refusing would make the command
        # unavailable exactly when a tender is closest to its closing date.
        doc = None

    if doc:
        meta, text, source = doc["metadata"], doc["document"], "corpus"
    else:
        note = _note_for_tender(tender_id)
        if note is None:
            return {"error": f"Tender {tender_id} is in neither the corpus nor the "
                             f"vault. Nothing to pre-mortem — check the id with "
                             f"`search` rather than assuming it exists."}
        content = note.read_text(encoding="utf-8")
        meta = _frontmatter(content)
        body = re.search(r"^##\s+Description\s*$(.*?)(?=^##\s|\Z)", content,
                         re.MULTILINE | re.DOTALL)
        text = (body.group(1) if body else content).strip()
        source = f"vault note ({note.parent.name}/)"

    # Attribution from whichever record we have. The corpus carries the two raw
    # entity fields; a promoted note already carries resolved keys, because
    # promote wrote them through this same function.
    if doc:
        attribution = _entity_attribution(
            str(meta.get("end_user_entity") or "").strip(),
            str(meta.get("contracting_entity") or "").strip(),
        )
        dept_keys = list(attribution)
        agency = _display_agency(meta)
    else:
        dept_keys = re.findall(r"\[\[([^\]]+)\]\]", str(meta.get("department", "")))
        attribution = {key: {"entity_source": "read_from_vault_note"}
                       for key in dept_keys}
        agency = meta.get("agency", "")

    matched = str(meta.get("matched_competencies") or "")
    terms = [t.strip() for t in re.split(r"[,\[\]]", matched) if t.strip()]

    incumbency = _incumbency(dept_keys, terms)
    probes = _probe_notice(text)

    subject = {
        "tender_id": tender_id,
        "title": meta.get("title", ""),
        "agency": agency,
        "departments": dept_keys,
        "attribution": attribution,
        "closing_date": meta.get("closing_date", ""),
        "opportunity_kind": meta.get("opportunity_kind", "unknown"),
        "kind_basis": meta.get("kind_basis", "unclassified"),
        # Absent means unknown. The ingest omits the key when nothing was
        # extracted, and a 0 here would read as a free contract.
        "estimated_value": meta.get("estimated_value"),
        "matched_competencies": matched,
        "read_from": source,
    }
    if doc:
        subject.update(_window_fields(meta))
    if meta.get("closing_date_conflict"):
        subject["closing_date_conflict"] = meta["closing_date_conflict"]
        subject["closing_date_conflict_note"] = (
            "The description states a submission deadline earlier than the "
            "structured field. This outranks closing_date; planning to the later "
            "date loses the bid regardless of everything else here."
        )

    return {
        "tender_id": tender_id,
        "generated": datetime.now().strftime("%Y-%m-%d"),
        "subject": subject,
        "provenance": _corpus_provenance() if doc else {
            "state": "not_from_corpus",
            "note": "Read from the vault note, so no corpus stamps apply. That "
                    "note is a dated snapshot taken at promote and does not "
                    "update as the feed does.",
        },
        LOST: {
            "question": "Assume we bid and lost. What, visible today, explains it?",
            "notice_signals": [p for p in probes if p["lens"] == LOST],
            "vehicles": _vehicle_statuses(),
            "incumbency": incumbency,
        },
        REGRETTED: {
            "question": "Assume we won and regretted it. What, visible today, "
                        "explains that?",
            "notice_signals": [p for p in probes if p["lens"] == REGRETTED],
            "profile_limits": _profile_limits(),
        },
        "prior_decisions": _prior_decisions(dept_keys, tender_id),
        "not_checked": _not_checked(tender_id, incumbency, source),
        "how_to_read": (
            "Two questions, deliberately not merged, and no score under either. "
            "Work the lost lens first: an eligibility gate settles the tender "
            "without any of the rest mattering, and a set-aside or a vehicle we "
            "do not hold is that kind of gate. Then the regret lens, which is the "
            "one enthusiasm skips — it asks what winning commits us to, and its "
            "evidence is the profile's own words quoted beside the notice's, never "
            "a comparison this tool made. A probe marked `absent` is a fact about "
            "the notice text and not about the solicitation package, so read "
            "`not_checked` before treating any of this as a clean bill. There is "
            "no lobbying section on purpose: who was in the room is presence, "
            "never influence, and a pre-mortem is exactly where that gets misread "
            "as the answer."
        ),
    }
