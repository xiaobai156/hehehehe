from __future__ import annotations

import argparse
import sys
from pathlib import Path

from he_app.config.settings import SITES_FILE
from he_app.config.sites import load_sites
from he_app.domain.periods import CyclePolicy, PeriodContext, PeriodKey
from he_app.runtime.process_jobs import IsolatedJobResult, run_isolated_jobs
from he_app.services.document_sources import requires_browser
from he_app.services.live_validation import LiveSiteResult
from he_app.services.live_validation_adaptive import (
    _default_cycle_period,
    _failed_result,
    _site_payload,
    build_live_report,
    write_live_report,
)


def _partition_jobs(sites, context, timeout):
    http_jobs = []
    browser_jobs = []
    for index, site in enumerate(sites):
        item = ((index, site.site_id), _site_payload(index, site, context, timeout))
        if requires_browser(site):
            browser_jobs.append(item)
        else:
            http_jobs.append(item)
    return http_jobs, browser_jobs


def _run_group(
    jobs,
    *,
    site_by_key,
    total_sites: int,
    max_workers: int,
    hard_timeout: float,
    progress: bool,
):
    if not jobs:
        return []

    def started(key):
        if progress:
            site = site_by_key[key]
            print(
                f"[LIVE START] {key[0] + 1}/{total_sites} {site.site_id} {site.name}",
                flush=True,
            )

    def finished(item):
        if progress:
            site = site_by_key[item.key]
            status = "OK" if item.ok else "FAIL"
            suffix = f" {item.error.splitlines()[0]}" if item.error else ""
            print(
                f"[LIVE {status}] {site.site_id} {site.name} {item.elapsed:.2f}s{suffix}",
                flush=True,
            )

    return run_isolated_jobs(
        jobs,
        worker_path="he_app.services.live_validation_adaptive:validate_live_site",
        max_workers=max(1, max_workers),
        hard_timeout=hard_timeout,
        poll_interval=0.1,
        on_started=started,
        on_finished=finished,
    )


def run_live_validation(
    sites,
    context,
    *,
    workers: int,
    browser_workers: int = 3,
    timeout: int,
    hard_timeout: float,
    progress: bool = True,
):
    """Validate all sites while preventing browser overcommit.

    HTTP/API/list sites retain the requested worker count.  Sites that may
    launch Selenium are isolated into a second pool capped at three workers,
    matching the production browser pool ceiling.  Each site still has its own
    hard deadline and unchanged strict parsing/period/direction rules.
    """

    if workers < 1:
        raise ValueError("workers must be positive")
    if browser_workers < 1:
        raise ValueError("browser_workers must be positive")

    http_jobs, browser_jobs = _partition_jobs(sites, context, timeout)
    site_by_key = {
        (index, site.site_id): site for index, site in enumerate(sites)
    }
    if progress:
        print(
            f"[LIVE POOLS] http={len(http_jobs)} workers={workers}; "
            f"browser={len(browser_jobs)} workers={min(browser_workers, 3)}",
            flush=True,
        )

    isolated_results = []
    isolated_results.extend(
        _run_group(
            http_jobs,
            site_by_key=site_by_key,
            total_sites=len(sites),
            max_workers=workers,
            hard_timeout=hard_timeout,
            progress=progress,
        )
    )
    isolated_results.extend(
        _run_group(
            browser_jobs,
            site_by_key=site_by_key,
            total_sites=len(sites),
            max_workers=min(browser_workers, 3),
            hard_timeout=hard_timeout,
            progress=progress,
        )
    )

    by_key = {item.key: item for item in isolated_results}
    all_jobs = [*http_jobs, *browser_jobs]
    results = []
    for key, _payload in all_jobs:
        site = site_by_key[key]
        isolated = by_key.get(key) or IsolatedJobResult(
            key=key,
            ok=False,
            error="RuntimeError: validation scheduler lost the site result",
        )
        if not isolated.ok or not isinstance(isolated.value, dict):
            results.append(_failed_result(key, site, context, isolated))
            continue
        try:
            raw = dict(isolated.value)
            raw["source_urls"] = tuple(raw.get("source_urls", ()))
            raw["authority_ids"] = tuple(raw.get("authority_ids", ()))
            results.append(LiveSiteResult(**raw))
        except (TypeError, ValueError) as exc:
            results.append(
                _failed_result(
                    key,
                    site,
                    context,
                    IsolatedJobResult(
                        key=key,
                        ok=False,
                        error=f"invalid worker result: {type(exc).__name__}: {exc}",
                        elapsed=isolated.elapsed,
                    ),
                )
            )

    if len(results) != len(sites) or {
        item.site_id for item in results
    } != {site.site_id for site in sites}:
        raise RuntimeError("live validation did not account for every configured site")
    return sorted(results, key=lambda item: item.index)


def build_arg_parser():
    default_cycle, default_period = _default_cycle_period()
    parser = argparse.ArgumentParser(
        description=(
            "逐个真实访问全部配置站点；HTTP/API与浏览器分池，浏览器最多3并发。"
        )
    )
    parser.add_argument("--period", type=int, default=default_period)
    parser.add_argument("--cycle", type=int, default=default_cycle)
    parser.add_argument("--cycle-mode", choices=("year", "fixed"), default="year")
    parser.add_argument("--cycle-length", type=int, default=None)
    parser.add_argument("--periods", type=int, default=10)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--browser-workers", type=int, default=3)
    parser.add_argument("--timeout", type=int, default=20)
    parser.add_argument("--hard-timeout", type=float, default=90.0)
    parser.add_argument("--sites", default=SITES_FILE)
    parser.add_argument("--output-dir", default="live-validation")
    parser.add_argument("--require-all-current", action="store_true")
    parser.add_argument("--require-all-history", action="store_true")
    return parser


def run(args):
    if args.cycle_mode == "fixed" and args.cycle_length is None:
        raise SystemExit("fixed cycle mode requires --cycle-length")
    if args.browser_workers < 1:
        raise SystemExit("--browser-workers 必须大于0")

    context = PeriodContext(
        PeriodKey(args.cycle, args.period),
        CyclePolicy(args.cycle_mode, args.cycle_length),
        args.periods,
    )
    sites = load_sites(Path(args.sites).resolve())
    results = run_live_validation(
        sites,
        context,
        workers=args.workers,
        browser_workers=args.browser_workers,
        timeout=args.timeout,
        hard_timeout=args.hard_timeout,
    )
    report = build_live_report(sites, context, results)
    json_path, markdown_path = write_live_report(Path(args.output_dir), report)
    print(
        f"[LIVE DONE] attempted={report['attempted_count']}/{report['site_count']} "
        f"current={report['current_success_count']} "
        f"history_complete={report['history_complete_count']} "
        f"hard_timeouts={report['hard_timeout_count']}",
        flush=True,
    )
    print(f"[LIVE REPORT] {json_path.resolve()}")
    print(f"[LIVE REPORT] {markdown_path.resolve()}")
    if report["attempted_count"] != report["site_count"]:
        return 2
    if args.require_all_current and report["current_success_count"] != report["site_count"]:
        return 3
    if args.require_all_history and report["history_complete_count"] != report["site_count"]:
        return 4
    return 0


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(run(build_arg_parser().parse_args()))


__all__ = [
    "build_arg_parser",
    "main",
    "run",
    "run_live_validation",
]
