"""Deterministic harness for NaN samples, impossible RoCoF, and latency."""

from __future__ import annotations

import asyncio
import random
import sys
import time
import tracemalloc
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

SEED = 20261001
ITERATIONS = 5000


def _percentile_index(count: int) -> int:
    index = (99 * count + 99) // 100 - 1
    if index < 0:
        return 0
    if index >= count:
        return count - 1
    return index


def _edge_nan_sample(module: object) -> str:
    engine_cls = module.FrequencyDriftClamp
    error_cls = module.EngineKernelException

    async def _run() -> None:
        engine = engine_cls()
        for sample in (float("nan"), float("inf")):
            try:
                await engine.ingest_sample(sample)
            except error_cls as exc:
                if "non-finite" not in str(exc):
                    raise AssertionError("non-finite fault was mislabeled") from exc
            else:
                raise AssertionError("non-finite sample was accepted")
        series = await engine.published_series()
        if series:
            raise AssertionError("non-finite sample entered the published series")

    try:
        asyncio.run(_run())
    except Exception as exc:
        print(f"edge_nan_sample failed: {exc}", file=sys.stderr)
        return "FAIL"
    return "PASS"


def _edge_impossible_rocof(module: object) -> str:
    engine_cls = module.FrequencyDriftClamp

    async def _run() -> None:
        engine = engine_cls()
        first = await engine.ingest_sample(60.0)
        if first["control_write"] is not False:
            raise AssertionError("control write emitted")
        if first["fault"] != "":
            raise AssertionError("nominal sample was faulted")
        stepped = await engine.ingest_sample(65.0)
        if stepped["fault"] != "impossible_rocof":
            raise AssertionError("impossible RoCoF was not flagged")
        if stepped["control_write"] is not False:
            raise AssertionError("control write emitted")
        if float(stepped["setpoint_hz"]) != 60.0:
            raise AssertionError("setpoint moved on an impossible RoCoF")
        if abs(float(stepped["rocof_hz_s"])) <= engine_cls.IMPOSSIBLE_ROCOF_HZ_S:
            raise AssertionError("RoCoF was inside the physical bound")
        series = await engine.published_series()
        if series != [60.0]:
            raise AssertionError("published series accepted the step change")

    try:
        asyncio.run(_run())
    except Exception as exc:
        print(f"edge_impossible_rocof failed: {exc}", file=sys.stderr)
        return "FAIL"
    return "PASS"


def _benchmark(module: object) -> tuple[int, float, float, int]:
    engine_cls = module.FrequencyDriftClamp
    rng = random.Random(SEED)
    samples_hz = [60.0 + (rng.random() - 0.5) * 0.02 for _ in range(ITERATIONS)]
    engine = engine_cls()

    async def _run() -> list[float]:
        timed: list[float] = []
        for hertz in samples_hz:
            started = time.perf_counter_ns()
            await engine.ingest_sample(hertz)
            elapsed_us = (time.perf_counter_ns() - started) / 1000.0
            timed.append(elapsed_us)
        return timed

    tracemalloc.start()
    try:
        timed = asyncio.run(_run())
    finally:
        peak = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
    average = sum(timed) / len(timed)
    ordered = sorted(timed)
    p99 = ordered[_percentile_index(len(ordered))]
    return len(timed), average, p99, peak


def main() -> int:
    import main as engine_module

    status = {
        "edge_nan_sample": _edge_nan_sample(engine_module),
        "edge_impossible_rocof": _edge_impossible_rocof(engine_module),
        "benchmark": "FAIL",
    }
    try:
        count, average, p99, peak = _benchmark(engine_module)
        print(
            f"BENCH n={count} avg_us={average:.2f} "
            f"p99_us={p99:.2f} peak_bytes={peak}"
        )
        if count >= ITERATIONS and average >= 0.0 and peak > 0:
            status["benchmark"] = "PASS"
    except Exception as exc:
        print(f"benchmark failed: {exc}", file=sys.stderr)
    print(status)
    if all(value == "PASS" for value in status.values()):
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
