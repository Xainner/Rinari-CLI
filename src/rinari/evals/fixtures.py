"""Eval fixture: an isolated, deterministic harness environment per case.

Every case gets its own RINARI_HOME, work directory, fake provider/model, and
sessions. The real policy engine, sandbox, tools, loop detection and
completion gate run unmodified; only the model is scripted.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rinari.application.config import writer
from rinari.application.context import AppContext, build_app_context
from rinari.application.provider_service import AddProviderInput
from rinari.application.services import ServiceContainer, build_services
from rinari.shared.clock import Clock, FakeClock
from rinari.shared.paths import HomeLayout, ensure_layout

EVAL_PROVIDER_ALIAS = "eval"
EVAL_MODEL_ID = "eval-model"


class FixtureError(RuntimeError):
    pass


@dataclass
class EvalRun:
    """Everything an expectation can inspect after the turn(s) finished."""

    case_id: str
    turns: list = field(default_factory=list)
    events: list = field(default_factory=list)
    tools: list[dict] = field(default_factory=list)
    changed_files: list[str] = field(default_factory=list)
    outcome: str = "passed"
    error: str | None = None
    fixture: EvalFixture | None = field(default=None, repr=False)

    def event_types(self) -> list[str]:
        return [e.type for e in self.events]

    def event_payloads(self, type_: str) -> list[dict]:
        return [e.payload for e in self.events if e.type == type_]

    def tool_names(self) -> list[str]:
        return [t.get("name") for t in self.tools]


@dataclass
class EvalFixture:
    case: object
    base_dir: Path
    clock: Clock | None = None

    home: Path = field(init=False, repr=False, default=None)
    work: Path = field(init=False, repr=False, default=None)
    layout: HomeLayout = field(init=False, repr=False, default=None)
    app_ctx: AppContext = field(init=False, repr=False, default=None)
    services: ServiceContainer = field(init=False, repr=False, default=None)
    model: object = field(init=False, repr=False, default=None)
    session: object = field(init=False, repr=False, default=None)
    record: object = field(init=False, repr=False, default=None)

    # -- lifecycle ----------------------------------------------------------

    def build(self) -> EvalFixture:
        from rinari.evals.scripted import ScriptedModel

        suffix = self.case.case_id.replace("/", "_")[:80]
        self.home = self.base_dir / f"{suffix}-home"
        self.work = self.base_dir / f"{suffix}-work"
        self.home.mkdir(parents=True, exist_ok=True)
        self.work.mkdir(parents=True, exist_ok=True)

        if self.case.config_overrides:
            layout = ensure_layout(self.home)
            user = writer.read_user_data(layout)
            for dotted, value in self.case.config_overrides.items():
                user = writer.set_dotted(user, dotted, value)
            writer.write_user_data(layout, user)

        self.layout = ensure_layout(self.home)
        self.app_ctx = build_app_context(home=self.home, clock=self.clock)
        self.services = build_services(self.app_ctx, user_home=self.home)

        self.services.providers.add(
            AddProviderInput(
                alias=EVAL_PROVIDER_ALIAS,
                provider_type="openai",
                auth_method="api-key",
                endpoint="http://127.0.0.1:9/v1",
                secret="sk-eval-deterministic",
            )
        )
        self.services.models.add(
            EVAL_PROVIDER_ALIAS,
            EVAL_MODEL_ID,
            EVAL_MODEL_ID,
            capabilities={"max_context_tokens": self.case.window, "tool_calls": True},
        )
        self.services.providers.use(EVAL_PROVIDER_ALIAS)

        if self.case.project:
            self.git("init", "-q")

        if self.case.setup is not None:
            self.case.setup(self)

        if self.case.script_lanes is not None:
            lanes = self.case.script_lanes(self)
            self.model = ScriptedModel(lanes, window=self.case.window, route=self.case.script_route)
        else:
            self.model = ScriptedModel(self.case.script_responses(self), window=self.case.window)

        started = self.services.sessions.start(self.work)
        self.record = started.session
        self.session = self.open_session()
        return self

    def open_session(self):
        """(Re)build the AgentSession for the current record (resume support)."""
        from rinari.cli.agent_runtime import build_agent_session

        return build_agent_session(
            self.services,
            self.record,
            interactive=False,
            user_home=self.home,
            profile=self._profile(self.case.profile),
            model_caller=self.model,
        )

    @staticmethod
    def _profile(name: str):
        from rinari.policy.engine import PermissionProfile, normalize_profile

        return normalize_profile(name) or PermissionProfile.WORKSPACE

    def close(self) -> None:
        try:
            if self.session is not None and getattr(self.session, "close", None):
                self.session.close()
        finally:
            if self.app_ctx is not None:
                self.app_ctx.close()

    # -- helpers -------------------------------------------------------------

    def file(self, rel: str, content: str) -> Path:
        path = self.work / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def git(self, *args: str) -> str:
        proc = subprocess.run(
            ["git", *args],
            cwd=self.work,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if proc.returncode != 0:
            raise FixtureError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
        return proc.stdout.strip()

    def events(self) -> list:
        return self.services.ctx.event_repo.list(self.record.id)

    def tools(self) -> list[dict]:
        from rinari.runtime.agent import EVENT_TOOL_COMPLETED

        # Runtime terminal events are correlated by tool_call_id. Successful
        # calls are ToolCompleted; failures are ToolFailed.
        return [
            e.payload
            for e in self.events()
            if e.type in (EVENT_TOOL_COMPLETED, "ToolFailed") and "tool_call_id" in e.payload
        ]

    @property
    def changed_files(self) -> list[str]:
        try:
            out = self.git("status", "--porcelain")
        except FixtureError:
            return []
        files = []
        for line in out.splitlines():
            if not line.strip():
                continue
            # porcelain v1: 2 status chars + path (tolerate missing leading space)
            path = line[2:].strip()
            path = path.split(" -> ", 1)[-1]
            files.append(path)
        return files


def make_clock() -> FakeClock:
    return FakeClock(start=1_750_000_000.0, step=1.0)


def profile_value(name: str) -> dict[str, Any]:
    return {"profile": name}
