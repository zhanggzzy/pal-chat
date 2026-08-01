from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    def now_utc(self) -> datetime: ...

    def monotonic_ns(self) -> int: ...


@dataclass(slots=True)
class RealClock:
    def now_utc(self) -> datetime:
        return datetime.now(UTC)

    def monotonic_ns(self) -> int:
        return int(self.now_utc().timestamp() * 1_000_000_000)


@dataclass(slots=True)
class VirtualClock:
    current: datetime = field(default_factory=lambda: datetime(2026, 1, 1, tzinfo=UTC))
    current_monotonic_ns: int = 0

    def now_utc(self) -> datetime:
        return self.current

    def monotonic_ns(self) -> int:
        return self.current_monotonic_ns

    def advance(self, delta: timedelta) -> None:
        self.current += delta
        self.current_monotonic_ns += int(delta.total_seconds() * 1_000_000_000)
