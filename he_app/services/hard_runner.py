from __future__ import annotations

import argparse
import re
import sys
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from he_app.config.settings import (
    DEFAULT_DUPLICATE_FINGERPRINT_CACHE,
    DEFAULT_FAILURE_OUTPUT_DIR,
    DEFAULT_OUTPUT_DIR,
)
from he_app.config.sites import load_sites
from he_app.domain.models import Site
from he_app.domain.periods import CyclePolicy, PeriodContext, PeriodKey, deserialize_period_mapping
from he_app.services.live_validation import LiveSiteResult, run_live_validation
from he_app.services.runner import _normalize_legacy_failures, build_arg_parser as build_legacy_arg_parser
from he_app.storage.atomic_write import commit_text_transaction_unlocked, exclusive_path_lock
from he_app.storage.cycle_cache import (
    DEFAULT_CYCLE_CACHE,
    CycleCacheState,
    load_cycle_cache,
    merge_cycle_fingerprints,
    write_cycle_cache,
)
from he_app.storage.recent_cache import (
    cache_update_allowed,
    record_recent_cache_failures,
    update_recent_cache_from_outcomes,
)
from he_app.storage.reports import (
    FAILURE_SITE_ID_RE,
    build_current_only_results,
    build_success_output_lines,
    format_failure_output,
    merge_failure_output,
    merge_success_output_lines,
)


Outcome = tuple[Site, str | None, str, str | None, list[str], str | None]


def _default_cycle() -> int:
    return datetime.now(ZoneInfo("Asia/Tokyo")).year


def build_arg_parser() -> argparse.ArgumentParser:
    parser = build_legacy_arg_parser()
    parser.description = (
        "抓取绝杀一合；DNS、TLS、浏览器启动、页面加载和解析均位于可强杀子进程内。"
    )
    parser.add_argument("--cycle", type=int, default=_default_cycle())
    parser.add_argument("--cycle-mode", choices=("year", "fixed"), default="year")
    parser.add_argument("--cycle-length", type=int, default=None)
    parser.add_argument("--hard-timeout", type=float, default=75.0)
    parser.add_argument("--cycle-cache", default=DEFAULT_CYCLE_CACHE)
    return parser


def _selected_sites_for_retry(
    sites: list[Site],
    fail_path: Path,
    period: int,
) -> list[Site]:
    failure_text = fail_path.read_text(encoding="utf-8-sig") if fail_path.exists() else ""
    failure_text = _normalize_legacy_failures(failure_text, sites)
    period_values = {int(value) for value in re.findall(r"期数:\s*(\d+)", failure_text)}
    if not period_values:
        raise SystemExit("失败TXT格式无有效期数记录")
    if period_values != {period}:
        raise SystemExit(
            f"失败TXT期数与目标期不一致: 记录={sorted(period_values)} 目标={period}"
        )
    failed_ids = set(FAILURE_SITE_ID_RE.findall(failure_text))
    if not failed_ids:
        return []
    selected = [site for site in sites if site.site_id in failed_ids]
    missing = failed_ids - {site.site_id for site in selected}
    if missing:
        raise SystemExit(f"失败TXT中的站点ID不在当前配置: {', '.join(sorted(missing))}")
    return selected


def _result_to_outcome(site: Site, result: LiveSiteResult, period: int) -> Outcome:
    if not result.current_ok or not result.current_value:
        detail = result.error or "现场验证没有得到指定期结果"
        return (
            site,
            None,
            detail,
            None,
            [],
            result.error_category or "现场验证失败",
        )
    values = [item for item in result.current_value.split(",") if item]
    sources = " / ".join(result.source_urls[:3]) or site.url
    detail = (
        f"{period}期 已验证来源={sources} 适配器={result.adapter} "
        f"历史={result.history_count}/{result.history_required}"
    )
    return (
        site,
        f"{result.current_value} {site.name}",
        detail,
        None,
        values,
        None,
    )


def _live_cycle_fingerprints(
    selected_sites: list[Site],
    selected_results: list[LiveSiteResult],
    all_sites: list[Site],
    context: PeriodContext,
) -> tuple[dict[int, dict[PeriodKey, str]], dict[int, str]]:
    full_index = {site.site_id: index for index, site in enumerate(all_sites)}
    fresh: dict[int, dict[PeriodKey, str]] = {}
    errors: dict[int, str] = {}
    for site, result in zip(selected_sites, selected_results, strict=True):
        index = full_index[site.site_id]
        if not result.current_ok:
            errors[index] = result.error or result.error_category or "现场验证失败"
            continue
        try:
            parsed = deserialize_period_mapping(result.fingerprint, context=context)
        except ValueError as exc:
            errors[index] = f"现场历史指纹无效: {exc}"
            continue
        if context.current not in parsed:
            errors[index] = "现场结果缺少指定当期"
            continue
        fresh[index] = parsed
    return fresh, errors


