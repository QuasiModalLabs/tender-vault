"""
The input ceiling: notices whose description gives no method anything to read.

Two flags, per the ref-004 phase 2 request:

  short             the raw description is under SHORT_CHARS characters.
  boilerplate_only  after removing invited-supplier lists (strip.py) and every
                    sentence matching a BOILERPLATE pattern, under SHORT_CHARS
                    characters remain.

THE PATTERNS ARE FROZEN AND LISTED, drawn from the labelled sheet's
no_description cases (the Ariba closing-time amendment, the supply-arrangement
qualification paragraph, "RFP documents will be e-mailed directly"). Each
removes whole SENTENCES that match it, never fragments, and each reports its
own hit count so an over-broad pattern is visible.

A LIMIT, stated. boilerplate_only uses the same 200-character bar as `short`,
so a genuinely short description of real work that also carries boilerplate
can be flagged (e.g. a one-sentence requirement after an Ariba amendment). The
`ceiling` command prints a sample of boilerplate_only residues and counts at
stricter residue bars (100, 50) so the reader can judge how much that costs.

This module changes nothing: it flags, and the headline is recomputed with the
flagged notices excluded ALONGSIDE the original, never instead of it.
"""
from __future__ import annotations

import re

from .strip import strip_supplier_lists

SHORT_CHARS = 200
STRICTER_BARS = (100, 50)

BOILERPLATE = {
    "ariba_closing_amendment": r"amendment to closing time",
    "disregard_posting_deadline": r"please disregard the ariba discovery",
    "submission_deadline": r"all required supporting documentation and .{0,40}must be submitted",
    "declared_non_responsive": r"will result in the (?:bid|proposal)s? being declared non-?responsive",
    "rfp_against_sa": r"request for proposal \(rfp\) against supply arrangement",
    "sa_holders_emailed": r"supply arrangement holders will be e-?mailed",
    "outside_sa_must_qualify": r"outside of the supply arrangement are interested",
    "contact_sa_officer": r"contact the supply arrangement contracting officer",
    "rfp_docs_emailed": r"(?:rfp|request for proposal) (?:\(rfp\) )?documents will be e-?mailed directly",
    "not_on_tendering_system": r"not available on the government electronic tendering system",
    "see_attached": r"^\s*see attached\.?\s*$",
}
_COMPILED = {k: re.compile(v, re.IGNORECASE) for k, v in BOILERPLATE.items()}
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")


def residue(description: str) -> tuple[str, list]:
    """Description minus supplier lists and boilerplate sentences; and which
    patterns fired."""
    text = strip_supplier_lists(description or "").text
    text = re.sub(r"\[invited-supplier list: \d+ names removed\]", " ", text)
    kept, fired = [], []
    for sentence in _SENTENCE_SPLIT.split(text):
        hits = [k for k, rx in _COMPILED.items() if rx.search(sentence)]
        if hits:
            fired += hits
        elif sentence.strip():
            kept.append(sentence.strip())
    return " ".join(kept), fired


def flags(description: str) -> dict:
    desc = description or ""
    res, fired = residue(desc)
    short = len(desc.strip()) < SHORT_CHARS
    return {"short": short,
            "boilerplate_only": (not short) and len(res) < SHORT_CHARS,
            "residue_chars": len(res), "residue": res, "fired": fired}
