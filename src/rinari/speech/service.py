"""Speech to text for dictation, run locally with whisper.cpp.

The binary ships next to the Engine's Python (`<engine>/speech/whisper-cli`),
like Tesseract for OCR; the model is downloaded on first use into
`~/.rinari/models/speech/`, checked against its pinned SHA-256. Audio is a
short WAV the desktop recorded (mono, 16 kHz, 16-bit); it is written to a
private temporary folder, transcribed and deleted. Nothing is sent anywhere.
"""

from __future__ import annotations

import hashlib
import io
import os
import subprocess
import sys
import tempfile
import time
import wave
from collections.abc import Callable
from pathlib import Path
from typing import Any

from rinari.speech.catalog import DEFAULT_MODEL, LANGUAGES, MODELS, SpeechModel

ENV_BINARY = "RINARI_WHISPER_BIN"
KEY_MODEL = "speech.model"
KEY_LANGUAGE = "speech.language"
KEY_VOCABULARY = "speech.vocabulary"

SAMPLE_RATE = 16000
MIN_AUDIO_MS = 200
MAX_AUDIO_S = 300
MAX_VOCABULARY = 300
TRANSCRIBE_TIMEOUT_S = 180.0
#: Always in the hint: the assistant's own name is the word Whisper misspells
#: most in dictation ("ReNari", "Reynari"). The bare name alone did not fix it
#: on a real recording; with "Rinari Agent" next to it, it did.
ALWAYS_VOCABULARY = "Rinari, Rinari Agent"

_WINDOWS = os.name == "nt"
_BINARY_NAMES = ("whisper-cli.exe",) if _WINDOWS else ("whisper-cli",)
#: Whisper's markers for what is not speech; they never belong in a message.
_NOISE = ("[BLANK_AUDIO]", "[MUSIC]", "[SILENCE]", "(silence)", "[no speech]")


class SpeechError(Exception):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


