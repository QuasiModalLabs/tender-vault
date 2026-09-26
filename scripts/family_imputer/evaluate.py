"""
Impute over the coded notices, then - and only then - score against their codes.

------------------------------------------------------------------------------
THE ORDER IS THE BLINDING
------------------------------------------------------------------------------
`load_coded()` returns two things kept apart: the blind items (notice id and an
ImputerState) and the answer key (notice id -> codes). `impute()` and the
keyword comparator receive only the blind items. The answer key is read by
`score()` alone, after both have answered. This module is the only place in the
package that reads `unspsc`.

------------------------------------------------------------------------------
THE HEADLINE IS THE ADMIT CLASS
------------------------------------------------------------------------------
Most coded notices sit outside segments 43/80/81, so agreement over all options
is dominated by the majority classes and a model that always answered the
biggest segment would score respectably on it. So the headline is two numbers
on the class the decision turns on:

  publisher admits   production's own coded-branch test:
                     ingest.matches_unspsc_families(codes, families) non-empty
  Jev admits         Jev's top choice is a profile option
  keyword admits     ingest.matched_competencies fired on the full text

  recall     of publisher-admits, the share each method admits
  precision  of each method's admits, the share the publisher also admits

Beside them, the majority-class baseline ("never admit"): recall 0, precision
undefined. Overall agreement is a diagnostic and is labelled one.

------------------------------------------------------------------------------
THE DECISION RULE IS NOT HERE TO BE TUNED
------------------------------------------------------------------------------
RULE restates the rule committed in ref-004 before any run. `pilot` and `run`
refuse unless that commit exists and the working-tree rule section still
matches it byte for byte - see preregistration_check(). The rule is evaluated
on point estimates; Wilson intervals are printed beside every number.
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
import random
import re
import sqlite3
import subprocess
import sys
from collections import Counter, defaultdict
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import ingest  # noqa: E402

from . import cache
from .client import JevAuthError, JevClient, JevError, ModelMismatch, validate_answer
from .comparator import keyword_hits
from .options import NONE_KEY, label_set
from .question import MODEL, Question
from .state import STATE_CHAR_BUDGET, ImputerState, prepare

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
NOTICES_DB = PROJECT_ROOT / "data" / "notices.db"
REF_004 = (PROJECT_ROOT / "vault" / "reference" / "filter-refinements"
           / "ref-004-jev-family-imputer.md")
RULE_HEADING = "## Pre-registered decision rule"

REPORT_JSON = cache.DATA_DIR / "report.json"
CONFUSION_CSV = cache.DATA_DIR / "confusion.csv"
LABELS_JSONL = cache.DATA_DIR / "disagreements.jsonl"
# The labelling standard, in one place. The label command validates against
# these keys and the reading sheet prints these definitions, so the standard
# the human reads is the standard the tool enforces.
#
# out_of_scope was added 2026-09-24, before any label was recorded. Reading the
# 187 notices both methods missed turned up correctly-coded IT-adjacent work
# the profile would not bid. Filing those as jev_wrong would blame the model
# for a profile boundary; filing them as publisher_miscoded would blame a
# correctly filed code.
LABEL_DEFINITIONS = {
    "jev_wrong": (
        "The publisher's code describes what the notice buys, and Jev's top "
        "choice does not."),
    "publisher_miscoded": (
        "Jev's top choice describes what the notice buys, and the publisher's "
        "code does not - wrong, used as a generic service code, or attached "
        "to something that is not a purchase."),
    "out_of_scope": (
        "The publisher's code is defensible for what is bought and the notice "
        "is IT-adjacent, but it is not work the profile would bid - e.g. "
        "vendor training, building automation, staffing roles filed under IT "
        "codes. A profile boundary, not a model or publisher error."),
    # The three below were added 2026-09-24 from the definitions at the top
    # of the reviewed sheet, before its labels were ingested. Without them
    # ten-plus cases would have been forced into the first three kinds, and the
    # tally would read far more confident than the reading was.
    "profile_gap": (
        "The publisher coded correctly and Jev answered correctly; it scored as "
        "a disagreement only because the code sits in a family "
        "unspsc_families does not list (e.g. 8010, 8016, 8110). Neither error. "
        "Points at a filter refinement with nothing to do with Jev."),
    "no_description": (
        "The description is empty or pure boilerplate, so the notice was "
        "classified from a title or less. An input ceiling that binds every "
        "method equally."),
    "unsure": (
        "Two kinds are both defensible on the facts available. Not a hedge to "
        "be resolved by picking the likelier one; the record carries the "
        "candidate kinds and what the call turns on."),
}
LABEL_KINDS = tuple(LABEL_DEFINITIONS)
LABELLERS = ("human", "assistant")

# docs.typesafe.ai/models, read 2026-09-23: input $0.042 per million tokens,
# output tokens free. Recorded with its source because a price is a fact that
# goes stale.
PRICE_PER_MILLION_INPUT = 0.042
PRICE_SOURCE = "docs.typesafe.ai/models (read 2026-09-23): $0.042/M input, output free"

PILOT_N = 200
SEED = 20260923
SAMPLE_PER_SIDE = 15

POPULATION_CAVEAT = (
    "POPULATION: every coded notice is WS or cb. These numbers are evidence "
    "about the model, not a measurement on the PW, SSC and MX notices the "
    "imputer would actually serve.")
KEYWORD_HANDICAP = (
    "KEYWORD ROW: matched_competencies never runs on WS/cb notices in "
    "production (they are decided on their codes), so a large keyword deficit "
    "here is partly an artefact of this population, not evidence about PW, SSC "
    "and MX prose. It does not change the rule; it changes how a big gap reads.")

# Restated from ref-004. The file is the authority; this is what executes it.
RULE = {
    "proceed": "jev_recall >= kw_recall + 0.10 AND jev_precision >= "
               "kw_precision - 0.05 AND jev_recall >= 0.60",
    "kill": "jev_recall <= kw_recall OR jev_precision < kw_precision - 0.15",
    "otherwise": "INCONCLUSIVE - returned to the human",
}


# ---------------------------------------------------------------------------
# Pre-registration gate
# ---------------------------------------------------------------------------

class NotPreregistered(RuntimeError):
    """The decision rule is not committed, or has been edited since."""


def _rule_section(text: str) -> str:
    start = text.find(RULE_HEADING)
    if start < 0:
        return ""
    end = text.find("\n## ", start + len(RULE_HEADING))
    return text[start:end if end >= 0 else len(text)].strip()


def preregistration_check(question_sha256: str) -> str:
    """
    Refuse to spend on imputation unless ref-004's rule was committed first.

    Finds the commit that ADDED ref-004 and requires (a) its rule section to
    name the frozen question hash and (b) the working tree's rule section to
    equal it exactly - so the rule cannot be edited after the run and still
    pass. Returns the commit id.
    """
    rel = REF_004.relative_to(PROJECT_ROOT).as_posix()
    try:
        added = subprocess.run(
            ["git", "log", "--diff-filter=A", "--format=%H", "--", rel],
            cwd=PROJECT_ROOT, capture_output=True, text=True, check=True
        ).stdout.split()
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        raise NotPreregistered(f"cannot read git history: {exc}") from None
    if not added:
        raise NotPreregistered(
            f"{rel} has never been committed. The decision rule must be "
            f"committed before any imputation run.")
    commit = added[-1]
    committed = subprocess.run(["git", "show", f"{commit}:{rel}"], cwd=PROJECT_ROOT,
                               capture_output=True, text=True, encoding="utf-8",
                               check=True).stdout
    committed_rule = _rule_section(committed)
    if not committed_rule or question_sha256 not in committed_rule:
        raise NotPreregistered(
            f"the rule committed in {commit[:10]} does not name question "
            f"{question_sha256[:16]}..")
    live_rule = _rule_section(REF_004.read_text(encoding="utf-8"))
    if live_rule != committed_rule:
        raise NotPreregistered(
            f"the rule section of {rel} differs from the version committed in "
            f"{commit[:10]}. A pre-registered rule is not revised.")
    return commit


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

def load_coded(db: Path = NOTICES_DB):
    """
    (blind items, answer key). Blind items are [(notice_id, ImputerState)];
    the answer key maps notice_id -> codes and must not be passed to the
    imputer or the comparator.
    """
    if not db.exists():
        raise FileNotFoundError(f"{db} not found - run scripts/notices_ingest.py")
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    blind, answers = [], {}
    for row in conn.execute(
            "SELECT reference_number, title, description, unspsc FROM notices "
            "ORDER BY reference_number"):
        codes = ingest.parse_unspsc_codes(row["unspsc"])
        if not codes:
            continue
        nid = row["reference_number"]
        blind.append((nid, ImputerState.from_row(row)))
        answers[nid] = codes
    conn.close()
    return blind, answers


def pilot_items(blind: list, n: int = PILOT_N) -> list:
    rng = random.Random(SEED)
    return sorted(rng.sample(blind, min(n, len(blind))), key=lambda x: x[0])


# ---------------------------------------------------------------------------
# Imputation
# ---------------------------------------------------------------------------

def impute(blind: list, question: Question, purpose: str, client: JevClient,
           conn, workers: int = 8, max_errors: int = 25, echo=print) -> dict:
    """
    Ask Jev about every blind item not already cached. Only ImputerStates reach
    the client; the thread calls nothing but client.ask, and validation and
    caching happen on this thread.
    """
    todo = []
    hits = 0
    for nid, state in blind:
        prepared = prepare(state)
        if cache.get(conn, nid, prepared.content_sha256, MODEL, question.sha256):
            hits += 1
        else:
            todo.append((nid, prepared))
    echo(f"{purpose}: {len(blind)} notices, {hits} cached, {len(todo)} to call")

    stats = {"requested": len(blind), "cache_hits": hits, "called": 0,
             "stored": 0, "errors": 0, "input_tokens": 0}
    if not todo:
        return stats

    # A SLIDING WINDOW, NOT A QUEUE. At most `workers` calls are ever in
    # flight, and the next is submitted only while no abort is pending. Submitting
    # everything up front let a fast failure (a 401 on every call) race the
    # workers through the whole queue before this thread could cancel anything -
    # tests/test_family_imputer.py measured 60 of 60 sent.
    #
    # ABORTING DRAINS, IT DOES NOT ABANDON. Calls already in flight were sent and
    # may be billed, so they are consumed and logged before the abort is raised.
    errors: list[str] = []
    abort: Exception | None = None
    pending = iter(todo)
    in_flight: dict = {}

    def submit_next(pool) -> None:
        if abort is not None:
            return
        item = next(pending, None)
        if item is not None:
            in_flight[pool.submit(client.ask, item[1].payload, question.payload)] = item

    def handle(nid, prepared, fut) -> None:
        nonlocal abort
        stats["called"] += 1
        try:
            body = fut.result()
        except JevAuthError as exc:
            cache.log_call(conn, purpose, nid, None, None, None, "auth_error")
            stats["errors"] += 1
            abort = abort or exc
            return
        except JevError as exc:
            cache.log_call(conn, purpose, nid, None, None, None, "error")
            stats["errors"] += 1
            errors.append(f"{nid}: {exc}")
            if stats["errors"] >= max_errors:
                abort = abort or JevError(
                    f"{max_errors} failed calls; stopping. Last: {errors[-1]}")
            return
        usage = body.get("usage") or {}
        try:
            verdict = validate_answer(body, question.keys)
        except ModelMismatch as exc:
            cache.log_call(conn, purpose, nid, body.get("model"),
                           usage.get("input_tokens"), usage.get("output_tokens"),
                           "refused_model_mismatch")
            abort = abort or exc
            return
        except JevError as exc:
            cache.log_call(conn, purpose, nid, body.get("model"),
                           usage.get("input_tokens"), usage.get("output_tokens"),
                           "refused_malformed")
            stats["errors"] += 1
            errors.append(f"{nid}: {exc}")
            return
        cache.put(conn, nid, prepared, question.sha256, verdict)
        cache.log_call(conn, purpose, nid, verdict["model"],
                       verdict["input_tokens"], verdict["output_tokens"], "stored")
        stats["stored"] += 1
        stats["input_tokens"] += verdict["input_tokens"]

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for _ in range(workers):
            submit_next(pool)
        while in_flight:
            done, _ = wait(in_flight, return_when=FIRST_COMPLETED)
            for fut in done:
                nid, prepared = in_flight.pop(fut)
                handle(nid, prepared, fut)
                submit_next(pool)
                if stats["called"] % 250 == 0:
                    conn.commit()
                    echo(f"  {stats['called']}/{len(todo)} called, "
                         f"{stats['errors']} errors, "
                         f"{stats['input_tokens']:,} input tokens")
    conn.commit()
    if abort is not None:
        raise abort
    stats["error_samples"] = errors[:5]
    return stats


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def wilson(k: int, n: int, z: float = 1.96):
    if n == 0:
        return None
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def _rate(k: int, n: int) -> dict:
    return {"k": k, "n": n, "value": (k / n) if n else None, "wilson95": wilson(k, n)}


def admit_metrics(rows: list, key: str) -> dict:
    truth = [r for r in rows if r["publisher_admit"]]
    said = [r for r in rows if r[key]]
    both = sum(1 for r in truth if r[key])
    return {"recall": _rate(both, len(truth)), "precision": _rate(both, len(said))}


def decide(jev: dict, kw: dict) -> dict:
    jr, jp = jev["recall"]["value"], jev["precision"]["value"]
    kr, kp = kw["recall"]["value"], kw["precision"]["value"]
    if None in (jr, jp, kr, kp):
        return {"outcome": "NOT EVALUATED", "why": "a recall or precision is undefined"}
    proceed = jr >= kr + 0.10 and jp >= kp - 0.05 and jr >= 0.60
    kill = jr <= kr or jp < kp - 0.15
    outcome = "PROCEED" if proceed else "KILL" if kill else "INCONCLUSIVE"
    return {"outcome": outcome, "rule": RULE,
            "inputs": {"jev_recall": jr, "jev_precision": jp,
                       "kw_recall": kr, "kw_precision": kp}}


def score(blind: list, answers: dict, question: Question, conn,
          families: list, competencies: list) -> dict:
    kinds = {o.key: o.kind for o in question.options}
    rows, missing = [], 0
    for nid, state in blind:
        prepared = prepare(state)
        verdict = cache.get(conn, nid, prepared.content_sha256, MODEL, question.sha256)
        if verdict is None:
            missing += 1
            continue
        # --- the answer key is read from here on, and not before ---
        codes = answers[nid]
        labels, unmappable = label_set(codes, question.options)
        probs = verdict["probabilities"]
        kw = keyword_hits(state, competencies)
        rows.append({
            "notice_id": nid,
            "source": ingest._source_system(nid),
            "codes": sorted(codes),
            "segments": sorted({c[:2] for c in codes}),
            "labels": sorted(labels),
            "unmappable_codes": unmappable,
            "publisher_admit": bool(ingest.matches_unspsc_families(codes, families)),
            "choice": verdict["choice"],
            "choice_p": probs[verdict["choice"]],
            "probabilities": probs,
            "p_profile": sum(p for k, p in probs.items() if kinds[k] == "profile"),
            "jev_admit": kinds[verdict["choice"]] == "profile",
            "kw_hits": kw,
            "kw_admit": bool(kw),
            "truncated": bool(verdict["truncated"]),
            "title": state.title,
            "description": state.description,
        })

    total = len(blind)
    out: dict = {"coverage": {"coded_notices": total, "imputed": len(rows),
                              "missing": missing}}
    if not rows:
        return out

    jev = admit_metrics(rows, "jev_admit")
    kw = admit_metrics(rows, "kw_admit")
    out["headline"] = {
        "jev": jev, "keyword": kw,
        "majority_baseline": {"recall": 0.0, "precision": None,
                              "note": "never admit: recall 0, precision undefined"},
        "publisher_admits": sum(r["publisher_admit"] for r in rows),
        "population_caveat": POPULATION_CAVEAT,
        "keyword_handicap": KEYWORD_HANDICAP,
    }
    out["decision"] = (decide(jev, kw) if missing == 0 else
                       {"outcome": "NOT EVALUATED",
                        "why": f"{missing} coded notices have no verdict yet"})

    out["by_source"] = {
        src: {"n": len(sub), "jev": admit_metrics(sub, "jev_admit"),
              "keyword": admit_metrics(sub, "kw_admit")}
        for src in sorted({r["source"] for r in rows})
        for sub in [[r for r in rows if r["source"] == src]]
    }

    # ---- diagnostics ----
    scorable = [r for r in rows if r["labels"]]
    agree = sum(r["choice"] in r["labels"] for r in scorable)
    option_cover = Counter(k for r in scorable for k in r["labels"])
    best_const, best_n = option_cover.most_common(1)[0]
    single = [r for r in scorable if len(r["labels"]) == 1]
    diag = {
        "set_agreement": _rate(agree, len(scorable)),
        "set_agreement_majority_baseline": {
            "constant_answer": best_const, **_rate(best_n, len(scorable))},
        "strict_agreement_single_label": _rate(
            sum(r["choice"] == r["labels"][0] for r in single), len(single)),
        "unscorable_empty_label_set": len(rows) - len(scorable),
        "notices_with_unmappable_codes": sum(1 for r in rows if r["unmappable_codes"]),
        "truncated": sum(r["truncated"] for r in rows),
        "multi_label": len(scorable) - len(single),
    }

    seg_order = sorted({s for r in rows for s in r["segments"]},
                       key=lambda s: -sum(s in r["segments"] for r in rows))
    by_segment = {}
    for seg in seg_order:
        sub = [r for r in rows if seg in r["segments"]]
        sc = [r for r in sub if r["labels"]]
        by_segment[seg] = {
            "n": len(sub),
            "set_agreement": _rate(sum(r["choice"] in r["labels"] for r in sc), len(sc)),
            "publisher_admit": sum(r["publisher_admit"] for r in sub),
            "jev_admit": sum(r["jev_admit"] for r in sub),
            "kw_admit": sum(r["kw_admit"] for r in sub),
        }
    diag["by_segment_note"] = ("a notice filing codes in several segments is "
                               "counted under each")
    diag["by_segment"] = by_segment

    deciles = defaultdict(lambda: [0, 0])
    for r in rows:
        b = min(9, int(r["p_profile"] * 10))
        deciles[b][0] += 1
        deciles[b][1] += r["publisher_admit"]
    diag["p_profile_deciles"] = [
        {"bucket": f"{b/10:.1f}-{(b+1)/10:.1f}", "n": n, "publisher_admit": k,
         "publisher_admit_rate": (k / n) if n else None}
        for b, (n, k) in sorted(deciles.items())]

    confusion = Counter((r["labels"][0], r["choice"]) for r in single)
    diag["confusion_note"] = (f"single-label notices only ({len(single)}); "
                              f"{diag['multi_label']} multi-label excluded")
    out["diagnostics"] = diag
    out["_confusion"] = confusion
    out["_rows"] = rows
    return out


def disagreement_sample(rows: list, per_side: int = SAMPLE_PER_SIDE) -> list:
    """Seeded, both directions of admit-class disagreement."""
    rng = random.Random(SEED)
    jev_only = [r for r in rows if r["jev_admit"] and not r["publisher_admit"]]
    pub_only = [r for r in rows if r["publisher_admit"] and not r["jev_admit"]]
    a = rng.sample(jev_only, min(per_side, len(jev_only)))
    b = rng.sample(pub_only, min(per_side, len(pub_only)))
    short = 2 * per_side - len(a) - len(b)
    if short > 0:  # top up from whichever side has more
        rest_a = [r for r in jev_only if r not in a]
        rest_b = [r for r in pub_only if r not in b]
        extra = rng.sample(rest_a + rest_b, min(short, len(rest_a) + len(rest_b)))
        a, b = a + [r for r in extra if r["jev_admit"]], b + [r for r in extra if not r["jev_admit"]]
    return ([dict(r, direction="jev_admits_publisher_did_not") for r in a] +
            [dict(r, direction="publisher_admits_jev_did_not") for r in b])


def write_outputs(result: dict, question: Question, spend: dict, sample: list) -> None:
    cache.DATA_DIR.mkdir(parents=True, exist_ok=True)
    keys = question.keys
    confusion = result.get("_confusion", Counter())
    with open(CONFUSION_CSV, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["publisher \\ jev"] + list(keys))
        for pk in keys:
            w.writerow([pk] + [confusion.get((pk, jk), 0) for jk in keys])
    public = {k: v for k, v in result.items() if not k.startswith("_")}
    public.update({
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "model": MODEL, "question_sha256": question.sha256,
        "spend": spend, "price_source": PRICE_SOURCE,
        "disagreement_sample": [
            {k: r[k] for k in ("notice_id", "direction", "codes", "labels",
                               "choice", "choice_p", "kw_hits")} for r in sample],
    })
    REPORT_JSON.write_text(json.dumps(public, indent=2, default=str), encoding="utf-8")


def cost(spend: dict) -> dict:
    tokens = sum(v["input_tokens"] for v in spend.values())
    return {"input_tokens": tokens,
            "output_tokens": sum(v["output_tokens"] for v in spend.values()),
            "calls": sum(v["calls"] for v in spend.values()),
            "usd": tokens / 1e6 * PRICE_PER_MILLION_INPUT}


# ---------------------------------------------------------------------------
# Labelling disagreements
# ---------------------------------------------------------------------------

def load_labels(labels_path: Path = LABELS_JSONL) -> list:
    if not labels_path.exists():
        return []
    return [json.loads(line) for line in
            labels_path.read_text(encoding="utf-8").splitlines() if line.strip()]


def record_label(notice_id: str, kind: str, note: str = "", *,
                 labelled_by: str, why_unsure: str = "", source: dict = None,
                 report_path: Path = REPORT_JSON,
                 labels_path: Path = LABELS_JSONL) -> dict:
    """
    Append one label to the scratch JSONL. Writes nowhere else - not
    filter-reviews.jsonl, not the golden set.

    Refuses: a kind outside LABEL_KINDS; a notice not in the printed sample;
    a second label for the same notice from the same labeller (so an ingest
    re-run cannot double-count); an `unsure` with no stated reason.

    `labelled_by` HAS NO DEFAULT, for the reason filter_audit.review gives for
    `reviewer`: a human judgement and an assistant's proposal are different
    measurements, and a default would let the log guess which one it holds.
    """
    if kind not in LABEL_KINDS:
        raise ValueError(f"kind must be one of {LABEL_KINDS}")
    if labelled_by not in LABELLERS:
        raise ValueError(f"labelled_by must be one of {LABELLERS}")
    if kind == "unsure" and not why_unsure.strip():
        raise ValueError("an unsure label must say which kinds it is between and why")
    if not report_path.exists():
        raise FileNotFoundError("no report yet - run `report` first")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    sample = {r["notice_id"]: r for r in report.get("disagreement_sample", [])}
    if notice_id not in sample:
        raise ValueError(f"{notice_id} is not in the printed disagreement sample")
    if any(r["notice_id"] == notice_id and r.get("labelled_by") == labelled_by
           for r in load_labels(labels_path)):
        raise ValueError(f"{notice_id} already has a {labelled_by} label; "
                         f"labels are append-only and are not overwritten")
    record = {
        "notice_id": notice_id, "kind": kind, "note": note,
        "why_unsure": why_unsure, "labelled_by": labelled_by,
        "direction": sample[notice_id]["direction"],
        "labelled_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "question_sha256": report["question_sha256"], "model": report["model"],
    }
    if source:
        record["source"] = source
    labels_path.parent.mkdir(parents=True, exist_ok=True)
    with open(labels_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")
    return record


class LabelSheetError(ValueError):
    """The reviewed sheet did not parse. Nothing was written."""


_BLOCK_MARK = re.compile(r"^\*\*\d{2} of \d+\*\*")
_LABEL_LINE = re.compile(r"^label:[ \t]*(.*?)\s*$")
_NOTE_LINE = re.compile(r"^note:[ \t]*(.*?)\s*$")
_WHY_LINE = re.compile(r"^\*\*Why unsure:\*\*[ \t]*(.*?)\s*$")


def parse_label_sheet(text: str, sample_ids: list) -> tuple[list, list]:
    """
    (parsed, errors) from a reviewed reading sheet.

    A NOTICE BLOCK is a `## <id>` heading followed by a `**NN of M**` line - so
    prose headings in the findings section are not mistaken for notices, while
    a mistyped id in a real block is still caught as unknown. Within a block,
    `label:`, `note:` and `**Why unsure:**` must start their line; description
    lines are quoted with `>`, so a word "label:" inside a notice cannot match.
    Nothing is guessed: every problem is returned, and the caller writes
    nothing unless the list is empty.
    """
    lines = text.replace("\r\n", "\n").split("\n")
    blocks, current = [], None
    for i, line in enumerate(lines):
        if line.startswith("## ") or line.startswith("# "):
            nxt = next((l for l in lines[i + 1:i + 4] if l.strip()), "")
            if line.startswith("## ") and _BLOCK_MARK.match(nxt):
                current = {"id": line[3:].strip(), "lines": []}
                blocks.append(current)
            else:
                current = None
            continue
        if current is not None:
            current["lines"].append(line)

    errors, parsed = [], []
    seen = Counter(b["id"] for b in blocks)
    for nid, n in seen.items():
        if n > 1:
            errors.append(f"{nid}: appears {n} times")
    for nid in sorted(set(sample_ids) - set(seen)):
        errors.append(f"{nid}: in the sample but has no block")
    for nid in sorted(set(seen) - set(sample_ids)):
        errors.append(f"{nid}: block heading is not a notice in the sample")

    for b in blocks:
        labels = [m.group(1) for l in b["lines"] if (m := _LABEL_LINE.match(l))]
        notes = [m.group(1) for l in b["lines"] if (m := _NOTE_LINE.match(l))]
        whys = [m.group(1) for l in b["lines"] if (m := _WHY_LINE.match(l))]
        where = b["id"]
        if len(labels) != 1:
            errors.append(f"{where}: {len(labels)} label: lines, need exactly 1")
            continue
        if len(notes) != 1:
            errors.append(f"{where}: {len(notes)} note: lines, need exactly 1")
            continue
        kind = labels[0].strip("` ")
        if kind not in LABEL_KINDS:
            errors.append(f"{where}: kind {labels[0]!r} is not one of {LABEL_KINDS}")
            continue
        if len(whys) > 1:
            errors.append(f"{where}: {len(whys)} Why unsure lines, need at most 1")
            continue
        why = whys[0] if whys else ""
        if kind == "unsure" and not why:
            errors.append(f"{where}: unsure with no **Why unsure:** line")
            continue
        if kind != "unsure" and why:
            errors.append(f"{where}: a Why unsure line on a {kind} label")
            continue
        parsed.append({"notice_id": b["id"], "kind": kind, "note": notes[0],
                       "why_unsure": why})
    return parsed, errors


def ingest_label_sheet(path: Path, labelled_by: str,
                       report_path: Path = REPORT_JSON,
                       labels_path: Path = LABELS_JSONL) -> list:
    """
    Parse a reviewed sheet and record every label through record_label.

    TWO PHASES. Everything is validated first - the sheet's ids against the
    printed sample, every kind, every unsure reason, and that none of these
    notices already carries a label from this labeller - and nothing is
    written unless all of it passes. Then each record goes through
    record_label, so the command's validation applies to the ingest too.
    """
    if not report_path.exists():
        raise FileNotFoundError("no report yet - run `report` first")
    raw = path.read_bytes()
    sample_ids = [r["notice_id"] for r in json.loads(
        report_path.read_text(encoding="utf-8"))["disagreement_sample"]]
    parsed, errors = parse_label_sheet(raw.decode("utf-8"), sample_ids)
    already = {r["notice_id"] for r in load_labels(labels_path)
               if r.get("labelled_by") == labelled_by}
    errors += [f"{p['notice_id']}: already has a {labelled_by} label"
               for p in parsed if p["notice_id"] in already]
    if errors:
        raise LabelSheetError(
            f"{path.name}: {len(errors)} problem(s), nothing written:\n  "
            + "\n  ".join(errors))
    try:
        shown = path.resolve().relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        shown = str(path)
    source = {"path": shown, "sha256": hashlib.sha256(raw).hexdigest()}
    order = {nid: i for i, nid in enumerate(sample_ids)}
    return [record_label(p["notice_id"], p["kind"], p["note"],
                         labelled_by=labelled_by, why_unsure=p["why_unsure"],
                         source=source, report_path=report_path,
                         labels_path=labels_path)
            for p in sorted(parsed, key=lambda p: order[p["notice_id"]])]


def label_table(labels: list) -> dict:
    """kind x direction, per labeller. Never summed across labellers."""
    out: dict = {}
    for r in labels:
        who = out.setdefault(r.get("labelled_by", "unstated"), {})
        row = who.setdefault(r["kind"], Counter())
        row[r["direction"]] += 1
    return out


# ---------------------------------------------------------------------------
# The reading sheet
# ---------------------------------------------------------------------------

SHEET_MD = cache.DATA_DIR / "disagreements.md"
DIRECTION_TEXT = {
    "jev_admits_publisher_did_not": "Jev admits / publisher didn't",
    "publisher_admits_jev_did_not": "Publisher admits / Jev didn't",
}


class SampleDrift(RuntimeError):
    """The recomputed sample is not the one the report printed."""


def write_label_sheet(sample: list, ref: dict, path: Path = SHEET_MD,
                      report_path: Path = REPORT_JSON) -> Path:
    """
    The disagreement sample as Markdown, for reading outside the terminal.

    SAME ORDER AS THE REPORT, CHECKED. The sample is recomputed from the cache
    and compared id-for-id with the one `report` wrote to report.json, and a
    mismatch refuses rather than writing a sheet whose ids `label` would reject.

    THE SHEET CARRIES NO VERDICT. It shows the notice, the publisher's codes,
    Jev's distribution and the keyword hits, with blank label and note lines.
    It suggests no label; the definitions at the top are the only guidance.
    """
    from .options import describe_code

    if not report_path.exists():
        raise FileNotFoundError("no report yet - run `report` first")
    printed = [r["notice_id"] for r in
               json.loads(report_path.read_text(encoding="utf-8"))["disagreement_sample"]]
    ids = [r["notice_id"] for r in sample]
    if ids != printed:
        raise SampleDrift("recomputed disagreement sample differs from report.json; "
                          "re-run `report` before writing the sheet")

    out = ["# Disagreement sample - reading sheet", "",
           f"{len(sample)} notices, seed {SEED}, in the order `report` printed them. "
           f"Model `{MODEL}`.",
           "",
           "## Label kinds", ""]
    for kind, definition in LABEL_DEFINITIONS.items():
        out.append(f"- **`{kind}`**: {definition}")
    out += ["", "Record each one with:", "",
            "```", "python scripts/family_imputer label <notice_id> "
            + "|".join(LABEL_KINDS) + " --note \"...\"", "```", ""]

    for i, r in enumerate(sample, 1):
        probs = sorted(r["probabilities"].items(), key=lambda kv: -kv[1])
        runners = [kv for kv in probs if kv[0] != r["choice"]][:2]
        out += ["---", "", f"## {r['notice_id']}", "",
                f"**{i:02} of {len(sample)}**: {DIRECTION_TEXT[r['direction']]}", "",
                f"**Title:** {' '.join(r['title'].split())}", "",
                "**Publisher's codes:**", ""]
        out += [f"- `{c}` {describe_code(c, ref)}" for c in r["codes"]]
        out += ["", "**Jev:**", "",
                f"- top choice: {r['choice']} (p = {r['choice_p']:.2f})"]
        out += [f"- next: {k} (p = {p:.2f})" for k, p in runners]
        out += ["", f"**Keyword hits:** {', '.join(r['kw_hits']) or 'none'}", ""]
        if r.get("truncated"):
            out += [f"*Jev saw this description cut to {STATE_CHAR_BUDGET:,} "
                    "characters; the full text is below.*", ""]
        out += ["**Description:**", ""]
        out += ["> " + line if line.strip() else ">"
                for line in r["description"].replace("\r\n", "\n").split("\n")]
        out += ["", "label:", "", "note:", ""]

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out), encoding="utf-8", newline="\n")
    return path
