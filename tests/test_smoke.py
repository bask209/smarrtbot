import pytest

from smarrtbot import __version__
from smarrtbot.cli import main


def test_version_is_set():
    assert __version__


def test_cli_runs(capsys):
    assert main([]) == 0
    assert "smarrtbot" in capsys.readouterr().out


def test_cli_version_flag(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert "smarrtbot" in capsys.readouterr().out
