"""Talking to the Claude API, for the authoring assistant.

Three decisions worth knowing about before reading the code.

**The key never goes into this repository, and never into a template.** It is
read from the ``ANTHROPIC_API_KEY`` environment variable if one is set, and
otherwise from the database, where it is stored encrypted with a key derived
from ``SECRET_KEY``. That is not as good as a secrets manager — anyone holding
both the database file and ``SECRET_KEY`` can recover it — and the
administration page says so plainly rather than implying a safety it does not
have. The environment variable is the better option and takes precedence.

**Nothing the model writes is trusted.** The assistant does not create
questions. It produces a CSV in exactly the format a person would upload, and
that CSV goes through ``question_io.parse_questions_csv`` and the same review
screen as any other import. So a malformed, dishonest or merely odd answer
from the model cannot put anything into a bank that a hand-typed sheet could
not, and the instructor sees every question before it is saved.

**There is no SDK dependency.** This calls the HTTPS endpoint with urllib from
the standard library. One fewer package to install on a host where installing
packages is awkward, and urllib honours the ``HTTPS_PROXY`` environment
variable, which is how PythonAnywhere requires outbound traffic to go.

A note on hosting, because it will come up: a free PythonAnywhere account
cannot reach api.anthropic.com at all. Outbound traffic from a free account
goes through an allowlist that does not include it, so the assistant will
report that it cannot connect however correct the key is. A paid account
removes the allowlist but still requires the proxy, so set
``HTTPS_PROXY=http://proxy.server:3128`` there.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import urllib.error
import urllib.request

from flask import current_app

API_URL = "https://api.anthropic.com/v1/messages"
MODELS_URL = "https://api.anthropic.com/v1/models"
API_VERSION = "2023-06-01"

SETTING_KEY = "ai_api_key_encrypted"
SETTING_MODEL = "ai_model"
SETTING_ENABLED = "ai_enabled"

DEFAULT_MODEL = "claude-sonnet-4-5"
TIMEOUT_SECONDS = 120


class AIError(RuntimeError):
    """Something went wrong that the instructor should be told about."""


# --------------------------------------------------------------------------- #
# Keeping the key
# --------------------------------------------------------------------------- #

def _fernet():
    """A cipher derived from SECRET_KEY, or None if unavailable."""
    try:
        from cryptography.fernet import Fernet
    except Exception:                                   # noqa: BLE001
        return None
    secret = (current_app.config.get("SECRET_KEY") or "").encode("utf-8")
    if not secret:
        return None
    digest = hashlib.sha256(b"mechexam-ai-key-v1" + secret).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def store_key(raw: str) -> None:
    """Encrypt and save an API key, or clear it when given nothing."""
    from .models import Setting

    raw = (raw or "").strip()
    if not raw:
        Setting.put(SETTING_KEY, "")
        return

    cipher = _fernet()
    if cipher is None:
        raise AIError(
            "The key cannot be stored because the encryption library is not "
            "installed on this host. Set ANTHROPIC_API_KEY as an environment "
            "variable instead."
        )
    Setting.put(SETTING_KEY, cipher.encrypt(raw.encode("utf-8")).decode("ascii"))


def resolve_key() -> str:
    """The key to use: the environment first, the database second."""
    from .models import Setting

    from_env = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if from_env:
        return from_env

    stored = (Setting.get(SETTING_KEY, "") or "").strip()
    if not stored:
        return ""

    cipher = _fernet()
    if cipher is None:
        return ""
    try:
        return cipher.decrypt(stored.encode("ascii")).decode("utf-8")
    except Exception:                                   # noqa: BLE001
        # A changed SECRET_KEY makes the stored key unreadable. Saying so is
        # more use than a blank box that looks like nothing was ever set.
        raise AIError(
            "The saved API key cannot be read. This happens when SECRET_KEY "
            "changes after the key was saved — the key was encrypted with the "
            "old one. Paste the key in again."
        ) from None


def key_source() -> str:
    """Where the key is coming from, for the administration page to show."""
    from .models import Setting

    if (os.environ.get("ANTHROPIC_API_KEY") or "").strip():
        return "environment"
    if (Setting.get(SETTING_KEY, "") or "").strip():
        return "database"
    return "none"


def is_configured() -> bool:
    try:
        return bool(resolve_key())
    except AIError:
        return False


def is_enabled() -> bool:
    from .models import Setting

    return Setting.get_bool(SETTING_ENABLED) and is_configured()


def current_model() -> str:
    from .models import Setting

    return (Setting.get(SETTING_MODEL, "") or "").strip() or DEFAULT_MODEL


# --------------------------------------------------------------------------- #
# Calling the API
# --------------------------------------------------------------------------- #

def _request(url: str, payload: dict | None, key: str, method: str = "POST"):
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        url,
        data=body,
        method=method,
        headers={
            "content-type": "application/json",
            "x-api-key": key,
            "anthropic-version": API_VERSION,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return json.loads(response.read().decode("utf-8"))

    except urllib.error.HTTPError as error:
        detail = ""
        try:
            detail = json.loads(error.read().decode("utf-8"))["error"]["message"]
        except Exception:                               # noqa: BLE001
            detail = error.reason or ""
        if error.code == 401:
            raise AIError(
                "The API key was rejected. Check it in Administration ▸ "
                "Assistant."
            ) from None
        if error.code == 429:
            raise AIError(
                "The API is rate-limiting this account. Wait a minute and try "
                "again."
            ) from None
        if error.code == 400 and "model" in detail.lower():
            raise AIError(
                f"The configured model was refused: {detail} Choose another in "
                f"Administration ▸ Assistant."
            ) from None
        raise AIError(f"The API returned an error ({error.code}): {detail}") from None

    except (urllib.error.URLError, socket.timeout, TimeoutError) as error:
        raise AIError(
            "Could not reach the API. On a free PythonAnywhere account this is "
            "expected: outbound traffic is restricted to an allowlist that "
            "does not include it. On a paid account, set HTTPS_PROXY to "
            "http://proxy.server:3128 and reload the web app. "
            f"({getattr(error, 'reason', error)})"
        ) from None

    except json.JSONDecodeError:
        raise AIError("The API returned something that was not JSON.") from None


def list_models() -> list[dict]:
    """What this key may actually use.

    Asked rather than hard-coded: the model names change, and a list typed
    into this file would be wrong before long.
    """
    key = resolve_key()
    if not key:
        raise AIError("No API key is set.")
    payload = _request(MODELS_URL + "?limit=50", None, key, method="GET")
    return payload.get("data", []) or []


def chat(messages: list[dict], system: str = "", max_tokens: int = 4096) -> str:
    """One turn. Returns the assistant's text."""
    key = resolve_key()
    if not key:
        raise AIError(
            "No API key is set. An administrator sets one in Administration ▸ "
            "Assistant."
        )

    payload = {
        "model": current_model(),
        "max_tokens": max(256, min(max_tokens, 16000)),
        "messages": messages,
    }
    if system:
        payload["system"] = system

    data = _request(API_URL, payload, key)

    parts = []
    for block in data.get("content", []) or []:
        if block.get("type") == "text":
            parts.append(block.get("text", ""))
    text = "\n".join(p for p in parts if p).strip()

    if not text:
        raise AIError("The API replied with nothing usable.")
    if data.get("stop_reason") == "max_tokens":
        text += (
            "\n\n[The reply was cut off at the length limit. Ask for fewer "
            "questions at a time.]"
        )
    return text


