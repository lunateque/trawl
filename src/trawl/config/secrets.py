import logging
import os
import stat
from ipaddress import ip_address
from pathlib import Path
from tomllib import TOMLDecodeError, loads
from urllib.parse import urlsplit

from trawl.config.models import (
    ConfigError,
    Secrets,
)
from trawl.config.values import (
    boolean,
    check_keys,
    string,
)


def slskd_insecure(url: str) -> bool:
    parsed = urlsplit(url)
    if parsed.scheme != "http":
        return False
    host = parsed.hostname or ""
    try:
        return not ip_address(host).is_loopback
    except ValueError:
        return host.rstrip(".") != "localhost"


def secrets_permissions_warning(config: Path) -> str | None:
    if os.name != "posix":
        return None
    path = config.with_name("secrets.toml")
    try:
        if path.stat().st_mode & (stat.S_IRGRP | stat.S_IROTH):
            return f"{path}: group/world readable; restrict permissions with chmod 600"
    except FileNotFoundError:
        pass
    return None


def load_secrets(config: Path) -> Secrets:
    path = config.with_name("secrets.toml")
    try:
        if warning := secrets_permissions_warning(config):
            logging.getLogger(__name__).warning("%s", warning)
        text = path.read_text(encoding="utf-8")
        values: dict[str, object] = loads(text)
        check_keys(
            values,
            {
                "acoustid_key",
                "youtube_cookies",
                "slskd_url",
                "slskd_api_key",
                "slskd_allow_insecure",
                "listenbrainz_token",
                "yandex_token",
                "spotify_client_id",
                "spotify_refresh_token",
            },
            text,
        )
        key = string(values, "acoustid_key", "").strip()
        cookies = string(values, "youtube_cookies", "")
        url = string(values, "slskd_url", "http://localhost:5030").strip()
        try:
            parsed = urlsplit(url)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.query
                or parsed.fragment
                or parsed.port == 0
            ):
                raise ValueError
        except ValueError:
            raise ConfigError(
                "slskd_url: expected an HTTP(S) URL without credentials, query or fragment"
            ) from None
        insecure = boolean(values.get("slskd_allow_insecure", False), "slskd_allow_insecure")
        if slskd_insecure(url) and not insecure:
            raise ConfigError(
                "slskd_url: HTTP is allowed only for loopback hosts; use HTTPS or explicitly "
                "set slskd_allow_insecure = true in secrets.toml"
            )
        return Secrets(
            key or None,
            path.parent / cookies if cookies else None,
            url,
            string(values, "slskd_api_key", "").strip() or None,
            string(values, "listenbrainz_token", "").strip() or None,
            string(values, "yandex_token", "").strip() or None,
            string(values, "spotify_client_id", "").strip() or None,
            string(values, "spotify_refresh_token", "").strip() or None,
            insecure,
        )
    except FileNotFoundError:
        return Secrets()
    except TOMLDecodeError as exc:
        raise ConfigError(f"{path}: invalid TOML") from exc
    except (OSError, UnicodeError, ConfigError) as exc:
        raise ConfigError(f"{path}: {exc}") from exc
