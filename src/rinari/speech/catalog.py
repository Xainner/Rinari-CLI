"""Whisper models Rinari offers for dictation, pinned by size and SHA-256.

The files are the official ggml conversions published with whisper.cpp
(huggingface.co/ggerganov/whisper.cpp). They are downloaded on first use, not
shipped in the installer, and a file whose hash does not match is discarded.

Measured on a 24-thread desktop CPU with `small`: 7.5 s of Spanish speech in
2.8 s with the language set and a vocabulary hint (4.4 s with detection).
"""

from __future__ import annotations

from dataclasses import dataclass

_BASE_URL = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/"


@dataclass(frozen=True, slots=True)
class SpeechModel:
    id: str
    file: str
    size: int
    sha256: str
    #: Who it is for, said in the desktop's settings.
    tier: str

    @property
    def url(self) -> str:
        return _BASE_URL + self.file

    def to_dict(self) -> dict:
        return {"id": self.id, "file": self.file, "size": self.size, "tier": self.tier}


MODELS: dict[str, SpeechModel] = {
    model.id: model
    for model in (
        SpeechModel(
            "base",
            "ggml-base-q5_1.bin",
            59707625,
            "422f1ae452ade6f30a004d7e5c6a43195e4433bc370bf23fac9cc591f01a8898",
            "light",
        ),
        SpeechModel(
            "small",
            "ggml-small-q5_1.bin",
            190085487,
            "ae85e4a935d7a567bd102fe55afc16bb595bdb618e11b2fc7591bc08120411bb",
            "default",
        ),
        SpeechModel(
            "large-v3-turbo",
            "ggml-large-v3-turbo-q5_0.bin",
            574041195,
            "394221709cd5ad1f40c46e6031ca61bce88931e6e088c188294c6d5a55ffa7e2",
            "best",
        ),
    )
}

DEFAULT_MODEL = "small"

#: Languages offered by name; "auto" lets Whisper detect it (slower, and a
#: short phrase is easier to misdetect).
LANGUAGES = ("auto", "es", "en", "pt", "fr", "de", "it")

__all__ = ["DEFAULT_MODEL", "LANGUAGES", "MODELS", "SpeechModel"]
