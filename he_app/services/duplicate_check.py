import argparse
import json
import multiprocessing
import queue
import sys
import time
import warnings
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import Lock

import requests
from requests.packages.urllib3.exceptions import InsecureRequestWarning

from he_app.config.settings import CACHE_FILE, SITES_FILE
from he_app.config.sites import load_sites
from he_app.domain.models import Site
from he_app.domain.policies import normalize_pick
from he_app.fetch.browser import BrowserClient, BrowserPool
from he_app.fetch.discovery import collect_documents
from he_app.fetch.http import build_host_locks, create_session, host_key
from he_app.parsers.common import format_failure_result
from he_app.services.crawler import apply_cached_urls
from he_app.services.document_sources import collect_special_site_documents
from he_app.services.fingerprint import build_site_fingerprint
from he_app.storage.atomic_write import exclusive_path_lock, write_text_atomic_unlocked
from he_app.storage.success_cache import load_cached_url_by_site


Fingerprint = dict[int, str]
DEFAULT_FINGERPRINT_CACHE = "outputs/recent_10_cache.json"


@dataclass(frozen=True)
class PairMatch:
    left_index: int
    right_index: int
    periods: tuple[int, ...]
    values: tuple[str, ...]

    @property
    def length(self) -> int:
        return len(self.periods)


def build_fingerprint(site: Site, documents: list[str], period: int, periods: int) -> Fingerprint:
    return build_site_fingerprint(site, documents, period, periods)


def longest_equal_consecutive_run(left: Fingerprint, right: Fingerprint) -> tuple[tuple[int, ...], tuple[str, ...]]:
    best_periods: list[int] = []
    best_values: list[str] = []
    current_periods: list[int] = []
    current_values: list[str] = []

    for period in sorted(set(left) & set(right), reverse=True):
        equal = left[period] == right[period]
        consecutive = bool(current_periods) and current_periods[-1] - period == 1
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
                fingerprints[left_index],
                fingerprints[right_index],
            )
            if len(periods) >= min_length:
                matches.append(PairMatch(left_index, right_index, periods, values))
    matches.sort(key=lambda item: (-item.length, item.left_index, item.right_index))
    return matches


def split_matches(matches: list[PairMatch]) -> tuple[list[PairMatch], list[PairMatch]]:
    reject = [match for match in matches if match.length >= 6]
    review = [match for match in matches if 3 <= match.length <= 5]
    return reject, review


def period_window(period: int, periods: int) -> set[int]:
    return set(range(period, period - periods, -1))


def trim_fingerprint(fingerprint: Fingerprint, period: int, periods: int) -> Fingerprint:
    allowed = period_window(period, periods)
    return {
        current_period: value
        for current_period, value in sorted(fingerprint.items(), reverse=True)
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
) -> None:
    with fingerprint_cache_lock(path, timeout=lock_timeout):
        _write_fingerprint_cache_unlocked(path, sites, fingerprints, errors, period, periods)


