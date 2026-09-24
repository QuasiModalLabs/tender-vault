"""
Jev family imputer - phase 1 evaluation.

    check-key   is TYPESAFE_API_KEY readable, and does one call succeed
    question    the live question hash beside the recorded one
    pilot       impute a seeded 200-notice sample; token count and cost estimate
    run         impute every coded notice (resumable - cached verdicts are skipped)
    report      score against the publisher's codes; write report.json + confusion.csv
    sheet       write the disagreement sample to disagreements.md for reading
    label       record one printed disagreement as jev_wrong, publisher_miscoded
                or out_of_scope

Usage:
    python scripts/family_imputer check-key
    python scripts/family_imputer pilot
    python scripts/family_imputer run --workers 8
    python scripts/family_imputer report
    python scripts/family_imputer label WS12345 publisher_miscoded --note "..."

pilot and run refuse unless the question hash matches its recorded literal AND
ref-004's decision rule was committed before them - see evaluate.py.
"""
from __future__ import annotations

import argparse
import sys
import textwrap

import ingest

from . import cache, evaluate as E
from .client import JevClient, JevError, key_status, validate_answer
from .question import (QUESTION_SHA256, FrozenQuestionDrift, build_question,
                       frozen_question)


def _fmt(rate: dict) -> str:
    if rate["value"] is None:
        return f"undefined   (0 of 0)"
    lo, hi = rate["wilson95"]
    return f"{rate['value']:.3f} ({lo:.3f}-{hi:.3f})  {rate['k']}/{rate['n']}"


def cmd_check_key(args) -> int:
    status = key_status()
    print(f"TYPESAFE_API_KEY: {status}")
    if status != "set":
        return 1
    probe = {"type": "choice", "instructions": "Which does this describe?",
             "criteria": {"furniture": None, "software": None}}
    conn = cache.connect()
    try:
        body = JevClient().ask({"title": "Supply of office chairs"}, probe)
        verdict = validate_answer(body, ("furniture", "software"))
    except JevError as exc:
        cache.log_call(conn, "check-key", None, None, None, None, "error")
        conn.commit()
        print(f"probe call FAILED: {exc}")
        return 1
    cache.log_call(conn, "check-key", None, verdict["model"], verdict["input_tokens"],
                   verdict["output_tokens"], "ok")
    conn.commit()
    print(f"probe call ok: model {verdict['model']}, answered {verdict['choice']!r}, "
          f"{verdict['input_tokens']} input tokens")
    return 0


def cmd_measure_overhead(args) -> int:
    """
    The per-call fixed cost, measured rather than fitted: the frozen question
    over a state with the run's shape and no text. Logged in the ledger as
    `overhead`; never cached as a verdict, because it is not about a notice.
    """
    q = frozen_question()
    conn = cache.connect()
    try:
        body = JevClient().ask({"title": "", "description": ""}, q.payload)
        verdict = validate_answer(body, q.keys)
    except JevError as exc:
        cache.log_call(conn, "overhead", None, None, None, None, "error")
        conn.commit()
        print(f"overhead call FAILED: {exc}")
        return 1
    cache.log_call(conn, "overhead", None, verdict["model"], verdict["input_tokens"],
                   verdict["output_tokens"], "ok")
    conn.commit()
    print(f"question {q.sha256[:16]}.. model {verdict['model']}")
    print(f"fixed overhead: {verdict['input_tokens']} input tokens "
          f"(empty title and description), {verdict['output_tokens']} output")
    return 0


def cmd_question(args) -> int:
    q = build_question()
    print(f"options   {len(q.options)}")
    for o in q.options:
        print(f"  {o.kind:8} {o.key}")
    print(f"live      {q.sha256}")
    print(f"recorded  {QUESTION_SHA256}")
    print("MATCH" if q.sha256 == QUESTION_SHA256 else "DRIFT - runs will refuse")
    return 0


def _gate():
    q = frozen_question()
    commit = E.preregistration_check(q.sha256)
    print(f"question {q.sha256[:16]}.. frozen; rule pre-registered in {commit[:10]}")
    return q


def cmd_pilot(args) -> int:
    q = _gate()
    blind, _answers = E.load_coded()
    items = E.pilot_items(blind, args.n)
    conn = cache.connect()
    stats = E.impute(items, q, "pilot", JevClient(), conn, workers=args.workers)
    pilot_ids = {nid for nid, _ in items}
    tokens = [r[0] for r in conn.execute(
        "SELECT notice_id, input_tokens FROM verdicts WHERE question_sha256=? "
        "AND model_version=?", (q.sha256, MODEL)) if r[0] in pilot_ids]
    mean = sum(tokens) / len(tokens) if tokens else 0
    est = mean * len(blind)
    print(f"pilot: {stats}")
    print(f"mean input tokens/notice {mean:,.0f} over {len(tokens)} verdicts")
    print(f"extrapolated full run: {est:,.0f} input tokens over {len(blind):,} "
          f"notices = ${est / 1e6 * E.PRICE_PER_MILLION_INPUT:.2f}")
    print(f"price: {E.PRICE_SOURCE}")
    return 0


