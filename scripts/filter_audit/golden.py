"""
The golden evaluation set: what the filter SHOULD do, held still so versions can be compared.

EVERY ENTRY FREEZES ITS RAW ROW, INCLUDING ARCHIVE-SOURCED ONES, and that is a
deliberate bend of "do not duplicate notices.db". Two reasons, both about
reproducibility rather than convenience:

  .cache/tenders.csv is overwritten on every download, so a feed-sourced entry
  has no other home. Two of the four real cases live only there.

  data/notices.db is gitignored and 120 MB, so a set that referenced it could
  not be evaluated on a fresh clone or in CI - and reproducible across machines
  is a requirement, not a nicety.

Thirty-odd frozen rows is evidence, not a copy of a corpus. `source` still
records where each row came from and therefore whether it is re-derivable, and
`verify` compares frozen rows against the archive whenever the archive is
present, REPORTING any disagreement rather than silently preferring either side.

THE LEAK THIS SET COULD HAVE. If entries were only ever promoted from reviewed
rejects, the set would be definitionally the population the filter gets wrong,
and "recall" measured on it would be recall over a set built to fail. So the
clearly_relevant and clearly_irrelevant strata are drawn by a deterministic rule
at a recorded seed BEFORE any review, and evaluate() reports the strata
separately so the reader can see how the set was built.

EVERY ENTRY CARRIES ITS OWN as_of, frozen. Evaluating a 2022 notice against
today's clock would reject it at stage 1 and measure nothing.

NO COMPOSITE SCORE. Precision and recall are two observed numbers with intervals
and n, never combined into one figure that decides which version is better. The
repo prohibits composite scoring, and it is right to: a single number is exactly
what lets "admits more" masquerade as "is better".
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Optional

import yaml

import ingest
from backtest import wilson

from . import predicates as P

PROJECT_ROOT = Path(__file__).parent.parent.parent
GOLDEN_FILE = PROJECT_ROOT / "vault" / "reference" / "filter-golden-set.yaml"
NOTICES_DB = PROJECT_ROOT / "data" / "notices.db"
DEFAULT_PROFILE = PROJECT_ROOT / "vault" / "profiles" / "my-company.md"

CLASSES = ("clearly_relevant", "clearly_irrelevant", "known_false_negative",
           "known_false_positive", "edge_case")

FROZEN_FIELDS = ("reference_number", "title", "description", "closing_date",
                 "publication_date", "contracting_entity", "end_user",
                 "notice_type", "procurement_category", "unspsc", "gsin")


def row_sha256(frozen_row: dict) -> str:
    """Content hash of a frozen row, so drift against the archive is detectable."""
    payload = json.dumps({k: frozen_row.get(k) for k in FROZEN_FIELDS},
                         sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


def load(path: Optional[Path] = None) -> dict:
    path = Path(path or GOLDEN_FILE)
    if not path.exists():
        return {"entries": [], "set_version": 0}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {"entries": []}


def evaluate(variant_id: Optional[str] = None,
             profile_path: Optional[Path] = None,
             path: Optional[Path] = None) -> dict:
    """
    Run one filter version over the whole set and score it.

    Positive class is ADMIT. Entries with `expected: null` are excluded from the
    matrix and reported separately - scoring an entry nobody agreed on
    manufactures a number.
    """
    data = load(path)
    entries = data.get("entries", [])
    if not entries:
        return {"error": f"No golden set at {path or GOLDEN_FILE}."}

    criteria = ingest.parse_profile(Path(profile_path or DEFAULT_PROFILE))
    decide = None
    if variant_id:
        from .variants import get_variant
        decide = get_variant(variant_id)

    matrix = {"TP": 0, "FP": 0, "TN": 0, "FN": 0}
    per_entry = []
    no_consensus = []
    by_class: dict = {}

    for entry in entries:
        notice = P.Notice.from_frozen_row(entry["frozen_row"])
        as_of = entry.get("as_of")
        if decide is not None:
            production, _ = decide(notice, criteria, as_of)
        else:
            production = P.production_decision(notice, criteria, as_of)
        admitted = production.admitted
        expected = entry.get("expected")

        record = {
            "id": entry["id"],
            "reference_number": entry["reference_number"],
            "class": entry["class"],
            "expected": expected,
            "got": "ADMIT" if admitted else "REJECT",
            "first_rejecting_stage": production.first_rejecting_stage,
            "correct": None,
        }
        if expected is None:
            no_consensus.append(record)
            per_entry.append(record)
            continue

        want_admit = expected == "ADMIT"
        record["correct"] = (admitted == want_admit)
        if want_admit and admitted:
            cell = "TP"
        elif want_admit and not admitted:
            cell = "FN"
        elif not want_admit and admitted:
            cell = "FP"
        else:
            cell = "TN"
        matrix[cell] += 1
        record["cell"] = cell
        per_entry.append(record)

        bucket = by_class.setdefault(entry["class"], {"n": 0, "correct": 0})
        bucket["n"] += 1
        bucket["correct"] += int(record["correct"])

    tp, fp, fn = matrix["TP"], matrix["FP"], matrix["FN"]
    precision = tp / (tp + fp) if (tp + fp) else None
    recall = tp / (tp + fn) if (tp + fn) else None

    return {
        "filter": variant_id or "current",
        "set_version": data.get("set_version"),
        "scored": sum(matrix.values()),
        "excluded_no_consensus": len(no_consensus),
        "matrix": matrix,
        "precision": precision,
        "precision_ci": wilson(tp, tp + fp) if (tp + fp) else None,
        "recall": recall,
        "recall_ci": wilson(tp, tp + fn) if (tp + fn) else None,
        "by_class": by_class,
        "per_entry": per_entry,
        "no_consensus": no_consensus,
    }


def verify(path: Optional[Path] = None,
           notices_db: Path = NOTICES_DB) -> dict:
    """
    Frozen rows against the archive, where the archive is available.

    REPORTS disagreement; does not resolve it. A frozen row that no longer
    matches the archive could mean the archive was rebuilt or the entry was
    edited, and preferring either silently would destroy the evidence that they
    ever differed.
    """
    data = load(path)
    entries = data.get("entries", [])
    if not notices_db.exists():
        return {"skipped": True,
                "reason": f"No archive at {notices_db}; frozen rows are "
                          f"self-contained, so the set still evaluates - this "
                          f"check simply could not run."}
    import sqlite3
    conn = sqlite3.connect(f"file:{notices_db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    checked = agreed = 0
    absent = []
    disagreed = []
    for entry in entries:
        stored_hash = entry.get("frozen_row_sha256")
        recomputed = row_sha256(entry["frozen_row"])
        if stored_hash and stored_hash != recomputed:
            disagreed.append({"id": entry["id"], "kind": "frozen_row edited",
                              "detail": "frozen_row_sha256 does not match the row"})
        if entry.get("source") != "archive":
            continue
        row = conn.execute("SELECT * FROM notices WHERE reference_number=?",
                           (entry["reference_number"],)).fetchone()
        if row is None:
            absent.append(entry["id"])
            continue
        checked += 1
        live = {k: row[k] for k in FROZEN_FIELDS if k in row.keys()}
        if row_sha256(live) == recomputed:
            agreed += 1
        else:
            differing = [k for k in FROZEN_FIELDS
                         if k in row.keys()
                         and str(row[k] or "") != str(entry["frozen_row"].get(k) or "")]
            disagreed.append({"id": entry["id"], "kind": "archive differs",
                              "fields": differing})
    conn.close()
    return {"skipped": False, "archive_checked": checked, "agreed": agreed,
            "absent_from_archive": absent, "disagreed": disagreed,
            "note": "Disagreement is reported, never resolved. A frozen row and "
                    "the archive differing is itself the finding."}


def render(result: dict) -> str:
    if "error" in result:
        return f"ERROR: {result['error']}"
    matrix = result["matrix"]

    def pct(rate, interval):
        if rate is None:
            return "n/a"
        low, high = interval
        return f"{rate:.3f}  (95% CI {low:.3f}-{high:.3f})"

    lines = [
        f"Golden set v{result['set_version']} - {result['scored']} scored, "
        f"{result['excluded_no_consensus']} excluded (expected: null)",
        f"  filter    {result['filter']}",
        f"  TP {matrix['TP']}   FP {matrix['FP']}   TN {matrix['TN']}   FN {matrix['FN']}",
        f"  precision {pct(result['precision'], result['precision_ci'])}",
        f"  recall    {pct(result['recall'], result['recall_ci'])}",
        "",
        "  by class (how the set is built - read this before the rates)",
    ]
    for name, bucket in sorted(result["by_class"].items()):
        lines.append(f"    {name:<24} {bucket['correct']}/{bucket['n']} correct")
    wrong = [e for e in result["per_entry"] if e.get("correct") is False]
    lines.append("")
    if wrong:
        lines.append(f"  WRONG ({len(wrong)}) - the part that means something at this n")
        for entry in wrong:
            lines.append(f"    {entry['id']:<36} want {entry['expected']:<6} "
                         f"got {entry['got']:<6} "
                         f"({entry['first_rejecting_stage'] or 'admitted'})")
    else:
        lines.append("  no entry scored wrong")
    lines.append("")
    lines.append("  At this n a rate to three decimals is theatre. Read the "
                 "per-entry table and the intervals, not the point estimates.")
    return "\n".join(lines)
