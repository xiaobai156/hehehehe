import argparse
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import Lock


from he_app.config.settings import CACHE_FILE, SITES_FILE
from he_app.config.sites import load_sites
from he_app.domain.errors import FingerprintCacheError
from he_app.domain.models import Site
from he_app.domain.periods import (
    PeriodKey,
    current_tokyo_period,
    period_key_sort_value,
    period_window as cycle_period_window,
    periods_are_consecutive_descending,
)
from he_app.fetch.browser import BrowserClient
from he_app.fetch.discovery import collect_documents
from he_app.fetch.http import build_host_locks, create_session
from he_app.parsers.common import format_failure_result
from he_app.services.document_sources import collect_special_site_documents, requires_browser
from he_app.services.fingerprint import build_site_fingerprint
from he_app.services.isolation import IsolatedJobResult, run_isolated_site_jobs
from he_app.storage.atomic_write import exclusive_path_lock, write_text_atomic_unlocked, commit_text_transaction
from he_app.storage.recent_cache import (
    cache_base_period_key, read_validated_fingerprints, write_history_fingerprints, load_recent_cache,
    validate_recent_cache_identity, validate_cache_freshness,
)


PeriodToken = int | PeriodKey
Fingerprint = dict[PeriodToken, str]
DEFAULT_FINGERPRINT_CACHE = "outputs/recent_10_cache.json"


@dataclass(frozen=True)
class PairMatch:
    left_index: int
    right_index: int
    periods: tuple[PeriodToken, ...]
    values: tuple[str, ...]

    @property
    def length(self) -> int:
        return len(self.periods)


def build_fingerprint(
    site: Site,
    documents: list[str],
    period: int,
    periods: int,
    cycle_year: int | None = None,
) -> Fingerprint:
    return build_site_fingerprint(site, documents, period, periods, cycle_year)


def longest_equal_consecutive_run(
    left: Fingerprint, right: Fingerprint
) -> tuple[tuple[PeriodToken, ...], tuple[str, ...]]:
    best_periods: list[PeriodToken] = []
    best_values: list[str] = []
    current_periods: list[PeriodToken] = []
    current_values: list[str] = []

    common = sorted(
        set(left) & set(right),
        key=period_key_sort_value,
        reverse=True,
    )
    for period in common:
        equal = left[period] == right[period]
        consecutive = bool(current_periods) and periods_are_consecutive_descending(
            current_periods[-1], period
        )
        if not equal or (current_periods and not consecutive):
            if len(current_periods) > len(best_periods):
                best_periods = current_periods
                best_values = current_values
            current_periods = []
            current_values = []
        if equal:
            current_periods.append(period)
            current_values.append(left[period])

    if len(current_periods) > len(best_periods):
        best_periods = current_periods
        best_values = current_values
    return tuple(best_periods), tuple(best_values)


def find_pair_matches(
    sites: list[Site],
    fingerprints: dict[int, Fingerprint],
    min_length: int = 3,
) -> list[PairMatch]:
    matches: list[PairMatch] = []
    indexes = sorted(fingerprints)
    for left_position, left_index in enumerate(indexes):
        for right_index in indexes[left_position + 1 :]:
            if sites[left_index].url == sites[right_index].url:
                continue
            periods, values = longest_equal_consecutive_run(
                fingerprints[left_index], fingerprints[right_index]
            )
            if len(periods) >= min_length:
                matches.append(
                    PairMatch(left_index, right_index, periods, values)
                )
    matches.sort(key=lambda item: (-item.length, item.left_index, item.right_index))
    return matches


def split_matches(matches: list[PairMatch]) -> tuple[list[PairMatch], list[PairMatch]]:
    return (
        [match for match in matches if match.length >= 6],
        [match for match in matches if 3 <= match.length <= 5],
    )


def period_window(
    period: int, periods: int, cycle_year: int | None = None
) -> set[PeriodToken]:
    if cycle_year is None:
        return set(range(period, period - periods, -1))
    return set(cycle_period_window(PeriodKey(cycle_year, period), periods))


def trim_fingerprint(
    fingerprint: Fingerprint,
    period: int,
    periods: int,
    cycle_year: int | None = None,
) -> Fingerprint:
    allowed = period_window(period, periods, cycle_year)
    return {
        current_period: value
        for current_period, value in sorted(
            fingerprint.items(),
            key=lambda item: period_key_sort_value(item[0]),
            reverse=True,
        )
        if current_period in allowed
    }