def cmd_run(args) -> int:
    q = _gate()
    blind, _answers = E.load_coded()
    conn = cache.connect()
    stats = E.impute(blind, q, "run", JevClient(), conn, workers=args.workers)
    print(f"run: {stats}")
    return 0 if stats["errors"] == 0 else 2


def _print_report(result: dict, spend: dict, sample: list) -> None:
    cov = result["coverage"]
    print(f"coverage: {cov['imputed']:,} of {cov['coded_notices']:,} coded notices "
          f"imputed ({cov['missing']:,} missing)")
    if "headline" not in result:
        return
    h = result["headline"]
    print(f"\nHEADLINE - admit class ({h['publisher_admits']:,} publisher admits)")
    print(f"{'':30}{'recall':42}precision")
    print(f"{'Jev (top choice)':30}{_fmt(h['jev']['recall']):42}{_fmt(h['jev']['precision'])}")
    print(f"{'Keyword branch (production)':30}{_fmt(h['keyword']['recall']):42}"
          f"{_fmt(h['keyword']['precision'])}")
    print(f"{'  ^ ' + 'see keyword handicap below':30}")
    print(f"{'Majority baseline (never)':30}{'0.000':42}undefined (no admits)")
    for line in (h["population_caveat"], h["keyword_handicap"]):
        print(textwrap.fill(line, 100, initial_indent="  ", subsequent_indent="  "))

    d = result["decision"]
    print(f"\nPRE-REGISTERED RULE: {d['outcome']}")
    if "inputs" in d:
        print(f"  proceed if {E.RULE['proceed']}")
        print(f"  kill if    {E.RULE['kill']}")
    else:
        print(f"  {d.get('why')}")
    print(textwrap.fill(E.POPULATION_CAVEAT, 100, initial_indent="  ",
                        subsequent_indent="  "))

    print("\nBy source system (population caveat applies to both)")
    for src, v in result["by_source"].items():
        print(f"  {src:4} n={v['n']:6,}  Jev R {_fmt(v['jev']['recall'])}  "
              f"P {_fmt(v['jev']['precision'])}")
        print(f"  {'':4} {'':8}  KW  R {_fmt(v['keyword']['recall'])}  "
              f"P {_fmt(v['keyword']['precision'])}")

    g = result["diagnostics"]
    print("\nDIAGNOSTICS (not the headline)")
    print(f"  set agreement           {_fmt(g['set_agreement'])}")
    b = g["set_agreement_majority_baseline"]
    print(f"  majority-class baseline {_fmt(b)}  always {b['constant_answer']!r}")
    print(f"  strict, single-label    {_fmt(g['strict_agreement_single_label'])}")
    print(f"  multi-label {g['multi_label']:,}; unscorable (only unmappable codes) "
          f"{g['unscorable_empty_label_set']}; with any unmappable code "
          f"{g['notices_with_unmappable_codes']}; truncated {g['truncated']}")
    print(f"  by publisher segment ({g['by_segment_note']}):")
    print(f"    {'seg':4}{'n':>7}  {'set agreement':34}{'pub adm':>8}{'jev adm':>8}{'kw adm':>8}")
    for seg, v in list(g["by_segment"].items())[:20]:
        print(f"    {seg:4}{v['n']:7,}  {_fmt(v['set_agreement']):34}"
              f"{v['publisher_admit']:8,}{v['jev_admit']:8,}{v['kw_admit']:8,}")
    print("  P(profile options) decile -> publisher admit rate:")
    for row in g["p_profile_deciles"]:
        print(f"    {row['bucket']}  n={row['n']:6,}  admit {row['publisher_admit']:5,}"
              f"  rate {row['publisher_admit_rate']:.3f}")
    conf = result["_confusion"]
    print(f"  confusion ({g['confusion_note']}) -> {E.CONFUSION_CSV}")
    off = sorted(((n, pk, jk) for (pk, jk), n in conf.items() if pk != jk), reverse=True)[:20]
    diag = sorted(((n, pk) for (pk, jk), n in conf.items() if pk == jk), reverse=True)[:10]
    print("    top off-diagonal (publisher -> Jev):")
    for n, pk, jk in off:
        print(f"      {n:5,}  {pk[:48]:48} -> {jk[:48]}")
    print("    top diagonal:")
    for n, pk in diag:
        print(f"      {n:5,}  {pk}")

    c = E.cost(spend)
    print(f"\nTOKENS AND COST (every call, from the ledger)")
    for purpose, v in spend.items():
        print(f"  {purpose:10} {v['calls']:7,} calls  {v['input_tokens']:13,} input  "
              f"{v['output_tokens']:9,} output")
    print(f"  total      {c['calls']:7,} calls  {c['input_tokens']:13,} input  "
          f"=> ${c['usd']:.2f}   ({E.PRICE_SOURCE})")

    print(f"\nDISAGREEMENTS - {len(sample)} sampled (seed {E.SEED}). Record each with:")
    print(f"  python scripts/family_imputer label <notice_id> {'|'.join(E.LABEL_KINDS)} [--note ...]")
    for i, r in enumerate(sample, 1):
        desc = " ".join(r["description"].split())[:500]
        print(f"\n[{i:02}] {r['notice_id']}  {r['direction']}")
        print(f"     title:     {r['title'][:140]}")
        print(f"     publisher: {', '.join(r['codes'][:8])}{' ...' if len(r['codes']) > 8 else ''}")
        print(f"                -> {'; '.join(r['labels'])}")
        print(f"     Jev:       {r['choice']}  p={r['choice_p']:.3f}")
        print(f"     keywords:  {', '.join(r['kw_hits']) or '(none)'}")
        print(textwrap.fill(desc, 100, initial_indent="     ", subsequent_indent="     "))


