from __future__ import annotations

import importlib
import os
import queue
import signal
import subprocess
import time
import traceback
from dataclasses import dataclass
from multiprocessing import get_context
from multiprocessing.context import BaseContext
from multiprocessing.process import BaseProcess
from typing import Any, Callable, Iterable


@dataclass(frozen=True, slots=True)
class IsolatedJobResult:
    key: Any
    ok: bool
    value: Any = None
    error: str | None = None
    elapsed: float = 0.0
    timed_out: bool = False


def _load_callable(path: str) -> Callable[[Any], Any]:
    module_name, separator, attribute = path.partition(":")
    if not separator or not module_name or not attribute:
        raise ValueError(f"worker path must be module:function, got {path!r}")
    value = getattr(importlib.import_module(module_name), attribute)
    if not callable(value):
        raise TypeError(f"worker is not callable: {path}")
    return value


def _become_process_group_leader() -> None:
    if os.name != "nt":
        try:
            os.setsid()
        except OSError:
            pass


def _child_entry(result_queue, token: int, worker_path: str, payload: Any) -> None:
    _become_process_group_leader()
    started = time.monotonic()
    try:
        worker = _load_callable(worker_path)
        value = worker(payload)
    except BaseException as exc:  # child must report SystemExit and browser errors too
        result_queue.put(
            (
                token,
                False,
                None,
                f"{type(exc).__name__}: {exc}",
                traceback.format_exc(limit=20),
                time.monotonic() - started,
            )
        )
    else:
        result_queue.put(
            (token, True, value, None, None, time.monotonic() - started)
        )


