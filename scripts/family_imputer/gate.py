"""
The ingest's relevance gate for uncoded notices (ref-006). The one module the
product may import from this package.

THE CONTRACT
------------
`make_imputer(profile_families)` returns a callable. The ingest hands it the
uncoded notices that survived the closed, exclusion, construction and
jurisdiction gates, as (notice_id, title, description), and gets back a
GateRun: an Imputation per notice the model answered for, plus counts of every
notice it did not and why. It NEVER raises for an operational failure - a
missing key, a drifted question, an auth error, a timeout. Each of those
leaves the notice without an imputation, and predicates.stage_relevance then
decides it by keywords, exactly as before the gate existed.

Coded notices never reach this module. filter_tenders sends only uncoded rows,
and tests/test_family_imputer.py asserts a coded notice produces no call.

THE QUESTION is the committed frozen_question.json, checked against
QUESTION_SHA256 on load, not rebuilt from the PSPC reference file (CI does not
have it). If the profile's families no longer match the question's profile
options, the gate stands down: an imputed family the profile no longer lists
would be a verdict about a question nobody is asking.

THE CACHE is .cache/family_imputer_gate.db - separate from the evaluation cache,
and under .cache/ so CI restores it. Same schema (cache.py): verdicts keyed on
notice id, content hash, model and question hash, so a notice open for six
weeks is paid for once, plus a ledger of every call.

THE PROBABILITY STAYS HERE. The full distribution is in the gate cache, which
is the audit trail. The Imputation carries the summed mass to
stage_relevance for the admit decision and the audit record; the ingest writes
only the basis, family and model into the corpus. See ref-006.
"""
from __future__ import annotations

import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from filter_audit.predicates import IMPUTER_THRESHOLD, Imputation  # noqa: E402

from . import cache  # noqa: E402
from .client import (JevAuthError, JevClient, JevError, ModelMismatch,  # noqa: E402
                     load_api_key, validate_answer)
from .question import (MODEL, QUESTION_SHA256, FrozenQuestionDrift,  # noqa: E402
                       load_frozen_payload)
from .state import ImputerState, prepare  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
GATE_CACHE = PROJECT_ROOT / ".cache" / "family_imputer_gate.db"

# A daily feed brings a few dozen uncoded notices; ten minutes is generous and
# stops a degraded API from holding the ingest hostage. Notices not reached in
# time fall back to keywords and are counted as such.
TIME_BUDGET_S = 600


@dataclass
class GateRun:
    imputations: dict = field(default_factory=dict)   # notice_id -> Imputation
    fallback: Counter = field(default_factory=Counter)  # reason -> notices
    status: str = "ok"          # ok | disabled: <reason> | degraded: <reason>
    called: int = 0
    cached: int = 0
    input_tokens: int = 0
    model: str = MODEL
    threshold: float = IMPUTER_THRESHOLD
    question_sha256: str = QUESTION_SHA256

    @property
    def fallback_total(self) -> int:
        return sum(self.fallback.values())


def _stand_down(items, reason: str) -> GateRun:
    run = GateRun(status=f"disabled: {reason}")
    run.fallback[reason] = len(items)
    return run


def make_imputer(profile_families, *, key=None, client=None,
                 cache_path: Path = GATE_CACHE, max_retries: int = 2,
                 timeout: float = 30.0, time_budget_s: float = TIME_BUDGET_S):
    """Build the gate. Cheap; nothing is loaded or called until it is used."""
    families = [str(f).strip() for f in profile_families]

    def impute(items) -> GateRun:
        items = list(items)
        if not items:
            return GateRun()
        try:
            frozen = load_frozen_payload()
        except (FrozenQuestionDrift, OSError, ValueError) as exc:
            return _stand_down(items, f"question unavailable ({type(exc).__name__})")
        profile_opts = [o for o in frozen["options"] if o["kind"] == "profile"]
        if sorted(o["prefix"] for o in profile_opts) != sorted(families):
            return _stand_down(items, "profile families differ from the frozen question")
        api_key = key if key is not None else load_api_key()
        if client is None and not api_key:
            return _stand_down(items, "TYPESAFE_API_KEY not set")
        jev = client or JevClient(key=api_key, max_retries=max_retries, timeout=timeout)
        payload = frozen["payload"]
        keys = tuple(payload["criteria"])
        prefix_of = {o["key"]: o["prefix"] for o in profile_opts}

        run = GateRun()
        conn = cache.connect(cache_path)
        deadline = time.monotonic() + time_budget_s
        stop_reason = None
        try:
            for nid, title, description in items:
                if stop_reason:
                    run.fallback[stop_reason] += 1
                    continue
                prepared = prepare(ImputerState(title=title or "",
                                                description=description or ""))
                verdict = cache.get(conn, nid, prepared.content_sha256, MODEL,
                                    QUESTION_SHA256)
                if verdict is not None:
                    run.cached += 1
                else:
                    if time.monotonic() > deadline:
                        stop_reason = "time budget exhausted"
                        run.fallback[stop_reason] += 1
                        continue
                    run.called += 1
                    try:
                        body = jev.ask(prepared.payload, payload)
                        verdict = validate_answer(body, keys)
                    except JevAuthError:
                        cache.log_call(conn, "gate", nid, None, None, None, "auth_error")
                        stop_reason = "auth error"
                        run.fallback[stop_reason] += 1
                        continue
                    except ModelMismatch:
                        cache.log_call(conn, "gate", nid, None, None, None,
                                       "refused_model_mismatch")
                        stop_reason = "model mismatch"
                        run.fallback[stop_reason] += 1
                        continue
                    except (JevError, OSError, ValueError, KeyError) as exc:
                        cache.log_call(conn, "gate", nid, None, None, None, "error")
                        run.fallback[f"api error ({type(exc).__name__})"] += 1
                        continue
                    cache.put(conn, nid, prepared, QUESTION_SHA256, verdict)
                    cache.log_call(conn, "gate", nid, verdict["model"],
                                   verdict["input_tokens"], verdict["output_tokens"],
                                   "stored")
                    run.input_tokens += verdict["input_tokens"]
                probs = verdict["probabilities"]
                profile_probs = {k: probs.get(k, 0.0) for k in prefix_of}
                top = max(profile_probs, key=profile_probs.get)
                run.imputations[nid] = Imputation(
                    family=prefix_of[top], mass=sum(profile_probs.values()),
                    model=MODEL, question_sha256=QUESTION_SHA256)
        finally:
            conn.commit()
            conn.close()
        if run.fallback:
            run.status = "degraded: " + "; ".join(sorted(run.fallback))
        return run

    return impute
