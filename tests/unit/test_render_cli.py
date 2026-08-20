"""Phase 7: presenters (renderer selection, banner, status line, approval)."""

from __future__ import annotations

from rich.console import Console

from rinari.cli.render import (
    RendererMode,
    approval_lines,
    banner_fields,
    detect_mode,
    json_stream_event,
    render_approval,
    render_banner,
    status_line,
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


def _console():
    return Console(record=True, no_color=True, width=100)


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
    assert "Rinari v9.9.9" in rich_text
    assert rich_text.count("____") >= 3

    plain_console = _console()
    render_banner(plain_console, snap, RendererMode.PLAIN)
    plain = plain_console.export_text()
    assert "session" in plain
    assert "provider" in plain
    assert "____" not in plain


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
    assert "ctx 5%" in line
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


# -- json stream --------------------------------------------------------------


def test_json_stream_event() -> None:
    import json

    line = json_stream_event("token", text="hi")
    doc = json.loads(line)
    assert doc["type"] == "token"
    assert doc["text"] == "hi"