def write_fingerprint_cache(
    path: Path,
    sites: list[Site],
    fingerprints: dict[int, Fingerprint],
    errors: dict[int, str],
    period: int,
    periods: int,
    lock_timeout: float = 30.0,
    cycle_year: int | None = None,
) -> None:
    write_history_fingerprints(
        path, sites, fingerprints, errors, period, periods, lock_timeout,
        cycle_year=cycle_year,
    )


@contextmanager
def fingerprint_cache_lock(path: Path, timeout: float = 30.0):
    try:
        with exclusive_path_lock(path, timeout=timeout):
            yield
    except TimeoutError as exc:
        raise TimeoutError(f"缓存锁等待超过{timeout:g}秒: {path}") from exc


def write_text_atomic(path: Path, text: str, encoding: str) -> None:
    write_text_atomic_unlocked(path, text, encoding)


def load_fingerprint_cache(
    path: Path, period: int, periods: int, cycle_year: int | None = None
) -> tuple[list[Site], dict[int, Fingerprint], dict[int, str]]:
    return read_validated_fingerprints(path, period, periods, cycle_year)


def load_compare_cache(
    path: Path,
    requested_period: int,
    periods: int,
    max_age_hours: float = 24.0,
    cycle_year: int | None = None,
) -> tuple[PeriodKey, list[Site], dict[int, Fingerprint], dict[int, str]]:
    data = load_recent_cache(path)
    validate_cache_freshness(data, max_age_hours)
    base = cache_base_period_key(data, cycle_year)
    if base is None:
        raise ValueError("--compare-cache 缓存缺少有效基准期")
    requested = PeriodKey(cycle_year or base.cycle_year, requested_period)
    if requested not in {base, base.previous()}:
        raise ValueError(
            "新增站测试期只允许缓存最新期"
            f"{base.cache_key}或上一期{base.previous().cache_key}"
        )
    sites, fingerprints, errors = load_fingerprint_cache(
        path, base.issue, periods, base.cycle_year
    )
    if not fingerprints:
        raise ValueError("--compare-cache 缓存没有可用旧站指纹，拒绝运行")
    return base, sites, fingerprints, errors


def reject_new_sites_without_baseline(
    fingerprints: dict[int, Fingerprint],
    errors: dict[int, str],
    new_indexes: list[int],
    base_period: PeriodKey,
) -> None:
    baseline_periods = {base_period, base_period.previous()}
    for index in new_indexes:
        fingerprint = fingerprints.get(index)
        if fingerprint and baseline_periods.intersection(fingerprint):
            continue
        fingerprints.pop(index, None)
        errors[index] = (
            "ValueError: 新站指纹未命中缓存基准期"
            f"{base_period.cache_key}或{base_period.previous().cache_key}"
        )


def build_duplicate_output(groups: list[list[Site]]) -> list[str]:
    lines: list[str] = []
    for group_index, group in enumerate(groups, start=1):
        if lines:
            lines.append("")
        lines.append(f"重复组{group_index}")
        for site in group:
            lines.append(site.url)
    return lines


def _period_label(period: PeriodToken) -> str:
    return period.cache_key if isinstance(period, PeriodKey) else str(period)


def build_duplicate_output_with_values(
    matches: list[PairMatch],
    sites: list[Site],
    fingerprints: dict[int, Fingerprint],
    period: PeriodToken,
    title: str = "重复组",
) -> list[str]:
    lines: list[str] = []
    for group_index, match in enumerate(matches, start=1):
        if lines:
            lines.append("")
        values = " ".join(
            f"{_period_label(item)}期:{value}"
            for item, value in zip(match.periods, match.values)
        )
        lines.append(f"{title}{group_index} 连续{match.length}期 {values}")
        for index in (match.left_index, match.right_index):
            site = sites[index]
            lines.append(f"{site.site_id} {site.name} {site.url}")
    return lines


