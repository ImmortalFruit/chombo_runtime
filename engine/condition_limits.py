"""Request-local resource accounting shared by v0.4 condition calculators."""

from __future__ import annotations

from time import monotonic_ns
from typing import Callable, Mapping


class SearchLimitReached(RuntimeError):
    """A bounded search stopped before it could prove completeness."""

    def __init__(self, reason: str, usage: Mapping[str, int], partial_candidates=()):
        self.reason = reason
        self.usage = dict(usage)
        self.partial_candidates = tuple(partial_candidates)
        super().__init__(reason)


class SearchBudget:
    """Mutable counters that are private to a single search invocation."""

    def __init__(
        self, scope: Mapping[str, int], *, clock_ns: Callable[[], int] = monotonic_ns,
    ):
        self.max_depth = scope["max_depth"]
        self.max_candidates = scope["max_candidates"]
        self.max_transitions = scope["max_transitions"]
        self.max_wall_time_ms = scope["max_wall_time_ms"]
        self.depth = 0
        self.candidates = 0
        self.transitions = 0
        self._clock_ns = clock_ns
        self._started_ns = clock_ns()

    def usage(self) -> dict[str, int]:
        return {
            "depth": self.depth,
            "candidates": self.candidates,
            "transitions": self.transitions,
        }

    def enter_depth(self) -> None:
        if self.depth + 1 > self.max_depth:
            raise SearchLimitReached("MAX_DEPTH_REACHED", self.usage())
        self.check_time()
        self.depth += 1

    def consume_transition(self) -> None:
        if self.transitions + 1 > self.max_transitions:
            raise SearchLimitReached("MAX_TRANSITIONS_REACHED", self.usage())
        self.check_time()
        self.transitions += 1

    def consume_candidate(self) -> None:
        if self.candidates + 1 > self.max_candidates:
            raise SearchLimitReached("MAX_CANDIDATES_REACHED", self.usage())
        self.check_time()
        self.candidates += 1

    def check_time(self) -> None:
        elapsed_ms = (self._clock_ns() - self._started_ns) // 1_000_000
        if elapsed_ms >= self.max_wall_time_ms:
            raise SearchLimitReached("MAX_WALL_TIME_REACHED", self.usage())
