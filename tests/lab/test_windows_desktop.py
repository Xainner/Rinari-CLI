"""Lab-only physical proving on an explicitly authorized Windows host.

NEVER runs in CI: the whole module skips unless RINARI_COMPUTER_LAB=1 on
win32. Operates ONLY on a Notepad window opened by each test over a unique
token file (PID allowlist after exe verification); refuses fullscreen
surfaces and non-foreground targets; kills the owned process without saving
at the end. Nothing else is touched.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("RINARI_COMPUTER_LAB") != "1" or sys.platform != "win32",
    reason="lab-only physical test (explicit host opt-in required)",
)

MARKER = "rinari-lab-ok 123"


def _open_lab_notepad(tmp_path):
    from rinari.computer import win32 as w

    token = "rinari-lab-" + uuid.uuid4().hex[:8] + ".txt"
    seed = tmp_path / token
    seed.write_text("", encoding="utf-8")
    subprocess.Popen(["notepad.exe", str(seed)])
    end = time.monotonic() + 20.0
    while time.monotonic() < end:
        for hwnd, title, pid in w.visible_windows():
            if token in title and w.process_exe(pid).lower() == "notepad.exe":
                w.set_topmost(hwnd, True)
                w.flash(hwnd)
                return hwnd, pid, seed
        time.sleep(0.2)
    raise AssertionError("lab notepad window never appeared")


def _kill(pid: int) -> None:
    from rinari.computer import win32 as w

    assert w.terminate_pid(pid), "lab notepad did not terminate"


def _ensure_foreground(hwnd: int, token: str, timeout_s: float = 300.0) -> None:
    from rinari.computer import win32 as w

    if w.focus_window(hwnd, timeout_s=3.0):
        return
    w.flash(hwnd)
    print("LAB: please bring the Notepad window to the front: " + token)
    end = time.monotonic() + timeout_s
    while time.monotonic() < end:
        if w.get_foreground() == hwnd:
            return
        time.sleep(0.5)
    raise AssertionError("lab window never took foreground; aborting without input")


def _edit_text(hwnd: int) -> str:
    from rinari.computer import win32 as w

    pairs = w.descendant_texts(hwnd)
    print("DIAG edit controls:", [(cls, len(text)) for cls, text in pairs])
    return pairs[0][1] if pairs else ""


def test_lab_capture_readonly(tmp_path) -> None:
    from rinari.computer.backend import WindowsBackend

    hwnd, pid, _seed = _open_lab_notepad(tmp_path)
    try:
        backend = WindowsBackend(lab=True, pid_allowlist=(pid,))
        target = "hwnd:" + str(hwnd)
        frame = backend.capture(target)
        assert frame.width > 100 and frame.height > 100
        assert frame.png[:8] == bytes([137, 80, 78, 71, 13, 10, 26, 10])
        assert frame.dpi_scale >= 1.0
        assert target in {t["target_id"] for t in backend.targets()}
    finally:
        _kill(pid)


def test_lab_grant_gated_type_roundtrip(tmp_path, monkeypatch) -> None:

    from rinari.application.context import build_app_context
    from rinari.artifacts.store import ArtifactStore
    from rinari.computer.backend import WindowsBackend
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

    monkeypatch.setenv("RINARI_KEYRING", "0")
    hwnd, pid, _seed = _open_lab_notepad(tmp_path)
    try:
        target = "hwnd:" + str(hwnd)
        import contextlib

        from rinari.computer import win32 as _cbw

        saved_clipboard = _cbw.get_clipboard_text()
        home = tmp_path / "rinari-home-lab"
        app = build_app_context(home=str(home), clock=FakeClock())
        try:
            store = ArtifactStore(app)
            backend = WindowsBackend(lab=True, pid_allowlist=(pid,))
            service = GraphicControlService(session_id="lab", backend=backend, artifact_store=store)
            service.issue_grant(target, ("observe", "input", "send"), 600.0, note="lab")
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
            seen = runtime.execute("computer.capture", {"target": target}, ctx)
            assert seen.ok, seen.error
            assert seen.data.get("visual") is True
            assert len(seen.images) == 1
            _ensure_foreground(hwnd, _seed.name)
            mid_x = float(seen.data["width"]) * 0.5
            mid_y = float(seen.data["height"]) * 0.5
            pressed = runtime.execute(
                "computer.click",
                {
                    "target": target,
                    "x": mid_x,
                    "y": mid_y,
                    "observation_id": seen.data["observation_id"],
                },
                ctx,
            )
            assert pressed.ok, pressed.error
            typed = runtime.execute(
                "computer.type",
                {
                    "target": target,
                    "text": MARKER,
                    "observation_id": seen.data["observation_id"],
                },
                ctx,
            )
            assert typed.ok, typed.error
            assert typed.data["dispatch"] == "dispatched"
            # Oracle: Win11 Notepad ignores WM_GETTEXT, so select-all + copy
            # and read the clipboard (OS ground truth, no vision, no guessing).
            from rinari.computer import win32 as _w

            # Never read foreign state: re-focus the edit control and copy in
            # immediate succession, and never print clipboard content.

            refocus = runtime.execute(
                "computer.click",
                {
                    "target": target,
                    "x": mid_x,
                    "y": mid_y,
                    "observation_id": seen.data["observation_id"],
                },
                ctx,
            )
            assert refocus.ok, refocus.error
            if _w.get_foreground() != hwnd:
                raise AssertionError("foreground moved before oracle; aborting")
            _w.select_all_and_copy(hwnd)
            # The app processes the keys asynchronously: poll without logging content.
            got = ""
            end = time.monotonic() + 10.0
            while time.monotonic() < end:
                got = _w.get_clipboard_text()
                if got == MARKER:
                    break
                time.sleep(0.2)
            assert got == MARKER, "oracle mismatch: len=" + str(len(got))
            (tmp_path / "proof.png").write_bytes(backend.capture(target).png)
        finally:
            with contextlib.suppress(Exception):
                _cbw.set_clipboard_text(saved_clipboard)
            app.close()
    finally:
        _kill(pid)
