import json
import time
from pathlib import Path

from he_app.domain.errors import FingerprintCacheError
from he_app.domain.models import Site
from he_app.storage.atomic_write import exclusive_path_lock, write_text_atomic_unlocked


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
        for key in (str(item.get("id", "")).strip(), str(item.get("url", "")).strip()):
            if key:
                existing_by_key[key] = item

    next_sites: list[dict] = []
    next_errors: list[dict] = []
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    seen_keys: set[str] = set()
    for index, site in enumerate(sites):
        previous = existing_by_key.get(cache_site_key(site)) or existing_by_key.get(site.url) or {}
        seen_keys.update((cache_site_key(site), site.url))
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
        if fingerprint:
            next_sites.append(
                {
                    "id": cache_site_key(site),
                    "name": site.name,
                    "url": site.url,
                    "pick": site.pick,
                    "browser": site.browser,
                    "click_first": site.click_first,
                    "fingerprint": fingerprint,
                    "updated_at": now,
                }
            )
        if not result or not fingerprint:
            next_errors.append(_failure_entry(site, (site, result, detail, error, rank_values, previous_reason), period, now))

    if preserve_unconfigured_sites:
        for item in cache.get("sites", []):
            if not isinstance(item, dict):
                continue
            item_id = str(item.get("id", "")).strip()
            item_url = str(item.get("url", "")).strip()
            if item_id in seen_keys or item_url in seen_keys:
                continue
            fingerprint = item.get("fingerprint", {}) if isinstance(item.get("fingerprint"), dict) else {}
            trimmed = trim_fingerprint(
                {str(key): str(value) for key, value in fingerprint.items()}, periods, base_period
            )
            if trimmed:
                kept = dict(item)
                kept["fingerprint"] = trimmed
                next_sites.append(kept)

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
    configured_keys = {
        key
        for site in sites
        for key in (cache_site_key(site), site.url)
        if key
    }
    failed_sites = {
        cache_site_key(site): (site, outcomes.get(index, (site, None, "", "未执行", [], None)))
        for index, site in enumerate(sites)
        if not outcomes.get(index, (site, None, "", "未执行", [], None))[1]
    }
    failed_sites_by_url = {
        site.url: value for site, value in failed_sites.values() if site.url
    }

    kept_sites: list[dict] = []
    for item in cache.get("sites", []):
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("id", "")).strip()
        item_url = str(item.get("url", "")).strip()
        failed = failed_sites.get(item_id) or failed_sites_by_url.get(item_url)
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
            or str(item.get("url", "")).strip() in configured_keys
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


# Compatibility names used by the existing command-line programs during migration.
load_duplicate_fingerprint_cache = load_recent_cache
trim_duplicate_fingerprint = trim_fingerprint
update_duplicate_fingerprint_cache_from_outcomes = update_recent_cache_from_outcomes
_update_duplicate_fingerprint_cache_from_outcomes_locked = _update_recent_cache_locked
