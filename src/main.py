"""Clamp a 60 Hz frequency series inside this process.

Samples arrive at a 100 ms cadence recorded in the measurement frame. The
published series is a bounded deque standing in for TimescaleDB. The engine
never emits a control write. An internal frame layout carries the measurement
and the clamped setpoint; it is not a field protocol.
"""

from __future__ import annotations

import asyncio
import logging
import math
import statistics
import struct
import sys
from collections import deque

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    force=True,
)
LOGGER = logging.getLogger("grid.frequency.clamp")


class EngineKernelException(Exception):
    """Raised when a frequency sample must not enter the published series."""


class FrequencyDriftClamp:
    """Deadband and RoCoF clamp for a nominal 60.000 Hz series."""

    NOMINAL_HZ = 60.0
    CADENCE_S = 0.1
    CADENCE_MS = 100
    DEADBAND_HZ = 0.050
    ROCOF_LIMIT_HZ_S = 1.0
    IMPOSSIBLE_ROCOF_HZ_S = 5.0
    HISTORY = 4096
    SYNC = 0x4652
    KIND = 1
    FLAG_CLAMPED = 0x01
    FLAG_IMPOSSIBLE = 0x02
    FLAG_ROCOF = 0x04
    FRAME = struct.Struct("<HBBHIiI")

    def __init__(self) -> None:
        if self.FRAME.size != 18:
            raise EngineKernelException("measurement frame width drifted")
        if self.CADENCE_S <= 0.0:
            raise EngineKernelException("cadence must be positive")
        self._lock = asyncio.Lock()
        self._series: deque[float] = deque(maxlen=self.HISTORY)
        self._window: deque[float] = deque(maxlen=64)
        self._frames: deque[bytes] = deque(maxlen=self.HISTORY)
        self._incidents: deque[bytes] = deque(maxlen=self.HISTORY)
        self._last_hz: float | None = None
        self._setpoint = self.NOMINAL_HZ

    async def ingest_sample(self, hertz: float) -> dict[str, object]:
        """Validate one sample and publish a setpoint inside the deadband."""
        await asyncio.sleep(0)
        if isinstance(hertz, bool) or not isinstance(hertz, (int, float)):
            raise EngineKernelException("frequency sample must be numeric")
        value = float(hertz)
        if not math.isfinite(value):
            LOGGER.warning("non-finite frequency sample rejected")
            raise EngineKernelException("non-finite frequency sample")
        async with self._lock:
            return self._ingest_unlocked(value)

    async def published_series(self) -> list[float]:
        async with self._lock:
            return list(self._series)

    def describe_frame(self, frame: bytes) -> dict[str, int]:
        """Unpack one internal measurement frame."""
        if not isinstance(frame, (bytes, bytearray)):
            raise EngineKernelException("frame must be bytes")
        if len(frame) != self.FRAME.size:
            raise EngineKernelException("measurement frame length mismatch")
        (
            sync,
            kind,
            flags,
            cadence_ms,
            freq_mhz,
            rocof_mhz_s,
            setpoint_mhz,
        ) = self.FRAME.unpack(frame)
        if sync != self.SYNC:
            raise EngineKernelException("measurement frame sync mismatch")
        return {
            "sync": sync,
            "kind": kind,
            "flags": flags,
            "cadence_ms": cadence_ms,
            "freq_mhz": freq_mhz,
            "rocof_mhz_s": rocof_mhz_s,
            "setpoint_mhz": setpoint_mhz,
        }

    def _ingest_unlocked(self, value: float) -> dict[str, object]:
        rocof = 0.0
        if self._last_hz is not None:
            rocof = (value - self._last_hz) / self.CADENCE_S
        impossible = self._last_hz is not None and math.fabs(rocof) > (
            self.IMPOSSIBLE_ROCOF_HZ_S
        )
        if impossible:
            return self._hold_setpoint(value, rocof)
        low = self.NOMINAL_HZ - self.DEADBAND_HZ
        high = self.NOMINAL_HZ + self.DEADBAND_HZ
        published = value
        if value > high:
            published = high
        elif value < low:
            published = low
        clamped = published != value
        rocof_hot = self._last_hz is not None and math.fabs(rocof) > (
            self.ROCOF_LIMIT_HZ_S
        )
        if clamped or rocof_hot:
            LOGGER.warning(
                "frequency %.3f Hz published as setpoint %.3f Hz "
                "rocof %.3f Hz/s; no control write",
                value,
                published,
                rocof,
            )
        self._setpoint = published
        self._last_hz = value
        self._series.append(published)
        self._window.append(published)
        frame = self._pack(
            value,
            rocof,
            published,
            clamped=clamped,
            impossible=False,
            rocof_hot=rocof_hot,
        )
        self._frames.append(frame)
        return self._result(value, published, rocof, clamped, "", frame)

    def _hold_setpoint(self, value: float, rocof: float) -> dict[str, object]:
        LOGGER.warning(
            "RoCoF %.3f Hz/s exceeds physical bound %.3f; "
            "holding setpoint %.3f Hz; transducer or clock skew; no control write",
            rocof,
            self.IMPOSSIBLE_ROCOF_HZ_S,
            self._setpoint,
        )
        frame = self._pack(
            value,
            rocof,
            self._setpoint,
            clamped=True,
            impossible=True,
            rocof_hot=True,
        )
        self._incidents.append(frame)
        return self._result(
            value, self._setpoint, rocof, True, "impossible_rocof", frame
        )

    def _pack(
        self,
        measured: float,
        rocof: float,
        setpoint: float,
        clamped: bool,
        impossible: bool,
        rocof_hot: bool,
    ) -> bytes:
        flags = 0
        if clamped:
            flags |= self.FLAG_CLAMPED
        if impossible:
            flags |= self.FLAG_IMPOSSIBLE
        if rocof_hot:
            flags |= self.FLAG_ROCOF
        return self.FRAME.pack(
            self.SYNC,
            self.KIND,
            flags,
            self.CADENCE_MS,
            self._millihertz(measured),
            self._rate_millihertz(rocof),
            self._millihertz(setpoint),
        )

    def _millihertz(self, hertz: float) -> int:
        scaled = hertz * 1000.0
        if scaled >= 0.0:
            rendered = int(math.floor(scaled + 0.5))
        else:
            rendered = int(math.ceil(scaled - 0.5))
        if rendered < 0 or rendered > 0xFFFFFFFF:
            raise EngineKernelException("frequency exceeds measurement width")
        return rendered

    def _rate_millihertz(self, rocof: float) -> int:
        scaled = rocof * 1000.0
        if scaled >= 0.0:
            rendered = int(math.floor(scaled + 0.5))
        else:
            rendered = int(math.ceil(scaled - 0.5))
        if rendered > 2_147_483_647 or rendered < -2_147_483_648:
            raise EngineKernelException("RoCoF exceeds measurement width")
        return rendered

    def _result(
        self,
        measured: float,
        setpoint: float,
        rocof: float,
        clamped: bool,
        fault: str,
        frame: bytes,
    ) -> dict[str, object]:
        if self._window:
            mean_hz = float(statistics.fmean(self._window))
        else:
            mean_hz = self._setpoint
        if len(self._window) > 1:
            spread_hz = float(statistics.pstdev(self._window))
        else:
            spread_hz = 0.0
        return {
            "hertz": measured,
            "setpoint_hz": setpoint,
            "rocof_hz_s": rocof,
            "clamped": clamped,
            "fault": fault,
            "control_write": False,
            "mean_hz": mean_hz,
            "spread_hz": spread_hz,
            "frame": frame,
            "series_len": len(self._series),
            "cadence_ms": self.CADENCE_MS,
        }


