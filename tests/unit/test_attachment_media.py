"""Audio and video attachments are kept as files (plan 0.4.0 #57)."""

from __future__ import annotations

import base64
import wave

import pytest

from rinari.artifacts import attachments
from rinari.artifacts.attachments import attachment_prompt, classify, prepare_attachments
from rinari.artifacts.store import ArtifactStore


def _wav(path, seconds=1.5):
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(8000)
        out.writeframes(b"\0\0" * int(8000 * seconds))
    return path


def test_audio_and_video_are_recognized(tmp_path):
    cases = {
        "a.mp3": b"ID3\x03" + b"\0" * 20,
        "b.wav": None,
        "c.mp4": b"\0\0\0\x18ftypisom" + b"\0" * 8,
        "d.webm": b"\x1a\x45\xdf\xa3" + b"\0" * 12,
        "e.m4a": b"\0\0\0\x18ftypM4A " + b"\0" * 8,
        "f.mov": b"\0" * 16,
    }
    kinds = {}
    for name, body in cases.items():
        path = tmp_path / name
        if body is None:
            _wav(path)
        else:
            path.write_bytes(body)
        kinds[name] = classify(path)[0]
    assert kinds == {
        "a.mp3": "audio",
        "b.wav": "audio",
        "c.mp4": "video",
        "d.webm": "video",
        "e.m4a": "audio",
        "f.mov": "video",
    }


def test_a_song_is_kept_as_a_file_with_its_duration(app_ctx, tmp_path):
    song = _wav(tmp_path / "Time Force.wav")
    prepared = prepare_attachments(ArtifactStore(app_ctx), "ses_m", [str(song)])
    item = prepared[0]
    assert item.kind == "audio" and item.extracted_text == "" and item.images == ()
    assert item.reference()["duration_s"] == 1.5
    prompt = attachment_prompt(prepared)
    assert '"Time Force.wav" - audio/wav' in prompt
    assert "1.5 s" in prompt and "audio.transcribe" in prompt


def test_media_skips_the_document_cap(app_ctx, tmp_path, monkeypatch):
    monkeypatch.setattr(attachments, "MAX_DOCUMENT_BYTES", 10)
    monkeypatch.setattr(attachments, "MAX_TOTAL_BYTES", 10)
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"\0\0\0\x18ftypisom" + b"\0" * 200)
    prepared = prepare_attachments(ArtifactStore(app_ctx), "ses_m", [str(clip)])
    assert prepared[0].kind == "video"
    monkeypatch.setattr(attachments, "MAX_MEDIA_BYTES", 100)
    with pytest.raises(ValueError, match=r"clip\.mp4"):
        prepare_attachments(ArtifactStore(app_ctx), "ses_m", [str(clip)])


def test_errors_name_the_real_file_not_the_temporary_one(app_ctx):
    data = "data:application/octet-stream;base64," + base64.b64encode(b"\x00\x01binary").decode()
    with pytest.raises(ValueError) as excinfo:
        prepare_attachments(
            ArtifactStore(app_ctx), "ses_m", [{"data_url": data, "name": "ers - Time Force.bin"}]
        )
    assert "ers - Time Force.bin" in str(excinfo.value)
    assert "rinari-attachment-" not in str(excinfo.value)
