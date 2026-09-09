"""Hard wall-clock isolation for network and browser site jobs.

Every active slot owns a child process.  DNS resolution, TLS/socket work,
Selenium startup and page parsing all happen inside that process.  The parent
starts the deadline before assignment and kills the complete process tree when
the deadline expires, so a stuck C extension, DNS resolver, socket or browser
cannot keep the command alive indefinitely.
"""
from __future__ import annotations

import multiprocessing
import os
import queue
import signal
import subprocess
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from he_app.domain.models import Site
from he_app.fetch.browser import BrowserClient, close_browser_safely
from he_app.fetch.http import host_key
from he_app.services.browser_policy import runtime_requires_browser


SiteJob = tuple[int, Site]
WorkerCallable = Callable[..., Any]
NeedsBrowser = Callable[[Site], bool]


@dataclass(frozen=True, slots=True)
class IsolatedJobResult:
    index: int
    site: Site
    value: Any | None
    error: str | None
    elapsed: float
    timed_out: bool = False


def _start_process_group() -> None:
    if os.name == "posix":
        try:
            os.setsid()
        except OSError:
            pass


def _close_queue(q: Any) -> None:
    try:
        q.close()
    except (AttributeError, OSError, ValueError):
        pass
    try:
        q.join_thread()
    except (AttributeError, AssertionError, OSError, ValueError):
        pass


def _worker_loop(
    task_queue: Any,
    result_queue: Any,
    worker_callable: WorkerCallable,
    worker_args: tuple[Any, ...],
    host_locks: dict[str, Any],
    browser_lane: bool,
    headless: bool,
) -> None:
    _start_process_group()
    browser: BrowserClient | None = None
    try:
        while True:
            task = task_queue.get()
            if task is None:
                return
            token, index, site = task
            started = time.monotonic()
            result_queue.put(("started", token, index, None, None, 0.0))
            try:
                if browser_lane and browser is None:
                    browser = BrowserClient(headless=headless)
                    browser.start()
                value = worker_callable(
                    index,
                    site,
                    browser,
                    host_locks,
                    *worker_args,
                )
                result_queue.put(
                    ("result", token, index, value, None, time.monotonic() - started)
                )
            except BaseException as exc:  # process boundary must report every failure
                result_queue.put(
                    (
                        "result",
                        token,
                        index,
                        None,
                        f"{type(exc).__name__}: {exc}",
                        time.monotonic() - started,
                    )
                )
            finally:
                if browser is not None:
                    try:
                        browser.clear_site_state()
                    except BaseException:
                        close_browser_safely(browser)
                        browser = None
    finally:
        if browser is not None:
            close_browser_safely(browser)


