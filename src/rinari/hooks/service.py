"""HookService: discovery (files + plugins), state, engine, CLI facade.

Declarations are discovered, not hardcoded:

- user scope:  `~/.rinari/hooks.json`          (always available)
- project:    `<root>/.rinari/hooks.json`      (requires project trust)
- plugins:    contributed via `PluginAPI.add_hook` at load time

Enable/disable persists per (scope, source, name) in the `hooks` table, so
`rinari hooks disable` survives restarts without touching the files.
"""

from __future__ import annotations

import json
from pathlib import Path

from rinari.application.context import AppContext
from rinari.shared.clock import now_iso
from rinari.trust import TrustService

from .engine import HookDeclaration, HookEngine, HookOutcome
from .events import ALL_EVENTS


def _now(ctx: AppContext) -> str:
    return now_iso(ctx.clock)


def _project_for(scope: str) -> Path | None:
    if scope != "project":
        return None
    from rinari.projects.detector import detect_project

    return detect_project(Path.cwd(), Path.home()).project_root


class HookService:
    def __init__(self, ctx: AppContext, trust: TrustService) -> None:
        self._ctx = ctx
        self._trust = trust

    # -- discovery ----------------------------------------------------------------

    def user_hooks_file(self) -> Path:
        return self._ctx.layout.dir("hooks.json")

    def project_hooks_file(self, root: Path) -> Path:
        return Path(root) / ".rinari" / "hooks.json"

    def discover(self, project: Path | None = None) -> list[HookDeclaration]:
        declarations: list[HookDeclaration] = []
        diagnostics: list[dict] = []
        user_file = self.user_hooks_file()
        for source, path, scope in (
            ("user", user_file, "global"),
            (
                "project",
                self.project_hooks_file(project) if project is not None else None,
                "project",
            ),
        ):
            if path is None or not path.is_file():
                continue
            entries = _load_entries(path, diagnostics)
            for raw in entries:
                try:
                    declarations.append(HookDeclaration.parse(raw, source, scope))
                except Exception as exc:
                    diagnostics.append(
                        {
                            "source": source,
                            "code": getattr(exc, "code", "HOOK_INVALID"),
                            "message": str(exc),
                        }
                    )
        return declarations

    # -- state (DB) ------------------------------------------------------------------

    def list(
        self, project: Path | None = None, plugin_hooks: list[tuple[str, str]] | None = None
    ) -> list[dict]:
        rows: list[dict] = []
        for decl in self.discover(project):
            state = self._ctx.hook_repo.find(decl.name, decl.source, decl.scope)
            row = decl.to_row()
            row["enabled"] = True if state is None else bool(state.get("enabled"))
            rows.append(row)
        for source, (event, name) in (plugin_hooks or {}).items():
            state = self._ctx.hook_repo.find(name, source, "global")
            rows.append(
                {
                    "name": name,
                    "event": event,
                    "source": source,
                    "scope": "global",
                    "handler_type": "plugin",
                    "handler": "(in-process)",
                    "capabilities": [],
                    "risk": "low",
                    "enabled": True if state is None else bool(state.get("enabled")),
                }
            )
        return rows

    def _state(self, decl: HookDeclaration) -> bool:
        row = self._ctx.hook_repo.find(decl.name, decl.source, decl.scope)
        return True if row is None else bool(row.get("enabled"))

    def _ensure_row(self, decl: HookDeclaration | None, name: str, source: str, scope: str) -> None:
        """Create a baseline state row so enable/disable work without pre-insertion."""
        if self._ctx.hook_repo.find(name, source, scope) is not None:
            return
        self._ctx.hook_repo.add(
            self._ctx.ids.new("hk"),
            name=name,
            event=decl.event if decl else "",
            source=source,
            scope=scope,
            handler_type=decl.handler_type if decl else "",
            handler_ref=decl.handler if decl else "",
            capabilities_json=json.dumps(sorted(set(decl.capabilities)) if decl else []),
            risk=decl.risk if decl else "low",
            created_at=_now(self._ctx),
        )

    def enable(self, name: str, source: str, scope: str = "global") -> dict | None:
        decl = next(
            (
                d
                for d in self.discover(_project_for(scope))
                if d.name == name and d.source == source
            ),
            None,
        )
        self._ensure_row(decl, name, source, scope)
        self._ctx.hook_repo.set_enabled(name, source, scope, True, _now(self._ctx))
        return self._ctx.hook_repo.find(name, source, scope)

    def disable(self, name: str, source: str, scope: str = "global") -> dict | None:
        decl = next(
            (
                d
                for d in self.discover(_project_for(scope))
                if d.name == name and d.source == source
            ),
            None,
        )
        self._ensure_row(decl, name, source, scope)
        self._ctx.hook_repo.set_enabled(name, source, scope, False, _now(self._ctx))
        return self._ctx.hook_repo.find(name, source, scope)

    # -- engine -------------------------------------------------------------------------

    def build_engine(
        self,
        project: Path | None = None,
        plugin_hooks: list[tuple[str, object]] | None = None,
        trace_sink: object | None = None,
    ) -> HookEngine:
        engine = HookEngine(trust=self._trust)
        for decl in self.discover(project):
            if not self._state(decl):
                continue
            engine.add(decl)
        for source, handler in plugin_hooks or []:
            if self._disabled_plugin_hook(source, handler):
                continue
            engine.add(_plugin_declaration(source, handler))
        if trace_sink is not None:
            engine.on_outcome = lambda outcome: trace_sink(outcome.to_dict())
        return engine

    def _disabled_plugin_hook(self, source: str, handler: object) -> bool:
        name = getattr(handler, "__name__", None) or type(handler).__name__
        row = self._ctx.hook_repo.find(name, source, "global")
        return row is not None and not bool(row.get("enabled"))

    # -- CLI support --------------------------------------------------------------------------

    def show(
        self, name: str, source: str, scope: str = "global", project: Path | None = None
    ) -> dict | None:
        for decl in self.discover(project):
            if decl.name == name and decl.source == source:
                row = decl.to_row()
                row["enabled"] = self._state(decl)
                return row
        return None

    def test(self, event: str, payload: dict | None, project: Path | None = None) -> list[dict]:
        if event not in ALL_EVENTS:
            raise ValueError(f"unknown event: {event!r}")
        engine = self.build_engine(project)
        outcomes: list[HookOutcome] = engine.emit(event, payload or {}, project=project)
        return [o.to_dict() for o in outcomes]

    def doctor(self, project: Path | None = None) -> list[dict]:
        report: list[dict] = []
        diagnostics: list[dict] = []
        user_file = self.user_hooks_file()
        for source, path, scope in (
            ("user", user_file, "global"),
            (
                "project",
                self.project_hooks_file(project) if project is not None else None,
                "project",
            ),
        ):
            if path is None:
                continue
            if not path.is_file():
                report.append({"file": str(path), "status": "absent", "hooks": 0})
                continue
            entries = _load_entries(path, diagnostics)
            good = 0
            for raw in entries:
                try:
                    HookDeclaration.parse(raw, source, scope)
                    good += 1
                except Exception as exc:
                    diagnostics.append(
                        {
                            "source": source,
                            "code": getattr(exc, "code", "HOOK_INVALID"),
                            "message": str(exc),
                        }
                    )
            report.append({"file": str(path), "status": "ok", "hooks": good})
        trusted = project is not None and self._trust.is_trusted(project)
        report.append(
            {
                "file": None,
                "status": "ok",
                "project_trusted": trusted if project is not None else None,
            }
        )
        if diagnostics:
            report.append({"file": None, "status": "errors", "diagnostics": diagnostics})
        return report


def _plugin_declaration(source: str, handler: object) -> HookDeclaration:
    name = getattr(handler, "__name__", None) or type(handler).__name__
    event = getattr(handler, "_rinari_hook_event", "PostToolUse")
    return HookDeclaration(
        name=name,
        event=event,
        handler_type="python",
        handler=source,
        source=source,
        risk="low",
        scope="global",
    )


def _load_entries(path: Path, diagnostics: list[dict]) -> list[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        diagnostics.append({"source": str(path), "code": "MANIFEST_INVALID", "message": str(exc)})
        return []
    if isinstance(data, dict):
        data = data.get("hooks") or []
    if not isinstance(data, list):
        diagnostics.append(
            {
                "source": str(path),
                "code": "MANIFEST_INVALID",
                "message": "hooks.json must be a list",
            }
        )
        return []
    return [e for e in data if isinstance(e, dict)]


__all__ = ["HookService"]
