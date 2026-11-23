import re
from urllib.parse import parse_qs, urlsplit

_VIDEO = re.compile(r"[A-Za-z0-9_-]{11}")


def video_id(url: str) -> str:
    parsed = urlsplit(url)
    host = parsed.hostname
    value = ""
    if parsed.scheme in {"https", "http"}:
        if host == "youtu.be":
            value = parsed.path.removeprefix("/")
        elif host in {"youtube.com", "www.youtube.com", "music.youtube.com", "m.youtube.com"}:
            if parsed.path == "/watch":
                value = parse_qs(parsed.query).get("v", [""])[0]
            elif parsed.path.startswith("/shorts/"):
                value = parsed.path.removeprefix("/shorts/")
    if not _VIDEO.fullmatch(value):
        raise ValueError("expected a YouTube or YouTube Music video URL")
    return value
