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

Only the fake backend ships. Anything else fails closed until selected,
reviewed and lab-approved (docs/adr/0001-windows-desktop-backend.md).
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
    """Real Windows backend seam. NOT implemented in this build.

    Intended direction (pending ADR approval and lab proving): per-window
    capture via Windows Graphics Capture CreateForWindow against an explicit
    HWND, accessibility via UI Automation, and native input with strict
    preconditions. SendInput is subject to UIPI and never resets keys held
    elsewhere, so it is not per-window isolated input: the ADR must resolve
    that before any real implementation lands. Every method fails closed.
    """

    name = "windows"

    def _unavailable(self) -> ComputerError:
        return ComputerError(
            "BACKEND_UNAVAILABLE",
            "no approved Windows graphic backend in this build; "
            "see docs/adr/0001-windows-desktop-backend.md",
        )

    def targets(self) -> list[dict[str, Any]]:
        raise self._unavailable()

    def capture(
        self, target_id: str, *, cancelled: Callable[[], bool] | None = None
    ) -> GraphicFrame:
        raise self._unavailable()

    def click(
        self, target_id: str, x: float, y: float, *, cancelled: Callable[[], bool] | None = None
    ) -> dict[str, Any]:
        raise self._unavailable()

    def type_text(
        self,
        target_id: str,
        text: str,
        *,
        submit: bool = False,
        cancelled: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        raise self._unavailable()

    def release_all(self, target_id: str) -> None:
        raise self._unavailable()


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
        raise ComputerError(
            "BACKEND_UNAVAILABLE",
            "windows-lab backend selected but no approved implementation exists yet; "
            "see docs/adr/0001-windows-desktop-backend.md",
        )
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
