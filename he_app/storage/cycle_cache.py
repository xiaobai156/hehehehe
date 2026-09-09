from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from he_app.domain.models import Site
from he_app.domain.periods import (
    CyclePolicy,
    PeriodContext,
    PeriodKey,
    deserialize_period_mapping,
    infer_cycle_from_timestamp,
    serialize_period_mapping,
    trim_period_mapping,
)
from he_app.storage.atomic_write import exclusive_path_lock, write_text_atomic_unlocked
from he_app.validation.period import is_valid_sum_value


SCHEMA_VERSION = 2
DEFAULT_CYCLE_CACHE = "outputs/recent_10_cycle_cache.json"


class CycleCacheError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class CycleCacheState:
    context: PeriodContext
    sites: list[Site]
    fingerprints: dict[int, dict[PeriodKey, str]]
    errors: dict[int, str]


def _site_value_count(site: Site) -> int:
    value = getattr(site, "value_count", 1)
    return value if value in {1, 2} else 1


def _valid_value(site: Site, value: str) -> bool:
    values = [item.strip() for item in str(value).split(",") if item.strip()]
    return (
        len(values) == _site_value_count(site)
        and len(set(values)) == len(values)
        and all(is_valid_sum_value(item) for item in values)
    )


def site_identity(site: Site) -> tuple[object, ...]:
    return (
        site.name,
        site.url,
        site.pick,
        bool(site.browser),
        bool(site.click_first),
        _site_value_count(site),
    )


