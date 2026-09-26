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


def _scored_rows():
    q = frozen_question()
    blind, answers = E.load_coded()
    profile = ingest.parse_profile(ingest.DEFAULT_PROFILE)
    result = E.score(blind, answers, q, cache.connect(), profile["unspsc_families"],
                     profile["competencies"])
    if result["coverage"]["missing"]:
        raise ValueError(f"{result['coverage']['missing']} notices lack a cached verdict")
    return q, result["_rows"]


def cmd_ceiling(args) -> int:
    """Input ceiling: flag empty/boilerplate descriptions; headline with and
    without them. Cache only."""
    import random
    from collections import Counter
    from . import ceiling as C
    q, rows = _scored_rows()
    fired, bars = Counter(), Counter()
    for r in rows:
        f = C.flags(r["description"])
        r["_short"], r["_bp"] = f["short"], f["boilerplate_only"]
        r["_residue"] = f["residue"]
        fired.update(set(f["fired"]))
        for bar in C.STRICTER_BARS:
            if not f["short"] and f["residue_chars"] < bar:
                bars[bar] += 1
    short = [r for r in rows if r["_short"]]
    bp = [r for r in rows if r["_bp"]]
    kept = [r for r in rows if not (r["_short"] or r["_bp"])]
    print("POST HOC. Population: WS/cb coded notices only. Recall is against publisher codes.")
    print(f"\nflagged short (<{C.SHORT_CHARS} chars raw):          {len(short):6,}  "
          f"publisher admits among them {sum(r['publisher_admit'] for r in short):,}")
    print(f"flagged boilerplate_only (residue <{C.SHORT_CHARS}):   {len(bp):6,}  "
          f"publisher admits among them {sum(r['publisher_admit'] for r in bp):,}")
    print(f"  (the flags are exclusive by construction: boilerplate_only excludes short)")
    print(f"  boilerplate_only at stricter residue bars: "
          + ", ".join(f"<{b}: {bars[b]:,}" for b in C.STRICTER_BARS))
    print(f"total flagged {len(short) + len(bp):,} of {len(rows):,}; kept {len(kept):,}")
    print("\npattern hits (notices where the pattern removed at least one sentence):")
    for k in C.BOILERPLATE:
        print(f"  {k:28} {fired[k]:6,}")
    strict = [r for r in rows if not (r["_short"] or
              (not r["_short"] and len(r["_residue"]) < min(C.STRICTER_BARS)))]
    print(f"\n{'':44}{'recall':38}precision")
    for label, sub in (("ALL coded notices", rows),
                       (f"EXCL. short + residue<{C.SHORT_CHARS} (n={len(kept):,})", kept),
                       (f"EXCL. short + residue<{min(C.STRICTER_BARS)} (n={len(strict):,})", strict)):
        for name, key in (("Jev top", "jev_admit"), ("Keywords", "kw_admit")):
            m = E.admit_metrics(sub, key)
            print(f"{label + ' / ' + name:44}{_fmt(m['recall']):38}{_fmt(m['precision'])}")
    print(textwrap.fill(E.POPULATION_CAVEAT, 100))
    rng = random.Random(E.SEED)
    print(f"\nSAMPLE: 10 boilerplate_only residues (seed {E.SEED}) - check the rule "
          f"is not eating real work")
    for r in rng.sample(bp, min(10, len(bp))):
        print(f"\n  {r['notice_id']}  raw {len(r['description']):,} chars  "
              f"publisher_admit={r['publisher_admit']}")
        print(f"    title:   {' '.join(r['title'].split())[:110]}")
        print(f"    residue: {r['_residue'][:200] or '(nothing)'}")
    return 0


