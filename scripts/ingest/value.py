"""
Contract value: the regex that is not trusted, and the LLM extractor that is
opt-in.
"""
from __future__ import annotations

import re
from typing import Optional


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
