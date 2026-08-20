"""Phase 7: presenters (renderer selection, banner, status line, approval)."""

from __future__ import annotations

from rich.console import Console

from rinari.cli.render import (
    RendererMode,
    approval_lines,
    banner_fields,
    context_meter,
    detect_mode,
    extensions_line,
    json_stream_event,
    render_approval,
    render_banner,
    status_line,
    tool_label,
    tool_verb,
)
from rinari.cli.snapshot import RuntimeSnapshot, SessionUsage
from rinari.policy.approvals import ApprovalRequest


def _snap(**overrides) -> RuntimeSnapshot:
    base = dict(
        version="9.9.9",
        session_id="ses_0123456789ABCDEF",
        session_kind="PROJECT",
        session_state="active",
        mode="auto",
        provider_alias="fake",
        provider_type="openai",
        model_alias="m1",
        provider_model_id="model-x",
        reasoning_effort=None,
        context_used_tokens=100,
        context_window_tokens=2000,
        context_percent=0.05,
        project_name="demo",
        branch="main",
        dirty=True,
        profile="workspace",
        network_mode="ask",
        tools_loaded=99,
        skills_active=("fix-ci",),
        skills_known=10,
        agents=(),
        last_completion=None,
        usage=SessionUsage(model_calls=2, tool_calls=5, cost_usd=0.123456),
    )
    base.update(overrides)
    return RuntimeSnapshot(**base)


def _console(*, width: int = 120):
    return Console(record=True, no_color=True, width=width)


# -- renderer selection ------------------------------------------------------


def test_detect_mode_matrix() -> None:
    mode, no_color, banner = detect_mode(interactive=True, json_flag=True, env={})
    assert mode is RendererMode.JSON
    assert banner is False

    mode, _, banner = detect_mode(interactive=False, json_flag=True, env={})
    assert mode is RendererMode.JSON_STREAM

    mode, no_color, _ = detect_mode(interactive=False, json_flag=False, env={})
    assert mode is RendererMode.PLAIN
    assert no_color is True

    mode, _, _ = detect_mode(interactive=True, json_flag=False, env={"TERM": "dumb"})
    assert mode is RendererMode.PLAIN

    mode, no_color, _ = detect_mode(interactive=True, json_flag=False, env={"NO_COLOR": "1"})
    assert mode is RendererMode.COMPACT
    assert no_color is True

    mode, _, _ = detect_mode(interactive=True, json_flag=False, env={"RINARI_RENDERER": "plain"})
    assert mode is RendererMode.PLAIN

    mode, no_color, banner = detect_mode(interactive=True, json_flag=False, env={})
    assert mode is RendererMode.RICH
    assert no_color is False
    assert banner is True


# -- banner -------------------------------------------------------------------


def test_banner_fields_known_values() -> None:
    rows = dict(banner_fields(_snap()))
    assert rows["provider"] == "fake (openai)"
    assert rows["model"] == "m1 (model-x)"
    assert "100/2000" in rows["context"]
    assert "5%" in rows["context"]
    assert rows["project"] == "demo [main] *"
    assert rows["skills"] == "fix-ci"
    assert "0.1235" in rows["usage"]


def test_banner_fields_unknown_stay_dashes() -> None:
    rows = dict(
        banner_fields(
            _snap(
                provider_alias=None,
                provider_type=None,
                model_alias=None,
                provider_model_id=None,
                context_used_tokens=None,
                context_window_tokens=None,
                context_percent=None,
                project_name=None,
                branch=None,
                dirty=None,
                skills_active=(),
                network_mode=None,
                usage=SessionUsage(),
            )
        )
    )
    assert rows["provider"] == "—"
    assert rows["model"] == "—"
    assert rows["context"] == "—"
    assert rows["project"] == "—"
    assert rows["skills"] == "- none"
    assert rows["agents"] == "- none"
    assert rows["network"] == "—"
    assert "$" not in rows["usage"]


def test_banner_rich_has_art_plain_has_none() -> None:
    snap = _snap()
    rich_console = _console()
    render_banner(rich_console, snap, RendererMode.RICH)
    rich_text = rich_console.export_text()
    assert "RINARI" in rich_text
    assert "v9.9.9" in rich_text
    assert "AI engineering companion" in rich_text
    assert "RUNTIME" in rich_text
    assert "WORKSPACE" in rich_text
    assert "CAPABILITIES" in rich_text
    assert "USAGE" in rich_text
    assert "/help" in rich_text
    assert "██████" in rich_text  # the A's crossbar
    assert "█████ " in rich_text  # the R's top row

    plain_console = _console()
    render_banner(plain_console, snap, RendererMode.PLAIN)
    plain = plain_console.export_text()
    assert "session" in plain
    assert "provider" in plain
    assert "█" not in plain


