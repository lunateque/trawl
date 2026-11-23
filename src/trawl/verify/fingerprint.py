import json
from dataclasses import dataclass
from pathlib import Path

from trawl.mb.models import array_value, object_value
from trawl.net.client import HttpClient, HttpError
from trawl.verify.probe import AudioError, command


@dataclass(frozen=True)
class AcoustResult:
    score: float
    recording_ids: tuple[str, ...]


def fingerprint(path: Path) -> tuple[int, str]:
    result = command("fpcalc", "-json", "-length", "120", str(path))
    try:
        body = object_value(json.loads(result.stdout))
        duration, fp = body["duration"], body["fingerprint"]
        if isinstance(duration, int | float) and isinstance(fp, str) and fp:
            return round(duration), fp
    except (KeyError, ValueError, OverflowError):
        pass
    raise AudioError("fpcalc did not produce a fingerprint")


class AcoustId:
    URL = "https://api.acoustid.org/v2/lookup"

    def __init__(self, http: HttpClient, key: str) -> None:
        self._http = http
        self._key = key

    async def lookup(self, duration: int, fp: str) -> list[AcoustResult]:
        body = object_value(
            await self._http.post_form(
                self.URL,
                {
                    "client": self._key,
                    "duration": str(duration),
                    "fingerprint": fp,
                    "meta": "recordingids",
                },
            )
        )
        if body.get("status") != "ok":
            raise HttpError(self.URL, 200, "AcoustID returned an error (check the application key)")
        results: list[AcoustResult] = []
        for item in array_value(body.get("results")):
            result = object_value(item)
            score = result.get("score")
            if not isinstance(score, int | float) or isinstance(score, bool):
                raise HttpError(self.URL, 200, "invalid AcoustID result score")
            ids = tuple(
                mbid
                for raw in array_value(result.get("recordings"))
                if isinstance(mbid := object_value(raw).get("id"), str)
            )
            results.append(AcoustResult(float(score), ids))
        return results