def _write_fingerprint_cache_unlocked(
    path: Path,
    sites: list[Site],
    fingerprints: dict[int, Fingerprint],
    errors: dict[int, str],
    period: int,
    periods: int,
) -> None:
    payload = {
        "base_period": period,
        "periods": periods,
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "sites": [],
        "errors": [],
    }
    previous_by_key: dict[str, Fingerprint] = {}
    if path.exists():
        previous_sites, previous_fingerprints, _previous_errors = load_fingerprint_cache(path, period, periods)
        for index, previous_site in enumerate(previous_sites):
            fingerprint = previous_fingerprints.get(index, {})
            for key in (previous_site.site_id, previous_site.url):
                if key:
                    previous_by_key[key] = fingerprint

    for index, site in enumerate(sites):
        fingerprint = trim_fingerprint(fingerprints.get(index, {}), period, periods)
        if not fingerprint:
            fingerprint = trim_fingerprint(
                previous_by_key.get(site.site_id) or previous_by_key.get(site.url) or {},
                period,
                periods,
            )
        if not fingerprint:
            continue
        payload["sites"].append(
            {
                "id": site.site_id,
                "name": site.name,
                "url": site.url,
                "pick": site.pick,
                "browser": site.browser,
                "click_first": site.click_first,
                "fingerprint": {str(key): value for key, value in fingerprint.items()},
            }
        )
    for index, error in sorted(errors.items()):
        site = sites[index]
        if trim_fingerprint(
            previous_by_key.get(site.site_id) or previous_by_key.get(site.url) or {},
            period,
            periods,
        ):
            continue
        payload["errors"].append(
            {
                "id": site.site_id,
                "name": site.name,
                "url": site.url,
                "error": error,
            }
        )

    write_text_atomic(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


@contextmanager
def fingerprint_cache_lock(path: Path, timeout: float = 30.0):
    try:
        with exclusive_path_lock(path, timeout=timeout):
            yield
    except TimeoutError as exc:
        raise TimeoutError(f"缓存锁等待超过{timeout:g}秒: {path}") from exc


def write_text_atomic(path: Path, text: str, encoding: str) -> None:
    write_text_atomic_unlocked(path, text, encoding)


def load_fingerprint_cache(path: Path, period: int, periods: int) -> tuple[list[Site], dict[int, Fingerprint], dict[int, str]]:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    sites: list[Site] = []
    fingerprints: dict[int, Fingerprint] = {}
    errors: dict[int, str] = {}

    for item in data.get("sites", []):
        if not isinstance(item, dict):
            continue
        site = Site(
            str(item.get("name", "")).strip(),
            str(item.get("url", "")).strip(),
            normalize_pick(str(item.get("pick", "top"))),
            bool(item.get("browser", False)),
            bool(item.get("click_first", False)),
            str(item.get("id", "")).strip(),
        )
        raw_fingerprint = item.get("fingerprint", {})
        if not site.name or not site.url or not isinstance(raw_fingerprint, dict):
            continue
        fingerprint = trim_fingerprint(
            {
                int(current_period): str(value)
                for current_period, value in raw_fingerprint.items()
                if str(current_period).isdigit() and value
            },
            period,
            periods,
        )
        if not fingerprint:
            continue
        fingerprints[len(sites)] = fingerprint
        sites.append(site)

    for item in data.get("errors", []):
        if isinstance(item, dict):
            errors[len(sites) + len(errors)] = str(item.get("error", "未抓到可参与重复检测的数据"))

    return sites, fingerprints, errors


def load_compare_cache(
    path: Path,
    requested_period: int,
    periods: int,
) -> tuple[int, list[Site], dict[int, Fingerprint], dict[int, str]]:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    base_period = data.get("base_period")
    if not isinstance(base_period, int) or isinstance(base_period, bool) or base_period < 1:
        raise ValueError("--compare-cache 缓存缺少有效整数 base_period")
    if requested_period not in {base_period, base_period - 1}:
        raise ValueError(
            f"新增站测试期只允许缓存最新期{base_period}或上一期{base_period - 1}"
        )
    sites, fingerprints, errors = load_fingerprint_cache(path, base_period, periods)
    if not fingerprints:
        raise ValueError("--compare-cache 缓存没有可用旧站指纹，拒绝运行")
    return base_period, sites, fingerprints, errors


def reject_new_sites_without_baseline(
    fingerprints: dict[int, Fingerprint],
    errors: dict[int, str],
    new_indexes: list[int],
    base_period: int,
) -> None:
    baseline_periods = {base_period, base_period - 1}
    for index in new_indexes:
        fingerprint = fingerprints.get(index)
        if fingerprint and baseline_periods.intersection(fingerprint):
            continue
        fingerprints.pop(index, None)
        errors[index] = (
            f"ValueError: 新站指纹未命中缓存基准期{base_period}期或{base_period - 1}期"
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


def build_duplicate_output_with_values(
    matches: list[PairMatch],
    sites: list[Site],
    fingerprints: dict[int, Fingerprint],
    period: int,
    title: str = "重复组",
) -> list[str]:
    lines: list[str] = []
    for group_index, match in enumerate(matches, start=1):
        if lines:
            lines.append("")
        values = " ".join(f"{period}期:{value}" for period, value in zip(match.periods, match.values))
        lines.append(f"{title}{group_index} 连续{match.length}期 {values}")
        for index in (match.left_index, match.right_index):
            site = sites[index]
            lines.append(f"{site.site_id} {site.name} {site.url}")
    return lines


def build_raw_output(
    sites: list[Site],
    fingerprints: dict[int, Fingerprint],
    errors: dict[int, str],
    period: int,
) -> list[str]:
    lines = [f"基准期: {period}", f"期数范围: {period}期-{period - 9}期", ""]
    for index, site in enumerate(sites):
        if index in fingerprints:
            values = " ".join(f"{period}期:{fingerprints[index][period]}" for period in sorted(fingerprints[index], reverse=True))
            lines.append(f"{index + 1:02d}. {site.site_id} {site.name} {values}")
        else:
            lines.append(f"{index + 1:02d}. {site.site_id} {site.name} 失败: {errors.get(index, '未找到完整数据')}")
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
) -> tuple[int, Fingerprint | None, str | None]:
    try:
        session = create_session(host_locks)
        documents = collect_special_site_documents(session, site, timeout)
        if documents is None:
            documents = collect_documents(session, site.url, timeout)
        fingerprint = build_fingerprint(site, documents, period, periods)
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
) -> tuple[Fingerprint | None, str | None]:
    try:
        documents = collect_special_site_documents(
            create_session(host_locks), site, timeout
        )
        if documents is None:
            documents = browser.get_documents(site.url, period, site.click_first, timeout)
        fingerprint = build_fingerprint(site, documents, period, periods)
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
) -> tuple[int, Fingerprint | None, str | None]:
    try:
        if site.browser:
            pool = BrowserPool(1, headless=not show_browser)
            pool.start()
            try:
                fingerprint, error = pool.run(
                    lambda browser: scrape_browser_fingerprint(
                        site, period, periods, timeout, browser, host_locks
                    )
                )
                return index, fingerprint, error
            finally:
                try:
                    pool.close()
                except Exception:
                    pass
        return scrape_http_fingerprint(index, site, period, periods, timeout, host_locks)
    except Exception as exc:
        return index, None, f"{type(exc).__name__}: {exc}"


