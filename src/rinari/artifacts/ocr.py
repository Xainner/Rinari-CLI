"""Small, cancellable Tesseract adapter used by attachment preparation.

The engine owns OCR discovery.  The desktop package may provide a pinned
copy, while development machines can opt into ``RINARI_TESSERACT_BIN`` or a
normal PATH installation.  All subprocess arguments are passed as a list so
document text cannot become command line syntax.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path


class OcrUnavailableError(RuntimeError):
    """Tesseract is not installed or its language data is unavailable."""


def resolve_tesseract() -> tuple[Path, Path | None]:
    """Return ``(executable, tessdata_dir)`` using the packaged search order."""

    configured = os.environ.get("RINARI_TESSERACT_BIN")
    candidates: list[Path] = []
    if configured:
        candidates.append(Path(configured).expanduser())
    candidates.append(Path(sys.executable).resolve().parent / "ocr" / "tesseract.exe")

    # A checkout of Code is useful while developing the CLI.  Keep this
    # lookup deliberately narrow: it never scans an arbitrary user directory.
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidates.append(
            parent / "Apps" / "Rinari-Code" / "src-tauri" / "engine-dist" / "ocr" / "tesseract.exe"
        )
        candidates.append(
            parent / "Rinari-Code" / "src-tauri" / "engine-dist" / "ocr" / "tesseract.exe"
        )
    on_path = shutil.which("tesseract")
    if on_path:
        candidates.append(Path(on_path))

    for candidate in candidates:
        if not candidate.is_file():
            continue
        root = candidate.parent
        tessdata = root / "tessdata"
        if (tessdata / "eng.traineddata").is_file() or (tessdata / "spa.traineddata").is_file():
            return candidate.resolve(), tessdata.resolve()
        # A system installation may expose TESSDATA_PREFIX instead.
        prefix = os.environ.get("TESSDATA_PREFIX")
        if prefix:
            return candidate.resolve(), Path(prefix).expanduser().resolve()
        return candidate.resolve(), None
    raise OcrUnavailableError(
        "Tesseract OCR is unavailable. Install the packaged OCR runtime, set "
        "RINARI_TESSERACT_BIN, or install tesseract on PATH."
    )


def run_ocr(
    image_path: Path, *, language: str = "eng+spa", cancellation=None, timeout_s: float = 30.0
) -> str:
    """OCR one image with literal arguments and a bounded, cancellable process."""

    executable, tessdata = resolve_tesseract()
    command = [str(executable), str(image_path), "stdout", "--dpi", "300", "-l", language]
    if tessdata is not None:
        command.extend(["--tessdata-dir", str(tessdata)])
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, **({"TESSDATA_PREFIX": str(tessdata)} if tessdata else {})},
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    deadline = time.monotonic() + timeout_s
    stdout = stderr = ""
    try:
        while True:
            if cancellation is not None and getattr(cancellation, "cancelled", False):
                process.kill()
                process.communicate()
                if hasattr(cancellation, "throw_if_cancelled"):
                    cancellation.throw_if_cancelled()
                raise RuntimeError("OCR cancelled")
            if time.monotonic() >= deadline:
                process.kill()
                process.communicate()
                raise TimeoutError(f"OCR exceeded {timeout_s:g}s")
            try:
                stdout, stderr = process.communicate(timeout=0.1)
                break
            except subprocess.TimeoutExpired:
                continue
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()
    if process.returncode != 0:
        detail = (stderr or "").strip()[:500]
        raise OcrUnavailableError(f"Tesseract failed ({process.returncode}): {detail}")
    return stdout.strip()


__all__ = ["OcrUnavailableError", "resolve_tesseract", "run_ocr"]
