import pytest

import sentinel
from sentinel.cli import main


def test_package_exposes_version() -> None:
    assert sentinel.__version__ == "0.1.0"


def test_cli_without_arguments_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    assert "Sentinel FX" in capsys.readouterr().out


def test_cli_version_flag_exits_cleanly() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
