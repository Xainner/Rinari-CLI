"""HookEngine: deterministic dispatch of lifecycle hook declarations.

A declaration names an event and a handler (`python` dotted reference or
`shell` command). The engine:

- orders hooks deterministically: (source rank, name);
- enforces trust (project-scope hooks only run for a trusted project);
- enforces capabilities (shell handlers must declare `shell.exec`);
- bounds each handler (timeout) and never lets a failure propagate:
  every outcome is recorded in the returned list (and, via `on_outcome`,
  in the session trace).

Handlers are *advisory observers*: their output is captured but cannot
alter tool execution or model flow.
"""

from __future__ import annotations

import json
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .events import ALL_EVENTS, HANDLER_TYPES

# Lower rank runs first: user config < project < plugins (third-party last).
_SOURCE_RANK = {"user": 10, "project": 20}
_PLUGIN_RANK = 30

DEFAULT_TIMEOUT_S = 10.0
MAX_OUTPUT_CHARS = 4096


class HookError(Exception):
    """Structured hook declaration problem (code + message)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        # HOOK_EVENT_INVALID | HOOK_HANDLER_INVALID | CAPABILITY_REQUIRED | TRUST_REQUIRED
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class HookDeclaration:
    name: str
    event: str
    handler_type: str
    handler: str  # python: "module:function" | shell: command line
    source: str  # "user" | "project" | "plugin:<name>"
    capabilities: tuple[str, ...] = ()
    risk: str = "low"
    scope: str = "global"

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.scope, self.source, self.name)

    def order_key(self) -> tuple[int, str]:
        if self.source.startswith("plugin:"):
            rank = _PLUGIN_RANK
        else:
            rank = _SOURCE_RANK.get(self.source, 20)
        return (rank, self.name)

    @classmethod
    def parse(cls, raw: dict, source: str, scope: str = "global") -> HookDeclaration:
        name = str(raw.get("name") or "")
        event = str(raw.get("event") or "")
        handler_type = str(raw.get("handler_type") or "")
        handler = str(raw.get("handler") or raw.get("command") or "")
        if not name or not event:
            raise HookError("HOOK_HANDLER_INVALID", "each hook needs 'name' and 'event'")
        if event not in ALL_EVENTS:
            raise HookError("HOOK_EVENT_INVALID", f"unknown event: {event!r}")
        if handler_type not in HANDLER_TYPES:
            raise HookError("HOOK_HANDLER_INVALID", f"unknown handler_type: {handler_type!r}")
        if not handler:
            detail = "command" if handler_type == "shell" else "handler"
            raise HookError("HOOK_HANDLER_INVALID", f"missing {detail} for hook {name!r}")
        caps = raw.get("capabilities") or ()
        if not isinstance(caps, (list, tuple)) or not all(isinstance(c, str) for c in caps):
            raise HookError("HOOK_HANDLER_INVALID", "'capabilities' must be a list of strings")
        return cls(
            name=name,
            event=event,
            handler_type=handler_type,
            handler=handler,
            source=source,
            capabilities=tuple(sorted(set(caps))),
            risk=str(raw.get("risk") or "low"),
            scope=scope,
        )

    def to_row(self) -> dict:
        return {
            "name": self.name,
            "event": self.event,
            "source": self.source,
            "scope": self.scope,
            "handler_type": self.handler_type,
            "handler": self.handler,
            "capabilities": list(self.capabilities),
            "risk": self.risk,
        }


@dataclass(frozen=True, slots=True)
class HookOutcome:
    name: str
    source: str
    event: str
    ok: bool
    output: str
    error: str
    duration_ms: float

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "source": self.source,
            "event": self.event,
            "ok": self.ok,
            "output": self.output,
            "error": self.error,
            "duration_ms": round(self.duration_ms, 1),
        }


class HookEngine:
    def __init__(self, trust: object | None = None, timeout_s: float = DEFAULT_TIMEOUT_S) -> None:
        self._declarations: list[HookDeclaration] = []
        self._trust = trust
        self._timeout_s = timeout_s
        self.on_outcome: Callable[[HookOutcome], None] | None = None

    # -- registration ---------------------------------------------------------

    def add(self, declaration: HookDeclaration) -> None:
        self._declarations.append(declaration)

    def add_all(self, declarations: list[HookDeclaration]) -> None:
        self._declarations.extend(declarations)

    @property
    def declarations(self) -> list[HookDeclaration]:
        return sorted(self._declarations, key=lambda d: (d.event, d.order_key()))

    def matching(self, event: str) -> list[HookDeclaration]:
        return sorted(
            (d for d in self._declarations if d.event == event), key=lambda d: d.order_key()
        )

    # -- dispatch ----------------------------------------------------------------

    def emit(self, event: str, payload: dict, *, project: Path | None = None) -> list[HookOutcome]:
        outcomes: list[HookOutcome] = []
        for declaration in self.matching(event):
            if not self._allowed(declaration, project):
                outcomes.append(self._skip(declaration, event, "TRUST_REQUIRED"))
                continue
            outcome = self._run(declaration, event, payload)
            outcomes.append(outcome)
            if self.on_outcome is not None:
                with _suppress():
                    self.on_outcome(outcome)
        return outcomes

    def _allowed(self, declaration: HookDeclaration, project: Path | None) -> bool:
        if declaration.source == "project":
            return (
                project is not None and self._trust is not None and self._trust.is_trusted(project)
            )
        return True

    def _skip(self, declaration: HookDeclaration, event: str, code: str) -> HookOutcome:
        return HookOutcome(
            name=declaration.name,
            source=declaration.source,
            event=event,
            ok=False,
            output="",
            error=code,
            duration_ms=0.0,
        )

    # -- handler execution ---------------------------------------------------------

    def _run(self, declaration: HookDeclaration, event: str, payload: dict) -> HookOutcome:
        started = _monotonic()
        capabilities = set(declaration.capabilities)
        if declaration.handler_type == "shell" and "shell.exec" not in capabilities:
            return HookOutcome(
                name=declaration.name,
                source=declaration.source,
                event=event,
                ok=False,
                output="",
                error="CAPABILITY_REQUIRED:shell.exec",
                duration_ms=0.0,
            )
        try:
            output = self._invoke(declaration, payload)
        except _HookTimeout:
            return self._finish(
                declaration,
                event,
                started,
                ok=False,
                output="",
                error=f"TIMEOUT after {self._timeout_s}s",
            )
        except Exception as exc:  # a hook can never take the session down
            return self._finish(
                declaration,
                event,
                started,
                ok=False,
                output="",
                error=f"{exc.__class__.__name__}: {exc}",
            )
        text = output if isinstance(output, str) else json.dumps(output, default=str)
        return self._finish(
            declaration,
            event,
            started,
            ok=True,
            output=text[:MAX_OUTPUT_CHARS],
            error="",
        )

    def _invoke(self, declaration: HookDeclaration, payload: dict) -> str:
        if declaration.handler_type == "python":
            return _run_python(declaration.handler, payload, self._timeout_s)
        return _run_shell(declaration.handler, payload, self._timeout_s)

    @staticmethod
    def _finish(
        declaration: HookDeclaration,
        event: str,
        started: float,
        *,
        ok: bool,
        output: str,
        error: str,
    ) -> HookOutcome:
        return HookOutcome(
            name=declaration.name,
            source=declaration.source,
            event=event,
            ok=ok,
            output=output,
            error=error,
            duration_ms=(_monotonic() - started) * 1000.0,
        )


class _HookTimeout(Exception):
    pass


class _suppress:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc_info: object) -> bool:
        return True


def _monotonic() -> float:
    import time

    return time.monotonic()


def _run_python(handler_ref: str, payload: dict, timeout_s: float) -> str:
    if ":" not in handler_ref:
        module_name, function_name = handler_ref, "main"
    else:
        module_name, function_name = handler_ref.rsplit(":", 1)
    import importlib

    module = importlib.import_module(module_name)
    func = getattr(module, function_name)
    if not callable(func):
        raise HookError("HOOK_HANDLER_INVALID", f"{handler_ref} is not callable")
    result_holder: list[object] = []
    error_holder: list[BaseException] = []
    finished = threading.Event()

    def target() -> None:
        try:
            result_holder.append(func(payload))
        except BaseException as exc:  # captured, re-raised below
            error_holder.append(exc)
        finally:
            finished.set()

    thread = threading.Thread(target=target, name=f"hook-{handler_ref}", daemon=True)
    thread.start()
    if not finished.wait(timeout=timeout_s):
        raise _HookTimeout
    if error_holder:
        raise error_holder[0]
    value = result_holder[0] if result_holder else None
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, default=str)


def _run_shell(command: str, payload: dict, timeout_s: float) -> str:
    try:
        completed = subprocess.run(
            command,
            shell=True,
            input=json.dumps(payload, default=str),
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as exc:
        raise _HookTimeout from exc
    stdout = completed.stdout or ""
    if completed.returncode != 0:
        stderr = (completed.stderr or "").strip()
        raise RuntimeError(f"exit {completed.returncode}: {stderr[:512]}")
    return stdout


__all__ = ["HookDeclaration", "HookEngine", "HookError", "HookOutcome"]
