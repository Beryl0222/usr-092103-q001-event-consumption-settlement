"""时间与权益有效窗口工具。

权益分三个阶段：赛前 PRE_RACE、赛中 RACE_DAY、赛后 POST_RACE，
每个权益实例有显式的 [window_start, window_end] 有效窗口。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

PRE_RACE = "PRE_RACE"
RACE_DAY = "RACE_DAY"
POST_RACE = "POST_RACE"
PHASES = (PRE_RACE, RACE_DAY, POST_RACE)

CN_TZ = timezone(timedelta(hours=8))


def now() -> datetime:
    return datetime.now(CN_TZ)


def parse_at(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=CN_TZ)
    return dt


def iso(dt: datetime) -> str:
    return dt.astimezone(CN_TZ).isoformat()


@dataclass(frozen=True)
class Window:
    start: datetime
    end: datetime

    @classmethod
    def of(cls, start: str | datetime, end: str | datetime) -> "Window":
        s, e = parse_at(start), parse_at(end)
        if e <= s:
            raise ValueError("有效窗口结束时间必须晚于开始时间")
        return cls(s, e)

    def contains(self, moment: str | datetime) -> bool:
        m = parse_at(moment)
        return self.start <= m <= self.end

    def shift(self, delta: timedelta) -> "Window":
        return Window(self.start + delta, self.end + delta)

    def as_payload(self) -> dict[str, str]:
        return {"window_start": iso(self.start), "window_end": iso(self.end)}
