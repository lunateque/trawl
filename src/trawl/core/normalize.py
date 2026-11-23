import re
import unicodedata

_REMASTER = re.compile(
    r"(?: - (?:\d{4} )?remaster(?:ed)?(?: \d{4})?(?: version)?"
    r"| \((?:\d{4} )?remaster(?:ed)?(?: \d{4})?\))$"
)
_CONJUNCTION = re.compile(r"(?<= )(?:&|и)(?= )")


def _without_punctuation(value: str) -> str:
    return " ".join(
        "".join(char for char in value if not unicodedata.category(char).startswith("P")).split()
    )


def norm_title(s: str) -> str:
    s = unicodedata.normalize("NFKC", s).casefold().strip()
    return _without_punctuation(_REMASTER.sub("", s))


def norm_artist(s: str) -> str:
    s = " ".join(unicodedata.normalize("NFKC", s).casefold().split())
    return _without_punctuation(_CONJUNCTION.sub("and", s)).removeprefix("the ")
