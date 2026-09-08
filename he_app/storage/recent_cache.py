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
    if not isinstance(data.get("base_period", 0), int) or not isinstance(data.get("periods", 10), int):
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
    value_count = item.get("value_count", 1)
    if type(value_count) is not int or value_count not in {1, 2}:
        raise FingerprintCacheError(f"指纹缓存第{index}个站点 value_count 必须为1或2")
    top_period_exception = item.get("top_period_exception")
    if top_period_exception is not None and (
        type(top_period_exception) is not int or top_period_exception < 1
    ):
        raise FingerprintCacheError(f"指纹缓存第{index}个站点 top_period_exception 非法")
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
        top_period_exception,
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

    if allow_configured_subset:
        cached_by_id = {site.site_id: site for site in cached_sites}
        missing_or_changed = [
            site.site_id for site in sites if cached_by_id.get(site.site_id) != site
        ]
        if not missing_or_changed:
            return
        detail = missing_or_changed[0]
    else:
        if cached_sites == sites:
            return
        mismatch = next(
            (
                index
                for index, (cached, configured) in enumerate(
                    zip(cached_sites, sites, strict=False), start=1
                )
                if cached != configured
            ),
            min(len(cached_sites), len(sites)) + 1,
        )
        detail = f"第{mismatch}条"
    raise FingerprintCacheError(f"指纹缓存与当前配置的数量、顺序或站点身份不一致: {detail}")


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
        if site.top_period_exception is not None:
            cache_item["top_period_exception"] = site.top_period_exception
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
            if trimmed:
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
