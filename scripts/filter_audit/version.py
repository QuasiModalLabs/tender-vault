"""
Filter version identity - what makes two audit records comparable, or not.

WHY NOT profile_sha256 ALONE. ingest.py's corpus_identity() hashes the feed and
the profile and deliberately refuses to hash the filter code, for a reason about
--skip-unchanged rather than about versioning. The consequence is stated in its
own docstring: a change to the filter alters the corpus without altering either
input. The refinement the vault has already proposed - stop matching keywords
inside URLs - changes no profile byte at all. A version keyed on the profile
would call that the same filter.

So a version is a STRUCTURED TRIPLE, not one opaque hash, because the three
answer different questions:

  profile_sha256         what we are looking for   (families, competencies, exclusions)
  predicates_sha256      how we decide             (the IDENTITY key)
  stage_manifest_sha256  what the funnel is shaped like (the COMPARABILITY key)

`predicates_sha256` is deliberately coarse: it hashes the whole module, so a
comment edit bumps the version. In a repo this comment-heavy that means churn,
and that is the safe direction - a version that moves too often costs a re-run,
a version that moves too rarely reports two different filters under one name.

`stage_manifest_sha256` absorbs the cost. It moves only when a stage is added,
removed, reordered, renamed, or changes whether it drops or is active. So
`compare` can distinguish "same funnel shape, different predicate bodies, these
are A/B comparable" from "the funnel itself changed, these confusion matrices
are not comparable". One hash cannot make that distinction.

NEWLINE NORMALIZATION IS NOT COSMETIC. .gitattributes normalizes *.md and
*.yaml but not *.py, so a CRLF checkout and an LF checkout of the same commit
would otherwise produce different predicates_sha256 and make two machines
disagree about which filter they ran. Fixed in both places: `*.py text eol=lf`
is added to .gitattributes, AND the bytes are normalized here - because
.gitattributes only bites on a fresh checkout or an explicit renormalize.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Optional

import yaml

from . import predicates

PROJECT_ROOT = Path(__file__).parent.parent.parent
DEFAULT_PROFILE = PROJECT_ROOT / "vault" / "profiles" / "my-company.md"
VERSIONS_FILE = PROJECT_ROOT / "vault" / "reference" / "filter-versions.yaml"

_PREDICATES_FILE = Path(predicates.__file__)


def sha256_text(path: Path) -> str:
    """
    Content hash with newlines normalized to LF.

    See the module docstring: without this, the same commit hashes differently
    on a CRLF checkout and the two machines silently disagree about which filter
    they ran.
    """
    raw = path.read_bytes().replace(b"\r\n", b"\n")
    return hashlib.sha256(raw).hexdigest()


def stage_manifest() -> list:
    """
    The funnel's shape, and nothing about how any stage decides.

    Order, name, whether it can drop, whether it is active. A predicate body
    change must NOT move this - that is the entire point of keeping it separate
    from predicates_sha256.
    """
    return [[s.order, s.name, s.drops, s.active] for s in predicates.STAGES]


def filter_version(profile_path: Optional[Path] = None) -> dict:
    """The triple, plus the short label that names it in every audit record."""
    profile_path = Path(profile_path or DEFAULT_PROFILE)
    profile = sha256_text(profile_path)
    preds = sha256_text(_PREDICATES_FILE)
    manifest = hashlib.sha256(
        json.dumps(stage_manifest(), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    label = "fv-" + hashlib.sha256(
        (profile + preds + manifest).encode()).hexdigest()[:8]
    return {
        "label": label,
        "profile_sha256": profile,
        "predicates_sha256": preds,
        "stage_manifest_sha256": manifest,
        # as_posix, not str: a committed registry with backslashes in it reads
        # differently on every machine that is not Windows, and this file is the
        # cross-machine record of what ran.
        "profile_path": profile_path.relative_to(PROJECT_ROOT).as_posix(),
    }


def comparable(base: dict, candidate: dict) -> tuple:
    """
    Whether two versions can share a confusion matrix.

    Same stage manifest means the funnel has the same shape and the matrices
    describe the same seven questions. A different manifest means a stage moved,
    and comparing the two would be arithmetic across two different definitions
    of what admission means.
    """
    if base["stage_manifest_sha256"] == candidate["stage_manifest_sha256"]:
        return True, "same stage manifest"
    return False, (
        "stage manifest differs - a stage was added, removed, reordered, renamed "
        "or had its drops/active flag changed. The funnels are not the same shape, "
        "so their confusion matrices are not comparable."
    )


def load_registry() -> list:
    """Registered versions, oldest first. Missing file is an empty registry."""
    if not VERSIONS_FILE.exists():
        return []
    loaded = yaml.safe_load(VERSIONS_FILE.read_text(encoding="utf-8")) or {}
    return loaded.get("versions", [])


def register(note: str, profile_path: Optional[Path] = None) -> dict:
    """
    Append this version to the registry.

    APPEND-ONLY, and re-registering an identical triple is a no-op rather than a
    duplicate. Historical audit records point at labels here; rewriting an entry
    would retroactively change what a past run claims to have been.
    """
    version = filter_version(profile_path)
    registry = load_registry()
    for existing in registry:
        if existing.get("label") == version["label"]:
            return {"registered": False, "reason": "already registered",
                    "version": existing}

    from datetime import datetime
    entry = dict(version)
    entry["declared"] = datetime.now().strftime("%Y-%m-%d")
    entry["note"] = note
    registry.append(entry)

    VERSIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    header = (
        "# Filter versions, append-only.\n"
        "#\n"
        "# A version is a triple, not one hash - see scripts/filter_audit/version.py.\n"
        "# `predicates_sha256` is the identity key and is coarse on purpose (a comment\n"
        "# edit bumps it). `stage_manifest_sha256` is the comparability key and moves\n"
        "# only when the funnel changes shape.\n"
        "#\n"
        "# NEVER edit or remove an entry. Audit records reference these labels, and a\n"
        "# rewritten entry retroactively changes what a past run claims to have been.\n"
    )
    VERSIONS_FILE.write_text(
        header + yaml.safe_dump({"versions": registry}, sort_keys=False,
                                default_flow_style=False, allow_unicode=True),
        encoding="utf-8", newline="\n")
    return {"registered": True, "version": entry}
