"""audio.transcribe: Rinari listens with the dictation engine (plan 0.4.0 #36)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from rinari.policy.engine import PermissionProfile
from rinari.policy.sandbox import FilesystemSandbox, ProcessLimits
from rinari.shared.clock import FakeClock
from rinari.speech import SpeechError, SpeechService
from rinari.tools.definition import ToolContext
from rinari.tools.native.audio import audio_tools
from tests.unit.test_speech import fake_binary, install, services  # noqa: F401 - fixtures

TOOL = audio_tools()[0]
SEGMENTED = (
    "[00:00:00.000 --> 00:00:02.500]   Esta es la canción\n"
    "[00:00:02.500 --> 00:00:05.000]   [MUSIC]\n"
    "[00:00:05.000 --> 00:00:07.250]   del Power Ranger.\n"
)


class Runner:
    def __init__(self, stdout: str = " Hola desde el audio.\n") -> None:
        self.calls: list[list[str]] = []
        self.stdout = stdout

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        assert kwargs.get("shell") is False
        if Path(argv[0]).stem == "ffmpeg":
            Path(argv[-1]).write_bytes(b"RIFF")
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        assert Path(argv[argv.index("-f") + 1]).is_file()
        return subprocess.CompletedProcess(argv, 0, self.stdout.encode(), b"")


def _service(services, fake_binary, runner, path_env: str = "") -> SpeechService:  # noqa: F811
    return SpeechService(
        services.ctx, env={"RINARI_WHISPER_BIN": str(fake_binary), "PATH": path_env}, runner=runner
    )


def test_a_song_is_read_straight_by_whisper(services, fake_binary, tmp_path):  # noqa: F811
    runner = Runner()
    service = _service(services, fake_binary, runner)
    install(service)
    song = tmp_path / "cancion.mp3"
    song.write_bytes(b"ID3")
    result = service.transcribe_file(song, language="es")
    assert result["text"] == "Hola desde el audio."
    argv = runner.calls[0]
    assert argv[argv.index("-f") + 1] == str(song) and "-nt" in argv
    assert argv[argv.index("-l") + 1] == "es"


def test_timestamps_come_back_as_segments_without_noise(services, fake_binary, tmp_path):  # noqa: F811
    service = _service(services, fake_binary, Runner(SEGMENTED))
    install(service)
    song = tmp_path / "cancion.wav"
    song.write_bytes(b"RIFF")
    result = service.transcribe_file(song, timestamps=True)
    assert result["segments"] == [
        {"start_ms": 0, "end_ms": 2500, "text": "Esta es la canción"},
        {"start_ms": 5000, "end_ms": 7250, "text": "del Power Ranger."},
    ]
    assert result["text"] == "Esta es la canción del Power Ranger."


def test_other_formats_need_ffmpeg(services, fake_binary, tmp_path):  # noqa: F811
    clip = tmp_path / "video.mp4"
    clip.write_bytes(b"\0\0\0\x18ftyp")
    without = _service(services, fake_binary, Runner())
    install(without)
    with pytest.raises(SpeechError) as excinfo:
        without.transcribe_file(clip)
    assert excinfo.value.code == "UNSUPPORTED_FORMAT"

    tools = tmp_path / "tools"
    tools.mkdir()
    ffmpeg = tools / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
    ffmpeg.write_bytes(b"")
    ffmpeg.chmod(0o755)
    runner = Runner()
    service = _service(services, fake_binary, runner, path_env=str(tools))
    assert service.transcribe_file(clip)["text"] == "Hola desde el audio."
    assert Path(runner.calls[0][0]).stem == "ffmpeg"
    assert runner.calls[1][runner.calls[1].index("-f") + 1].endswith("audio.wav")


def _ctx(root: Path, speech, seen) -> ToolContext:
    return ToolContext(
        session_id="s1",
        kind="PROJECT",
        cwd=root,
        project_root=root,
        user_home=root.parent,
        profile=PermissionProfile.WORKSPACE,
        sandbox=FilesystemSandbox(root, (root,)),
        limits=ProcessLimits(),
        artifact_root=root.parent / "artifacts",
        clock=FakeClock(),
        activity_sink=lambda name, payload: seen.append((name, payload)),
        speech=speech,
    )


def test_the_tool_listens_inside_the_project_only(services, fake_binary, tmp_path):  # noqa: F811
    root = tmp_path / "project"
    root.mkdir()
    (root / "voz.ogg").write_bytes(b"OggS")
    service = _service(services, fake_binary, Runner())
    install(service)
    seen: list = []
    ctx = _ctx(root, service, seen)
    result = TOOL.handler({"path": "voz.ogg"}, ctx)
    assert result.ok and result.data["file"] == "voz.ogg"
    assert result.data["text"] == "Hola desde el audio."
    outside = tmp_path / "otro.mp3"
    outside.write_bytes(b"ID3")
    assert not TOOL.handler({"path": str(outside)}, ctx).ok
    assert seen == []


def test_without_the_engine_the_owner_is_told_how_to_install_it(services, tmp_path):  # noqa: F811
    root = tmp_path / "project"
    root.mkdir()
    (root / "voz.mp3").write_bytes(b"ID3")
    missing = SpeechService(services.ctx, env={"PATH": ""}, runner=Runner())
    seen: list = []
    result = TOOL.handler({"path": "voz.mp3"}, _ctx(root, missing, seen))
    assert not result.ok and result.data == {"speech_error": "NOT_INSTALLED"}
    assert "Settings → Dictation" in result.error.message
    assert seen == [
        (
            "speech.setup_needed",
            {"reason": "NOT_INSTALLED", "tool": "audio.transcribe", "file": "voz.mp3"},
        )
    ]
