"""A stand-in for the official `claude` binary, for deterministic tests.

It speaks the subset of the real CLI contract that Rinari's transport uses —
`--version`, `auth status --json`, and `--print --output-format stream-json`
— and can be told to misbehave (hang, emit a second generation, exit empty)
through environment variables, so the transport's guards are exercised
without an Anthropic account.

Verified against Claude Code 2.1.286; see docs/providers/claude-subscription.md.
"""

from __future__ import annotations

import json
import os
import sys
import time

# Test knobs. Each one reproduces a failure the real CLI has shown.
ENV_AUTH = "FAKE_CLAUDE_AUTH"  # subscription | console | logged_out | broken
ENV_MODE = "FAKE_CLAUDE_MODE"  # ok | double | empty | hang | error | rate_limit
ENV_TEXT = "FAKE_CLAUDE_TEXT"
ENV_RECORD = "FAKE_CLAUDE_RECORD"  # path: dump argv + env + stdin for assertions
ENV_VERSION = "FAKE_CLAUDE_VERSION"
ENV_THINKING = "FAKE_CLAUDE_THINKING"
ENV_ECHO = "FAKE_CLAUDE_ECHO"  # reply names the line it answers


def _emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def _stream(event: dict) -> None:
    """One Anthropic stream event, as the CLI wraps it in print mode."""
    _emit({"type": "stream_event", "event": event})