# --------------------------------------------------------------------------- #
# What the assistant is for
# --------------------------------------------------------------------------- #

AUTHORING_SYSTEM = """\
You are helping a Mechanical Engineering lecturer at Western Philippines \
University prepare examinations. The students are Filipino undergraduates in \
BSME and allied engineering programmes.

Write questions that are correct engineering first and good assessment \
second. Use SI units. Where a numerical answer is wanted, work it out and give \
the value you actually computed — never a placeholder, never a rounded guess, \
and say the tolerance you think is fair. If a question as asked is ambiguous \
or has no single defensible answer, say so instead of inventing one.

Mathematics is written in LaTeX between single dollar signs, like \
$P_{1}V_{1} = P_{2}V_{2}$, because the portal renders it that way.

When the lecturer asks for questions, reply with a CSV code block and nothing \
else before it except at most two sentences. The CSV must have this header row \
exactly:

type,question,points,answer,option_a,option_b,option_c,option_d,option_e,option_f,tolerance,unit,topic,difficulty,explanation,parts

Column rules:
- type: mcq | truefalse | numeric | short | matching | essay | multipart
- answer: for mcq the letter of the correct option; for truefalse True or \
False; for numeric the value; for short the accepted answers separated by | ; \
leave blank for matching, essay and multipart
- matching: put the pairs in the option columns as "Left = Right"
- multipart: leave answer blank and put the sub-items in parts, separated by \
semicolons, each as "label | question | answer | type | points | tolerance"
- tolerance is a percentage; difficulty is easy, moderate or hard
- quote any field containing a comma

The lecturer reviews every question before any of it is saved, so give them \
your real reasoning in the explanation column rather than filler.
"""
