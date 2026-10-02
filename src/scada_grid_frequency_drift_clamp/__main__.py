"""Run a realistic in-process frequency batch."""

from __future__ import annotations

import asyncio
import logging
import math
import sys

from .engine import ScadaGridFrequencyDriftClamp
from .exceptions import EngineKernelException


async def _batch() -> dict[str, object]:
    engine = ScadaGridFrequencyDriftClamp()
    samples = [engine.NOMINAL_HZ + 0.01 * math.sin(index / 5.0) for index in range(40)]
    samples.append(60.2)
    samples.append(65.0)
    result = await engine.run(samples)
    if int(result["clamps"]) < 1 or int(result["rejected"]) < 1:
        raise EngineKernelException("scenario did not clamp the excursion")
    if math.fabs(float(result["published_hz"]) - 65.0) < 1e-6:
        raise EngineKernelException("impossible step was published")
    if (
        math.fabs(float(result["published_hz"]) - engine.NOMINAL_HZ)
        > engine.DEADBAND_HZ + 1e-6
    ):
        raise EngineKernelException("published setpoint left the deadband")
    logging.getLogger("grid.frequency.clamp").info(
        "scenario complete published_hz=%.6f clamps=%s rejected=%s local-only",
        float(result["published_hz"]),
        result["clamps"],
        result["rejected"],
    )
    return result


def main() -> int:
    """Clamp an in-process 60 Hz batch. Return 0."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    result = asyncio.run(_batch())
    print(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
