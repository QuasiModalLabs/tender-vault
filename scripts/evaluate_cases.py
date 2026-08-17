"""
Run the blinded evaluation over the case bundles and record predictions.

ISOLATION IS STRUCTURAL, NOT REQUESTED. The evaluator runs with its working
directory set to a scratch folder containing one file — the bundle — and with
every tool disallowed. It cannot open notices.db, cannot read truth.json, and
cannot look up how the department's procurement actually went, because it has no
mechanism to do any of those things. A prompt that merely ASKS a model not to
use its tools is not a control.

WHAT ISOLATION CANNOT FIX, AND THE HONEST NAME FOR IT. A model's pretraining
runs past every T0 in this experiment and past most outcome dates. It may simply
know that a department ran a competition in 2024. No prompt can remove that, and
claiming a "knowledge cutoff" enforced by instruction would be false. This is why
the Z arm exists: it asks for a decision with NO supplied evidence, so whatever
the model scores there is the ceiling of what prior knowledge and base rates buy
it. Every A/B/C/D result is read against Z, and the whole model-evaluated run is
labelled EXPLORATORY for this reason.

The bundles and the response schema are deliberately plain markdown so a human
analyst can run the identical protocol on the identical artifacts. The human
version is the stronger evidence about the product thesis; this one is a fast,
repeatable proxy for validating the machinery.

Predictions are appended to a JSONL file and never rewritten. Nothing here opens
truth.json — case_metrics.py does that, after the predictions are hashed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CASES_DIR = PROJECT_ROOT / "data" / "cases"
PREDICTIONS = CASES_DIR / "predictions.jsonl"

CONDITIONS = ("Z", "A", "B", "C", "D")

# Nothing about finding an opportunity, nothing about what the file is for, and
# no hint that cases come in matched pairs or that half are positive. The
# question in the bundle is the whole instruction.
SYSTEM_PROMPT = (
    "You are assessing a Government of Canada department and capability from a "
    "dossier of public evidence. Answer only from the evidence in the message. "
    "Follow the response format in the dossier exactly, using the given headings."
)


def _decision(text: str) -> str:
    m = re.search(r"\*{0,2}Decision:?\*{0,2}\s*:?\s*(DO NOT INVESTIGATE|INVESTIGATE)",
                  text, re.I)
    if m:
        return "DO_NOT_INVESTIGATE" if m.group(1).upper().startswith("DO NOT") else "INVESTIGATE"
    # Fall back to whichever appears first, so a reworded heading still parses.
    hits = [(text.upper().find(k), v) for k, v in
            (("DO NOT INVESTIGATE", "DO_NOT_INVESTIGATE"), ("INVESTIGATE", "INVESTIGATE"))
            if text.upper().find(k) >= 0]
    return min(hits)[1] if hits else "UNPARSED"


def _confidence(text: str) -> int | None:
    m = re.search(r"\*{0,2}Confidence:?\*{0,2}\s*:?\s*(\d{1,3})", text, re.I)
    if not m:
        return None
    v = int(m.group(1))
    return v if 0 <= v <= 100 else None


def _section(text: str, name: str) -> str:
    m = re.search(rf"\*{{0,2}}{name}:?\*{{0,2}}\s*:?\s*(.+?)(?=\n\*{{0,2}}[A-Z][a-z]+[a-z ]*:|\Z)",
                  text, re.S | re.I)
    return m.group(1).strip()[:1500] if m else ""


def run_cli(bundle: Path, model: str, timeout: int) -> str:
    """
    One bundle, one fresh `claude -p` process, deaf to everything but the bundle.

    FOUR THINGS HAD TO BE TRUE, and the first version got two of them wrong.

    --system-prompt REPLACES the default rather than appending to it. With
    --append-system-prompt the evaluator is still Claude Code: it inherits the
    agent framing, the memory-file instructions, and a per-machine block naming
    the working directory and the enclosing git repository. The first run spent
    its answer reporting that the temp folder was empty and that the home
    directory is a git repo with no commits. That is not an evaluator, it is an
    assistant reading its own environment.

    THE BUNDLE ARRIVES ON STDIN, not as an argv positional. Passed as an
    argument on Windows it hit the command-line length limit and the quoting
    rules, and the model received a fragment — it answered about the string
    "CASE-003" because that is all it was given.

    MCP servers are cleared, so no connector can reach a calendar or a drive.

    The working directory is a fresh empty temp folder and every tool is
    disallowed, which is the belt to the braces above: with no default system
    prompt there is no tool guidance either, but a model that improvised a tool
    call would still find nothing to read.
    """
    # shutil.which, not the bare name: on Windows the npm shim is claude.CMD and
    # subprocess without shell=True will not apply PATHEXT to find it. shell=True
    # would find it and then re-parse the argument list, which is how the bundle
    # got mangled the first time.
    exe = shutil.which("claude")
    if not exe:
        raise RuntimeError("claude CLI not found on PATH; use --backend api instead")
    workdir = Path(tempfile.mkdtemp(prefix="caseeval-"))
    try:
        cmd = [
            exe, "-p",
            "--model", model,
            "--system-prompt", SYSTEM_PROMPT,
            "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
            "--no-session-persistence",
            "--disallowedTools", "Bash", "Read", "Write", "Edit", "Glob", "Grep",
            "WebFetch", "WebSearch", "Task", "NotebookEdit",
        ]
        proc = subprocess.run(
            cmd, cwd=workdir, input=bundle.read_text(encoding="utf-8"),
            capture_output=True, text=True, encoding="utf-8", timeout=timeout)
        if proc.returncode != 0:
            raise RuntimeError(f"claude exited {proc.returncode}: {proc.stderr[:400]}")
        out = proc.stdout.strip()
        if not out:
            raise RuntimeError("claude returned empty output")
        return out
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def run_api(bundle: Path, model: str, timeout: int) -> str:
    import anthropic
    client = anthropic.Anthropic(timeout=timeout)
    msg = client.messages.create(
        model=model, max_tokens=1500, system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": bundle.read_text(encoding="utf-8")}])
    return "".join(b.text for b in msg.content if b.type == "text").strip()


def already_done() -> set:
    if not PREDICTIONS.exists():
        return set()
    done = set()
    for line in PREDICTIONS.read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = json.loads(line)
            done.add((r["case_id"], r["condition"]))
    return done


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", default="claude-sonnet-5")
    ap.add_argument("--backend", choices=("cli", "api"), default="cli")
    ap.add_argument("--conditions", default=",".join(CONDITIONS))
    ap.add_argument("--cases", default="", help="Comma-separated case ids; default all")
    ap.add_argument("--timeout", type=int, default=180)
    ap.add_argument("--limit", type=int, default=0, help="Stop after N evaluations")
    ap.add_argument("--seal", action="store_true",
                    help="Hash the prediction file and stop. Run before revealing truth.")
    args = ap.parse_args()

    if args.seal:
        digest = hashlib.sha256(PREDICTIONS.read_bytes()).hexdigest()
        n = sum(1 for line in PREDICTIONS.read_text(encoding="utf-8").splitlines() if line.strip())
        (CASES_DIR / "predictions.sha256").write_text(
            f"{digest}  predictions.jsonl  {n} predictions\n", encoding="utf-8")
        print(f"Sealed {n} predictions\nsha256 {digest}")
        return 0

    cases = json.loads((CASES_DIR / "cases.json").read_text(encoding="utf-8"))["cases"]
    wanted_cases = set(args.cases.split(",")) if args.cases else {c["case_id"] for c in cases}
    conditions = [c for c in args.conditions.split(",") if c in CONDITIONS]
    done = already_done()

    todo = [(c["case_id"], cond) for c in cases for cond in conditions
            if c["case_id"] in wanted_cases and (c["case_id"], cond) not in done]
    if args.limit:
        todo = todo[:args.limit]
    print(f"{len(todo)} evaluation(s) to run ({len(done)} already recorded), "
          f"model {args.model}, backend {args.backend}")

    runner = run_cli if args.backend == "cli" else run_api
    PREDICTIONS.parent.mkdir(parents=True, exist_ok=True)
    failures = 0
    for i, (case_id, cond) in enumerate(todo, 1):
        bundle = CASES_DIR / "bundles" / f"{case_id}-{cond}.md"
        started = time.time()
        try:
            text = runner(bundle, args.model, args.timeout)
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"  [{i}/{len(todo)}] {case_id}-{cond} FAILED: {type(exc).__name__}: {exc}")
            continue
        record = {
            "case_id": case_id, "condition": cond, "model": args.model,
            "backend": args.backend,
            "bundle_sha256": hashlib.sha256(bundle.read_bytes()).hexdigest()[:16],
            "decision": _decision(text),
            "confidence": _confidence(text),
            "earliest_window": _section(text, "Earliest plausible procurement window"),
            "supporting": _section(text, "Supporting evidence"),
            "contradictory": _section(text, "Contradictory evidence"),
            "unknowns": _section(text, "Unknowns"),
            "reasoning": _section(text, "Reasoning"),
            "raw": text,
            "elapsed_s": round(time.time() - started, 1),
        }
        with open(PREDICTIONS, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
        print(f"  [{i}/{len(todo)}] {case_id}-{cond}: {record['decision']} "
              f"@{record['confidence']} ({record['elapsed_s']}s)")
    print(f"\nDone. {failures} failure(s). Predictions at {PREDICTIONS}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
