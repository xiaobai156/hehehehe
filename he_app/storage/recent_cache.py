import json
import time
from pathlib import Path

from he_app.domain.errors import FingerprintCacheError
from he_app.domain.models import Site
from he_app.storage.atomic_write import exclusive_path_lock, write_text_atomic_unlocked
from he_app.validation.period import is_valid_sum_value


Outcome = tuple[Site, str | None, str, str | None, list[str], str | None]
CACHE_UPDATE_SUCCESS_PERCENT = 85


def cache_site_key(site: Site) -> str:
    return site.site_id or " ".join(site.name.split())


def cache_update_allowed(success_count: int, total_count: int) -> bool:
    """Allow cache progression only when success rate is strictly above 85%."""

    return total_count > 0 and success_count * 100 > total_count * CACHE_UPDATE_SUCCESS_PERCENT


def _outcome_failure_message(outcome: Outcome) -> str:
    _site, _result, detail, error, _rank_values, previous_reason = outcome
    return error or detail or previous_reason or "未抓到可参与缓存的数据"


def _failure_entry(site: Site, outcome: Outcome, period: int, now: str) -> dict:
    return {
        "id": cache_site_key(site),
        "name": site.name,
        "url": site.url,
        "period": period,
        "status": "失败",
        "error": _outcome_failure_message(outcome),
        "updated_at": now,
    }


