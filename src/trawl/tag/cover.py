from dataclasses import dataclass

from trawl.net.client import HttpClient, HttpError


@dataclass(frozen=True)
class Cover:
    full: bytes
    embed: bytes


async def fetch_cover(http: HttpClient, release_mbid: str, release_group_mbid: str) -> Cover | None:
    for kind, mbid in (("release", release_mbid), ("release-group", release_group_mbid)):
        root = f"https://coverartarchive.org/{kind}/{mbid}"
        try:
            full = await http.get_bytes(f"{root}/front")
        except HttpError as exc:
            if exc.status != 404:
                raise
            continue
        try:
            embed = await http.get_bytes(f"{root}/front-1200")
        except HttpError as exc:
            if exc.status != 404:
                raise
            embed = full
        return Cover(full, embed)
    return None
