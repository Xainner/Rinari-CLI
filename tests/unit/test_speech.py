"""Local dictation: settings, pinned model download, WAV checks, whisper.cpp
invocation and the job protocol. Nothing here runs the real binary or
downloads a model; the real smoke is in the PR description."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import subprocess
import time
import wave
from pathlib import Path

import pytest

from rinari.speech import SpeechError, SpeechService
from rinari.speech import catalog as speech_catalog
from rinari.speech.catalog import SpeechModel


def wav_bytes(seconds: float = 1.0, rate: int = 16000, channels: int = 1, width: int = 2) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(width)
        wav.setframerate(rate)
        wav.writeframes(b"\x00\x00" * int(rate * seconds) * channels)
    return buffer.getvalue()


@pytest.fixture
def services(app_ctx, tmp_path):
    from rinari.application.services import build_services

    home = tmp_path / "home"
    home.mkdir()
    return build_services(app_ctx, user_home=home)


@pytest.fixture
def fake_binary(tmp_path) -> Path:
    path = (
        tmp_path / "bin" / ("whisper-cli.exe" if __import__("os").name == "nt" else "whisper-cli")
    )
    path.parent.mkdir()
    path.write_bytes(b"")
    return path


class Runner:
    def __init__(self, stdout: str = " Hola Rinari.\n", returncode: int = 0) -> None:
        self.calls: list[list[str]] = []
        self.stdout = stdout
        self.returncode = returncode

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        assert kwargs.get("shell") is False
        assert Path(argv[argv.index("-f") + 1]).is_file(), "the WAV exists while it runs"
        return subprocess.CompletedProcess(argv, self.returncode, self.stdout.encode(), b"")


def speech(services, fake_binary, runner=None, **kwargs) -> SpeechService:
    return SpeechService(
        services.ctx,
        env={"RINARI_WHISPER_BIN": str(fake_binary), "PATH": ""},
        runner=runner or Runner(),
        **kwargs,
    )


def install(service: SpeechService, model: str = "small") -> None:
    path = service.model_path(speech_catalog.MODELS[model])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"model")


# -- binary and status -----------------------------------------------------------------


def test_status_says_what_is_missing(services, fake_binary):
    service = speech(services, fake_binary)
    status = service.status()
    assert status["binary_found"] is True
    assert status["model"] == "small" and status["model_installed"] is False
    assert status["ready"] is False
    assert status["language"] == ""  # never chosen: the client passes its UI language
    install(service)
    assert service.status()["ready"] is True


def test_the_binary_override_must_be_absolute_and_named_like_whisper(services, tmp_path):
    impostor = tmp_path / "evil.exe"
    impostor.write_bytes(b"")
    service = SpeechService(services.ctx, env={"RINARI_WHISPER_BIN": str(impostor), "PATH": ""})
    assert service.binary() is None
    relative = SpeechService(
        services.ctx, env={"RINARI_WHISPER_BIN": "whisper-cli.exe", "PATH": "."}
    )
    assert relative.binary() is None


# -- settings --------------------------------------------------------------------------


def test_settings_validate_and_persist(services, fake_binary):
    service = speech(services, fake_binary)
    saved = service.set_settings(model="base", language="es", vocabulary="  Boards,\n skills  ")
    assert saved == {"model": "base", "language": "es", "vocabulary": "Boards, skills"}
    assert speech(services, fake_binary).settings()["model"] == "base"
    with pytest.raises(SpeechError) as bad_model:
        service.set_settings(model="huge")
    assert bad_model.value.code == "INVALID_MODEL"
    with pytest.raises(SpeechError) as bad_language:
        service.set_settings(language="klingon")
    assert bad_language.value.code == "INVALID_LANGUAGE"


# -- transcription -----------------------------------------------------------------------


def test_transcribe_runs_whisper_with_language_vocabulary_and_no_timestamps(services, fake_binary):
    runner = Runner(stdout=" [BLANK_AUDIO]\n Revisa el módulo de pagos.\n")
    service = speech(services, fake_binary, runner)
    install(service)
    service.set_settings(vocabulary="Boards")
    result = service.transcribe(wav_bytes(1.5), language="es")
    assert result["text"] == "Revisa el módulo de pagos."
    assert result["model"] == "small" and result["language"] == "es" and result["audio_ms"] == 1500
    argv = runner.calls[0]
    assert argv[0] == str(fake_binary)
    assert argv[argv.index("-l") + 1] == "es"
    assert "-nt" in argv and "-np" in argv
    assert argv[argv.index("--prompt") + 1] == "Rinari, Rinari Agent, Boards"
    assert argv[argv.index("-m") + 1].endswith("ggml-small-q5_1.bin")


def test_without_a_language_it_uses_the_saved_one_then_auto(services, fake_binary):
    runner = Runner()
    service = speech(services, fake_binary, runner)
    install(service)
    service.transcribe(wav_bytes())
    assert runner.calls[-1][runner.calls[-1].index("-l") + 1] == "auto"
    service.set_settings(language="en")
    service.transcribe(wav_bytes())
    assert runner.calls[-1][runner.calls[-1].index("-l") + 1] == "en"


@pytest.mark.parametrize(
    ("audio", "code"),
    [
        (b"", "INVALID_AUDIO"),
        (b"not a wav", "INVALID_AUDIO"),
        (wav_bytes(rate=44100), "INVALID_AUDIO"),
        (wav_bytes(channels=2), "INVALID_AUDIO"),
        (wav_bytes(0.05), "TOO_SHORT"),
    ],
    ids=["empty", "not-wav", "44khz", "stereo", "too-short"],
)
def test_bad_audio_is_refused_before_running_anything(services, fake_binary, audio, code):
    runner = Runner()
    service = speech(services, fake_binary, runner)
    install(service)
    with pytest.raises(SpeechError) as exc:
        service.transcribe(audio)
    assert exc.value.code == code
    assert runner.calls == []


def test_a_missing_model_or_binary_is_named(services, fake_binary, tmp_path):
    with pytest.raises(SpeechError) as missing_model:
        speech(services, fake_binary).transcribe(wav_bytes())
    assert missing_model.value.code == "MODEL_MISSING"
    no_binary = SpeechService(services.ctx, env={"PATH": ""})
    install(no_binary)
    with pytest.raises(SpeechError) as missing_binary:
        no_binary.transcribe(wav_bytes())
    assert missing_binary.value.code == "NOT_INSTALLED"


def test_a_failing_or_slow_whisper_is_an_error(services, fake_binary):
    service = speech(services, fake_binary, Runner(returncode=3))
    install(service)
    with pytest.raises(SpeechError) as failed:
        service.transcribe(wav_bytes())
    assert failed.value.code == "TRANSCRIBE_FAILED"

    def slow(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 1)

    slow_service = speech(services, fake_binary, slow)
    install(slow_service)
    with pytest.raises(SpeechError) as timeout:
        slow_service.transcribe(wav_bytes())
    assert timeout.value.code == "TIMEOUT"


# -- model download -----------------------------------------------------------------------


class FakeResponse:
    def __init__(self, body: bytes, status: int = 200) -> None:
        self.body = body
        self.status_code = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def iter_bytes(self, size):
        for start in range(0, len(self.body), 3):
            yield self.body[start : start + 3]


class FakeClient:
    def __init__(self, body: bytes, status: int = 200) -> None:
        self.response = FakeResponse(body, status)
        self.urls: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def stream(self, method, url):
        self.urls.append(url)
        return self.response


def pinned(monkeypatch, body: bytes) -> SpeechModel:
    model = SpeechModel(
        "small", "ggml-test.bin", len(body), hashlib.sha256(body).hexdigest(), "default"
    )
    monkeypatch.setitem(speech_catalog.MODELS, "small", model)
    return model


def test_a_download_is_verified_then_placed(services, fake_binary, monkeypatch):
    body = b"pretend-model-bytes"
    model = pinned(monkeypatch, body)
    client = FakeClient(body)
    seen: list[tuple[int, int]] = []
    service = speech(services, fake_binary, client_factory=lambda: client)
    result = service.download("small", progress=lambda r, t: seen.append((r, t)))
    assert result == {"model": "small", "installed": True, "downloaded": True}
    assert service.model_path(model).read_bytes() == body
    assert client.urls == [model.url]
    assert seen[-1] == (len(body), len(body))
    # Already there: nothing is fetched again.
    assert service.download("small")["downloaded"] is False


@pytest.mark.parametrize(
    ("served", "code"),
    [
        (b"tampered-model-byte", "CHECKSUM_MISMATCH"),
        (b"pretend-model-bytes-and-more", "DOWNLOAD_FAILED"),
    ],
)
def test_a_wrong_or_oversized_download_is_discarded(
    services, fake_binary, monkeypatch, served, code
):
    model = pinned(monkeypatch, b"pretend-model-bytes")
    service = speech(services, fake_binary, client_factory=lambda: FakeClient(served))
    with pytest.raises(SpeechError) as exc:
        service.download("small")
    assert exc.value.code == code
    assert not service.model_path(model).exists()
    assert not list(service.models_dir().glob("*.part"))


def test_a_cancelled_download_leaves_nothing(services, fake_binary, monkeypatch):
    model = pinned(monkeypatch, b"pretend-model-bytes")
    service = speech(
        services, fake_binary, client_factory=lambda: FakeClient(b"pretend-model-bytes")
    )
    with pytest.raises(SpeechError) as exc:
        service.download("small", cancelled=lambda: True)
    assert exc.value.code == "CANCELLED"
    assert not service.model_path(model).exists()


def test_the_catalog_is_pinned():
    for model in speech_catalog.MODELS.values():
        assert len(model.sha256) == 64 and model.size > 1_000_000
        assert model.url.startswith("https://huggingface.co/ggerganov/whisper.cpp/resolve/main/")
    assert speech_catalog.DEFAULT_MODEL == "small"


# -- protocol --------------------------------------------------------------------------------


def test_transcription_is_a_job_over_the_protocol(services, fake_binary, tmp_path, monkeypatch):
    from rinari.engine_protocol.server import EngineServer

    monkeypatch.setenv("RINARI_WHISPER_BIN", str(fake_binary))
    runner = Runner(stdout=" Hola desde el dictado.\n")
    monkeypatch.setattr("rinari.speech.service.subprocess.run", runner)
    server = EngineServer(services, user_home=tmp_path / "home")
    try:
        ids = iter(range(100))

        def call(method, params):
            line = json.dumps({"id": f"r{next(ids)}", "method": method, "params": params})
            return server.handle_line(line)

        status = call("speech.status", {})["result"]
        assert status["binary_found"] is True and status["ready"] is False
        install(SpeechService(services.ctx, env={"RINARI_WHISPER_BIN": str(fake_binary)}))
        assert call("speech.settings.set", {"language": "es"})["result"]["language"] == "es"
        assert call("speech.settings.set", {"language": 3})["ok"] is False
        audio = base64.b64encode(wav_bytes()).decode()
        started = call("speech.transcribe", {"audio": audio})["result"]
        assert started["status"] == "running"
        deadline = time.monotonic() + 10
        found = None
        while found is None and time.monotonic() < deadline:
            for frame in server.drain_events():
                if frame.get("event") == "speech.transcribed":
                    found = frame["payload"]
            time.sleep(0.05)
        assert found is not None and found["job_id"] == started["job_id"]
        assert found["text"] == "Hola desde el dictado." and found["language"] == "es"
        assert call("speech.transcribe", {"audio": "%%%"})["ok"] is False
    finally:
        server.close()
