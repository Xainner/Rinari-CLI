"""Graphic-control tests (computer use, engine side): grants, fake backend, tools.

No real desktop is touched: every test runs against FakeBackend. Real
backend proving belongs to the lab gate (docs/adr/0001-windows-desktop-backend.md).
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from rinari.application.context import build_app_context
from rinari.artifacts.store import ArtifactStore
from rinari.computer.backend import (
    ComputerError,
    FakeBackend,
    WindowsBackend,
    select_backend,
)
from rinari.computer.grants import GrantDenied, GrantStore
from rinari.computer.service import GraphicControlService
from rinari.policy.approvals import ApprovalEngine
from rinari.policy.engine import PermissionProfile, PolicyEngine
from rinari.policy.network import NetworkGuard, NetworkPolicy
from rinari.policy.sandbox import FilesystemSandbox, ProcessLimits
from rinari.runtime.cancellation import CancellationToken
from rinari.shared.clock import FakeClock
from rinari.tools.definition import ToolContext
from rinari.tools.native.computer import computer_tools
from rinari.tools.registry import ToolRegistry
from rinari.tools.runtime import ToolRuntime


class TickClock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


def _rig(tmp_path: Path, monkeypatch, *, scopes=("observe", "input", "send"), ttl=300.0):
    monkeypatch.setenv("RINARI_KEYRING", "0")
    home = tmp_path / "rinari-home-computer"
    app = build_app_context(home=str(home), clock=FakeClock())
    store = ArtifactStore(app)
    backend = FakeBackend()
    service = GraphicControlService(session_id="lab", backend=backend, artifact_store=store)
    grant = service.issue_grant("lab-app", scopes, ttl, note="test")
    root = tmp_path / "work"
    root.mkdir(parents=True, exist_ok=True)
    ctx = ToolContext(
        session_id="lab",
        kind="CHAT",
        cwd=root,
        project_root=None,
        user_home=tmp_path,
        profile=PermissionProfile.WORKSPACE,
        sandbox=FilesystemSandbox(read_root=root, write_roots=(root,)),
        limits=ProcessLimits(timeout_s=30, max_output_bytes=65536),
        artifact_root=tmp_path / "artifacts",
        clock=FakeClock(),
        cancellation=CancellationToken(),
        network=NetworkGuard(NetworkPolicy(mode="allow")),
        computer=service,
    )
    registry = ToolRegistry()
    registry.register_all(computer_tools())
    runtime = ToolRuntime(
        registry,
        PolicyEngine(network=NetworkPolicy(mode="allow")),
        ApprovalEngine(prompt=lambda req: "y"),
        clock=FakeClock(),
    )
    return service, backend, grant, app, ctx, runtime


def test_grant_scopes_are_separate(tmp_path, monkeypatch) -> None:
    service, _backend, _grant, app, _ctx, _runtime = _rig(
        tmp_path, monkeypatch, scopes=("observe",)
    )
    try:
        service.grants.check("lab", "lab-app", "observe")
        with pytest.raises(GrantDenied):
            service.grants.check("lab", "lab-app", "input")
        with pytest.raises(GrantDenied):
            service.grants.check("lab", "lab-app", "send")
    finally:
        app.close()


def test_grant_expires(tmp_path, monkeypatch) -> None:
    clock = TickClock()
    grants = GrantStore(clock=clock)
    grants.issue("lab", "lab-app", ("observe", "input", "send"), 10.0)
    grants.check("lab", "lab-app", "input")
    clock.now += 11.0
    with pytest.raises(GrantDenied, match="expired"):
        grants.check("lab", "lab-app", "input")


def test_grant_revoked_and_bound(tmp_path, monkeypatch) -> None:
    service, _backend, grant, app, _ctx, _runtime = _rig(tmp_path, monkeypatch)
    try:
        assert service.revoke_grant(grant.grant_id) is True
        with pytest.raises(GrantDenied):
            service.grants.check("lab", "lab-app", "observe")
        with pytest.raises(GrantDenied):
            service.grants.check("other-session", "lab-app", "observe")
        with pytest.raises(GrantDenied):
            service.grants.check("lab", "other-target", "observe")
        assert service.revoke_grant("missing") is False
    finally:
        app.close()


def test_select_backend_defaults_fake_and_lab_refuses(tmp_path, monkeypatch) -> None:
    assert isinstance(select_backend(None), FakeBackend)
    assert isinstance(select_backend("fake"), FakeBackend)
    monkeypatch.delenv("RINARI_COMPUTER_LAB", raising=False)
    with pytest.raises(ComputerError) as exc_info:
        select_backend("windows-lab")
    assert exc_info.value.code == "BACKEND_UNAVAILABLE"
    with pytest.raises(ComputerError) as exc_info:
        select_backend("nope")
    assert exc_info.value.code == "INVALID_ARGUMENT"
    windows = WindowsBackend()
    for op in (
        lambda: windows.targets(),
        lambda: windows.capture("t"),
        lambda: windows.click("t", 1.0, 2.0),
        lambda: windows.type_text("t", "hi"),
    ):
        with pytest.raises(ComputerError) as exc_info:
            op()
        assert exc_info.value.code == "BACKEND_UNAVAILABLE"


def test_tools_deny_without_service(tmp_path, monkeypatch) -> None:
    _service, _backend, _grant, app, ctx, runtime = _rig(tmp_path, monkeypatch)
    bare = dataclasses.replace(ctx, computer=None)
    try:
        for name, args in (
            ("computer.state", {}),
            ("computer.capture", {"target": "lab-app"}),
            ("computer.click", {"target": "lab-app", "x": 1, "y": 2}),
        ):
            result = runtime.execute(name, args, bare)
            assert not result.ok
            assert result.error.code.value == "DEPENDENCY_ERROR"
    finally:
        app.close()


def test_tools_deny_without_grant(tmp_path, monkeypatch) -> None:
    service, _backend, grant, app, ctx, runtime = _rig(tmp_path, monkeypatch)
    try:
        assert service.revoke_grant(grant.grant_id)
        for name, args in (
            ("computer.capture", {"target": "lab-app"}),
            ("computer.click", {"target": "lab-app", "x": 1, "y": 2}),
            ("computer.type", {"target": "lab-app", "text": "hi"}),
        ):
            result = runtime.execute(name, args, ctx)
            assert not result.ok, name
            assert result.error.code.value == "PERMISSION_DENIED", name
    finally:
        app.close()


def test_read_only_profile_denies_computer(tmp_path, monkeypatch) -> None:
    _service, _backend, _grant, app, ctx, runtime = _rig(tmp_path, monkeypatch)
    denied = dataclasses.replace(ctx, profile=PermissionProfile.READ_ONLY)
    try:
        result = runtime.execute("computer.click", {"target": "lab-app", "x": 1, "y": 2}, denied)
        assert not result.ok
        assert result.error.code.value == "POLICY_DENIED"
    finally:
        app.close()


def test_capture_binds_observation_with_images(tmp_path, monkeypatch) -> None:
    _service, _backend, _grant, app, ctx, runtime = _rig(tmp_path, monkeypatch)
    try:
        result = runtime.execute("computer.capture", {"target": "lab-app"}, ctx)
        assert result.ok, result.error
        assert result.data["uri"].startswith("artifact://")
        assert result.data["observation_id"]
        assert result.data["dispatch"] == "dispatched"
        assert result.data.get("visual") is True
        assert len(result.images) == 1
        assert result.images[0].uri == result.data["uri"]
        assert result.data["width"] == 64
        assert result.data["height"] == 48
    finally:
        app.close()


def test_capture_without_send_scope_degrades(tmp_path, monkeypatch) -> None:
    _service, _backend, _grant, app, ctx, runtime = _rig(tmp_path, monkeypatch, scopes=("observe",))
    try:
        denied = runtime.execute("computer.capture", {"target": "lab-app"}, ctx)
        assert not denied.ok
        assert denied.error.code.value == "PERMISSION_DENIED"
        result = runtime.execute(
            "computer.capture", {"target": "lab-app", "include_images": False}, ctx
        )
        assert result.ok, result.error
        assert result.data["visual"] is False
        assert result.images == ()
        assert result.data["uri"].startswith("artifact://")
    finally:
        app.close()


def test_click_flows_with_observation(tmp_path, monkeypatch) -> None:
    _service, _backend, _grant, app, ctx, runtime = _rig(tmp_path, monkeypatch)
    try:
        seen = runtime.execute("computer.capture", {"target": "lab-app"}, ctx)
        assert seen.ok, seen.error
        result = runtime.execute(
            "computer.click",
            {"target": "lab-app", "x": 10, "y": 20, "observation_id": seen.data["observation_id"]},
            ctx,
        )
        assert result.ok, result.error
        assert result.data["dispatch"] == "dispatched"
        assert result.data["clicked"] == {"x": 10.0, "y": 20.0}
        result = runtime.execute(
            "computer.click",
            {"target": "lab-app", "x": 10, "y": 20, "observation_id": "stale-id"},
            ctx,
        )
        assert not result.ok
        assert result.error.code.value == "INVALID_ARGUMENT"
    finally:
        app.close()


def test_click_cancel_releases_and_reports_unknown(tmp_path, monkeypatch) -> None:
    service, backend, _grant, app, _ctx, _runtime = _rig(tmp_path, monkeypatch)
    try:

        def cancelled() -> bool:
            return any(e.get("op") == "press" for e in backend.events)

        with pytest.raises(ComputerError) as exc_info:
            service.click("lab-app", 10.0, 20.0, cancelled=cancelled)
        assert exc_info.value.code == "CANCELLED"
        assert [e["op"] for e in backend.events] == ["press", "release_all"]
        assert service._ledger[-1]["dispatch"] == "unknown"
    finally:
        app.close()


def test_cancelled_input_error_contract(tmp_path, monkeypatch) -> None:
    from rinari.computer.backend import ComputerError as _CE

    class Cancelling(FakeBackend):
        def click(self, target_id, x, y, *, cancelled=None):
            self.events.append({"op": "press", "target_id": target_id})
            self.release_all(target_id)
            raise _CE("CANCELLED", "cancelled in test")

    service, _backend, _grant, app, ctx, runtime = _rig(tmp_path, monkeypatch)
    cancelling = Cancelling()
    ctx = dataclasses.replace(ctx, computer=None)
    service2 = GraphicControlService(
        session_id="lab", backend=cancelling, artifact_store=service.artifact_store
    )
    service2.issue_grant("lab-app", ("observe", "input", "send"), 300.0)
    ctx = dataclasses.replace(ctx, computer=service2)
    try:
        result = runtime.execute("computer.click", {"target": "lab-app", "x": 1, "y": 2}, ctx)
        assert not result.ok
        assert result.error.code.value == "CANCELLED"
        assert [e["op"] for e in cancelling.events] == ["press", "release_all"]
    finally:
        app.close()


def test_input_validation(tmp_path, monkeypatch) -> None:
    _service, _backend, _grant, app, ctx, runtime = _rig(tmp_path, monkeypatch)
    try:
        for args in (
            {"target": "lab-app", "x": "left", "y": 2},
            {"target": "lab-app", "x": 1, "y": True},
            {"target": "", "x": 1, "y": 2},
        ):
            result = runtime.execute("computer.click", args, ctx)
            assert not result.ok, args
            assert result.error.code.value == "INVALID_ARGUMENT", args
        for args in ({"target": "lab-app", "text": ""}, {"target": "lab-app", "text": "z" * 501}):
            result = runtime.execute("computer.type", args, ctx)
            assert not result.ok, args
            assert result.error.code.value == "INVALID_ARGUMENT", args
    finally:
        app.close()


def test_computer_tools_registered() -> None:
    from rinari.tools.native import all_native_tools

    names = {tool.name for tool in computer_tools()}
    assert names == {"computer.state", "computer.capture", "computer.click", "computer.type"}
    for tool in computer_tools():
        assert tool.always_loaded is False
    by_name = {tool.name: tool for tool in all_native_tools()}
    assert "computer.capture" in by_name
    assert by_name["computer.click"].idempotent is False
    assert by_name["computer.type"].idempotent is False
