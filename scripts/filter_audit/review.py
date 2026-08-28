"""
Sampling rejects, disposing of them blinded, and recording what was learned.

TWO PHASES, AND THE SPLIT IS NOT ADMINISTRATIVE.

  phase 1  disposition     "Is this relevant work for us?"  ACCEPT/REJECT/UNCERTAIN
           BLINDED. This is the judgment that must be untainted, so the reviewer
           sees the notice and nothing the filter concluded about it.

  phase 2  categorization  failure category, evidence, explanation
           POST-REVEAL, on purpose. Naming WHY the filter erred is a claim about
           what the filter did; forcing it while blinded would produce guesses.
           Analysis should be informed. Judgment should not.

Because phase 1 is provably pre-reveal - the store refuses to reveal before a
disposition exists, and the disposition is immutable once written - blinded
agreement rate per stratum is a real measurement rather than a self-report.

REVIEWS LIVE IN THE VAULT, NOT IN data/filter_audit.db. They are human labour,
they are small, and they should turn up in a diff. The audit database is
regenerable scaffolding; a review is evidence. Append-only JSONL, one object per
line, corrections appended with `supersedes` rather than edited in place.

NOTHING HERE IS READ BY A DECISION. predicates.py does not import this module,
so an ACCEPT label physically cannot reach admission. UNCERTAIN is a review
state and only a review state.
"""
from __future__ import annotations

import json
import random
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml

from .blinding import RevealRefused, blind, reveal_payload
from .replay import AUDIT_DB, NOTICES_DB, connect

PROJECT_ROOT = Path(__file__).parent.parent.parent
REVIEWS_FILE = PROJECT_ROOT / "vault" / "reference" / "filter-reviews.jsonl"
TAXONOMY_FILE = PROJECT_ROOT / "vault" / "reference" / "filter-failure-taxonomy.yaml"

DECISIONS = ("ACCEPT", "REJECT", "UNCERTAIN")

QUEUE_SCHEMA = """
CREATE TABLE IF NOT EXISTS review_queue (
    item_id         TEXT PRIMARY KEY,
    queue_id        TEXT NOT NULL,
    run_id          TEXT NOT NULL,
    reference_number TEXT NOT NULL,
    -- Stored, and NEVER rendered before a disposition exists for the item.
    -- Knowing which stratum an item came from is knowing the answer.
    stratum         TEXT NOT NULL,
    position        INTEGER NOT NULL,
    sampling_strategy TEXT NOT NULL,
    sampling_seed   INTEGER,
    -- WHICH AXIS THIS ITEM WAS DRAWN TO COVER, for a stratified strategy; NULL
    -- for the ones that draw uniformly. Not withheld and not withholdable: it
    -- is the notice's own UNSPSC segment, and `next` already prints the notice's
    -- unspsc field, so the reviewer is looking at it either way. It is here so
    -- that AFTER review the answer can be "segments 43 and 81 produced
    -- disagreements, everything else was a clean reject" - which is the output
    -- the exercise is for, and which nothing in the record could express.
    drawn_for_segment TEXT,
    revealed_at     TEXT,
    force_unblinded INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS queue_by_queue ON review_queue(queue_id, position);
"""


def _queue_conn(audit_db: Path = AUDIT_DB) -> sqlite3.Connection:
    conn = connect(audit_db)
    conn.executescript(QUEUE_SCHEMA)
    # CREATE TABLE IF NOT EXISTS does nothing to a table that already exists, so
    # a database written before drawn_for_segment existed keeps the old shape.
    # Added here rather than by a migration script because there is one column
    # and the alternative is a queue that silently records nothing. Existing
    # rows keep NULL, which is the honest value: they were not drawn for a
    # segment.
    columns = {r[1] for r in conn.execute("PRAGMA table_info(review_queue)")}
    if "drawn_for_segment" not in columns:
        with conn:
            conn.execute("ALTER TABLE review_queue ADD COLUMN drawn_for_segment TEXT")
    return conn


# ---------------------------------------------------------------------------
# Taxonomy
# ---------------------------------------------------------------------------