class SpeechService:
    def __init__(
        self,
        ctx,
        *,
        env: dict[str, str] | None = None,
        runner: Callable[..., subprocess.CompletedProcess] | None = None,
        client_factory: Callable[[], Any] | None = None,
    ) -> None:
        self._ctx = ctx
        self._env = dict(os.environ if env is None else env)
        self._run = runner or subprocess.run
        self._client_factory = client_factory

    # -- where things are ------------------------------------------------------------

    def models_dir(self) -> Path:
        return Path(self._ctx.layout.root) / "models" / "speech"

    def binary(self) -> Path | None:
        """The whisper.cpp CLI: override, then bundled, then absolute PATH entries.

        Never the current directory or a relative PATH entry: a binary with
        the right name inside a cloned repo must not run.
        """
        override = self._env.get(ENV_BINARY)
        if override:
            path = Path(override)
            if path.is_absolute() and path.name.lower() in _BINARY_NAMES and path.is_file():
                return path
        bundled = Path(sys.executable).resolve().parent / "speech"
        for name in _BINARY_NAMES:
            if (bundled / name).is_file():
                return bundled / name
        for entry in (self._env.get("PATH") or "").split(os.pathsep):
            directory = entry.strip().strip('"')
            if not directory or not os.path.isabs(directory):
                continue
            for name in _BINARY_NAMES:
                candidate = Path(directory) / name
                if candidate.is_file():
                    return candidate
        return None

    def model_path(self, model: SpeechModel) -> Path:
        return self.models_dir() / model.file

    # -- settings ------------------------------------------------------------------

    def settings(self) -> dict[str, str]:
        config = self._ctx.config_repo
        model = config.get(KEY_MODEL)
        language = config.get(KEY_LANGUAGE)
        return {
            "model": model if model in MODELS else DEFAULT_MODEL,
            # Empty: never chosen, so the client passes its own UI language.
            "language": language if language in LANGUAGES else "",
            "vocabulary": config.get(KEY_VOCABULARY) or "",
        }

    def set_settings(
        self,
        *,
        model: str | None = None,
        language: str | None = None,
        vocabulary: str | None = None,
    ) -> dict[str, str]:
        from rinari.storage.records import ConfigValue

        updates = {}
        if model is not None:
            if model not in MODELS:
                raise SpeechError("INVALID_MODEL", f"unknown dictation model: {model}")
            updates[KEY_MODEL] = model
        if language is not None:
            if language not in LANGUAGES:
                raise SpeechError("INVALID_LANGUAGE", f"unsupported language: {language}")
            updates[KEY_LANGUAGE] = language
        if vocabulary is not None:
            updates[KEY_VOCABULARY] = _clean_vocabulary(vocabulary)
        from rinari.shared.clock import now_iso

        now = now_iso(self._ctx.clock)
        for key, value in updates.items():
            self._ctx.config_repo.set(ConfigValue(key=key, value=value, updated_at=now))
        return self.settings()

    def status(self) -> dict[str, Any]:
        settings = self.settings()
        binary = self.binary()
        models = [
            {**model.to_dict(), "installed": self.model_path(model).is_file()}
            for model in MODELS.values()
        ]
        installed = any(m["installed"] for m in models if m["id"] == settings["model"])
        return {
            **settings,
            "binary_found": binary is not None,
            "model_installed": installed,
            "ready": binary is not None and installed,
            "models": models,
            "languages": list(LANGUAGES),
        }

    # -- model download ------------------------------------------------------------

    def download(
        self,
        model_id: str,
        *,
        progress: Callable[[int, int], None] | None = None,
        cancelled: Callable[[], bool] = lambda: False,
    ) -> dict[str, Any]:
        """Fetch one pinned model; a file that does not match its hash is discarded."""
        model = MODELS.get(model_id)
        if model is None:
            raise SpeechError("INVALID_MODEL", f"unknown dictation model: {model_id}")
        target = self.model_path(model)
        if target.is_file():
            return {"model": model.id, "installed": True, "downloaded": False}
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_suffix(target.suffix + ".part")
        digest = hashlib.sha256()
        received = 0
        try:
            with self._client() as client, client.stream("GET", model.url) as response:
                if response.status_code != 200:
                    raise SpeechError(
                        "DOWNLOAD_FAILED", f"model download answered HTTP {response.status_code}"
                    )
                with partial.open("wb") as out:
                    for chunk in response.iter_bytes(1024 * 1024):
                        if cancelled():
                            raise SpeechError("CANCELLED", "model download cancelled")
                        received += len(chunk)
                        if received > model.size:
                            raise SpeechError("DOWNLOAD_FAILED", "model is larger than expected")
                        digest.update(chunk)
                        out.write(chunk)
                        if progress is not None:
                            progress(received, model.size)
            if received != model.size or digest.hexdigest() != model.sha256:
                raise SpeechError("CHECKSUM_MISMATCH", "the downloaded model is not the pinned one")
            partial.replace(target)
        except SpeechError:
            partial.unlink(missing_ok=True)
            raise
        except Exception as exc:  # network, disk
            partial.unlink(missing_ok=True)
            raise SpeechError("DOWNLOAD_FAILED", f"model download failed: {exc}") from exc
        return {"model": model.id, "installed": True, "downloaded": True}

    def remove_model(self, model_id: str) -> bool:
        model = MODELS.get(model_id)
        if model is None:
            raise SpeechError("INVALID_MODEL", f"unknown dictation model: {model_id}")
        path = self.model_path(model)
        if not path.is_file():
            return False
        path.unlink()
        return True

    def _client(self):
        if self._client_factory is not None:
            return self._client_factory()
        import httpx

        return httpx.Client(follow_redirects=True, timeout=httpx.Timeout(60.0, connect=20.0))

    # -- transcription ---------------------------------------------------------------

    def transcribe(self, audio: bytes, *, language: str | None = None) -> dict[str, Any]:
        audio_ms = _check_wav(audio)
        binary = self.binary()
        if binary is None:
            raise SpeechError(
                "NOT_INSTALLED", "the dictation engine (whisper.cpp) is not installed"
            )
        settings = self.settings()
        model = MODELS[settings["model"]]
        model_file = self.model_path(model)
        if not model_file.is_file():
            raise SpeechError(
                "MODEL_MISSING", f"the {model.id} dictation model is not downloaded", model=model.id
            )
        lang = language or settings["language"] or "auto"
        if lang not in LANGUAGES:
            raise SpeechError("INVALID_LANGUAGE", f"unsupported language: {lang}")
        vocabulary = ", ".join(part for part in (ALWAYS_VOCABULARY, settings["vocabulary"]) if part)
        started = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="rinari-dictation-") as work:
            wav = Path(work) / "audio.wav"
            wav.write_bytes(audio)
            argv = [
                str(binary),
                "-m",
                str(model_file),
                "-f",
                str(wav),
                "-l",
                lang,
                "-t",
                str(_threads()),
                "-nt",
                "-np",
                "--prompt",
                vocabulary,
            ]
            try:
                completed = self._run(
                    argv,
                    capture_output=True,
                    stdin=subprocess.DEVNULL,
                    cwd=work,
                    timeout=TRANSCRIBE_TIMEOUT_S,
                    shell=False,
                    **_quiet(),
                )
            except subprocess.TimeoutExpired as exc:
                raise SpeechError("TIMEOUT", "dictation took too long") from exc
            except OSError as exc:
                raise SpeechError("NOT_INSTALLED", f"could not start whisper.cpp: {exc}") from exc
        if completed.returncode != 0:
            raise SpeechError(
                "TRANSCRIBE_FAILED", f"whisper.cpp exited with {completed.returncode}"
            )
        return {
            "text": _clean_text(_decode(completed.stdout)),
            "model": model.id,
            "language": lang,
            "audio_ms": audio_ms,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
        }


