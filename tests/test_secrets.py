"""Tests del bloqueo de secrets en las tools de archivo."""

import pytest

from rinari.agent.tools import _resolve, is_secret_path, read_file, write_file


@pytest.fixture
def workdir(tmp_path):
    return str(tmp_path)


def test_env_file_is_secret(tmp_path):
    p = tmp_path / ".env"
    p.write_text("SECRET=1\n")
    assert is_secret_path(_resolve(str(tmp_path), ".env"))


def test_key_and_pem_are_secret(tmp_path):
    for name in ("id_rsa", "server.key", "cert.pem", "credentials.json"):
        p = tmp_path / name
        p.write_text("x\n")
        assert is_secret_path(_resolve(str(tmp_path), name)), name


def test_ssh_dir_is_secret(tmp_path):
    d = tmp_path / ".ssh"
    d.mkdir()
    (d / "id_ed25519").write_text("x\n")
    assert is_secret_path(_resolve(str(tmp_path), ".ssh/id_ed25519"))


def test_normal_files_not_secret(tmp_path):
    (tmp_path / "main.py").write_text("x\n")
    (tmp_path / "README.md").write_text("x\n")
    assert not is_secret_path(_resolve(str(tmp_path), "main.py"))
    assert not is_secret_path(_resolve(str(tmp_path), "README.md"))


def test_read_file_blocks_env(workdir, tmp_path):
    p = tmp_path / ".env"
    p.write_text("SECRET=1\n")
    result = read_file({"path": str(p)}, cwd=workdir)
    assert result["ok"] is False
    assert "secret" in result["error"].lower()


def test_write_file_blocks_env(workdir, tmp_path):
    result = write_file({"path": str(tmp_path / ".env"), "content": "SECRET=1"}, cwd=workdir)
    assert result["ok"] is False
    assert "secret" in result["error"].lower()
    assert not (tmp_path / ".env").exists()


def test_read_file_allows_normal(workdir, tmp_path):
    p = tmp_path / "main.py"
    p.write_text("print('hi')\n")
    result = read_file({"path": str(p)}, cwd=workdir)
    assert result["exists"] is True
    assert "hi" in result["content"]