def build_raw_output(
    sites: list[Site],
    fingerprints: dict[int, Fingerprint],
    errors: dict[int, str],
    period: PeriodToken,
) -> list[str]:
    all_keys = {key for values in fingerprints.values() for key in values}
    ordered_keys = sorted(all_keys, key=period_key_sort_value, reverse=True)
    range_text = (
        f"{_period_label(ordered_keys[0])}期-{_period_label(ordered_keys[-1])}期"
        if ordered_keys
        else "无可用历史"
    )
    lines = [f"基准期: {_period_label(period)}", f"期数范围: {range_text}", ""]
    for index, site in enumerate(sites):
        if index in fingerprints:
            values = " ".join(
                f"{_period_label(key)}期:{value}"
                for key, value in sorted(
                    fingerprints[index].items(),
                    key=lambda item: period_key_sort_value(item[0]),
                    reverse=True,
                )
            )
            lines.append(f"{index + 1:02d}. {site.site_id} {site.name} {values}")
        else:
            lines.append(
                f"{index + 1:02d}. {site.site_id} {site.name} 失败: "
                f"{errors.get(index, '未找到完整数据')}"
            )
    return lines


def format_duplicate_failure_result(period: int, site: Site, error: str | None) -> str:
    message = (error or "未找到完整数据").strip()
    return format_failure_result(period, site, "", message)


def scrape_http_fingerprint(
    index: int,
    site: Site,
    period: int,
    periods: int,
    timeout: int,
    host_locks: dict[str, Lock] | None = None,
    cycle_year: int | None = None,
) -> tuple[int, Fingerprint | None, str | None]:
    try:
        with create_session(host_locks) as session:
            documents = collect_special_site_documents(
                session, site, timeout, period
            )
            if documents is None:
                documents = collect_documents(session, site.url, timeout)
        fingerprint = build_fingerprint(
            site, documents, period, periods, cycle_year
        )
        if not fingerprint:
            raise ValueError("未抓到可参与重复检测的数据")
        return index, fingerprint, None
    except Exception as exc:
        return index, None, f"{type(exc).__name__}: {exc}"


def scrape_browser_fingerprint(
    site: Site,
    period: int,
    periods: int,
    timeout: int,
    browser: BrowserClient,
    host_locks: dict[str, Lock] | None = None,
    cycle_year: int | None = None,
) -> tuple[Fingerprint | None, str | None]:
    try:
        with create_session(host_locks) as session:
            documents = collect_special_site_documents(
                session, site, timeout, period
            )
        if documents is None:
            documents = browser.get_documents(
                site.url, period, site.click_first, timeout
            )
        fingerprint = build_fingerprint(
            site, documents, period, periods, cycle_year
        )
        if not fingerprint:
            raise ValueError("未抓到可参与重复检测的数据")
        return fingerprint, None
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"


def scrape_fingerprint(
    index: int,
    site: Site,
    period: int,
    periods: int,
    timeout: int,
    show_browser: bool,
    host_locks: dict[str, Lock] | None = None,
    cycle_year: int | None = None,
) -> tuple[int, Fingerprint | None, str | None]:
    """Compatibility in-process helper; production uses hard isolation."""

    try:
        if requires_browser(site):
            browser = BrowserClient(headless=not show_browser)
            try:
                browser.start()
                fingerprint, error = scrape_browser_fingerprint(
                    site,
                    period,
                    periods,
                    timeout,
                    browser,
                    host_locks,
                    cycle_year,
                )
                return index, fingerprint, error
            finally:
                browser.close()
        return scrape_http_fingerprint(
            index,
            site,
            period,
            periods,
            timeout,
            host_locks,
            cycle_year,
        )
    except Exception as exc:
        return index, None, f"{type(exc).__name__}: {exc}"


def browser_worker_count(sites: list[Site], workers: int) -> int:
    return min(
        3,
        max(1, workers),
        sum(1 for site in sites if requires_browser(site)),
    )


def _isolated_fingerprint_worker(
    index: int,
    site: Site,
    browser: BrowserClient | None,
    host_locks,
    period: int,
    periods: int,
    timeout: int,
    cycle_year: int | None,
):
    if requires_browser(site):
        if browser is None:
            raise RuntimeError("浏览器站点被分配到非浏览器隔离槽")
        return scrape_browser_fingerprint(
            site,
            period,
            periods,
            timeout,
            browser,
            host_locks,
            cycle_year,
        )
    _index, fingerprint, error = scrape_http_fingerprint(
        index,
        site,
        period,
        periods,
        timeout,
        host_locks,
        cycle_year,
    )
    return fingerprint, error


def _custom_fingerprint_worker(
    index: int,
    site: Site,
    _browser: BrowserClient | None,
    host_locks,
    callable_object,
    period: int,
    periods: int,
    timeout: int,
    show_browser: bool,
):
    returned_index, fingerprint, error = callable_object(
        index,
        site,
        period,
        periods,
        timeout,
        show_browser,
        host_locks,
    )
    if returned_index != index:
        raise RuntimeError(
            f"任务返回站点索引不一致: expected={index}, actual={returned_index}"
        )
    return fingerprint, error


