"""
The Choice's options, built deterministically from the UNSPSC side of the PSPC
reference file, and the map from a publisher's codes onto them.

THE GSIN COLUMNS NEVER LOAD. The reference file is PSPC's GSIN/NIBS-to-UNSPSC
linkage file, and every row carries a GSIN code beside the UNSPSC one. Bridging
the two systems through it is prohibited - unspsc_discover.py records why
(telecom cable laying and highway paving both map to GSIN 5153). That module
drops the GSIN columns on load; this one goes further and never reads them:
`load_reference` resolves the indices of three whitelisted UNSPSC columns from
the header and keeps only those cells, so nothing downstream can reach a GSIN
value because none is ever held.

FOUR KINDS OF OPTION, in this order:

  profile   one per `unspsc_families` entry, at whatever granularity the
            profile states it. 80101507 is an L4 commodity, not a family, and
            stays one - collapsing it into 8010 would put management
            consulting inside the admit class.
  sibling   every other L2 family in a segment the profile touches. Where a
            profile entry is finer than a family (80101507 inside 8010), the
            sibling is that family MINUS the entry, and says so in its text.
  segment   the frozen top segments by coded-notice volume, each as one option,
            so the non-IT mass is not one heterogeneous "none" whose
            probabilities mean nothing.
  none      the genuine tail: every segment not otherwise listed.

MAPPING A PUBLISHER CODE is longest-prefix over the same options, so 80101507
lands on the profile option and 80101501 on "8010 other". A code inside a
profile segment that reaches no family option - a segment-level code like
81000000, which names the segment and no family - is UNMAPPABLE: we cannot say
which option the publisher meant, and guessing would score Jev against a label
we invented.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
REFERENCE_CSV = PROJECT_ROOT / ".cache" / "unspsc_reference.csv"

# The whitelist. Nothing else in the file is read - in particular not
# GSINCode-NIBSCode or either GSIN description column.
_CODE_COL = "UNSPSC-Code"
_DESC_COL = "UNSPSC-Description-eng"
_LEVEL_COL = "HierarchyLevel-NiveauHierarchique"
UNSPSC_COLUMNS = (_CODE_COL, _DESC_COL, _LEVEL_COL)

NONE_KEY = "none of these"
UNMAPPABLE = "unmappable"
KINDS = ("profile", "sibling", "segment", "none")

# Digits of UNSPSC code carried by each hierarchy level.
_LEVEL_DIGITS = {"L1": 2, "L2": 4, "L3": 6, "L4": 8}


@dataclass(frozen=True)
class RefEntry:
    code: str       # 8 digits, trailing zeros for L1-L3
    level: str      # L1..L4
    description: str

    @property
    def prefix(self) -> str:
        return self.code[:_LEVEL_DIGITS[self.level]]


@dataclass(frozen=True)
class Option:
    key: str                 # the text the model sees as the option name
    kind: str                # profile | sibling | segment | none
    prefix: str              # UNSPSC prefix it covers; '' for none
    description: object      # criteria value sent to the model


def load_reference(path: Path = REFERENCE_CSV) -> dict[str, RefEntry]:
    """
    The UNSPSC side of the PSPC file, keyed by bare prefix (e.g. '8111').

    Reads only UNSPSC_COLUMNS, by index. One UNSPSC code appears once per GSIN
    it was linked to; the first occurrence is kept, and the UNSPSC fields are
    identical across those duplicates because they describe the UNSPSC code.
    """
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found. It is the PSPC reference file that "
            f"scripts/unspsc_discover.py downloads; run that once.")
    out: dict[str, RefEntry] = {}
    with open(path, encoding="utf-8-sig", newline="") as fh:
        reader = csv.reader(fh)
        header = next(reader)
        missing = [c for c in UNSPSC_COLUMNS if c not in header]
        if missing:
            raise ValueError(f"reference file lacks columns {missing}")
        idx = [header.index(c) for c in UNSPSC_COLUMNS]
        for row in reader:
            code, desc, level = (row[i].strip() for i in idx)
            if level not in _LEVEL_DIGITS or len(code) != 8 or not code.isdigit():
                continue
            entry = RefEntry(code, level, desc)
            out.setdefault(entry.prefix, entry)
    return out


def _children(ref: dict[str, RefEntry], prefix: str) -> list[str]:
    """Descriptions one level below `prefix`, sorted by code."""
    width = len(prefix) + 2
    return [e.description for p, e in sorted(ref.items())
            if len(p) == width and p.startswith(prefix)]


def _describe(ref: dict[str, RefEntry], prefix: str, excluding: list[str]) -> object:
    entry = ref.get(prefix)
    if entry is None:
        raise KeyError(f"UNSPSC {prefix} is not in the reference file")
    parts: dict = {"what": entry.description}
    kids = _children(ref, prefix)
    if kids:
        parts["includes"] = kids
    if excluding:
        parts["not_for"] = [f"{ref[x].description} ({x}), which is a separate option"
                            for x in excluding]
    return parts


def build_options(ref: dict[str, RefEntry], profile_families: list[str],
                  segments: tuple[str, ...]) -> tuple[Option, ...]:
    """
    Every option, in a fixed order. Pure: same inputs, same tuple, same bytes.

    Refuses a profile entry the reference file does not know, rather than
    sending the model an option with no description.
    """
    profile = [str(f).strip() for f in profile_families]
    options: list[Option] = []

    for fam in profile:
        entry = ref.get(fam)
        if entry is None:
            raise KeyError(f"profile family {fam!r} is not in the reference file")
        options.append(Option(f"{fam} {entry.description}", "profile", fam,
                              _describe(ref, fam, [])))

    profile_segments = sorted({f[:2] for f in profile})
    profile_set = set(profile)
    for prefix, entry in sorted(ref.items()):
        if entry.level != "L2" or prefix[:2] not in profile_segments:
            continue
        if prefix in profile_set:
            continue
        carved = sorted(f for f in profile if len(f) > 4 and f.startswith(prefix))
        key = f"{prefix} {entry.description}"
        if carved:
            key += f" (other than {', '.join(carved)})"
        options.append(Option(key, "sibling", prefix, _describe(ref, prefix, carved)))

    clash = set(segments) & set(profile_segments)
    if clash:
        raise ValueError(f"segments {sorted(clash)} are already split into families")
    for seg in segments:
        entry = ref.get(seg)
        if entry is None:
            raise KeyError(f"segment {seg!r} is not in the reference file")
        options.append(Option(f"{seg} {entry.description}", "segment", seg,
                              _describe(ref, seg, [])))

    options.append(Option(NONE_KEY, "none", "", {
        "what": "The main goods or services being bought fall in none of the "
                "other listed families or segments."}))

    keys = [o.key for o in options]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate option keys")
    return tuple(options)


def map_code(code: str, options: tuple[Option, ...]) -> str:
    """
    One publisher code -> an option key, or UNMAPPABLE.

    Longest matching prefix wins, so a carved-out profile entry beats the
    sibling family it sits inside.
    """
    best = None
    for opt in options:
        if opt.prefix and code.startswith(opt.prefix):
            if best is None or len(opt.prefix) > len(best.prefix):
                best = opt
    if best is not None:
        return best.key
    profile_segments = {o.prefix[:2] for o in options if o.kind == "profile"}
    if code[:2] in profile_segments:
        return UNMAPPABLE
    return NONE_KEY


def describe_code(code: str, ref: dict[str, RefEntry]) -> str:
    """
    The English UNSPSC description of an 8-digit code, at the level it was
    filed: 81112000 is a class and reads as the class, 81000000 as the segment.
    Walks commodity -> class -> family -> segment, taking a coarser level only
    when the trailing digits are zero, so a commodity is never silently read
    as its parent.
    """
    for width in (8, 6, 4, 2):
        if code[width:].strip("0"):
            break
        entry = ref.get(code[:width])
        if entry is not None:
            return entry.description
    return "(not in the PSPC reference file)"


def label_set(codes: set[str], options: tuple[Option, ...]) -> tuple[frozenset, int]:
    """
    The publisher's codes as a set of option keys, and how many codes were
    unmappable. An empty set with unmappable codes means the notice cannot be
    scored on set agreement at all.
    """
    labels, unmappable = set(), 0
    for code in codes:
        key = map_code(code, options)
        if key == UNMAPPABLE:
            unmappable += 1
        else:
            labels.add(key)
    return frozenset(labels), unmappable