def load_taxonomy() -> dict:
    if not TAXONOMY_FILE.exists():
        return {}
    loaded = yaml.safe_load(TAXONOMY_FILE.read_text(encoding="utf-8")) or {}
    return loaded.get("categories", {})


def taxonomy_keys() -> list:
    return sorted(load_taxonomy())


# ---------------------------------------------------------------------------
# Sampling
# ---------------------------------------------------------------------------

def _segments_of(audit_stage_results: str) -> set:
    """
    The UNSPSC segments a notice was coded into, from its stored decision.

    Two digits - the top of the UNSPSC hierarchy. Read out of the recorded
    audit rather than from a new column, so this works on runs that were
    persisted before this strategy existed and needs no re-replay. Parsing all
    21,471 rows of the branch costs ~0.4s, measured, which is cheaper than a
    schema migration and cannot fall out of step with the decision it describes.

    A code that is not eight digits is skipped rather than truncated: the field
    is publisher-supplied and a malformed value is not evidence of a segment.
    """
    for result in json.loads(audit_stage_results):
        if result.get("stage") != "relevance":
            continue
        codes = result.get("evidence", {}).get("codes") or []
        return {str(code)[:2] for code in codes
                if len(str(code)) == 8 and str(code).isdigit()}
    return set()


def _coded_wrong_family_population(conn, run_id, stage=None) -> tuple:
    """
    The branch, bucketed by segment. Returns (buckets, rows, report).

    `buckets` maps segment -> notice ids, and a notice carrying codes in two
    segments appears in both - it is genuinely evidence about both, and the draw
    below takes it once.
    """
    where = ["run_id = ?", "production_admitted = 0",
             "has_codes = 1", "family_result = 'wrong_family'"]
    params = [run_id]
    if stage:
        where.append("first_rejecting_stage = ?")
        params.append(stage)
    # ORDER BY so the pre-shuffle order is fixed: a seeded shuffle over an
    # unordered scan is only reproducible by accident.
    sql = ("SELECT notice_id, production_admitted, first_rejecting_stage, "
           "has_codes, family_result, keyword_result, audit_stage_results "
           "FROM decisions WHERE " + " AND ".join(where) + " ORDER BY notice_id")

    buckets: dict = {}
    rows: dict = {}
    without_segment = 0
    for row in conn.execute(sql, params):
        segments = _segments_of(row["audit_stage_results"])
        if not segments:
            # Cannot happen while has_codes is derived from the same code list,
            # so it is counted rather than ignored: a nonzero figure here means
            # the two have come apart, and the sampling frame would be quietly
            # smaller than the branch.
            without_segment += 1
            continue
        rows[row["notice_id"]] = {
            key: row[key] for key in
            ("notice_id", "production_admitted", "first_rejecting_stage",
             "has_codes", "family_result", "keyword_result")}
        for segment in segments:
            buckets.setdefault(segment, []).append(row["notice_id"])

    rejects = conn.execute(
        "SELECT COUNT(*) FROM decisions WHERE run_id=? AND production_admitted=0",
        (run_id,)).fetchone()[0]
    report = {
        "branch": "coded_wrong_family",
        "rows": len(rows),
        "rejects_in_run": rejects,
        "share_of_rejects": round(100 * len(rows) / rejects, 1) if rejects else None,
        "segments": len(buckets),
        # Largest first: the shape of the branch is the point, and the reader
        # needs to see how lopsided it is before reading any draw from it.
        "segment_sizes": {segment: len(ids) for segment, ids in
                          sorted(buckets.items(),
                                 key=lambda kv: (-len(kv[1]), kv[0]))},
        "rows_without_segment": without_segment,
        "stage_filter": stage,
    }
    return buckets, rows, report


