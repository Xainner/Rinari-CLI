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

from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

if TYPE_CHECKING:  # pragma: no cover
    from rinari.cli.snapshot import RuntimeSnapshot
    from rinari.policy.approvals import ApprovalRequest

# Block-letter art for RINARI: hand-set 5-row glyphs (fixed per letter),
# rendered in the single UI accent color. Keep rows of each glyph aligned.
_ART_LETTERS: dict[str, tuple[str, ...]] = {
    "R": (
        "█████ ",
        "█    █",
        "█████ ",
        "█  █  ",
        "█   ██",
    ),
    "I": (
        "█████",
        "  █  ",
        "  █  ",
        "  █  ",
        "█████",
    ),
    "N": (
        "█    █",
        "██   █",
        "█ █  █",
        "█  █ █",
        "█   ██",
    ),
    "A": (
        " ████ ",
        "█    █",
        "██████",
        "█    █",
        "█    █",
    ),
}

_ART_WORDS = "RINARI"

_ACCENT = "bright_magenta"
_BORDER = "magenta"
_MUTED_BORDER = "bright_black"
_WIDE_BANNER = 104
_CARD_ROW = 116

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


def banner_art() -> list[Text]:
    """`RINARI` as big block letters in the accent color, one Text per row."""
    rows: list[Text] = [Text() for _ in range(len(next(iter(_ART_LETTERS.values()))))]
    for index, letter in enumerate(_ART_WORDS):
        for row, row_text in enumerate(_ART_LETTERS[letter]):
            rows[row].append(row_text, style=_ACCENT)
            if index < len(_ART_WORDS) - 1:
                rows[row].append("  ")
    return rows


def _field_grid(rows: list[tuple[str, str]], *, expand: bool = True) -> Table:
    grid = Table.grid(expand=expand, padding=(0, 1), pad_edge=False)
    grid.add_column(style=f"bold {_ACCENT}", no_wrap=True)
    grid.add_column(ratio=1, overflow="fold")
    for label, value in rows:
        # Values are runtime data, not Rich markup (a Git branch may contain brackets).
        grid.add_row(label, Text(value))
    return grid


def _section(title: str, rows: list[tuple[str, str]], *, border: str = _MUTED_BORDER) -> Panel:
    return Panel(
        _field_grid(rows),
        title=Text(f" {title} ", style=f"bold {_ACCENT}"),
        title_align="left",
        border_style=border,
        padding=(0, 1),
    )


def _usage_rows(snap: RuntimeSnapshot) -> list[tuple[str, str]]:
    tokens = snap.usage.tokens_total
    return [
        ("model calls", str(snap.usage.model_calls)),
        ("tool calls", str(snap.usage.tool_calls)),
        ("tokens", f"{tokens:,}" if tokens is not None else UNKNOWN),
        ("cost", f"${snap.usage.cost_usd:.4f}" if snap.usage.cost_usd is not None else UNKNOWN),
    ]


def _session_strip(snap: RuntimeSnapshot) -> Text:
    strip = Text()
    strip.append("SESSION  ", style=f"bold {_ACCENT}")
    strip.append(snap.session_id[:12])
    strip.append("  ·  ", style="dim")
    strip.append(snap.session_kind.upper(), style="bold")
    strip.append("  ·  ", style="dim")
    state_style = "green" if snap.session_state == "active" else None
    strip.append(snap.session_state.lower(), style=state_style)
    return strip


def _summary_cards(fields: dict[str, str], snap: RuntimeSnapshot, *, horizontal: bool):
    workspace = _section(
        "WORKSPACE",
        [
            ("project", fields["project"]),
            ("profile", fields["profile"]),
            ("network", fields["network"]),
        ],
    )
    capabilities = _section(
        "CAPABILITIES",
        [
            ("context", fields["context"]),
            ("tools", f"{fields['tools']} loaded"),
            ("skills", fields["skills"]),
            ("agents", fields["agents"]),
        ],
    )
    usage = _section("USAGE", _usage_rows(snap))
    if not horizontal:
        return Group(workspace, capabilities, usage)

    row = Table.grid(expand=True, padding=(0, 1), pad_edge=False)
    row.add_column(ratio=1)
    row.add_column(ratio=1)
    row.add_column(ratio=1)
    row.add_row(workspace, capabilities, usage)
    return row


