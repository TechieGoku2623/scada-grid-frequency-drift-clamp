# SCADA Grid Frequency Drift Clamp

A high-throughput, low-latency asynchronous engine engineered to resolve out-of-band grid frequency samples by smoothing a nominal 60 Hz series in process and publishing a deadband- and RoCoF-limited setpoint.

## 🏗️ Systems Architecture & Event Topology

`ScadaGridFrequencyDriftClamp` ingests a frequency series under an `asyncio.Lock`. Nominal frequency is 60.0 Hz and the sample period is 0.1 s (100 ms). `run(records)` returns a JSON-serializable dict: `published_hz`, `rocof_hz_s`, `clamps`, and `rejected`.

The smoother is exponential:

```
f_hat = f_hat + alpha * (f - f_hat)
RoCoF = (f_hat - previous_f_hat) / dt
```

`alpha` defaults to 0.30. If `abs(f - 60)` exceeds the deadband (0.05 Hz) or `abs(RoCoF)` exceeds 1.0 Hz/s, the engine publishes the clamped setpoint: nominal plus the deadband in the direction of the error, then limited so the published value cannot move faster than `rocof_limit * dt` from the previous published value. A warning is logged. The published series is a bounded `deque` (default 4096) standing in for TimescaleDB.

An implied raw RoCoF above the hard ceiling (5.0 Hz/s) is clamped and counted. The smoother does not follow that sample. NaN and any non-finite sample raise `EngineKernelException` before the series changes.

The measurement record is `struct` format `<HHHiii`: sync `0x4652`, flags, cadence in milliseconds, measured millihertz, RoCoF in millihertz per second, and published millihertz. A length prefix seals the frame. The engine never emits a control write and never opens a field protocol.

## 📊 Core Visual Walkthrough & Engine Pipeline Flow

```
frequency sample (Hz), dt = 0.1 s
        |
        v
finite gate ---- NaN or inf --> EngineKernelException
        |
        v
implied RoCoF versus last admitted sample
        |
        +--> abs(implied) > 5 Hz/s
        |       clamp, count clamps and rejected
        |       do not update f_hat
        |
        v
f_hat = f_hat + alpha * (f - f_hat)
RoCoF = (f_hat - previous) / dt
        |
        +--> outside deadband or |RoCoF| > 1 Hz/s
        |       publish nominal ± deadband
        |       limit the step to rocof_limit * dt
        |
        +--> inside the band --> publish f
        |
        v
bounded deque (TimescaleDB stand-in) + measurement frame
        |
        v
{published_hz, rocof_hz_s, clamps, rejected}
```

Insert the structural terminal walkthrough recording at docs/assets/terminal-walkthrough.gif before publishing the release notes.

## ⚡ Low-Level OS Mechanics & Network Physics

The sample period is the model `dt`, not a sleep. A batch of samples is scored as if they arrived 100 ms apart, which keeps the benchmark on the arithmetic path.

The deadband edges sit at 59.95 Hz and 60.05 Hz. A sample of 60.2 Hz is admitted to the smoother, because the implied step from a nearby in-band sample is under the 5 Hz/s ceiling, and the published setpoint is pulled back to 60.05 Hz when the previous setpoint is within one RoCoF step (`1.0 Hz/s * 0.1 s = 0.1 Hz`).

A step such as 60.2 Hz then 65.0 Hz implies about 48 Hz/s. That is above the hard ceiling. The engine publishes 60.05 Hz, increments `clamps` and `rejected`, stores the implied rate in `rocof_hz_s`, and leaves `f_hat` and the last admitted sample unchanged. The next in-band 60.0 Hz sample is therefore still admissible.

`statistics.fmean` and `statistics.pstdev` run on a 64-sample window so the warning carries the recent mean and spread. `math.floor` rounds the millihertz fields. `math.copysign` picks a direction when the error is zero and the rate is not. The frame is packed and unpacked before it enters the deque, so a width mismatch fails closed.

## ⚖️ Architecture Trade-offs & Pragmatic Decisions

