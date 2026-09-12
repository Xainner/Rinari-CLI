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
import time

import typer
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from rinari.cli import agent_runtime, render, slash
from rinari.cli.agent_runtime import AgentSession
from rinari.cli.snapshot import build_snapshot
from rinari.shared.errors import InvalidUsageError, RinariError


def _on_delta(delta: str) -> None:
    sys.stdout.write(delta)
    sys.stdout.flush()


def render_tool_event(
    console: Console,
    phase: str,
    name: str,
    detail: object,
    *,
    json_mode: bool,
    ascii_: bool,
    state: dict,
    elapsed: object,
) -> None:
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
        label = render.tool_label(name, detail)
        state["label"] = label
        state["arguments"] = detail if isinstance(detail, dict) else {}
        active = render.symbol(render.SYMBOL_ACTIVE, ascii_=ascii_)
        console.print(Text(f"{active} {label}", style=render.tool_style(name)), highlight=False)
    else:
        from rinari.tools.definition import ToolResult
        from rinari.runtime.agent import _tool_activity_presentation

        label = state.pop("label", name)
        arguments = state.pop("arguments", {})
        if isinstance(detail, ToolResult) and name in {
            "shell.exec",
            "process.output",
            "process.start",
        }:
            _render_command_result(
                console, _tool_activity_presentation(name, arguments, detail), ascii_=ascii_
            )
            if detail.error:
                console.print(
                    Text(f"{detail.error.code.value}: {detail.error.message}", style="red")
                )
            return
        if isinstance(detail, ToolResult) and not detail.ok and detail.error is not None:
            retryable = detail.error.retryable
            glyph = render.SYMBOL_RETRY if retryable else render.SYMBOL_FAIL
            sym = render.symbol(glyph, ascii_=ascii_)
            style = "yellow" if retryable else "red"
            tail = f" · {elapsed():.0f}s" if not retryable else ""
            message = detail.error.message.strip().replace("\n", " ")[:160]
            console.print(
                Text(
                    f"  {sym} {label}  {detail.error.code.value}{tail}  "
                    f"({detail.duration_ms:.0f}ms) · {message}",
                    style=style,
                ),
                highlight=False,
            )
            if (
                isinstance(detail, ToolResult)
                and isinstance(detail.data, dict)
                and name in {"shell.exec", "process.output"}
            ):
                _render_command_result(console, detail.data, ascii_=ascii_)
        else:
            sym = render.symbol(render.SYMBOL_OK, ascii_=ascii_)
            ms = f" ({detail.duration_ms:.0f}ms)" if isinstance(detail, ToolResult) else ""
            line = Text(f"  {sym} ", style="green")
            line.append(f"{label} · {elapsed():.0f}s{ms}", style="dim")
            console.print(line, highlight=False)
            if (
                isinstance(detail, ToolResult)
                and isinstance(detail.data, dict)
                and name in {"shell.exec", "process.output"}
            ):
                _render_command_result(console, detail.data, ascii_=ascii_)