def _site_from_item(item: Mapping[str, object]) -> Site:
    try:
        return Site(
            name=str(item["name"]).strip(),
            url=str(item["url"]).strip(),
            pick=str(item["pick"]).strip(),
            browser=bool(item["browser"]),
            click_first=bool(item["click_first"]),
            site_id=str(item["id"]).strip(),
            value_count=int(item.get("value_count", 1)),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise CycleCacheError(f"invalid cached site identity: {item!r}") from exc


def _empty_state(context: PeriodContext, sites: list[Site]) -> CycleCacheState:
    return CycleCacheState(
        context=context,
        sites=list(sites),
        fingerprints={index: {} for index in range(len(sites))},
        errors={},
    )


def _policy_from_payload(payload: Mapping[str, object], fallback: CyclePolicy) -> CyclePolicy:
    mode = str(payload.get("cycle_mode", fallback.mode))
    raw_length = payload.get("cycle_length", fallback.fixed_length)
    fixed_length = None if raw_length in (None, "") else int(raw_length)
    return CyclePolicy(mode=mode, fixed_length=fixed_length)


def _migrate_legacy_payload(
    payload: Mapping[str, object],
    *,
    requested_context: PeriodContext,
    configured_sites: list[Site],
) -> CycleCacheState:
    base_number = payload.get("base_period")
    if isinstance(base_number, bool) or not isinstance(base_number, int) or base_number < 1:
        raise CycleCacheError("legacy cache has no valid base_period")
    legacy_cycle = infer_cycle_from_timestamp(
        payload.get("updated_at"),
        requested_context.current.cycle,
    )
    legacy_context = PeriodContext(
        current=PeriodKey(legacy_cycle, base_number),
        policy=requested_context.policy,
        periods=int(payload.get("periods", requested_context.periods) or requested_context.periods),
    )
    raw_sites = payload.get("sites", [])
    if not isinstance(raw_sites, list):
        raise CycleCacheError("legacy cache sites must be a list")
    by_id = {
        str(item.get("id", "")).strip(): item
        for item in raw_sites
        if isinstance(item, dict) and str(item.get("id", "")).strip()
    }
    state = _empty_state(requested_context, configured_sites)
    fingerprints: dict[int, dict[PeriodKey, str]] = {}
    errors: dict[int, str] = {}
    for index, site in enumerate(configured_sites):
        item = by_id.get(site.site_id)
        if item is None:
            fingerprints[index] = {}
            errors[index] = "legacy cache has no site entry"
            continue
        cached_site = _site_from_item(item)
        if site_identity(cached_site) != site_identity(site):
            raise CycleCacheError(f"legacy cache identity mismatch: {site.site_id}")
        raw_fingerprint = item.get("fingerprint", {})
        if not isinstance(raw_fingerprint, dict):
            raise CycleCacheError(f"legacy fingerprint is not an object: {site.site_id}")
        parsed = deserialize_period_mapping(
            raw_fingerprint,
            context=legacy_context,
            legacy_cycle=legacy_cycle,
        )
        fingerprints[index] = {
            key: value
            for key, value in parsed.items()
            if key in set(requested_context.window) and _valid_value(site, value)
        }
    raw_errors = payload.get("errors", [])
    if isinstance(raw_errors, list):
        index_by_id = {site.site_id: index for index, site in enumerate(configured_sites)}
        for item in raw_errors:
            if not isinstance(item, dict):
                continue
            site_index = index_by_id.get(str(item.get("id", "")).strip())
            if site_index is not None:
                errors[site_index] = str(item.get("error", "legacy cache error"))
    return CycleCacheState(requested_context, list(configured_sites), fingerprints, errors)


def load_cycle_cache(
    path: Path,
    *,
    context: PeriodContext,
    sites: list[Site],
    legacy_path: Path | None = None,
) -> CycleCacheState:
    if not path.exists():
        if legacy_path is not None and legacy_path.exists():
            try:
                payload = json.loads(legacy_path.read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError) as exc:
                raise CycleCacheError(f"legacy cache cannot be read: {legacy_path}: {exc}") from exc
            if not isinstance(payload, dict):
                raise CycleCacheError("legacy cache root must be an object")
            return _migrate_legacy_payload(
                payload,
                requested_context=context,
                configured_sites=sites,
            )
        return _empty_state(context, sites)

    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CycleCacheError(f"cycle cache cannot be read: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise CycleCacheError("cycle cache root must be an object")
    if payload.get("schema_version") != SCHEMA_VERSION:
        return _migrate_legacy_payload(
            payload,
            requested_context=context,
            configured_sites=sites,
        )

    cached_policy = _policy_from_payload(payload, context.policy)
    if cached_policy != context.policy:
        raise CycleCacheError("cycle policy differs from the current command")
    raw_sites = payload.get("sites", [])
    if not isinstance(raw_sites, list):
        raise CycleCacheError("cycle cache sites must be a list")
    by_id: dict[str, Mapping[str, object]] = {}
    for item in raw_sites:
        if not isinstance(item, dict):
            raise CycleCacheError("cycle cache contains a non-object site")
        site_id = str(item.get("id", "")).strip()
        if not site_id or site_id in by_id:
            raise CycleCacheError(f"cycle cache contains an empty or duplicate id: {site_id!r}")
        by_id[site_id] = item

    fingerprints: dict[int, dict[PeriodKey, str]] = {}
    errors: dict[int, str] = {}
    for index, site in enumerate(sites):
        item = by_id.get(site.site_id)
        if item is None:
            fingerprints[index] = {}
            errors[index] = "cycle cache has no site entry"
            continue
        cached_site = _site_from_item(item)
        if site_identity(cached_site) != site_identity(site):
            raise CycleCacheError(f"cycle cache identity mismatch: {site.site_id}")
        raw_fingerprint = item.get("fingerprint", {})
        if not isinstance(raw_fingerprint, dict):
            raise CycleCacheError(f"cycle fingerprint is not an object: {site.site_id}")
        try:
            parsed = deserialize_period_mapping(raw_fingerprint, context=context)
        except ValueError as exc:
            raise CycleCacheError(f"invalid cycle fingerprint for {site.site_id}: {exc}") from exc
        invalid = [value for value in parsed.values() if not _valid_value(site, value)]
        if invalid:
            raise CycleCacheError(f"invalid value in cycle cache for {site.site_id}: {invalid[0]}")
        fingerprints[index] = trim_period_mapping(parsed, context)

    raw_errors = payload.get("errors", [])
    if isinstance(raw_errors, list):
        index_by_id = {site.site_id: index for index, site in enumerate(sites)}
        for item in raw_errors:
            if not isinstance(item, dict):
                continue
            index = index_by_id.get(str(item.get("id", "")).strip())
            if index is not None:
                errors[index] = str(item.get("error", "cycle cache error"))
    return CycleCacheState(context, list(sites), fingerprints, errors)


def build_cycle_cache_payload(
    state: CycleCacheState,
    *,
    fingerprints: Mapping[int, Mapping[PeriodKey, str]] | None = None,
    errors: Mapping[int, str] | None = None,
) -> str:
    active_fingerprints = fingerprints or state.fingerprints
    active_errors = errors or {}
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    site_items: list[dict[str, object]] = []
    for index, site in enumerate(state.sites):
        fingerprint = trim_period_mapping(
            dict(active_fingerprints.get(index, {})),
            state.context,
        )
        for value in fingerprint.values():
            if not _valid_value(site, value):
                raise CycleCacheError(f"refusing invalid value for {site.site_id}: {value}")
        site_items.append(
            {
                "id": site.site_id,
                "name": site.name,
                "url": site.url,
                "pick": site.pick,
                "browser": bool(site.browser),
                "click_first": bool(site.click_first),
                "value_count": _site_value_count(site),
                "fingerprint": serialize_period_mapping(
                    fingerprint,
                    context=state.context,
                ),
                "updated_at": now,
            }
        )
    error_items = [
        {
            "id": state.sites[index].site_id,
            "name": state.sites[index].name,
            "url": state.sites[index].url,
            "period": state.context.current.token(),
            "status": "失败",
            "error": error,
            "updated_at": now,
        }
        for index, error in sorted(active_errors.items())
        if 0 <= index < len(state.sites)
    ]
    payload = {
        "schema_version": SCHEMA_VERSION,
        "cycle_mode": state.context.policy.mode,
        "cycle_length": state.context.policy.fixed_length,
        "base_period": state.context.current.token(),
        "periods": state.context.periods,
        "updated_at": now,
        "sites": site_items,
        "errors": error_items,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def merge_cycle_fingerprints(
    state: CycleCacheState,
    fresh: Mapping[int, Mapping[PeriodKey, str]],
    errors: Mapping[int, str],
) -> tuple[dict[int, dict[PeriodKey, str]], dict[int, str]]:
    merged: dict[int, dict[PeriodKey, str]] = {}
    next_errors: dict[int, str] = {}
    current = state.context.current
    for index, site in enumerate(state.sites):
        fingerprint = trim_period_mapping(state.fingerprints.get(index, {}), state.context)
        current_fresh = dict(fresh.get(index, {}))
        if current_fresh:
            for key, value in current_fresh.items():
                if key not in set(state.context.window):
                    continue
                if not _valid_value(site, value):
                    raise CycleCacheError(f"fresh value is invalid for {site.site_id}: {value}")
                fingerprint[key] = value
        if index in errors:
            fingerprint.pop(current, None)
            next_errors[index] = errors[index]
        elif current not in fingerprint:
            next_errors[index] = "current period has no verified value"
        merged[index] = trim_period_mapping(fingerprint, state.context)
    return merged, next_errors


def write_cycle_cache(
    path: Path,
    state: CycleCacheState,
    *,
    fingerprints: Mapping[int, Mapping[PeriodKey, str]],
    errors: Mapping[int, str],
) -> None:
    payload = build_cycle_cache_payload(
        state,
        fingerprints=fingerprints,
        errors=errors,
    )
    with exclusive_path_lock(path):
        write_text_atomic_unlocked(path, payload, "utf-8")


__all__ = [
    "CycleCacheError",
    "CycleCacheState",
    "DEFAULT_CYCLE_CACHE",
    "SCHEMA_VERSION",
    "build_cycle_cache_payload",
    "load_cycle_cache",
    "merge_cycle_fingerprints",
    "site_identity",
    "write_cycle_cache",
]
