"""
The profile's YAML frontmatter is the filter config.

Named company_profile rather than profile because a module named `profile.py`
shadows the stdlib module of that name for anything that imports it while this
directory is on sys.path — which broke sentence_transformers three imports deep
the last time this package split happened.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml


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