def _check_wav(audio: bytes) -> int:
    """Duration of a mono 16 kHz 16-bit WAV, or a clear refusal."""
    if not isinstance(audio, (bytes, bytearray)) or not audio:
        raise SpeechError("INVALID_AUDIO", "no audio")
    try:
        with wave.open(io.BytesIO(bytes(audio))) as wav:
            channels, width, rate, frames = (
                wav.getnchannels(),
                wav.getsampwidth(),
                wav.getframerate(),
                wav.getnframes(),
            )
    except (wave.Error, EOFError) as exc:
        raise SpeechError("INVALID_AUDIO", "the audio is not a WAV file") from exc
    if (channels, width, rate) != (1, 2, SAMPLE_RATE):
        raise SpeechError(
            "INVALID_AUDIO", "the audio must be mono, 16 kHz, 16-bit PCM", channels=channels
        )
    duration_ms = int(frames * 1000 / rate)
    if duration_ms < MIN_AUDIO_MS:
        raise SpeechError("TOO_SHORT", "the recording is too short")
    if duration_ms > MAX_AUDIO_S * 1000:
        raise SpeechError("TOO_LONG", f"dictation is limited to {MAX_AUDIO_S // 60} minutes")
    return duration_ms


def _decode(raw: bytes | str | None) -> str:
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    return raw.decode("utf-8", errors="replace")


def _clean_text(text: str) -> str:
    lines = []
    for line in text.splitlines():
        line = line.strip()
        for noise in _NOISE:
            line = line.replace(noise, "")
        if line.strip():
            lines.append(line.strip())
    return " ".join(lines).strip()


def _clean_vocabulary(text: str) -> str:
    flat = " ".join(str(text or "").split())
    return flat[:MAX_VOCABULARY]


def _threads() -> int:
    return max(1, min(8, (os.cpu_count() or 4) - 1))


def _quiet() -> dict[str, Any]:
    if _WINDOWS:
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {}


__all__ = ["SpeechError", "SpeechService"]
