"""
What Jev is allowed to see: a notice's title and description, and nothing else.

`ImputerState` has exactly two fields. The publisher's UNSPSC, the GSIN, the
entity, the notice type - the fields that would let the model read the answer
off the notice rather than impute it - are absent from the type, and
`from_row` reads no other key, so a caller holding one cannot pass them on.
tests/test_family_imputer.py asserts the field set by introspection.

TRUNCATION IS RECORDED, NOT SILENT. Jev accepts 32k tokens for state plus the
longest question; the question is ~3k tokens and the longest archive notice is
77k characters. The description is cut to fit STATE_CHAR_BUDGET and the cut is
flagged with the original length, so the report can say how many verdicts were
made on a partial notice. The KEYWORD COMPARATOR reads the FULL text - that is
what production does - so truncation can only handicap Jev, never the
comparator.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, fields

# ~4 characters per English token, leaving well over the question's size spare.
STATE_CHAR_BUDGET = 60_000


@dataclass(frozen=True)
class ImputerState:
    title: str
    description: str

    @staticmethod
    def from_row(row) -> "ImputerState":
        """Read title and description, and no other key."""
        def text(key) -> str:
            value = row[key]
            return "" if value is None else str(value)
        return ImputerState(title=text("title"), description=text("description"))

    @property
    def text(self) -> str:
        """title + ' ' + description - the same string predicates.Notice.text builds."""
        return f"{self.title} {self.description}"


STATE_FIELDS = tuple(f.name for f in fields(ImputerState))


@dataclass(frozen=True)
class Prepared:
    payload: dict              # exactly what is sent as `state`
    truncated: bool
    original_chars: int
    content_sha256: str


def prepare(state: ImputerState, budget: int = STATE_CHAR_BUDGET) -> Prepared:
    """The state as sent, with truncation recorded and the content hash taken
    over the exact bytes sent - so a change in the budget is a cache miss."""
    original = len(state.title) + len(state.description)
    room = max(0, budget - len(state.title))
    description = state.description[:room]
    payload = {"title": state.title, "description": description}
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"),
                   ensure_ascii=False).encode("utf-8")).hexdigest()
    return Prepared(payload, len(description) < len(state.description),
                    original, digest)