def terminate_process_tree(process: BaseProcess, grace_seconds: float = 1.0) -> None:
    """Terminate a job and every browser/curl process created under it."""

    if process.pid is None:
        return
    pid = process.pid
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=max(2.0, grace_seconds + 1.0),
            )
        except (OSError, subprocess.SubprocessError):
            pass
    else:
        try:
            process_group = os.getpgid(pid)
        except (OSError, ProcessLookupError):
            process_group = None
        if process_group is not None:
            try:
                os.killpg(process_group, signal.SIGTERM)
            except (OSError, ProcessLookupError):
                pass
        else:
            try:
                process.terminate()
            except (OSError, AttributeError):
                pass

    process.join(timeout=max(0.0, grace_seconds))
    if process.is_alive():
        if os.name == "nt":
            try:
                process.kill()
            except (OSError, AttributeError):
                process.terminate()
        else:
            try:
                process_group = os.getpgid(pid)
                os.killpg(process_group, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                try:
                    process.kill()
                except (OSError, AttributeError):
                    pass
        process.join(timeout=1.0)


@dataclass(slots=True)
class _RunningJob:
    token: int
    key: Any
    process: BaseProcess
    started_at: float


def _start_job(
    context: BaseContext,
    result_queue,
    token: int,
    key: Any,
    payload: Any,
    worker_path: str,
) -> _RunningJob:
    process = context.Process(
        target=_child_entry,
        args=(result_queue, token, worker_path, payload),
        daemon=False,
    )
    started_at = time.monotonic()
    process.start()
    return _RunningJob(token, key, process, started_at)


def run_isolated_jobs(
    jobs: Iterable[tuple[Any, Any]],
    *,
    worker_path: str,
    max_workers: int,
    hard_timeout: float,
    poll_interval: float = 0.1,
    on_started: Callable[[Any], None] | None = None,
    on_finished: Callable[[IsolatedJobResult], None] | None = None,
) -> list[IsolatedJobResult]:
    """Run each network job in its own killable process.

    The timeout clock begins before ``Process.start`` returns, so DNS lookup,
    TLS negotiation, browser start-up, page loading and parsing are all inside
    the same hard deadline.  A timed-out job cannot keep a browser orphaned in
    the background.
    """

    if max_workers < 1:
        raise ValueError("max_workers must be positive")
    if hard_timeout <= 0:
        raise ValueError("hard_timeout must be positive")

    pending = list(jobs)
    if not pending:
        return []
    context = get_context("spawn")
    result_queue = context.Queue()
    running: dict[int, _RunningJob] = {}
    results: dict[int, IsolatedJobResult] = {}
    next_token = 0

    def fill_slots() -> None:
        nonlocal next_token
        while pending and len(running) < max_workers:
            key, payload = pending.pop(0)
            token = next_token
            next_token += 1
            running[token] = _start_job(
                context,
                result_queue,
                token,
                key,
                payload,
                worker_path,
            )
            if on_started is not None:
                on_started(key)

    def finish(result: IsolatedJobResult) -> None:
        results[int(result.key[0]) if isinstance(result.key, tuple) and result.key and isinstance(result.key[0], int) else len(results)] = result
        if on_finished is not None:
            on_finished(result)

    fill_slots()
    try:
        while running:
            try:
                token, ok, value, error, child_traceback, child_elapsed = result_queue.get(
                    timeout=max(0.01, poll_interval)
                )
            except queue.Empty:
                token = None
            if token is not None:
                slot = running.pop(token, None)
                if slot is not None:
                    slot.process.join(timeout=1.0)
                    if slot.process.is_alive():
                        terminate_process_tree(slot.process)
                    detail = error
                    if detail and child_traceback:
                        detail = f"{detail}\n{child_traceback}"
                    result = IsolatedJobResult(
                        key=slot.key,
                        ok=bool(ok),
                        value=value,
                        error=detail,
                        elapsed=max(float(child_elapsed), time.monotonic() - slot.started_at),
                        timed_out=False,
                    )
                    finish(result)
                    fill_slots()

            now = time.monotonic()
            for current_token, slot in list(running.items()):
                if now - slot.started_at > hard_timeout:
                    running.pop(current_token, None)
                    terminate_process_tree(slot.process)
                    finish(
                        IsolatedJobResult(
                            key=slot.key,
                            ok=False,
                            error=f"TimeoutError: hard deadline {hard_timeout:g}s exceeded",
                            elapsed=now - slot.started_at,
                            timed_out=True,
                        )
                    )
                    fill_slots()
                    continue
                if not slot.process.is_alive():
                    # Give a just-written queue message one short chance to arrive.
                    try:
                        queued = result_queue.get_nowait()
                    except queue.Empty:
                        queued = None
                    if queued is not None:
                        queued_token, ok, value, error, child_traceback, child_elapsed = queued
                        queued_slot = running.pop(queued_token, None)
                        if queued_slot is not None:
                            detail = error
                            if detail and child_traceback:
                                detail = f"{detail}\n{child_traceback}"
                            finish(
                                IsolatedJobResult(
                                    key=queued_slot.key,
                                    ok=bool(ok),
                                    value=value,
                                    error=detail,
                                    elapsed=max(
                                        float(child_elapsed),
                                        time.monotonic() - queued_slot.started_at,
                                    ),
                                )
                            )
                            fill_slots()
                        continue
                    running.pop(current_token, None)
                    slot.process.join(timeout=0.2)
                    finish(
                        IsolatedJobResult(
                            key=slot.key,
                            ok=False,
                            error=(
                                "RuntimeError: isolated job exited without a result "
                                f"(exitcode={slot.process.exitcode})"
                            ),
                            elapsed=now - slot.started_at,
                        )
                    )
                    fill_slots()
    finally:
        for slot in running.values():
            terminate_process_tree(slot.process)
        try:
            result_queue.close()
            result_queue.join_thread()
        except (OSError, ValueError):
            pass

    # Preserve caller order rather than process completion order.
    ordered_keys = [key for key, _payload in jobs] if not isinstance(jobs, list) else [key for key, _payload in jobs]
    by_key = {result.key: result for result in results.values()}
    return [by_key[key] for key in ordered_keys if key in by_key]


__all__ = ["IsolatedJobResult", "run_isolated_jobs", "terminate_process_tree"]
