"""
The one question Jev is asked, frozen.

------------------------------------------------------------------------------
WHY A RECORDED HASH AND NOT A COMPUTED ONE
------------------------------------------------------------------------------
Every cached verdict is keyed on QUESTION_SHA256, and the ref-004 decision
rule was committed against it. A hash derived at import from the payload beside
it would agree with that payload by construction and prove nothing. Written
down, it is a second statement of the same fact, so a change to the
instructions, to an option, or to the profile families the options are built
from is CAUGHT: `frozen_question()` raises FrozenQuestionDrift and nothing runs.

It does not auto-resolve and it does not re-derive - same rule as
backtest._check_frozen_manifest. A drift means results cached under the old
question describe a different question, and the resolution is a human one:
re-record the hash deliberately in a new refinement, knowing every earlier
verdict is keyed out of reach.

------------------------------------------------------------------------------
THE FROZEN SEGMENT LIST
------------------------------------------------------------------------------
SEGMENTS is the top 12 segments by coded-notice volume in data/notices.db,
excluding 43/80/81 (which are split into families instead), counted
2026-09-23 as notices filing at least one code in the segment:

    72 3963   25 2238   41 1660   56 1151   78 993   46 727
    86  664   24  649   76  641   40  638   30 597   77 570
    -- cut --  85 520   82 464   70 382

It is a literal, not a query, so the question cannot move when the archive is
rebuilt. Everything below 77 is "none of these".

------------------------------------------------------------------------------
WHAT THE INSTRUCTIONS DELIBERATELY DO NOT SAY
------------------------------------------------------------------------------
No company, no competency, no "IT", no preference. The model is asked what the
notice BUYS, never whether it is a fit. Fit is the profile's decision alone.
"""
from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path

from . import options as O

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import ingest  # noqa: E402

MODEL = "jev-1.13.0"
QUESTION_ID = "commodity"

SEGMENTS = ("72", "25", "41", "56", "78", "46", "86", "24", "76", "40", "30", "77")

INSTRUCTIONS = (
    "This is a Government of Canada procurement notice: `title` and "
    "`description`. Which UNSPSC commodity family or segment best describes the "
    "main goods or services the notice is buying? Judge what is being "
    "purchased, not who is buying it or where the work happens. If the notice "
    "buys several things, choose the one that is the principal purchase. "
    "Choose 'none of these' only when the principal purchase belongs to none "
    "of the other options."
)

# Recorded 2026-09-23 against jev-1.13.0, the profile at commit 132f245 and the
# PSPC reference file dated 2021-05-12. See the module docstring before editing.
QUESTION_SHA256 = (
    "c5725ac4e364c103e429025214c7d4b1cb718f1d89cca5f750be59f17a055158")


class FrozenQuestionDrift(RuntimeError):
    """The question that would be sent is not the question that was frozen."""


@dataclass(frozen=True)
class Question:
    options: tuple
    payload: dict          # {"type", "instructions", "criteria"} as sent
    sha256: str

    def option(self, key: str) -> O.Option:
        for opt in self.options:
            if opt.key == key:
                return opt
        raise KeyError(key)

    @property
    def keys(self) -> tuple:
        return tuple(o.key for o in self.options)

    def kind_of(self, key: str) -> str:
        return self.option(key).kind


def question_sha256(payload: dict) -> str:
    """Canonical hash of instructions + criteria. Sorted keys, no whitespace."""
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"),
                   ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def build_question(profile_path: Path = ingest.DEFAULT_PROFILE,
                   reference_path: Path = O.REFERENCE_CSV) -> Question:
    """The live question. Not checked - see frozen_question()."""
    families = ingest.parse_profile(profile_path)["unspsc_families"]
    opts = O.build_options(O.load_reference(reference_path), families, SEGMENTS)
    payload = {
        "type": "choice",
        "instructions": INSTRUCTIONS,
        "criteria": {o.key: o.description for o in opts},
    }
    return Question(opts, payload, question_sha256(payload))


def check_frozen(question: Question, recorded: str = None) -> None:
    """Raise unless the live question hashes to the recorded literal."""
    recorded = QUESTION_SHA256 if recorded is None else recorded
    if question.sha256 != recorded:
        raise FrozenQuestionDrift(
            f"The question has changed: live {question.sha256[:16]}.. vs "
            f"recorded {recorded[:16]}... Instructions, an option, or the "
            f"profile families it is built from moved. Verdicts cached under "
            f"the recorded hash describe a different question, and the ref-004 "
            f"decision rule was committed against it. Not auto-resolved: "
            f"re-record deliberately, in a new refinement, or revert the change.")


def frozen_question(**kwargs) -> Question:
    """The ONLY way run paths obtain the question. Refuses on drift."""
    question = build_question(**kwargs)
    check_frozen(question)
    return question
