"""Cycle-aware period identity for daily issue numbers.

The source pages expose bare day-of-year issue numbers (001..365/366). A bare
integer is not globally unique, so persistence and duplicate comparison use a
``PeriodKey`` containing the calendar cycle year and the issue number.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

# Tokyo has used UTC+09:00 year-round since 1951. A fixed offset avoids
# depending on the optional IANA tzdata package on Windows Python installs.
TOKYO_ZONE = timezone(timedelta(hours=9), name="Asia/Tokyo")
_PERIOD_KEY_RE = re.compile(r"^(?P<year>\d{4})[-/](?P<issue>\d{1,3})$")


def issues_in_cycle(cycle_year: int) -> int:
    """Return the number of daily issues in the calendar cycle."""

    if not 1 <= cycle_year <= 9999:
        raise ValueError(f"非法周期年份: {cycle_year}")
    return date(cycle_year, 12, 31).timetuple().tm_yday


def current_tokyo_period(now: datetime | None = None) -> "PeriodKey":
    """Return the current Tokyo calendar-year/day-of-year period."""

    current = now.astimezone(TOKYO_ZONE) if now is not None else datetime.now(TOKYO_ZONE)
    return PeriodKey(current.year, current.timetuple().tm_yday)


@dataclass(frozen=True, order=True, slots=True)
class PeriodKey:
    """Globally unique daily issue identity.

    ``issue`` is the one-based day-of-year issue number for ``cycle_year``.
    Ordering by ``(cycle_year, issue)`` is chronological for this contract.
    """

    cycle_year: int
    issue: int

    def __post_init__(self) -> None:
        maximum = issues_in_cycle(self.cycle_year)
        if type(self.issue) is not int or not 1 <= self.issue <= maximum:
            raise ValueError(
                f"{self.cycle_year}周期期数必须在1至{maximum}之间: {self.issue!r}"
            )

    @property
    def calendar_date(self) -> date:
        return date(self.cycle_year, 1, 1) + timedelta(days=self.issue - 1)

    @property
    def ordinal(self) -> int:
        return self.calendar_date.toordinal()

    @property
    def cache_key(self) -> str:
        return f"{self.cycle_year:04d}-{self.issue:03d}"

    def display(self, *, include_year: bool = True) -> str:
        return f"{self.cache_key}期" if include_year else f"{self.issue}期"

    def previous(self, steps: int = 1) -> "PeriodKey":
        if type(steps) is not int or steps < 0:
            raise ValueError("回退步数必须为非负整数")
        target = self.calendar_date - timedelta(days=steps)
        return PeriodKey(target.year, target.timetuple().tm_yday)

    def next(self, steps: int = 1) -> "PeriodKey":
        if type(steps) is not int or steps < 0:
            raise ValueError("前进步数必须为非负整数")
        target = self.calendar_date + timedelta(days=steps)
        return PeriodKey(target.year, target.timetuple().tm_yday)

    @classmethod
    def parse(cls, raw: object, default_year: int | None = None) -> "PeriodKey":
        if isinstance(raw, cls):
            return raw
        if type(raw) is int:
            return cls(default_year or current_tokyo_period().cycle_year, raw)
        text = str(raw or "").strip()
        match = _PERIOD_KEY_RE.fullmatch(text)
        if match is not None:
            return cls(int(match.group("year")), int(match.group("issue")))
        if text.isdigit():
            return cls(default_year or current_tokyo_period().cycle_year, int(text))
        raise ValueError(f"非法期数标识: {raw!r}；应为 N 或 YYYY-NNN")


def period_window(base: PeriodKey, count: int) -> tuple[PeriodKey, ...]:
    if type(count) is not int or count < 1:
        raise ValueError("历史窗口必须为正整数")
    return tuple(base.previous(offset) for offset in range(count))


def issue_map_for_window(base: PeriodKey, count: int) -> dict[int, PeriodKey]:
    """Map bare page issue numbers to unique cycle-aware keys in one window."""

    result: dict[int, PeriodKey] = {}
    for key in period_window(base, count):
        previous = result.get(key.issue)
        if previous is not None and previous != key:
            raise ValueError("历史窗口过长，裸期数在多个周期内重复，无法无歧义映射")
        result[key.issue] = key
    return result


def resolve_issue_in_window(issue: int, base: PeriodKey, count: int) -> PeriodKey | None:
    if type(issue) is not int:
        return None
    return issue_map_for_window(base, count).get(issue)


def period_key_sort_value(value: int | PeriodKey) -> int:
    return value.ordinal if isinstance(value, PeriodKey) else int(value)


def periods_are_consecutive_descending(previous: int | PeriodKey, current: int | PeriodKey) -> bool:
    if isinstance(previous, PeriodKey) and isinstance(current, PeriodKey):
        return previous.previous() == current
    if isinstance(previous, PeriodKey) or isinstance(current, PeriodKey):
        return False
    return int(previous) - int(current) == 1


def infer_legacy_cycle_year(updated_at: object, fallback_year: int | None = None) -> int:
    text = str(updated_at or "").strip()
    match = re.match(r"^(\d{4})-", text)
    if match is not None:
        return int(match.group(1))
    return fallback_year or current_tokyo_period().cycle_year
