"""Roundtrip, happy path, and both frequency-clamp edge cases."""

from __future__ import annotations

import asyncio
import json
import math
import unittest

from scada_grid_frequency_drift_clamp import (
    EngineKernelException,
    ScadaGridFrequencyDriftClamp,
)
from scada_grid_frequency_drift_clamp.wire import (
    SYNC,
    pack_measurement,
    unpack_measurement,
)


class FrequencyClampTest(unittest.TestCase):
    def test_wire_roundtrip(self) -> None:
        payload = pack_measurement(SYNC, 0x1, 100, 60050, -250, 60050)
        unpacked = unpack_measurement(payload)
        self.assertEqual(unpacked, (SYNC, 1, 100, 60050, -250, 60050))
        self.assertEqual(pack_measurement(*unpacked), payload)
        self.assertEqual(len(payload), 18)
        with self.assertRaises(EngineKernelException):
            unpack_measurement(payload[:-1])
        damaged = bytearray(payload)
        damaged[0] = 0
        damaged[1] = 0
        with self.assertRaises(EngineKernelException):
            unpack_measurement(bytes(damaged))

    def test_happy_path(self) -> None:
        asyncio.run(self._happy_path())

    async def _happy_path(self) -> None:
        engine = ScadaGridFrequencyDriftClamp()
        quiet = await engine.run(
            [60.0 + 0.01 * math.sin(index / 4.0) for index in range(16)]
        )
        json.dumps(quiet)
        self.assertEqual(quiet["clamps"], 0)
        self.assertEqual(quiet["rejected"], 0)
        self.assertLess(abs(float(quiet["published_hz"]) - 60.0), engine.DEADBAND_HZ)
        self.assertTrue(math.isfinite(float(quiet["rocof_hz_s"])))
        clamped = await engine.run([60.2])
        json.dumps(clamped)
        self.assertGreaterEqual(clamped["clamps"], 1)
        self.assertEqual(clamped["rejected"], 0)
        self.assertAlmostEqual(float(clamped["published_hz"]), 60.05, places=6)
        self.assertTrue(math.isfinite(float(clamped["rocof_hz_s"])))

    def test_edge_nan(self) -> None:
        asyncio.run(self._edge_nan())

    async def _edge_nan(self) -> None:
        engine = ScadaGridFrequencyDriftClamp()
        before = await engine.run([60.0, 60.01])
        with self.assertRaises(EngineKernelException) as caught:
            await engine.run([float("nan")])
        self.assertIn("non-finite", str(caught.exception))
        after = await engine.run([60.0])
        self.assertEqual(after["clamps"], before["clamps"])
        self.assertEqual(after["rejected"], before["rejected"])
        self.assertTrue(math.isfinite(float(after["published_hz"])))
        self.assertAlmostEqual(float(after["published_hz"]), 60.0, places=6)

    def test_edge_impossible_step(self) -> None:
        asyncio.run(self._edge_impossible_step())

    async def _edge_impossible_step(self) -> None:
        engine = ScadaGridFrequencyDriftClamp()
        base = await engine.run([60.0, 60.0, 60.0])
        bad = await engine.run([65.0])
        self.assertEqual(bad["rejected"], int(base["rejected"]) + 1)
        self.assertEqual(bad["clamps"], int(base["clamps"]) + 1)
        self.assertAlmostEqual(float(bad["published_hz"]), 60.05, places=6)
        self.assertGreater(abs(float(bad["published_hz"]) - 65.0), 1.0)
        self.assertGreater(abs(float(bad["rocof_hz_s"])), engine.HARD_CEILING_HZ_S)
        followed = await engine.run([60.0])
        self.assertEqual(followed["rejected"], bad["rejected"])
        self.assertAlmostEqual(float(followed["published_hz"]), 60.0, places=6)


if __name__ == "__main__":
    unittest.main()
