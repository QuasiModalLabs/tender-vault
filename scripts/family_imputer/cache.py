"""
Verdict cache and call ledger - data/family_imputer/cache.db, gitignored.

KEYED ON FOUR THINGS, because any one of them changing means a different
measurement: notice id, the hash of the exact state sent, the model version
that answered, and the question hash. A notice whose text is re-ingested, a
new state budget, a new model or a new question is a miss, never a stale hit.

APPEND-ONLY, ENFORCED BY THE STORE. Triggers abort any UPDATE or DELETE on
either table, so a verdict cannot be quietly replaced after the report has
read it. A second insert under the same key is refused by the primary key.

THE LEDGER COUNTS EVERY CALL, including check-key's probe and any response
refused for a model mismatch - those were billed too. Cost is summed from the
ledger, not from the verdicts, so it is what was spent rather than what was
kept.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = PROJECT_ROOT / "data" / "family_imputer"
CACHE_DB = DATA_DIR / "cache.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS verdicts (
    notice_id       TEXT NOT NULL,
    content_sha256  TEXT NOT NULL,
    model_version   TEXT NOT NULL,
    question_sha256 TEXT NOT NULL,
    choice          TEXT NOT NULL,
    probabilities   TEXT NOT NULL,
    confidence      REAL,
    input_tokens    INTEGER NOT NULL,
    truncated       INTEGER NOT NULL,
    original_chars  INTEGER NOT NULL,
    created_at      TEXT NOT NULL,
    PRIMARY KEY (notice_id, content_sha256, model_version, question_sha256)
);
CREATE TABLE IF NOT EXISTS calls (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    purpose         TEXT NOT NULL,
    notice_id       TEXT,
    model_version   TEXT,
    input_tokens    INTEGER,
    output_tokens   INTEGER,
    outcome         TEXT NOT NULL,
    created_at      TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS verdicts_no_update BEFORE UPDATE ON verdicts
    BEGIN SELECT RAISE(ABORT, 'verdicts are append-only'); END;
CREATE TRIGGER IF NOT EXISTS verdicts_no_delete BEFORE DELETE ON verdicts
    BEGIN SELECT RAISE(ABORT, 'verdicts are append-only'); END;
CREATE TRIGGER IF NOT EXISTS calls_no_update BEFORE UPDATE ON calls
    BEGIN SELECT RAISE(ABORT, 'the call ledger is append-only'); END;
CREATE TRIGGER IF NOT EXISTS calls_no_delete BEFORE DELETE ON calls
    BEGIN SELECT RAISE(ABORT, 'the call ledger is append-only'); END;
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: Path = CACHE_DB) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


def get(conn, notice_id, content_sha256, model_version, question_sha256):
    row = conn.execute(
        "SELECT * FROM verdicts WHERE notice_id=? AND content_sha256=? "
        "AND model_version=? AND question_sha256=?",
        (notice_id, content_sha256, model_version, question_sha256)).fetchone()
    if row is None:
        return None
    out = dict(row)
    out["probabilities"] = json.loads(out["probabilities"])
    return out


def put(conn, notice_id, prepared, question_sha256, verdict: dict) -> None:
    conn.execute(
        "INSERT INTO verdicts VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (notice_id, prepared.content_sha256, verdict["model"], question_sha256,
         verdict["choice"], json.dumps(verdict["probabilities"], sort_keys=True),
         verdict["confidence"], verdict["input_tokens"], int(prepared.truncated),
         prepared.original_chars, _now()))


def log_call(conn, purpose, notice_id, model_version, input_tokens,
             output_tokens, outcome) -> None:
    conn.execute(
        "INSERT INTO calls (purpose, notice_id, model_version, input_tokens, "
        "output_tokens, outcome, created_at) VALUES (?,?,?,?,?,?,?)",
        (purpose, notice_id, model_version, input_tokens, output_tokens,
         outcome, _now()))


def spend(conn) -> dict:
    rows = conn.execute(
        "SELECT purpose, COUNT(*) n, COALESCE(SUM(input_tokens),0) inp, "
        "COALESCE(SUM(output_tokens),0) outp FROM calls GROUP BY purpose").fetchall()
    return {r["purpose"]: {"calls": r["n"], "input_tokens": r["inp"],
                           "output_tokens": r["outp"]} for r in rows}
