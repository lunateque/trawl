from base64 import b64encode
from pathlib import Path
from typing import Any

from mutagen import File, MutagenError  # pyright: ignore[reportMissingTypeStubs,reportUnknownVariableType]
from mutagen.flac import FLAC, Picture  # pyright: ignore[reportMissingTypeStubs]
from mutagen.id3 import APIC, TXXX, UFID, USLT, Frames  # pyright: ignore[reportMissingTypeStubs,reportUnknownVariableType]
from mutagen.mp3 import MP3  # pyright: ignore[reportMissingTypeStubs]
from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm  # pyright: ignore[reportMissingTypeStubs]
from mutagen.oggopus import OggOpus  # pyright: ignore[reportMissingTypeStubs]
from mutagen.oggvorbis import OggVorbis  # pyright: ignore[reportMissingTypeStubs]

from trawl.tag.mapping import TagFields
from trawl.verify.probe import AudioError

# Vorbis comment, MP4 atom/freeform suffix, ID3 frame/description respectively.
_TEXT_TAGS = {
    "title": ("TITLE", "©nam", "TIT2"),
    "artist": ("ARTIST", "©ART", "TPE1"),
    "artists": ("ARTISTS", "ARTISTS", "TXXX:Artists"),
    "albumartist": ("ALBUMARTIST", "aART", "TPE2"),
    "album": ("ALBUM", "©alb", "TALB"),
    "date": ("DATE", "©day", "TDRC"),
    "original_date": ("ORIGINALDATE", "ORIGINALDATE", "TDOR"),
    "recording_mbid": (
        "MUSICBRAINZ_TRACKID",
        "MusicBrainz Track Id",
        "UFID:http://musicbrainz.org",
    ),
    "release_track_mbid": (
        "MUSICBRAINZ_RELEASETRACKID",
        "MusicBrainz Release Track Id",
        "TXXX:MusicBrainz Release Track Id",
    ),
    "release_mbid": ("MUSICBRAINZ_ALBUMID", "MusicBrainz Album Id", "TXXX:MusicBrainz Album Id"),
    "release_group_mbid": (
        "MUSICBRAINZ_RELEASEGROUPID",
        "MusicBrainz Release Group Id",
        "TXXX:MusicBrainz Release Group Id",
    ),
    "artist_mbids": ("MUSICBRAINZ_ARTISTID", "MusicBrainz Artist Id", "TXXX:MusicBrainz Artist Id"),
    "albumartist_mbids": (
        "MUSICBRAINZ_ALBUMARTISTID",
        "MusicBrainz Album Artist Id",
        "TXXX:MusicBrainz Album Artist Id",
    ),
    "release_types": ("RELEASETYPE", "MusicBrainz Album Type", "TXXX:MusicBrainz Album Type"),
    "release_status": (
        "RELEASESTATUS",
        "MusicBrainz Album Status",
        "TXXX:MusicBrainz Album Status",
    ),
    "isrcs": ("ISRC", "ISRC", "TSRC"),
    "labels": ("LABEL", "LABEL", "TPUB"),
    "genres": ("GENRE", "©gen", "TCON"),
}


def _text_values(fields: TagFields, field: str) -> list[str]:
    value: str | tuple[str, ...] | None = getattr(fields, field)
    return [value] if isinstance(value, str) else list(value or ())


def _vorbis(audio: Any, fields: TagFields, cover: bytes | None, lyrics: str | None) -> None:
    for field, (vorbis, _, _) in _TEXT_TAGS.items():
        if values := _text_values(fields, field):
            audio[vorbis] = values
    for name, number in (
        ("TRACKNUMBER", fields.track),
        ("TRACKTOTAL", fields.track_total),
        ("DISCNUMBER", fields.disc),
        ("DISCTOTAL", fields.disc_total),
        ("COMPILATION", int(fields.compilation)),
    ):
        audio[name] = [str(number)]
    if lyrics:
        audio["LYRICS"] = [lyrics]
    if isinstance(audio, FLAC):
        audio.clear_pictures()  # pyright: ignore[reportUnknownMemberType]
    if cover:
        picture: Any = Picture()
        picture.data = cover
        picture.type = 3
        picture.mime = "image/jpeg"
        if isinstance(audio, FLAC):
            audio.add_picture(picture)  # pyright: ignore[reportUnknownMemberType]
        else:
            audio["METADATA_BLOCK_PICTURE"] = [b64encode(picture.write()).decode("ascii")]


def _mp4(audio: Any, fields: TagFields, cover: bytes | None, lyrics: str | None) -> None:
    atoms = {"©nam", "©ART", "aART", "©alb", "©day", "©gen"}
    for field, (_, name, _) in _TEXT_TAGS.items():
        if not (values := _text_values(fields, field)):
            continue
        if name in atoms:
            audio[name] = values
        else:
            audio[f"----:com.apple.iTunes:{name}"] = [
                MP4FreeForm(v.encode("utf-8")) for v in values
            ]
    audio["trkn"] = [(fields.track, fields.track_total)]
    audio["disk"] = [(fields.disc, fields.disc_total)]
    audio["cpil"] = fields.compilation
    if lyrics:
        audio["©lyr"] = [lyrics]
    if cover:
        audio["covr"] = [MP4Cover(cover, imageformat=MP4Cover.FORMAT_JPEG)]  # pyright: ignore[reportUnknownMemberType]


def _id3(tags: Any, fields: TagFields, cover: bytes | None, lyrics: str | None) -> None:
    frames: dict[str, Any] = Frames
    for field, (_, _, name) in _TEXT_TAGS.items():
        if not (values := _text_values(fields, field)):
            continue
        frame, _, description = name.partition(":")
        if frame == "UFID":
            tags.add(UFID(owner=description, data=values[0].encode("ascii")))
        elif frame == "TXXX":
            tags.add(TXXX(encoding=3, desc=description, text=values))
        else:
            tags.add(frames[frame](encoding=3, text=values))
    for name, value in (
        ("TRCK", f"{fields.track}/{fields.track_total}"),
        ("TPOS", f"{fields.disc}/{fields.disc_total}"),
        ("TCMP", str(int(fields.compilation))),
    ):
        tags.add(frames[name](encoding=3, text=[value]))
    if lyrics:
        tags.add(USLT(encoding=3, lang="und", text=lyrics))
    if cover:
        tags.add(APIC(encoding=3, mime="image/jpeg", type=3, data=cover))


def write(path: Path, fields: TagFields, cover_jpeg: bytes | None, lyrics: str | None) -> None:
    try:
        audio: Any = File(path)
        if not isinstance(audio, FLAC | OggOpus | OggVorbis | MP4 | MP3):
            raise AudioError(f"unsupported audio tags: {path.suffix}")
        raw: Any = audio
        if raw.tags is None:
            raw.add_tags()
        tags: Any = raw.tags
        tags.clear()
        if isinstance(audio, MP3):
            _id3(tags, fields, cover_jpeg, lyrics)
            raw.save(v1=0, v2_version=4)
        else:
            if isinstance(audio, MP4):
                _mp4(raw, fields, cover_jpeg, lyrics)
            else:
                _vorbis(raw, fields, cover_jpeg, lyrics)
            raw.save()
    except MutagenError as exc:
        raise AudioError(f"could not write tags: {exc}") from exc
