import argparse
import re
import sys
import time
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import Lock

import requests
from requests.packages.urllib3.exceptions import InsecureRequestWarning

from he_app.config.settings import (
    DEFAULT_DUPLICATE_FINGERPRINT_CACHE,
    DEFAULT_FAILURE_OUTPUT_DIR,
    DEFAULT_OUTPUT_DIR,
    SITES_FILE,
)
from he_app.config.sites import load_sites
from he_app.domain.models import Site
from he_app.fetch.browser import BrowserPool, default_browser_pool_size
from he_app.fetch.http import build_host_locks
from he_app.observability.progress import (
    build_slow_site_lines,
    format_completion_progress,
    format_progress_prefix,
)
from he_app.services.crawler import (
    print_outcome,
    scrape_parallel_site,
)
from he_app.storage.atomic_write import (
    commit_text_transaction,
)
from he_app.storage.recent_cache import (
    cache_update_allowed,
    record_recent_cache_failures,
    update_recent_cache_from_outcomes,
)
from he_app.storage.reports import (
    build_current_only_results,
    build_success_output_lines,
    format_failure_output,
    merge_failure_output,
    merge_success_output_lines,
)
from he_app.storage.reports import FAILURE_SITE_ID_RE


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="抓取绝杀一合的合数，并分别输出成功和失败 txt。")
    parser.add_argument("--period", type=int, required=True, help="要抓取的期数，例如 236")
    parser.add_argument("--success", default=None, help="成功结果文件，默认按期数生成")
    parser.add_argument("--fail", default=None, help="失败结果文件，默认按期数生成")
    parser.add_argument("--timeout", type=int, default=20, help="单个请求超时秒数")
    parser.add_argument("--workers", type=int, default=8, help="全部站点并发数量，默认 8")
    parser.add_argument("--retries", type=int, default=0, help="兼容旧命令；每个站点始终只抓一次")
    parser.add_argument("--delay", type=float, default=0.0, help="提交站点任务之间的间隔秒数")
    parser.add_argument("--show-browser", action="store_true", help="显示二次点击站点的浏览器窗口")
    parser.add_argument("--cache", help=argparse.SUPPRESS)
    parser.add_argument("--mirror-limit", type=int, default=0, help=argparse.SUPPRESS)
    parser.add_argument("--sites", default=SITES_FILE, help="站点配置 JSON，默认 sites.json")
    parser.add_argument("--cache-max-age-hours", type=float, help=argparse.SUPPRESS)
    parser.add_argument("--fingerprint-cache", default=DEFAULT_DUPLICATE_FINGERPRINT_CACHE, help=argparse.SUPPRESS)
    parser.add_argument("--no-fingerprint-cache-sync", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--append-success",
        action="store_true",
        help="失败站修复模式：保留已有成功结果，仅去重追加本次修复成功站点",
    )
    parser.add_argument(
        "--preserve-unconfigured-cache-sites",
        action="store_true",
        help="局部同步时显式保留指纹缓存中未包含在本次站点配置里的旧站",
    )
    parser.add_argument("--retry-failures", action="store_true", help="只重抓当前期失败TXT中的站点")
    return parser


def consume_site_future(future, site: Site, fallback_index: int):
    try:
        return future.result()
    except Exception as exc:
        return fallback_index, site, None, "", f"{type(exc).__name__}: {exc}", [], None, 0.0


