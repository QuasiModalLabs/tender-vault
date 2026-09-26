"""
Variant B's state transform: strip invited-supplier lists from a description.

DESIGNED AND COSTED, NOT RUN. Nothing here is called on the imputation path;
`prepare()` in state.py is unchanged. A variant B run needs its own
pre-registered record first (see ref-004).

WHY. Supply-arrangement call-ups often list every invited SA holder - 156
names on one TBIPS notice, 170 on a TSPS one. That is thousands of tokens of
state the question does not need, which is the context rot TypeSafe's
guidance warns about, and the reading found the same lists behind keyword
false positives (every hit on cb-282-89433549 came from supplier names).

THE DETECTOR, and why it is conservative. A line is NAME-SHAPED if it is short
and carries a legal-entity suffix (Inc, Ltd, LLP, Corp, ULC, S.E.N.C, "joint
venture"...). Generic words like "services" or "solutions" do NOT count - a
bulleted requirement list would otherwise vanish. A run of lines is removed
only when it holds at least MIN_NAMES name-shaped lines and name-shaped lines
make up at least MIN_SHARE of its non-blank lines. The run is replaced by a
marker saying how many names went, so the state still says a list was there.

FROZEN like the question: STRIP_RULES_SHA256 is a recorded literal, and a
change to any rule below must re-record it deliberately.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

MIN_NAMES = 10
MIN_SHARE = 0.8
MAX_LINE_CHARS = 200
MAX_SHORT_WORDS = 10

_SUFFIX = (r"\b(?:inc|incorporated|ltd|lt[ée]e|limited|llp|l\.l\.p|lp|llc|ulc|corp|"
           r"corporation|co|cie|s\.e\.n\.c|s\.e\.c|senc|gmbh|plc|joint\s+venture|jv|cjv)\b\.?")
_NUMBERED = r"^\s*\d{1,3}[.)]\s*"
RULES = {"min_names": MIN_NAMES, "min_share": MIN_SHARE,
         "max_line_chars": MAX_LINE_CHARS, "max_short_words": MAX_SHORT_WORDS,
         "suffix": _SUFFIX, "numbered": _NUMBERED,
         "marker": "[invited-supplier list: {n} names removed]"}

# Recorded 2026-09-24. See the module docstring before editing.
STRIP_RULES_SHA256 = (
    "cd74c1350ba0699f7daf822d1678901623ef65dc65b59a126671afa7c0938311")

_SUFFIX_RE = re.compile(_SUFFIX, re.IGNORECASE)


def rules_sha256(rules: dict = RULES) -> str:
    return hashlib.sha256(json.dumps(rules, sort_keys=True, separators=(",", ":"))
                          .encode("utf-8")).hexdigest()


def _kind(line: str) -> str:
    s = line.strip()
    if not s:
        return "blank"
    if len(s) > MAX_LINE_CHARS:
        return "other"
    if _SUFFIX_RE.search(s):
        return "name"
    body = re.sub(_NUMBERED, "", s)
    if (len(body.split()) <= MAX_SHORT_WORDS and not s.endswith(":")
            and not re.search(r"[.!?]\s+\S", body)):
        return "short"
    return "other"


@dataclass(frozen=True)
class Stripped:
    text: str
    runs: tuple          # (first_line, last_line, names) per removed run
    names_removed: int
    chars_removed: int


def strip_supplier_lists(description: str) -> Stripped:
    lines = description.replace("\r\n", "\n").split("\n")
    kinds = [_kind(l) for l in lines]
    out, runs, i = [], [], 0
    while i < len(lines):
        if kinds[i] == "other":
            out.append(lines[i])
            i += 1
            continue
        j = i
        while j < len(lines) and kinds[j] != "other":
            j += 1
        # trim the run to its first and last name-shaped line
        idx = [k for k in range(i, j) if kinds[k] == "name"]
        if idx:
            a, b = idx[0], idx[-1]
            names = len(idx)
            nonblank = sum(1 for k in range(a, b + 1) if kinds[k] != "blank")
            if names >= MIN_NAMES and names / nonblank >= MIN_SHARE:
                out.extend(lines[i:a])
                out.append(RULES["marker"].format(n=names))
                out.extend(lines[b + 1:j])
                runs.append((lines[a].strip(), lines[b].strip(), names))
                i = j
                continue
        out.extend(lines[i:j])
        i = j
    text = "\n".join(out)
    return Stripped(text, tuple(runs), sum(r[2] for r in runs),
                    len(description) - len(text))


def swallowed_lines(description: str) -> list:
    """Non-blank lines inside removed runs that are NOT name-shaped - the
    detector's false-positive risk, reported rather than assumed away."""
    lines = description.replace("\r\n", "\n").split("\n")
    kinds = [_kind(l) for l in lines]
    out, i = [], 0
    while i < len(lines):
        if kinds[i] == "other":
            i += 1
            continue
        j = i
        while j < len(lines) and kinds[j] != "other":
            j += 1
        idx = [k for k in range(i, j) if kinds[k] == "name"]
        if idx:
            a, b = idx[0], idx[-1]
            nonblank = sum(1 for k in range(a, b + 1) if kinds[k] != "blank")
            if len(idx) >= MIN_NAMES and len(idx) / nonblank >= MIN_SHARE:
                out += [lines[k].strip() for k in range(a, b + 1) if kinds[k] == "short"]
        i = j
    return out


def check_frozen() -> None:
    if rules_sha256() != STRIP_RULES_SHA256:
        raise RuntimeError(
            f"strip rules changed: live {rules_sha256()[:16]}.. vs recorded "
            f"{STRIP_RULES_SHA256[:16]}... Re-record deliberately.")
