"""
The Jev HTTP call. Plain `requests`, no SDK: one endpoint, one question type.

THE KEY. Read from the process environment, falling back to the gitignored
.env through python-dotenv. It is placed in the Authorization header and
nowhere else: never printed, never logged, never written, and scrubbed from
any error text before that text leaves this module - an API that echoes a bad
key back in its error body would otherwise put it on the terminal.

THE MODEL IS PINNED. The request names MODEL; the response's `model` field says
which version answered, and anything else raises ModelMismatch BEFORE the
caller can cache it. A verdict from an unpinned model cached under the pinned
version's key would be a mislabelled measurement.

THE ANSWER IS VALIDATED, not trusted. The choice must be one of the options,
and the probabilities must cover exactly the options. A malformed answer
raises; it is never coerced into a verdict.
"""
from __future__ import annotations

import os
import random
import time
from pathlib import Path

import requests

from .question import MODEL, QUESTION_ID

ENDPOINT = "https://api.typesafe.ai/v1/systemone"
ENV_VAR = "TYPESAFE_API_KEY"
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DOTENV = PROJECT_ROOT / ".env"

RETRY_STATUSES = (429, 500, 502, 503, 504, 529)


class JevError(RuntimeError):
    """A call failed. The message never contains the key."""


class ModelMismatch(JevError):
    """The response came from a model other than the pinned one."""


class JevAuthError(JevError):
    """401/403. No retry and no later call can succeed, so a run stops at once."""


def load_api_key() -> str | None:
    """Process env first, then .env. Returns None rather than raising."""
    key = os.environ.get(ENV_VAR)
    if not key and DOTENV.exists():
        from dotenv import dotenv_values
        key = dotenv_values(DOTENV).get(ENV_VAR)
    return key.strip() if key and key.strip() else None


def key_status() -> str:
    return "set" if load_api_key() else "missing"


def _scrub(text: str, key: str) -> str:
    text = str(text)
    return text.replace(key, "[redacted]") if key else text


def validate_answer(body: dict, option_keys: tuple, model: str = MODEL) -> dict:
    """Refuse anything but a well-formed choice from the pinned model."""
    answered_by = body.get("model")
    if answered_by != model:
        raise ModelMismatch(
            f"response came from {answered_by!r}, pinned model is {model!r}; "
            f"refusing to record it")
    answer = (body.get("answers") or {}).get(QUESTION_ID)
    if not answer or answer.get("type") != "choice":
        raise JevError(f"no choice answer for {QUESTION_ID!r} in response")
    choice = answer.get("choice")
    probs = answer.get("probabilities") or {}
    if choice not in option_keys:
        raise JevError(f"choice {choice!r} is not one of the options")
    if set(probs) != set(option_keys):
        raise JevError("probabilities do not cover exactly the options")
    usage = body.get("usage") or {}
    if "input_tokens" not in usage:
        raise JevError("response carries no usage.input_tokens; cost would be unaccountable")
    return {
        "model": answered_by,
        "choice": choice,
        "probabilities": {k: float(v) for k, v in probs.items()},
        "confidence": answer.get("confidence"),
        "input_tokens": int(usage["input_tokens"]),
        "output_tokens": int(usage.get("output_tokens") or 0),
    }


class JevClient:
    def __init__(self, key: str | None = None, model: str = MODEL,
                 session=None, timeout: float = 90.0, max_retries: int = 7):
        self._key = key if key is not None else load_api_key()
        if not self._key:
            raise JevError(f"{ENV_VAR} is not set in the environment or in .env")
        self.model = model
        self._session = session or requests.Session()
        self.timeout = timeout
        self.max_retries = max_retries

    def ask(self, state: dict, question_payload: dict) -> dict:
        """
        One Choice over one state. Returns the raw response body.

        Retries with jittered exponential backoff on rate limiting, overload
        and 5xx; raises on anything else, including 401 and 422.
        """
        body = {"state": state, "model": self.model,
                "questions": {QUESTION_ID: question_payload}}
        headers = {"Authorization": f"Bearer {self._key}",
                   "Content-Type": "application/json"}
        delay = 2.0
        for attempt in range(self.max_retries + 1):
            try:
                resp = self._session.post(ENDPOINT, json=body, headers=headers,
                                          timeout=self.timeout)
            except requests.RequestException as exc:
                if attempt == self.max_retries:
                    raise JevError(_scrub(f"transport error: {type(exc).__name__}: {exc}",
                                          self._key)) from None
            else:
                if resp.status_code == 200:
                    return resp.json()
                if resp.status_code in (401, 403):
                    raise JevAuthError(_scrub(
                        f"HTTP {resp.status_code}: {resp.text[:300]}", self._key))
                if resp.status_code not in RETRY_STATUSES or attempt == self.max_retries:
                    raise JevError(_scrub(
                        f"HTTP {resp.status_code}: {resp.text[:300]}", self._key))
            time.sleep(delay + random.uniform(0, delay / 2))
            delay = min(delay * 2, 60.0)
        raise JevError("retries exhausted")  # unreachable; loop raises first
