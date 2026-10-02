"""Claude CLI transport: binary discovery, auth guard, isolation, streaming.

Everything runs against the fake CLI in tests/fixtures, through a real child
process, so the Windows `.cmd` shim, the process group and the stream parsing
are exercised rather than mocked.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
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


def test_probes_never_inherit_the_engine_stdin(tmp_path):
    """The Engine speaks NDJSON over stdin; a probe must not touch that pipe.

    `capture_output` redirects only stdout and stderr. Inheriting stdin let the
    probe's child swallow protocol bytes, and the desktop host then timed out
    every request - a failure no unit test sees, because their stdin is a
    console. Caught by the first real Electron smoke.
    """
    seen: dict[str, object] = {}

    def runner(*args, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(args[0], 0, "2.1.286 (Claude Code)", "")

    ClaudeCliRuntime(
        command_override=str(fake_cli(tmp_path)), env=dict(_BASE_ENV), runner=runner
    ).version()
    assert seen["stdin"] is subprocess.DEVNULL


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
    assert set(BILLING_ENV_VARS) <= set(dropped)
    assert not any(name in env for name in BILLING_ENV_VARS)
    assert env["KEEP_ME"] == "yes"
    # The caller's own environment is untouched.
    assert set(dirty) >= set(BILLING_ENV_VARS)


def test_another_claude_session_identity_never_reaches_the_child(tmp_path):
    """A parent Claude Code session must not lend the child its identity.

    Caught in the first real smoke: running Rinari from inside Claude Code
    handed the transport's child a messaging token, OAuth scopes and the
    account id of an unrelated session. The CLI authenticates from the user's
    own ~/.claude, so none of it is needed.
    """
    dirty = {
        **_BASE_ENV,
        "CLAUDE_CODE_MESSAGING_TOKEN": "tok",
        "CLAUDE_CODE_OAUTH_SCOPES": "user:inference",
        "CLAUDE_CODE_ACCOUNT_UUID": "acct",
        "CLAUDECODE": "1",
        "CLAUDE_EFFORT": "max",
        "USERPROFILE": "C:/Users/someone",
        "HOME": "/home/someone",
    }
    env, dropped = ClaudeCliRuntime(command_override=str(fake_cli(tmp_path)), env=dirty).child_env()
    assert not [name for name in env if name.upper().startswith(("ANTHROPIC_", "CLAUDECODE"))]
    assert "CLAUDE_CODE_MESSAGING_TOKEN" not in env
    assert "CLAUDE_CODE_OAUTH_SCOPES" not in env
    assert "CLAUDE_CODE_ACCOUNT_UUID" not in env
    assert {"CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_CODE_OAUTH_SCOPES", "CLAUDECODE"} <= set(dropped)
    # What the CLI needs to find its own credentials survives.
    assert env["USERPROFILE"] == "C:/Users/someone"
    assert env["HOME"] == "/home/someone"
    # Rinari's own marker is set after the sweep.
    assert env["CLAUDE_CODE_ENTRYPOINT"] == "rinari"


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
    # The prompt travels as a file, so a system prompt larger than the Windows
    # command-line limit still works and never shows up in the process list.
    assert "--system-prompt" not in argv
    assert seen["system_prompt"] == "Sos Rinari."
    assert Path(argv[argv.index("--system-prompt-file") + 1]).parent == Path(seen["cwd"])
    assert argv[argv.index("--model") + 1] == "sonnet"
    # `--bare` would force ANTHROPIC_API_KEY auth and never read the
    # subscription, so it must never appear.
    assert "--bare" not in argv


def test_a_system_prompt_larger_than_the_command_line_limit_still_runs(tmp_path):
    """Rinari's system prompt runs past the 32 KB Windows command line.

    Passing it as an argument failed in the first real Electron smoke with a
    bare non-zero exit, which is exactly the kind of failure a fixture-only
    suite never sees.
    """
    record = tmp_path / "record.json"
    huge = "x" * 60_000
    result = ClaudeCliStream(runtime(tmp_path, FAKE_CLAUDE_RECORD=str(record))).run(
        ClaudeRunRequest(
            model=None,
            system=huge,
            messages=({"role": "user", "content": [{"type": "text", "text": "hola"}]},),
        )
    )
    assert result.text
    seen = json.loads(record.read_text(encoding="utf-8"))
    assert len(" ".join(seen["argv"])) < 32_000
    assert seen["system_prompt"] == huge


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


def test_models_come_from_the_account_picker(tmp_path):
    """Plan section 19: the live picker of the account, not a static list.

    The CLI answers an `initialize` control request with the models the
    signed-in account offers, before any user message exists, so discovery
    spends no inference call.
    """
    record = tmp_path / "record.json"
    models = {
        m.provider_model_id: m
        for m in adapter(tmp_path, FAKE_CLAUDE_RECORD=str(record)).list_models(None)
    }
    # `default` is a pointer to another entry: offering it would list Opus twice.
    assert set(models) == {"opus", "claude-opus-4-6", "haiku"}
    assert {m.availability for m in models.values()} == {"available"}
    opus = models["opus"].capabilities
    assert opus["label"] == "Opus 5.5"
    assert opus["resolved_model"] == "claude-opus-5-5"
    assert opus["source"] == "claude-cli-picker"
    assert opus["max_context_window"] is None
    # No print-mode request reached the CLI: nothing was generated.
    assert not record.exists()


def test_each_model_offers_only_the_effort_levels_it_takes(tmp_path):
    """Levels come per model from the picker, not one list for the product.

    Haiku 4.5 carries no `supportsEffort` field and takes no effort; the 4.6
    models stop at `max`. One list for every model put choices in the
    composer that the CLI would then ignore.
    """
    models = {m.provider_model_id: m.capabilities for m in adapter(tmp_path).list_models(None)}
    assert models["opus"]["reasoning_levels"] == ["low", "medium", "high", "xhigh", "max"]
    assert models["claude-opus-4-6"]["reasoning_levels"] == ["low", "medium", "high", "max"]
    assert models["haiku"]["reasoning_effort"] is False
    assert models["haiku"]["reasoning_levels"] == []


def test_without_a_picker_the_documented_aliases_are_the_fallback(tmp_path):
    """An older CLI that does not answer `initialize` still offers models."""
    models = adapter(tmp_path, FAKE_CLAUDE_NO_PICKER="1").list_models(None)
    assert [m.provider_model_id for m in models] == ["fable", "opus", "sonnet", "haiku"]
    assert {m.availability for m in models} == {"unknown"}
    # Nothing is claimed about effort: the product default in metadata.py applies.
    assert all("reasoning_levels" not in m.capabilities for m in models)


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


# -- razonamiento ----------------------------------------------------------


@pytest.mark.parametrize("level", ["low", "medium", "high", "xhigh", "max"])
def test_an_effort_the_cli_takes_is_forwarded(tmp_path, level):
    record = tmp_path / "record.json"
    ClaudeCliStream(runtime(tmp_path, FAKE_CLAUDE_RECORD=str(record))).run(
        ClaudeRunRequest(
            model=None, system=None, messages=({"role": "user", "content": []},), effort=level
        )
    )
    argv = json.loads(record.read_text(encoding="utf-8"))["argv"]
    assert argv[argv.index("--effort") + 1] == level


@pytest.mark.parametrize("level", ["none", "minimal", "ultra"])
def test_an_effort_the_cli_ignores_is_not_sent(tmp_path, level):
    """Rinari offers eight levels; `--effort` takes five.

    The CLI answers an unknown value with a warning on stderr and falls back
    to its default, which nobody sees. Sending it anyway would show the user a
    setting that looks applied and is not.
    """
    record = tmp_path / "record.json"
    ClaudeCliStream(runtime(tmp_path, FAKE_CLAUDE_RECORD=str(record))).run(
        ClaudeRunRequest(
            model=None, system=None, messages=({"role": "user", "content": []},), effort=level
        )
    )
    argv = json.loads(record.read_text(encoding="utf-8"))["argv"]
    assert "--effort" not in argv


def test_thinking_blocks_survive_the_transport(tmp_path):
    """Reasoning reached Rinari through the HTTP adapter and was dropped here."""
    result = ClaudeCliStream(
        runtime(tmp_path, FAKE_CLAUDE_THINKING="estoy pensando", FAKE_CLAUDE_TEXT="respuesta")
    ).run(ClaudeRunRequest(model=None, system=None, messages=({"role": "user", "content": []},)))
    kinds = [block.get("type") for block in result.blocks]
    assert kinds == ["thinking", "text"]
    thinking = result.blocks[0]
    assert thinking["thinking"] == "estoy pensando"
    # The signature travels with it: Anthropic requires it to replay the block.
    assert thinking["signature"] == "sig-abc"
    # Thinking is never mixed into the answer.
    assert result.text == "respuesta"


def test_the_adapter_hands_thinking_to_rinari_as_items(tmp_path):
    from rinari.models.types import ChatMessage, ModelRequest

    response = adapter(tmp_path, FAKE_CLAUDE_THINKING="paso a paso").invoke_stream(
        ModelRequest(model="sonnet", messages=(ChatMessage.user("hola"),)),
        None,
        None,
        lambda _d: None,
    )
    assert [item.type for item in response.items] == ["thinking", "text"]
    assert response.items[0].data["thinking"] == "paso a paso"


# -- forma real del stream (verificada contra 2.1.286) ----------------------


def test_thinking_and_text_of_one_generation_are_not_a_second_generation(tmp_path):
    """The real CLI emits one assistant event per content block, same id.

    Counting events killed every reply with thinking before its `result`, so
    the provider-reported usage never arrived. Haiku with no effort set
    already thinks, so this was every turn, not an edge case.
    """
    result = ClaudeCliStream(
        runtime(tmp_path, FAKE_CLAUDE_THINKING="pienso", FAKE_CLAUDE_TEXT="respuesta")
    ).run(ClaudeRunRequest(model=None, system=None, messages=({"role": "user", "content": []},)))
    assert result.text == "respuesta"
    # The `result` event was read: that is the only place usage comes from.
    assert result.raw_result is not None
    assert result.usage == {"input_tokens": 11, "output_tokens": 7, "cache_read_input_tokens": 3}


def test_a_different_message_id_is_still_a_second_generation(tmp_path):
    result = ClaudeCliStream(
        runtime(tmp_path, FAKE_CLAUDE_MODE="double", FAKE_CLAUDE_TEXT="primera")
    ).run(ClaudeRunRequest(model=None, system=None, messages=({"role": "user", "content": []},)))
    assert result.text == "primera"
    assert "SEGUNDA" not in result.text


def test_an_error_reported_as_success_is_an_error(tmp_path):
    """The CLI marks a rejected call `subtype: success` with `is_error: true`.

    Seen for real with a model the plan does not cover: an out-of-credits
    notice arrived as an assistant message and the subtype said success, so
    it would have been shown to the user as the model's answer.
    """
    deltas: list[str] = []
    with pytest.raises(ProviderError) as exc:
        ClaudeCliStream(runtime(tmp_path, FAKE_CLAUDE_MODE="usage_credits")).run(
            ClaudeRunRequest(model=None, system=None, messages=({"role": "user", "content": []},)),
            on_delta=deltas.append,
        )
    assert exc.value.error_code == ProviderErrorCode.RATE_LIMIT
    assert exc.value.retryable is False
    # Nothing of the notice was streamed as if the model had said it.
    assert deltas == []


# -- origen de la credencial en cada turno -------------------------------------


def test_a_run_that_would_bill_an_api_key_is_stopped(tmp_path):
    """The init event names the credential the child picked; check it every run.

    Verified against 2.1.286: with an API key reaching the child the init
    event says `apiKeySource: "ANTHROPIC_API_KEY"` while `claude auth status`
    keeps saying claude.ai. The environment sweep stops the known variables;
    this stops whatever it cannot see, such as a managed `apiKeyHelper`.
    """
    deltas: list[str] = []
    with pytest.raises(ProviderError) as exc:
        ClaudeCliStream(runtime(tmp_path, FAKE_CLAUDE_API_KEY_SOURCE="apiKeyHelper")).run(
            ClaudeRunRequest(model=None, system=None, messages=({"role": "user", "content": []},)),
            on_delta=deltas.append,
        )
    assert exc.value.error_code == ProviderErrorCode.AUTH
    assert "apiKeyHelper" in str(exc.value)
    assert deltas == []


@pytest.mark.parametrize("source", ["none", "<absent>"])
def test_the_subscription_or_an_unreported_source_runs(tmp_path, source):
    """`none` is the subscription; a field the CLI does not send is left to
    the environment sweep and the auth check instead of blocking every turn."""
    result = ClaudeCliStream(runtime(tmp_path, FAKE_CLAUDE_API_KEY_SOURCE=source)).run(
        ClaudeRunRequest(model=None, system=None, messages=({"role": "user", "content": []},))
    )
    assert result.text
