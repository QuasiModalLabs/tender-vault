"""
Ingest Treasury Board's Departmental Plans / Results expenditures-by-program
data into a local SQLite database, scoring each program's planning and variance
prose by SEMANTIC THEME so pre-RFP opportunity signals surface.

Data: GC InfoBase — Expenditures and FTE by Program and by Organization
      https://open.canada.ca/data/en/dataset/b15ee8d7-2ac0-4656-8330-6c60d085cda8
Licence: Open Government Licence - Canada.

WHY THIS EXISTS. The contracts dataset says WHAT was bought, in coarse category
labels. It never says WHY a department might be vulnerable. Departmental Plans
do, in two different tenses:

  - `planning_explanation` — forward-looking. What a department says it intends
    to do: "investing to modernize the case management platform." This is the
    PRIMARY signal, because intent precedes procurement.
  - `variance_explanation` — retrospective. Why spending or headcount diverged
    from plan: "additional spending to address backlog pressures," "operational
    pressures related to the pay system." Secondary context — evidence of the
    strain that produces intent.

Both are capability-level signals a contract record can never carry.

TWO-POLE SEMANTIC THEMING. Both fields are mixed: some rows carry real signal,
others are dry boilerplate (timing of statutory payments — no opportunity). A
keyword filter can't tell them apart reliably ("unable to meet demand" has no
keyword). Instead we score by MEANING, each field against its own theme pair:

    intent_score   = similarity(text, MODERNIZATION_theme)
                   - similarity(text, ROUTINE_theme)

    pressure_score = similarity(text, PRESSURE_theme)
                   - similarity(text, ACCOUNTING_theme)

Subtracting the second pole is what does the work: a single positive theme
ranks anything that sounds governmental, while the anti-pole actively pushes
boilerplate down. Ranking and --show-extremes use intent_score; see
score_programs for why planning beats variance as the primary field.

Each theme is defined in the profile by EXAMPLE SENTENCES (not keywords). The
embedding model (all-MiniLM-L6-v2, the same one the tender corpus uses) turns
the examples into a reference vector; every explanation is scored by cosine
similarity toward its positive pole and away from its paired anti-pole. Editing
the example sentences in the profile reshapes what the theme catches — you tune
by DESCRIBING the signal, and the model generalizes to phrasings you didn't list.

The scoring runs once, at ingest, entirely locally (no Claude tokens). Queries
just sort by the stored score. See `program-signals` in tender_tools.

Usage:
    python scripts/plans_ingest.py
    python scripts/plans_ingest.py --source path/to/local.csv   # offline/test
    python scripts/plans_ingest.py --show-extremes              # print top/bottom scored
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).parent))
from ingest import (  # noqa: E402
    REQUEST_HEADERS, output_path, parse_profile, staged_db,
)

PLANS_URL = (
    "https://open.canada.ca/data/en/datastore/dump/"
    "64774bc1-c90a-4ae2-a3ac-d9b50673a895?bom=True"
)
PROJECT_ROOT = Path(__file__).parent.parent
DEFAULT_PROFILE = PROJECT_ROOT / "vault" / "profiles" / "my-company.md"
DB_PATH = PROJECT_ROOT / "data" / "plans.db"
CACHE_PATH = PROJECT_ROOT / ".cache" / "plans.csv"
EMBED_MODEL = "all-MiniLM-L6-v2"

# Fallback themes if the profile defines none — so the script runs out of the
# box. Real tuning happens in the profile's plan_themes block.
DEFAULT_PRESSURE = [
    "spending increased to address service backlogs and processing delays",
    "additional resources needed to meet growing demand and workload",
    "program struggled to keep pace with rising caseloads and pressures",
    "modernizing aging systems that could no longer support operations",
]
DEFAULT_ACCOUNTING = [
    "variance due to timing of statutory payments and reprofiled funds",
    "difference reflects legislative timing and accounting adjustments",
]
DEFAULT_INTENT = [
    "investing to modernize and replace an aging IT system",
    "planned funding to migrate systems and infrastructure to the cloud",
    "modernization initiative to transform digital service delivery",
    "developing a new platform to support program operations",
]
DEFAULT_ROUTINE = [
    "ongoing delivery of the program at planned funding levels",
    "routine statutory transfer payments to recipients",
    "no significant change from the prior year plan",
]

# Column names from the diagnosed schema (rbpo_rppo_en.csv).
COLS = {
    "year": "fy_ef",
    "org_id": "organization_id",
    "org": "organization",
    "core": "core_responsibility",
    "program_id": "program_id",
    "program": "program_name",
    "planned_1": "planned_spending_1",
    "actual": "actual_spending",
    "planned_2": "planned_spending_2",
    "planned_3": "planned_spending_3",
    "planned_ftes_1": "planned_ftes_1",
    "actual_ftes": "actual_ftes",
    "variance": "variance_explanation",
    "planning": "planning_explanation",
}


def load_source(source: str | None) -> pd.DataFrame:
    """Read the plans CSV from a local path or download+cache it."""
    if source:
        print(f"Reading local file: {source}")
        return pd.read_csv(source, low_memory=False)
    if CACHE_PATH.exists():
        age_h = (datetime.now().timestamp() - CACHE_PATH.stat().st_mtime) / 3600
        if age_h < 24:
            print(f"Using cached plans.csv ({age_h:.1f}h old)")
            return pd.read_csv(CACHE_PATH, low_memory=False)
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    print("Downloading plans CSV from open.canada.ca ...")
    r = requests.get(PLANS_URL, headers=REQUEST_HEADERS, timeout=180)
    r.raise_for_status()
    CACHE_PATH.write_bytes(r.content)
    print(f"Saved {len(r.content)//1024} KB to {CACHE_PATH}")
    return pd.read_csv(CACHE_PATH, low_memory=False)


def theme_vector(model, examples: list[str]):
    """Average the embeddings of a theme's example sentences into one vector."""
    import numpy as np
    embs = model.encode(examples, normalize_embeddings=True)
    v = embs.mean(axis=0)
    n = (v ** 2).sum() ** 0.5
    return v / n if n else v


