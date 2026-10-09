"""Temas documentales: tokens con los que se compone, no adjetivos en un prompt.

La interfaz de Rinari es morada, pero un documento del usuario no tiene por
qué serlo: primero su plantilla o marca, luego el propósito y la audiencia,
luego un tema profesional; el tema `rinari` solo cuando corresponde.

Las medidas van en puntos. Una diapositiva 16:9 mide 960 x 540 pt.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from rinari.documents.contracts import DocumentError, DocumentErrorCode


@dataclass(frozen=True, slots=True)
class Theme:
    id: str
    version: str
    name: str
    description: str
    background: str
    surface: str
    text: str
    muted: str
    accent: str
    accent_text: str
    border: str
    positive: str
    negative: str
    palette: tuple[str, ...]
    heading_font: str
    body_font: str
    mono_font: str = "Consolas"
    # Escala tipográfica (pt).
    title_size: float = 36
    heading_size: float = 28
    body_size: float = 18
    caption_size: float = 12
    min_body_size: float = 14
    min_title_size: float = 26
    # Espaciado (pt).
    margin_x: float = 56
    margin_top: float = 44
    margin_bottom: float = 36
    gutter: float = 28
    radius: float = 6
    dark: bool = False
    # Acento como texto sobre el fondo, cuando el del relleno no da contraste.
    accent_ink: str | None = None
    extras: dict[str, Any] = field(default_factory=dict)

    @property
    def ink(self) -> str:
        return self.accent_ink or self.accent

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "version": self.version,
            "name": self.name,
            "description": self.description,
            "dark": self.dark,
            "fonts": {"heading": self.heading_font, "body": self.body_font},
            "colors": {
                "background": self.background,
                "surface": self.surface,
                "text": self.text,
                "muted": self.muted,
                "accent": self.accent,
                "palette": list(self.palette),
            },
        }


THEMES: dict[str, Theme] = {
    theme.id: theme
    for theme in (
        Theme(
            id="executive-light",
            version="1.0.0",
            name="Executive Light",
            description="Sobrio y claro: informes de dirección, comités, clientes.",
            background="FFFFFF",
            surface="F3F4F6",
            text="161A1F",
            muted="5A6372",
            accent="2F5BEA",
            accent_text="FFFFFF",
            border="D9DDE3",
            positive="0E7A5F",
            negative="C8363D",
            palette=("2F5BEA", "13A38A", "F2A93B", "E5484D", "7C5CFF", "8A94A6"),
            heading_font="Calibri",
            body_font="Calibri",
        ),
        Theme(
            id="executive-dark",
            version="1.0.0",
            name="Executive Dark",
            description="Fondo oscuro de alto contraste: presentaciones en pantalla.",
            background="0F1318",
            surface="1A2029",
            text="F2F4F7",
            muted="A3ACBA",
            accent="7AA2FF",
            accent_text="0F1318",
            border="2C3441",
            positive="3DD6A8",
            negative="FF6B70",
            palette=("7AA2FF", "3DD6A8", "FFC35A", "FF6B70", "B39DFF", "9AA4B5"),
            heading_font="Calibri",
            body_font="Calibri",
            dark=True,
        ),
        Theme(
            id="editorial",
            version="1.0.0",
            name="Editorial",
            description="Serif en títulos y papel cálido: relatos, investigación, informes largos.",
            background="FBF8F3",
            surface="F1ECE3",
            text="231F1A",
            muted="6B6156",
            accent="B24A2E",
            accent_text="FFFFFF",
            border="DDD4C6",
            positive="2F6B3F",
            negative="B24A2E",
            palette=("B24A2E", "2E6F8E", "C9A227", "5B8C5A", "7A5C99", "8C8278"),
            heading_font="Georgia",
            body_font="Calibri",
            title_size=38,
        ),
        Theme(
            id="rinari",
            version="1.0.0",
            name="Rinari",
            description="La identidad de Rinari: violeta nocturno. Para material propio de Rinari.",
            background="0E0B16",
            surface="1A1426",
            text="F4F1FB",
            muted="B3A8C9",
            accent="9645EE",
            accent_text="FFFFFF",
            accent_ink="C084FC",
            border="2E2440",
            positive="34D399",
            negative="F87171",
            palette=("A855F7", "22D3EE", "F472B6", "FBBF24", "34D399", "94A3B8"),
            heading_font="Calibri",
            body_font="Calibri",
            dark=True,
        ),
    )
}


def theme(theme_id: str | None) -> Theme:
    found = THEMES.get(theme_id or "executive-light")
    if found is None:
        raise DocumentError(
            DocumentErrorCode.INVALID_SPEC,
            f"Unknown theme {theme_id!r}",
            details={"themes": sorted(THEMES)},
        )
    return found


def hex_to_rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def _luminance(value: str) -> float:
    def channel(c: int) -> float:
        c = c / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = hex_to_rgb(value)
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def contrast(foreground: str, background: str) -> float:
    """Contraste WCAG entre dos colores hex."""
    a, b = _luminance(foreground), _luminance(background)
    lighter, darker = max(a, b), min(a, b)
    return (lighter + 0.05) / (darker + 0.05)