def test_banner_narrow_keeps_essential_runtime_information() -> None:
    console = _console(width=72)
    render_banner(console, _snap(), RendererMode.RICH)
    text = console.export_text()

    assert "RINARI" in text
    assert "fake (openai)" in text
    assert "m1 (model-x)" in text
    assert "demo [main] *" in text
    assert "100/2000 tokens (5%)" in text
    assert "99 loaded" in text
    assert "fix-ci" in text


def test_banner_compact_uses_wordmark_without_block_art() -> None:
    console = _console(width=80)
    render_banner(console, _snap(), RendererMode.COMPACT)
    text = console.export_text()

    assert "RINARI" in text
    assert "v9.9.9" in text
    assert "█" not in text
    assert "provider" in text
    assert "project" in text
    assert "2 model · 5 tools · $0.1235" in text


def test_banner_art_rows_spells_rinari() -> None:
    from rinari.cli.render import _ART_LETTERS, _ART_WORDS, banner_art

    expected = ["  ".join(_ART_LETTERS[ch][row] for ch in _ART_WORDS) for row in range(5)]
    rows = banner_art()
    assert [row.plain for row in rows] == expected


def test_banner_agents_running() -> None:
    snap = _snap(
        agents=(
            {"id": "agt_1", "agent": "implementer", "state": "running"},
            {"id": "agt_2", "agent": "reviewer", "state": "completed"},
        )
    )
    rows = dict(banner_fields(snap))
    assert rows["agents"] == "1 running (implementer)"


# -- status rail --------------------------------------------------------------


def test_status_line_only_real_numbers() -> None:
    line = status_line(_snap(), turn_kind="answer")
    assert line.startswith("answer")
    assert "ctx" in line and "5%" in line
    assert "░░" in line  # context meter bar (5% of 8 wide)
    assert "2m/5t" in line
    assert "$0.1235" in line

    line = status_line(_snap(usage=SessionUsage()), turn_kind="budget")
    assert "$" not in line
    assert line.startswith("budget")

    line = status_line(
        _snap(agents=({"id": "a", "agent": "debugger", "state": "running"},)),
        turn_kind="answer",
        active_tool="shell.exec",
    )
    assert "1 agents" in line
    assert "shell.exec" in line


# -- approval -----------------------------------------------------------------


def test_approval_lines_critical_on_high_risk() -> None:
    request = ApprovalRequest(
        capability="git.push",
        description="remote mutation",
        target="origin/main",
        risk="high",
    )
    lines = approval_lines(request)
    assert any("git.push origin/main" in line for line in lines)
    assert any("critical" in line for line in lines)

    request = ApprovalRequest(capability="fs.read", description="local read", risk="low")
    assert not any("critical" in line for line in approval_lines(request))


def test_render_approval_panel() -> None:
    console = _console()
    render_approval(
        console, ApprovalRequest(capability="shell.exec", description="run command", risk="high")
    )
    text = console.export_text()
    assert "shell.exec" in text
    assert "approval required" in text


# -- tool rendering / context meter / extensions (R1-R3) ---------------------


def test_context_meter_unknown_is_empty() -> None:
    assert context_meter(None) == ""
    assert context_meter(0.5) == "████░░░░"
    assert context_meter(0.5, ascii_=True) == "####----"


def test_tool_verb_and_label() -> None:
    assert tool_verb("fs.read") == "read"
    assert tool_verb("fs.patch") == "edit"
    assert tool_verb("shell.exec") == "run"
    assert tool_verb("search.regex") == "read"
    assert tool_verb("git.status") == "git"
    assert tool_verb("browser.open") == "browser"
    assert tool_label("fs.read", {"path": "src/a.ts"}) == "read src/a.ts"
    assert tool_label("shell.exec", {"command": "pytest"}) == "run pytest"


def test_extensions_line_counts() -> None:
    snap = _snap(
        skills_active=("fix-ci",),
        skills_known=10,
        agents=({"id": "a", "agent": "debugger", "state": "running"},),
        mcp_connected=2,
        plugins_loaded=3,
    )
    line = extensions_line(snap)
    assert "tools 99" in line
    assert "skills 1/10" in line
    assert "agents 1/1" in line
    assert "mcp 2" in line
    assert "plugins 3" in line


def test_approval_panel_numbered_options() -> None:
    console = _console()
    render_approval(
        console, ApprovalRequest(capability="git.push", description="remote", risk="high")
    )
    text = console.export_text()
    assert "approval required" in text
    assert "[1] Allow once" in text
    assert "[2] Allow for session" in text
    assert "[3] Allow for project" in text
    assert "[5] Deny" in text


def test_thinking_status_is_transient_live() -> None:
    from rinari.cli.render import thinking_status

    live = thinking_status(_console(), spinner="dots12")
    assert live.transient is True
    assert hasattr(live, "start") and hasattr(live, "stop")


# -- json stream --------------------------------------------------------------


def test_json_stream_event() -> None:
    import json

    line = json_stream_event("token", text="hi")
    doc = json.loads(line)
    assert doc["type"] == "token"
    assert doc["text"] == "hi"