def _rich_hero(console: Console, fields: dict[str, str], snap: RuntimeSnapshot) -> Panel:
    runtime = Group(
        Text("RUNTIME", style=f"bold {_ACCENT}"),
        _field_grid(
            [
                ("mode", fields["mode"]),
                ("provider", fields["provider"]),
                ("model", fields["model"]),
                ("reasoning", fields["reasoning"]),
            ]
        ),
    )

    if console.width >= _WIDE_BANNER:
        brand = Group(
            *banner_art(),
            Text("AI engineering companion", style=_ACCENT),
        )
        body = Table.grid(expand=True, padding=(0, 3), pad_edge=False)
        body.add_column(ratio=3)
        body.add_column(ratio=2)
        body.add_row(brand, runtime)
    else:
        body = Group(
            Text("RINARI", style=f"bold {_ACCENT}"),
            Text("AI engineering companion", style=_ACCENT),
            Text(""),
            runtime,
        )

    return Panel(
        Group(body, Text(""), _session_strip(snap), Text(extensions_line(snap), style="dim")),
        title=Text(" RINARI ", style=f"bold {_ACCENT}"),
        subtitle=Text(f" v{snap.version} ", style=_ACCENT),
        title_align="left",
        subtitle_align="right",
        border_style=_BORDER,
        padding=(1, 2),
    )


def _compact_banner(fields: dict[str, str], snap: RuntimeSnapshot) -> Panel:
    rows = [
        ("session", fields["session"]),
        ("mode", fields["mode"]),
        ("provider", fields["provider"]),
        ("model", fields["model"]),
        ("reasoning", fields["reasoning"]),
        ("profile", fields["profile"]),
        ("project", fields["project"]),
        ("context", fields["context"]),
        ("tools", f"{fields['tools']} loaded"),
        ("skills", fields["skills"]),
        ("agents", fields["agents"]),
        ("network", fields["network"]),
        ("usage", fields["usage"]),
    ]
    return Panel(
        Group(
            Text("RINARI", style="bold"),
            Text(f"AI engineering companion  ·  v{snap.version}", style="dim"),
            Text(""),
            _field_grid(rows),
        ),
        border_style=_MUTED_BORDER,
        padding=(0, 1),
    )


def render_banner(console: Console, snap: RuntimeSnapshot, mode: RendererMode) -> None:
    field_rows = banner_fields(snap)
    if mode is RendererMode.PLAIN:
        for label, value in field_rows:
            console.print(f"{label:<10} {value}")
        return
    fields = dict(field_rows)
    if mode is RendererMode.COMPACT:
        console.print(_compact_banner(fields, snap))
        return

    console.print(
        Group(
            _rich_hero(console, fields, snap),
            _summary_cards(fields, snap, horizontal=console.width >= _CARD_ROW),
            Text("  /help commands  ·  /status runtime details", style="dim"),
        )
    )


def status_line(
    snap: RuntimeSnapshot, *, turn_kind: str, active_tool: str | None = None, ascii_: bool = False
) -> str:
    """One dim line after each turn: state + the few numbers that exist."""
    kind = turn_kind
    context = ""
    if snap.context_percent is not None and snap.context_window_tokens:
        meter = context_meter(snap.context_percent, ascii_=ascii_)
        context = f" ctx {meter} {snap.context_percent:.0%}"
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


# -- live-state symbols (harness.md): rich glyphs with ASCII fallback ----------

SYMBOL_ACTIVE = "◆"
SYMBOL_OK = "✓"
SYMBOL_WARN = "!"
SYMBOL_FAIL = "×"  # noqa: RUF001
SYMBOL_RETRY = "↻"

_ASCII_SYMBOLS = {
    SYMBOL_ACTIVE: ">",
    SYMBOL_OK: "OK",
    SYMBOL_WARN: "!",
    SYMBOL_FAIL: "X",
    SYMBOL_RETRY: "~",
}


def symbol(glyph: str, *, ascii_: bool) -> str:
    return _ASCII_SYMBOLS.get(glyph, glyph) if ascii_ else glyph


def context_meter(percent: float | None, width: int = 8, *, ascii_: bool = False) -> str:
    """Fill bar for context usage; empty string when the percent is unknown."""
    if percent is None:
        return ""
    filled = max(0, min(width, round(percent * width)))
    on, off = ("█", "░") if not ascii_ else ("#", "-")
    return on * filled + off * (width - filled)


