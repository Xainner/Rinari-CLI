"""Claude CLI transport: binary discovery, auth guard, isolation, streaming.

Everything runs against the fake CLI in tests/fixtures, through a real child
process, so the Windows `.cmd` shim, the process group and the stream parsing
are exercised rather than mocked.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import time
from pathlib import Path

import pytest

from rinari.providers import claude_cli
from rinari.providers.adapters.claude_subscription import ClaudeSubscriptionAdapter
from rinari.providers.claude_cli import (
    BILLING_ENV_VARS,
    STATE_CONNECTED,
    STATE_LOGGED_OUT,
    STATE_MISSING_CLI,
    STATE_NON_SUBSCRIPTION_AUTH,
    ClaudeCliRuntime,
    ClaudeCliStream,
    ClaudeRunRequest,
)
from rinari.providers.errors import ProviderError, ProviderErrorCode

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "fake_claude.py"
WINDOWS = os.name == "nt"
# A child still needs the OS loader paths on Windows; nothing else is inherited.
_BASE_ENV = {
    "PATH": os.environ.get("PATH", ""),
    "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
}


def fake_cli(tmp_path: Path) -> Path:
    """A real executable wrapper, so resolution and spawning are not faked.

    On Windows this is a `.cmd` shim exactly like the one npm installs, which
    is the case section 55 calls out as first class.
    """
    if WINDOWS:
        path = tmp_path / "claude.cmd"
        path.write_text(f'@echo off\r\n"{sys.executable}" "{FIXTURE}" %*\r\n', encoding="utf-8")
        return path
    path = tmp_path / "claude"
    path.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FIXTURE}" "$@"\n', encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


def runtime(tmp_path: Path, **env: str) -> ClaudeCliRuntime:
    base = dict(_BASE_ENV)
    base.update(env)
    return ClaudeCliRuntime(
        command_override=str(fake_cli(tmp_path)), env=base, home=tmp_path / "home"
    )


# -- binary discovery ------------------------------------------------------


def test_missing_binary_is_reported_not_guessed(tmp_path):
    # `home` is isolated so a real install on the test machine cannot make
    # this pass or fail by accident.
    empty = ClaudeCliRuntime(env={"PATH": str(tmp_path)}, home=tmp_path / "home")
    assert empty.resolve() is None
    status = empty.auth_status()
    assert status.state == STATE_MISSING_CLI
    assert status.safe_for_subscription is False


def test_env_override_resolves_before_path(tmp_path):
    binary = fake_cli(tmp_path)
    found = ClaudeCliRuntime(
        env={"PATH": "", "RINARI_CLAUDE_COMMAND": str(binary)}, home=tmp_path / "home"
    ).resolve()
    assert found is not None
    assert found.source == "env"
    assert Path(found.path) == binary


def test_version_is_parsed_from_the_real_child(tmp_path):
    version = runtime(tmp_path).version()
    assert version.raw.startswith("2.1.286")
    assert version.parts == (2, 1, 286)
    assert version.supported is True


def test_an_older_cli_is_not_supported(tmp_path):
    version = runtime(tmp_path, FAKE_CLAUDE_VERSION="2.0.9 (Claude Code)").version()
    assert version.parts == (2, 0, 9)
    assert version.supported is False


# -- auth guard ------------------------------------------------------------


def test_subscription_auth_is_the_only_connected_state(tmp_path):
    status = runtime(tmp_path, FAKE_CLAUDE_AUTH="subscription").auth_status()
    assert status.state == STATE_CONNECTED
    assert status.safe_for_subscription is True
    assert status.subscription_type == "pro"


@pytest.mark.parametrize(
    ("mode", "state"),
    [
        ("logged_out", STATE_LOGGED_OUT),
        ("console", STATE_NON_SUBSCRIPTION_AUTH),
        ("bedrock", STATE_NON_SUBSCRIPTION_AUTH),
    ],
)
def test_every_non_subscription_source_blocks(tmp_path, mode, state):
    status = runtime(tmp_path, FAKE_CLAUDE_AUTH=mode).auth_status()
    assert status.state == state
    assert status.safe_for_subscription is False


def test_unreadable_auth_output_fails_closed(tmp_path):
    status = runtime(tmp_path, FAKE_CLAUDE_AUTH="broken").auth_status()
    assert status.safe_for_subscription is False
    assert status.state != STATE_CONNECTED


def test_auth_status_never_echoes_paths_or_unknown_fields(tmp_path):
    status = runtime(tmp_path).auth_status()
    assert "configDirectory" not in status.raw
    assert set(status.raw) <= {"loggedIn", "authMethod", "apiProvider", "subscriptionType"}


# -- environment sanitization ---------------------------------------------


def test_billing_env_is_stripped_from_the_child_only(tmp_path):
    dirty = {name: "x" for name in BILLING_ENV_VARS}
    dirty.update(_BASE_ENV)
    dirty["KEEP_ME"] = "yes"
    cli = ClaudeCliRuntime(command_override=str(fake_cli(tmp_path)), env=dirty)
    env, dropped = cli.child_env()
    assert set(dropped) == set(BILLING_ENV_VARS)
    assert not any(name in env for name in BILLING_ENV_VARS)
    assert env["KEEP_ME"] == "yes"
    # The caller's own environment is untouched.
    assert set(dirty) >= set(BILLING_ENV_VARS)


def test_the_child_really_runs_without_the_api_key(tmp_path):
    record = tmp_path / "record.json"
    cli = ClaudeCliRuntime(
        command_override=str(fake_cli(tmp_path)),
        env={
            **_BASE_ENV,
            "ANTHROPIC_API_KEY": "sk-should-not-reach-the-child",
            "CLAUDE_CODE_USE_BEDROCK": "1",
            "FAKE_CLAUDE_RECORD": str(record),
        },
    )
    ClaudeCliStream(cli).run(
        ClaudeRunRequest(
            model=None,
            system=None,
            messages=({"role": "user", "content": [{"type": "text", "text": "hola"}]},),
        )
    )
    seen = json.loads(record.read_text(encoding="utf-8"))
    assert "ANTHROPIC_API_KEY" not in seen["env"]
    assert "CLAUDE_CODE_USE_BEDROCK" not in seen["env"]


# -- isolation -------------------------------------------------------------


def test_the_child_is_started_as_a_transport_not_as_an_agent(tmp_path):
    record = tmp_path / "record.json"
    cli = runtime(tmp_path, FAKE_CLAUDE_RECORD=str(record))
    ClaudeCliStream(cli).run(
        ClaudeRunRequest(
            model="sonnet",
            system="Sos Rinari.",
            messages=({"role": "user", "content": [{"type": "text", "text": "hola"}]},),
        )
    )
    seen = json.loads(record.read_text(encoding="utf-8"))
    argv = seen["argv"]
    assert "--tools" in argv and argv[argv.index("--tools") + 1] == ""
    assert "--disable-slash-commands" in argv
    assert "--strict-mcp-config" in argv
    assert "--no-session-persistence" in argv
    assert argv[argv.index("--setting-sources") + 1] == ""
    assert argv[argv.index("--system-prompt") + 1] == "Sos Rinari."
    assert argv[argv.index("--model") + 1] == "sonnet"
    # `--bare` would force ANTHROPIC_API_KEY auth and never read the
    # subscription, so it must never appear.
    assert "--bare" not in argv


def test_the_child_runs_outside_the_workspace(tmp_path, monkeypatch):
    record = tmp_path / "record.json"
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / "CLAUDE.md").write_text("project instructions", encoding="utf-8")
    monkeypatch.chdir(workspace)
    cli = runtime(tmp_path, FAKE_CLAUDE_RECORD=str(record))
    ClaudeCliStream(cli).run(
        ClaudeRunRequest(model=None, system=None, messages=({"role": "user", "content": []},))
    )
    seen = json.loads(record.read_text(encoding="utf-8"))
    assert Path(seen["cwd"]).resolve() != workspace.resolve()
    assert not (Path(seen["cwd"]) / "CLAUDE.md").exists()


# -- streaming -------------------------------------------------------------


def test_text_streams_as_deltas_and_reports_usage(tmp_path):
    deltas: list[str] = []
    result = ClaudeCliStream(runtime(tmp_path, FAKE_CLAUDE_TEXT="uno dos tres")).run(
        ClaudeRunRequest(model=None, system=None, messages=({"role": "user", "content": []},)),
        on_delta=deltas.append,
    )
    assert result.text == "uno dos tres"
    assert len(deltas) > 1
    assert result.usage == {"input_tokens": 11, "output_tokens": 7, "cache_read_input_tokens": 3}
    assert result.model == "claude-sonnet-4-6-20260219"


def test_a_second_generation_is_rejected_locally(tmp_path):
    """One Rinari turn is one subscription call, with or without --max-turns."""
    result = ClaudeCliStream(
        runtime(tmp_path, FAKE_CLAUDE_MODE="double", FAKE_CLAUDE_TEXT="primera")
    ).run(ClaudeRunRequest(model=None, system=None, messages=({"role": "user", "content": []},)))
    assert result.text == "primera"
    assert "SEGUNDA" not in result.text


def test_empty_output_is_a_named_failure_not_an_empty_answer(tmp_path):
    with pytest.raises(ProviderError) as exc:
        ClaudeCliStream(runtime(tmp_path, FAKE_CLAUDE_MODE="empty")).run(
            ClaudeRunRequest(model=None, system=None, messages=({"role": "user", "content": []},))
        )
    assert exc.value.error_code == ProviderErrorCode.STREAM_INTERRUPTED


def test_rate_limit_is_normalized(tmp_path):
    with pytest.raises(ProviderError) as exc:
        ClaudeCliStream(runtime(tmp_path, FAKE_CLAUDE_MODE="rate_limit")).run(
            ClaudeRunRequest(model=None, system=None, messages=({"role": "user", "content": []},))
        )
    assert exc.value.error_code == ProviderErrorCode.RATE_LIMIT


def test_cancellation_stops_a_silent_child_promptly(tmp_path):
    """A child that prints nothing must still be cancellable.

    Reading the stream with a blocking loop made cancel wait for the process
    to end on its own (ten minutes, in the fixture). The deadline here is what
    keeps that regression from coming back unnoticed.
    """

    class Token:
        def is_cancelled(self) -> bool:
            return True

    started = time.monotonic()
    with pytest.raises(ProviderError) as exc:
        ClaudeCliStream(runtime(tmp_path, FAKE_CLAUDE_MODE="hang")).run(
            ClaudeRunRequest(model=None, system=None, messages=({"role": "user", "content": []},)),
            cancellation=Token(),
        )
    assert exc.value.error_code == ProviderErrorCode.STREAM_INTERRUPTED
    assert time.monotonic() - started < 30


def test_a_stalled_child_hits_the_first_token_deadline(tmp_path, monkeypatch):
    monkeypatch.setattr(claude_cli, "TIMEOUT_FIRST_TOKEN_S", 1.0)
    started = time.monotonic()
    with pytest.raises(ProviderError) as exc:
        ClaudeCliStream(runtime(tmp_path, FAKE_CLAUDE_MODE="hang")).run(
            ClaudeRunRequest(model=None, system=None, messages=({"role": "user", "content": []},))
        )
    assert exc.value.error_code == ProviderErrorCode.TIMEOUT
    assert time.monotonic() - started < 30


# -- adapter contract ------------------------------------------------------


def adapter(tmp_path: Path, **env: str) -> ClaudeSubscriptionAdapter:
    return ClaudeSubscriptionAdapter(runtime=runtime(tmp_path, **env))


def test_the_adapter_refuses_to_run_on_non_subscription_auth(tmp_path):
    with pytest.raises(ProviderError) as exc:
        adapter(tmp_path, FAKE_CLAUDE_AUTH="console").require_subscription()
    assert exc.value.error_code == ProviderErrorCode.AUTH


def test_the_adapter_never_accepts_a_credential(tmp_path):
    with pytest.raises(ProviderError):
        adapter(tmp_path).validate_credential("sk-anything")


def test_models_are_offered_without_claiming_the_account_has_them(tmp_path):
    models = adapter(tmp_path).list_models(None)
    assert [m.provider_model_id for m in models] == ["fable", "opus", "sonnet", "haiku"]
    assert {m.availability for m in models} == {"unknown"}
    assert all(m.capabilities["max_context_window"] is None for m in models)


def test_capabilities_do_not_promise_tools_before_the_bridge_exists(tmp_path):
    caps = adapter(tmp_path).capabilities()
    assert caps.streaming is True
    assert caps.tool_calls is False
    assert caps.vision is None
    assert caps.max_context_tokens is None


def test_a_tool_request_fails_loudly_instead_of_silently_dropping_tools(tmp_path):
    from rinari.models.types import ChatMessage, ModelRequest, ToolSchema

    request = ModelRequest(
        model="sonnet",
        messages=(ChatMessage.user("hola"),),
        tools=(ToolSchema(name="file_read", description="", parameters={}),),
    )
    with pytest.raises(ProviderError):
        adapter(tmp_path).invoke_stream(request, None, None, lambda _d: None)


def test_invoke_builds_history_from_rinari_not_from_claude(tmp_path):
    from rinari.models.types import ChatMessage, ModelRequest

    record = tmp_path / "record.json"
    request = ModelRequest(
        model="sonnet",
        messages=(
            ChatMessage.system("Sos Rinari."),
            ChatMessage.user("primera"),
            ChatMessage.assistant("respuesta"),
            ChatMessage.user("segunda"),
        ),
    )
    response = adapter(tmp_path, FAKE_CLAUDE_RECORD=str(record)).invoke_stream(
        request, None, None, lambda _d: None
    )
    seen = json.loads(record.read_text(encoding="utf-8"))
    lines = [json.loads(line) for line in seen["stdin"].splitlines() if line.strip()]
    texts = [block["text"] for line in lines for block in line["message"]["content"]]
    assert texts == ["primera", "[assistant]\nrespuesta", "segunda"]
    # The system prompt replaces Claude Code's own, it is not a user turn.
    assert "Sos Rinari." not in texts
    assert response.provider_state["transport"] == "claude-cli"
    assert response.provider_state["resolved_model"] == "claude-sonnet-4-6-20260219"
    assert response.usage.input_tokens == 11
