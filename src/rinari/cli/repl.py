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
from rich.text import Text

from rinari.cli import agent_runtime, render, slash
from rinari.cli.agent_runtime import AgentSession
from rinari.cli.snapshot import build_snapshot
from rinari.shared.errors import InvalidUsageError, RinariError


def _on_delta(delta: str) -> None:
    sys.stdout.write(delta)
    sys.stdout.flush()


def _on_tool(
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
        active = render.symbol(render.SYMBOL_ACTIVE, ascii_=ascii_)
        console.print(Text(f"{active} {label}", style="bold"), highlight=False)
    else:
        from rinari.tools.definition import ToolResult

        label = state.pop("label", name)
        if isinstance(detail, ToolResult) and not detail.ok and detail.error is not None:
            retryable = detail.error.retryable
            glyph = render.SYMBOL_RETRY if retryable else render.SYMBOL_FAIL
            sym = render.symbol(glyph, ascii_=ascii_)
            style = "yellow" if retryable else "red"
            tail = f" · {elapsed():.0f}s" if not retryable else ""
            console.print(
                Text(
                    f"  {sym} {label}  {detail.error.code.value}{tail}  "
                    f"({detail.duration_ms:.0f}ms)",
                    style=style,
                ),
                highlight=False,
            )
        else:
            sym = render.symbol(render.SYMBOL_OK, ascii_=ascii_)
            ms = f" ({detail.duration_ms:.0f}ms)" if isinstance(detail, ToolResult) else ""
            console.print(
                Text(f"  {sym} {label} · {elapsed():.0f}s{ms}", style="dim"),
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

        if not json_mode:
            typer.echo()
            console.print(Text(f"you > {message}", style="bold"))
        streamed = {"any": False}
        turn_started = time.monotonic()

        def _delta(delta: str, streamed=streamed) -> None:
            if json_mode:
                print(render.json_stream_event("token", text=delta), flush=True)
            else:
                if not streamed["any"]:
                    streamed["any"] = True
                    console.print(Text("rinari > ", style="bold"), end="", highlight=False)
                _on_delta(delta)

        def _elapsed(turn_started=turn_started) -> float:
            return time.monotonic() - turn_started

        tool_state: dict = {}

        def _tool(phase: str, name: str, detail: object, tool_state=tool_state) -> None:
            if no_progress and not json_mode:
                return
            _on_tool(
                console,
                phase,
                name,
                detail,
                json_mode=json_mode,
                ascii_=ascii_,
                state=tool_state,
                elapsed=_elapsed,
            )

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
                from rich.markdown import Markdown

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


__all__ = ["run_repl"]