def cmd_report(args) -> int:
    q = frozen_question()
    blind, answers = E.load_coded()
    profile = ingest.parse_profile(ingest.DEFAULT_PROFILE)
    conn = cache.connect()
    result = E.score(blind, answers, q, conn, profile["unspsc_families"],
                     profile["competencies"])
    spend = cache.spend(conn)
    sample = E.disagreement_sample(result.get("_rows", []))
    E.write_outputs(result, q, spend, sample)
    _print_report(result, spend, sample)
    print(f"\nwrote {E.REPORT_JSON} and {E.CONFUSION_CSV}")
    return 0


def cmd_sweep(args) -> int:
    """Phase 2: threshold sweep and keyword union, from the cache only.
    Constructs no JevClient, so it cannot make a call."""
    from . import sweep as S
    q = frozen_question()
    blind, answers = E.load_coded()
    profile = ingest.parse_profile(ingest.DEFAULT_PROFILE)
    result = E.score(blind, answers, q, cache.connect(), profile["unspsc_families"],
                     profile["competencies"])
    if result["coverage"]["missing"]:
        print(f"error: {result['coverage']['missing']} notices lack a cached verdict")
        return 1
    rows = result["_rows"]
    points, ref = S.curve(rows)
    out = cache.DATA_DIR / "sweep.csv"
    S.write_csv(points, out)

    print("POST HOC: nothing below was pre-registered. Recall is against publisher codes.")
    print(textwrap.fill(E.POPULATION_CAVEAT, 100))
    for name, m in ref.items():
        print(f"reference  {name:15} R {_fmt(m['recall'])}  P {_fmt(m['precision'])}  "
              f"admits {m['admits']:,}")
    print(f"\n{'t':>5}  {'Jev mass  recall':36}{'precision':36}{'admits':>7}"
          f"   {'union  recall':36}{'precision':36}{'admits':>7}")
    for p in points:
        j, u = p["jev_mass"], p["union"]
        print(f"{p['t']:5.3f}  {_fmt(j['recall']):36}{_fmt(j['precision']):36}{j['admits']:7,}"
              f"   {_fmt(u['recall']):36}{_fmt(u['precision']):36}{u['admits']:7,}")
    print(f"\npublisher admits by method (TP), and false positives by method")
    print(f"{'t':>5} {'both':>6} {'Jev only':>9} {'KW only':>8} {'neither':>8}"
          f"   {'FP both':>8} {'FP Jev only':>12} {'FP KW only':>11}")
    for p in points:
        print(f"{p['t']:5.3f} {p['tp_both']:6,} {p['tp_jev_only']:9,} {p['tp_kw_only']:8,} "
              f"{p['tp_neither']:8,}   {p['fp_both']:8,} {p['fp_jev_only']:12,} {p['fp_kw_only']:11,}")
    for key in ("jev_mass", "union"):
        hit = S.highest_t_reaching(points, key)
        if hit is None:
            print(f"\n{key}: no threshold on the grid reaches recall {S.RECALL_TARGET}")
            continue
        print(f"\n{key}: highest t with recall >= {S.RECALL_TARGET} is {hit['t']:.3f}: "
              f"R {_fmt(hit[key]['recall'])}  P {_fmt(hit[key]['precision'])}  "
              f"admits {hit[key]['admits']:,}")
        sh = S.split_half(rows, key)
        if sh["chosen_t"] is not None:
            print(f"  split-half: t chosen on half A = {sh['chosen_t']:.3f}; "
                  f"half A R {_fmt(sh['half_a']['recall'])}; "
                  f"half B R {_fmt(sh['half_b']['recall'])}  P {_fmt(sh['half_b']['precision'])}")
    print("\nNOTE: Jev returns probabilities rounded to 0.01, so profile mass moves in")
    print("steps of 0.01 and any t in (0, 0.01] selects the same notices.")
    print(f"\nwrote {out}")
    return 0


