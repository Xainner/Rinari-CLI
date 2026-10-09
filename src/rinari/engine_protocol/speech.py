"""`speech.*`: local dictation for the desktop composer.

Transcribing takes seconds and a model download minutes, and the stdio loop
answers one request at a time: both run as jobs on a worker thread. The
request returns a `job_id` at once; the outcome arrives as an event
(`speech.transcribed` / `speech.failed`, `speech.model.progress` /
`speech.model.ready` / `speech.model.failed`), so turns keep streaming while
the owner dictates.
"""

from __future__ import annotations

import base64
import binascii
import threading
import time
from collections.abc import Callable
from typing import Any

from rinari.engine_protocol.errors import INVALID_PARAMS, EngineProtocolError
from rinari.engine_protocol.messages import event
from rinari.speech import SpeechError, SpeechService

#: A five-minute 16 kHz mono WAV is ~9.6 MB; base64 adds a third.
MAX_AUDIO_BASE64 = 13 * 1024 * 1024
#: Progress events at most this often per download.
_PROGRESS_EVERY_S = 0.5


class SpeechMethods:
    def __init__(self, services, emit: Callable[[dict], None]) -> None:
        self._speech = SpeechService(services.ctx)
        self._ids = services.ctx.ids
        self._emit = emit
        self._lock = threading.Lock()
        self._downloads: dict[str, threading.Event] = {}

    def register(self, dispatcher) -> None:
        dispatcher.register("speech.status", self.status)
        dispatcher.register("speech.settings.set", self.settings_set)
        dispatcher.register("speech.model.download", self.model_download)
        dispatcher.register("speech.model.cancel", self.model_cancel)
        dispatcher.register("speech.model.remove", self.model_remove)
        dispatcher.register("speech.transcribe", self.transcribe)

    # -- inline --------------------------------------------------------------------

    def status(self, params: dict[str, Any]) -> dict[str, Any]:
        status = self._speech.status()
        with self._lock:
            status["downloading"] = sorted(self._downloads)
        return status

    def settings_set(self, params: dict[str, Any]) -> dict[str, Any]:
        unknown = set(params) - {"model", "language", "vocabulary"}
        if unknown:
            raise EngineProtocolError(
                INVALID_PARAMS, f"Unknown params: {', '.join(sorted(unknown))}"
            )
        for key in ("model", "language", "vocabulary"):
            if key in params and not isinstance(params[key], str):
                raise EngineProtocolError(INVALID_PARAMS, f"Param '{key}' must be a string.")
        self._call(self._speech.set_settings, **params)
        return self.status({})

    def model_remove(self, params: dict[str, Any]) -> dict[str, Any]:
        removed = self._call(self._speech.remove_model, _model(params))
        return {"removed": removed, "status": self.status({})}

    def model_cancel(self, params: dict[str, Any]) -> dict[str, Any]:
        model = _model(params)
        with self._lock:
            flag = self._downloads.get(model)
        if flag is not None:
            flag.set()
        return {"cancelled": flag is not None}

    # -- jobs ----------------------------------------------------------------------

    def model_download(self, params: dict[str, Any]) -> dict[str, Any]:
        model = _model(params)
        with self._lock:
            if model in self._downloads:
                return {"model": model, "status": "running"}
            cancel = threading.Event()
            self._downloads[model] = cancel
        last = [0.0]

        def progress(received: int, total: int) -> None:
            now = time.monotonic()
            if now - last[0] >= _PROGRESS_EVERY_S or received >= total:
                last[0] = now
                self._emit(
                    event(
                        "speech.model.progress",
                        {"model": model, "received": received, "total": total},
                    )
                )

        def run() -> None:
            try:
                self._speech.download(model, progress=progress, cancelled=cancel.is_set)
                self._emit(event("speech.model.ready", {"model": model}))
            except SpeechError as exc:
                self._emit(event("speech.model.failed", {"model": model, "error": _error(exc)}))
            except Exception as exc:  # never kill the engine over a download
                failure = {"code": "DOWNLOAD_FAILED", "message": str(exc)}
                self._emit(event("speech.model.failed", {"model": model, "error": failure}))
            finally:
                with self._lock:
                    self._downloads.pop(model, None)

        threading.Thread(target=run, name=f"rinari-speech-{model}", daemon=True).start()
        return {"model": model, "status": "running"}

    def transcribe(self, params: dict[str, Any]) -> dict[str, Any]:
        raw = params.get("audio")
        if not isinstance(raw, str) or not raw:
            raise EngineProtocolError(INVALID_PARAMS, "Param 'audio' must be base64 WAV.")
        if len(raw) > MAX_AUDIO_BASE64:
            raise EngineProtocolError(INVALID_PARAMS, "The recording is too long to dictate.")
        try:
            audio = base64.b64decode(raw, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise EngineProtocolError(INVALID_PARAMS, "Param 'audio' is not base64.") from exc
        language = params.get("language")
        if language is not None and not isinstance(language, str):
            raise EngineProtocolError(INVALID_PARAMS, "Param 'language' must be a string.")
        job_id = self._ids.new("job")

        def run() -> None:
            try:
                result = self._speech.transcribe(audio, language=language or None)
                self._emit(event("speech.transcribed", {"job_id": job_id, **result}))
            except SpeechError as exc:
                self._emit(event("speech.failed", {"job_id": job_id, "error": _error(exc)}))
            except Exception as exc:  # report, never kill the engine
                failure = {"code": "TRANSCRIBE_FAILED", "message": str(exc)}
                self._emit(event("speech.failed", {"job_id": job_id, "error": failure}))

        threading.Thread(target=run, name=f"rinari-dictation-{job_id}", daemon=True).start()
        return {"job_id": job_id, "status": "running"}

    @staticmethod
    def _call(function, *args, **kwargs):
        try:
            return function(*args, **kwargs)
        except SpeechError as exc:
            raise EngineProtocolError(INVALID_PARAMS, exc.message, details=_error(exc)) from exc


def _model(params: dict[str, Any]) -> str:
    model = params.get("model")
    if not isinstance(model, str) or not model:
        raise EngineProtocolError(INVALID_PARAMS, "Param 'model' must be a non-empty string.")
    return model


def _error(exc: SpeechError) -> dict[str, Any]:
    return {"code": exc.code, "message": exc.message, **exc.details}


__all__ = ["SpeechMethods"]
