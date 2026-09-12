import hashlib
import subprocess
import sys

import pytest
from PIL import Image

from rinari.artifacts.ssh_transfer import _REMOTE
from rinari.artifacts.store import ArtifactStore
from rinari.artifacts.transfer import import_file
from rinari.models.images import references
from rinari.models.types import ChatMessage
from rinari.providers.adapters.anthropic import _convert_to_anthropic
from rinari.providers.adapters.openai_compatible import _message_to_openai
from rinari.providers.adapters.responses import _message_to_responses


def test_synthetic_image_reaches_all_transports(app_ctx, tmp_path):
    path = tmp_path / "synthetic.png"
    Image.new("RGB", (32, 32), "blue").save(path)
    original = path.read_bytes()
    store = ArtifactStore(app_ctx)
    record = import_file(store, "ses_image", path)
    attachments = [{"uri": record.uri(), "sha256": record.sha256}]
    refs = references(store, "ses_image", attachments)
    message = ChatMessage(role="user", content="Describe esta imagen sintética", images=refs)
    assert _message_to_openai(message)["content"][1]["type"] == "image_url"
    assert _message_to_responses(message, {})[0]["content"][1]["type"] == "input_image"
    assert _convert_to_anthropic((message,))[1][0]["content"][1]["type"] == "image"
    assert store.get(record.uri()) == original
    with pytest.raises(ValueError):
        references(store, "other", attachments)
    with pytest.raises(ValueError):
        references(store, "ses_image", attachments * 5)


def test_import_quota_and_ssh_transfer_integrity(app_ctx, tmp_path):
    path = tmp_path / "synthetic.bin"
    content = b"synthetic payload" * 100
    path.write_bytes(content)
    with pytest.raises(ValueError, match="quota"):
        import_file(ArtifactStore(app_ctx), "ses_file", path, quota=1)
    # Execute the exact remote program without SSH or any real server/media access.
    result = subprocess.run(
        [sys.executable, "-c", _REMOTE, str(path.resolve()), "10000"], capture_output=True
    )
    assert result.returncode == 0, result.stderr
    header, payload = result.stdout.split(b"\n", 1)
    assert payload[:-64] == content
    assert payload[-64:] == hashlib.sha256(content).hexdigest().encode()


def test_media_quota_excludes_logs_and_invalid_hash_never_commits(app_ctx, tmp_path):
    store = ArtifactStore(app_ctx)
    store.create("ses_file", "logs", "output.txt", b"log" * 100)
    source = tmp_path / "small.bin"
    source.write_bytes(b"x")
    with pytest.raises(ValueError, match="validation"):
        import_file(store, "ses_file", source, quota=2, expected_hash="0" * 64)
    assert not [r for r in store.list(session_id="ses_file") if r.namespace == "media"]
    result = import_file(store, "ses_file", source, quota=2, provenance="local:synthetic")
    assert result.provenance == "local:synthetic"
