"""Cycle-aware period identity and compatibility helpers.

Production pages expose bare issue numbers. Persistence and duplicate checks use
``PeriodKey`` so the same number from different cycles never collides.  The
module supports calendar-year cycles and explicitly-sized fixed cycles while
keeping the older ``cycle_year/issue`` API used by the single-period cache.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Mapping

TOKYO_ZONE = timezone(timedelta(hours=9), name="Asia/Tokyo")
_PERIOD_KEY_RE = re.compile(r"^(?P<cycle>\d{1,4})[-/:](?P<issue>\d{1,3})$")


def issues_in_cycle(cycle_year: int) -> int:
    """Return the number of daily issues in a Gregorian calendar year."""

    if type(cycle_year) is not int or not 1 <= cycle_year <= 9999:
        raise ValueError(f"非法周期年份: {cycle_year}")
    return date(cycle_year, 12, 31).timetuple().tm_yday


def current_tokyo_period(now: datetime | None = None) -> "PeriodKey":
    current = now.astimezone(TOKYO_ZONE) if now is not None else datetime.now(TOKYO_ZONE)
    return PeriodKey(current.year, current.timetuple().tm_yday)


@dataclass(frozen=True, order=True, slots=True)
class PeriodKey:
    """Globally unique issue identity.

    Calendar-year callers use a four-digit cycle.  Fixed-cycle callers may use
    smaller positive cycle numbers; their exact cycle length is enforced by
    ``CyclePolicy``/``PeriodContext`` rather than by this value object.
    """

    cycle_year: int
    issue: int

    def __post_init__(self) -> None:
        if type(self.cycle_year) is not int or self.cycle_year < 1:
            raise ValueError(f"非法周期: {self.cycle_year!r}")
        if type(self.issue) is not int:
            raise ValueError(f"非法期数: {self.issue!r}")
        maximum = issues_in_cycle(self.cycle_year) if self.cycle_year >= 1000 else 999
        if not 1 <= self.issue <= maximum:
            raise ValueError(
                f"{self.cycle_year}周期期数必须在1至{maximum}之间: {self.issue!r}"
            )

    @property
    def cycle(self) -> int:
        return self.cycle_year

    @property
    def number(self) -> int:
        return self.issue

    @property
    def calendar_date(self) -> date:
        if self.cycle_year < 1000:
            raise ValueError("fixed-cycle PeriodKey has no Gregorian calendar date")
        return date(self.cycle_year, 1, 1) + timedelta(days=self.issue - 1)

    @property
    def ordinal(self) -> int:
        if self.cycle_year < 1000:
            return self.cycle_year * 1000 + self.issue
        return self.calendar_date.toordinal()

    @property
    def cache_key(self) -> str:
        return f"{self.cycle_year:04d}-{self.issue:03d}"

    def token(self) -> str:
        return f"{self.cycle_year}:{self.issue:03d}"

    def display(self, *, include_year: bool = True) -> str:
        return f"{self.cache_key}期" if include_year else f"{self.issue}期"

    def previous(self, steps: int = 1) -> "PeriodKey":
        if type(steps) is not int or steps < 0:
            raise ValueError("回退步数必须为非负整数")
        if self.cycle_year < 1000:
            raise ValueError("fixed-cycle PeriodKey requires CyclePolicy for navigation")
        target = self.calendar_date - timedelta(days=steps)
        return PeriodKey(target.year, target.timetuple().tm_yday)

    def next(self, steps: int = 1) -> "PeriodKey":
        if type(steps) is not int or steps < 0:
            raise ValueError("前进步数必须为非负整数")
        if self.cycle_year < 1000:
            raise ValueError("fixed-cycle PeriodKey requires CyclePolicy for navigation")
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
            return cls(int(match.group("cycle")), int(match.group("issue")))
        if text.isdigit():
            return cls(default_year or current_tokyo_period().cycle_year, int(text))
        raise ValueError(f"非法期数标识: {raw!r}；应为 N、YYYY-NNN 或 YYYY:NNN")


@dataclass(frozen=True, slots=True)
class CyclePolicy:
    """How issue numbers roll across cycle boundaries."""

    mode: str = "year"
    fixed_length: int | None = None

    def __post_init__(self) -> None:
        if self.mode not in {"year", "fixed"}:
            raise ValueError(f"unsupported cycle mode: {self.mode}")
        if self.mode == "year":
            if self.fixed_length is not None:
                raise ValueError("year cycle mode does not accept fixed_length")
            return
        if type(self.fixed_length) is not int or not 1 <= self.fixed_length <= 999:
            raise ValueError("fixed cycle mode requires cycle length from 1 to 999")

    def length(self, cycle: int) -> int:
        return issues_in_cycle(cycle) if self.mode == "year" else int(self.fixed_length)

    def validate(self, key: PeriodKey) -> None:
        maximum = self.length(key.cycle)
        if not 1 <= key.number <= maximum:
            raise ValueError(
                f"{key.cycle}周期期数必须在1至{maximum}之间: {key.number}"
            )

    def previous(self, key: PeriodKey, steps: int = 1) -> PeriodKey:
        if type(steps) is not int or steps < 0:
            raise ValueError("回退步数必须为非负整数")
        self.validate(key)
        current = key
        for _ in range(steps):
            if current.number > 1:
                current = PeriodKey(current.cycle, current.number - 1)
            else:
                if current.cycle <= 1:
                    raise ValueError("cannot move before cycle 1")
                previous_cycle = current.cycle - 1
                current = PeriodKey(previous_cycle, self.length(previous_cycle))
        return current

    def next(self, key: PeriodKey, steps: int = 1) -> PeriodKey:
        if type(steps) is not int or steps < 0:
            raise ValueError("前进步数必须为非负整数")
        self.validate(key)
        current = key
        for _ in range(steps):
            maximum = self.length(current.cycle)
            if current.number < maximum:
                current = PeriodKey(current.cycle, current.number + 1)
            else:
                current = PeriodKey(current.cycle + 1, 1)
        return current


@dataclass(frozen=True, slots=True)
class PeriodContext:
    current: PeriodKey
    policy: CyclePolicy
    periods: int = 10

    def __post_init__(self) -> None:
        if type(self.periods) is not int or self.periods < 1:
            raise ValueError("历史窗口必须为正整数")
        self.policy.validate(self.current)
        # A bare issue number must map to at most one key in one context.
        numbers = [key.number for key in self.window]
        if len(numbers) != len(set(numbers)):
            raise ValueError("历史窗口跨越过多周期，裸期数出现重复，无法无歧义映射")

    @property
    def window(self) -> tuple[PeriodKey, ...]:
        values = [self.current]
        for _ in range(1, self.periods):
            values.append(self.policy.previous(values[-1]))
        return tuple(values)

    def resolve_number(self, number: int) -> PeriodKey | None:
        matches = [key for key in self.window if key.number == number]
        if len(matches) > 1:
            raise ValueError(f"期数{number}在窗口内对应多个周期")
        return matches[0] if matches else None


def period_window(base: PeriodKey, count: int) -> tuple[PeriodKey, ...]:
    if type(count) is not int or count < 1:
        raise ValueError("历史窗口必须为正整数")
    if base.cycle >= 1000:
        return tuple(base.previous(offset) for offset in range(count))
    raise ValueError("fixed-cycle window requires PeriodContext/CyclePolicy")


def issue_map_for_window(base: PeriodKey, count: int) -> dict[int, PeriodKey]:
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
        if previous.cycle >= 1000 and current.cycle >= 1000:
            return previous.previous() == current
        if previous.cycle == current.cycle:
            return previous.number - current.number == 1
        return False
    if isinstance(previous, PeriodKey) or isinstance(current, PeriodKey):
        return False
    return int(previous) - int(current) == 1


def serialize_period_mapping(
    mapping: Mapping[PeriodKey, str],
    *,
    context: PeriodContext,
) -> dict[str, str]:
    output: dict[str, str] = {}
    for key in context.window:
        if key in mapping:
            output[key.token()] = str(mapping[key])
    extras = sorted((key for key in mapping if key not in set(context.window)), reverse=True)
    for key in extras:
        output[key.token()] = str(mapping[key])
    return output


def deserialize_period_mapping(
    mapping: Mapping[object, object],
    *,
    context: PeriodContext,
    legacy_cycle: int | None = None,
) -> dict[PeriodKey, str]:
    if not isinstance(mapping, Mapping):
        raise ValueError("period mapping must be an object")
    by_number: dict[int, PeriodKey] = {}
    for key in context.window:
        if key.number in by_number and by_number[key.number] != key:
            raise ValueError(f"期数{key.number}在窗口内对应多个周期")
        by_number[key.number] = key

    result: dict[PeriodKey, str] = {}
    for raw_key, raw_value in mapping.items():
        text = str(raw_key).strip()
        if not text:
            raise ValueError("period mapping contains an empty key")
        if text.isdigit():
            number = int(text)
            key = by_number.get(number)
            if key is None:
                if legacy_cycle is None:
                    raise ValueError(f"legacy period {number} is outside the requested window")
                key = PeriodKey(int(legacy_cycle), number)
        else:
            key = PeriodKey.parse(text)
        value = str(raw_value)
        previous = result.get(key)
        if previous is not None and previous != value:
            raise ValueError(f"period {key.token()} has conflicting values")
        result[key] = value
    return result


def trim_period_mapping(
    mapping: Mapping[PeriodKey, str],
    context: PeriodContext,
) -> dict[PeriodKey, str]:
    return {
        key: str(mapping[key])
        for key in context.window
        if key in mapping
    }


def longest_equal_consecutive_run(
    left: Mapping[PeriodKey, str],
    right: Mapping[PeriodKey, str],
    context: PeriodContext,
) -> tuple[tuple[PeriodKey, ...], tuple[str, ...]]:
    best_keys: list[PeriodKey] = []
    best_values: list[str] = []
    current_keys: list[PeriodKey] = []
    current_values: list[str] = []
    for key in context.window:
        if key in left and key in right and left[key] == right[key]:
            current_keys.append(key)
            current_values.append(left[key])
            continue
        if len(current_keys) > len(best_keys):
            best_keys, best_values = current_keys, current_values
        current_keys, current_values = [], []
    if len(current_keys) > len(best_keys):
        best_keys, best_values = current_keys, current_values
    return tuple(best_keys), tuple(best_values)


def infer_cycle_from_timestamp(updated_at: object, fallback_cycle: int | None = None) -> int:
    text = str(updated_at or "").strip()
    match = re.match(r"^(\d{4})-", text)
    if match is not None:
        return int(match.group(1))
    return fallback_cycle or current_tokyo_period().cycle


def infer_legacy_cycle_year(updated_at: object, fallback_year: int | None = None) -> int:
    return infer_cycle_from_timestamp(updated_at, fallback_year)


__all__ = [
    "CyclePolicy",
    "PeriodContext",
    "PeriodKey",
    "TOKYO_ZONE",
    "current_tokyo_period",
    "deserialize_period_mapping",
    "infer_cycle_from_timestamp",
    "infer_legacy_cycle_year",
    "issue_map_for_window",
    "issues_in_cycle",
    "longest_equal_consecutive_run",
    "period_key_sort_value",
    "period_window",
    "periods_are_consecutive_descending",
    "resolve_issue_in_window",
    "serialize_period_mapping",
    "trim_period_mapping",
]