def run_fingerprint_jobs(
    all_sites: list[tuple[int, Site]],
    sites: list[Site],
    workers: int,
    period: int,
    periods: int,
    timeout: int,
    show_browser: bool,
    host_locks: dict[str, Lock],
    hard_timeout: float | None = None,
    poll_interval: float = 0.1,
    worker_callable=None,
    cycle_year: int | None = None,
    browser_hard_timeout: float | None = None,
    diagnostics: dict[int, IsolatedJobResult] | None = None,
) -> tuple[dict[int, Fingerprint], dict[int, str]]:
    fingerprints: dict[int, Fingerprint] = {}
    errors: dict[int, str] = {}
    if not all_sites:
        return fingerprints, errors

    http_limit = hard_timeout if hard_timeout is not None else max(45.0, float(timeout) * 3.0)
    browser_limit = (
        browser_hard_timeout
        if browser_hard_timeout is not None
        else max(90.0, float(timeout) * 5.0)
    )
    handler = _isolated_fingerprint_worker
    handler_args = (period, periods, timeout, cycle_year)
    if worker_callable is not None:
        handler = _custom_fingerprint_worker
        handler_args = (
            worker_callable,
            period,
            periods,
            timeout,
            show_browser,
        )

    def on_start(_index: int, site: Site) -> None:
        print(f"[START] {site.name} ({site.pick})")

    def on_result(job: IsolatedJobResult) -> None:
        if diagnostics is not None:
            diagnostics[job.index] = job
        if job.error is not None:
            errors[job.index] = job.error
            print(f"[FAIL] {job.site.name}: {job.error}")
            return
        value = job.value
        if not isinstance(value, tuple) or len(value) != 2:
            errors[job.index] = "RuntimeError: 指纹隔离任务返回格式错误"
            print(f"[FAIL] {job.site.name}: {errors[job.index]}")
            return
        fingerprint, error = value
        if fingerprint is None:
            errors[job.index] = error or "未找到完整数据"
            print(f"[FAIL] {job.site.name}: {errors[job.index]}")
            return
        fingerprints[job.index] = fingerprint
        print(f"[OK] {job.site.name} 历史{len(fingerprint)}期")

    run_isolated_site_jobs(
        all_sites,
        workers=workers,
        worker_callable=handler,
        worker_args=handler_args,
        needs_browser=requires_browser,
        hard_timeout=http_limit,
        browser_hard_timeout=browser_limit,
        browser_limit=3,
        headless=not show_browser,
        host_keys=host_locks,
        poll_interval=poll_interval,
        on_start=on_start,
        on_result=on_result,
    )
    return fingerprints, errors


def is_custom_sites_path(sites_arg: str) -> bool:
    return Path(sites_arg).resolve() != Path(SITES_FILE).resolve()


def is_recent_fingerprint_cache_path(cache_arg: str) -> bool:
    return Path(cache_arg).resolve() == Path(DEFAULT_FINGERPRINT_CACHE).resolve()


def validate_duplicate_checker_args(args: argparse.Namespace) -> None:
    if args.period < 1 or args.timeout <= 0 or args.workers < 1:
        raise SystemExit("期数、超时和并发必须为正数")
    if args.hard_timeout <= 0 or args.browser_hard_timeout <= 0:
        raise SystemExit("硬超时必须为正数")
    cycle_year = args.cycle_year or current_tokyo_period().cycle_year
    try:
        PeriodKey(cycle_year, args.period)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    if args.periods < 1:
        raise SystemExit("--periods 必须大于等于 1")
    if args.mirror_limit:
        raise SystemExit("自动拼接镜像已停用")
    if args.compare_cache and args.cache_max_age_hours <= 0:
        raise SystemExit("比较缓存有效期必须大于0")
    if args.compare_cache and args.write_fingerprint_cache:
        raise SystemExit("--compare-cache 只用于新增站快速对比，不能同时覆盖写入缓存")
    if not is_custom_sites_path(args.sites):
        return
    if not args.compare_cache:
        raise SystemExit(
            "新增站检测必须使用 --compare-cache outputs/recent_10_cache.json；不能用旧实时指纹脚本当新增判重依据"
        )
    if not is_recent_fingerprint_cache_path(args.compare_cache):
        raise SystemExit("新增站检测只能使用 outputs/recent_10_cache.json 作为 --compare-cache")


