import pytest

from trawl.normalize import norm_artist, norm_title


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("  \uff2f\uff2b  Computer - 2011 Remastered Version  ", "ok computer"),
        ("Hey! (Remaster 2009)", "hey"),
        ("Hey - Remaster", "hey"),
        ("Hey - Live", "hey live"),
        ("Love, Etc.", "love etc"),
        ("КИНО", "кино"),
        ("夜が明けたら", "夜が明けたら"),
    ],
)
def test_title(title: str, expected: str) -> None:
    assert norm_title(title) == expected


@pytest.mark.parametrize(
    ("artist", "expected"),
    [
        (" The Strokes ", "strokes"),
        ("Кино", "кино"),
        ("A & B", "a and b"),
        ("A и B", "a and b"),
        ("A And B", "a and b"),
        ("Earth,   Wind & Fire", "earth wind and fire"),
    ],
)
def test_artist(artist: str, expected: str) -> None:
    assert norm_artist(artist) == expected