def _kill_process_tree(process: Any, graceful: bool = False) -> None:
    if graceful and process.is_alive():
        process.join(timeout=2.0)
    if not process.is_alive():
        try:
            process.join(timeout=0.2)
        except (AssertionError, ValueError):
            pass
        return

    pid = process.pid
    if pid:
        if os.name == "nt":
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    capture_output=True,
                    check=False,
                    timeout=5,
                )
            except (OSError, subprocess.SubprocessError):
                pass
        elif os.name == "posix":
            try:
                os.killpg(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
    if process.is_alive():
        try:
            process.kill()
        except (AttributeError, OSError):
            process.terminate()
    process.join(timeout=2.0)
    if process.is_alive():
        process.terminate()
        process.join(timeout=1.0)


def _start_slot(
    context: multiprocessing.context.BaseContext,
    result_queue: Any,
    worker_callable: WorkerCallable,
    worker_args: tuple[Any, ...],
    host_locks: dict[str, Any],
    browser_lane: bool,
    headless: bool,
) -> dict[str, Any]:
    task_queue = context.Queue(maxsize=1)
    process = context.Process(
        target=_worker_loop,
        args=(
            task_queue,
            result_queue,
            worker_callable,
            worker_args,
            host_locks,
            browser_lane,
            headless,
        ),
    )
    process.start()
    return {
        "process": process,
        "queue": task_queue,
        "browser": browser_lane,
        "current": None,
        "assigned_at": None,
    }


def _stop_slot(slot: dict[str, Any], graceful: bool = False) -> None:
    process = slot["process"]
    task_queue = slot["queue"]
    if graceful and process.is_alive() and slot.get("current") is None:
        try:
            task_queue.put_nowait(None)
        except (queue.Full, OSError, ValueError):
            pass
        process.join(timeout=2.0)
    _kill_process_tree(process)
    _close_queue(task_queue)


def _runtime_needs_browser(site: Site, needs_browser: NeedsBrowser) -> bool:
    return runtime_requires_browser(site, needs_browser)


def _lane_counts(
    jobs: list[SiteJob],
    workers: int,
    needs_browser: NeedsBrowser,
    browser_limit: int,
) -> tuple[int, int]:
    browser_count = sum(
        1 for _index, site in jobs if _runtime_needs_browser(site, needs_browser)
    )
    http_count = len(jobs) - browser_count
    browser_slots = min(max(0, browser_limit), browser_count, workers)
    if browser_count and http_count and workers > 1:
        browser_slots = min(browser_slots or 1, workers - 1)
    http_slots = min(http_count, max(0, workers - browser_slots))
    if http_count and http_slots == 0:
        http_slots = 1
        browser_slots = max(0, workers - 1)
    if browser_count and browser_slots == 0:
        browser_slots = 1
        http_slots = max(0, workers - 1)
    return http_slots, browser_slots


def run_isolated_site_jobs(
    jobs: Iterable[SiteJob],
    *,
    workers: int,
    worker_callable: WorkerCallable,
    worker_args: tuple[Any, ...] = (),
    needs_browser: NeedsBrowser,
    hard_timeout: float,
    browser_hard_timeout: float | None = None,
    browser_limit: int = 3,
    headless: bool = True,
    host_keys: Iterable[str] = (),
    poll_interval: float = 0.1,
    assignment_delay: float = 0.0,
    on_start: Callable[[int, Site], None] | None = None,
    on_result: Callable[[IsolatedJobResult], None] | None = None,
) -> dict[int, IsolatedJobResult]:
    """Execute jobs with a killable deadline covering the whole task.

    Results are indexed by the original site index.  ``hard_timeout`` and
    ``browser_hard_timeout`` are wall-clock limits measured from assignment,
    including process scheduling and the first Selenium startup in a browser
    slot.
    """

    pending = list(jobs)
    if not pending:
        return {}
    if workers < 1:
        raise ValueError("workers 必须大于0")
    if hard_timeout <= 0:
        raise ValueError("hard_timeout 必须大于0")
    browser_timeout = browser_hard_timeout or hard_timeout
    if browser_timeout <= 0:
        raise ValueError("browser_hard_timeout 必须大于0")

    indexes = [index for index, _site in pending]
    if len(indexes) != len(set(indexes)):
        raise ValueError("隔离任务包含重复站点索引")

    context = multiprocessing.get_context("spawn")
    manager = context.Manager()
    shared_locks = {
        key: manager.Lock()
        for key in dict.fromkeys(str(value) for value in host_keys if str(value))
    }
    for _index, site in pending:
        shared_locks.setdefault(host_key(site.url), manager.Lock())

    result_queue = context.Queue()
    http_jobs = [
        (index, site)
        for index, site in pending
        if not _runtime_needs_browser(site, needs_browser)
    ]
    browser_jobs = [
        (index, site)
        for index, site in pending
        if _runtime_needs_browser(site, needs_browser)
    ]
    if workers == 1 and http_jobs and browser_jobs:
        # One browser-capable slot can also execute HTTP-only jobs; handlers
        # decide from the site policy whether the provided browser is used.
        browser_jobs = list(pending)
        http_jobs = []
        http_slots, browser_slots = 0, 1
    else:
        http_slots, browser_slots = _lane_counts(
            pending, workers, needs_browser, browser_limit
        )
    slots = [
        _start_slot(
            context,
            result_queue,
            worker_callable,
            worker_args,
            shared_locks,
            browser_lane,
            headless,
        )
        for browser_lane, count in ((False, http_slots), (True, browser_slots))
        for _ in range(count)
    ]
    queues = {False: http_jobs, True: browser_jobs}
    active: dict[int, dict[str, Any]] = {}
    results: dict[int, IsolatedJobResult] = {}
    token_counter = 0

    def assign(slot: dict[str, Any]) -> None:
        nonlocal token_counter
        lane_jobs = queues[slot["browser"]]
        if not lane_jobs:
            return
        index, site = lane_jobs.pop(0)
        token = token_counter
        token_counter += 1
        slot["current"] = (token, index, site)
        slot["assigned_at"] = time.monotonic()
        active[token] = slot
        if assignment_delay > 0:
            time.sleep(assignment_delay)
        if on_start is not None:
            on_start(index, site)
        slot["queue"].put((token, index, site))

    def replace_slot(slot: dict[str, Any]) -> dict[str, Any]:
        _stop_slot(slot)
        replacement = _start_slot(
            context,
            result_queue,
            worker_callable,
            worker_args,
            shared_locks,
            slot["browser"],
            headless,
        )
        slots[slots.index(slot)] = replacement
        return replacement

    try:
        for slot in slots:
            assign(slot)
        while active:
            try:
                message, token, returned_index, value, error, elapsed = result_queue.get(
                    timeout=max(0.001, poll_interval)
                )
            except queue.Empty:
                message = None

            if message == "result" and token in active:
                slot = active.pop(token)
                _expected_token, index, site = slot["current"]
                slot["current"] = None
                slot["assigned_at"] = None
                if returned_index != index:
                    value = None
                    error = (
                        "RuntimeError: 任务返回站点索引不一致: "
                        f"expected={index}, actual={returned_index}"
                    )
                result = IsolatedJobResult(
                    index=index,
                    site=site,
                    value=value,
                    error=error,
                    elapsed=float(elapsed),
                )
                results[index] = result
                if on_result is not None:
                    on_result(result)
                assign(slot)

            now = time.monotonic()
            for slot in list(slots):
                current = slot.get("current")
                if current is None:
                    continue
                token, index, site = current
                process = slot["process"]
                if not process.is_alive():
                    active.pop(token, None)
                    result = IsolatedJobResult(
                        index=index,
                        site=site,
                        value=None,
                        error="RuntimeError: 隔离进程异常退出",
                        elapsed=max(0.0, now - float(slot["assigned_at"])),
                    )
                    results[index] = result
                    if on_result is not None:
                        on_result(result)
                    assign(replace_slot(slot))
                    continue

                limit = browser_timeout if slot["browser"] else hard_timeout
                elapsed = now - float(slot["assigned_at"])
                if elapsed <= limit:
                    continue
                active.pop(token, None)
                result = IsolatedJobResult(
                    index=index,
                    site=site,
                    value=None,
                    error=f"TimeoutError: 站点任务超过硬超时{limit:g}秒，已终止进程树",
                    elapsed=elapsed,
                    timed_out=True,
                )
                results[index] = result
                if on_result is not None:
                    on_result(result)
                assign(replace_slot(slot))
    finally:
        for slot in slots:
            _stop_slot(slot, graceful=True)
        _close_queue(result_queue)
        manager.shutdown()

    missing = set(indexes) - set(results)
    for index in sorted(missing):
        site = next(site for job_index, site in pending if job_index == index)
        results[index] = IsolatedJobResult(
            index=index,
            site=site,
            value=None,
            error="RuntimeError: 隔离调度结束但任务没有结果",
            elapsed=0.0,
        )
    return results