def main() -> None:
    parser = argparse.ArgumentParser(description="按最近 M 期绝杀一合数据指纹检测重复网站。")
    parser.add_argument("--period", type=int, required=True, help="当前期数，例如 251")
    parser.add_argument("--periods", type=int, default=10, help="优先抓取多少期，默认 10；不足时按实际抓到的期数检测")
    parser.add_argument("--success", default=None, help="重复拒收输出文件，默认 N期重复网站.txt")
    parser.add_argument("--review", default=None, help="疑似重复审核输出文件，默认 N期疑似重复网站.txt")
    parser.add_argument("--fail", default=None, help="失败输出文件，默认 N期重复检测失败.txt")
    parser.add_argument("--timeout", type=int, default=20, help="单个请求超时秒数")
    parser.add_argument("--hard-timeout", type=float, default=60.0, help="单站HTTP硬超时秒数，覆盖DNS/TLS/解析")
    parser.add_argument("--browser-hard-timeout", type=float, default=120.0, help="单站浏览器硬超时秒数，覆盖浏览器启动")
    parser.add_argument("--cycle-year", type=int, default=None, help="期数所属周期年份；默认东京当前年份")
    parser.add_argument("--workers", type=int, default=8, help="全部站点并发数量，默认 8")
    parser.add_argument("--retries", type=int, default=0, help=argparse.SUPPRESS)
    parser.add_argument("--show-browser", action="store_true", help="显示二次点击站点的浏览器窗口")
    parser.add_argument("--sites", default=SITES_FILE, help="站点配置 JSON，默认 sites.json")
    parser.add_argument("--cache", default=CACHE_FILE, help="成功结果缓存 JSON，默认 he_success_cache.json")
    parser.add_argument("--cache-max-age-hours", type=float, default=24.0, help="比较缓存最大有效小时数，默认24；必须大于0")
    parser.add_argument("--mirror-limit", type=int, default=0, help=argparse.SUPPRESS)
    parser.add_argument("--no-browser-fallback", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--raw", default=None, help="可选：原始排序数据输出文件；默认不生成")
    parser.add_argument("--fingerprint-cache", default=DEFAULT_FINGERPRINT_CACHE, help="重复检测指纹备份 JSON，默认 outputs/recent_10_cache.json")
    parser.add_argument("--write-fingerprint-cache", action="store_true", help="本次全站检测完成后覆盖写入最近 N 期指纹备份")
    parser.add_argument("--compare-cache", default=None, help="读取指纹备份作为旧站数据，只抓 --sites 指定的新站并与缓存比较")
    args = parser.parse_args()

    validate_duplicate_checker_args(args)

    sys.stdout.reconfigure(encoding="utf-8")

    cycle_year = args.cycle_year or current_tokyo_period().cycle_year
    base_period_key = PeriodKey(cycle_year, args.period)
    sites_path = Path(args.sites).resolve()
    input_sites = load_sites(sites_path)
    compare_base_period = None
    new_site_indexes: list[int] = []
    if args.compare_cache:
        try:
            compare_base_period, cached_sites, fingerprints, _cached_errors = load_compare_cache(
                Path(args.compare_cache).resolve(),
                args.period,
                args.periods,
                args.cache_max_age_hours,
                cycle_year,
            )
        except (OSError, ValueError, FingerprintCacheError) as exc:
            raise SystemExit(f"--compare-cache 不可用: {exc}") from exc
        validate_recent_cache_identity(
            load_recent_cache(Path(args.compare_cache).resolve()),
            load_sites(Path(SITES_FILE).resolve()),
            cycle_year=compare_base_period.cycle_year,
        )
        new_sites = input_sites
        old_ids = {site.site_id for site in cached_sites}
        if old_ids.intersection(site.site_id for site in new_sites):
            raise SystemExit("新增站ID与正式缓存已有站点重复")
        sites = cached_sites + new_sites
        errors = {}
        old_count = len(cached_sites)
        host_locks = build_host_locks(new_sites, {})
        all_sites = [(old_count + index, site) for index, site in enumerate(new_sites)]
        new_site_indexes = [index for index, _site in all_sites]
        print(f"[INFO] 读取旧站指纹缓存: {len(cached_sites)} 个，只抓新站: {len(new_sites)} 个")
    else:
        sites = input_sites
        host_locks = build_host_locks(sites, {})
        fingerprints = {}
        errors = {}
        all_sites = list(enumerate(sites))

    workers = max(1, args.workers)

    print(
        f"[INFO] 本次抓取站点: {len(all_sites)} 个，workers={workers}，"
        f"周期={base_period_key.cache_key}"
    )
    scraped_fingerprints, scraped_errors = run_fingerprint_jobs(
        all_sites,
        sites,
        workers,
        args.period,
        args.periods,
        args.timeout,
        args.show_browser,
        host_locks,
        hard_timeout=args.hard_timeout,
        cycle_year=cycle_year,
        browser_hard_timeout=args.browser_hard_timeout,
    )
    fingerprints.update(scraped_fingerprints)
    errors.update(scraped_errors)

    if compare_base_period is not None:
        reject_new_sites_without_baseline(
            fingerprints, errors, new_site_indexes, compare_base_period
        )

    # A valid current row with insufficient history is not a failed scrape.
    # Report incompleteness without deleting that verified row from the cache.
    report_errors = dict(errors)
    for index, values in scraped_fingerprints.items():
        if index in fingerprints and len(values) < 6:
            report_errors[index] = (
                f"历史不足: 仅{len(values)}期；至少6个连续有效期才能完成重复拒收结论"
            )
    fail_lines = [
        format_duplicate_failure_result(args.period, sites[index], error)
        for index, error in sorted(report_errors.items())
        if index not in fingerprints or error.startswith("历史不足:")
    ]

    pair_matches = find_pair_matches(sites, fingerprints)
    if args.compare_cache:
        new_set = set(new_site_indexes)
        pair_matches = [pair for pair in pair_matches if
                        pair.left_index in new_set or pair.right_index in new_set]
    reject_matches, review_matches = split_matches(pair_matches)
    success_file = args.success or f"{args.period}期重复网站.txt"
    review_file = args.review or f"{args.period}期疑似重复网站.txt"
    fail_file = args.fail or f"{args.period}期重复检测失败.txt"
    success_path = Path(success_file).resolve()
    review_path = Path(review_file).resolve()
    fail_path = Path(fail_file).resolve()
    duplicate_lines = build_duplicate_output_with_values(reject_matches, sites, fingerprints, base_period_key, "重复组")
    review_lines = build_duplicate_output_with_values(review_matches, sites, fingerprints, base_period_key, "疑似组")

    changes = {
        success_path: ("\n".join(duplicate_lines) + ("\n" if duplicate_lines else ""), "utf-8-sig"),
        review_path: ("\n".join(review_lines) + ("\n" if review_lines else ""), "utf-8-sig"),
        fail_path: ("\n".join(fail_lines) + ("\n" if fail_lines else ""), "utf-8-sig"),
    }
    output_paths = [success_path, review_path, fail_path]
    if args.raw:
        raw_path = Path(args.raw).resolve()
        output_paths.append(raw_path)
        raw_lines = build_raw_output(sites, fingerprints, errors, base_period_key)
        changes[raw_path] = ("\n".join(raw_lines) + "\n", "utf-8-sig")
    if len(set(output_paths)) != len(output_paths) or sites_path in output_paths:
        raise SystemExit("判重输出路径必须互不相同，且不能覆盖站点配置")
    cache_paths = {Path(args.fingerprint_cache).resolve()}
    if args.compare_cache:
        cache_paths.add(Path(args.compare_cache).resolve())
    if set(output_paths) & cache_paths:
        raise SystemExit("判重TXT输出禁止覆盖指纹缓存")
    commit_text_transaction(changes)
    if args.write_fingerprint_cache:
        fingerprint_cache_path = Path(args.fingerprint_cache).resolve()
        write_fingerprint_cache(
            fingerprint_cache_path,
            sites,
            fingerprints,
            errors,
            args.period,
            args.periods,
            cycle_year=cycle_year,
        )

    print(f"\n完成检测 {len(fingerprints)} 个，重复拒收 {len(reject_matches)} 组，保存到: {success_path}")
    print(f"疑似审核 {len(review_matches)} 组，保存到: {review_path}")
    print(f"失败 {len(fail_lines)} 个，保存到: {fail_path}")
    if args.raw:
        print(f"原始排序数据保存到: {raw_path}")
    if args.write_fingerprint_cache:
        print(f"指纹备份已更新: {fingerprint_cache_path}")


if __name__ == "__main__":
    main()

