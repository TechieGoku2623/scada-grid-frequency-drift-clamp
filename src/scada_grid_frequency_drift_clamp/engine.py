"""In-process deadband and RoCoF clamp for a nominal 60 Hz series.

Samples are smoothed in this process. The published series is a bounded deque
standing in for TimescaleDB. The engine never emits a control write and never
opens a field protocol.
"""

from __future__ import annotations

import asyncio
import logging
import math
import statistics
import struct
from collections import deque

from .exceptions import EngineKernelException
from .wire import MEASUREMENT, SYNC, pack_measurement, unpack_measurement

LOGGER = logging.getLogger("grid.frequency.clamp")


class ScadaGridFrequencyDriftClamp:
    """Clamp a 60.000 Hz series to the deadband and a RoCoF ceiling."""

    NOMINAL_HZ = 60.0
    DT_S = 0.1
    CADENCE_MS = 100
    DEADBAND_HZ = 0.05
    ROCOF_LIMIT_HZ_S = 1.0
    HARD_CEILING_HZ_S = 5.0
    ALPHA = 0.30
    HISTORY = 4096
    FLAG_CLAMPED = 0x01
    FLAG_IMPOSSIBLE = 0x02
    FLAG_ROCOF = 0x04

    def __init__(
        self,
        alpha: float = ALPHA,
        deadband_hz: float = DEADBAND_HZ,
        rocof_limit_hz_s: float = ROCOF_LIMIT_HZ_S,
        hard_ceiling_hz_s: float = HARD_CEILING_HZ_S,
        history: int = HISTORY,
    ) -> None:
        if not math.isfinite(alpha) or not 0.0 < alpha <= 1.0:
            raise EngineKernelException("alpha out of range")
        if not math.isfinite(deadband_hz) or deadband_hz < 0.0:
            raise EngineKernelException("deadband out of range")
        if not math.isfinite(rocof_limit_hz_s) or rocof_limit_hz_s <= 0.0:
            raise EngineKernelException("RoCoF limit out of range")
        if not math.isfinite(hard_ceiling_hz_s) or hard_ceiling_hz_s <= 0.0:
            raise EngineKernelException("hard ceiling out of range")
        if history < 1:
            raise EngineKernelException("history must be positive")
        if self.DT_S <= 0.0 or self.CADENCE_MS != int(self.DT_S * 1000):
            raise EngineKernelException("sample period must be positive")
        if struct.calcsize(MEASUREMENT.format) != MEASUREMENT.size:
            raise EngineKernelException("measurement record width drifted")
        self._alpha = float(alpha)
        self._deadband = float(deadband_hz)
        self._rocof_limit = float(rocof_limit_hz_s)
        self._hard_ceiling = float(hard_ceiling_hz_s)
        self._lock = asyncio.Lock()
        self._series: deque[float] = deque(maxlen=history)
        self._window: deque[float] = deque(maxlen=64)
        self._frames: deque[bytes] = deque(maxlen=history)
        self._f_hat = self.NOMINAL_HZ
        self._last_rocof = 0.0
        self._prev_published = self.NOMINAL_HZ
        self._last_admitted: float | None = None
        self._clamps = 0
        self._rejected = 0
        self._last_mean = self.NOMINAL_HZ
        self._last_spread = 0.0

    async def run(self, records: list[float]) -> dict[str, object]:
        """Ingest one batch and return the JSON-serializable clamp state."""
        await asyncio.sleep(0)
        async with self._lock:
            if not isinstance(records, (list, tuple)):
                raise EngineKernelException("records must be a sequence of samples")
            for sample in records:
                self._ingest(self._coerce(sample))
            return {
                "published_hz": self._prev_published,
                "rocof_hz_s": self._last_rocof,
                "clamps": self._clamps,
                "rejected": self._rejected,
            }

    def _coerce(self, sample: float) -> float:
        if isinstance(sample, bool) or not isinstance(sample, (int, float)):
            raise EngineKernelException("frequency sample must be numeric")
        value = float(sample)
        if not math.isfinite(value):
            LOGGER.warning("non-finite frequency sample rejected")
            raise EngineKernelException("non-finite frequency sample")
        return value

    def _ingest(self, value: float) -> None:
        reference = (
            self.NOMINAL_HZ if self._last_admitted is None else self._last_admitted
        )
        implied = (value - reference) / self.DT_S
        if math.fabs(implied) > self._hard_ceiling:
            published = self._clamp_target(value)
            self._rejected += 1
            self._clamps += 1
            self._last_rocof = implied
            self._prev_published = published
            self._remember(published)
            self._store(
                value,
                implied,
                published,
                clamped=True,
                impossible=True,
                rocof_hot=True,
            )
            self._warn(value, published, implied)
            return
        previous_hat = self._f_hat
        self._f_hat = previous_hat + self._alpha * (value - previous_hat)
        rocof = (self._f_hat - previous_hat) / self.DT_S
        self._last_rocof = rocof
        self._last_admitted = value
        out_of_band = math.fabs(value - self.NOMINAL_HZ) > self._deadband
        rocof_hot = math.fabs(rocof) > self._rocof_limit
        if out_of_band or rocof_hot:
            published = self._clamp_target(value)
            self._clamps += 1
            self._prev_published = published
            self._remember(published)
            self._store(
                value,
                rocof,
                published,
                clamped=True,
                impossible=False,
                rocof_hot=rocof_hot,
            )
            self._warn(value, published, rocof)
            return
        self._prev_published = value
        self._remember(value)
        self._store(
            value,
            rocof,
            value,
            clamped=False,
            impossible=False,
            rocof_hot=False,
        )

    def _clamp_target(self, value: float) -> float:
        error = value - self.NOMINAL_HZ
        if error > 0.0:
            direction = 1.0
        elif error < 0.0:
            direction = -1.0
        elif self._last_rocof != 0.0:
            direction = math.copysign(1.0, self._last_rocof)
        else:
            direction = 1.0
        candidate = self.NOMINAL_HZ + direction * self._deadband
        return self._limited(candidate)

    def _limited(self, candidate: float) -> float:
        max_delta = self._rocof_limit * self.DT_S
        lower = self._prev_published - max_delta
        upper = self._prev_published + max_delta
        if candidate < lower:
            return lower
        if candidate > upper:
            return upper
        return candidate

    def _remember(self, published: float) -> None:
        self._series.append(published)
        self._window.append(published)
        self._last_mean = float(statistics.fmean(self._window))
        if len(self._window) > 1:
            self._last_spread = float(statistics.pstdev(self._window))
        else:
            self._last_spread = 0.0

    def _warn(self, value: float, published: float, rate: float) -> None:
        LOGGER.warning(
            "clamped %.6f Hz to %.6f Hz rocof %.6f Hz/s "
            "mean %.6f spread %.6f local setpoint only",
            value,
            published,
            rate,
            self._last_mean,
            self._last_spread,
        )

    def _store(
        self,
        measured: float,
        rocof: float,
        published: float,
        clamped: bool,
        impossible: bool,
        rocof_hot: bool,
    ) -> None:
        flags = 0
        if clamped:
            flags |= self.FLAG_CLAMPED
        if impossible:
            flags |= self.FLAG_IMPOSSIBLE
        if rocof_hot:
            flags |= self.FLAG_ROCOF
        frame = pack_measurement(
            SYNC,
            flags,
            self.CADENCE_MS,
            self._to_milli(measured),
            self._to_milli(rocof),
            self._to_milli(published),
        )
        unpacked = unpack_measurement(frame)
        if unpacked[5] != self._to_milli(published) or unpacked[0] != SYNC:
            raise EngineKernelException("measurement roundtrip failed")
        self._frames.append(self._seal(frame))

    def _seal(self, frame: bytes) -> bytes:
        if len(frame) != MEASUREMENT.size:
            raise EngineKernelException("measurement record width drifted")
        prefix = struct.pack("<H", len(frame))
        if struct.unpack("<H", prefix)[0] != len(frame):
            raise EngineKernelException("measurement seal failed")
        return prefix + frame

    def _to_milli(self, value: float) -> int:
        scaled = value * 1000.0
        if scaled >= 0.0:
            rendered = int(math.floor(scaled + 0.5))
        else:
            rendered = int(math.ceil(scaled - 0.5))
        if rendered > 2_147_483_647 or rendered < -2_147_483_648:
            raise EngineKernelException("scaled measurement exceeds record width")
        return rendered