def browser_worker_count(sites: list[Site], workers: int) -> int:
    return min(3, max(1, workers), sum(1 for site in sites if site.browser))


def _run_isolated_worker(
    task_queue,
    result_queue,
    worker_callable,
    period: int,
    periods: int,
    timeout: int,
    show_browser: bool,
    host_locks,
    browser_worker: bool,
) -> None:
    warnings.simplefilter("ignore", InsecureRequestWarning)
    requests.packages.urllib3.disable_warnings()
    browser_pool = None
    try:
        if browser_worker and worker_callable is scrape_fingerprint:
            browser_pool = BrowserPool(1, headless=not show_browser)
            browser_pool.start()
        while True:
            task = task_queue.get()
            if task is None:
                return
            token, index, site = task
            result_queue.put(("started", token, index, None, None))
            try:
                if browser_pool is not None:
                    fingerprint, error = browser_pool.run(
                        lambda browser: scrape_browser_fingerprint(
                            site, period, periods, timeout, browser, host_locks
                        )
                    )
                    result = (index, fingerprint, error)
                else:
                    result = worker_callable(
                        index, site, period, periods, timeout, show_browser, host_locks
                    )
                result_queue.put(("result", token, *result))
            except BaseException as exc:
                result_queue.put(
                    ("result", token, index, None, f"{type(exc).__name__}: {exc}")
                )
    finally:
        if browser_pool is not None:
            try:
                browser_pool.close()
            except Exception:
                pass