def load_recent_cache(path: Path) -> dict:
    if not path.exists():
        return {"base_period": 0, "periods": 10, "sites": [], "errors": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FingerprintCacheError(f"指纹缓存无法读取: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise FingerprintCacheError(f"指纹缓存格式错误: {path}")
    if type(data.get("base_period", 0)) is not int or type(data.get("periods", 10)) is not int or data.get("base_period", 0) < 0 or data.get("periods", 10) < 1:
        raise FingerprintCacheError(f"指纹缓存基准字段错误: {path}")
    if not isinstance(data.get("sites", []), list) or not isinstance(data.get("errors", []), list):
        raise FingerprintCacheError(f"指纹缓存站点字段错误: {path}")
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
    value_count = item.get("value_count", 2 if item["id"] == "s085_kcvpleh" else 1)
    if type(value_count) is not int or value_count != (2 if item["id"] == "s085_kcvpleh" else 1):
        raise FingerprintCacheError(f"指纹缓存第{index}个站点 value_count 与站点数据契约不一致")
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
) -> None:
    raw_sites = cache.get("sites", [])
    if not raw_sites and int(cache.get("base_period", 0) or 0) == 0:
        return
    cached_sites: list[Site] = []
    used_ids: set[str] = set()
    base_period = int(cache.get("base_period", 0) or 0)
    periods = int(cache.get("periods", 0) or 0)
    minimum_period = base_period - periods + 1
    for index, item in enumerate(raw_sites, start=1):
        if not isinstance(item, dict):
            raise FingerprintCacheError(f"指纹缓存第{index}个站点不是对象")
        cached_site = _cached_site_identity(item, index)
        if cached_site.site_id in used_ids:
            raise FingerprintCacheError(f"指纹缓存包含重复站点ID: {cached_site.site_id}")
        used_ids.add(cached_site.site_id)
        raw_fingerprint = item["fingerprint"]
        if not isinstance(raw_fingerprint, dict):
            raise FingerprintCacheError(f"指纹缓存第{index}个站点 fingerprint 不是对象")
        for raw_period, raw_value in raw_fingerprint.items():
            if not str(raw_period).isdigit() or not isinstance(raw_value, str):
                raise FingerprintCacheError(f"指纹缓存第{index}个站点存在非法期数或合数")
            current_period = int(raw_period)
            values = raw_value.split(",")
            if not minimum_period <= current_period <= base_period:
                raise FingerprintCacheError(
                    f"指纹缓存第{index}个站点包含窗口外期数: {current_period}"
                )
            if (
                len(values) != cached_site.value_count
                or len(set(values)) != len(values)
                or not all(is_valid_sum_value(value) for value in values)
            ):
                raise FingerprintCacheError(
                    f"指纹缓存第{index}个站点{current_period}期合数非法: {raw_value}"
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
    fingerprint: dict[str, str],
    periods: int,
    base_period: int | None = None,
) -> dict[str, str]:
    normalized = {
        int(raw_period): str(value)
        for raw_period, value in fingerprint.items()
        if str(raw_period).isdigit() and value
    }
    if base_period is None:
        selected = sorted(normalized, reverse=True)[:periods]
    else:
        minimum = base_period - periods + 1
        selected = [period for period in sorted(normalized, reverse=True) if minimum <= period <= base_period]
    return {str(period): normalized[period] for period in selected}


def _update_recent_cache_locked(
    path: Path,
    sites: list[Site],
    outcomes: dict[int, Outcome],
    period: int,
    periods: int,
    preserve_unconfigured_sites: bool,
) -> None:
    cache = load_recent_cache(path)
    if preserve_unconfigured_sites and not cache["sites"]:
        raise FingerprintCacheError("局部重抓不能初始化空的正式缓存，请先完整抓取")
    validate_recent_cache_identity(cache, sites, preserve_unconfigured_sites)
    payload = build_recent_cache_payload(
        cache, sites, outcomes, period, periods, preserve_unconfigured_sites
    )
    write_text_atomic_unlocked(path, payload, "utf-8")


def build_recent_cache_payload(
    cache: dict,
    sites: list[Site],
    outcomes: dict[int, Outcome],
    period: int,
    periods: int,
    preserve_unconfigured_sites: bool,
) -> str:
    cached_base_period = int(cache.get("base_period", 0) or 0)
    if cached_base_period >= 300 and 1 <= period <= 30:
        raise FingerprintCacheError("检测到期数回绕：旧缓存没有周期身份，禁止把新周期与旧周期合并；请先备份并初始化新周期缓存")
    base_period = max(cached_base_period, period)
    existing_by_key: dict[str, dict] = {}
    for item in cache.get("sites", []):
        if not isinstance(item, dict):
            continue
        for key in (str(item.get("id", "")).strip(),):
            if key:
                existing_by_key[key] = item

    next_sites: list[dict] = []
    next_errors: list[dict] = []
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    seen_keys: set[str] = set()
    for index, site in enumerate(sites):
        previous = existing_by_key.get(cache_site_key(site)) or {}
        seen_keys.add(cache_site_key(site))
        previous_fingerprint = previous.get("fingerprint", {}) if isinstance(previous.get("fingerprint"), dict) else {}
        fingerprint = trim_fingerprint(
            {str(key): str(value) for key, value in previous_fingerprint.items()}, periods, base_period
        )
        _site, result, detail, error, rank_values, previous_reason = outcomes.get(
            index, (site, None, "", "未执行", [], None)
        )

        period_in_window = base_period - periods < period <= base_period
        if _site != site:
            raise FingerprintCacheError(f"结果站点身份不一致: {site.site_id}")
        if result and (error or len(rank_values) != site.value_count
                       or len(set(rank_values)) != len(rank_values)
                       or not all(is_valid_sum_value(value) for value in rank_values)):
            raise FingerprintCacheError(f"本期指纹数据数量或格式错误: {site.site_id}")
        if result and rank_values and period_in_window:
            fingerprint[str(period)] = ",".join(rank_values)
            fingerprint = trim_fingerprint(fingerprint, periods, base_period)
        elif period_in_window:
            fingerprint.pop(str(period), None)
        cache_item = {
            "id": cache_site_key(site),
            "name": site.name,
            "url": site.url,
            "pick": site.pick,
            "browser": site.browser,
            "click_first": site.click_first,
            "fingerprint": fingerprint,
            "updated_at": now,
        }
        if site.value_count != 1:
            cache_item["value_count"] = site.value_count
        next_sites.append(cache_item)
        if not result or not fingerprint:
            next_errors.append(_failure_entry(site, (site, result, detail, error, rank_values, previous_reason), period, now))

    if preserve_unconfigured_sites:
        configured_by_id = {
            str(item.get("id", "")).strip(): item for item in next_sites
        }
        ordered_sites: list[dict] = []
        for item in cache.get("sites", []):
            if not isinstance(item, dict):
                continue
            item_id = str(item.get("id", "")).strip()
            replacement = configured_by_id.pop(item_id, None)
            if replacement is not None:
                ordered_sites.append(replacement)
                continue
            fingerprint = item.get("fingerprint", {}) if isinstance(item.get("fingerprint"), dict) else {}
            trimmed = trim_fingerprint(
                {str(key): str(value) for key, value in fingerprint.items()}, periods, base_period
            )
            kept = dict(item)
            kept["fingerprint"] = trimmed
            ordered_sites.append(kept)
        ordered_sites.extend(
            item for item in next_sites if str(item.get("id", "")).strip() in configured_by_id
        )
        next_sites = ordered_sites
        next_errors = [
            item
            for item in cache.get("errors", [])
            if not isinstance(item, dict)
            or not (
                str(item.get("id", "")).strip() in seen_keys
                or str(item.get("url", "")).strip() in seen_keys
            )
        ] + next_errors

    payload = {
        "base_period": base_period,
        "periods": periods,
        "updated_at": now,
        "sites": next_sites,
        "errors": next_errors,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def _record_recent_cache_failures_locked(
    path: Path,
    sites: list[Site],
    outcomes: dict[int, Outcome],
    period: int,
) -> None:
    """Record failures without advancing the cache base or successful fingerprints."""

    cache = load_recent_cache(path)
    validate_recent_cache_identity(cache, sites, allow_configured_subset=True)
    configured_keys = {cache_site_key(site) for site in sites if cache_site_key(site)}
    failed_sites = {
        cache_site_key(site): (site, outcomes.get(index, (site, None, "", "未执行", [], None)))
        for index, site in enumerate(sites)
        if not outcomes.get(index, (site, None, "", "未执行", [], None))[1]
    }

    kept_sites: list[dict] = []
    for item in cache.get("sites", []):
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("id", "")).strip()
        failed = failed_sites.get(item_id)
        if failed is not None:
            kept = dict(item)
            fingerprint = item.get("fingerprint", {})
            if isinstance(fingerprint, dict):
                fingerprint = dict(fingerprint)
                fingerprint.pop(str(period), None)
                kept["fingerprint"] = fingerprint
            kept_sites.append(kept)
        else:
            kept_sites.append(dict(item))

    now = time.strftime("%Y-%m-%d %H:%M:%S")
    kept_errors = [
        item
        for item in cache.get("errors", [])
        if not isinstance(item, dict)
        or not (
            str(item.get("id", "")).strip() in configured_keys
        )
    ]
    kept_errors.extend(
        _failure_entry(site, outcome, period, now)
        for site, outcome in failed_sites.values()
    )

    payload = dict(cache)
    payload["sites"] = kept_sites
    payload["errors"] = kept_errors
    write_text_atomic_unlocked(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n", "utf-8")


def record_recent_cache_failures(
    path: Path,
    sites: list[Site],
    outcomes: dict[int, Outcome],
    period: int,
) -> None:
    """Write failure markers while leaving the cache base and successes unchanged."""

    with exclusive_path_lock(path):
        _record_recent_cache_failures_locked(path, sites, outcomes, period)


def update_recent_cache_from_outcomes(
    path: Path,
    sites: list[Site],
    outcomes: dict[int, Outcome],
    period: int,
    periods: int = 10,
    preserve_unconfigured_sites: bool = False,
) -> None:
    with exclusive_path_lock(path):
        _update_recent_cache_locked(path, sites, outcomes, period, periods, preserve_unconfigured_sites)


def read_validated_fingerprints(path: Path, period: int, periods: int):
    """Shared reader for daily collection and the duplicate checker."""
    cache = load_recent_cache(path)
    sites = [_cached_site_identity(item, index) for index, item in enumerate(cache["sites"], 1)]
    validate_recent_cache_identity(cache, sites)
    fingerprints = {}
    errors = {}
    indexes = {site.site_id: index for index, site in enumerate(sites)}
    for index, item in enumerate(cache["sites"]):
        values = trim_fingerprint(item["fingerprint"], periods, period)
        if values:
            fingerprints[index] = {int(key): value for key, value in values.items()}
    for item in cache["errors"]:
        if not isinstance(item, dict) or item.get("id") not in indexes:
            raise FingerprintCacheError("缓存错误记录缺少对应站点身份")
        errors[indexes[item["id"]]] = str(item.get("error", "未抓到可参与重复检测的数据"))
    return sites, fingerprints, errors


def validate_cache_freshness(cache: dict, max_age_hours: float = 24.0) -> None:
    if max_age_hours <= 0:
        raise FingerprintCacheError("正式判重缓存有效期必须为正数")
    stamp = cache.get("updated_at")
    try:
        updated = time.mktime(time.strptime(stamp, "%Y-%m-%d %H:%M:%S"))
    except (TypeError, ValueError, OverflowError) as exc:
        raise FingerprintCacheError("正式判重缓存缺少有效更新时间") from exc
    age = time.time() - updated
    if age < -300 or age > max_age_hours * 3600:
        raise FingerprintCacheError("正式判重缓存已过期或更新时间在未来")


def write_history_fingerprints(path: Path, sites: list[Site], fingerprints: dict[int, dict[int, str]],
                               errors: dict[int, str], period: int, periods: int,
                               lock_timeout: float = 30.0) -> None:
    """Never borrow another site's URL fingerprint or conceal a failed current issue."""
    with exclusive_path_lock(path, timeout=lock_timeout):
        cache = load_recent_cache(path)
        validate_recent_cache_identity(cache, sites)
        outcomes = {}
        for index, site in enumerate(sites):
            values = fingerprints.get(index, {}).get(period)
            success = values is not None and index not in errors
            outcomes[index] = (site, f"{values} {site.name}" if success else None,
                               errors.get(index, "当期没有有效历史行"), None,
                               values.split(",") if success else [], None)
        payload = json.loads(build_recent_cache_payload(cache, sites, outcomes, period, periods, False))
        base = payload["base_period"]
        for index, item in enumerate(payload["sites"]):
            if index in errors:
                continue
            fresh = fingerprints.get(index, {})
            for issue, value in fresh.items():
                if type(issue) is not int or not base - periods < issue <= base or not isinstance(value, str):
                    raise FingerprintCacheError(f"非法历史指纹期数: {sites[index].site_id}")
                values = value.split(",")
                if (len(values) != sites[index].value_count or len(set(values)) != len(values)
                        or not all(is_valid_sum_value(v) for v in values)):
                    raise FingerprintCacheError(f"非法历史指纹合数: {sites[index].site_id}")
                item["fingerprint"][str(issue)] = value
            item["fingerprint"] = trim_fingerprint(item["fingerprint"], periods, base)
        validate_recent_cache_identity(payload, sites)
        write_text_atomic_unlocked(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n", "utf-8")
