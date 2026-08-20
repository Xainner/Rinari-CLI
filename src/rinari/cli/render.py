"""CLI presenters: renderer selection, banner, status rail, approval panel
(phase 7).

Presentation only: every metric here comes from a `RuntimeSnapshot` (or an
`ApprovalRequest`) — values the runtime does not know are shown as `—`,
never invented (AGENTS.md 22).
"""

from __future__ import annotations

import os
from enum import Enum
from typing import TYPE_CHECKING

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

if TYPE_CHECKING:  # pragma: no cover
    from rinari.cli.snapshot import RuntimeSnapshot
    from rinari.policy.approvals import ApprovalRequest

ART = (
    " ____   ____ ____  ____  ",
    "/ ___| / ___/ ___||  _ \\ ",
    "\\___ \\\\ |  | |__  | |_) |",
    " ___) | |__| ___| |  _ < ",
    "|____/ \\____|_|   |_| \\_\\",
)

ART_TEXT = "\n".join(ART).rstrip()

UNKNOWN = "—"


class RendererMode(Enum):
    RICH = "rich"
    COMPACT = "compact"
    PLAIN = "plain"
    JSON = "json"
    JSON_STREAM = "json_stream"


def detect_mode(
    *,
    interactive: bool,
    json_flag: bool,
    env: dict[str, str] | None = None,
) -> tuple[RendererMode, bool, bool]:
    """(mode, no_color, banner_allowed) from flags + environment.

    `--json` wins for machine consumers; piped output degrades to plain;
    `TERM=dumb` forces plain; `NO_COLOR` strips styling but keeps layout.
    """
    env = env if env is not None else dict(os.environ)
    no_color = "NO_COLOR" in env
    term_dumb = env.get("TERM", "") == "dumb"

    if json_flag:
        mode = RendererMode.JSON if interactive else RendererMode.JSON_STREAM
        return mode, no_color, False
    forced = env.get("RINARI_RENDERER", "").strip().lower()
    if forced == "plain" or term_dumb:
        return RendererMode.PLAIN, no_color, False
    if not interactive:
        return RendererMode.PLAIN, True, True
    if forced == "compact" or no_color:
        return RendererMode.COMPACT, True, True
    return RendererMode.RICH, no_color, True


def make_console(mode: RendererMode, no_color: bool, *, width: int | None = None) -> Console:
    return Console(no_color=no_color, width=width if width and width > 0 else None)


def _join(*parts: str | None) -> str:
    parts = [p for p in parts if p]
    return " ".join(parts) if parts else UNKNOWN


def banner_fields(snap: RuntimeSnapshot) -> list[tuple[str, str]]:
    model = _join(
        snap.model_alias, f"({snap.provider_model_id})" if snap.provider_model_id else None
    )
    if snap.context_used_tokens is not None and snap.context_window_tokens:
        context = f"{snap.context_used_tokens}/{snap.context_window_tokens} tokens"
        if snap.context_percent is not None and snap.context_percent > 0:
            context += f" ({snap.context_percent:.0%})"
    else:
        context = UNKNOWN
    project: str
    if snap.project_name:
        project = _join(snap.project_name, f"[{snap.branch}]" if snap.branch else None)
        if snap.dirty is True:
            project += " *"
    else:
        project = UNKNOWN
    skills = ", ".join(snap.skills_active) if snap.skills_active else "- none"
    running = [
        a
        for a in snap.agents
        if a.get("state") not in ("completed", "failed", "cancelled", "budget")
    ]
    if running:
        agents = f"{len(running)} running (" + ", ".join(a.get("agent", "?") for a in running) + ")"
    elif snap.agents:
        agents = f"{len(snap.agents)} finished"
    else:
        agents = "- none"
    cost = f"${snap.usage.cost_usd:.4f}" if snap.usage.cost_usd is not None else UNKNOWN
    return [
        ("session", f"{snap.session_id[:12]}  {snap.session_kind.lower()} · {snap.session_state}"),
        ("mode", snap.mode),
        (
            "provider",
            _join(snap.provider_alias, f"({snap.provider_type})")
            if snap.provider_alias
            else UNKNOWN,
        ),
        ("model", model),
        ("reasoning", snap.reasoning_effort or UNKNOWN),
        ("context", context),
        ("profile", snap.profile),
        ("project", project),
        ("tools", str(snap.tools_loaded)),
        ("skills", skills),
        ("agents", agents),
        ("network", snap.network_mode or UNKNOWN),
        ("usage", f"{snap.usage.model_calls} model · {snap.usage.tool_calls} tools · {cost}"),
    ]


def render_banner(console: Console, snap: RuntimeSnapshot, mode: RendererMode) -> None:
    fields = banner_fields(snap)
    if mode is RendererMode.PLAIN:
        for label, value in fields:
            console.print(f"{label:<10} {value}")
        return
    body = Text()
    if mode is RendererMode.RICH:
        body.append(ART_TEXT)
        body.append("\n\n")
    for label, value in fields:
        body.append(f"{label:<11}", style="bold")
        body.append(f"{value}\n")
    header = Text(f" Rinari v{snap.version}", style="bold magenta")
    console.print(Panel(body, title=header, expand=False, padding=(0, 1)))


def status_line(snap: RuntimeSnapshot, *, turn_kind: str, active_tool: str | None = None) -> str:
    """One dim line after each turn: state + the few numbers that exist."""
    kind = turn_kind
    context = ""
    if snap.context_percent is not None and snap.context_window_tokens:
        context = f" ctx {snap.context_percent:.0%}"
    usage = f" {snap.usage.model_calls}m/{snap.usage.tool_calls}t"
    cost = f" ${snap.usage.cost_usd:.4f}" if snap.usage.cost_usd is not None else ""
    elapsed = f" {snap.usage.elapsed_s:.1f}s"
    agents = ""
    running = [
        a
        for a in snap.agents
        if a.get("state") not in ("completed", "failed", "cancelled", "budget")
    ]
    if running:
        agents = f" {len(running)} agents"
    tool = f" {active_tool}" if active_tool else ""
    line = f"{kind}{context}{usage}{cost}{elapsed}{agents}{tool}"
    return line.strip()


def approval_lines(request: ApprovalRequest) -> list[str]:
    target = f" {request.target}" if request.target else ""
    risk = request.risk or "medium"
    lines = [
        f"action  {request.capability}{target}",
        f"reason  {request.description or UNKNOWN}",
        f"risk    {risk}" + ("  ⚠ critical" if risk == "high" else ""),
    ]
    return lines


def render_approval(console: Console, request: ApprovalRequest) -> None:
    body = Text("\n".join(approval_lines(request)))
    critical = (request.risk or "medium") == "high"
    title = Text(" approval required ", style="bold red" if critical else "bold yellow")
    console.print(Panel(body, title=title, expand=False, padding=(0, 1)))
    console.print(
        Text(
            "  [y]es once · [s]ession · [p]roject · [a]lways · [n]o (default)",
            style="dim",
        )
    )


def json_stream_event(event_type: str, **payload: object) -> str:
    import json

    doc = {"type": event_type}
    doc.update(payload)
    return json.dumps(doc, sort_keys=True, default=str)


__all__ = [
    "ART",
    "UNKNOWN",
    "RendererMode",
    "approval_lines",
    "banner_fields",
    "detect_mode",
    "json_stream_event",
    "make_console",
    "render_approval",
    "render_banner",
    "status_line",
]
