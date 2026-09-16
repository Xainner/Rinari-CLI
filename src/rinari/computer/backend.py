"""Graphic backends: the only layer allowed to touch a user-authorized surface.

Contract for every backend (real or fake):

- capture/a11y-style reads never inject input;
- every multi-dispatch input completes its own gesture: if cancellation lands
  between press and release, the backend still dispatches the matching
  release (best-effort, uncancellable) and only then reports CANCELLED, so no
  button or key is ever left held by the controller;
- release targets the same authorized surface the press went to. CDP-style
  per-target input (and, later, per-window capture such as WGC
  CreateForWindow) is not OS-focus dependent, so same-target cleanup cannot
  leak onto another window.

The fake backend is the default. The Windows backend exists for explicit,
user-authorized lab proving only: it refuses to construct without lab=True
and RINARI_COMPUTER_LAB=1, resolves explicit targets, and never enumerates
untargeted surfaces (docs/adr/0001-windows-desktop-backend.md).
"""

from __future__ import annotations

import abc
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


class ComputerError(Exception):
    """Structured graphic-control failure (maps to tool error codes)."""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        # BACKEND_UNAVAILABLE | TARGET_NOT_FOUND | CAPTURE_FAILED |
        # INPUT_FAILED | INVALID_ARGUMENT | RESOURCE_EXHAUSTED | CANCELLED
        self.code = code
        self.message = message
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class GraphicFrame:
    png: bytes
    width: int
    height: int
    dpi_scale: float = 1.0


@dataclass
class FakeSurface:
    target_id: str
    title: str = "lab window"
    png: bytes | None = None
    width: int = 64
    height: int = 48


class GraphicBackend(abc.ABC):
    name: str = "abstract"

    @abc.abstractmethod
    def targets(self) -> list[dict[str, Any]]: ...

    @abc.abstractmethod
    def capture(
        self, target_id: str, *, cancelled: Callable[[], bool] | None = None
    ) -> GraphicFrame: ...

    @abc.abstractmethod
    def click(
        self, target_id: str, x: float, y: float, *, cancelled: Callable[[], bool] | None = None
    ) -> dict[str, Any]: ...

    @abc.abstractmethod
    def type_text(
        self,
        target_id: str,
        text: str,
        *,
        submit: bool = False,
        cancelled: Callable[[], bool] | None = None,
    ) -> dict[str, Any]: ...

    @abc.abstractmethod
    def release_all(self, target_id: str) -> None:
        """Best-effort release of input held by this controller on the target."""


def _raise_if_cancelled(cancelled: Callable[[], bool] | None) -> None:
    if cancelled is not None and cancelled():
        raise ComputerError("CANCELLED", "graphic-control operation cancelled")


