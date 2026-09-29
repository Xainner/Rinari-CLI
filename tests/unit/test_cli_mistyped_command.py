"""A mistyped subcommand must not start a real model turn."""

from __future__ import annotations

import pytest

from rinari.cli import main as cli_main
from rinari.cli.main import mistyped_command


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (["sesion", "list"], ("sesion", "session")),
        (["--json", "sesions"], ("sesions", "session")),
        (["revew", "this"], ("revew", "review")),
    ],
)
def test_a_near_miss_of_a_command_is_caught(args, expected) -> None:
    assert mistyped_command(args) == expected


@pytest.mark.parametrize(
    "args",
    [
        ["sessions", "list"],  # hidden alias of `session`
        ["session", "list"],
        ["fix the checkout bug"],  # a quoted prompt is always a prompt
        ["arregla", "el", "test"],
        ["explain", "this"],
        [],
    ],
)
def test_real_commands_and_prompts_pass(args) -> None:
    assert mistyped_command(args) is None


def test_main_refuses_the_typo_without_starting_a_turn(monkeypatch, capsys) -> None:
    started: list[str] = []
    monkeypatch.setattr(cli_main, "start_flow", lambda *a, **k: started.append("turn"))
    monkeypatch.setattr("sys.argv", ["rinari", "sesions", "list"])
    code = cli_main.main()
    assert code == 2
    assert started == []
    err = capsys.readouterr().err
    assert "rinari session" in err
    assert 'rinari "sesions' in err