def _strategy_coded_wrong_family(conn, run_id, n, seed, stage, branch,
                                 include_admitted):
    """
    Draw across the coded_wrong_family branch, spread over UNSPSC segments.

    WHY THIS IS NOT `--strategy random --branch coded_wrong_family`. That draws
    the same population but records `random` on the queue row and persists
    nothing about the branch, so the review log cannot say what was sampled. And
    it draws uniformly, which on this branch means proportionally: segment 72
    holds 3,934 of 21,471 rows, so a queue of 25 spends four items there and
    never sees most of the tail.

    WHY THE BRANCH AT ALL. It is 78% of all rejects and no keyword refinement
    can reach any of it - stage 5's coded and uncoded paths are mutually
    exclusive, so a notice carrying codes is judged on its codes and the
    competency list is never consulted. Uniform sampling over all rejects keeps
    surfacing vocabulary bugs, which live in the OTHER branch, and never tests
    whether the family list is where the real blindness is.

    The draw is round-robin over segments in a seeded order, taking one notice
    per segment per pass. So `n` items reach `min(n, segments)` distinct
    segments, and the result reads as a shortlist of segments to look at rather
    than as a rate.
    """
    if branch:
        raise KeyError(
            f"--branch {branch!r} contradicts --strategy coded_wrong_family, "
            f"which IS a branch. Drop the flag, or use --strategy random "
            f"--branch {branch} to sample that branch uniformly.")
    if include_admitted:
        raise KeyError(
            "--include-admitted cannot be honoured here: no admitted notice is "
            "in this branch, by construction - a wrong-family verdict IS the "
            "rejection. Mixing in admitted notices would mean drawing them from "
            "a different population and calling the result one queue. The "
            "natural control arm is the coded_pass branch, and building that is "
            "a separate decision.")

    buckets, rows, report = _coded_wrong_family_population(conn, run_id, stage)
    if not buckets:
        return [], report

    rng = random.Random(seed)
    order = sorted(buckets)
    rng.shuffle(order)
    for segment in order:
        rng.shuffle(buckets[segment])

    drawn, seen = [], set()
    while len(drawn) < n:
        progressed = False
        for segment in order:
            if len(drawn) >= n:
                break
            bucket = buckets[segment]
            while bucket:
                notice_id = bucket.pop()
                if notice_id in seen:
                    continue        # already drawn for one of its other segments
                seen.add(notice_id)
                drawn.append((rows[notice_id], segment))
                progressed = True
                break
        if not progressed:
            break                   # the branch is exhausted before n

    report["drawn"] = len(drawn)
    report["segments_drawn"] = len({segment for _, segment in drawn})
    report["segments_unsampled"] = report["segments"] - report["segments_drawn"]
    return drawn, report


def _strategy_random(conn, run_id, n, seed, stage, branch, include_admitted):
    where = ["run_id = ?"]
    params = [run_id]
    if not include_admitted:
        where.append("production_admitted = 0")
    if stage:
        where.append("first_rejecting_stage = ?")
        params.append(stage)
    if branch:
        # The relevance branch, expressed as the two fields that carry it.
        mapping = {
            "coded_wrong_family": ("has_codes = 1", "family_result = 'wrong_family'"),
            "coded_pass": ("has_codes = 1", "family_result = 'matched'"),
            "uncoded_no_keyword": ("has_codes = 0", "keyword_result = 'no_hit'"),
            "uncoded_pass": ("has_codes = 0", "keyword_result = 'matched'"),
        }
        if branch not in mapping:
            raise KeyError(f"Unknown branch {branch!r}. "
                           f"Known: {', '.join(sorted(mapping))}")
        where.extend(mapping[branch])
    sql = (f"SELECT notice_id, production_admitted, first_rejecting_stage, "
           f"has_codes, family_result, keyword_result FROM decisions "
           f"WHERE {' AND '.join(where)}")
    rows = conn.execute(sql, params).fetchall()
    rng = random.Random(seed)
    rng.shuffle(rows)
    # (row, drawn_for_segment) like every strategy, and None because this one
    # draws uniformly: there is no axis it was spread across, and writing one in
    # would claim a coverage the draw does not have.
    return [(row, None) for row in rows[:n]], None


# Strategies the interface is designed for. `random` is built; the rest parse
# and refuse, naming what each would need. A stub that pretends is worse than a
# stub that says what is missing.
_UNBUILT = {
    "recent": "needs a recency ordering the run does not yet record beyond publication_date",
    "high-value": ("needs a value field. The feed publishes none and the regex that "
                   "invented one has been retired - this strategy may never be buildable, "
                   "and that is a finding rather than a TODO"),
    "unusual": "needs an outlier metric over the decision record; none is defined",
    "near-miss": "needs a distance metric over relevance; none is defined",
    "buyer": "needs org_keys on the decision record; currently only on the notice",
    "predicate": "use --stage / --branch on the random strategy instead",
}