def _stop_process_slot(slot: dict, graceful: bool = False) -> None:
    process = slot["process"]
    task_queue = slot["queue"]
    if graceful and process.is_alive():
        task_queue.put(None)
        process.join(timeout=5.0)
    if process.is_alive():
        process.terminate()
        process.join(timeout=1)
    task_queue.close()


def _start_process_slot(
    context,
    result_queue,
    worker_callable,
    period,
    periods,
    timeout,
    show_browser,
    host_locks,
    browser_worker,
) -> dict:
    task_queue = context.Queue()
    process = context.Process(
        target=_run_isolated_worker,
        args=(
            task_queue,
            result_queue,
            worker_callable,
            period,
            periods,
            timeout,
            show_browser,
            host_locks,
            browser_worker,
        ),
    )
    process.start()
    return {
        "process": process,
        "queue": task_queue,
        "browser": browser_worker,
        "current": None,
        "started_at": None,
    }


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
    poll_interval: float = 1.0,
    worker_callable=None,
) -> tuple[dict[int, Fingerprint], dict[int, str]]:
    fingerprints: dict[int, Fingerprint] = {}
    errors: dict[int, str] = {}
    timeout_limit = hard_timeout if hard_timeout is not None else max(float(timeout) * 15.0, 300.0)
    if not all_sites:
        return fingerprints, errors
    worker_callable = worker_callable or scrape_fingerprint
    context = multiprocessing.get_context("spawn")
    manager = context.Manager()
    lock_keys = set(host_locks) | {host_key(site.url) for _, site in all_sites}
    process_host_locks = {key: manager.Lock() for key in lock_keys if key}
    result_queue = context.Queue()
    browser_jobs = [(index, site) for index, site in all_sites if site.browser]
    http_jobs = [(index, site) for index, site in all_sites if not site.browser]
    browser_slots = browser_worker_count([site for _, site in all_sites], workers)
    http_slots = min(max(1, workers), len(http_jobs)) if http_jobs else 0
    slots = [
        _start_process_slot(
            context, result_queue, worker_callable, period, periods, timeout,
            show_browser, process_host_locks, browser_worker,
        )
        for browser_worker, count in ((False, http_slots), (True, browser_slots))
        for _ in range(count)
    ]
    pending_jobs = {False: http_jobs, True: browser_jobs}
    token_to_slot = {}
    next_token = 0

    def assign(slot: dict) -> None:
        nonlocal next_token
        jobs = pending_jobs[slot["browser"]]
        if not jobs:
            return
        index, site = jobs.pop(0)
        token = next_token
        next_token += 1
        slot["current"] = (token, index, site)
        slot["started_at"] = None
        token_to_slot[token] = slot
        print(f"[START] {site.name} ({site.pick})")
        slot["queue"].put((token, index, site))

    try:
        for slot in slots:
            assign(slot)
        while token_to_slot:
            try:
                message, token, result_index, fingerprint, error = result_queue.get(
                    timeout=max(0.001, poll_interval)
                )
            except queue.Empty:
                message = None
            if message is not None and token in token_to_slot:
                slot = token_to_slot[token]
                if message == "started":
                    slot["started_at"] = time.monotonic()
                else:
                    _token, index, site = slot["current"]
                    token_to_slot.pop(token, None)
                    slot["current"] = None
                    slot["started_at"] = None
                    if result_index != index:
                        fingerprint = None
                        error = (
                            f"RuntimeError: 任务返回站点索引不一致: "
                            f"expected={index}, actual={result_index}"
                        )
                    if fingerprint is None:
                        errors[index] = error or "未找到完整数据"
                        print(f"[FAIL] {site.name}: {errors[index]}")
                    else:
                        fingerprints[index] = fingerprint
                        print(f"[OK] {site.name}")
                    assign(slot)

            now = time.monotonic()
            for slot in list(slots):
                current = slot["current"]
                started_at = slot["started_at"]
                if current is not None and not slot["process"].is_alive():
                    token, index, site = current
                    errors[index] = "RuntimeError: 隔离进程异常退出"
                    print(f"[FAIL] {site.name}: {errors[index]}")
                    token_to_slot.pop(token, None)
                    _stop_process_slot(slot)
                    replacement = _start_process_slot(
                        context, result_queue, worker_callable, period, periods, timeout,
                        show_browser, process_host_locks, slot["browser"],
                    )
                    slots[slots.index(slot)] = replacement
                    assign(replacement)
                    continue
                if current is None or started_at is None or now - started_at <= timeout_limit:
                    continue
                token, index, site = current
                errors[index] = f"TimeoutError: 超过{timeout_limit:.0f}秒未返回"
                print(f"[FAIL] {site.name}: {errors[index]}")
                token_to_slot.pop(token, None)
                _stop_process_slot(slot)
                replacement = _start_process_slot(
                    context, result_queue, worker_callable, period, periods, timeout,
                    show_browser, process_host_locks, slot["browser"],
                )
                slots[slots.index(slot)] = replacement
                assign(replacement)
    finally:
        for slot in slots:
            _stop_process_slot(slot, graceful=True)
        result_queue.close()
        manager.shutdown()
    return fingerprints, errors