def _render_command_result(console: Console, data: dict, *, ascii_: bool) -> None:
    """Render shell output as a readable Rich card, preserving stderr."""

    command = data.get("command")
    if isinstance(command, list):
        command = " ".join(str(part) for part in command)
    command = str(command or "")
    cwd = str(data.get("cwd") or "")
    exit_code = data.get("exit_code")
    stdout = str(data.get("stdout") or "")
    stderr = str(data.get("stderr") or "")
    header = Text("Shell", style="bold")
    if cwd:
        header.append(f" · {cwd}", style="dim")
    if isinstance(exit_code, int):
        header.append(f" · exit {exit_code}", style="red" if exit_code else "green")
    body = []
    if command:
        prompt = "PS> " if ":\\" in cwd else "$ "
        body.append(Text(prompt + command, style="cyan"))
    if stdout:
        body.append(Text("stdout\n" + stdout.rstrip("\n"), style="white"))
    if stderr:
        body.append(Text("stderr\n" + stderr.rstrip("\n"), style="yellow"))
        if exit_code == 0:
            body.append(
                Text(
                    "Process exited successfully but wrote to stderr; task outcome is not verified.",
                    style="yellow",
                )
            )
    if data.get("truncated"):
        body.append(Text("Visible output truncated. Full captured output:", style="dim"))
    for uri in data.get("artifacts", []):
        body.append(Text(str(uri), style="cyan"))
    if data.get("capture_truncated"):
        body.append(Text("Execution capture reached its 50 MiB limit.", style="yellow"))
    if body:
        from rich.console import Group

        console.print(Panel(Group(*body), title=header, border_style="dim", expand=False))


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
    json_mode = mode in (render.RendererMode.JSON, render.RendererMode.JSON_STREAM)
    ascii_ = mode is not render.RendererMode.RICH

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
        initial_message = prompt is not None
        if initial_message:
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

        if not json_mode:
            typer.echo()
            # typer.prompt already echoed interactive input. Only commands that
            # supplied an initial prompt need an explicit user transcript line.
            if initial_message:
                user_line = Text("YOU  ", style="bold bright_magenta")
                user_line.append(message)
                console.print(user_line)
        streamed = {"any": False, "segment_open": False, "ends_newline": True}
        turn_started = time.monotonic()
        status_state = {"live": None}

        def _start_status(
            label: str = "rinari thinking…",
            status_state=status_state,
            json_mode=json_mode,
            no_progress=no_progress,
        ) -> None:
            if json_mode or no_progress or status_state["live"] is not None:
                return
            live = render.thinking_status(console, label=label)
            status_state["live"] = live
            live.start()

        def _stop_status(status_state=status_state) -> None:
            live = status_state["live"]
            if live is not None:
                live.stop()
                status_state["live"] = None

        def _close_segment(streamed=streamed) -> None:
            if not streamed["segment_open"]:
                return
            if not streamed["ends_newline"]:
                sys.stdout.write("\n")
                sys.stdout.flush()
            streamed["segment_open"] = False

        def _delta(delta, streamed=streamed, stop=_stop_status) -> None:
            if json_mode:
                print(render.json_stream_event("token", text=delta), flush=True)
            else:
                if not streamed["segment_open"]:
                    streamed["any"] = True
                    streamed["segment_open"] = True
                    stop()
                    console.print(
                        Text("RINARI  ", style="bold bright_magenta"),
                        end="",
                        highlight=False,
                    )
                _on_delta(delta)
                streamed["ends_newline"] = delta.endswith(("\n", "\r"))

        def _elapsed(turn_started=turn_started) -> float:
            return time.monotonic() - turn_started

        tool_state: dict = {}

        def _tool(phase: str, name: str, detail: object, tool_state=tool_state) -> None:
            if no_progress and not json_mode:
                return
            if phase == "start" and not json_mode:
                _stop_status()
                _close_segment()
            render_tool_event(
                console,
                phase,
                name,
                detail,
                json_mode=json_mode,
                ascii_=ascii_,
                state=tool_state,
                elapsed=_elapsed,
            )
            if phase == "end" and not json_mode:
                _start_status("rinari evaluating results…")

        _start_status()
        try:
            message = agent_runtime.prepare_attachment_message(session, message)
            result = agent_runtime.run_turn(session, message, on_delta=_delta, on_tool=_tool)
        except KeyboardInterrupt:
            _stop_status()
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
            _stop_status()
            typer.echo(f"error: {err.message}", err=True)
            if err.hint:
                typer.echo(f"hint: {err.hint}", err=True)
            continue
        finally:
            _stop_status()
            _close_segment()

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
            # Non-streaming providers deliver the answer only in the result.
            if not streamed["any"] and result.content:
                from rich.markdown import Markdown

                console.print(Text("RINARI", style="bold bright_magenta"))
                console.print(Markdown(result.content))
            if result.kind == "truncated":
                typer.echo("(output stopped at the model's max tokens)", err=True)

        snap = build_snapshot(session)
        typer.echo(
            Text(render.status_line(snap, turn_kind=result.kind, ascii_=ascii_), style="dim")
        )

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


__all__ = ["render_tool_event", "run_repl"]
