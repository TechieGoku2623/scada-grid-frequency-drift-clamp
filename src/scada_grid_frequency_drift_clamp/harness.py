"""Deterministic latency harness for the frequency clamp."""

from __future__ import annotations

import asyncio
import math
import random
import statistics
import sys
import tracemalloc
from time import perf_counter_ns

from .engine import ScadaGridFrequencyDriftClamp
from .exceptions import EngineKernelException

SEED = 20261002
ITERATIONS = 5000


def _p99(samples: list[float]) -> float:
    ordered = sorted(samples)
    count = len(ordered)
    index = math.ceil(0.99 * count) - 1
    if index < 0:
        return ordered[0]
    if index >= count:
        return ordered[-1]
    return ordered[index]


def _edge_nan() -> bool:
    async def _run() -> None:
        engine = ScadaGridFrequencyDriftClamp()
        before = await engine.run([60.0, 60.01])
        try:
            await engine.run([float("nan")])
        except EngineKernelException as exc:
            if "non-finite" not in str(exc):
                raise AssertionError("NaN fault was mislabeled") from exc
        else:
            raise AssertionError("NaN sample was accepted")
        after = await engine.run([60.0])
        if (
            after["clamps"] != before["clamps"]
            or after["rejected"] != before["rejected"]
        ):
            raise AssertionError("NaN sample mutated clamp counters")
        if not math.isfinite(float(after["published_hz"])):
            raise AssertionError("NaN sample reached the published series")

    try:
        asyncio.run(_run())
    except Exception as exc:
        print(f"edge_nan failed: {exc}", file=sys.stderr)
        return False
    return True


def _edge_impossible_step() -> bool:
    async def _run() -> None:
        engine = ScadaGridFrequencyDriftClamp()
        base = await engine.run([60.0, 60.0, 60.0])
        bad = await engine.run([65.0])
        if int(bad["rejected"]) != int(base["rejected"]) + 1:
            raise AssertionError("impossible step was not counted")
        if int(bad["clamps"]) != int(base["clamps"]) + 1:
            raise AssertionError("impossible step was not clamped")
        if math.fabs(float(bad["published_hz"]) - 60.05) > 1e-6:
            raise AssertionError("impossible step was followed")
        if math.fabs(float(bad["rocof_hz_s"])) <= engine.HARD_CEILING_HZ_S:
            raise AssertionError("implied RoCoF was not reported")
        followed = await engine.run([60.0])
        if int(followed["rejected"]) != int(bad["rejected"]):
            raise AssertionError("estimator followed the impossible step")

    try:
        asyncio.run(_run())
    except Exception as exc:
        print(f"edge_impossible_step failed: {exc}", file=sys.stderr)
        return False
    return True


def _benchmark() -> tuple[int, float, float, int]:
    engine = ScadaGridFrequencyDriftClamp()
    rng = random.Random(SEED)
    samples: list[float] = []

    async def _run() -> None:
        for index in range(ITERATIONS):
            wobble = (rng.random() - 0.5) * 0.004
            sample = engine.NOMINAL_HZ + 0.01 * math.sin(index / 17.0) + wobble
            started = perf_counter_ns()
            await engine.run([sample])
            samples.append((perf_counter_ns() - started) / 1000.0)

    tracemalloc.start()
    try:
        asyncio.run(_run())
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    average = float(statistics.fmean(samples))
    return len(samples), average, _p99(samples), peak


def main() -> int:
    """Print the harness status dict and return 0 only on success."""
    failures = 0
    if not _edge_nan():
        failures += 1
    if not _edge_impossible_step():
        failures += 1
    iterations = 0
    average = 0.0
    p99 = 0.0
    peak = 0
    try:
        iterations, average, p99, peak = _benchmark()
    except Exception as exc:
        print(f"benchmark failed: {exc}", file=sys.stderr)
        failures += 1
    else:
        if iterations < ITERATIONS or peak <= 0:
            failures += 1
    status = "ok" if failures == 0 else "fail"
    print(
        {
            "status": status,
            "failures": failures,
            "latency_us": round(average, 3),
            "memory_peak_bytes": peak,
            "benchmark_iterations": iterations,
            "benchmark_avg_us": round(average, 3),
            "benchmark_p99_us": round(p99, 3),
        }
    )
    return 0 if status == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
