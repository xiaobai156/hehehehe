import argparse
import os
import re
import sys
import time
from contextlib import ExitStack
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import Lock


from he_app.config.settings import (
    DEFAULT_DUPLICATE_FINGERPRINT_CACHE,
    DEFAULT_FAILURE_OUTPUT_DIR,
    DEFAULT_OUTPUT_DIR,
    SITES_FILE,
)
from he_app.config.sites import load_sites
from he_app.domain.models import Site
from he_app.domain.periods import PeriodKey, current_tokyo_period
from he_app.fetch.browser import BrowserClient, BrowserPool, default_browser_pool_size
from he_app.fetch.http import build_host_locks
from he_app.observability.progress import (
    build_slow_site_lines,
    format_completion_progress,
    format_progress_prefix,
)
from he_app.services.document_sources import requires_browser
from he_app.services.crawler import (
    build_mirror_url_map,
    matched_period_from_reason,
    print_outcome,
    scrape_browser_site,
    scrape_http_site,
    scrape_parallel_site,
)
from he_app.services.isolation import IsolatedJobResult, run_isolated_site_jobs
from he_app.storage.atomic_write import (
    commit_text_transaction_unlocked,
    exclusive_path_lock,
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
from he_app.storage.failure_records import parse_failure_records, serialize_failure_record


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="抓取绝杀一合的合数，并分别输出成功和失败 txt。")
    parser.add_argument("--period", type=int, required=True, help="要抓取的期数，例如 236")
    parser.add_argument("--success", default=None, help="成功结果文件，默认按期数生成")
    parser.add_argument("--fail", default=None, help="失败结果文件，默认按期数生成")
    parser.add_argument("--timeout", type=int, default=20, help="单个请求超时秒数")
    parser.add_argument("--hard-timeout", type=float, default=60.0, help="单站HTTP硬超时秒数，包含DNS和进程调度")
    parser.add_argument("--browser-hard-timeout", type=float, default=120.0, help="单站浏览器硬超时秒数，包含浏览器启动")
    parser.add_argument("--cycle-year", type=int, default=None, help="期数所属周期年份；默认东京当前年份")
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
    parser.add_argument("--no-isolation", action="store_true", help=argparse.SUPPRESS)
    return parser


def consume_site_future(future, site: Site, fallback_index: int):
    try:
        return future.result()
    except Exception as exc:
        return fallback_index, site, None, "", f"{type(exc).__name__}: {exc}", [], None, 0.0


def _normalize_legacy_failures(text: str, sites: list[Site]) -> str:
    return "\n\n".join(
        serialize_failure_record(record)
        for record in parse_failure_records(text, sites)
    )


def _split_cache_outcomes_by_matched_period(
    sites: list[Site],
    outcomes: dict[int, tuple[Site, str | None, str, str | None, list[str], str | None]],
    requested_period: int,
):
    """Keep mixed requested/next-period successes accurate in the fingerprint cache.

    The first full update handles the requested issue.  A successful immediate
    next-period fallback is represented as a requested-period miss in that
    update, then written in one partial follow-up update under its real issue.
    If that follow-up fails, the cache remains fail-closed (missing data) rather
    than ever storing a next-period value under the requested issue number.
    """

    requested_outcomes = {}
    next_sites: list[Site] = []
    next_outcomes = {}
    next_period = requested_period + 1
    for index, site in enumerate(sites):
        outcome = outcomes.get(index, (site, None, "", "未执行", [], None))
        returned_site, result, detail, error, values, metadata = outcome
        if returned_site != site:
            requested_outcomes[index] = (
                site,
                None,
                "",
                "RuntimeError: 结果站点身份不一致",
                [],
                None,
            )
            continue
        matched_period = (
            matched_period_from_reason(requested_period, metadata)
            if result
            else requested_period
        )
        if result and matched_period == next_period:
            requested_outcomes[index] = (
                site,
                None,
                detail,
                None,
                [],
                f"已严格命中{next_period}期{site.pick}边界；不写入{requested_period}期缓存",
            )
            next_index = len(next_sites)
            next_sites.append(site)
            next_outcomes[next_index] = (
                site,
                result,
                detail,
                error,
                values,
                None,
            )
            continue
        requested_outcomes[index] = outcome
    return requested_outcomes, next_sites, next_outcomes, next_period


