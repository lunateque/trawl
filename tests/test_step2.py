from pathlib import Path

import pytest
from typer.testing import CliRunner

from trawl.cli import app
from trawl.config import ConfigError, load


def test_run_requires_acoustid(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        'contact_email="a@b"\n' + f'data_dir="{(tmp_path / "data").as_posix()}"\n',
        encoding="utf-8",
    )
    result = CliRunner().invoke(app, ["--config", str(path), "run", "--once"])
    assert result.exit_code == 1
    assert "https://acoustid.org/new-application" in result.output


@pytest.mark.parametrize("source", ["soulseek", "local"])
def test_reject_unimplemented_source(tmp_path: Path, source: str) -> None:
    path = tmp_path / "config.toml"
    path.write_text(f'contact_email="a@b"\n[sources]\norder=["{source}"]\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="available from step"):
        load(path)
