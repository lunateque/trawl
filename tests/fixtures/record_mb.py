"""Record the MusicBrainz responses used by the offline tests."""

import json
from pathlib import Path
from time import monotonic, sleep
from typing import Any, cast

import httpx

BASE = "https://musicbrainz.org/ws/2"
DIRECTORY = Path(__file__).parent / "mb"
LOOKUP_INC = "recordings+artist-credits+release-groups+media+isrcs+labels+genres"
BROWSE_INC = "media+recordings+artist-credits+release-groups"


def main() -> None:
    DIRECTORY.mkdir(parents=True, exist_ok=True)
    last_start = float("-inf")

    def limit(_: httpx.Request) -> None:
        nonlocal last_start
        sleep(max(0, 1.0 - (monotonic() - last_start)))
        last_start = monotonic()

    with httpx.Client(
        headers={"User-Agent": "trawl/0.1.0 ( me@lunateque.com )"},
        timeout=30,
        follow_redirects=True,
        event_hooks={"request": [limit]},
    ) as client:

        def record(name: str, endpoint: str, **params: str) -> dict[str, Any]:
            path = DIRECTORY / f"{name}.json"
            if path.exists():
                return cast("dict[str, Any]", json.loads(path.read_text(encoding="utf-8")))
            url = f"{BASE}/{endpoint}"
            query = {"fmt": "json", **params}
            response = client.get(url, params=query)
            for attempt in range(4):
                if response.status_code not in {429, 503}:
                    break
                retry_after = response.headers.get("Retry-After", "")
                sleep(min(300, int(retry_after)) if retry_after.isdecimal() else 2**attempt)
                response = client.get(url, params=query)
            response.raise_for_status()
            body = cast("dict[str, Any]", response.json())
            path.write_text(
                json.dumps(body, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            return body

        ok = record(
            "ok_search",
            "release-group",
            query='releasegroup:"OK Computer" AND artist:"Radiohead"',
        )["release-groups"][0]
        editions = record(
            "ok_editions", "release", **{"release-group": ok["id"], "inc": BROWSE_INC, "limit": "8"}
        )
        multi_id = next(
            release["id"] for release in editions["releases"] if len(release["media"]) > 1
        )
        multi = record("multi_release", f"release/{multi_id}", inc=LOOKUP_INC)
        medium = multi["media"][0]
        if not medium.get("pregap") or not medium.get("data-tracks"):
            track = medium["tracks"][0]
            medium["pregap"] = {
                **track,
                "id": "00000000-0000-4000-8000-000000000001",
                "position": 0,
                "number": "0",
            }
            medium["data-tracks"] = [
                {**track, "id": "00000000-0000-4000-8000-000000000002", "number": "DATA"}
            ]
            (DIRECTORY / "extra_tracks_synthetic.json").write_text(
                json.dumps(multi, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

        kino = record("kino_search", "artist", query='artist:"Кино"', limit="10")["artists"][0]
        record(
            "kino_groups", "release-group", artist=kino["id"], type="album|ep|single", limit="100"
        )
        lucky = next(
            track["recording"]["id"]
            for disc in multi["media"]
            for track in disc["tracks"]
            if track["title"] == "Lucky"
        )
        record(
            "recording_lucky",
            f"recording/{lucky}",
            inc="releases+release-groups+artist-credits+isrcs",
        )


if __name__ == "__main__":
    main()
