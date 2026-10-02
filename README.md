# SCADA Grid Frequency Drift Clamp

A high-throughput, low-latency asynchronous engine engineered to resolve unbounded publication of grid frequency by clamping the published value to a physical band around the 60 Hz nominal and by holding the last setpoint when the rate of change of frequency is impossible.

## 🏗️ Systems Architecture & Event Topology

`FrequencyDriftClamp` ingests one scalar at a time through `ingest_sample`. The nominal is 60 Hz. The cadence recorded in the measurement frame is 100 ms. The published series is a bounded `deque` standing in for TimescaleDB. `published_series` returns that series. `describe_frame` decodes the packed measurement and the clamped setpoint.

The frame layout is internal. It is not a field protocol, and the engine never emits a control write. An `asyncio.Lock` covers the series, the setpoint, and the frame deque. `logging.basicConfig` timestamps every line. A non-finite sample and an impossible step raise `EngineKernelException` (the NaN path rejects; the impossible RoCoF path holds the setpoint and records the fault). `run_scenario` is what `python src/main.py` awaits.

NERC CIP monitoring alignment here means: measure, clamp what is published, and keep the control output absent. This process does not speak DNP3, Modbus, or IEC 61850, and it does not write a setpoint to a relay.

## 📊 Core Visual Walkthrough & Engine Pipeline Flow

```
frequency sample (Hz) at 100 ms cadence
    |
    v
finite gate ---- NaN / inf --> EngineKernelException, series unchanged
    |
    v
RoCoF = (sample - previous) / cadence
    |
    +-- |RoCoF| within the physical bound --> publish clamped value, WARNING if moved
    |
    +-- |RoCoF| above the bound ------------> hold setpoint, fault=impossible_rocof
                                               control_write remains false
    v
bounded deque + packed frame
```

Insert the structural terminal walkthrough recording at docs/assets/terminal-walkthrough.gif before publishing the release notes.

## ⚡ Low-Level OS Mechanics & Network Physics

RoCoF is a first difference divided by the fixed cadence. With a 100 ms frame, a 0.2 Hz move is 2 Hz/s. The physical bound in the scenario is 5 Hz/s. A step that implies 48 Hz/s is treated as a transducer or clock fault, not as a grid event the publisher should repeat. `math.isfinite` is the first gate so a NaN cannot become a RoCoF of NaN and slip through the comparison.

The published value is clamped toward the nominal before it is appended. The raw sample is not what `published_series` returns after a clamp. `statistics` summarizes the held window for the scenario report. The frame is `struct`-packed to a fixed 18-byte width asserted in `__init__`. No serial port and no TCP session is opened.

## ⚖️ Architecture Trade-offs & Pragmatic Decisions

A state estimator would separate a real frequency excursion from a stuck transducer using PMU redundancy. This clamp has one series. The safe publication rule with one series is: move the published value only inside the RoCoF bound, and hold it when the step is physically implausible. Holding is a monitoring decision. It is not a command to a generator.

The nominal is 60 Hz because that is the interconnection this monitor is written for. A 50 Hz deployment would change the constant and the bound together. They are not inferred from the first sample, which is how a stuck 0 Hz transducer would otherwise redefine the nominal.

## 🚀 Local Installation & Benchmarking

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python src/main.py
python src/test_harness.py
```

```python
import asyncio

from src.main import FrequencyDriftClamp


async def demo() -> None:
    clamp = FrequencyDriftClamp()
    await clamp.ingest_sample(60.0)
    series = await clamp.published_series()
    return series


asyncio.run(demo())
```

The runtime is the Python 3.12 standard library. `pip install -r requirements.txt` succeeds with nothing to fetch.

## 🖥️ Terminal Diagnostic Output Preview

```
WARNING grid.frequency.clamp frequency 60.200 Hz published as setpoint 60.050 Hz rocof 1.995 Hz/s; no control write
WARNING grid.frequency.clamp RoCoF 48.000 Hz/s exceeds physical bound 5.000; holding setpoint 60.050 Hz; transducer or clock skew; no control write
WARNING grid.frequency.clamp non-finite frequency sample rejected
INFO grid.frequency.clamp scenario complete series_len=21 fault=impossible_rocof control_write=False
```

`python src/main.py` exits 0. Stdout reports `control_write` false and `nan_rejected` true.

## 📊 Empirical Benchmarking Performance Report

Measured by `python src/test_harness.py` with a deterministic seed, 5000 iterations, `time.perf_counter_ns` latency in microseconds, and `tracemalloc` peak.

| Metric | Measured |
| --- | ---: |
| Status | PASS |
| Iterations | 5000 |
| Average latency | 375.71 µs |
| Empirical P99 | 479.07 µs |
| tracemalloc peak | 449254 bytes |
| Edge: NaN sample | PASS |
| Edge: impossible RoCoF | PASS |

## 🛡️ Edge-Case Resilience & SOC2/Regulatory Compliance

NaN and infinity are rejected with `EngineKernelException` and do not enter `published_series`. An impossible step is flagged, the setpoint is held, and `control_write` stays false. The log line states that no control write was emitted.

The monitor is aligned with NERC CIP expectations for visibility into a measurement path: detect the bad sample, do not actuate, keep an ordered series. It is not a CIP evidence package and it does not authenticate to a control center. SOC 2 processing integrity is the publication rule itself: the series contains the clamped value, and the fault code names the sample that was withheld.
