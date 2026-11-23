import sqlite3
from collections.abc import Mapping
from enum import StrEnum
from typing import Literal


class TrackState(StrEnum):
    PLANNED = "planned"
    SEARCHING = "searching"
    FETCHED = "fetched"
    VERIFIED = "verified"
    TAGGED = "tagged"
    DONE = "done"
    MISSING = "missing"
    SKIPPED = "skipped"


class Actor(StrEnum):
    PLAN = "plan"
    RUNNER = "runner"
    USER = "user"


_P, _S, _F, _V, _T, _D, _M, _K = TrackState
_RUN = frozenset({Actor.RUNNER})
_USER = frozenset({Actor.USER})
_BOTH = _RUN | _USER

ALLOWED: Mapping[tuple[TrackState, TrackState], frozenset[Actor]] = {
    (_P, _S): _BOTH,
    (_S, _F): _RUN,
    (_F, _S): _RUN,
    (_F, _V): _RUN,
    (_V, _T): _RUN,
    (_T, _D): _RUN,
    (_T, _T): _RUN,
    (_S, _M): _RUN,
    (_S, _P): _RUN,
    (_M, _P): _BOTH,
    (_K, _P): _USER,
    (_M, _S): _USER,
    (_S, _S): _USER,
    (_P, _V): _RUN,
    (_M, _V): _USER,
    (_D, _D): _RUN,
    **{(state, _K): _USER for state in TrackState if state != _D},
}

_MUTABLE = {
    "pinned_candidate",
    "source",
    "codec",
    "lossless",
    "bitrate",
    "verified_by",
    "path",
    "next_try_at",
}


def transition(
    conn: sqlite3.Connection,
    track_id: int,
    src: TrackState,
    dst: TrackState,
    actor: Actor,
    *,
    expected_pin: str | Literal[False] | None = False,
    **fields: object,
) -> bool:
    if actor not in ALLOWED.get((src, dst), ()):
        raise ValueError(f"transition {src} -> {dst} not allowed for {actor}")
    if unknown := fields.keys() - _MUTABLE:
        raise ValueError(f"unknown transition fields: {', '.join(sorted(unknown))}")
    assignments = ", ".join(["state = ?", *(f"{name} = ?" for name in fields)])
    sql = f"UPDATE tracks SET {assignments} WHERE id = ? AND state = ?"  # noqa: S608
    params = (dst, *fields.values(), track_id, src)
    if expected_pin is not False:
        sql += " AND pinned_candidate IS ?"
        params += (expected_pin,)
    return conn.execute(sql, params).rowcount == 1
