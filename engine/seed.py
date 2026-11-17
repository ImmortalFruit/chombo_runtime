"""Seed defaults shared by Chombo hand entry points."""

from __future__ import annotations

import time
from collections.abc import Callable


def resolve_seed(
    seed: int | None,
    *,
    clock_ns: Callable[[], int] = time.time_ns,
) -> int:
    """Preserve an explicit seed or derive a new one from Unix milliseconds."""
    if seed is not None:
        return seed
    return clock_ns() // 1_000_000
