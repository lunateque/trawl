from pathlib import Path

import pytest

from trawl.config.load import config_path, load
from trawl.config.models import ConfigError, MbConfig, PlanConfig

MINIMAL = 'library_root = "library"\ncontact_email = "owner@example.com"\n'


def test_load_defaults(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(MINIMAL, encoding="utf-8")
    cfg = load(path)
    assert cfg.source_path == path
    assert cfg.library_root == Path("library")
    assert cfg.contact_email == "owner@example.com"
    assert cfg.plan == PlanConfig()
    assert cfg.mb == MbConfig()
    assert cfg.data_dir.name == "trawl"


def test_default_library_root(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('contact_email = "owner@example.com"\n', encoding="utf-8")
    assert load(path).library_root == Path("D:/music")


def test_load_full(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        MINIMAL
        + 'data_dir = "data"\n[mb]\ncache_days = 0\n[plan]\ntypes = ["album"]\n'
        + 'countries = ["DE"]\nprefer = "deluxe"\ndiscography_limit = 2\n',
        encoding="utf-8",
    )
    cfg = load(path)
    assert cfg.data_dir == Path("data")
    assert cfg.mb == MbConfig(0)
    assert cfg.plan == PlanConfig(("album",), ("DE",), "deluxe", 2)


def test_load_network_proxy(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        MINIMAL + '[net.proxy]\nurl = "http://proxy.example:8888"\n'
        'hosts = ["Archive.ORG", "*.archive.org", "coverartarchive.org"]\n',
        encoding="utf-8",
    )
    proxy = load(path).proxy
    assert proxy.url == "http://proxy.example:8888"
    assert proxy.hosts == ("archive.org", "*.archive.org", "coverartarchive.org")


@pytest.mark.parametrize(
    ("text", "error"),
    [
        ('library_root = false\ncontact_email = "a@b"', "library_root.*string"),
        ('library_root = "a"', "contact_email"),
        (MINIMAL.replace("owner@example.com", ""), "contact_email"),
        (MINIMAL.replace("owner@example.com", "owner"), "contact_email"),
        (MINIMAL + "extra = 1", "extra.*line 3"),
        (MINIMAL + '[plan]\nprefr = "a"', r"plan\.prefr.*line 4"),
        (MINIMAL + 'plan = { prefr = "a" }', r"plan\.prefr.*line 3"),
        (MINIMAL + "[plan]\ntypes = [1]", r"plan\.types.*strings"),
        (MINIMAL + '[plan]\nprefer = "later"', r"plan\.prefer"),
        (MINIMAL + "[plan]\ndiscography_limit = 0", r"plan\.discography_limit"),
        (MINIMAL + "[mb]\ncache_days = -1", r"mb\.cache_days"),
        (MINIMAL + "[mb]\ncache_days = true", r"mb\.cache_days.*integer"),
        (MINIMAL + '[net.proxy]\nurl = "file:///tmp/proxy"', r"net\.proxy\.url"),
        (MINIMAL + '[net.proxy]\nurl = "http://user:pwd@proxy.test"', r"net\.proxy\.url"),
        (MINIMAL + '[net.proxy]\nhosts = ["*archive.org"]', r"net\.proxy\.hosts"),
        (MINIMAL + '[net.proxy]\nunknown = "a"', r"net\.proxy\.unknown"),
        (MINIMAL + "plan = []", "plan.*table"),
    ],
)
def test_invalid_config(tmp_path: Path, text: str, error: str) -> None:
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError, match=error):
        load(path)


def test_missing_config(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"missing.toml.*config.example.toml"):
        load(tmp_path / "missing.toml")


def test_path_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRAWL_CONFIG", raising=False)
    assert config_path(None).name == "config.toml"
    monkeypatch.setenv("TRAWL_CONFIG", "from-env.toml")
    assert config_path(None) == Path("from-env.toml")
    assert config_path(Path("explicit.toml")) == Path("explicit.toml")
