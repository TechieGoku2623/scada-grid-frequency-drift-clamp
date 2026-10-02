"""Packed measurement record for the in-process frequency series."""

from __future__ import annotations

import struct

from .exceptions import EngineKernelException

SYNC = 0x4652
MEASUREMENT = struct.Struct("<HHHiii")


def pack_measurement(
    sync: int,
    flags: int,
    cadence_ms: int,
    measured_mhz: int,
    rocof_mhz_s: int,
    published_mhz: int,
) -> bytes:
    """Pack one measurement. Values are millihertz and millihertz per second."""
    _check_unsigned("sync", sync, 0xFFFF)
    _check_unsigned("flags", flags, 0xFFFF)
    _check_unsigned("cadence_ms", cadence_ms, 0xFFFF)
    _check_signed("measured_mhz", measured_mhz)
    _check_signed("rocof_mhz_s", rocof_mhz_s)
    _check_signed("published_mhz", published_mhz)
    return MEASUREMENT.pack(
        sync,
        flags,
        cadence_ms,
        measured_mhz,
        rocof_mhz_s,
        published_mhz,
    )


def unpack_measurement(payload: bytes) -> tuple[int, int, int, int, int, int]:
    """Unpack a record produced by :func:`pack_measurement`."""
    if not isinstance(payload, (bytes, bytearray)) or len(payload) != MEASUREMENT.size:
        raise EngineKernelException("measurement record length mismatch")
    (
        sync,
        flags,
        cadence_ms,
        measured_mhz,
        rocof_mhz_s,
        published_mhz,
    ) = MEASUREMENT.unpack(payload)
    if int(sync) != SYNC:
        raise EngineKernelException("measurement sync mismatch")
    return (
        int(sync),
        int(flags),
        int(cadence_ms),
        int(measured_mhz),
        int(rocof_mhz_s),
        int(published_mhz),
    )


def _check_unsigned(label: str, value: int, limit: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise EngineKernelException(f"field {label} must be an int")
    if value < 0 or value > limit:
        raise EngineKernelException(f"field {label} out of range")


def _check_signed(label: str, value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise EngineKernelException(f"field {label} must be an int")
    if value > 2_147_483_647 or value < -2_147_483_648:
        raise EngineKernelException(f"field {label} exceeds measurement width")
