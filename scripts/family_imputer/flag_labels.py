"""
Labelling ref-007's coded-notice flags - blind, before any flag exists to label.

THE VOCABULARY IS CODE, NOT PROSE. The four kinds are FLAG_LABEL_DEFINITIONS,
applied in that order; tests/test_family_imputer.py asserts they are exactly
the kinds ref-007 defines. The label path refuses anything else. A notice that
fits none is left UNASSIGNED - a blank `label:` with a reason on the `note:`
line - which ref-007 says is a count with a reason, never a fifth kind.

BLIND, AND THE BLINDING IS filter_audit.blinding's, NOT A COPY. Every block on
the sheet is rendered from blinding.blind(), which returns a BlindedNotice -
a type with no field for Jev's choice, the band, or whether the notice was
flagged - and each block's payload goes through blinding.assert_blinded(),
which refuses the ref-007 fields listed in WITHHELD_UNTIL_DISPOSED. Choice and
band are revealed per item only once a disposition exists for it: reveal()
raises blinding.RevealRefused otherwise.

MIXED 1:1 WITH UNFLAGGED CODED REJECTS, because a queue of flags alone tells
the reader Jev's verdict before they read a word. Controls are coded notices
from the current feed that pass gates 1-4, are rejected on their codes, and
are not in the flag store. Membership is in the queue file (QUEUE_JSON), not on
the sheet. That withholds it from the sheet; it does not hide it from anyone
who opens the queue file, and it cannot stop a reader inferring "this looks
like IT, so probably a flag" - blinding.py's own caveat about expertise.

LIMITS, stated. A control is "not in the store", which is what CI's flagger
not flagging it looks like - but it is also what a notice CI never reached
(time budget, API error) looks like. And controls come from the feed on this
machine's disk, so they are from the day it was fetched, while flags accumulate
across months.

WRITES: the sheet, the queue and the revealed sheet under data/family_imputer/
(working files, not committed), and dispositions to data/coded_flag_labels.jsonl
(ingest.paths.CODED_FLAG_LABELS) - append-only, first disposition immutable.
Never the flag store, the corpus, the golden set or the profile.

THE DISPOSITIONS ARE COMMITTED, BESIDE THE FLAG STORE, unlike REF-004's labels.
Those were a one-off analysis; these are the standing evidence a promotion
decision rests on, and a durable store interpreted by a disposable file is the
wrong way round. So each record also carries its ROLE (flag or control), taken
from the queue when the disposition is written: the labels file stands alone
and does not need the uncommitted flag-queue.json to be read.
"""
from __future__ import annotations

import hashlib
import json
import random
import re
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import ingest
from filter_audit import blinding
from filter_audit import predicates as P
from ingest import flag_store
from ingest import paths as ingest_paths

from . import cache
from .evaluate import LABELLERS, NOTICES_DB, PROJECT_ROOT

FLAG_LABEL_DEFINITIONS = {
    "vehicle": (
        "The notice qualifies suppliers onto, refreshes, or issues a call-up under a "
        "supply arrangement or standing offer for IT professional services (TBIPS, "
        "SBIPS, ProServices streams, PASS IT streams and the like), however it is "
        "coded. Apply this first."),
    "jev_wrong": (
        "The principal purchase is not IT work that a profile family (8111, 8116, "
        "4323, 80101507) describes. Named for a flag, where it means the model put a "
        "non-IT notice in a profile family; on an unflagged notice it is the expected "
        "answer. Judge the notice, not the name."),
    "miscoded": (
        "The principal purchase is IT work that a profile family describes, and the "
        "filed codes do not describe it."),
    "out_of_scope": (
        "The filed codes describe the purchase acceptably, and the work is IT-adjacent "
        "but outside the profile's families: IT audit, IT roles filed as temporary "
        "personnel (staff augmentation), vendor training, and the like."),
}
FLAG_LABEL_KINDS = tuple(FLAG_LABEL_DEFINITIONS)
UNASSIGNED = "unassigned"   # not a kind: a blank label with a stated reason (ref-007)

SEED = 20260925
SHEET_MD = cache.DATA_DIR / "flag-sheet.md"
QUEUE_JSON = cache.DATA_DIR / "flag-queue.json"
REVEALED_MD = cache.DATA_DIR / "flag-revealed.md"
WITHHELD_TEXT = "withheld until this notice is labelled"