def _isolated_single_site_worker(
    index: int,
    site: Site,
    browser: BrowserClient | None,
    host_locks,
    period: int,
    timeout: int,
):
    if browser is None:
        _index, returned_site, result, detail, error, values, previous = scrape_http_site(
            index,
            site,
            period,
            timeout,
            host_locks,
            [],
        )
        return returned_site, result, detail, error, values, previous
    with __import__("he_app.fetch.http", fromlist=["create_session"]).create_session(host_locks) as session:
        result, detail, error, values, previous = scrape_browser_site(
            session,
            site,
            period,
            timeout,
            browser,
        )
    return site, result, detail, error, values, previous


def _run_threaded_site_jobs(
    all_sites: list[tuple[int, Site]],
    *,
    period: int,
    timeout: int,
    workers: int,
    show_browser: bool,
    delay: float,
    host_locks,
    mirror_url_map: dict[int, list[str]],
):
    outcomes = {}
    timings = []
    browser_pool_size = default_browser_pool_size(
        [site for _index, site in all_sites], workers
    )
    has_http = any(not requires_browser(site) for _index, site in all_sites)
    split_pools = has_http and browser_pool_size > 0 and workers > 1
    if split_pools:
        browser_pool_size = min(browser_pool_size, workers - 1)
    browser_pool = (
        BrowserPool(browser_pool_size, headless=not show_browser)
        if browser_pool_size
        else None
    )

    def timed(index, site):
        started = time.perf_counter()
        outcome = scrape_parallel_site(
            index,
            site,
            period,
            timeout,
            show_browser,
            host_locks=host_locks,
            mirror_urls=mirror_url_map.get(index, []),
            browser_pool=browser_pool,
        )
        return (*outcome, time.perf_counter() - started)

    try:
        with ExitStack() as executors:
            http_workers = workers - browser_pool_size if split_pools else workers
            executor = executors.enter_context(
                ThreadPoolExecutor(max_workers=http_workers)
            )
            browser_executor = (
                executors.enter_context(
                    ThreadPoolExecutor(max_workers=browser_pool_size)
                )
                if split_pools
                else executor
            )
            future_map = {}
            total = len(all_sites)
            for index, site in all_sites:
                print(
                    f"{format_progress_prefix(index, total)} [START] "
                    f"{site.name} ({site.pick})"
                )
                selected = browser_executor if requires_browser(site) else executor
                future_map[selected.submit(timed, index, site)] = (index, site)
                if delay > 0:
                    time.sleep(delay)
            for future in as_completed(future_map):
                fallback_index, fallback_site = future_map[future]
                (
                    index,
                    site,
                    result,
                    detail,
                    error,
                    values,
                    previous,
                    elapsed,
                ) = consume_site_future(future, fallback_site, fallback_index)
                outcomes[index] = (site, result, detail, error, values, previous)
                timings.append((site.name, elapsed, bool(result)))
    finally:
        if browser_pool is not None:
            browser_pool.close()
    return outcomes, timings


def _run_hard_isolated_site_jobs(
    all_sites: list[tuple[int, Site]],
    *,
    period: int,
    timeout: int,
    workers: int,
    show_browser: bool,
    delay: float,
    hard_timeout: float,
    browser_hard_timeout: float,
    host_locks,
):
    outcomes = {}
    timings = []
    total = len(all_sites)

    def on_start(index: int, site: Site) -> None:
        print(
            f"{format_progress_prefix(index, total)} [START] "
            f"{site.name} ({site.pick})"
        )

    def on_result(job: IsolatedJobResult) -> None:
        if job.error is not None:
            outcome = (job.site, None, "", job.error, [], None)
        else:
            outcome = job.value
            if (
                not isinstance(outcome, tuple)
                or len(outcome) != 6
                or outcome[0] != job.site
            ):
                outcome = (
                    job.site,
                    None,
                    "",
                    "RuntimeError: 隔离任务返回格式或站点身份错误",
                    [],
                    None,
                )
        outcomes[job.index] = outcome
        timings.append((job.site.name, job.elapsed, bool(outcome[1])))

    run_isolated_site_jobs(
        all_sites,
        workers=workers,
        worker_callable=_isolated_single_site_worker,
        worker_args=(period, timeout),
        needs_browser=requires_browser,
        hard_timeout=hard_timeout,
        browser_hard_timeout=browser_hard_timeout,
        browser_limit=3,
        headless=not show_browser,
        host_keys=host_locks,
        assignment_delay=max(0.0, delay),
        on_start=on_start,
        on_result=on_result,
    )
    return outcomes, timings


