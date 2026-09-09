"""Validated recent-history cache with cycle-aware period identity.

Schema v1 used bare issue numbers.  Schema v2 stores ``YYYY-NNN`` keys while
retaining ``base_period`` for human/backwards compatibility.  Reading v1 is
supported; the first production update with ``cycle_year`` migrates it to v2.
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Mapping

from he_app.domain.errors import FingerprintCacheError
from he_app.domain.models import Site
from he_app.domain.periods import (
    PeriodKey,
    infer_legacy_cycle_year,
    period_window,
    resolve_issue_in_window,
)
from he_app.storage.atomic_write import exclusive_path_lock, write_text_atomic_unlocked
from he_app.validation.period import is_valid_sum_value


Outcome = tuple[Site, str | None, str, str | None, list[str], str | None]
CACHE_UPDATE_SUCCESS_PERCENT = 85
SCHEMA_VERSION = 2


def cache_site_key(site: Site) -> str:
    return site.site_id or " ".join(site.name.split())


def cache_update_allowed(success_count: int, total_count: int) -> bool:
    return total_count > 0 and success_count * 100 > total_count * CACHE_UPDATE_SUCCESS_PERCENT


def _outcome_failure_message(outcome: Outcome) -> str:
    _site, _result, detail, error, _rank_values, previous_reason = outcome
    return error or detail or previous_reason or "未抓到可参与缓存的数据"


def _is_cycle_schema(cache: Mapping[str, object]) -> bool:
    return cache.get("schema_version") == SCHEMA_VERSION or bool(cache.get("base_period_key"))


def _base_key(cache: Mapping[str, object], cycle_year: int | None = None) -> PeriodKey | None:
    raw_key = cache.get("base_period_key")
    if raw_key:
        try:
            return PeriodKey.parse(raw_key)
        except ValueError as exc:
            raise FingerprintCacheError(f"指纹缓存 base_period_key 非法: {raw_key}") from exc
    raw_period = cache.get("base_period", 0)
    if type(raw_period) is not int or raw_period < 0:
        raise FingerprintCacheError("指纹缓存 base_period 非法")
    if raw_period == 0:
        return None
    year = cycle_year or cache.get("cycle_year")
    if type(year) is not int:
        year = infer_legacy_cycle_year(cache.get("updated_at"))
    try:
        return PeriodKey(int(year), raw_period)
    except ValueError as exc:
        raise FingerprintCacheError(f"指纹缓存基准期非法: {year}-{raw_period}") from exc



def cache_base_period_key(
    cache: Mapping[str, object], cycle_year: int | None = None
) -> PeriodKey | None:
    """Return the globally unique cache baseline, migrating v1 in memory."""

    return _base_key(cache, cycle_year)

def _parse_period_token(
    raw: object,
    *,
    base: PeriodKey,
    periods: int,
) -> PeriodKey:
    text = str(raw).strip()
    try:
        if "-" in text or "/" in text:
            return PeriodKey.parse(text)
        if text.isdigit():
            resolved = resolve_issue_in_window(int(text), base, periods)
            if resolved is None:
                raise ValueError("裸期数不在缓存窗口")
            return resolved
    except ValueError as exc:
        raise FingerprintCacheError(f"非法缓存期数键: {raw}") from exc
    raise FingerprintCacheError(f"非法缓存期数键: {raw}")


def _validate_value(site: Site, value: object, label: str) -> str:
    if not isinstance(value, str):
        raise FingerprintCacheError(f"{label}不是文本")
    values = value.split(",")
    if (
        len(values) != site.value_count
        or len(set(values)) != len(values)
        or not all(is_valid_sum_value(item) for item in values)
    ):
        raise FingerprintCacheError(f"{label}合数非法: {value}")
    return value


def _fingerprint_as_keys(
    raw: object,
    *,
    base: PeriodKey,
    periods: int,
    site: Site | None = None,
) -> dict[PeriodKey, str]:
    if not isinstance(raw, dict):
        raise FingerprintCacheError("fingerprint 不是对象")
    allowed = set(period_window(base, periods))
    result: dict[PeriodKey, str] = {}
    for raw_period, raw_value in raw.items():
        key = _parse_period_token(raw_period, base=base, periods=periods)
        if key not in allowed:
            raise FingerprintCacheError(f"指纹包含窗口外期数: {key.cache_key}")
        value = (
            _validate_value(site, raw_value, f"{key.cache_key}期")
            if site is not None
            else str(raw_value)
        )
        if key in result and result[key] != value:
            raise FingerprintCacheError(f"同一期存在冲突指纹: {key.cache_key}")
        result[key] = value
    return dict(sorted(result.items(), reverse=True))


def _serialize_fingerprint(
    fingerprint: Mapping[PeriodKey, str], *, cycle_mode: bool
) -> dict[str, str]:
    return {
        (key.cache_key if cycle_mode else str(key.issue)): value
        for key, value in sorted(fingerprint.items(), reverse=True)
    }


def _failure_entry(
    site: Site,
    outcome: Outcome,
    period: int,
    now: str,
    cycle_year: int | None = None,
) -> dict:
    entry = {
        "id": cache_site_key(site),
        "name": site.name,
        "url": site.url,
        "period": period,
        "status": "失败",
        "error": _outcome_failure_message(outcome),
        "updated_at": now,
    }
    if cycle_year is not None:
        entry["period_key"] = PeriodKey(cycle_year, period).cache_key
    return entry


def load_recent_cache(path: Path) -> dict:
    if not path.exists():
        return {
            "base_period": 0,
            "periods": 10,
            "sites": [],
            "errors": [],
        }
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FingerprintCacheError(f"指纹缓存无法读取: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise FingerprintCacheError(f"指纹缓存格式错误: {path}")
    if type(data.get("base_period", 0)) is not int or data.get("base_period", 0) < 0:
        raise FingerprintCacheError(f"指纹缓存基准字段错误: {path}")
    if type(data.get("periods", 10)) is not int or data.get("periods", 10) < 1:
        raise FingerprintCacheError(f"指纹缓存历史窗口错误: {path}")
    if not isinstance(data.get("sites", []), list) or not isinstance(data.get("errors", []), list):
        raise FingerprintCacheError(f"指纹缓存站点字段错误: {path}")
    if data.get("schema_version") not in {None, 1, SCHEMA_VERSION}:
        raise FingerprintCacheError(f"不支持的指纹缓存版本: {data.get('schema_version')}")
    data.setdefault("sites", [])
    data.setdefault("errors", [])
    return data


def _cached_site_identity(item: dict, index: int) -> Site:
    required = {"id", "name", "url", "pick", "browser", "click_first", "fingerprint"}
    if not required.issubset(item):
        raise FingerprintCacheError(f"指纹缓存第{index}个站点字段不完整")
    if not all(isinstance(item[key], str) for key in ("id", "name", "url", "pick")):
        raise FingerprintCacheError(f"指纹缓存第{index}个站点文本字段类型错误")
    if type(item["browser"]) is not bool or type(item["click_first"]) is not bool:
        raise FingerprintCacheError(f"指纹缓存第{index}个站点布尔字段类型错误")
    expected_count = 2 if item["id"] == "s085_kcvpleh" else 1
    value_count = item.get("value_count", expected_count)
    if type(value_count) is not int or value_count != expected_count:
        raise FingerprintCacheError(
            f"指纹缓存第{index}个站点 value_count 与站点数据契约不一致"
        )
    if item.get("top_period_exception") is not None:
        raise FingerprintCacheError("不支持期数方向例外 top_period_exception")
    if not item["id"].strip() or not item["name"].strip() or not item["url"].strip():
        raise FingerprintCacheError(f"指纹缓存第{index}个站点身份为空")
    if item["pick"] not in {"top", "bottom"}:
        raise FingerprintCacheError(f"指纹缓存第{index}个站点方向非法")
    return Site(
        item["name"].strip(),
        item["url"].strip(),
        item["pick"],
        item["browser"],
        item["click_first"],
        item["id"].strip(),
        value_count,
    )


def validate_recent_cache_identity(
    cache: dict,
    sites: list[Site],
    allow_configured_subset: bool = False,
    cycle_year: int | None = None,
) -> None:
    raw_sites = cache.get("sites", [])
    base = _base_key(cache, cycle_year)
    if not raw_sites and base is None:
        return
    if base is None:
        raise FingerprintCacheError("非空缓存缺少基准期")
    periods = int(cache.get("periods", 0) or 0)
    cached_sites: list[Site] = []
    used_ids: set[str] = set()
    for index, item in enumerate(raw_sites, start=1):
        if not isinstance(item, dict):
            raise FingerprintCacheError(f"指纹缓存第{index}个站点不是对象")
        cached_site = _cached_site_identity(item, index)
        if cached_site.site_id in used_ids:
            raise FingerprintCacheError(f"指纹缓存包含重复站点ID: {cached_site.site_id}")
        used_ids.add(cached_site.site_id)
        _fingerprint_as_keys(
            item["fingerprint"],
            base=base,
            periods=periods,
            site=cached_site,
        )
        cached_sites.append(cached_site)

    cached_by_id = {site.site_id: site for site in cached_sites}
    configured_by_id = {site.site_id: site for site in sites}
    if len(configured_by_id) != len(sites):
        raise FingerprintCacheError("当前配置包含重复站点ID")
    if not allow_configured_subset and cached_by_id.keys() != configured_by_id.keys():
        raise FingerprintCacheError("指纹缓存与当前配置的站点ID集合不一致")
    for site_id, configured in configured_by_id.items():
        if cached_by_id.get(site_id) != configured:
            raise FingerprintCacheError(f"指纹缓存与当前配置的站点身份不一致: {site_id}")


def trim_fingerprint(
    fingerprint: Mapping[object, str],
    periods: int,
    base_period: int | PeriodKey | None = None,
    cycle_year: int | None = None,
) -> dict[str, str]:
    if periods < 1:
        raise FingerprintCacheError("历史窗口必须为正数")
    cycle_mode = isinstance(base_period, PeriodKey) or cycle_year is not None or any(
        isinstance(key, PeriodKey) or "-" in str(key) for key in fingerprint
    )
    if isinstance(base_period, PeriodKey):
        base = base_period
    elif base_period is not None:
        base = PeriodKey(cycle_year or 2000, int(base_period))
    elif fingerprint:
        parsed_explicit = [
            PeriodKey.parse(key)
            for key in fingerprint
            if isinstance(key, PeriodKey) or "-" in str(key)
        ]
        if parsed_explicit:
            base = max(parsed_explicit)
        else:
            base = PeriodKey(cycle_year or 2000, max(int(key) for key in fingerprint))
    else:
        return {}
    normalized = _fingerprint_as_keys(
        dict(fingerprint), base=base, periods=periods, site=None
    )
    return _serialize_fingerprint(normalized, cycle_mode=cycle_mode)


def _current_and_base(
    cache: dict,
    period: int,
    cycle_year: int | None,
) -> tuple[PeriodKey, PeriodKey, bool]:
    cycle_mode = cycle_year is not None or _is_cycle_schema(cache)
    if cycle_mode:
        year = cycle_year or cache.get("cycle_year") or infer_legacy_cycle_year(cache.get("updated_at"))
        current = PeriodKey(int(year), period)
        cached = _base_key(cache, int(year))
        base = max(current, cached) if cached is not None else current
        return current, base, True

    cached_period = int(cache.get("base_period", 0) or 0)
    if cached_period >= 300 and 1 <= period <= 30:
        raise FingerprintCacheError(
            "检测到期数回绕：调用方必须提供 cycle_year 才能迁移并连续保存跨周期历史"
        )
    current = PeriodKey(2000, period)
    base = PeriodKey(2000, max(cached_period, period))
    return current, base, False


def _existing_fingerprint(
    item: dict,
    *,
    source_base: PeriodKey,
    periods: int,
    site: Site,
) -> dict[PeriodKey, str]:
    """Decode a stored fingerprint against the cache baseline that wrote it.

    The caller may be advancing to a much newer baseline.  Decoding old bare
    issue keys against the *new* baseline would incorrectly reject a valid but
    stale v1 cache before it has a chance to be trimmed/migrated.
    """

    raw = item.get("fingerprint", {})
    if not isinstance(raw, dict):
        return {}
    return _fingerprint_as_keys(
        raw, base=source_base, periods=periods, site=site
    )


def build_recent_cache_payload(
    cache: dict,
    sites: list[Site],
    outcomes: dict[int, Outcome],
    period: int,
    periods: int,
    preserve_unconfigured_sites: bool,
    cycle_year: int | None = None,
) -> str:
    current, base, cycle_mode = _current_and_base(cache, period, cycle_year)
    if cycle_mode:
        source_base = _base_key(cache, cycle_year) or current
    else:
        source_base = PeriodKey(
            2000, int(cache.get("base_period", 0) or current.issue)
        )
    source_periods = int(cache.get("periods", periods) or periods)
    allowed = set(period_window(base, periods))
    existing_by_key = {
        str(item.get("id", "")).strip(): item
        for item in cache.get("sites", [])
        if isinstance(item, dict) and str(item.get("id", "")).strip()
    }

    next_sites: list[dict] = []
    next_errors: list[dict] = []
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    seen_keys: set[str] = set()
    for index, site in enumerate(sites):
        site_key = cache_site_key(site)
        previous = existing_by_key.get(site_key) or {}
        seen_keys.add(site_key)
        fingerprint = {
            key: value
            for key, value in _existing_fingerprint(
                previous,
                source_base=source_base,
                periods=source_periods,
                site=site,
            ).items()
            if key in allowed
        }
        outcome = outcomes.get(index, (site, None, "", "未执行", [], None))
        returned_site, result, detail, error, rank_values, previous_reason = outcome
        if returned_site != site:
            raise FingerprintCacheError(f"结果站点身份不一致: {site.site_id}")
        if result and (
            error
            or len(rank_values) != site.value_count
            or len(set(rank_values)) != len(rank_values)
            or not all(is_valid_sum_value(value) for value in rank_values)
        ):
            raise FingerprintCacheError(f"本期指纹数据数量或格式错误: {site.site_id}")
        if current in allowed:
            if result and rank_values:
                fingerprint[current] = ",".join(rank_values)
            else:
                fingerprint.pop(current, None)

        cache_item = {
            "id": site_key,
            "name": site.name,
            "url": site.url,
            "pick": site.pick,
            "browser": site.browser,
            "click_first": site.click_first,
            "fingerprint": _serialize_fingerprint(
                fingerprint, cycle_mode=cycle_mode
            ),
            "updated_at": now,
        }
        if site.value_count != 1:
            cache_item["value_count"] = site.value_count
        next_sites.append(cache_item)
        if not result or not fingerprint:
            next_errors.append(
                _failure_entry(
                    site,
                    (site, result, detail, error, rank_values, previous_reason),
                    period,
                    now,
                    current.cycle_year if cycle_mode else None,
                )
            )

    if preserve_unconfigured_sites:
        configured_by_id = {item["id"]: item for item in next_sites}
        ordered_sites: list[dict] = []
        for item in cache.get("sites", []):
            if not isinstance(item, dict):
                continue
            item_id = str(item.get("id", "")).strip()
            replacement = configured_by_id.pop(item_id, None)
            if replacement is not None:
                ordered_sites.append(replacement)
                continue
            cached_site = _cached_site_identity(item, len(ordered_sites) + 1)
            fingerprint = _existing_fingerprint(
                item,
                source_base=source_base,
                periods=source_periods,
                site=cached_site,
            )
            kept = dict(item)
            kept["fingerprint"] = _serialize_fingerprint(
                {key: value for key, value in fingerprint.items() if key in allowed},
                cycle_mode=cycle_mode,
            )
            ordered_sites.append(kept)
        ordered_sites.extend(
            item for item in next_sites if item["id"] in configured_by_id
        )
        next_sites = ordered_sites
        next_errors = [
            item
            for item in cache.get("errors", [])
            if not isinstance(item, dict)
            or str(item.get("id", "")).strip() not in seen_keys
        ] + next_errors

    payload = {
        "base_period": base.issue,
        "periods": periods,
        "updated_at": now,
        "sites": next_sites,
        "errors": next_errors,
    }
    if cycle_mode:
        payload.update(
            {
                "schema_version": SCHEMA_VERSION,
                "cycle_year": base.cycle_year,
                "base_period_key": base.cache_key,
            }
        )
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def _update_recent_cache_locked(
    path: Path,
    sites: list[Site],
    outcomes: dict[int, Outcome],
    period: int,
    periods: int,
    preserve_unconfigured_sites: bool,
    cycle_year: int | None,
) -> None:
    cache = load_recent_cache(path)
    if preserve_unconfigured_sites and not cache["sites"]:
        raise FingerprintCacheError("局部重抓不能初始化空的正式缓存，请先完整抓取")
    validate_recent_cache_identity(
        cache, sites, preserve_unconfigured_sites, cycle_year=cycle_year
    )
    payload = build_recent_cache_payload(
        cache,
        sites,
        outcomes,
        period,
        periods,
        preserve_unconfigured_sites,
        cycle_year=cycle_year,
    )
    write_text_atomic_unlocked(path, payload, "utf-8")


def update_recent_cache_from_outcomes(
    path: Path,
    sites: list[Site],
    outcomes: dict[int, Outcome],
    period: int,
    periods: int = 10,
    preserve_unconfigured_sites: bool = False,
    *,
    cycle_year: int | None = None,
) -> None:
    with exclusive_path_lock(path):
        _update_recent_cache_locked(
            path,
            sites,
            outcomes,
            period,
            periods,
            preserve_unconfigured_sites,
            cycle_year,
        )


def _record_recent_cache_failures_locked(
    path: Path,
    sites: list[Site],
    outcomes: dict[int, Outcome],
    period: int,
    cycle_year: int | None,
) -> None:
    cache = load_recent_cache(path)
    validate_recent_cache_identity(
        cache, sites, allow_configured_subset=True, cycle_year=cycle_year
    )
    base = _base_key(cache, cycle_year)
    if base is None:
        raise FingerprintCacheError("正式缓存没有基准期，不能只记录局部失败")
    current = PeriodKey(cycle_year or base.cycle_year, period)
    cycle_mode = cycle_year is not None or _is_cycle_schema(cache)
    configured_keys = {cache_site_key(site) for site in sites}
    failed_sites = {
        cache_site_key(site): (
            site,
            outcomes.get(index, (site, None, "", "未执行", [], None)),
        )
        for index, site in enumerate(sites)
        if not outcomes.get(index, (site, None, "", "未执行", [], None))[1]
    }

    kept_sites: list[dict] = []
    for item in cache.get("sites", []):
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("id", "")).strip()
        cached_site = _cached_site_identity(item, len(kept_sites) + 1)
        fingerprint = _existing_fingerprint(
            item,
            source_base=base,
            periods=int(cache.get("periods", 10)),
            site=cached_site,
        )
        if item_id in failed_sites:
            fingerprint.pop(current, None)
        kept = dict(item)
        kept["fingerprint"] = _serialize_fingerprint(
            fingerprint, cycle_mode=cycle_mode
        )
        kept_sites.append(kept)

    now = time.strftime("%Y-%m-%d %H:%M:%S")
    kept_errors = [
        item
        for item in cache.get("errors", [])
        if not isinstance(item, dict)
        or str(item.get("id", "")).strip() not in configured_keys
    ]
    kept_errors.extend(
        _failure_entry(
            site,
            outcome,
            period,
            now,
            current.cycle_year if cycle_mode else None,
        )
        for site, outcome in failed_sites.values()
    )
    payload = dict(cache)
    payload["sites"] = kept_sites
    payload["errors"] = kept_errors
    if cycle_mode:
        payload["schema_version"] = SCHEMA_VERSION
        payload["cycle_year"] = base.cycle_year
        payload["base_period_key"] = base.cache_key
    write_text_atomic_unlocked(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        "utf-8",
    )


def record_recent_cache_failures(
    path: Path,
    sites: list[Site],
    outcomes: dict[int, Outcome],
    period: int,
    *,
    cycle_year: int | None = None,
) -> None:
    with exclusive_path_lock(path):
        _record_recent_cache_failures_locked(
            path, sites, outcomes, period, cycle_year
        )


def read_validated_fingerprints(
    path: Path,
    period: int,
    periods: int,
    cycle_year: int | None = None,
):
    cache = load_recent_cache(path)
    base = PeriodKey(cycle_year, period) if cycle_year is not None else _base_key(cache)
    if base is None:
        return [], {}, {}
    sites = [
        _cached_site_identity(item, index)
        for index, item in enumerate(cache["sites"], 1)
    ]
    validate_recent_cache_identity(cache, sites, cycle_year=base.cycle_year)
    requested = PeriodKey(cycle_year or base.cycle_year, period)
    cycle_mode = cycle_year is not None or _is_cycle_schema(cache)
    fingerprints = {}
    errors = {}
    indexes = {site.site_id: index for index, site in enumerate(sites)}
    for index, item in enumerate(cache["sites"]):
        values = _fingerprint_as_keys(
            item["fingerprint"],
            base=requested,
            periods=periods,
            site=sites[index],
        )
        if values:
            fingerprints[index] = (
                values
                if cycle_mode
                else {key.issue: value for key, value in values.items()}
            )
    for item in cache["errors"]:
        if not isinstance(item, dict) or item.get("id") not in indexes:
            raise FingerprintCacheError("缓存错误记录缺少对应站点身份")
        errors[indexes[item["id"]]] = str(
            item.get("error", "未抓到可参与重复检测的数据")
        )
    return sites, fingerprints, errors


def validate_cache_freshness(cache: dict, max_age_hours: float = 24.0) -> None:
    if max_age_hours <= 0:
        raise FingerprintCacheError("正式判重缓存有效期必须为正数")
    stamp = cache.get("updated_at")
    try:
        updated = datetime.strptime(str(stamp), "%Y-%m-%d %H:%M:%S").timestamp()
    except (TypeError, ValueError, OverflowError) as exc:
        raise FingerprintCacheError("正式判重缓存缺少有效更新时间") from exc
    age = time.time() - updated
    if age < -300 or age > max_age_hours * 3600:
        raise FingerprintCacheError("正式判重缓存已过期或更新时间在未来")


def write_history_fingerprints(
    path: Path,
    sites: list[Site],
    fingerprints: dict[int, Mapping[int | PeriodKey, str]],
    errors: dict[int, str],
    period: int,
    periods: int,
    lock_timeout: float = 30.0,
    *,
    cycle_year: int | None = None,
) -> None:
    """Write complete history without hiding a failed current issue."""

    with exclusive_path_lock(path, timeout=lock_timeout):
        cache = load_recent_cache(path)
        validate_recent_cache_identity(cache, sites, cycle_year=cycle_year)
        base_key = PeriodKey(cycle_year, period) if cycle_year is not None else PeriodKey(2000, period)
        outcomes: dict[int, Outcome] = {}
        for index, site in enumerate(sites):
            raw = fingerprints.get(index, {})
            current_value = raw.get(base_key)
            if current_value is None:
                current_value = raw.get(period)
            success = current_value is not None and index not in errors
            current_text = str(current_value) if current_value is not None else ""
            outcomes[index] = (
                site,
                f"{current_text} {site.name}" if success else None,
                errors.get(index, "当期没有有效历史行"),
                None,
                current_text.split(",") if success else [],
                None,
            )
        payload = json.loads(
            build_recent_cache_payload(
                cache,
                sites,
                outcomes,
                period,
                periods,
                False,
                cycle_year=cycle_year,
            )
        )
        base = _base_key(payload, cycle_year)
        assert base is not None
        allowed = set(period_window(base, periods))
        for index, item in enumerate(payload["sites"]):
            if index in errors:
                continue
            existing = _fingerprint_as_keys(
                item["fingerprint"],
                base=base,
                periods=periods,
                site=sites[index],
            )
            for raw_key, raw_value in fingerprints.get(index, {}).items():
                key = (
                    raw_key
                    if isinstance(raw_key, PeriodKey)
                    else resolve_issue_in_window(int(raw_key), base, periods)
                )
                if key is None or key not in allowed:
                    raise FingerprintCacheError(
                        f"非法历史指纹期数: {sites[index].site_id}"
                    )
                value = _validate_value(
                    sites[index], raw_value, f"{sites[index].site_id} {key.cache_key}"
                )
                existing[key] = value
            item["fingerprint"] = _serialize_fingerprint(
                existing,
                cycle_mode=cycle_year is not None or _is_cycle_schema(payload),
            )
        validate_recent_cache_identity(
            payload, sites, cycle_year=cycle_year or base.cycle_year
        )
        write_text_atomic_unlocked(
            path,
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            "utf-8",
        )
