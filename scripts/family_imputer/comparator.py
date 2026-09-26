"""
The production keyword branch, run as a comparator on the same notices.

NOT A REIMPLEMENTATION. KEYWORD_MATCHER is `ingest.matched_competencies` itself
- the function object stage_relevance calls on uncoded notices
(filter_audit/predicates.py) - and the test suite checks that by identity. The
text is `ImputerState.text`, the same `title + ' ' + description` string as
predicates.Notice.text, also checked by the suite. Like the imputer, it sees
only an ImputerState, so it is blind to the codes it will be scored against.

ITS POPULATION HANDICAP, stated where the comparator lives. In production this
matcher never runs on WS or cb notices - they file codes and are decided on
them. Scoring it on coded WS/cb notices asks it to find IT work in prose from
publishers who never needed their prose to carry keywords. A large keyword
deficit here is partly an artefact of that population, not evidence about how
the matcher reads PW, SSC and MX notices.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import ingest  # noqa: E402

from .state import ImputerState

KEYWORD_MATCHER = ingest.matched_competencies


def keyword_hits(state: ImputerState, competencies: list[str]) -> list[str]:
    """Full text, untruncated - production reads the whole notice."""
    return KEYWORD_MATCHER(state.text, competencies)
