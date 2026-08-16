from typer.testing import CliRunner

from rinari import __version__
from rinari.cli.main import app

runner = CliRunner()


def test_version_is_positive_semver():
    parts = __version__.split(".")
    assert len(parts) == 3
    assert all(part.isdigit() for part in parts)


def test_cli_version_flag():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.output.strip() == __version__