The published value and the smoother are different series. In-band samples publish the measurement and move `f_hat`. Out-of-band samples still move `f_hat` by `alpha`, and the value that enters the deque is the clamped setpoint. An impossible step does neither to `f_hat`: following a 48 Hz/s spike would make every later sample look impossible.

The RoCoF limit on the published setpoint is a slew limit of `rocof_limit * dt` per sample. With the defaults, the deadband edge at 60.05 Hz is reachable in one step from 60.0 Hz. A setpoint that had already walked away from nominal would approach the new clamp over later samples.

This package aligns with NERC CIP monitoring practice in the narrow sense that an out-of-band frequency is recorded, clamped, and counted inside the process. It does not claim a certification, and it does not describe a substation action.

## 🚀 Local Installation & Benchmarking

```bash
python3 -m venv venv
source venv/bin/activate
pip install -e ".[dev]"
python -m scada_grid_frequency_drift_clamp
python -m scada_grid_frequency_drift_clamp.harness
```

```python
import asyncio

from scada_grid_frequency_drift_clamp import ScadaGridFrequencyDriftClamp


async def demo() -> None:
    engine = ScadaGridFrequencyDriftClamp()
    result = await engine.run([60.0, 60.01, 60.2])
    print(result)


asyncio.run(demo())
```

The runtime is the Python 3.12 standard library. `pip install -r requirements.txt` succeeds with comments only. Install the package with `pip install .`.

## 🖥️ Terminal Diagnostic Output Preview

```
2026-10-02T02:54:19+0000 WARNING [grid.frequency.clamp] clamped 60.200000 Hz to 60.050000 Hz rocof 0.576643 Hz/s mean 60.002491 spread 0.010160 local setpoint only
2026-10-02T02:54:19+0000 WARNING [grid.frequency.clamp] clamped 65.000000 Hz to 60.050000 Hz rocof 48.000000 Hz/s mean 60.003622 spread 0.012378 local setpoint only
2026-10-02T02:54:19+0000 INFO [grid.frequency.clamp] scenario complete published_hz=60.050000 clamps=2 rejected=1 local-only
{'published_hz': 60.05, 'rocof_hz_s': 47.99999999999997, 'clamps': 2, 'rejected': 1}
```

`python -m scada_grid_frequency_drift_clamp` exits 0. The 65 Hz step is clamped to 60.05 Hz. The log records a local setpoint. No control write is emitted.

## 📊 Empirical Benchmarking Performance Report

Measured by `python -m scada_grid_frequency_drift_clamp.harness` with seed `20261002`, 5000 iterations, `perf_counter_ns` latency in microseconds, and `tracemalloc` peak. Each iteration is one in-band sample. The per-sample mean and spread on the 64-wide window dominate the latency. The harness prints this status dict and exits 0 only when every edge passes:

```
{'status': 'ok', 'failures': 0, 'latency_us': 389.093, 'memory_peak_bytes': 554188, 'benchmark_iterations': 5000, 'benchmark_avg_us': 389.093, 'benchmark_p99_us': 521.961}
```

| Metric | Measured |
| --- | ---: |
| Status | ok |
| Failures | 0 |
| Iterations | 5000 |
| Average latency | 389.093 µs |
| Empirical P99 | 521.961 µs |
| tracemalloc peak | 554188 bytes |
| Edge: NaN sample | pass |
| Edge: impossible RoCoF step | pass |

## 🛡️ Edge-Case Resilience & SOC2/Regulatory Compliance

A NaN sample raises `EngineKernelException` with a non-finite message. `clamps` and `rejected` stay at their previous values, and the next finite 60.0 Hz sample still publishes 60.0 Hz.

An impossible step, a raw jump whose implied RoCoF exceeds 5 Hz/s, is clamped to 60.05 Hz when the previous setpoint is 60.0 Hz, counted in both `clamps` and `rejected`, and kept out of the smoother. The following 60.0 Hz sample does not increment `rejected`, which is the evidence the estimator did not follow the spike.

The series never leaves this process. NERC CIP monitoring alignment is the audit frame, the clamp counter, and the refusal to publish the raw excursion. This package does not claim a certification and does not perform a grid control write.