# The built ones. A registry rather than a chain of `if strategy == ...`, so
# adding one is adding a function and cannot forget a call site.
_STRATEGIES = {
    "random": _strategy_random,
    "coded_wrong_family": _strategy_coded_wrong_family,
}


def sample_rejects(run_id: Optional[str] = None,
                   strategy: str = "random",
                   n: int = 20,
                   seed: Optional[int] = None,
                   stage: Optional[str] = None,
                   branch: Optional[str] = None,
                   include_admitted: bool = False,
                   audit_db: Path = AUDIT_DB) -> dict:
    """
    Build a shuffled review queue and return its id, its size, and - for a
    stratified strategy - the shape of the population it drew from.

    NOTHING ABOUT THE QUEUE'S OWN COMPOSITION, which is the rule that matters:
    per-item strata stay withheld until a disposition exists, because knowing
    which stratum an item came from is knowing the answer.

    `branch_population` does not break that and is not an exception to it. It
    describes the BRANCH, which the caller named by choosing the strategy, and
    it is reported BEFORE the draw rather than after the review, because the
    thing a reader needs to know in advance is how much of the population a
    queue of this size can reach. Learning after the fact that 25 items covered
    a quarter of the segments would make the result look stronger than it is at
    exactly the moment it is being read.

    A single-stratum queue does still tell the reviewer the verdict was REJECT.
    That gap is old, it is why `--include-admitted` exists on the uniform
    strategy, and this strategy cannot close it - see its docstring.
    """
    if strategy in _UNBUILT:
        return {"error": f"Strategy {strategy!r} is not implemented: "
                         f"{_UNBUILT[strategy]}"}
    if strategy not in _STRATEGIES:
        return {"error": f"Unknown strategy {strategy!r}. "
                         f"Built: {', '.join(sorted(_STRATEGIES))}. "
                         f"Designed for: {', '.join(sorted(_UNBUILT))}"}

    conn = _queue_conn(audit_db)
    if run_id is None:
        row = conn.execute(
            "SELECT run_id FROM runs ORDER BY first_written_at DESC LIMIT 1"
        ).fetchone()
        if not row:
            conn.close()
            return {"error": "No runs recorded. Run `replay-filter --persist` first."}
        run_id = row["run_id"]

    if seed is None:
        seed = random.randrange(1, 2**31)

    try:
        rows, population = _STRATEGIES[strategy](
            conn, run_id, n, seed, stage, branch, include_admitted)
    except KeyError as exc:
        conn.close()
        return {"error": str(exc)}

    if not rows:
        conn.close()
        return {"error": "No matching decisions. Check the run id, stage or branch."}

    queue_id = f"q-{datetime.now().strftime('%Y%m%d')}-{seed % 100000:05d}"
    records = []
    for position, (row, segment) in enumerate(rows):
        item_id = f"{queue_id}-{position:03d}"
        stratum = _stratum_of(row)
        records.append((item_id, queue_id, run_id, row["notice_id"], stratum,
                        position, strategy, seed, segment, None, 0))
    with conn:
        # Named columns, not positional: the table has grown a column once and
        # a positional insert is how the next one lands in the wrong field.
        conn.executemany(
            "INSERT OR IGNORE INTO review_queue "
            "(item_id, queue_id, run_id, reference_number, stratum, position, "
            " sampling_strategy, sampling_seed, drawn_for_segment, revealed_at, "
            " force_unblinded) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            records)
    conn.close()
    # Queue id and size. Nothing about the queue's own composition; see the
    # docstring for why the branch's shape is a different question.
    out = {"queue_id": queue_id, "n": len(records)}
    if population is not None:
        out["branch_population"] = population
    return out


def _stratum_of(row) -> str:
    """The label a reviewer must not see until they have disposed of the item."""
    if row["production_admitted"]:
        return "admitted"
    stage = row["first_rejecting_stage"] or "unknown"
    if stage != "relevance":
        return f"reject:{stage}"
    if row["has_codes"]:
        return ("reject:relevance:coded_wrong_family"
                if row["family_result"] != "matched" else "reject:relevance:coded")
    return ("reject:relevance:uncoded_no_keyword"
            if row["keyword_result"] != "matched" else "reject:relevance:uncoded")


# ---------------------------------------------------------------------------
# The blinded surface
# ---------------------------------------------------------------------------

def next_item(queue_id: str, audit_db: Path = AUDIT_DB,
              notices_db: Path = NOTICES_DB) -> dict:
    """
    The next undisposed item in a queue, blinded.

    Returns a BlindedNotice payload. There is no code path from here to the
    decision record - see blinding.py.
    """
    conn = _queue_conn(audit_db)
    disposed = _disposed_item_ids()
    rows = conn.execute(
        "SELECT * FROM review_queue WHERE queue_id=? ORDER BY position",
        (queue_id,)).fetchall()
    conn.close()
    if not rows:
        return {"error": f"No queue {queue_id!r}."}
    pending = [r for r in rows if r["item_id"] not in disposed]
    if not pending:
        return {"queue_id": queue_id, "remaining": 0,
                "message": "Every item in this queue has a disposition. "
                           "Use `reveal` to compare, or `categorize` to attach a "
                           "failure category to the ones you disagreed with."}
    item = pending[0]
    notice = _notice_row(item["reference_number"], notices_db)
    if notice is None:
        return {"error": f"{item['reference_number']} is not in {notices_db.name}."}
    payload = blind(item["item_id"], notice).to_dict()
    payload["remaining"] = len(pending)
    payload["blinded"] = (
        "The rejection reason, the failing gate and the stratum are withheld "
        "until you record a decision. Judge the notice, not the filter.")
    return payload


def _notice_row(reference_number: str, notices_db: Path = NOTICES_DB):
    if not notices_db.exists():
        return None
    conn = sqlite3.connect(f"file:{notices_db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM notices WHERE reference_number=?",
                       (reference_number,)).fetchone()
    conn.close()
    return row


# ---------------------------------------------------------------------------
# Reviews - append-only
# ---------------------------------------------------------------------------

def load_reviews() -> list:
    if not REVIEWS_FILE.exists():
        return []
    out = []
    for line in REVIEWS_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def _append_review(record: dict) -> None:
    REVIEWS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with REVIEWS_FILE.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def _disposed_item_ids() -> set:
    return {r["item_id"] for r in load_reviews() if r.get("reviewed_decision")}


def dispositions_by_item() -> dict:
    """
    Latest disposition per item, folded along the `supersedes` chain.

    The FIRST disposition is the blinded one and is what agreement statistics
    count; later ones are corrections and are marked post_reveal.
    """
    out: dict = {}
    for record in load_reviews():
        if record.get("reviewed_decision"):
            out.setdefault(record["item_id"], []).append(record)
    return {k: sorted(v, key=lambda r: r["disposition_at"]) for k, v in out.items()}


def record_review(item_id: str, decision: str, reviewer: str,
                  audit_db: Path = AUDIT_DB) -> dict:
    """
    Phase 1. Write the blinded disposition, then reveal in the same breath.

    The reveal comes back with the record because seeing whether you agreed is
    the useful part - but it comes back AFTER the disposition is durable, which
    is what makes the blinding meaningful rather than ceremonial.

    `reviewer` HAS NO DEFAULT, deliberately. It used to default to "human", and
    the first three records this package ever wrote were produced by an
    assistant and claimed to be human because nobody passed the argument. In a
    log whose whole purpose is auditable provenance, the wrong answer must not
    be the one you get by saying nothing.
    """
    decision = decision.upper()
    if decision not in DECISIONS:
        return {"error": f"decision must be one of {', '.join(DECISIONS)}"}

    conn = _queue_conn(audit_db)
    item = conn.execute("SELECT * FROM review_queue WHERE item_id=?",
                        (item_id,)).fetchone()
    if not item:
        conn.close()
        return {"error": f"No queue item {item_id!r}."}
    decision_row = conn.execute(
        "SELECT * FROM decisions WHERE run_id=? AND notice_id=?",
        (item["run_id"], item["reference_number"])).fetchone()
    if not decision_row:
        conn.close()
        return {"error": f"No decision recorded for {item['reference_number']} "
                         f"in run {item['run_id']}."}

    existing = dispositions_by_item().get(item_id, [])
    post_reveal = bool(existing)

    run = conn.execute("SELECT * FROM runs WHERE run_id=?",
                       (item["run_id"],)).fetchone()

    # Numbered over DISPOSITIONS, not over lines. Counting every line made a
    # categorization consume an id, so the log ran rv-001 then rv-003 - and an
    # append-only audit log with a gap in it reads as a deleted record, which is
    # the one thing this file must never appear to have done.
    sequence = sum(1 for r in load_reviews() if r.get("failure_category") is None
                   and r.get("reviewed_decision")) + 1
    record = {
        "review_id": f"rv-{datetime.now().strftime('%Y%m%d')}-{sequence:03d}",
        "item_id": item_id,
        "reference_number": item["reference_number"],
        "run_id": item["run_id"],
        "filter_version_label": run["filter_version_label"] if run else None,
        "sampling_strategy": item["sampling_strategy"],
        "sampling_seed": item["sampling_seed"],
        # The axis this item was drawn to cover, or None for a uniform draw.
        # In the log rather than only in the queue database, because the log is
        # the evidence that survives - filter_audit.db is regenerable
        # scaffolding, and "which segments produced disagreements" has to be
        # answerable from the committed reviews alone.
        "drawn_for_segment": item["drawn_for_segment"],
        # Read from the run. NEVER typed by the reviewer - a reviewer who could
        # state what production decided could state it wrong.
        "original_decision": "ACCEPT" if decision_row["production_admitted"] else "REJECT",
        "original_first_rejecting_stage": decision_row["first_rejecting_stage"],
        "stratum": item["stratum"],
        "reviewed_decision": decision,
        "disposition_at": datetime.now().isoformat(timespec="seconds"),
        "reviewer": reviewer,
        "post_reveal": int(post_reveal),
        "supersedes": existing[-1]["review_id"] if existing else None,
        "failure_category": None,
        "evidence": None,
        "explanation": None,
        "blinded_at_disposition": not post_reveal,
    }
    _append_review(record)

    with conn:
        conn.execute("UPDATE review_queue SET revealed_at=? WHERE item_id=?",
                     (record["disposition_at"], item_id))
    payload = reveal_payload(decision_row, item, record)
    conn.close()
    payload["review_id"] = record["review_id"]
    payload["post_reveal_correction"] = post_reveal
    return payload


def reveal(item_id: str, force_unblind: bool = False,
           audit_db: Path = AUDIT_DB) -> dict:
    """
    The verdict for one item. Refused unless a disposition exists.

    `force_unblind` exists for debugging and RECORDS that it was used, marking
    the queue compromised so its items are excluded from agreement statistics.
    An escape hatch that leaves no trace is not an escape hatch, it is a hole.
    """
    conn = _queue_conn(audit_db)
    item = conn.execute("SELECT * FROM review_queue WHERE item_id=?",
                        (item_id,)).fetchone()
    if not item:
        conn.close()
        return {"error": f"No queue item {item_id!r}."}
    decision_row = conn.execute(
        "SELECT * FROM decisions WHERE run_id=? AND notice_id=?",
        (item["run_id"], item["reference_number"])).fetchone()

    existing = dispositions_by_item().get(item_id, [])
    if not existing and force_unblind:
        with conn:
            conn.execute("UPDATE review_queue SET force_unblinded=1 WHERE queue_id=?",
                         (item["queue_id"],))
        item = conn.execute("SELECT * FROM review_queue WHERE item_id=?",
                            (item_id,)).fetchone()
        payload = reveal_payload(decision_row, item,
                                 {"reviewed_decision": None})
        conn.close()
        payload["force_unblinded"] = True
        payload["warning"] = (
            "Queue marked COMPROMISED. Its items are excluded from blinded "
            "agreement statistics, because a disposition recorded after seeing "
            "this is not a blinded judgment.")
        return payload
    conn.close()
    try:
        return reveal_payload(decision_row, item, existing[-1] if existing else None)
    except RevealRefused as exc:
        return {"error": str(exc)}


def categorize(item_id: str, category: str, evidence: str,
               explanation: str) -> dict:
    """
    Phase 2. Attach the failure category, after the reveal.

    Refuses a category outside the taxonomy - the vocabulary is closed so the
    counts mean something. Extend it with `taxonomy --add`, deliberately.
    """
    known = taxonomy_keys()
    if category not in known:
        return {"error": f"Unknown failure category {category!r}. "
                         f"Known: {', '.join(known)}. "
                         f"Add one with `taxonomy --add` if it is genuinely new."}
    dispositions = dispositions_by_item().get(item_id)
    if not dispositions:
        return {"error": f"No disposition for {item_id!r}. Record a decision "
                         f"before categorizing - the category describes why the "
                         f"filter erred, which presumes you know what it did."}
    latest = dispositions[-1]
    record = dict(latest)
    record.update({
        "review_id": f"{latest['review_id']}-cat",
        "failure_category": category,
        "evidence": evidence,
        "explanation": explanation,
        "categorized_at": datetime.now().isoformat(timespec="seconds"),
        "supersedes": latest["review_id"],
        # A categorization is not a second disposition. It inherits the blinded
        # judgment rather than replacing it.
        "post_reveal": 1,
        "blinded_at_disposition": latest.get("blinded_at_disposition", False),
    })
    _append_review(record)
    return {"review_id": record["review_id"], "item_id": item_id,
            "reference_number": latest["reference_number"],
            "reviewed_decision": latest["reviewed_decision"],
            "failure_category": category,
            "note": "Categorization recorded. The blinded disposition it "
                    "inherits is unchanged."}


def agreement(audit_db: Path = AUDIT_DB) -> dict:
    """
    How often the blinded reviewer agreed with the filter, by stratum.

    Only pre-reveal dispositions on uncompromised queues count. That restriction
    is the entire reason the number is worth reporting.

    BROKEN OUT BY REVIEWER, and never summed across them. A human judgement and
    an assistant's are not the same measurement, and a single blended rate would
    hide which one it mostly describes. The top-level `by_stratum` is kept for
    one reviewer at a time; `by_reviewer` is what tells you whether that is what
    you are looking at.
    """
    conn = _queue_conn(audit_db)
    compromised = {r["queue_id"] for r in conn.execute(
        "SELECT DISTINCT queue_id FROM review_queue WHERE force_unblinded=1")}
    queues = {r["item_id"]: r["queue_id"] for r in conn.execute(
        "SELECT item_id, queue_id FROM review_queue")}
    conn.close()

    by_stratum: dict = {}
    by_reviewer: dict = {}
    counted = skipped = 0
    for item_id, records in dispositions_by_item().items():
        first = records[0]
        if not first.get("blinded_at_disposition"):
            skipped += 1
            continue
        if queues.get(item_id) in compromised:
            skipped += 1
            continue
        counted += 1
        agreed = first["reviewed_decision"] == first["original_decision"]
        bucket = by_stratum.setdefault(
            first.get("stratum", "unknown"), {"n": 0, "agreed": 0})
        bucket["n"] += 1
        bucket["agreed"] += int(agreed)

        who = by_reviewer.setdefault(
            first.get("reviewer", "unstated"), {"n": 0, "agreed": 0, "strata": {}})
        who["n"] += 1
        who["agreed"] += int(agreed)
        stratum = who["strata"].setdefault(
            first.get("stratum", "unknown"), {"n": 0, "agreed": 0})
        stratum["n"] += 1
        stratum["agreed"] += int(agreed)

    return {"counted": counted, "excluded": skipped,
            "excluded_because": "post-reveal correction, or a force-unblinded queue",
            "by_reviewer": by_reviewer,
            "by_stratum": by_stratum,
            "note": ("Rates are per reviewer and are not summed across them. An "
                     "assistant's judgement and a human's are different "
                     "measurements; read `by_reviewer` before `by_stratum`.")}
