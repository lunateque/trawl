import asyncio
import base64
import hashlib
import json
import os
import re
import secrets
import tempfile
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from tomllib import loads
from urllib.parse import parse_qs, urlencode, urlsplit

from trawl.config.models import ConfigError
from trawl.net.client import HttpClient
from trawl.taste.spotify import spotify_token, text

_CALLBACK = "http://127.0.0.1:8888/callback"
_KEY = "spotify_refresh_token"
_ASSIGNMENT = re.compile(r"""^[ \t]*(?P<key>[\w-]+|'[^']*'|"(?:\\.|[^"\\])*")[ \t]*=[ \t]*""")


def replace_token(original: str, token: str) -> str:
    rendered = json.dumps(token, ensure_ascii=False)
    if _KEY not in (values := loads(original)):
        newline = "\r\n" if "\r\n" in original else "\n"
        return f"{_KEY} = {rendered}{newline}{original}"
    for line in re.finditer(r"^.*", original, flags=re.M):
        match = _ASSIGNMENT.match(line[0])
        if match is None or _KEY not in loads(f"{match['key']}=1"):
            continue
        try:
            prefix = loads(original[: line.start()])
        except ValueError:
            continue
        if _KEY in prefix:
            break
        first = line.start() + match.end()
        # Preserve the assignment's whitespace and trailing comment, including multiline strings.
        quote = original[first : first + 1]
        for last in (
            index + 1 for index in range(first, len(original)) if original[index] == quote
        ):
            try:
                candidate = f"{original[:first]}{rendered}{original[last:]}"
                if loads(candidate) == {**values, _KEY: token}:
                    return candidate
            except ValueError:
                continue
        break
    raise ConfigError("Could not locate spotify_refresh_token assignment in secrets.toml")


def write_token(path: Path, token: str) -> None:
    try:
        with path.open(encoding="utf-8", newline="") as stream:
            original = stream.read()
    except FileNotFoundError:
        original = ""
    content = replace_token(original, token)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent, mode="w", encoding="utf-8", newline="", delete=False
        ) as stream:
            temp = Path(stream.name)
            stream.write(content)
        if os.name == "posix":
            temp.chmod(0o600)
        temp.replace(path)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


def _authorize(client_id: str, verifier: str, state: str) -> str:
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    )
    params = urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "scope": "user-library-read playlist-read-private playlist-read-collaborative",
            "redirect_uri": _CALLBACK,
            "state": state,
            "code_challenge_method": "S256",
            "code_challenge": challenge,
        }
    )
    code = ""
    denied = False

    class Callback(BaseHTTPRequestHandler):
        def setup(self) -> None:
            self.request.settimeout(5)
            super().setup()

        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_GET(self) -> None:
            nonlocal code, denied
            url = urlsplit(self.path)
            query = parse_qs(url.query)
            if url.path != "/callback" or query.get("state") != [state]:
                self.send_error(400, "Invalid Spotify callback")
                return
            values = query.get("code", [])
            if len(values) == 1:
                code = values[0]
            else:
                denied = True
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Callback received. Return to trawl.")

    with HTTPServer(("127.0.0.1", 8888), Callback) as server:
        if not webbrowser.open(f"https://accounts.spotify.com/authorize?{params}"):
            raise ConfigError("Could not open the browser for Spotify authorization")
        server.timeout = 1
        deadline = time.monotonic() + 180
        while not (code or denied) and time.monotonic() < deadline:
            server.handle_request()
    if not code:
        raise ConfigError("Spotify authorization was declined or timed out")
    return code


async def auth_spotify(http: HttpClient, client_id: str, path: Path) -> None:
    verifier, state = secrets.token_urlsafe(48), secrets.token_urlsafe(24)
    code = await asyncio.to_thread(_authorize, client_id, verifier, state)
    result = await spotify_token(
        http,
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": _CALLBACK,
            "client_id": client_id,
            "code_verifier": verifier,
        },
    )
    if not (refresh := text(result.get("refresh_token"))):
        raise ConfigError("Spotify did not return a refresh token")
    await asyncio.to_thread(write_token, path, refresh)