def _score_field(df, model, colkey, pos_vec, neg_vec, label):
    """Two-pole score for one text column: cosine(pos) - cosine(neg). Rows with
    no text score None (missing = 'nothing said', not 'neutral')."""
    texts = df[COLS[colkey]].fillna("").astype(str).map(_strip_md)
    mask = texts.str.strip() != ""
    scores = pd.Series([None] * len(df), index=df.index, dtype=object)
    if mask.any():
        filled = texts[mask].tolist()
        print(f"Embedding {len(filled)} {label}...")
        embs = model.encode(filled, normalize_embeddings=True,
                            show_progress_bar=False, batch_size=64)
        two_pole = (embs @ pos_vec) - (embs @ neg_vec)
        scores.loc[mask] = [round(float(s), 4) for s in two_pole]
    return scores


def score_programs(df, intent_ex, noise_ex, pressure_ex, accounting_ex):
    """
    Score TWO fields with TWO theme pairs:
      - planning_explanation  -> intent_score  (forward-looking: what they PLAN
        to do — modernize, replace, invest — the true pre-RFP signal)
      - variance_explanation  -> pressure_score (retrospective: what strained —
        kept as secondary context; a program that both struggled and plans to
        fix it is the strongest signal, but planning is what we rank on)

    The planning field is the primary signal because it's forward-looking AND
    because it's the one that stays populated. Discovered by reading the data:
    variance-based ranking surfaced years-old archaeology and scored real IT
    modernizations negative.

    Rows carrying text, measured against data/plans.db (ingested 2026-08-04,
    the live GC InfoBase resource — an earlier frozen CSV stopped at 2021 and
    gave a different, worse picture):

        year   planning   variance
        2020        213        834
        2021        411        781
        2022        444        835
        2023        403        730
        2024        408        490
        2025        520          0

    Variance is the field that dies, not planning — it thins from 2024 and is
    empty for 2025, while planning is at its strongest there. Ranking on
    variance would mean ranking on nothing for the most recent year in the
    data, which is the only year a pre-RFP signal is actually actionable.
    Re-measure before trusting these counts; they are a snapshot, not an
    invariant.
    """
    from sentence_transformers import SentenceTransformer
    print(f"Loading embedding model ({EMBED_MODEL})...")
    model = SentenceTransformer(EMBED_MODEL)

    intent_vec = theme_vector(model, intent_ex)
    noise_vec = theme_vector(model, noise_ex)
    pressure_vec = theme_vector(model, pressure_ex)
    accounting_vec = theme_vector(model, accounting_ex)

    intent_scores = _score_field(df, model, "planning", intent_vec, noise_vec,
                                 "planning explanations (intent)")
    pressure_scores = _score_field(df, model, "variance", pressure_vec, accounting_vec,
                                   "variance explanations (strain)")
    return intent_scores, pressure_scores


