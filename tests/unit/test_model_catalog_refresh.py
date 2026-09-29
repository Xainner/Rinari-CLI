"""The public model catalog: downloaded by "Refresh models", newest wins."""

from __future__ import annotations

import pytest

from rinari.providers import metadata


class _Response:
    def __init__(self, payload, status=200):
        self._payload, self.status_code = payload, status

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


PUBLIC = {
    "opencode-go": {
        "models": {
            "space-bunny-free": {
                "limit": {"context": 1048576, "input": 524288, "output": 524288},
                "name": "Space Bunny Free",
                "cost": {"input": 0},
            }
        }
    }
}


@pytest.fixture(autouse=True)
def cache_dir(tmp_path):
    metadata.use_cache_dir(tmp_path / "cache")
    yield tmp_path / "cache"
    metadata.use_cache_dir(None)


def test_refreshing_brings_a_model_the_bundled_snapshot_lacks(cache_dir):
    """Card 05: Space Bunny was published a day after the snapshot."""
    calls = []

    def get(url, **kwargs):
        calls.append(url)
        return _Response(PUBLIC)

    summary = metadata.refresh_public_catalog(get)
    assert calls == [metadata.PUBLIC_CATALOG_URL]
    assert summary["models"] == 1
    entry = metadata.catalog()["models"]["opencode-go"]["space-bunny-free"]
    assert entry["limit"]["context"] == 1048576
    assert "cost" not in entry  # only the fields Rinari uses are kept
    assert (cache_dir / "model_metadata.json").is_file()


def test_an_older_download_does_not_replace_the_bundled_snapshot(cache_dir):
    cache_dir.mkdir()
    (cache_dir / "model_metadata.json").write_text(
        '{"source": "x", "updated_at": "2000-01-01T00:00:00+00:00", "models": {}}',
        encoding="utf-8",
    )
    metadata.use_cache_dir(cache_dir)
    assert metadata.catalog()["updated_at"] > "2000"


def test_a_failed_download_keeps_the_catalog_in_use(cache_dir):
    before = metadata.catalog()
    with pytest.raises(RuntimeError):
        metadata.refresh_public_catalog(lambda url, **kwargs: _Response({}, status=503))
    with pytest.raises(ValueError):
        metadata.refresh_public_catalog(lambda url, **kwargs: _Response({"other": {}}))
    assert metadata.catalog() is before
    assert not (cache_dir / "model_metadata.json").exists()
