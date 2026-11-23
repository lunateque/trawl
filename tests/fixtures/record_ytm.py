"""Record the public YouTube Music queries used by the offline source test."""

import json
from pathlib import Path

from ytmusicapi import YTMusic  # pyright: ignore[reportMissingTypeStubs]


def main() -> None:
    api = YTMusic()
    albums = api.search("The Strokes The Modern Age", filter="albums")[:5]
    details = {album["browseId"]: api.get_album(album["browseId"]) for album in albums}
    songs = api.search("The Strokes Last Nite", filter="songs")[:5]
    path = Path(__file__).with_name("ytmusic.json")
    path.write_text(
        json.dumps(
            {"albums": albums, "details": details, "songs": songs}, ensure_ascii=False, indent=2
        )
        + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