def _auth_payload() -> dict:
    mode = os.environ.get(ENV_AUTH, "subscription")
    if mode == "logged_out":
        return {"loggedIn": False, "authMethod": "none", "apiProvider": "firstParty"}
    if mode == "console":
        # Signed in, but against API billing: Rinari must refuse this.
        return {
            "loggedIn": True,
            "authMethod": "console",
            "apiProvider": "firstParty",
            "subscriptionType": None,
        }
    if mode == "bedrock":
        return {"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "bedrock"}
    if mode == "broken":
        return {}
    return {
        "loggedIn": True,
        "authMethod": "claude.ai",
        "apiProvider": "firstParty",
        "subscriptionType": "pro",
        # A field the real CLI also returns; diagnostics must not echo it.
        "configDirectory": "/home/someone/.claude",
    }


def _system_prompt(argv: list[str]) -> str | None:
    """The real CLI reads the prompt from a file; the record keeps its text.

    The transport deletes the request directory when the turn ends, so a test
    can only see the prompt from inside the child.
    """
    if "--system-prompt-file" not in argv:
        return None
    path = argv[argv.index("--system-prompt-file") + 1]
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return None


def _record(argv: list[str], stdin_text: str) -> None:
    path = os.environ.get(ENV_RECORD)
    if not path:
        return
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(
            {
                "argv": argv,
                "stdin": stdin_text,
                "system_prompt": _system_prompt(argv),
                "cwd": os.getcwd(),
                "env": {
                    k: v for k, v in os.environ.items() if k.startswith(("ANTHROPIC", "CLAUDE"))
                },
            },
            handle,
        )


MESSAGE_ID = "msg_fake_first_generation"


def _assistant(text: str, message_id: str = MESSAGE_ID, *, block: str = "text") -> dict:
    """One assistant event carries ONE content block, like the real CLI.

    Verified against 2.1.286: a reply with thinking and text arrives as two
    `assistant` events that share one message id. A second generation is a
    different id.
    """
    content = (
        {"type": "thinking", "thinking": text, "signature": "sig-abc"}
        if block == "thinking"
        else {"type": "text", "text": text}
    )
    return {
        "type": "assistant",
        "message": {
            "id": message_id,
            "role": "assistant",
            "model": "claude-sonnet-4-6-20260219",
            "stop_reason": None,
            "content": [content],
        },
    }


# The account picker the real CLI answers `initialize` with (2.1.286), cut to
# the cases that matter: a model with every level, one without `xhigh`, Haiku
# with no `supportsEffort` field at all, and the `default` pointer.
PICKER = [
    {
        "value": "default",
        "resolvedModel": "claude-opus-5-5",
        "displayName": "Default (recommended)",
        "supportsEffort": True,
        "supportedEffortLevels": ["low", "medium", "high", "xhigh", "max"],
    },
    {
        "value": "opus",
        "resolvedModel": "claude-opus-5-5",
        "displayName": "Opus 5.5",
        "description": "For complex work and everyday tasks",
        "supportsEffort": True,
        "supportedEffortLevels": ["low", "medium", "high", "xhigh", "max"],
        "supportsAdaptiveThinking": True,
    },
    {
        "value": "claude-opus-4-6",
        "resolvedModel": "claude-opus-4-6",
        "displayName": "Opus 4.6",
        "supportsEffort": True,
        "supportedEffortLevels": ["low", "medium", "high", "max"],
    },
    {
        "value": "haiku",
        "resolvedModel": "claude-haiku-4-5-20251001",
        "displayName": "Haiku 4.5",
        "description": "Fastest for quick answers",
    },
]
ENV_NO_PICKER = "FAKE_CLAUDE_NO_PICKER"
ENV_API_KEY_SOURCE = "FAKE_CLAUDE_API_KEY_SOURCE"


def _control_initialize(stdin_text: str) -> bool:
    """Answer an SDK `initialize` without generating anything, like the real CLI."""
    for line in stdin_text.splitlines():
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if message.get("type") == "control_request" and (
            (message.get("request") or {}).get("subtype") == "initialize"
        ):
            if os.environ.get(ENV_NO_PICKER):
                return True  # an older CLI: no answer, just exit
            _emit(
                {
                    "type": "control_response",
                    "response": {
                        "subtype": "success",
                        "request_id": message.get("request_id"),
                        "response": {"models": PICKER, "account": {"email": "x@example.com"}},
                    },
                }
            )
            return True
    return False


def _print_mode(argv: list[str]) -> int:
    stdin_text = "" if sys.stdin is None else sys.stdin.read()
    if _control_initialize(stdin_text):
        return 0
    _record(argv, stdin_text)
    mode = os.environ.get(ENV_MODE, "ok")
    text = os.environ.get(ENV_TEXT, "Hola desde el CLI falso.")

    if mode == "hang":
        time.sleep(600)
        return 0
    if mode == "empty":
        return 0  # exit 0 with no output: the print-mode regression of section 53
    if mode == "usage_credits":
        # Verified against 2.1.286 with a model the plan does not cover: a
        # synthetic assistant notice, then a result whose subtype says
        # success while is_error and api_error_status say otherwise.
        notice = "You are out of usage credits. Switch to another model."
        _emit(_assistant(notice, "4b6f0a1e-synthetic"))
        _emit(
            {
                "type": "result",
                "subtype": "success",
                "is_error": True,
                "api_error_status": 429,
                "result": notice,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            }
        )
        return 1
    if mode == "error":
        _emit({"type": "result", "subtype": "error_during_execution", "result": "boom"})
        return 1

    _emit(
        {
            "type": "system",
            "subtype": "init",
            "model": "claude-sonnet-4-6-20260219",
            # "none" means OAuth: the subscription. Anything else names the
            # API credential the real CLI picked.
            **(
                {}
                if os.environ.get(ENV_API_KEY_SOURCE) == "<absent>"
                else {"apiKeySource": os.environ.get(ENV_API_KEY_SOURCE, "none")}
            ),
        }
    )
    turns = _user_turns(stdin_text) or [""]
    for number, turn_text in enumerate(turns):
        message_id = MESSAGE_ID if number == 0 else f"msg_fake_turn_{number}"
        reply = f"respuesta a: {turn_text[-60:]}" if os.environ.get(ENV_ECHO) else text
        status = _generation(reply, message_id, mode)
        if status is not None:
            return status
    return 0


def _user_turns(stdin_text: str) -> list[str]:
    """The text of each `user` line, which the real CLI answers one by one."""
    turns = []
    for line in stdin_text.splitlines():
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if message.get("type") != "user":
            continue
        content = (message.get("message") or {}).get("content")
        if isinstance(content, str):
            turns.append(content)
        elif isinstance(content, list):
            turns.append("".join(b.get("text", "") for b in content if isinstance(b, dict)))
    return turns


def _generation(text: str, message_id: str, mode: str) -> int | None:
    """One generation with its own `result`, as the real CLI emits per turn."""
    if os.environ.get(ENV_THINKING):
        # Extended thinking arrives as the same Anthropic blocks the HTTP API
        # sends, only wrapped in `stream_event`.
        _stream(
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "thinking", "thinking": ""},
            }
        )
        _stream(
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "thinking_delta", "thinking": os.environ[ENV_THINKING]},
            }
        )
        _stream(
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "signature_delta", "signature": "sig-abc"},
            }
        )
    _stream(
        {
            "type": "content_block_start",
            "index": 1,
            "content_block": {"type": "text", "text": ""},
        }
    )
    for chunk in _chunks(text):
        _stream(
            {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "text_delta", "text": chunk},
            }
        )
    if os.environ.get(ENV_THINKING):
        # Thinking first, as its own event, with the same id as the text.
        _emit(_assistant(os.environ[ENV_THINKING], message_id, block="thinking"))
    _emit(_assistant(text, message_id))
    if mode == "double":
        # A second generation for one request: Rinari must reject it locally.
        _emit(_assistant("SEGUNDA GENERACION", "msg_fake_second_generation"))
    if mode == "rate_limit":
        _emit({"type": "result", "subtype": "error_rate_limit", "result": "rate limit reached"})
        return 1
    _emit(
        {
            "type": "result",
            "subtype": "success",
            "result": text,
            "usage": {"input_tokens": 11, "output_tokens": 7, "cache_read_input_tokens": 3},
        }
    )
    return None


def _chunks(text: str) -> list[str]:
    size = max(1, len(text) // 3)
    return [text[i : i + size] for i in range(0, len(text), size)] or [""]


def main(argv: list[str]) -> int:
    if "--version" in argv:
        print(os.environ.get(ENV_VERSION, "2.1.286 (Claude Code)"))
        return 0
    if argv[:2] == ["auth", "status"]:
        print(json.dumps(_auth_payload(), indent=2))
        return 0
    if argv[:1] == ["auth"]:
        return 0
    if "-p" in argv or "--print" in argv:
        return _print_mode(argv)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
