"""
Replay the tender filter, audit what it rejected, and evaluate proposed fixes.

The loop this exists to support:

    replay-filter          what did the filter decide, and at which gate
    sample-rejects         draw a review queue (returns an id and a size only)
    next                   the next item, BLINDED
    record-review          your decision, then the reveal in the same breath
    categorize             why the filter erred - post-reveal, on purpose
    promote-golden         turn a reviewed case into a permanent test
    evaluate-golden        precision and recall for one filter version
    compare-filters        two versions on the same entries, with the tradeoff
    evaluate-refinement    score a proposal that has code behind it

Usage:
    python scripts/filter_audit replay-filter --persist
    python scripts/filter_audit explain SSC-22-00019111:T
    python scripts/filter_audit verify-equivalence --source feed
    python scripts/filter_audit sample-rejects --branch uncoded_no_keyword --n 25
    python scripts/filter_audit next --queue q-20260820-00306
    python scripts/filter_audit record-review --item q-...-005 --decision ACCEPT
    python scripts/filter_audit categorize --item q-...-005 \\
        --category vocabulary_mismatch --evidence "Cyber Security" --explanation "..."
    python scripts/filter_audit compare-filters --candidate v-cyber-security-spacing
    python scripts/filter_audit evaluate-golden --variant v-url-stripped-keywords

Every verb prints JSON with --json; most print a readable report otherwise.
Errors go to stderr with a non-zero exit.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

_BRANCHES = ("coded_wrong_family", "coded_pass", "uncoded_no_keyword", "uncoded_pass")

_AS_OF_HELP = (
    "publication (default) judges each notice against its own publication_date "
    "- deterministic, and the only mode that asks whether a notice would have "
    "been admitted when it was live. A YYYY-MM-DD fixes one clock for the whole "
    "run. `none` skips the closed stage and records it as skipped, never passed."
)


def _as_of(value: str):
    if value in ("publication", "none"):
        return value
    datetime.strptime(value, "%Y-%m-%d")      # fail loudly on a bad date
    return value


def cmd_replay(args):
    from .replay import render_funnel, replay
    result = replay(source=args.source, as_of_spec=args.as_of,
                    profile_path=args.profile, sample=args.sample,
                    seed=args.seed, variant_id=args.variant,
                    persist=args.persist)
    if args.output and "error" not in result:
        Path(args.output).write_text(json.dumps(result, indent=2, default=str),
                                     encoding="utf-8")
        result["written_to"] = args.output
    return result, render_funnel(result)


def cmd_explain(args):
    """Production beside audit, on one notice. The demonstration verb."""
    import sqlite3

    import ingest

    from . import predicates as P
    from .replay import DEFAULT_PROFILE, NOTICES_DB

    criteria = ingest.parse_profile(Path(args.profile or DEFAULT_PROFILE))
    conn = sqlite3.connect(f"file:{NOTICES_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM notices WHERE reference_number=?",
                       (args.notice_id,)).fetchone()
    conn.close()
    if row is None:
        return {"error": f"{args.notice_id} is not in data/notices.db"}, None

    notice = P.Notice.from_archive_row(row)
    as_of = notice.publication_date if args.as_of == "publication" else (
        None if args.as_of == "none" else args.as_of)
    production = P.production_decision(notice, criteria, as_of)
    audit = P.audit_decision(notice, criteria, as_of)

    reached = {r.stage for r in production.evaluated}
    lines = [
        f"{notice.notice_id}  \"{notice.title[:72]}\"",
        f"  {str(notice.contracting_entity)[:48]} - published "
        f"{notice.publication_date} - as_of {as_of}",
        "",
        f"  {'stage':<15} {'PRODUCTION':<22} AUDIT",
    ]
    for result in audit.results:
        if result.stage in reached:
            prod = next(r.outcome for r in production.evaluated
                        if r.stage == result.stage)
            prod_text = prod.upper() if prod == "drop" else prod
            if result.stage == production.first_rejecting_stage:
                prod_text += "  <- first"
        else:
            prod_text = "not reached"
        lines.append(f"  {result.stage:<15} {prod_text:<22} {result.outcome}"
                     f"   {result.basis[:34]}")
        if result.stage == "relevance":
            detail = result.detail
            for key in ("branch", "has_codes", "family_result", "keyword_result",
                        "keyword_consulted_in_production"):
                if key in detail:
                    lines.append(f"  {'':<15} {'':<22}   {key:<32} {detail[key]}")
            evidence = result.evidence
            lines.append(f"  {'':<15} {'':<22}   {'expected_families':<32} "
                         f"{detail.get('expected_families')}")
            lines.append(f"  {'':<15} {'':<22}   {'codes':<32} "
                         f"{evidence.get('codes')}")
            lines.append(f"  {'':<15} {'':<22}   {'matched_keywords':<32} "
                         f"{evidence.get('matched_keywords')}")
    lines.append("")
    lines.append(f"  PRODUCTION: "
                 f"{'ADMIT' if production.admitted else 'REJECT at ' + str(production.first_rejecting_stage)}")
    active = [r for r, s in zip(audit.results, P.STAGES) if s.active]
    passing = sum(1 for r in active if r.outcome == "pass")
    lines.append(f"  AUDIT:      {passing} of {len(active)} active stages pass, "
                 f"evaluated independently")
    return {"production": production.to_dict(), "audit": audit.to_dict(),
            "as_of": str(as_of)}, "\n".join(lines)


def cmd_verify_equivalence(args):
    from .equivalence import render, verify
    as_of = date.fromisoformat(args.as_of_date) if args.as_of_date else None
    result = verify(source=args.source, as_of=as_of, profile_path=args.profile,
                    limit=args.limit)
    return result, render(result)


def cmd_sample_rejects(args):
    from .review import sample_rejects
    result = sample_rejects(run_id=args.run, strategy=args.strategy, n=args.n,
                            seed=args.seed, stage=args.stage, branch=args.branch,
                            include_admitted=args.include_admitted)
    text = None
    if "error" not in result:
        text = (f"queue {result['queue_id']} - {result['n']} items\n"
                f"  Composition is deliberately not reported: a single-stratum "
                f"queue would announce its own answer.\n"
                f"  Next: filter_audit next --queue {result['queue_id']}")
    return result, text


def cmd_next(args):
    from .review import next_item
    result = next_item(args.queue)
    if "error" in result or result.get("remaining") == 0:
        return result, None
    text = "\n".join([
        f"item {result['item_id']}   ({result['remaining']} left in queue)",
        "",
        f"  {result['title']}",
        f"  {result['contracting_entity']}"
        + (f"  (end user: {result['end_user']})" if result['end_user'] else ""),
        f"  {result['notice_type'] or '-'} | "
        f"{str(result['procurement_category']).replace(chr(10), ',') or '-'} | "
        f"unspsc {str(result['unspsc'])[:40] or '-'}",
        f"  published {result['publication_date']}  closes {result['closing_date']}",
        "",
        "  " + (result["description"] or "")[:900].replace("\n", " "),
        "",
        f"  {result['blinded']}",
        f"  Decide: filter_audit record-review --item {result['item_id']} "
        f"--decision ACCEPT|REJECT|UNCERTAIN",
    ])
    return result, text


def cmd_record_review(args):
    from .review import record_review
    result = record_review(args.item, args.decision, reviewer=args.reviewer)
    if "error" in result:
        return result, None
    relevance = result["relevance"]
    text = "\n".join([
        f"recorded {result['review_id']}  ({result['reference_number']})",
        "",
        "  REVEAL - withheld until now",
        f"    your decision        {result['your_decision']}",
        f"    production decision  {result['production_decision']}"
        + (f" at `{result['first_rejecting_stage']}`"
           if result["first_rejecting_stage"] else ""),
        f"    agreed               {result['agreed']}",
        f"    stratum              {result['stratum']}",
        f"    relevance            has_codes={relevance['has_codes']} "
        f"family={relevance['family_result']} keyword={relevance['keyword_result']}",
        "",
        f"  {result['next_step']}",
    ])
    return result, text


def cmd_reveal(args):
    from .review import reveal
    return reveal(args.item, force_unblind=args.force_unblind), None


def cmd_categorize(args):
    from .review import categorize
    return categorize(args.item, args.category, args.evidence,
                      args.explanation), None


def cmd_agreement(args):
    from .review import agreement
    return agreement(), None


def cmd_taxonomy(args):
    from .review import TAXONOMY_FILE, load_taxonomy
    if args.add:
        if not args.definition:
            return {"error": "--add requires --definition. A category without a "
                             "definition is a label, and labels do not count."}, None
        import yaml
        data = yaml.safe_load(TAXONOMY_FILE.read_text(encoding="utf-8"))
        if args.add in data["categories"]:
            return {"error": f"{args.add!r} already exists."}, None
        data["categories"][args.add] = {
            "definition": args.definition,
            "not": args.not_ or "TODO: name the nearest neighbour this is not.",
            "exemplar": None,
        }
        TAXONOMY_FILE.write_text(
            yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
            encoding="utf-8", newline="\n")
        return {"added": args.add}, None
    return {"categories": load_taxonomy()}, None


def cmd_evaluate_golden(args):
    from .golden import evaluate, render
    result = evaluate(variant_id=args.variant, profile_path=args.profile)
    text = render(result)
    if args.gate and "error" not in result:
        # THE GATE IS REGRESSION PROTECTION, NOT A BACKLOG ALARM.
        #
        # known_false_negative and known_false_positive entries are cases the
        # filter is KNOWN to get wrong and has not been fixed for yet - that is
        # what makes them worth recording. Gating on them would paint CI red
        # permanently and train everyone to ignore it, which costs more than it
        # buys. They are reported in the output either way.
        #
        # The deterministic strata are different: they were drawn before any
        # review, the current filter gets them right today, and a refinement
        # that breaks one has broken something real.
        anchors = ("clearly_relevant", "clearly_irrelevant")
        broken = [e for e in result["per_entry"]
                  if e.get("correct") is False and e["class"] in anchors]
        result["gate_pass"] = not broken
        result["gate_anchors"] = anchors
        result["gate_broken"] = broken
        backlog = [e for e in result["per_entry"]
                   if e.get("correct") is False and e["class"] not in anchors]
        text += (f"\n\n  GATE: {'pass' if not broken else 'FAIL'} "
                 f"({len(broken)} regression-anchor failures). "
                 f"{len(backlog)} known-unfixed case(s) reported but not gated - "
                 f"they are the backlog this audit exists to work through.")
    return result, text


def cmd_verify_golden(args):
    from .golden import verify
    return verify(), None


def cmd_compare(args):
    from .compare import compare, render
    result = compare(args.base, args.candidate, args.profile)
    return result, render(result)


def cmd_evaluate_refinement(args):
    from .compare import render
    from .refinement import append_evaluation, evaluate_refinement
    result = evaluate_refinement(args.id, args.profile)
    if "error" in result:
        return result, None
    text = render(result)
    if args.record:
        appended = append_evaluation(args.id, result)
        result["appended"] = appended
        text += f"\n\n  Appended to {appended.get('path')}"
    text += f"\n\n  {result['promotion_note']}"
    return result, text


def cmd_proposals(args):
    from .refinement import load_all, validate
    if args.validate:
        return validate(), None
    items = [{k: v for k, v in i.items() if not k.startswith("_")}
             for i in load_all()]
    return {"refinements": items}, None


def cmd_runs(args):
    from .replay import connect
    conn = connect()
    rows = [dict(r) for r in conn.execute(
        "SELECT run_id, filter_version_label, as_of_spec, source, corpus_rows, "
        "first_written_at FROM runs ORDER BY first_written_at DESC")]
    conn.close()
    return {"runs": rows}, None


def cmd_register_version(args):
    from .version import register
    return register(args.note, args.profile), None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--json", action="store_true",
                        help="Print the raw JSON instead of the report")
    parser.add_argument("--profile", type=Path, default=None,
                        help="Company profile (default: vault/profiles/my-company.md)")
    sub = parser.add_subparsers(dest="command", required=True)

    replay = sub.add_parser("replay-filter",
                            help="Replay the filter over a corpus and print the funnel")
    replay.add_argument("--source", choices=["archive", "feed"], default="archive")
    replay.add_argument("--as-of", type=_as_of, default="publication",
                        help=_AS_OF_HELP)
    replay.add_argument("--variant", help="Evaluate a candidate variant instead")
    replay.add_argument("--sample", type=int, help="Evaluate roughly N notices")
    replay.add_argument("--seed", type=int, help="Seed for --sample")
    replay.add_argument("--persist", action="store_true",
                        help="Write decisions to data/filter_audit.db")
    replay.add_argument("--output", help="Also write the funnel JSON here")
    replay.set_defaults(func=cmd_replay)

    explain = sub.add_parser("explain",
                             help="One notice: production beside audit")
    explain.add_argument("notice_id")
    explain.add_argument("--as-of", type=_as_of, default="publication",
                         help=_AS_OF_HELP)
    explain.set_defaults(func=cmd_explain)

    equiv = sub.add_parser(
        "verify-equivalence",
        help="Prove the audit path reproduces ingest.py exactly")
    equiv.add_argument("--source", choices=["feed", "archive"], default="feed")
    equiv.add_argument("--as-of-date", help="YYYY-MM-DD; pins both clocks")
    equiv.add_argument("--limit", type=int)
    equiv.set_defaults(func=cmd_verify_equivalence)

    sample = sub.add_parser("sample-rejects",
                            help="Build a blinded review queue")
    sample.add_argument("--run", help="Run id (default: most recent)")
    sample.add_argument("--strategy", default="random")
    sample.add_argument("--n", type=int, default=20)
    sample.add_argument("--seed", type=int)
    sample.add_argument("--stage", help="Only rejects whose first gate was this")
    sample.add_argument("--branch", choices=_BRANCHES,
                        help="Only this relevance branch")
    sample.add_argument("--include-admitted", action="store_true",
                        help="Mix admitted notices into the queue. Off by "
                             "default; on, it closes the residual gap that a "
                             "reject-only queue still tells you the verdict was "
                             "REJECT, and makes false positives discoverable by "
                             "the same effort.")
    sample.set_defaults(func=cmd_sample_rejects)

    nxt = sub.add_parser("next", help="Next undisposed item in a queue, blinded")
    nxt.add_argument("--queue", required=True)
    nxt.set_defaults(func=cmd_next)

    rec = sub.add_parser("record-review",
                         help="Record a blinded disposition; reveals in the same call")
    rec.add_argument("--item", required=True)
    rec.add_argument("--decision", required=True,
                     choices=["ACCEPT", "REJECT", "UNCERTAIN"])
    rec.add_argument("--reviewer", required=True, choices=["human", "assistant"],
                     help="Who made this judgement. REQUIRED and with no default: "
                          "the first three records this package wrote were "
                          "produced by an assistant and claimed to be human "
                          "because the argument defaulted. In a provenance log "
                          "the wrong answer must not be the one you get by "
                          "saying nothing.")
    rec.set_defaults(func=cmd_record_review)

    rev = sub.add_parser("reveal", help="Show the verdict (needs a disposition)")
    rev.add_argument("--item", required=True)
    rev.add_argument("--force-unblind", action="store_true",
                     help="Debugging only. RECORDS that it was used and marks "
                          "the queue compromised, excluding it from agreement "
                          "statistics.")
    rev.set_defaults(func=cmd_reveal)

    cat = sub.add_parser("categorize",
                         help="Attach a failure category (post-reveal)")
    cat.add_argument("--item", required=True)
    cat.add_argument("--category", required=True)
    cat.add_argument("--evidence", required=True)
    cat.add_argument("--explanation", required=True)
    cat.set_defaults(func=cmd_categorize)

    agree = sub.add_parser("agreement",
                           help="Blinded agreement rate by stratum")
    agree.set_defaults(func=cmd_agreement)

    tax = sub.add_parser("taxonomy", help="The failure taxonomy")
    tax.add_argument("--add", help="Add a category")
    tax.add_argument("--definition")
    tax.add_argument("--not", dest="not_",
                     help="What this category is NOT, and its nearest neighbour")
    tax.set_defaults(func=cmd_taxonomy)

    ev = sub.add_parser("evaluate-golden",
                        help="Precision and recall for one filter version")
    ev.add_argument("--variant")
    ev.add_argument("--gate", action="store_true",
                    help="Exit non-zero if any entry scores wrong")
    ev.set_defaults(func=cmd_evaluate_golden)

    vg = sub.add_parser("verify-golden",
                        help="Frozen rows against the archive, where available")
    vg.set_defaults(func=cmd_verify_golden)

    cmp_ = sub.add_parser("compare-filters",
                          help="Two versions on the same golden set")
    cmp_.add_argument("--base", help="Variant id (default: current predicates)")
    cmp_.add_argument("--candidate", help="Variant id (default: current predicates)")
    cmp_.set_defaults(func=cmd_compare)

    er = sub.add_parser("evaluate-refinement",
                        help="Score a refinement that has code behind it")
    er.add_argument("--id", required=True)
    er.add_argument("--record", action="store_true",
                    help="Append the result to the refinement file as a dated "
                         "section (the proposal itself is never edited)")
    er.set_defaults(func=cmd_evaluate_refinement)

    pr = sub.add_parser("proposals", help="List or validate refinement proposals")
    pr.add_argument("--validate", action="store_true")
    pr.set_defaults(func=cmd_proposals)

    runs = sub.add_parser("runs", help="Recorded replay runs")
    runs.set_defaults(func=cmd_runs)

    regv = sub.add_parser("register-version",
                          help="Append the current filter version to the registry")
    regv.add_argument("--note", required=True)
    regv.set_defaults(func=cmd_register_version)

    return parser


def main():
    args = build_parser().parse_args()
    result, text = args.func(args)
    if args.json or text is None:
        print(json.dumps(result, indent=2, default=str))
    else:
        print(text)
    if isinstance(result, dict):
        if "error" in result:
            sys.exit(1)
        if getattr(args, "gate", False) and result.get("gate_pass") is False:
            sys.exit(1)