def _fy_to_year(fy):
    """Fiscal-year field is 'YYYY-YYYY' (e.g. '2024-2025'); take the start year
    as an int. Falls back to None on anything unparseable."""
    if fy is None or (isinstance(fy, float) and pd.isna(fy)):
        return None
    s = str(fy).strip()
    head = s.split("-")[0]
    return int(head) if head.isdigit() else None


def _strip_md(text: str) -> str:
    """The newer planning/variance prose embeds markdown ('**Planned Spending**',
    bullet '*'). Strip it so the embedding model scores clean prose, not markup."""
    if not text:
        return ""
    import re
    t = re.sub(r"\*\*(.+?)\*\*", r"\1", str(text))   # bold
    t = re.sub(r"(?m)^\s*[\*\-]\s+", "", t)             # bullet markers
    t = t.replace("\r", " ").replace("\n", " ")
    return re.sub(r"\s+", " ", t).strip()


def build_db(df, intent_scores, pressure_scores, source_note: str,
             db_path: Path = DB_PATH) -> int:
    # Build to a .part file and swap only once complete, so a failure part-way
    # through the insert leaves the previous database untouched instead of
    # leaving none at all.
    def num(x):
        try:
            return None if pd.isna(x) else float(x)
        except (TypeError, ValueError):
            return None

    def txt(x):
        return "" if (x is None or (isinstance(x, float) and pd.isna(x))) else str(x)

    def ident(x):
        """organization_id is a stable numeric key (int64, no nulls in the
        source). Keep it an int so a later join isn't comparing '1' to '1.0' —
        which is exactly what txt() would produce if pandas ever widens the
        column to float because one row went null."""
        try:
            return None if pd.isna(x) else int(x)
        except (TypeError, ValueError):
            return None

    rows = []
    for idx, r in df.iterrows():
        rows.append((
            _fy_to_year(r[COLS["year"]]),
            ident(r[COLS["org_id"]]),
            txt(r[COLS["org"]]), txt(r[COLS["core"]]),
            txt(r[COLS["program_id"]]), txt(r[COLS["program"]]),
            num(r[COLS["planned_1"]]), num(r[COLS["actual"]]),
            num(r[COLS["planned_2"]]), num(r[COLS["planned_3"]]),
            num(r[COLS["planned_ftes_1"]]), num(r[COLS["actual_ftes"]]),
            _strip_md(txt(r[COLS["variance"]])), _strip_md(txt(r[COLS["planning"]])),
            intent_scores.loc[idx], pressure_scores.loc[idx],
        ))
    with staged_db(db_path) as con:
        con.execute("""
            CREATE TABLE programs (
                year INTEGER, organization_id INTEGER, organization TEXT,
                core_responsibility TEXT,
                program_id TEXT, program_name TEXT,
                planned_spending REAL, actual_spending REAL,
                planned_spending_next REAL, planned_spending_next2 REAL,
                planned_ftes REAL, actual_ftes REAL,
                variance_explanation TEXT, planning_explanation TEXT,
                intent_score REAL, pressure_score REAL
            )
        """)
        con.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        con.executemany(
            "INSERT INTO programs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows
        )
        con.execute("CREATE INDEX idx_org ON programs(organization)")
        con.execute("CREATE INDEX idx_org_id ON programs(organization_id)")
        con.execute("CREATE INDEX idx_intent ON programs(intent_score)")
        con.execute("CREATE INDEX idx_score ON programs(pressure_score)")
        con.execute("CREATE INDEX idx_year ON programs(year)")
        for k, v in [
            ("ingest_date", datetime.now().strftime("%Y-%m-%d")),
            ("source", source_note),
            ("row_count", str(len(rows))),
            ("intent_scored", str(intent_scores.notna().sum())),
            ("pressure_scored", str(pressure_scores.notna().sum())),
            ("licence", "Open Government Licence - Canada"),
        ]:
            con.execute("INSERT INTO meta VALUES (?, ?)", (k, v))
    return len(rows)


