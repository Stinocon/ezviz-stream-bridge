"""The parameter sets each camera's stream opened with, kept for the sessions that join it later.

A camera keeps one stream running across sessions, and a session that joins it mid-GOP gets
slices with no parameter set to open them by. The cache is how such a join is served: per
serial, process-wide, never expiring -- a camera does not change its sets between sessions of
the same stream, and the next session that opens one overwrites the entry. Two kinds, because
the two transports need different things handed over: an RTP join gets an Annex-B prelude for
the codec it was identified from, an MPEG-PS join the sets wrapped in a pack of their own.

Split out of `session.py` in 0.1.18. What goes in is still decided there: only sets the stream
actually opened with, and for MPEG-PS only sets that read (see `hevc.py`).
"""

from __future__ import annotations

import threading

from .mpeg_ps import ParameterSets as PsParameterSets

# The parameter sets a camera's stream opened with, kept per serial for the sessions that join
# the same stream mid-GOP and get none of their own. One entry per camera, overwritten every
# time a session opens a stream, guarded because one camera can serve several sessions at once.
_PARAMETER_SETS: dict[str, tuple[str, int, bytes]] = {}
_PARAMETER_SETS_LOCK = threading.Lock()
_PS_PARAMETER_SETS: dict[str, PsParameterSets] = {}


def remember(serial: str, codec: str, payload_type: int, prelude: bytes) -> None:
    """Keep the Annex-B parameter sets an RTP stream of `serial` opened with."""
    with _PARAMETER_SETS_LOCK:
        _PARAMETER_SETS[serial] = (codec, payload_type, prelude)


def cached(serial: str) -> tuple[str, int, bytes] | None:
    """The codec, payload type and Annex-B prelude an RTP stream of `serial` opened with."""
    with _PARAMETER_SETS_LOCK:
        return _PARAMETER_SETS.get(serial)


def remember_ps(serial: str, sets: PsParameterSets) -> None:
    """Keep the HEVC parameter sets, and their PS framing, an MPEG-PS stream opened with."""
    with _PARAMETER_SETS_LOCK:
        _PS_PARAMETER_SETS[serial] = sets


def cached_ps(serial: str) -> PsParameterSets | None:
    """The HEVC parameter sets an MPEG-PS stream of `serial` opened with, if any."""
    with _PARAMETER_SETS_LOCK:
        return _PS_PARAMETER_SETS.get(serial)


def clear() -> None:
    """Forget every camera's sets. For tests: a warm cache would make one depend on another."""
    with _PARAMETER_SETS_LOCK:
        _PARAMETER_SETS.clear()
        _PS_PARAMETER_SETS.clear()