async def run_scenario() -> dict[str, object]:
    engine = FrequencyDriftClamp()
    inbox: asyncio.Queue[float] = asyncio.Queue()
    for index in range(20):
        await inbox.put(engine.NOMINAL_HZ + 0.01 * math.sin(index / 3.0))
    await inbox.put(60.2)
    await inbox.put(65.0)
    published: dict[str, object] = {}
    faulted: dict[str, object] = {}
    while not inbox.empty():
        sample = inbox.get_nowait()
        if math.isclose(sample, 65.0):
            faulted = await engine.ingest_sample(sample)
        elif math.isclose(sample, 60.2):
            published = await engine.ingest_sample(sample)
        else:
            await engine.ingest_sample(sample)
    if not published.get("clamped") or published.get("control_write") is not False:
        raise EngineKernelException("deadband clamp failed")
    if faulted.get("fault") != "impossible_rocof":
        raise EngineKernelException("impossible RoCoF was not flagged")
    if faulted.get("control_write") is not False:
        raise EngineKernelException("control write emitted")
    frame = faulted.get("frame")
    if not isinstance(frame, bytes):
        raise EngineKernelException("measurement frame missing")
    described = engine.describe_frame(frame)
    if not described["flags"] & engine.FLAG_IMPOSSIBLE:
        raise EngineKernelException("impossible flag missing from the frame")
    series = await engine.published_series()
    if any(math.fabs(item - 65.0) < 1e-9 for item in series):
        raise EngineKernelException("published series stored the impossible step")
    if any(math.fabs(item - 60.2) < 1e-9 for item in series):
        raise EngineKernelException("published series stored the raw excursion")
    nan_rejected = False
    try:
        await engine.ingest_sample(float("nan"))
    except EngineKernelException:
        nan_rejected = True
    if not nan_rejected:
        raise EngineKernelException("NaN sample was accepted")
    after = await engine.published_series()
    if len(after) != len(series):
        raise EngineKernelException("NaN sample extended the published series")
    summary = {
        "setpoint_hz": faulted["setpoint_hz"],
        "fault": faulted["fault"],
        "control_write": False,
        "rocof_hz_s": faulted["rocof_hz_s"],
        "series_len": len(after),
        "nan_rejected": nan_rejected,
        "cadence_ms": engine.CADENCE_MS,
    }
    LOGGER.info(
        "scenario complete series_len=%s fault=%s control_write=%s",
        len(after),
        faulted["fault"],
        False,
    )
    return summary


def main() -> int:
    summary = asyncio.run(run_scenario())
    print(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
