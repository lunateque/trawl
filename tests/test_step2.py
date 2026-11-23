from pathlib import Path

import pytest
from typer.testing import CliRunner

from trawl.cli import app
from trawl.config.load import load
from trawl.config.models import ConfigError


def test_run_requires_acoustid(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        'contact_email="a@b"\n' + f'data_dir="{(tmp_path / "data").as_posix()}"\n',
        encoding="utf-8",
    )
    result = CliRunner().invoke(app, ["--config", str(path), "run", "--once"])
    assert result.exit_code == 1
    assert "https://acoustid.org/new-application" in result.output


def test_soulseek_requires_download_directory(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        'contact_email="a@b"\n[sources]\norder=["local","soulseek","ytmusic"]\n', encoding="utf-8"
    )
    with pytest.raises(ConfigError, match="download_dir"):
        load(path)
    with path.open("a", encoding="utf-8") as config:
        config.write('[sources.soulseek]\ndownload_dir="download"\n')
    assert load(path).sources.order == ("local", "soulseek", "ytmusic")
