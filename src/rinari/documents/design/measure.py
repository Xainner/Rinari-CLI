"""Medida de texto antes del render: estimación con la fuente real si existe.

Se mide con la fuente instalada (o su equivalente métrico) y se deja holgura;
el render posterior es el que confirma. Nunca se promete exactitud de Office a
partir de esta medida.
"""

from __future__ import annotations

import functools
import os
import sys
from pathlib import Path

# Archivo por familia y peso; Carlito y Liberation tienen las métricas de
# Calibri y Georgia/Times en Linux.
_CANDIDATES = {
    ("Calibri", False): ["calibri.ttf", "Carlito-Regular.ttf"],
    ("Calibri", True): ["calibrib.ttf", "Carlito-Bold.ttf"],
    ("Calibri Light", False): ["calibril.ttf", "Carlito-Regular.ttf"],
    ("Georgia", False): ["georgia.ttf", "Gelasio-Regular.ttf", "LiberationSerif-Regular.ttf"],
    ("Georgia", True): ["georgiab.ttf", "Gelasio-Bold.ttf", "LiberationSerif-Bold.ttf"],
    ("Segoe UI", False): ["segoeui.ttf"],
    ("Segoe UI", True): ["segoeuib.ttf"],
}
SAFETY = 1.06  # holgura frente a diferencias de render


def _font_dirs() -> list[Path]:
    dirs = []
    if sys.platform == "win32":
        windir = os.environ.get("WINDIR", r"C:\Windows")
        dirs += [Path(windir) / "Fonts", Path.home() / "AppData/Local/Microsoft/Windows/Fonts"]
    elif sys.platform == "darwin":
        dirs += [
            Path("/Library/Fonts"),
            Path("/System/Library/Fonts"),
            Path.home() / "Library/Fonts",
        ]
    else:
        dirs += [Path("/usr/share/fonts"), Path("/usr/local/share/fonts"), Path.home() / ".fonts"]
    return [d for d in dirs if d.is_dir()]


@functools.lru_cache(maxsize=64)
def font_file(family: str, bold: bool = False) -> Path | None:
    names = _CANDIDATES.get((family, bold)) or _CANDIDATES.get((family, False)) or []
    for directory in _font_dirs():
        for name in names:
            direct = directory / name
            if direct.is_file():
                return direct
            matches = list(directory.rglob(name)) if directory.name != "Fonts" else []
            if matches:
                return matches[0]
    return None


@functools.lru_cache(maxsize=256)
def _pil_font(family: str, bold: bool, size_tenths: int):
    from PIL import ImageFont

    path = font_file(family, bold)
    if path is None:
        return None
    return ImageFont.truetype(str(path), size=max(1, size_tenths) / 10 * 4)


def text_width(text: str, family: str, size: float, *, bold: bool = False) -> float:
    """Ancho en puntos; sin fuente, una estimación media conservadora."""
    font = _pil_font(family, bold, round(size * 10))
    if font is None:
        return len(text) * size * (0.56 if bold else 0.52)
    # Se mide a 4x (el tamaño de la fuente PIL está en "píxeles" = pt x 4).
    return font.getlength(text) / 4


def wrap(text: str, width: float, family: str, size: float, *, bold: bool = False) -> list[str]:
    lines: list[str] = []
    for paragraph in str(text).split("\n"):
        words = paragraph.split(" ")
        current = ""
        for word in words:
            candidate = f"{current} {word}".strip()
            if not current or text_width(candidate, family, size, bold=bold) * SAFETY <= width:
                current = candidate
            else:
                lines.append(current)
                current = word
        lines.append(current)
    return lines


def text_height(
    text: str, width: float, family: str, size: float, *, bold: bool = False, spacing: float = 1.15
) -> tuple[float, int]:
    lines = wrap(text, width, family, size, bold=bold)
    return len(lines) * size * spacing * 1.2, len(lines)


def fit(
    text: str,
    width: float,
    height: float,
    family: str,
    size: float,
    min_size: float,
    *,
    bold: bool = False,
    spacing: float = 1.15,
) -> tuple[float, bool]:
    """El mayor tamaño (bajando de 2 en 2 pt) que cabe; `False` si ni el mínimo cabe."""
    current = size
    while current >= min_size:
        needed, _ = text_height(text, width, family, current, bold=bold, spacing=spacing)
        if needed <= height:
            return current, True
        current -= 2
    return min_size, False