def cmd_misses(args) -> int:
    """
    Phase 2: the publisher admits both methods miss, from the cache only.
    Mass histogram for misses at --t (and for phase 1's top-choice misses),
    then every admit found by neither Jev mass >= t nor the keyword branch.
    """
    from . import sweep as S
    q = frozen_question()
    blind, answers = E.load_coded()
    profile = ingest.parse_profile(ingest.DEFAULT_PROFILE)
    families = profile["unspsc_families"]
    result = E.score(blind, answers, q, cache.connect(), families,
                     profile["competencies"])
    rows = result["_rows"]
    pos = [r for r in rows if r["publisher_admit"]]
    for label, missed in (
            (f"Jev mass < {args.t}", [r for r in pos if r["p_profile"] < args.t]),
            ("phase 1 top choice (not a profile option)",
             [r for r in pos if not r["jev_admit"]])):
        print(f"\nprofile-option mass of publisher admits missed by {label}: n={len(missed)}")
        for b in S.mass_histogram(missed):
            print(f"  [{b['lo']:.3f}, {b['hi']:.3f})  {b['n']:5,}")
    both = S.neither(rows, args.t)
    print(f"\nFOUND BY NEITHER (Jev mass < {args.t} and no keyword): n={len(both)}")
    for r in sorted(both, key=lambda r: (r["choice"], r["notice_id"])):
        pcodes = [c for c in r["codes"] if ingest.matches_unspsc_families({c}, families)]
        print(f"{r['notice_id']}\t{r['source']}\tmass={r['p_profile']:.2f}\t"
              f"jev={r['choice']} p={r['choice_p']:.2f}\t"
              f"profile_codes={','.join(pcodes)}\tall_codes={len(r['codes'])}\t"
              f"{' '.join(r['title'].split())[:110]}")
    return 0


def cmd_sheet(args) -> int:
    """The disagreement sample as a Markdown reading sheet. Cache only."""
    from .options import load_reference
    q = frozen_question()
    blind, answers = E.load_coded()
    profile = ingest.parse_profile(ingest.DEFAULT_PROFILE)
    result = E.score(blind, answers, q, cache.connect(), profile["unspsc_families"],
                     profile["competencies"])
    sample = E.disagreement_sample(result.get("_rows", []))
    path = E.write_label_sheet(sample, load_reference())
    print(f"wrote {len(sample)} notices to {path}")
    return 0


def cmd_label(args) -> int:
    rec = E.record_label(args.notice_id, args.kind, args.note or "")
    print(f"recorded {rec['notice_id']} as {rec['kind']} -> {E.LABELS_JSONL}")
    return 0


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="family_imputer",
                                 description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check-key").set_defaults(fn=cmd_check_key)
    sub.add_parser("question").set_defaults(fn=cmd_question)
    sub.add_parser("measure-overhead").set_defaults(fn=cmd_measure_overhead)
    p = sub.add_parser("pilot")
    p.add_argument("--n", type=int, default=E.PILOT_N)
    p.add_argument("--workers", type=int, default=8)
    p.set_defaults(fn=cmd_pilot)
    p = sub.add_parser("run")
    p.add_argument("--workers", type=int, default=8)
    p.set_defaults(fn=cmd_run)
    sub.add_parser("report").set_defaults(fn=cmd_report)
    sub.add_parser("sweep").set_defaults(fn=cmd_sweep)
    sub.add_parser("sheet").set_defaults(fn=cmd_sheet)
    p = sub.add_parser("misses")
    p.add_argument("--t", type=float, default=0.05)
    p.set_defaults(fn=cmd_misses)
    p = sub.add_parser("label")
    p.add_argument("notice_id")
    p.add_argument("kind", choices=E.LABEL_KINDS)
    p.add_argument("--note")
    p.set_defaults(fn=cmd_label)
    args = ap.parse_args(argv)
    try:
        sys.exit(args.fn(args))
    except (JevError, FrozenQuestionDrift, E.NotPreregistered, E.SampleDrift,
            ValueError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