def run(args: argparse.Namespace) -> int:
    defaults = build_arg_parser().parse_args(["--period", str(args.period)])
    args = argparse.Namespace(**(vars(defaults) | vars(args)))
    if args.period < 1:
        raise SystemExit("--period 必须为正整数")
    if args.timeout < 1:
        raise SystemExit("--timeout 必须为正整数")
    if args.hard_timeout <= 0 or args.browser_hard_timeout <= 0:
        raise SystemExit("硬超时必须为正数")
    cycle_year = args.cycle_year or current_tokyo_period().cycle_year
    try:
        period_key = PeriodKey(cycle_year, args.period)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
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
    configured_sites = list(sites)
    fingerprint_cache_path = Path(args.fingerprint_cache).resolve()
    active_paths = [success_path, fail_path]
    if not args.no_fingerprint_cache_sync:
        active_paths.append(fingerprint_cache_path)
    if len(set(active_paths)) != len(active_paths) or sites_path in active_paths:
        raise SystemExit("输出路径必须互不相同，且不能覆盖站点配置")
    if (
        sites_path != Path(SITES_FILE).resolve()
        and fingerprint_cache_path
        == Path(DEFAULT_DUPLICATE_FINGERPRINT_CACHE).resolve()
        and not args.no_fingerprint_cache_sync
    ):
        raise SystemExit(
            "自定义站点配置禁止写入正式指纹缓存；"
            "请使用 --no-fingerprint-cache-sync 或隔离缓存路径"
        )
    if args.retry_failures:
        failure_text = (
            fail_path.read_text(encoding="utf-8-sig") if fail_path.exists() else ""
        )
        failure_text = _normalize_legacy_failures(failure_text, sites)
        if not failure_text:
            print(f"[INFO] 未找到 {args.period}期失败站点，未执行抓取")
            return 0
        period_values = re.findall(r"期数:\s*(\d+)", failure_text)
        if not period_values:
            raise SystemExit("失败TXT格式无有效期数记录")
        recorded_periods = {int(value) for value in period_values}
        if recorded_periods and recorded_periods != {args.period}:
            raise SystemExit(
                f"失败TXT期数与目标期不一致: "
                f"记录={sorted(recorded_periods)} 目标={args.period}"
            )
        failed_ids = set(FAILURE_SITE_ID_RE.findall(failure_text))
        if not failed_ids:
            print(f"[INFO] 未找到 {args.period}期失败站点，未执行抓取")
            return 0
        sites = [site for site in sites if site.site_id in failed_ids]
        missing_ids = failed_ids - {site.site_id for site in sites}
        if missing_ids:
            raise SystemExit(
                f"失败TXT中的站点ID不在当前配置: {', '.join(sorted(missing_ids))}"
            )
        args.append_success = True
        args.preserve_unconfigured_cache_sites = True
    all_sites = list(enumerate(sites))
    mirror_url_map = build_mirror_url_map(sites, args.mirror_limit)
    host_locks = build_host_locks(sites, mirror_url_map)
    workers = max(1, args.workers)
    fingerprint_cache_path = Path(args.fingerprint_cache).resolve()

    mode = "失败TXT定向重抓" if args.retry_failures else "全部站点抓取"
    isolation_label = "进程硬隔离" if not args.no_isolation else "兼容线程模式"
    print(
        f"[INFO] {mode}: {len(all_sites)} 个，workers={workers}，"
        f"周期={period_key.cache_key}，模式={isolation_label}"
    )
    if args.no_isolation:
        outcomes, site_timings = _run_threaded_site_jobs(
            all_sites,
            period=args.period,
            timeout=args.timeout,
            workers=workers,
            show_browser=args.show_browser,
            delay=args.delay,
            host_locks=host_locks,
            mirror_url_map=mirror_url_map,
        )
    else:
        outcomes, site_timings = _run_hard_isolated_site_jobs(
            all_sites,
            period=args.period,
            timeout=args.timeout,
            workers=workers,
            show_browser=args.show_browser,
            delay=args.delay,
            hard_timeout=args.hard_timeout,
            browser_hard_timeout=args.browser_hard_timeout,
            host_locks=host_locks,
        )

    completed_count = success_count = fail_count = 0
    total_progress = len(all_sites)
    next_period_successes = []
    for index, site in all_sites:
        outcome = outcomes.get(index, (site, None, "", "未执行", [], None))
        returned_site, result, detail, error, _values, metadata = outcome
        if returned_site != site:
            outcome = (
                site,
                None,
                "",
                "RuntimeError: 结果站点身份不一致",
                [],
                None,
            )
            outcomes[index] = outcome
            returned_site, result, detail, error, _values, metadata = outcome
        if result and matched_period_from_reason(args.period, metadata) == args.period + 1:
            next_period_successes.append(site.name)
        completed_count += 1
        success_count += int(bool(result))
        fail_count += int(not result)
        elapsed = next(
            (
                value
                for name, value, _ok in site_timings
                if name == site.name
            ),
            0.0,
        )
        print_outcome(
            site,
            result,
            detail,
            error,
            format_progress_prefix(index, total_progress),
        )
        print(
            format_completion_progress(
                completed_count,
                total_progress,
                success_count,
                fail_count,
                site.name,
                elapsed,
            )
        )

    for line in build_slow_site_lines(site_timings):
        print(line)

    total_sites = len(sites)
    success_rate = (success_count / total_sites * 100.0) if total_sites else 0.0
    print(f"[INFO] 成功率: {success_count}/{total_sites} ({success_rate:.2f}%)")
    if next_period_successes:
        print(
            f"[INFO] {args.period + 1}期严格{sites[0].pick if len(sites) == 1 else '边界'}"
            f"替代命中: {len(next_period_successes)} 个；"
            f"仅在{args.period}期未能通过各站自身top/bottom边界时采用"
        )

    success_lines, fail_lines, ranking_values, failure_categories = (
        build_current_only_results(sites, outcomes, args.period)
    )
    with ExitStack() as output_locks:
        for path in sorted(
            {success_path, fail_path}, key=lambda p: os.path.normcase(str(p))
        ):
            output_locks.enter_context(exclusive_path_lock(path))
        existing_success = (
            success_path.read_text(encoding="utf-8-sig")
            if success_path.exists()
            else ""
        )
        if args.append_success:
            configured_names = [site.name for site in configured_sites]
            if len(set(configured_names)) != len(configured_names):
                raise ValueError("成功TXT无法按名称唯一解析站点，禁止追加")
            success_output = merge_success_output_lines(existing_success, success_lines)
        else:
            success_output = build_success_output_lines(success_lines, ranking_values)
        success_text = "\n".join(success_output) + ("\n" if success_output else "")
        existing_failure = (
            fail_path.read_text(encoding="utf-8-sig") if fail_path.exists() else ""
        )
        if args.append_success:
            successful_site_ids = {
                site.site_id
                for site, result, _detail, _error, _rank_values, _metadata in outcomes.values()
                if result
            }
            failure_output = merge_failure_output(
                _normalize_legacy_failures(existing_failure, configured_sites),
                fail_lines,
                successful_site_ids,
            )
        else:
            failure_output = format_failure_output(fail_lines, failure_categories)
        changes = {
            fail_path: (failure_output, "utf-8-sig") if failure_output else None
        }
        if not (
            args.append_success
            and success_text.splitlines() == existing_success.splitlines()
        ):
            changes[success_path] = (success_text, "utf-8-sig")
        commit_text_transaction_unlocked(changes)

    cache_update_error: Exception | None = None
    cache_updated = False
    if not args.no_fingerprint_cache_sync:
        try:
            if args.retry_failures or cache_update_allowed(success_count, total_sites):
                (
                    requested_cache_outcomes,
                    next_cache_sites,
                    next_cache_outcomes,
                    next_cache_period,
                ) = _split_cache_outcomes_by_matched_period(
                    sites,
                    outcomes,
                    args.period,
                )
                update_recent_cache_from_outcomes(
                    fingerprint_cache_path,
                    sites,
                    requested_cache_outcomes,
                    args.period,
                    10,
                    args.preserve_unconfigured_cache_sites,
                    cycle_year=cycle_year,
                )
                if next_cache_sites:
                    next_key = period_key.next()
                    if (
                        next_key.cycle_year != cycle_year
                        or next_key.issue != next_cache_period
                    ):
                        raise ValueError(
                            "跨周期下一期不能隐式混入本期缓存；"
                            "请使用新周期显式运行"
                        )
                    update_recent_cache_from_outcomes(
                        fingerprint_cache_path,
                        next_cache_sites,
                        next_cache_outcomes,
                        next_cache_period,
                        10,
                        True,
                        cycle_year=cycle_year,
                    )
                cache_updated = True
            else:
                record_recent_cache_failures(
                    fingerprint_cache_path,
                    sites,
                    outcomes,
                    args.period,
                    cycle_year=cycle_year,
                )
        except Exception as exc:
            cache_update_error = exc
            print(
                f"[ERROR] 缓存更新未完成: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )

    print(f"\n成功 {len(success_lines)} 个，保存到: {success_path}")
    print(
        f"失败 {len(fail_lines)} 个，保存到: {fail_path}"
        if fail_lines
        else "失败 0 个，未生成失败文件"
    )
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
    raise SystemExit(run(build_arg_parser().parse_args()))