def run(args: argparse.Namespace) -> int:
    if args.period < 1:
        raise SystemExit("--period 必须为正整数")
    if args.timeout < 1:
        raise SystemExit("--timeout 必须为正整数")
    success_output_dir = Path(DEFAULT_OUTPUT_DIR)
    failure_output_dir = Path(DEFAULT_FAILURE_OUTPUT_DIR)
    success_path = (
        Path(args.success).resolve()
        if args.success
        else (success_output_dir / f"{args.period}期-合.txt").resolve()
    )
    fail_path = (
        Path(args.fail).resolve()
        if args.fail
        else (failure_output_dir / f"{args.period}期-合-失败.txt").resolve()
    )
    sites_path = Path(args.sites).resolve()
    sites = load_sites(sites_path)
    if args.retry_failures:
        failure_text = fail_path.read_text(encoding="utf-8-sig") if fail_path.exists() else ""
        recorded_periods = {
            int(value) for value in re.findall(r"期数:\s*(\d+)", failure_text)
        }
        if recorded_periods and recorded_periods != {args.period}:
            raise SystemExit(
                f"失败TXT期数与目标期不一致: 记录={sorted(recorded_periods)} 目标={args.period}"
            )
        failed_ids = set(FAILURE_SITE_ID_RE.findall(failure_text))
        if not failed_ids:
            print(f"[INFO] 未找到 {args.period}期失败站点，未执行抓取")
            return 0
        sites = [site for site in sites if site.site_id in failed_ids]
        missing_ids = failed_ids - {site.site_id for site in sites}
        if missing_ids:
            raise SystemExit(f"失败TXT中的站点ID不在当前配置: {', '.join(sorted(missing_ids))}")
        args.append_success = True
        args.preserve_unconfigured_cache_sites = True
    outcomes: dict[int, tuple[Site, str | None, str, str | None, list[str], str | None]] = {}
    all_sites = list(enumerate(sites))
    host_locks = build_host_locks(sites)
    workers = max(1, args.workers)
    fingerprint_cache_path = Path(args.fingerprint_cache).resolve()

    def run_timed_parallel_site(
        index: int,
        site: Site,
        period: int,
        timeout: int,
        show_browser: bool,
        locks: dict[str, Lock],
        pool: BrowserPool | None,
    ):
        started_at = time.perf_counter()
        outcome = scrape_parallel_site(
            index,
            site,
            period,
            timeout,
            show_browser,
            host_locks=locks,
            browser_pool=pool,
        )
        return (*outcome, time.perf_counter() - started_at)

    mode = "失败TXT定向重抓" if args.retry_failures else "全部站点并发抓取"
    print(f"[INFO] {mode}: {len(all_sites)} 个，workers={workers}")
    completed_count = success_count = fail_count = 0
    site_timings: list[tuple[str, float, bool]] = []
    browser_pool_size = default_browser_pool_size(sites, workers)
    browser_pool = BrowserPool(browser_pool_size, headless=not args.show_browser) if browser_pool_size else None
    try:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_map = {}
            total_sites = len(all_sites)
            for index, site in all_sites:
                print(f"{format_progress_prefix(index, total_sites)} [START] {site.name} ({site.pick})")
                future = executor.submit(
                    run_timed_parallel_site,
                    index,
                    site,
                    args.period,
                    args.timeout,
                    args.show_browser,
                    host_locks,
                    browser_pool,
                )
                future_map[future] = (index, site)
                if args.delay > 0:
                    time.sleep(args.delay)

            for future in as_completed(future_map):
                fallback_index, fallback_site = future_map[future]
                index, site, result, detail, error, rank_values, previous_reason, elapsed = consume_site_future(
                    future, fallback_site, fallback_index
                )
                outcome = (site, result, detail, error, rank_values, previous_reason)
                site, result, detail, error, rank_values, previous_reason = outcome
                outcomes[index] = outcome
                completed_count += 1
                success_count += int(bool(result))
                fail_count += int(not result)
                site_timings.append((site.name, elapsed, bool(result)))
                print_outcome(site, result, detail, error, format_progress_prefix(index, total_sites))
                print(format_completion_progress(completed_count, total_sites, success_count, fail_count, site.name, elapsed))
    finally:
        if browser_pool is not None:
            browser_pool.close()

    for line in build_slow_site_lines(site_timings):
        print(line)

    total_sites = len(sites)
    success_rate = (success_count / total_sites * 100.0) if total_sites else 0.0
    print(f"[INFO] 成功率: {success_count}/{total_sites} ({success_rate:.2f}%)")

    success_lines, fail_lines, ranking_values, failure_categories = build_current_only_results(
        sites, outcomes, args.period
    )
    existing_success = ""
    if args.append_success:
        existing_success = (
            success_path.read_text(encoding="utf-8-sig") if success_path.exists() else ""
        )
        success_output = merge_success_output_lines(existing_success, success_lines)
    else:
        success_output = build_success_output_lines(success_lines, ranking_values)
    success_text = "\n".join(success_output) + ("\n" if success_output else "")
    if args.append_success:
        existing_failure = fail_path.read_text(encoding="utf-8-sig") if fail_path.exists() else ""
        successful_site_ids = {
            site.site_id
            for site, result, _detail, _error, _rank_values, _previous_reason in outcomes.values()
            if result
        }
        failure_output = merge_failure_output(
            existing_failure,
            fail_lines,
            successful_site_ids,
        )
    else:
        failure_output = format_failure_output(fail_lines, failure_categories)
    changes: dict[Path, tuple[str, str] | None] = {
        fail_path: (failure_output, "utf-8-sig") if failure_output else None,
    }
    success_unchanged = (
        args.append_success
        and success_text.splitlines() == existing_success.splitlines()
    )
    if not success_unchanged:
        changes[success_path] = (success_text, "utf-8-sig")
    commit_text_transaction(changes)

    cache_update_error: Exception | None = None
    cache_updated = False
    if not args.no_fingerprint_cache_sync:
        try:
            if args.retry_failures or cache_update_allowed(success_count, total_sites):
                update_recent_cache_from_outcomes(
                    fingerprint_cache_path,
                    sites,
                    outcomes,
                    args.period,
                    10,
                    args.preserve_unconfigured_cache_sites,
                )
                cache_updated = True
            else:
                record_recent_cache_failures(
                    fingerprint_cache_path,
                    sites,
                    outcomes,
                    args.period,
                )
        except Exception as exc:
            cache_update_error = exc
            print(f"[ERROR] 缓存更新未完成: {type(exc).__name__}: {exc}", file=sys.stderr)

    print(f"\n成功 {len(success_lines)} 个，保存到: {success_path}")
    print(f"失败 {len(fail_lines)} 个，保存到: {fail_path}" if fail_lines else "失败 0 个，未生成失败文件")
    print(f"站点配置: {sites_path}")
    if cache_update_error is not None:
        return 1
    if cache_updated:
        print(f"重复检测指纹备份已同步: {fingerprint_cache_path}")
    elif not args.no_fingerprint_cache_sync:
        print(
            f"缓存未推进：成功率 {success_rate:.2f}% 不超过 85%；"
            f"失败站点已标记到 {fingerprint_cache_path}"
        )
    return 0


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    warnings.simplefilter("ignore", InsecureRequestWarning)
    requests.packages.urllib3.disable_warnings()
    raise SystemExit(run(build_arg_parser().parse_args()))