class FlagSheetError(ValueError):
    """The sheet or queue did not validate. Nothing was written."""


# ---------------------------------------------------------------------------
# Where a notice's text comes from
# ---------------------------------------------------------------------------

def _row_from_notice(n: P.Notice) -> dict:
    """NaN-safe through predicates._s, so blind() never renders 'nan'."""
    s = P._s
    return {"reference_number": n.notice_id, "title": s(n.title),
            "description": s(n.description), "contracting_entity": s(n.contracting_entity),
            "end_user": s(n.end_user), "notice_type": s(n.notice_type),
            "procurement_category": s(n.procurement_category), "unspsc": s(n.unspsc),
            "gsin": s(n.gsin), "publication_date": s(n.publication_date),
            "closing_date": s(n.raw_closing_date)}


def find_rows(ids, feed_paths=None, notices_db: Path = NOTICES_DB) -> dict:
    """
    Raw notice rows for `ids`: the current feed, then the snapshots newest
    first, then notices.db (latest amendment). A flag can outlive its notice's
    stay in the open feed by months, and a notice read without its description
    cannot be labelled - REF-004 lost four of thirty that way.
    """
    from filter_audit import replay
    want, out = set(ids), {}
    if feed_paths is None:
        feed_paths = [replay.FEED_CSV] + sorted(replay.SNAPSHOT_DIR.glob("*.csv*"),
                                                reverse=True)
    for path in feed_paths:
        if not want - set(out) or not Path(path).exists():
            continue
        for n, _ in replay.iter_feed(path):
            if n.notice_id in want and n.notice_id not in out:
                out[n.notice_id] = _row_from_notice(n)
    missing = want - set(out)
    if missing and notices_db.exists():
        conn = sqlite3.connect(f"file:{notices_db.as_posix()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        for nid in sorted(missing):
            row = conn.execute("SELECT * FROM notices WHERE reference_number=? "
                               "ORDER BY amendment_number DESC LIMIT 1", (nid,)).fetchone()
            if row is not None:
                out[nid] = dict(row)
        conn.close()
    return out


def control_pool(flagged_ids, criteria=None, feed_path=None) -> list:
    """Coded notices in the feed that pass gates 1-4, are rejected on their
    codes, and are not flagged - as of the feed's own download date."""
    from datetime import date
    from filter_audit import replay
    criteria = criteria or ingest.parse_profile(ingest.DEFAULT_PROFILE)
    feed_path = Path(feed_path or replay.FEED_CSV)
    meta = feed_path.parent / (feed_path.name + ".http.json")
    as_of = (date.fromisoformat(json.loads(meta.read_text())["fetched_at"][:10])
             if meta.exists() else None)
    stage = {s.name: s for s in P.STAGES}
    flagged, pool = set(flagged_ids), []
    for n, _ in replay.iter_feed(feed_path):
        codes = ingest.parse_unspsc_codes(n.unspsc)
        if not codes or n.notice_id in flagged:
            continue
        if ingest.matches_unspsc_families(codes, criteria["unspsc_families"]):
            continue
        if any(stage[s].evaluate(n, criteria, as_of).drops
               for s in ("closed", "exclusion", "construction", "jurisdiction")):
            continue
        pool.append(n.notice_id)
    return sorted(set(pool))


# ---------------------------------------------------------------------------
# Dispositions
# ---------------------------------------------------------------------------

def load_labels(path: Path | None = None) -> list:
    path = path or ingest_paths.CODED_FLAG_LABELS
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def disposed_ids(labels: list, labelled_by: str | None = None) -> set:
    return {r["notice_id"] for r in labels
            if labelled_by is None or r["labelled_by"] == labelled_by}


# ---------------------------------------------------------------------------
# The queue and the sheet
# ---------------------------------------------------------------------------

def build_queue(flags: list, pool: list, *, limit: int | None, seed: int,
                labels: list) -> dict:
    """
    Pending flags (no disposition yet), oldest first, up to `limit`, plus the
    same number of controls drawn from `pool`, shuffled together. Refuses when
    the pool cannot match the flags 1:1 - a lopsided queue leaks the verdict.
    """
    done = disposed_ids(labels)
    pending = [f for f in sorted(flags, key=lambda f: (f["first_seen"], f["notice_id"]))
               if f["notice_id"] not in done]
    if limit:
        pending = pending[:limit]
    if not pending:
        raise FlagSheetError("no flags without a disposition - nothing to put on a sheet")
    rng = random.Random(seed)
    pool = [p for p in pool if p not in done]
    if len(pool) < len(pending):
        raise FlagSheetError(
            f"{len(pending)} flags but only {len(pool)} unflagged coded rejects to mix "
            f"with them 1:1; refusing a lopsided queue. Pass --limit {len(pool)} or "
            f"refresh the feed.")
    controls = rng.sample(pool, len(pending))
    items = ([{"notice_id": f["notice_id"], "role": "flag"} for f in pending]
             + [{"notice_id": c, "role": "control"} for c in controls])
    rng.shuffle(items)
    return {"seed": seed, "created_at": datetime.now(timezone.utc).isoformat(
                timespec="seconds"),
            "items": items}


def _codes(unspsc) -> list:
    return sorted(ingest.parse_unspsc_codes(unspsc))


def render_sheet(queue: dict, rows: dict, ref: dict | None) -> str:
    """The blind reading sheet. Every block is built from a BlindedNotice."""
    from .options import describe_code
    items = queue["items"]
    out = ["# Coded-notice flags - blind reading sheet (ref-007)", "",
           f"{len(items)} notices, seed {queue['seed']}. Half were flagged by the model "
           f"and half were not; which is which is withheld, as are the model's choice "
           f"and band, until you have labelled a notice.", "",
           "## Label kinds, in order - take the first that fits", ""]
    for kind, definition in FLAG_LABEL_DEFINITIONS.items():
        out.append(f"- **`{kind}`**: {definition}")
    out += ["", "**None fits:** leave `label:` blank and say why on `note:`. It is "
            "recorded as unassigned, which is a count with a reason and not a kind. "
            "A block with both lines blank is skipped as not yet read.", "",
            "Record the sheet with:", "", "```",
            "python scripts/family_imputer flag-labels "
            f"{SHEET_MD.relative_to(PROJECT_ROOT).as_posix()} --labelled-by human",
            "```", ""]
    for i, item in enumerate(items, 1):
        nid = item["notice_id"]
        row = rows.get(nid) or {"reference_number": nid}
        seen = blinding.blind(nid, row)
        payload = seen.to_dict()
        blinding.assert_blinded(payload)
        codes = _codes(payload["unspsc"])
        described = [f"- `{c}` {describe_code(c, ref)}" if ref else f"- `{c}`"
                     for c in codes]
        out += ["---", "", f"## {nid}", "", f"**{i:02} of {len(items)}**", "",
                f"**Title:** {' '.join(payload['title'].split()) or '(not found locally)'}",
                "",
                f"**Buyer:** {payload['contracting_entity'] or '-'}"
                + (f"; end user {payload['end_user']}" if payload["end_user"] else ""),
                "",
                f"**Notice type:** {payload['notice_type'] or '-'}; closing "
                f"{payload['closing_date'] or '-'}", "",
                "**Filed codes:**", ""] + (described or ["- (none found locally)"])
        out += ["", f"**Model's choice:** {WITHHELD_TEXT}", "",
                f"**Band:** {WITHHELD_TEXT}", "", "**Description:**", ""]
        text = payload["description"].replace("\r\n", "\n")
        out += (["> " + ln if ln.strip() else ">" for ln in text.split("\n")]
                if text.strip() else
                ["*No description found locally (feed, snapshots, notices.db). "
                 "Run scripts/notices_ingest.py for the current fiscal year, or "
                 "leave this unassigned with that reason.*"])
        out += ["", "label:", "", "note:", ""]
    if not ref:
        out.insert(4, "*Code descriptions unavailable: the PSPC reference file is not "
                      "on this machine (scripts/unspsc_discover.py).*\n")
    return "\n".join(out)


def write_sheet(*, limit: int | None = None, seed: int = SEED, replace: bool = False,
                store_path: Path | None = None, feed_path=None, feed_paths=None,
                sheet_path: Path = SHEET_MD, queue_path: Path = QUEUE_JSON,
                labels_path: Path | None = None, ref=None) -> dict:
    """Build the queue, render the sheet, and record the queue beside it.
    Refuses to replace a queue that still has undisposed items unless asked."""
    labels = load_labels(labels_path)
    if queue_path.exists() and not replace:
        old = json.loads(queue_path.read_text(encoding="utf-8"))
        open_items = [it["notice_id"] for it in old["items"]
                      if it["notice_id"] not in disposed_ids(labels)]
        if open_items:
            raise FlagSheetError(
                f"the current sheet has {len(open_items)} notices with no disposition; "
                f"ingest it with flag-labels first, or pass --replace to discard it")
    flags = flag_store.load(store_path)
    if not flags:
        raise FlagSheetError("the flag store is empty - nothing has been flagged yet "
                             "(CI writes data/coded_flags.jsonl; git pull after a run)")
    pool = control_pool([f["notice_id"] for f in flags], feed_path=feed_path)
    queue = build_queue(flags, pool, limit=limit, seed=seed, labels=labels)
    rows = find_rows([it["notice_id"] for it in queue["items"]], feed_paths=feed_paths)
    if ref is None:
        try:
            from .options import load_reference
            ref = load_reference()
        except FileNotFoundError:
            ref = {}
    text = render_sheet(queue, rows, ref)
    sheet_path.parent.mkdir(parents=True, exist_ok=True)
    sheet_path.write_text(text, encoding="utf-8", newline="\n")
    queue["sheet_sha256"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
    queue["store_notice_count"] = len(flags)
    queue["missing_text"] = sorted(it["notice_id"] for it in queue["items"]
                                   if not (rows.get(it["notice_id"]) or {}).get("description"))
    queue_path.write_text(json.dumps(queue, indent=1), encoding="utf-8", newline="\n")
    return queue


# ---------------------------------------------------------------------------
# Reading the labels back
# ---------------------------------------------------------------------------

_BLOCK_MARK = re.compile(r"^\*\*\d{2} of \d+\*\*")
_LABEL_LINE = re.compile(r"^label:[ \t]*(.*?)\s*$")
_NOTE_LINE = re.compile(r"^note:[ \t]*(.*?)\s*$")


def parse_sheet(text: str, queue_ids: list) -> tuple[list, list, list]:
    """
    (parsed, skipped, errors). A block is a `## <id>` heading followed by a
    `**NN of M**` line. Every problem is returned; the caller writes nothing
    unless there are none. An id not in the queue is an error, not a skip.
    """
    lines = text.replace("\r\n", "\n").split("\n")
    blocks, current = [], None
    for i, line in enumerate(lines):
        if line.startswith("#"):
            nxt = next((l for l in lines[i + 1:i + 4] if l.strip()), "")
            current = None
            if line.startswith("## ") and _BLOCK_MARK.match(nxt):
                current = {"id": line[3:].strip(), "lines": []}
                blocks.append(current)
            continue
        if current is not None:
            current["lines"].append(line)

    errors, parsed, skipped = [], [], []
    seen = Counter(b["id"] for b in blocks)
    errors += [f"{nid}: appears {n} times" for nid, n in seen.items() if n > 1]
    errors += [f"{nid}: not on the current sheet (flag-queue.json); refusing it"
               for nid in sorted(set(seen) - set(queue_ids))]
    errors += [f"{nid}: on the sheet but has no block" for nid in sorted(set(queue_ids) - set(seen))]
    for b in blocks:
        labels = [m.group(1) for l in b["lines"] if (m := _LABEL_LINE.match(l))]
        notes = [m.group(1) for l in b["lines"] if (m := _NOTE_LINE.match(l))]
        if len(labels) != 1 or len(notes) != 1:
            errors.append(f"{b['id']}: needs exactly one label: and one note: line "
                          f"(found {len(labels)} and {len(notes)})")
            continue
        kind, note = labels[0].strip("` "), notes[0].strip()
        if not kind and not note:
            skipped.append(b["id"])
            continue
        if not kind:
            kind = UNASSIGNED
        elif kind not in FLAG_LABEL_KINDS:
            errors.append(f"{b['id']}: {labels[0]!r} is not one of {FLAG_LABEL_KINDS} "
                          f"(leave it blank with a note if none fits)")
            continue
        parsed.append({"notice_id": b["id"], "kind": kind, "note": note})
    return parsed, skipped, errors


def ingest_sheet(path: Path, labelled_by: str, *, queue_path: Path = QUEUE_JSON,
                 labels_path: Path | None = None) -> dict:
    """
    Validate everything, then append. A notice already disposed by this
    labeller is refused: the first disposition is the one that counts, as in
    filter_audit.blinding, because a second one could have been made after a
    reveal.
    """
    if labelled_by not in LABELLERS:
        raise ValueError(f"labelled_by must be one of {LABELLERS}")
    if not queue_path.exists():
        raise FlagSheetError("no current sheet - run flag-sheet first")
    queue = json.loads(queue_path.read_text(encoding="utf-8"))
    raw = Path(path).read_bytes()
    parsed, skipped, errors = parse_sheet(raw.decode("utf-8"),
                                          [it["notice_id"] for it in queue["items"]])
    already = disposed_ids(load_labels(labels_path), labelled_by)
    errors += [f"{p['notice_id']}: already has a {labelled_by} disposition; the first "
               f"one stands" for p in parsed if p["notice_id"] in already]
    if errors:
        raise FlagSheetError(f"{Path(path).name}: {len(errors)} problem(s), nothing "
                             f"written:\n  " + "\n  ".join(errors))
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    source = {"sheet_sha256": hashlib.sha256(raw).hexdigest(),
              "queue_sheet_sha256": queue.get("sheet_sha256")}
    # Role is written WITH the disposition, never before it: the committed file
    # must be readable without the uncommitted queue, and at this point the
    # reader has already decided.
    role = {it["notice_id"]: it["role"] for it in queue["items"]}
    source["queue_seed"] = queue.get("seed")
    records = [{"notice_id": p["notice_id"], "kind": p["kind"], "note": p["note"],
                "role": role[p["notice_id"]], "labelled_by": labelled_by,
                "labelled_at": now, "source": source}
               for p in parsed]
    labels_path = labels_path or ingest_paths.CODED_FLAG_LABELS
    labels_path.parent.mkdir(parents=True, exist_ok=True)
    with labels_path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write("".join(json.dumps(r, sort_keys=True) + "\n" for r in records))
    return {"written": len(records), "skipped_unread": len(skipped),
            "unassigned": sum(r["kind"] == UNASSIGNED for r in records)}


# ---------------------------------------------------------------------------
# Reveal
# ---------------------------------------------------------------------------

def reveal(item: dict, disposition: dict | None, flags_by_id: dict) -> dict:
    """Role, choice and band for one item - refused without a disposition."""
    if not disposition:
        raise blinding.RevealRefused(
            f"{item['notice_id']}: no disposition recorded. Blinding is enforced "
            f"here, not by convention - label it on the sheet first.")
    flag = flags_by_id.get(item["notice_id"]) if item["role"] == "flag" else None
    return {"notice_id": item["notice_id"], "label": disposition["kind"],
            "note": disposition["note"], "role": item["role"],
            "jev_choice": flag["jev_choice"] if flag else None,
            "mass_band": flag["mass_band"] if flag else None}


def write_revealed(labelled_by: str = "human", *, queue_path: Path = QUEUE_JSON,
                   labels_path: Path | None = None, store_path: Path | None = None,
                   out_path: Path = REVEALED_MD) -> dict:
    queue = json.loads(queue_path.read_text(encoding="utf-8"))
    first = {}
    for r in load_labels(labels_path):
        if r["labelled_by"] == labelled_by:
            first.setdefault(r["notice_id"], r)
    flags_by_id = {f["notice_id"]: f for f in flag_store.load(store_path)}
    shown, withheld = [], 0
    for item in queue["items"]:
        try:
            shown.append(reveal(item, first.get(item["notice_id"]), flags_by_id))
        except blinding.RevealRefused:
            withheld += 1
    table = Counter((r["label"], r["role"]) for r in shown)
    kinds = FLAG_LABEL_KINDS + (UNASSIGNED,)
    out = [f"# Coded-notice flags - revealed ({labelled_by})", "",
           f"{len(shown)} labelled and revealed; {withheld} still withheld (no "
           f"disposition).", "", "| label | flag | control |", "|---|---|---|"]
    out += [f"| {k} | {table[(k, 'flag')]} | {table[(k, 'control')]} |" for k in kinds]
    out += [""]
    for r in shown:
        out += [f"## {r['notice_id']}", "", f"- label: `{r['label']}`  note: {r['note']}",
                f"- role: **{r['role']}**"
                + (f"; model's choice {r['jev_choice']}, band {r['mass_band']}"
                   if r["role"] == "flag" else ""), ""]
    out_path.write_text("\n".join(out), encoding="utf-8", newline="\n")
    return {"revealed": len(shown), "withheld": withheld,
            "table": {f"{k}/{role}": n for (k, role), n in table.items()}}