def cmd_strip_preview(args) -> int:
    """Variant B design: before/after, corpus-wide saving, cost. Cache only;
    nothing is sent and nothing is cached."""
    import sqlite3
    import statistics as st
    from . import strip as S
    S.check_frozen()
    q, rows = _scored_rows()
    conn = cache.connect()
    tokens = {r[0]: r[1] for r in conn.execute(
        "SELECT notice_id, input_tokens FROM verdicts WHERE question_sha256=? "
        "AND model_version=?", (q.sha256, E.MODEL))}
    OVERHEAD = 3471  # measured, ref-004 `measure-overhead`
    changed, swallowed = [], []
    for r in rows:
        s = S.strip_supplier_lists(r["description"])
        if s.runs:
            changed.append((r, s))
            swallowed += [(r["notice_id"], l) for l in S.swallowed_lines(r["description"])]

    def slope(pairs):
        xs, ys = [p[0] for p in pairs], [p[1] for p in pairs]
        mx, my = st.mean(xs), st.mean(ys)
        return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sum((x - mx) ** 2 for x in xs)

    all_pairs = [(len(r["title"]) + len(r["description"]), tokens[r["notice_id"]])
                 for r in rows if not r["truncated"]]
    ch_pairs = [(len(r["title"]) + len(r["description"]), tokens[r["notice_id"]])
                for r, _ in changed if not r["truncated"]]
    b_all, b_ch = slope(all_pairs), slope(ch_pairs)
    # per-token cost of list text specifically: actual tokens minus overhead
    # minus what the NON-list text should cost at the corpus slope, over list chars
    list_tpc = (sum(tokens[r["notice_id"]] - OVERHEAD
                    - b_all * (len(r["title"]) + len(s.text)) for r, s in changed)
                / sum(s.chars_removed for _, s in changed))

    print(f"strip rules {S.STRIP_RULES_SHA256[:16]}.. (frozen); question {q.sha256[:16]}.. unchanged")
    print(f"\nnotices changed: {len(changed):,} of {len(rows):,} coded; "
          f"runs {sum(len(s.runs) for _, s in changed):,}; "
          f"names removed {sum(s.names_removed for _, s in changed):,}; "
          f"chars removed {sum(s.chars_removed for _, s in changed):,}")
    print(f"non-name lines swallowed inside removed runs: {len(swallowed):,}"
          + (" - sample:" if swallowed else ""))
    for nid, line in swallowed[:15]:
        print(f"    {nid[:28]:28} | {line[:90]}")

    # before/after
    chosen = ["cb-309-38456671", "cb-282-89433549"]
    rest = sorted((r for r, _ in changed if r["notice_id"] not in chosen),
                  key=lambda r: -tokens[r["notice_id"]])
    chosen.append(rest[0]["notice_id"])
    by_id = {r["notice_id"]: (r, s) for r, s in changed}
    for nid in chosen:
        r, s = by_id[nid]
        print(f"\n=== {nid}  ({tokens[nid]:,} input tokens today)")
        print(f"  title: {' '.join(r['title'].split())[:110]}")
        print(f"  description {len(r['description']):,} -> {len(s.text):,} chars "
              f"({s.chars_removed:,} removed, {s.names_removed} names in {len(s.runs)} run(s))")
        for a, b, n in s.runs:
            print(f"  removed run of {n}: '{a[:60]}' ... '{b[:60]}'")
        lines = s.text.split("\n")
        at = next(i for i, l in enumerate(lines) if l.startswith("[invited-supplier list"))
        print("  after, around the cut:")
        for l in lines[max(0, at - 3):at + 3]:
            print(f"    | {l[:110]}")

    # A saving can never exceed what the notice actually cost beyond the fixed
    # overhead and its remaining text: the one truncated notice was only ever
    # sent 60,000 characters, so its list beyond the cap cost nothing to strip.
    def saving(r, s):
        spent = tokens[r["notice_id"]]
        floor = OVERHEAD + b_all * (len(r["title"]) + min(len(s.text), 60_000))
        return max(0.0, min(list_tpc * s.chars_removed, spent - floor))
    saved = sorted(saving(r, s) for r, s in changed)
    total_saved = sum(saved)
    run_tokens = sum(tokens[r["notice_id"]] - saving(r, s) for r, s in changed)
    top = sorted(rows, key=lambda r: -tokens[r["notice_id"]])[:max(1, len(rows) // 100)]
    touched = {r["notice_id"] for r, _ in changed}
    print(f"\nTOP 1% BY INPUT TOKENS ({len(top)} notices, >= {tokens[top[-1]['notice_id']]:,} "
          f"tokens): {sum(r['notice_id'] in touched for r in top)} carry a strippable list; "
          f"their tokens {sum(tokens[r['notice_id']] for r in top):,} of which list text "
          f"~{sum(saving(r, s) for r, s in changed if r['notice_id'] in {t['notice_id'] for t in top}):,.0f}")
    print(f"\nTOKEN SAVING (estimate - no call made)")
    print(f"  tokens/char: corpus slope {b_all:.4f}; slope within changed notices {b_ch:.4f}; "
          f"list text specifically {list_tpc:.4f}")
    print(f"  per changed notice: mean {st.mean(saved):,.0f}, median {st.median(saved):,.0f}, "
          f"p99 {saved[int(0.99 * (len(saved) - 1))]:,.0f}, max {saved[-1]:,.0f}")
    print(f"  across the corpus: {total_saved:,.0f} tokens "
          f"= {total_saved / 90_111_081:.1%} of the phase 1 spend "
          f"(${total_saved / 1e6 * E.PRICE_PER_MILLION_INPUT:.2f})")
    print(f"\nCOST OF A VARIANT B RUN: only the {len(changed):,} changed notices need a call "
          f"(an unchanged notice's request is byte-identical and hits the cache).")
    print(f"  ~{run_tokens:,.0f} input tokens = ${run_tokens / 1e6 * E.PRICE_PER_MILLION_INPUT:.2f} "
          f"at {E.PRICE_SOURCE}")
    print("  Requires first: a variant B record with its own pre-registered rule. Not run.")
    return 0


def cmd_budget(args) -> int:
    """
    ref-006 scoping, cache and feed only: split-half spread per threshold, and
    what the corpus becomes at each t.

    THE PROXY, stated. Per-t admit rates are measured on coded archive notices
    that pass exclusion/construction/jurisdiction. The gate will run on
    UNCODED feed notices, which Jev has never seen. The keyword branch is the
    calibration check: its rate on the archive proxy is printed beside its
    rate actually observed on the feed's uncoded notices.
    """
    import json
    import sqlite3
    from datetime import date
    from filter_audit import predicates as P
    from filter_audit import replay
    from . import sweep as S

    q, rows = _scored_rows()
    criteria = ingest.parse_profile(ingest.DEFAULT_PROFILE)
    conn = sqlite3.connect(f"file:{E.NOTICES_DB.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    pre = ("exclusion", "construction", "jurisdiction")
    stage = {s.name: s for s in P.STAGES}
    passing = []
    for r in rows:
        n = P.Notice.from_archive_row(conn.execute(
            "SELECT * FROM notices WHERE reference_number=?", (r["notice_id"],)).fetchone())
        if not any(stage[s].evaluate(n, criteria, None).drops for s in pre):
            passing.append(r)

    meta = json.loads((replay.FEED_CSV.parent / "tenders.csv.http.json").read_text())
    as_of = date.fromisoformat(meta["fetched_at"][:10])
    feed = {"total": 0, "uncoded": 0, "uncoded_at_relevance": 0, "uncoded_kw_admit": 0,
            "coded_at_relevance": 0, "coded_admit": 0}
    for n, _ in replay.iter_feed():
        feed["total"] += 1
        coded = bool(ingest.parse_unspsc_codes(n.unspsc))
        feed["uncoded"] += not coded
        if any(stage[s].evaluate(n, criteria, as_of).drops for s in ("closed",) + pre):
            continue
        rel = stage["relevance"].evaluate(n, criteria, as_of)
        if coded:
            feed["coded_at_relevance"] += 1
            feed["coded_admit"] += not rel.drops
        else:
            feed["uncoded_at_relevance"] += 1
            feed["uncoded_kw_admit"] += not rel.drops

    npass = len(passing)
    kw_rate = sum(r["kw_admit"] for r in passing) / npass
    feed_kw_rate = feed["uncoded_kw_admit"] / max(1, feed["uncoded_at_relevance"])
    today = feed["coded_admit"] + feed["uncoded_kw_admit"]
    print(f"FEED .cache/tenders.csv fetched {meta['fetched_at']} (as_of {as_of}):")
    print(f"  {feed['total']:,} notices; {feed['uncoded']:,} uncoded; reaching the relevance "
          f"gate: {feed['uncoded_at_relevance']:,} uncoded, {feed['coded_at_relevance']:,} coded")
    print(f"  today's corpus: {feed['coded_admit']:,} coded admits + "
          f"{feed['uncoded_kw_admit']:,} uncoded keyword admits = {today:,}")
    print(f"\nPROXY CHECK - keyword admit rate: archive coded proxy {kw_rate:.3f} "
          f"vs feed uncoded observed {feed_kw_rate:.3f} "
          f"(ratio {feed_kw_rate / kw_rate:.2f}). The Jev rates below carry the same "
          f"proxy and have no observed counterpart.")
    print(f"archive proxy population: {npass:,} coded notices passing {', '.join(pre)}")

    spread = {row["t"]: row for row in S.split_spread(rows)}
    print(f"\n{'t':>6} {'archive':>8} {'per 970':>8} {'feed':>6} {'corpus':>7} {'vs today':>9}"
          f"   {'recall':>6} {'prec':>6}   split-half spread B-A, 95% of 50 splits")
    print(f"{'':>6} {'rate':>8} {'uncoded':>8} {'uncod.':>6} {'total':>7} {'':>9}"
          f"   {'':>6} {'':>6}   recall | precision | admit rate")
    for t in S.THRESHOLDS:
        admit = [r for r in passing if r["p_profile"] >= t]
        rate = len(admit) / npass
        feed_admit = rate * feed["uncoded_at_relevance"]
        corpus = feed["coded_admit"] + feed_admit
        m = S._metrics(rows, lambda r, t=t: r["p_profile"] >= t)
        sp = spread[t]
        print(f"{t:6.3f} {rate:8.4f} {rate * 970:8.1f} {feed_admit:6.0f} {corpus:7.0f} "
              f"{corpus - today:+9.0f}   {m['recall']['value']:6.3f} {m['precision']['value']:6.3f}"
              f"   {sp['recall']['lo']:+.3f}..{sp['recall']['hi']:+.3f} | "
              f"{sp['precision']['lo']:+.3f}..{sp['precision']['hi']:+.3f} | "
              f"{sp['admit_rate']['lo']:+.4f}..{sp['admit_rate']['hi']:+.4f}")
    print("\nOPTIMISM when t is picked on half A to hit a recall target, scored on B (50 splits):")
    for target in (0.85, 0.90):
        p = S.pick_on_a(rows, target)
        if not p["splits"]:
            print(f"  target {target}: never reached on half A")
            continue
        print(f"  target {target}: mean A-minus-B recall {p['mean_optimism']:+.4f}; "
              f"half B misses the target in {p['b_below_target']:.0%} of splits; "
              f"t picked {p['t_picked']}")
    print("\n" + textwrap.fill(E.POPULATION_CAVEAT, 100))
    return 0


def _feed_uncoded_at_relevance():
    """The feed's uncoded notices that pass closed/exclusion/construction/
    jurisdiction, as of the feed's own download date, with keyword hits."""
    import json
    from datetime import date
    from filter_audit import predicates as P
    from filter_audit import replay
    from .comparator import keyword_hits
    from .state import ImputerState
    criteria = ingest.parse_profile(ingest.DEFAULT_PROFILE)
    meta = json.loads((replay.FEED_CSV.parent / "tenders.csv.http.json").read_text())
    as_of = date.fromisoformat(meta["fetched_at"][:10])
    stage = {s.name: s for s in P.STAGES}
    out = []
    for n, _ in replay.iter_feed():
        if ingest.parse_unspsc_codes(n.unspsc):
            continue
        if any(stage[s].evaluate(n, criteria, as_of).drops
               for s in ("closed", "exclusion", "construction", "jurisdiction")):
            continue
        state = ImputerState(title=n.title, description=n.description)
        out.append((n.notice_id, state, keyword_hits(state, criteria["competencies"])))
    return meta, out


def cmd_observe_feed(args) -> int:
    """
    Impute the feed's uncoded notices that reach the relevance gate - the
    population the ref-006 gate would serve - so the corpus table rests on an
    observation rather than the coded-archive proxy. Makes API calls (the
    ledger records them as `feed-observe`); cached, so a re-run is free.
    """
    from collections import Counter
    from . import sweep as S
    q = _gate()
    meta, items = _feed_uncoded_at_relevance()
    conn = cache.connect()
    before = cache.spend(conn).get("feed-observe", {"calls": 0, "input_tokens": 0})
    stats = E.impute([(nid, st) for nid, st, _ in items], q, "feed-observe",
                     JevClient(), conn, workers=4)
    after = cache.spend(conn)["feed-observe"]
    kinds = {o.key: o.kind for o in q.options}
    from .state import prepare
    rows = []
    for nid, st, kw in items:
        v = cache.get(conn, nid, prepare(st).content_sha256, E.MODEL, q.sha256)
        mass = sum(p for k, p in v["probabilities"].items() if kinds[k] == "profile")
        rows.append((nid, st, kw, v, mass))
    spent = after["input_tokens"] - before["input_tokens"]
    print(f"\nfeed {meta['fetched_at']}: {len(items)} uncoded notices at the relevance gate; "
          f"{stats['called']} called, {stats['cache_hits']} cached, {stats['errors']} errors")
    print(f"this run: {after['calls'] - before['calls']} calls, {spent:,} input tokens "
          f"= ${spent / 1e6 * E.PRICE_PER_MILLION_INPUT:.4f}")
    print(f"\nsources: {dict(Counter(ingest._source_system(nid) for nid, *_ in rows))}")
    print(f"keyword branch admits today: {sum(bool(kw) for _, _, kw, _, _ in rows)}")
    print(f"\n{'t':>6} {'Jev admits':>11} {'kw admits':>10} {'union':>6} {'Jev only':>9} {'kw only':>8}")
    for t in S.THRESHOLDS:
        j = {nid for nid, _, _, _, m in rows if m >= t}
        k = {nid for nid, _, kw, _, _ in rows if kw}
        print(f"{t:6.3f} {len(j):11} {len(k):10} {len(j | k):6} {len(j - k):9} {len(k - j):8}")
    print("\nper notice (for the human reader only - never shown to Claude's tools):")
    for nid, st, kw, v, mass in sorted(rows, key=lambda r: -r[4]):
        print(f"  {nid[:28]:28} mass={mass:.2f}  jev={v['choice'][:40]:40} "
              f"kw={','.join(kw) or '-':22} | {' '.join(st.title.split())[:70]}")
    return 0


GAP_FAMILIES = ("8010", "8016")


def cmd_profile_gap(args) -> int:
    """
    ref-005 scoping: coded notices rejected on their codes that carry a code
    in 8010 (other than the profile's 80101507) or 8016. Cache and archive
    only. The other active stages are evaluated through filter_audit's own
    predicates, so the count says how many would get past everything else.
    """
    import sqlite3
    from collections import Counter
    from filter_audit import predicates as P
    q, rows = _scored_rows()
    families = ingest.parse_profile(ingest.DEFAULT_PROFILE)["unspsc_families"]
    criteria = ingest.parse_profile(ingest.DEFAULT_PROFILE)

    def gap_codes(codes):
        return [c for c in codes if c[:4] in GAP_FAMILIES and not
                ingest.matches_unspsc_families({c}, families)]

    gap = [r for r in rows if not r["publisher_admit"] and gap_codes(r["codes"])]
    conn = sqlite3.connect(f"file:{E.NOTICES_DB.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    other_stage_drop = Counter()
    passes_other = 0
    for r in gap:
        row = conn.execute("SELECT * FROM notices WHERE reference_number=?",
                           (r["notice_id"],)).fetchone()
        n = P.Notice.from_archive_row(row)
        dropped = [s.name for s in P.STAGES
                   if s.active and s.name in ("exclusion", "construction", "jurisdiction")
                   and s.evaluate(n, criteria, None).drops]
        other_stage_drop.update(dropped)
        passes_other += not dropped
        r["_passes_other"] = not dropped

    fam = Counter(f for r in gap for f in {c[:4] for c in gap_codes(r["codes"])})
    l4 = Counter(c for r in gap for c in gap_codes(r["codes"]))
    print("Population: WS/cb coded notices. Rejected today at relevance (coded, no profile family),")
    print(f"carrying a code in {' or '.join(GAP_FAMILIES)} (excluding the profile's 80101507).")
    print(f"\nnotices: {len(gap):,}   by family: " + ", ".join(f"{k} {v:,}" for k, v in sorted(fam.items())))
    print(f"  8010 only {sum(1 for r in gap if {c[:4] for c in gap_codes(r['codes'])} == {'8010'}):,}; "
          f"8016 only {sum(1 for r in gap if {c[:4] for c in gap_codes(r['codes'])} == {'8016'}):,}; "
          f"both {sum(1 for r in gap if {c[:4] for c in gap_codes(r['codes'])} == {'8010', '8016'}):,}")
    print(f"  by source: " + ", ".join(f"{k} {v:,}" for k, v in Counter(r['source'] for r in gap).items()))
    print(f"  would also be dropped by another active stage: {dict(other_stage_drop)}; "
          f"pass exclusion, construction and jurisdiction: {passes_other:,}")
    print("  (closed is not evaluated: the archive is historical and every notice would fail it)")
    print(f"\ntop 15 gap codes (a notice can carry several):")
    ref = __import__("family_imputer.options", fromlist=["x"])
    reference = ref.load_reference()
    print(f"  {'code':10}{'notices':>8}{'Jev top':>9}{'mass>=.02':>10}  description")
    for code, n in l4.most_common(15):
        sub = [r for r in gap if code in r["codes"]]
        print(f"  {code:10}{n:8,}{sum(r['jev_admit'] for r in sub):9,}"
              f"{sum(r['p_profile'] >= 0.02 for r in sub):10,}  {ref.describe_code(code, reference)}")
    for label, sub in (("all gap notices", gap),
                       ("gap notices passing the other stages", [r for r in gap if r["_passes_other"]])):
        print(f"\nJev on {label} (n={len(sub):,}):")
        print(f"  admits by top choice   {sum(r['jev_admit'] for r in sub):6,}")
        for t in (0.02, 0.01):
            print(f"  admits by mass >= {t}  {sum(r['p_profile'] >= t for r in sub):6,}")
        print(f"  keyword branch would fire (production never asks) {sum(r['kw_admit'] for r in sub):,}")
        print("  Jev's top choice: " + ", ".join(
            f"{k.split(' (')[0][:34]} {v:,}" for k, v in Counter(r['choice'] for r in sub).most_common(6)))
    return 0


def cmd_label(args) -> int:
    rec = E.record_label(args.notice_id, args.kind, args.note or "",
                         labelled_by=args.labelled_by, why_unsure=args.why or "")
    print(f"recorded {rec['notice_id']} as {rec['kind']} "
          f"(labelled_by={rec['labelled_by']}) -> {E.LABELS_JSONL}")
    return 0


def cmd_ingest_labels(args) -> int:
    from pathlib import Path
    records = E.ingest_label_sheet(Path(args.path), args.labelled_by)
    print(f"recorded {len(records)} labels (labelled_by={args.labelled_by}) "
          f"from {records[0]['source']['path'] if records else args.path} "
          f"sha256 {records[0]['source']['sha256'][:16] if records else ''}.. "
          f"-> {E.LABELS_JSONL}")
    return cmd_labels(args)


def cmd_labels(args) -> int:
    table = E.label_table(E.load_labels())
    if not table:
        print("no labels recorded")
        return 0
    dirs = list(E.DIRECTION_TEXT)
    for who, kinds in table.items():
        total = sum(sum(c.values()) for c in kinds.values())
        print(f"\nlabelled_by={who}  (n={total}; never summed with another labeller)")
        print(f"  {'kind':20}{'Jev admits/pub didnt':>22}{'pub admits/Jev didnt':>22}{'total':>7}")
        for kind in E.LABEL_KINDS:
            c = kinds.get(kind, {})
            print(f"  {kind:20}{c.get(dirs[0], 0):22}{c.get(dirs[1], 0):22}"
                  f"{sum(c.values()):7}")
        col = [sum(kinds.get(k, {}).get(d, 0) for k in E.LABEL_KINDS) for d in dirs]
        print(f"  {'total':20}{col[0]:22}{col[1]:22}{total:7}")
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
    p.add_argument("--labelled-by", required=True, choices=E.LABELLERS,
                   help="who made this judgement; no default, on purpose")
    p.add_argument("--note")
    p.add_argument("--why", help="required for unsure: the candidate kinds and what the call turns on")
    p.set_defaults(fn=cmd_label)
    p = sub.add_parser("ingest-labels")
    p.add_argument("path")
    p.add_argument("--labelled-by", required=True, choices=E.LABELLERS)
    p.set_defaults(fn=cmd_ingest_labels)
    sub.add_parser("labels").set_defaults(fn=cmd_labels)
    sub.add_parser("ceiling").set_defaults(fn=cmd_ceiling)
    sub.add_parser("strip-preview").set_defaults(fn=cmd_strip_preview)
    sub.add_parser("profile-gap").set_defaults(fn=cmd_profile_gap)
    sub.add_parser("budget").set_defaults(fn=cmd_budget)
    sub.add_parser("observe-feed").set_defaults(fn=cmd_observe_feed)
    args = ap.parse_args(argv)
    try:
        sys.exit(args.fn(args))
    except (JevError, FrozenQuestionDrift, E.NotPreregistered, E.SampleDrift,
            ValueError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