def is_custom_sites_path(sites_arg: str) -> bool:
    return Path(sites_arg).resolve() != Path(SITES_FILE).resolve()


def is_recent_fingerprint_cache_path(cache_arg: str) -> bool:
    return Path(cache_arg).resolve() == Path(DEFAULT_FINGERPRINT_CACHE).resolve()


def validate_duplicate_checker_args(args: argparse.Namespace) -> None:
    if args.periods < 1:
        raise SystemExit("--periods 必须大于等于 1")
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
    parser.add_argument("--period", type=int, default=124, help="当前期数，例如 124")
    parser.add_argument("--periods", type=int, default=10, help="优先抓取多少期，默认 10；不足时按实际抓到的期数检测")
    parser.add_argument("--success", default=None, help="重复拒收输出文件，默认 N期重复网站.txt")
    parser.add_argument("--review", default=None, help="疑似重复审核输出文件，默认 N期疑似重复网站.txt")
    parser.add_argument("--fail", default=None, help="失败输出文件，默认 N期重复检测失败.txt")
    parser.add_argument("--timeout", type=int, default=20, help="单个请求超时秒数")
    parser.add_argument("--workers", type=int, default=8, help="全部站点并发数量，默认 8")
    parser.add_argument("--retries", type=int, default=0, help=argparse.SUPPRESS)
    parser.add_argument("--show-browser", action="store_true", help="显示二次点击站点的浏览器窗口")
    parser.add_argument("--sites", default=SITES_FILE, help="站点配置 JSON，默认 sites.json")
    parser.add_argument("--cache", default=CACHE_FILE, help="成功结果缓存 JSON，默认 he_success_cache.json")
    parser.add_argument("--cache-max-age-hours", type=float, default=24.0, help="缓存最大有效小时数，默认 24；0 表示永不过期")
    parser.add_argument("--mirror-limit", type=int, default=0, help=argparse.SUPPRESS)
    parser.add_argument("--no-browser-fallback", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--raw", default=None, help="可选：原始排序数据输出文件；默认不生成")
    parser.add_argument("--fingerprint-cache", default=DEFAULT_FINGERPRINT_CACHE, help="重复检测指纹备份 JSON，默认 outputs/recent_10_cache.json")
    parser.add_argument("--write-fingerprint-cache", action="store_true", help="本次全站检测完成后覆盖写入最近 N 期指纹备份")
    parser.add_argument("--compare-cache", default=None, help="读取指纹备份作为旧站数据，只抓 --sites 指定的新站并与缓存比较")
    args = parser.parse_args()

    validate_duplicate_checker_args(args)

    sys.stdout.reconfigure(encoding="utf-8")
    warnings.simplefilter("ignore", InsecureRequestWarning)
    requests.packages.urllib3.disable_warnings()

    sites_path = Path(args.sites).resolve()
    cache_path = Path(args.cache).resolve()
    input_sites = load_sites(sites_path)
    compare_base_period = None
    new_site_indexes: list[int] = []
    if args.compare_cache:
        try:
            compare_base_period, cached_sites, fingerprints, _cached_errors = load_compare_cache(
                Path(args.compare_cache).resolve(), args.period, args.periods
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise SystemExit(f"--compare-cache 不可用: {exc}") from exc
        new_sites = input_sites
        sites = cached_sites + new_sites
        errors = {}
        old_count = len(cached_sites)
        host_locks = build_host_locks(new_sites, {})
        all_sites = [(old_count + index, site) for index, site in enumerate(new_sites)]
        new_site_indexes = [index for index, _site in all_sites]
        print(f"[INFO] 读取旧站指纹缓存: {len(cached_sites)} 个，只抓新站: {len(new_sites)} 个")
    else:
        sites = input_sites
        cached_urls = load_cached_url_by_site(cache_path, args.period, sites, args.cache_max_age_hours)
        sites = apply_cached_urls(sites, cached_urls)
        host_locks = build_host_locks(sites, {})
        fingerprints = {}
        errors = {}
        all_sites = list(enumerate(sites))

    workers = max(1, args.workers)

    print(f"[INFO] 本次抓取站点: {len(all_sites)} 个，workers={workers}")
    scraped_fingerprints, scraped_errors = run_fingerprint_jobs(
        all_sites,
        sites,
        workers,
        args.period,
        args.periods,
        args.timeout,
        args.show_browser,
        host_locks,
    )
    fingerprints.update(scraped_fingerprints)
    errors.update(scraped_errors)
    if compare_base_period is not None:
        reject_new_sites_without_baseline(
            fingerprints, errors, new_site_indexes, compare_base_period
        )

    fail_lines = [
        format_duplicate_failure_result(args.period, sites[index], error)
        for index, error in sorted(errors.items())
        if index not in fingerprints
    ]

    pair_matches = find_pair_matches(sites, fingerprints)
    reject_matches, review_matches = split_matches(pair_matches)
    success_file = args.success or f"{args.period}期重复网站.txt"
    review_file = args.review or f"{args.period}期疑似重复网站.txt"
    fail_file = args.fail or f"{args.period}期重复检测失败.txt"
    success_path = Path(success_file).resolve()
    review_path = Path(review_file).resolve()
    fail_path = Path(fail_file).resolve()
    duplicate_lines = build_duplicate_output_with_values(reject_matches, sites, fingerprints, args.period, "重复组")
    review_lines = build_duplicate_output_with_values(review_matches, sites, fingerprints, args.period, "疑似组")

    success_path.write_text("\n".join(duplicate_lines) + ("\n" if duplicate_lines else ""), encoding="utf-8-sig")
    review_path.write_text("\n".join(review_lines) + ("\n" if review_lines else ""), encoding="utf-8-sig")
    fail_path.write_text("\n".join(fail_lines) + ("\n" if fail_lines else ""), encoding="utf-8-sig")
    if args.raw:
        raw_path = Path(args.raw).resolve()
        raw_lines = build_raw_output(sites, fingerprints, errors, args.period)
        raw_path.write_text("\n".join(raw_lines) + ("\n" if raw_lines else ""), encoding="utf-8-sig")
    if args.write_fingerprint_cache:
        fingerprint_cache_path = Path(args.fingerprint_cache).resolve()
        write_fingerprint_cache(fingerprint_cache_path, sites, fingerprints, errors, args.period, args.periods)

    print(f"\n完成检测 {len(fingerprints)} 个，重复拒收 {len(reject_matches)} 组，保存到: {success_path}")
    print(f"疑似审核 {len(review_matches)} 组，保存到: {review_path}")
    print(f"失败 {len(fail_lines)} 个，保存到: {fail_path}")
    if args.raw:
        print(f"原始排序数据保存到: {raw_path}")
    if args.write_fingerprint_cache:
        print(f"指纹备份已更新: {fingerprint_cache_path}")


if __name__ == "__main__":
    main()

