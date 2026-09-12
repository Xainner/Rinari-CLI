"""Interactive slash commands (phase 7).

Read-only commands surface live runtime state (snapshot, services, repositories)
and never call a model. `/test` and `/review` are the exception: they queue a
pre-cooked agent turn because running tests or reviewing code *is* agent work
the user explicitly asked for.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass

import typer
from rich.console import Console

from rinari.cli.agent_runtime import AgentSession
from rinari.cli.render import banner_fields
from rinari.cli.snapshot import build_snapshot
from rinari.shared.errors import InvalidUsageError

_HELP = """\
/exit | /quit          leave the session
/help                  this help
/status                full runtime snapshot (banner fields)
/provider [alias]      list providers / switch (session only)
/model [alias]         list models / switch (session only)
/session               show this session
/mode                  session kind and mode
/usage                 model/tool calls, tokens, elapsed, cost
/tokens                context usage (estimate + window)
/plan                  task tree for this project
/tasks                 task list for this project
/diff                  uncommitted changes (project sessions)
/test                  run the project's test suite (one agent turn)
/review                review uncommitted changes (one agent turn)
/skills [query]        list / search skills
/tools                 loaded tools with risk classes
/agents                subagent state
/permissions           policy profile, network mode, approval scopes
/checkpoint            latest checkpoints for this project
/undo                  restore the latest checkpoint (asks to confirm)
/compact               report compaction pressure (compacts when due)
/context               prompt-stack segments and sizes
/trace [n]             last n session events (default 15)
/new                   start a new session (same context)
/resume [id]           resume a session by id
/attach <path>         attach a file to the next turn
/attach --ocr <path>   use image OCR text instead of vision
/attach --vision <path> explicitly try a model with unknown vision support
Ctrl+C                 cancel turn (again: exit)
"""


@dataclass(frozen=True)
class SlashOutcome:
    action: str  # "stay" | "exit" | "new_session" | "resume_session" | "turn"
    prompt: str | None = None
    resume_ref: str | None = None


_STAY = SlashOutcome("stay")
_EXIT = SlashOutcome("exit")


_TEST_PROMPT = (
    "Run this project's test suite: detect the standard command from the project "
    "configuration (package.json, pyproject.toml, Makefile, ...), execute it, and "
    "report the outcome with failures summarized. Do not modify any code."
)
_REVIEW_PROMPT = (
    "Review the uncommitted changes in this repository (git diff plus untracked "
    "files). Report concrete issues ordered by severity: bugs first, then "
    "security, then conventions. Reference file and line. Do not modify any code."
)


def _project_root(session: AgentSession) -> str | None:
    return session.record.project_root_snapshot


def _project_required(session: AgentSession) -> str:
    root = _project_root(session)
    if not root:
        raise InvalidUsageError(
            "This command needs a project session",
            hint="Start inside a project or adopt one first.",
        )
    return root


def handle(session: AgentSession, console: Console, message: str) -> SlashOutcome:
    parts = message.split()
    command = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 and parts[1].strip() else None
    rest = message.partition(" ")[2].strip()

    if command in ("/exit", "/quit"):
        return _EXIT
    if command == "/help":
        console.print(_HELP)
        return _STAY
    if command == "/attach":
        if not rest:
            raise InvalidUsageError("Usage: /attach <path>")
        from rinari.artifacts.attachments import prepare_attachments

        options = {}
        if rest.startswith("--ocr "):
            options["ocr"] = True
            rest = rest[6:].strip()
        elif rest.startswith("--vision "):
            session.context.allow_unconfirmed_vision = True
            rest = rest[9:].strip()
        try:
            prepared = prepare_attachments(
                session.services.artifacts,
                session.record.id,
                [{"path": rest.strip('"'), **options}],
                cancellation=session.token,
            )
        except (OSError, ValueError, RuntimeError) as exc:
            raise InvalidUsageError(f"Could not prepare attachment: {exc}") from exc
        for item in prepared:
            session.pending_attachments.append(item.reference())
            console.print(
                f"Ready: {item.name} ({item.kind}, {item.source.byte_count} bytes)", markup=False
            )
            if item.warning:
                console.print(item.warning, markup=False)
        return _STAY
    if command == "/status":
        snap = build_snapshot(session)
        for label, value in banner_fields(snap):
            console.print(f"[bold]{label:<11}[/bold]{value}")
        return _STAY
    if command == "/mode":
        record = session.record
        console.print(f"session {record.id[:12]}  kind={record.kind}  mode={record.mode}")
        return _STAY
    if command == "/provider":
        if arg is None:
            for record in session.services.providers.list():
                marker = "*" if record.id == session.record.provider_id else " "
                console.print(f" {marker} {record.alias} ({record.type})")
            return _STAY
        from rinari.cli import agent_runtime

        result = agent_runtime.switch_provider(session, arg)
        console.print(f"session now uses {result.provider_alias} / {result.model_alias or '-'}")
        return _STAY
    if command == "/model":
        if arg is None:
            for record in session.services.ctx.model_repo.list(session.record.provider_id):
                marker = "*" if record.id == session.record.model_id else " "
                console.print(f" {marker} {record.alias}  ({record.provider_model_id})")
            return _STAY
        from rinari.cli import agent_runtime

        result = agent_runtime.switch_model(session, arg)
        console.print(f"session now uses {result.provider_alias} / {result.model_alias}")
        return _STAY
    if command == "/session":
        console.print(f"session {session.record.id} ({session.record.kind})")
        return _STAY
    if command == "/usage":
        snap = build_snapshot(session)
        u = snap.usage
        cost = f"${u.cost_usd:.4f}" if u.cost_usd is not None else "unknown"
        for label, value in (
            ("model calls", str(u.model_calls)),
            ("tool calls", str(u.tool_calls)),
            ("input tokens", str(u.input_tokens) if u.input_tokens is not None else "unknown"),
            ("output tokens", str(u.output_tokens) if u.output_tokens is not None else "unknown"),
            (
                "cached tokens",
                str(u.cached_input_tokens) if u.cached_input_tokens is not None else "n/a",
            ),
            (
                "reasoning tokens",
                str(u.reasoning_tokens) if u.reasoning_tokens is not None else "n/a",
            ),
            ("elapsed", f"{u.elapsed_s:.1f}s"),
            ("cost", cost),
        ):
            console.print(f"  {label:<16} {value}")
        return _STAY
    if command == "/tokens":
        snap = build_snapshot(session)
        used = snap.context_used_tokens
        window = snap.context_window_tokens
        if used is not None and window:
            pct = f" ({snap.context_percent:.0%})" if snap.context_percent is not None else ""
            console.print(f"context {used}/{window} tokens{pct}  (character/4 estimate)")
        else:
            console.print("context —  (no window known for this model)")
        console.print(f"history messages: {len(session.context.history)}")
        return _STAY
    if command in ("/plan", "/tasks"):
        _print_tasks(session, console, arg if command == "/tasks" else None)
        return _STAY
    if command == "/diff":
        _print_diff(session, console)
        return _STAY
    if command == "/test":
        return SlashOutcome("turn", prompt=_TEST_PROMPT)
    if command == "/review":
        return SlashOutcome("turn", prompt=_REVIEW_PROMPT)
    if command == "/skills":
        _print_skills(session, console, arg)
        return _STAY
    if command == "/tools":
        _print_tools(session, console)
        return _STAY
    if command == "/agents":
        if session.orchestrator is None:
            console.print("multi-agent not available in this session")
            return _STAY
        agents = session.orchestrator.list()
        if not agents:
            console.print("no subagents started yet (use the agent.spawn tool)")
            return _STAY
        for item in agents:
            task = f" task={item['task_id']}" if item.get("task_id") else ""
            console.print(
                f"  {item.get('state', '?'):<10} {item.get('agent', '?'):<14} "
                f"id={item.get('id', '?')}{task}"
            )
        return _STAY
    if command == "/permissions":
        snap = build_snapshot(session)
        console.print(f"  profile   {snap.profile}")
        console.print(f"  network   {snap.network_mode or 'unknown'}")
        console.print("  approvals ask per action; grants: once / session / project / persistent")
        return _STAY
    if command == "/checkpoint":
        _print_checkpoints(session, console)
        return _STAY
    if command == "/undo":
        return _do_undo(session, console)
    if command == "/compact":
        return _do_compact(session, console)
    if command == "/context":
        _print_context(session, console)
        return _STAY
    if command == "/trace":
        n = int(arg) if arg and arg.isdigit() else 15
        _print_trace(session, console, n)
        return _STAY
    if command == "/new":
        return SlashOutcome("new_session")
    if command == "/resume":
        return SlashOutcome("resume_session", resume_ref=arg)
    raise InvalidUsageError(f"Unknown command: {command}", hint="Try /help")


def _print_tasks(session: AgentSession, console: Console, filter_arg: str | None) -> None:
    root = _project_required(session)
    tasks = session.services.tasks.list(root)
    if filter_arg:
        needle = filter_arg.lower()
        tasks = [t for t in tasks if needle in t["title"].lower() or needle in t["id"].lower()]
    if not tasks:
        console.print("no tasks for this project (rinari tasks add <title>)")
        return
    from rinari.tasks import core

    depths = core.tree_depths(tasks)
    for task in tasks:
        pad = "  " * depths[task["id"]]
        console.print(f"{pad}[{task['status']:<9}] {task['id']}  {task['title']}")


def _print_diff(session: AgentSession, console: Console) -> None:
    root = _project_required(session)
    out = subprocess.run(
        ["git", "-C", root, "diff", "--stat"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if out.returncode != 0:
        raise InvalidUsageError("git diff failed", hint=out.stderr.strip() or None)
    if not out.stdout.strip():
        console.print("no uncommitted changes")
        return
    console.print(out.stdout.rstrip())
    full = subprocess.run(
        ["git", "-C", root, "diff", "-U3"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    body = full.stdout.strip()
    limit = 4000
    if len(body) > limit:
        body = body[:limit] + f"\n… ({len(body) - limit} more characters)"
    if body:
        console.print(body)


def _print_skills(session: AgentSession, console: Console, query: str | None) -> None:
    services = session.services
    root = _project_root(session)
    if query:
        rows = services.skills.search(query, root)
        if not rows:
            console.print(f"no skill matches {query!r}")
            return
    else:
        rows = services.skills.summaries(root, session=session.record)
    for row in rows:
        active = "*" if row["active"] else " "
        console.print(f" {active} {row['name']:<20} {row['risk']:<8} {row['description'][:60]}")


def _print_tools(session: AgentSession, console: Console) -> None:
    registry = session.loop._tools.registry
    items = []
    for name in registry.names():
        tool = registry.get(name)
        risk = getattr(getattr(tool, "definition", None), "risk", None)
        items.append((name, risk.value if risk is not None else "?"))
    for name, risk in items:
        console.print(f"  {name:<28} {risk}")


def _print_checkpoints(session: AgentSession, console: Console) -> None:
    root = _project_required(session)
    rows = session.services.checkpoints.list(root)[-5:]
    if not rows:
        console.print("no checkpoints yet (auto-created at turn boundaries)")
        return
    for row in rows:
        label = f"  {row['label']}" if row.get("label") else ""
        console.print(f"  {row['id']}  {row.get('created_at', '')}{label}")


def _do_undo(session: AgentSession, console: Console) -> SlashOutcome:
    root = _project_required(session)
    preview = session.services.checkpoints.restore(root, checkpoint_id=None, preview=True)
    applied = preview.get("applied") or []
    skipped = preview.get("skipped") or []
    if not applied and not skipped:
        console.print("nothing to undo (no agent-owned changes recorded)")
        return _STAY
    console.print(f"would restore {len(applied)} path(s):")
    for path in applied:
        console.print(f"  restored  {path}")
    for item in skipped:
        console.print(f"  skipped   {item['path']}  ({item['reason']})")
    answer = typer.prompt("Proceed?", default="n").strip().lower()
    if answer not in ("y", "yes"):
        console.print("cancelled")
        return _STAY
    result = session.services.checkpoints.restore(root, checkpoint_id=None, preview=False)
    console.print(f"restored {result['checkpoint_id']}: {len(result['applied'])} path(s)")
    return _STAY


def _do_compact(session: AgentSession, console: Console) -> SlashOutcome:
    services = session.services
    window = None
    try:
        window = session.loop._provider.capabilities().max_context_tokens
    except Exception:
        window = None
    from rinari.context import tokens
    from rinari.prompts.assembler import PromptAssembler

    record = session.record
    pressure = None
    estimated = tokens.estimate_tokens(
        system_prompt=" "
        * len(PromptAssembler().build(session.context.assembler_base).system_prompt),
        history=session.context.history,
    )
    window_resolved = tokens.resolve_context_window(window)
    if window_resolved:
        pressure = tokens.pressure(estimated, window_resolved)
    if pressure is None:
        console.print("compaction pressure unknown (no context window known)")
        return _STAY
    threshold = tokens.PRESSURE_COMPACT
    if pressure < threshold:
        console.print(
            f"pressure {pressure:.0%} < compact threshold {threshold:.0%} — nothing to compact"
        )
        return _STAY
    did = services.context.maybe_compact(
        session.context, session_id=record.id, window_tokens=window
    )
    console.print(
        "compacted: yes (dropped to the kept tail, task state preserved)"
        if did
        else "pressure above threshold but no droppable history"
    )
    return _STAY


def _print_context(session: AgentSession, console: Console) -> None:
    from rinari.prompts.assembler import PromptAssembler

    bundle = PromptAssembler().build(session.context.assembler_base)
    total = 0
    for seg in bundle.segments:
        total += seg.length
        console.print(f"  {seg.id:<16} {seg.length:>7} chars  {seg.kind:<12} trust={seg.trust}")
    console.print(f"  {'(system total)':<16} {total:>7} chars")
    session_len = len("".join(_content(m) for m in session.context.history))
    console.print(
        f"  history              {session_len:>7} chars ({len(session.context.history)} messages)"
    )
    if session.context.compact_state_text:
        console.print(
            f"  compact-state        {len(session.context.compact_state_text):>7} chars (active)"
        )


def _content(message) -> str:
    content = getattr(message, "content", "")
    return content if isinstance(content, str) else ""


def _print_trace(session: AgentSession, console: Console, n: int) -> None:
    events = session.services.ctx.event_repo.list(session.record.id)
    events = events[-n:]
    if not events:
        console.print("no events recorded yet")
        return
    for event in events:
        detail = ""
        payload = event.payload or {}
        for key in ("tool", "agent", "agent_id", "outcome", "error"):
            if key in payload:
                detail = f" {payload[key]}"
                break
        console.print(f"  #{event.seq:<4} {event.type}{detail}")


__all__ = ["SlashOutcome", "handle"]