# -- tool rendering (harness.md #Tool Rendering) -------------------------------

_TOOL_VERB_READ = (
    "fs.read",
    "fs.read_lines",
    "fs.list",
    "fs.glob",
    "fs.stat",
    "fs.diff",
    "search.files",
    "search.regex",
    "search.symbols",
    "search.references",
    "search.hybrid",
    "context.retrieve",
    "memory.recall",
)

_TOOL_ARG_KEYS = (
    "path",
    "pattern",
    "command",
    "url",
    "href",
    "host",
    "glob",
    "query",
    "name",
    "ref",
)


def tool_verb(name: str) -> str:
    if name in _TOOL_VERB_READ or name.startswith(("fs.read", "search.", "context.retrieve")):
        return "read"
    if name.startswith("fs."):
        return "edit"
    if name.startswith(("shell.", "process.", "pty.")):
        return "run"
    if name.startswith("git."):
        return "git"
    if name.startswith(("web.", "http.")):
        return "web"
    if name.startswith("browser."):
        return "browser"
    if name.startswith("memory."):
        return "memory"
    if name.startswith("context."):
        return "context"
    if name.startswith("verify."):
        return "verify"
    return name


def tool_arg(detail: object) -> str:
    if not isinstance(detail, dict):
        return ""
    for key in _TOOL_ARG_KEYS:
        value = detail.get(key)
        if value:
            return str(value)
    return ""


def tool_label(name: str, detail: object) -> str:
    verb = tool_verb(name)
    arg = tool_arg(detail)
    return f"{verb} {arg}".strip() if arg else verb


def extensions_line(snap: RuntimeSnapshot) -> str:
    running = sum(
        1
        for a in snap.agents
        if a.get("state") not in ("completed", "failed", "cancelled", "budget")
    )
    skills = (
        f"{len(snap.skills_active)}/{snap.skills_known}"
        if snap.skills_known
        else str(len(snap.skills_active))
    )
    return "  ·  ".join(
        [
            f"tools {snap.tools_loaded}",
            f"skills {skills}",
            f"agents {running}/{len(snap.agents)}",
            f"mcp {snap.mcp_connected}",
            f"plugins {snap.plugins_loaded}",
        ]
    )


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
    critical = (request.risk or "medium") == "high"
    body = Text("\n".join(approval_lines(request)))
    body.append("\n\n")
    body.append("  [1] Allow once  ", style="bold")
    body.append("[2] Allow for session  ", style="bold")
    body.append("[3] Allow for project  ", style="bold")
    body.append("[4] Always  ", style="bold")
    body.append("[5] Deny (default)")
    title = Text(" approval required ", style="bold red" if critical else "bold yellow")
    console.print(
        Panel(
            body,
            title=title,
            border_style="red" if critical else "yellow",
            expand=False,
            padding=(0, 1),
        )
    )


def thinking_status(console: Console, label: str = "rinari thinking…", spinner: str = "dots12"):
    """A transient rich `Live` spinner for the 'thinking' phase of a turn.

    Returns a `Live` you `start()` before a turn and `stop()` when the first
    token streams (or the turn ends). `transient=True` erases it on stop so it
    never collides with streamed model output.
    """
    from rich.live import Live
    from rich.spinner import Spinner

    spin = Spinner(spinner, text=label, style=_ACCENT)
    return Live(spin, console=console, refresh_per_second=10, transient=True)


def json_stream_event(event_type: str, **payload: object) -> str:
    import json

    doc = {"type": event_type}
    doc.update(payload)
    return json.dumps(doc, sort_keys=True, default=str)


__all__ = [
    "SYMBOL_ACTIVE",
    "SYMBOL_FAIL",
    "SYMBOL_OK",
    "SYMBOL_RETRY",
    "SYMBOL_WARN",
    "UNKNOWN",
    "RendererMode",
    "approval_lines",
    "banner_art",
    "banner_fields",
    "context_meter",
    "detect_mode",
    "extensions_line",
    "json_stream_event",
    "make_console",
    "render_approval",
    "render_banner",
    "status_line",
    "symbol",
    "thinking_status",
    "tool_arg",
    "tool_label",
    "tool_verb",
]
