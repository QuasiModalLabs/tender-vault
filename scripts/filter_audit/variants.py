"""
Candidate predicate overrides - proposals with code behind them, and nothing else.

THIS MODULE IS NEVER IMPORTED BY ingest.py OR predicates.py, and that is the
enforcement rather than a promise. A variant physically cannot reach production,
because nothing on the production import path can see it. tests assert
`'filter_audit.variants' not in sys.modules` after `import ingest`.

A variant is what turns a PROPOSED refinement into a TESTING one. Writing the
function is a deliberate human act; an LLM may author the proposal markdown, but
a proposal with no variant names no code and cannot be evaluated.

ACCEPTING a variant still changes nothing here. Promotion is a separate human
commit that edits the profile or predicates.py and registers a new filter
version. There is no code path from this file into the corpus.
"""
from __future__ import annotations

import re
from typing import Callable

from . import predicates as P

# Matches a bare URL or a bare hostname carrying a dotted TLD-ish tail. Both
# forms appear in these descriptions: full https:// links and naked
# portal.us.bn.cloud.ariba.com hostnames pasted into prose.
_URL = re.compile(
    r"(?:https?://|www\.)\S+"
    r"|\b(?:[a-z0-9-]+\.){2,}[a-z]{2,}\b",
    re.IGNORECASE)


def _strip_urls(text: str) -> str:
    return _URL.sub(" ", text)


class Variant:
    """One named candidate change, and the decision pair it produces."""

    def __init__(self, variant_id: str, stage: str, summary: str,
                 rationale: str, decide: Callable):
        self.id = variant_id
        self.stage = stage
        self.summary = summary
        self.rationale = rationale
        self.decide = decide

    def __call__(self, notice, criteria, as_of):
        return self.decide(notice, criteria, as_of)


def _cyber_security_spacing(notice, criteria, as_of):
    modified = dict(criteria)
    terms = list(modified.get("competencies") or [])
    if "cyber security" not in terms:
        terms = terms + ["cyber security"]
    modified["competencies"] = terms
    return (P.production_decision(notice, modified, as_of),
            P.audit_decision(notice, modified, as_of))


def _url_stripped_keywords(notice, criteria, as_of):
    stripped = P.Notice(
        notice_id=notice.notice_id,
        title=_strip_urls(notice.title),
        description=_strip_urls(notice.description),
        contracting_entity=notice.contracting_entity,
        end_user=notice.end_user,
        notice_type=notice.notice_type,
        procurement_category=notice.procurement_category,
        unspsc=notice.unspsc,
        gsin=notice.gsin,
        publication_date=notice.publication_date,
        raw_closing_date=notice.raw_closing_date,
        closing_day=notice.closing_day,
        source=notice.source,
    )
    return (P.production_decision(stripped, criteria, as_of),
            P.audit_decision(stripped, criteria, as_of))


def _admit_everything(notice, criteria, as_of):
    """
    A straw variant, and it exists to be REJECTED.

    It is the mechanical proof that "recovers a known false negative" is not a
    verdict: this recovers every one of them and is worse by every other
    measure. test_regression_protection asserts the comparison says so.
    """
    empty = dict(criteria)
    empty["unspsc_families"] = []
    empty["competencies"] = []
    empty["exclude"] = []
    results = tuple(
        P.StageResult(s.name, s.order, "pass", "admit-everything straw variant")
        for s in P.STAGES)
    return (P.ProductionDecision(notice.notice_id, True, None, results),
            P.AuditDecision(notice.notice_id, results, True))


VARIANTS = {
    "v-cyber-security-spacing": Variant(
        "v-cyber-security-spacing", "relevance",
        "Add the two-word 'cyber security' alongside the one-word competency.",
        "SSC files no UNSPSC, so its notices can only reach the keyword branch. "
        "matched_competencies is word-boundary, so \bcybersecurity\b cannot "
        "match 'Cyber Security'. Measured on the archive over rows where "
        "parse_unspsc_codes returns empty: 35 say 'cyber security', 12 say "
        "'cybersecurity'. The profile picked the minority spelling, on the "
        "largest federal IT buyer.",
        _cyber_security_spacing),
    "v-url-stripped-keywords": Variant(
        "v-url-stripped-keywords", "relevance",
        "Strip URLs from title+description before keyword matching.",
        "Proposed in vault/briefings/briefing-2026-08-17.md against "
        "MX-443978767209, where 'cloud' matched only inside "
        "portal.us.bn.cloud.ariba.com. The briefing said the fix is NOT to "
        "prune 'cloud'. This does not prune it.",
        _url_stripped_keywords),
    "v-admit-everything": Variant(
        "v-admit-everything", "all",
        "Admit every notice. A straw variant that exists to be rejected.",
        "Recovers every known false negative and is worse by every other "
        "measure. Kept so the comparison can demonstrate that recovering a "
        "false negative is not on its own a better verdict.",
        _admit_everything),
}


def get_variant(variant_id: str) -> Variant:
    if variant_id not in VARIANTS:
        raise KeyError(
            f"Unknown variant {variant_id!r}. Known: {', '.join(sorted(VARIANTS))}")
    return VARIANTS[variant_id]