class FakeBackend(GraphicBackend):
    """Scripted backend for unit tests and dry runs. Touches no real surface."""

    name = "fake"

    def __init__(self, surfaces: list[FakeSurface] | None = None) -> None:
        self._surfaces = {s.target_id: s for s in (surfaces or [FakeSurface("lab-app")])}
        self.events: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def targets(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {"target_id": tid, "title": s.title, "kind": "lab"}
                for tid, s in self._surfaces.items()
            ]

    def _surface(self, target_id: str) -> FakeSurface:
        try:
            return self._surfaces[target_id]
        except KeyError:
            raise ComputerError("TARGET_NOT_FOUND", f"unknown lab target: {target_id}") from None

    def capture(
        self, target_id: str, *, cancelled: Callable[[], bool] | None = None
    ) -> GraphicFrame:
        _raise_if_cancelled(cancelled)
        surface = self._surface(target_id)
        png = surface.png
        if png is None:
            import io

            from PIL import Image as _PILImage

            buf = io.BytesIO()
            _PILImage.new("RGB", (surface.width, surface.height), "teal").save(buf, format="PNG")
            png = buf.getvalue()
        with self._lock:
            self.events.append({"op": "capture", "target_id": target_id, "bytes": len(png)})
        return GraphicFrame(png=png, width=surface.width, height=surface.height)

    def click(
        self, target_id: str, x: float, y: float, *, cancelled: Callable[[], bool] | None = None
    ) -> dict[str, Any]:
        self._surface(target_id)
        with self._lock:
            self.events.append({"op": "press", "target_id": target_id, "x": x, "y": y})
        try:
            _raise_if_cancelled(cancelled)
        except ComputerError:
            self.release_all(target_id)
            raise
        with self._lock:
            self.events.append({"op": "release", "target_id": target_id, "x": x, "y": y})
        return {"clicked": {"x": x, "y": y}}

    def type_text(
        self,
        target_id: str,
        text: str,
        *,
        submit: bool = False,
        cancelled: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        self._surface(target_id)
        for char in text:
            with self._lock:
                self.events.append({"op": "keyDown", "target_id": target_id, "key": char})
            try:
                _raise_if_cancelled(cancelled)
            except ComputerError:
                self.release_all(target_id)
                raise
            with self._lock:
                self.events.append({"op": "keyUp", "target_id": target_id, "key": char})
        if submit:
            with self._lock:
                self.events.append({"op": "keyDown", "target_id": target_id, "key": "Enter"})
                self.events.append({"op": "keyUp", "target_id": target_id, "key": "Enter"})
        return {"typed": len(text), "submit": submit}

    def release_all(self, target_id: str) -> None:
        with self._lock:
            self.events.append({"op": "release_all", "target_id": target_id})


class WindowsBackend(GraphicBackend):
    """Lab Windows backend: explicit-HWND capture and bounded native input.

    Lab-approved (user-authorized physical proving, 2026-09-16): user-mode
    ctypes only, no elevation, no hooks. Targets resolve from explicit
    hwnd:/pid: ids or from a PID allowlist owned by the lab harness; window
    titles are never enumerated to callers, so targets() discloses nothing
    beyond allowlisted surfaces. Input refuses fullscreen surfaces and
    non-foreground targets; capture is a read-only region screenshot.
    """

    name = "windows"

    def __init__(self, *, lab: bool = False, pid_allowlist: tuple = ()) -> None:
        import os
        import sys

        if sys.platform != "win32":
            raise ComputerError("BACKEND_UNAVAILABLE", "Windows backend needs Windows")
        if not lab or os.environ.get("RINARI_COMPUTER_LAB") != "1":
            raise ComputerError(
                "BACKEND_UNAVAILABLE",
                "Windows backend requires explicit lab opt-in (lab=True and "
                "RINARI_COMPUTER_LAB=1); refusing on this host. "
                "See docs/adr/0001-windows-desktop-backend.md",
            )
        self.events: list[dict[str, Any]] = []
        self._pid_allowlist = tuple(pid_allowlist)

    def _resolve(self, target_id: str) -> int:
        from rinari.computer import win32 as _w

        hwnd = 0
        if target_id.startswith("hwnd:"):
            hwnd = int(target_id.split(":", 1)[1])
        elif target_id.startswith("pid:"):
            pid = int(target_id.split(":", 1)[1])
            if self._pid_allowlist and pid not in self._pid_allowlist:
                raise ComputerError("TARGET_NOT_FOUND", "pid outside lab allowlist")
            found = _w.enum_windows_for_pid(pid)
            if not found:
                raise ComputerError("TARGET_NOT_FOUND", "no visible window for pid")
            hwnd = found[0]
        else:
            raise ComputerError("INVALID_ARGUMENT", "target must look like hwnd:<n> or pid:<n>")
        if not _w.user32.IsWindow(hwnd) or not _w.user32.IsWindowVisible(hwnd):
            raise ComputerError("TARGET_NOT_FOUND", "target window is gone")
        if self._pid_allowlist:
            owner = _w.wintypes.DWORD()
            _w.user32.GetWindowThreadProcessId(hwnd, _w.ctypes.byref(owner))
            if owner.value not in self._pid_allowlist:
                raise ComputerError("TARGET_NOT_FOUND", "window outside lab allowlist")
        return hwnd

    def _check_fullscreen(self, hwnd: int) -> None:
        from rinari.computer import win32 as _w

        left, top, right, bottom = _w.window_rect(hwnd)
        _vx, _vy, vw, vh = _w.virtual_screen()
        if (right - left) * (bottom - top) >= int(vw * vh * 0.95):
            raise ComputerError("INVALID_ARGUMENT", "refusing input on a fullscreen surface")

    def targets(self) -> list[dict[str, Any]]:
        from rinari.computer import win32 as _w

        out: list[dict[str, Any]] = []
        for pid in self._pid_allowlist:
            for hwnd in _w.enum_windows_for_pid(pid):
                out.append({"target_id": f"hwnd:{hwnd}", "title": "", "kind": "lab"})
        return out

    def capture(
        self, target_id: str, *, cancelled: Callable[[], bool] | None = None
    ) -> GraphicFrame:
        from rinari.computer import win32 as _w

        _raise_if_cancelled(cancelled)
        hwnd = self._resolve(target_id)
        left, top, right, bottom = _w.window_rect(hwnd)
        if right <= left or bottom <= top:
            raise ComputerError("CAPTURE_FAILED", "target window has no area")
        from PIL import ImageGrab

        shot = ImageGrab.grab(bbox=(left, top, right, bottom))
        import io

        buf = io.BytesIO()
        shot.save(buf, format="PNG")
        self.events.append({"op": "capture", "target_id": target_id})
        return GraphicFrame(
            png=buf.getvalue(),
            width=shot.width,
            height=shot.height,
            dpi_scale=_w.dpi_scale(hwnd),
        )

    def _ensure_foreground(self, hwnd: int) -> None:
        from rinari.computer import win32 as _w

        if not _w.focus_window(hwnd):
            raise ComputerError("INPUT_FAILED", "target is not foreground; refusing blind input")

    def click(
        self, target_id: str, x: float, y: float, *, cancelled: Callable[[], bool] | None = None
    ) -> dict[str, Any]:
        from rinari.computer import win32 as _w

        _raise_if_cancelled(cancelled)
        hwnd = self._resolve(target_id)
        self._check_fullscreen(hwnd)
        self._ensure_foreground(hwnd)
        left, top, _right, _bottom = _w.window_rect(hwnd)
        try:
            _w.mouse_click_screen(left + x, top + y)
        except OSError as exc:
            raise ComputerError("INPUT_FAILED", "mouse click failed: " + str(exc)) from exc
        self.events.append({"op": "click", "target_id": target_id, "x": x, "y": y})
        return {"clicked": {"x": x, "y": y}}

    def type_text(
        self,
        target_id: str,
        text: str,
        *,
        submit: bool = False,
        cancelled: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        from rinari.computer import win32 as _w

        _raise_if_cancelled(cancelled)
        hwnd = self._resolve(target_id)
        self._check_fullscreen(hwnd)
        self._ensure_foreground(hwnd)

        def check() -> bool:
            if cancelled is not None and cancelled():
                return True
            # Shared-machine guard: stop at the first char that would land
            # outside our verified window instead of typing blind.
            return _w.get_foreground() != hwnd

        try:
            _w.type_unicode(text, cancelled=check, pacing_s=0.02)
        except TimeoutError as exc:
            try:
                _w.mouse_release_all()
            finally:
                if _w.get_foreground() != hwnd:
                    raise ComputerError(
                        "CANCELLED", "foreground lost mid-type; stopped cleanly"
                    ) from exc
                raise ComputerError("CANCELLED", str(exc)) from exc
        except OSError as exc:
            raise ComputerError("INPUT_FAILED", "keyboard input failed: " + str(exc)) from exc
        if submit:
            _w.press_enter()
        self.events.append({"op": "type", "target_id": target_id, "chars": len(text)})
        return {"typed": len(text), "submit": submit}

    def release_all(self, target_id: str) -> None:
        import contextlib

        from rinari.computer import win32 as _w

        with contextlib.suppress(Exception):
            _w.mouse_release_all()
        self.events.append({"op": "release_all", "target_id": target_id})

    def read_text(self, target_id: str) -> str:
        """Lab verification helper (not a tool): readable text of the surface."""
        from rinari.computer import win32 as _w

        return _w.read_text(self._resolve(target_id))


def select_backend(name: str | None, *, fake: FakeBackend | None = None) -> GraphicBackend:
    """Resolve the backend by explicit name only. Defaults to the fake.

    A real backend is never selected by accident: 'windows-lab' additionally
    requires RINARI_COMPUTER_LAB=1 and still fails closed until an approved
    implementation exists.
    """
    import os

    if name in (None, "", "fake"):
        return fake or FakeBackend()
    if name == "windows-lab":
        if os.environ.get("RINARI_COMPUTER_LAB") != "1":
            raise ComputerError(
                "BACKEND_UNAVAILABLE",
                "windows-lab backend requires RINARI_COMPUTER_LAB=1 in a separate "
                "lab environment; refusing on this host",
            )
        return WindowsBackend(lab=True)
    raise ComputerError("INVALID_ARGUMENT", f"unknown graphic backend: {name!r}")


__all__ = [
    "ComputerError",
    "FakeBackend",
    "FakeSurface",
    "GraphicBackend",
    "GraphicFrame",
    "WindowsBackend",
    "select_backend",
]