def show_extremes(n: int = 8, db_path: Path = DB_PATH) -> None:
    """Print highest/lowest INTENT-scored planning explanations so you can
    eyeball whether the theme examples pull the right rows (tuning aid)."""
    con = sqlite3.connect(db_path)
    print("\n=== HIGHEST intent_score (should read as forward-looking IT/modernization intent) ===")
    for org, prog, sc, pe in con.execute("""
        SELECT organization, program_name, intent_score, planning_explanation
        FROM programs WHERE intent_score IS NOT NULL
        ORDER BY intent_score DESC LIMIT ?""", (n,)):
        print(f"[{sc:+.3f}] {org} / {prog}\n    {pe[:200]}\n")
    print("=== LOWEST intent_score (should read as routine/no-intent) ===")
    for org, prog, sc, pe in con.execute("""
        SELECT organization, program_name, intent_score, planning_explanation
        FROM programs WHERE intent_score IS NOT NULL
        ORDER BY intent_score ASC LIMIT ?""", (n,)):
        print(f"[{sc:+.3f}] {org} / {prog}\n    {pe[:200]}\n")
    con.close()


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    parser.add_argument("--source", help="Local CSV path instead of downloading")
    parser.add_argument("--show-extremes", action="store_true",
                        help="Print top/bottom scored rows after ingest (tuning aid)")
    parser.add_argument(
        "--db", type=Path, default=None,
        help=f"Output DB path (default {DB_PATH}). --source redirects output to a "
             ".sample path unless this is given.",
    )
    args = parser.parse_args()

    db_path = output_path(
        DB_PATH, args.db,
        f"--source reads {args.source} instead of the published dataset"
        if args.source else None,
    )

    criteria = parse_profile(args.profile)
    themes = criteria.get("plan_themes") or {}
    intent_ex = themes.get("modernization_intent") or DEFAULT_INTENT
    noise_ex = themes.get("routine_noise") or DEFAULT_ROUTINE
    pressure_ex = themes.get("operational_pressure") or DEFAULT_PRESSURE
    accounting_ex = themes.get("accounting_noise") or DEFAULT_ACCOUNTING
    print(f"Pressure theme: {len(pressure_ex)} examples")
    print(f"Accounting (anti-)theme: {len(accounting_ex)} examples")

    df = load_source(args.source)
    print(f"Loaded {len(df)} program-year rows")

    missing = [c for c in COLS.values() if c not in df.columns]
    if missing:
        sys.stderr.write(f"Schema mismatch, missing columns: {missing}\n"
                         f"Present: {list(df.columns)}\n")
        sys.exit(2)

    intent_scores, pressure_scores = score_programs(
        df, intent_ex, noise_ex, pressure_ex, accounting_ex)
    source_note = args.source or PLANS_URL
    n = build_db(df, intent_scores, pressure_scores, source_note, db_path)
    print(f"\nWrote {n} rows to {db_path} ({db_path.stat().st_size/1e6:.1f} MB)")
    print(f"Intent-scored {intent_scores.notna().sum()} planning explanations, "
          f"pressure-scored {pressure_scores.notna().sum()} variance explanations")

    if args.show_extremes:
        show_extremes(db_path=db_path)

    print("\nAttribution: contains information licensed under the "
          "Open Government Licence - Canada.")


if __name__ == "__main__":
    main()
