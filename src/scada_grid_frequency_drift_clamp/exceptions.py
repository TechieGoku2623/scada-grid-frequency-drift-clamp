"""Kernel faults for the in-process frequency clamp."""

from __future__ import annotations


class EngineKernelException(Exception):
    """Raised when a frequency sample must not enter the smoother."""
