"""Interactive REPL for the agent session (phase 2, phase-7 renderers).

Render + input layer only: no business logic. The renderer (rich / compact /
plain / json_stream) is chosen from TTY state and environment (`NO_COLOR`,
`TERM=dumb`, `RINARI_RENDERER`); the startup banner and the post-turn status
rail read a `RuntimeSnapshot` so the UI never invents metrics (AGENTS.md 22).

Slash commands: see `rinari.cli.slash` (/help inside the session lists them).

Cancellation (harness.md phase-2 cancellation spec):
    Ctrl+C during a turn  -> cancels the turn (model stream / tool / subprocess)
    Ctrl+C again          -> exits the REPL
"""

from __future__ import annotations

import sys

import typer
from rich.console import Console
from rich.text import Text

from rinari.cli import agent_runtime, render, slash
from rinari.cli.agent_runtime import AgentSession
from rinari.cli.snapshot import build_snapshot
from rinari.shared.errors import InvalidUsageError, RinariError


def _on_delta(delta: str) -> None:
    sys.stdout.write(delta)
    sys.stdout.flush()


def _on_tool(console: Console, phase: str, name: str, detail: object, *, json_mode: bool) -> None:
    if json_mode:
        if phase == "start":
            print(render.json_stream_event("tool", phase="start", tool=name), flush=True)
        else:
            ok = getattr(detail, "ok", None)
            print(
                render.json_stream_event(
                    "tool",
                    phase="end",
                    tool=name,
                    ok=ok,
                    duration_ms=getattr(detail, "duration_ms", None),
                ),
                flush=True,
            )
        return
    if phase == "start":
        args = ""
        if isinstance(detail, dict):
            for key in ("path", "pattern", "command"):
                value = detail.get(key)
                if value:
                    args = f" {str(value)[:80]}"
                    break
        console.print(Text(f"  [tool] {name}{args} ...", style="dim"), highlight=False)
    else:
        from rinari.tools.definition import ToolResult

        if isinstance(detail, ToolResult):
            state = "ok" if detail.ok else f"error({detail.error.code.value})"
            console.print(
                Text(f"  [tool] {name} {state} ({detail.duration_ms:.0f}ms)", style="dim"),
                highlight=False,
            )


def run_repl(
    session: AgentSession,
    initial_prompt: str | None = None,
    *,
    no_banner: bool = False,
    no_progress: bool = False,
    json_flag: bool = False,
) -> str | None:
    """Run the interactive loop.

    Returns None on exit, "new:<forced_chat>" after /new, or "resume:<ref>"
    after /resume so the session flow can restart with the right target.
    """
    mode, no_color, banner_allowed = render.detect_mode(
        interactive=True, json_flag=json_flag, env=None
    )
    console = render.make_console(mode, no_color)
    json_mode = mode is render.RendererMode.JSON_STREAM

    if not no_banner and banner_allowed:
        render.render_banner(console, build_snapshot(session), mode)
    elif json_mode:
        snap = build_snapshot(session)
        print(render.json_stream_event("session", **snap.to_dict()), flush=True)

    interrupted_this_turn = False
    prompt = initial_prompt

    while True:
        session.token.reset()
        interrupted_this_turn = False
        if prompt is not None:
            message = prompt
            prompt = None
        else:
            message = typer.prompt("rinari", prompt_suffix="> ")
            message = message.strip()
            if not message:
                continue

        if message.startswith("/"):
            try:
                outcome = slash.handle(session, console, message)
            except (InvalidUsageError, RinariError) as err:
                typer.echo(f"error: {err.message}", err=True)
                if err.hint:
                    typer.echo(f"hint: {err.hint}", err=True)
                continue
            if outcome.action == "exit":
                return None
            if outcome.action == "new_session":
                return "new"
            if outcome.action == "resume_session":
                if not outcome.resume_ref:
                    typer.echo("usage: /resume <session id>", err=True)
                    continue
                return f"resume:{outcome.resume_ref}"
            if outcome.action == "turn" and outcome.prompt:
                prompt = outcome.prompt
                continue
            continue

        _print_turn_header(json_mode)
        streamed = {"any": False}

        def _delta(delta: str, streamed=streamed) -> None:
            if json_mode:
                print(render.json_stream_event("token", text=delta), flush=True)
            else:
                streamed["any"] = True
                _on_delta(delta)

        def _tool(phase: str, name: str, detail: object) -> None:
            if no_progress and not json_mode:
                return
            _on_tool(console, phase, name, detail, json_mode=json_mode)

        try:
            result = agent_runtime.run_turn(session, message, on_delta=_delta, on_tool=_tool)
        except KeyboardInterrupt:
            # First Ctrl+C: cancel the in-flight turn. Second one: hard exit.
            if interrupted_this_turn:
                return None
            interrupted_this_turn = True
            session.token.cancel()
            sys.stdout.write("\n")
            sys.stdout.flush()
            typer.echo("turn cancelled (Ctrl+C again to exit)", err=True)
            continue
        except RinariError as err:
            typer.echo(f"error: {err.message}", err=True)
            if err.hint:
                typer.echo(f"hint: {err.hint}", err=True)
            continue

        if json_mode:
            print(
                render.json_stream_event("turn_end", **_turn_payload(result, session)), flush=True
            )
            continue

        if result.kind == "cancelled":
            typer.echo("turn cancelled", err=True)
        elif result.kind not in ("answer", "truncated"):
            console.print(Text(result.content, style="yellow"))
        else:
            typer.echo()
            # Non-streaming providers deliver the answer only in the result.
            if not streamed["any"] and result.content:
                console.print(result.content)
            if result.kind == "truncated":
                typer.echo("(output stopped at the model's max tokens)", err=True)

        snap = build_snapshot(session)
        typer.echo(Text(render.status_line(snap, turn_kind=result.kind), style="dim"))

        if result.compacted:
            console.print(
                Text(
                    "context: compacted (history summarized, task state preserved)",
                    style="dim",
                )
            )
        if result.completion:
            outcome = result.completion.get("outcome")
            if outcome:
                reasons = "; ".join(result.completion.get("reasons") or [])
                text = f"completion: {outcome}"
                if reasons:
                    text += f" ({reasons})"
                console.print(Text(text, style="green" if outcome == "DONE" else "yellow"))

        if session.promoted_root is not None:
            console.print(
                Text(
                    f"*** session promoted to PROJECT — root: {session.promoted_root} ***",
                    style="bold green",
                )
            )
            session.promoted_root = None
            if banner_allowed:
                render.render_banner(console, build_snapshot(session), mode)


def _turn_payload(result, session) -> dict:
    from rinari.cli.session_flow import _turn_dict

    payload = dict(_turn_dict(result))
    payload["session_id"] = session.record.id
    return payload


def _print_turn_header(json_mode: bool) -> None:
    if not json_mode:
        typer.echo()


__all__ = ["run_repl"]