def run(args: argparse.Namespace) -> int:
    if args.period < 1:
        raise SystemExit("--period 必须为正整数")
    if args.timeout < 1:
        raise SystemExit("--timeout 必须为正整数")
    if args.hard_timeout <= 0:
        raise SystemExit("--hard-timeout 必须大于0")
    if args.cycle_mode == "fixed" and args.cycle_length is None:
        raise SystemExit("fixed cycle mode requires --cycle-length")

    policy = CyclePolicy(args.cycle_mode, args.cycle_length)
    context = PeriodContext(PeriodKey(args.cycle, args.period), policy, 10)
    success_path = (
        Path(args.success).resolve()
        if args.success
        else (Path(DEFAULT_OUTPUT_DIR) / f"{args.period}期-合.txt").resolve()
    )
    fail_path = (
        Path(args.fail).resolve()
        if args.fail
        else (Path(DEFAULT_FAILURE_OUTPUT_DIR) / f"{args.period}期-合-失败.txt").resolve()
    )
    if success_path == fail_path:
        raise SystemExit("成功文件和失败文件不能是同一路径")

    sites_path = Path(args.sites).resolve()
    all_sites = load_sites(sites_path)
    selected_sites = list(all_sites)
    if args.retry_failures:
        selected_sites = _selected_sites_for_retry(all_sites, fail_path, args.period)
        if not selected_sites:
            print(f"[INFO] 未找到 {args.period}期失败站点，未执行抓取")
            return 0
        args.append_success = True
        args.preserve_unconfigured_cache_sites = True

    print(
        f"[INFO] 硬隔离抓取: {len(selected_sites)} 个，workers={max(1, args.workers)} "
        f"hard_timeout={args.hard_timeout:g}s"
    )
    live_results = run_live_validation(
        selected_sites,
        context,
        workers=max(1, args.workers),
        timeout=args.timeout,
        hard_timeout=args.hard_timeout,
    )
    outcomes: dict[int, Outcome] = {
        index: _result_to_outcome(site, result, args.period)
        for index, (site, result) in enumerate(zip(selected_sites, live_results, strict=True))
    }
    success_count = sum(1 for outcome in outcomes.values() if outcome[1])
    total_count = len(selected_sites)
    success_rate = success_count / total_count * 100.0 if total_count else 0.0

    success_lines, fail_lines, ranking_values, failure_categories = build_current_only_results(
        selected_sites,
        outcomes,
        args.period,
    )
    with ExitStack() as locks:
        for path in sorted({success_path, fail_path}, key=lambda item: str(item).casefold()):
            locks.enter_context(exclusive_path_lock(path))
        existing_success = success_path.read_text(encoding="utf-8-sig") if success_path.exists() else ""
        success_output = (
            merge_success_output_lines(existing_success, success_lines)
            if args.append_success
            else build_success_output_lines(success_lines, ranking_values)
        )
        success_text = "\n".join(success_output) + ("\n" if success_output else "")
        existing_failure = fail_path.read_text(encoding="utf-8-sig") if fail_path.exists() else ""
        if args.append_success:
            successful_ids = {
                site.site_id
                for site, outcome in zip(selected_sites, outcomes.values(), strict=True)
                if outcome[1]
            }
            failure_text = merge_failure_output(existing_failure, fail_lines, successful_ids)
        else:
            failure_text = format_failure_output(fail_lines, failure_categories)
        changes = {
            fail_path: (failure_text, "utf-8-sig") if failure_text else None,
            success_path: (success_text, "utf-8-sig"),
        }
        commit_text_transaction_unlocked(changes)

    cache_error: Exception | None = None
    if not args.no_fingerprint_cache_sync:
        try:
            allowed = bool(args.retry_failures) or cache_update_allowed(success_count, total_count)
            if allowed:
                update_recent_cache_from_outcomes(
                    Path(args.fingerprint_cache).resolve(),
                    selected_sites,
                    outcomes,
                    args.period,
                    10,
                    bool(args.preserve_unconfigured_cache_sites),
                )
            else:
                record_recent_cache_failures(
                    Path(args.fingerprint_cache).resolve(),
                    selected_sites,
                    outcomes,
                    args.period,
                )

            cycle_path = Path(args.cycle_cache).resolve()
            cycle_state = load_cycle_cache(
                cycle_path,
                context=context,
                sites=all_sites,
                legacy_path=Path(DEFAULT_DUPLICATE_FINGERPRINT_CACHE).resolve(),
            )
            fresh, cycle_errors = _live_cycle_fingerprints(
                selected_sites,
                live_results,
                all_sites,
                context,
            )
            if not allowed:
                fresh = {}
            merged, merged_errors = merge_cycle_fingerprints(
                cycle_state,
                fresh,
                cycle_errors,
            )
            cycle_state = CycleCacheState(context, all_sites, merged, merged_errors)
            write_cycle_cache(
                cycle_path,
                cycle_state,
                fingerprints=merged,
                errors=merged_errors,
            )
        except Exception as exc:
            cache_error = exc
            print(f"[ERROR] 缓存更新未完成: {type(exc).__name__}: {exc}", file=sys.stderr)

    print(f"[INFO] 成功率: {success_count}/{total_count} ({success_rate:.2f}%)")
    print(f"成功 {len(success_lines)} 个，保存到: {success_path}")
    print(f"失败 {len(fail_lines)} 个，保存到: {fail_path}" if fail_lines else "失败 0 个")
    return 1 if cache_error is not None else 0


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(run(build_arg_parser().parse_args()))


__all__ = ["build_arg_parser", "main", "run"]
