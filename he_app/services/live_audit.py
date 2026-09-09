"""Read-only production validation for every configured site.

The audit never writes the official success/failure files or fingerprint cache.
It executes the same fetch, parser, history and hard-isolation paths used by the
production duplicate checker and writes an immutable JSON/TXT evidence report
to a caller-selected directory.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from he_app.config.settings import SITES_FILE
from he_app.config.sites import load_sites
from he_app.domain.models import Site
from he_app.domain.periods import PeriodKey, TOKYO_ZONE, current_tokyo_period
from he_app.fetch.http import build_host_locks
from he_app.services.document_sources import requires_browser
from he_app.services.duplicate_check import Fingerprint, run_fingerprint_jobs
from he_app.services.isolation import IsolatedJobResult
from he_app.storage.atomic_write import commit_text_transaction

TOKYO = TOKYO_ZONE
DEFAULT_REPORT_DIR = "audit/live_validation"


def consecutive_history_count(
    fingerprint: Mapping[PeriodKey, str], base: PeriodKey
) -> int:
    count = 0
    current = base
    while current in fingerprint:
        count += 1
        current = current.previous()
    return count


def _fingerprint_payload(fingerprint: Fingerprint) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, value in sorted(
        fingerprint.items(),
        key=lambda item: item[0].ordinal if isinstance(item[0], PeriodKey) else int(item[0]),
        reverse=True,
    ):
        label = key.cache_key if isinstance(key, PeriodKey) else str(key)
        result[label] = value
    return result


def _result_status(
    *,
    fingerprint: Mapping[PeriodKey, str],
    base: PeriodKey,
    minimum_history: int,
    error: str | None,
    diagnostic: IsolatedJobResult | None,
) -> str:
    if diagnostic is not None and diagnostic.timed_out:
        return "timeout"
    if error or base not in fingerprint:
        return "failed"
    if consecutive_history_count(fingerprint, base) < minimum_history:
        return "history_incomplete"
    return "verified"


def build_live_report(
    sites: list[Site],
    fingerprints: dict[int, Fingerprint],
    errors: dict[int, str],
    diagnostics: dict[int, IsolatedJobResult],
    base: PeriodKey,
    minimum_history: int,
    *,
    source_sha256: str,
    started_at: datetime,
    finished_at: datetime,
    source_commit: str = "",
) -> dict:
    rows: list[dict] = []
    for index, site in enumerate(sites):
        raw_fingerprint = fingerprints.get(index, {})
        fingerprint = {
            key: value
            for key, value in raw_fingerprint.items()
            if isinstance(key, PeriodKey)
        }
        diagnostic = diagnostics.get(index)
        error = errors.get(index)
        status = _result_status(
            fingerprint=fingerprint,
            base=base,
            minimum_history=minimum_history,
            error=error,
            diagnostic=diagnostic,
        )
        consecutive = consecutive_history_count(fingerprint, base)
        rows.append(
            {
                "index": index,
                "site_id": site.site_id,
                "name": site.name,
                "url": site.url,
                "pick": site.pick,
                "requires_browser": requires_browser(site),
                "status": status,
                "current_value": fingerprint.get(base),
                "history_count": len(fingerprint),
                "consecutive_history_count": consecutive,
                "fingerprint": _fingerprint_payload(raw_fingerprint),
                "error": error,
                "elapsed_seconds": round(diagnostic.elapsed, 3) if diagnostic else None,
                "timed_out": bool(diagnostic and diagnostic.timed_out),
            }
        )

    counts = Counter(row["status"] for row in rows)
    completed = sum(index in diagnostics for index in range(len(sites)))
    current_verified = sum(row["current_value"] is not None and not row["error"] for row in rows)
    history_complete = sum(
        row["current_value"] is not None
        and row["consecutive_history_count"] >= minimum_history
        and not row["error"]
        for row in rows
    )
    return {
        "schema_version": 1,
        "read_only": True,
        "source_commit": source_commit,
        "sites_sha256": source_sha256,
        "started_at_utc": started_at.astimezone(timezone.utc).isoformat(),
        "finished_at_utc": finished_at.astimezone(timezone.utc).isoformat(),
        "started_at_tokyo": started_at.astimezone(TOKYO).isoformat(),
        "finished_at_tokyo": finished_at.astimezone(TOKYO).isoformat(),
        "base_period_key": base.cache_key,
        "minimum_consecutive_history": minimum_history,
        "site_count": len(sites),
        "attempted_count": len(diagnostics),
        "completed_count": completed,
        "current_verified_count": current_verified,
        "history_complete_count": history_complete,
        "status_counts": dict(sorted(counts.items())),
        "all_attempted": completed == len(sites),
        "all_current_verified": current_verified == len(sites),
        "all_history_complete": history_complete == len(sites),
        "results": rows,
    }


def build_text_summary(report: dict) -> str:
    lines = [
        "杀合全站只读生产验收报告",
        f"源码提交: {report['source_commit'] or 'unknown'}",
        f"站点配置SHA256: {report['sites_sha256']}",
        f"验收周期: {report['base_period_key']}",
        f"站点总数: {report['site_count']}",
        f"已完成任务: {report['completed_count']}",
        f"当期验证通过: {report['current_verified_count']}",
        f"连续历史达到{report['minimum_consecutive_history']}期: {report['history_complete_count']}",
        "状态统计: " + ", ".join(
            f"{key}={value}" for key, value in report["status_counts"].items()
        ),
        "",
    ]
    for row in report["results"]:
        history = f"历史{row['consecutive_history_count']}/{row['history_count']}期"
        elapsed = (
            f"{row['elapsed_seconds']:.3f}s"
            if isinstance(row["elapsed_seconds"], (int, float))
            else "未返回"
        )
        detail = row["error"] or row["current_value"] or "无当期结果"
        lines.append(
            f"{row['index'] + 1:03d}. [{row['status']}] {row['site_id']} "
            f"{row['name']} {history} {elapsed} - {detail}"
        )
    return "\n".join(lines) + "\n"


def run(args: argparse.Namespace) -> int:
    current = current_tokyo_period()
    cycle_year = args.cycle_year or current.cycle_year
    period = args.period or (current.issue if cycle_year == current.cycle_year else 1)
    try:
        base = PeriodKey(cycle_year, period)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    if args.periods < 1 or args.minimum_history < 1:
        raise SystemExit("历史窗口和最低连续历史必须为正数")
    if args.minimum_history > args.periods:
        raise SystemExit("最低连续历史不能大于抓取历史窗口")
    if args.timeout <= 0 or args.hard_timeout <= 0 or args.browser_hard_timeout <= 0:
        raise SystemExit("网络和硬超时必须为正数")
    if args.workers < 1:
        raise SystemExit("并发必须为正数")

    sites_path = Path(args.sites).resolve()
    sites_bytes = sites_path.read_bytes()
    sites = load_sites(sites_path)
    output_dir = Path(args.output_dir).resolve()
    report_path = output_dir / "report.json"
    summary_path = output_dir / "summary.txt"
    if sites_path in {report_path, summary_path}:
        raise SystemExit("验收输出不能覆盖站点配置")

    diagnostics: dict[int, IsolatedJobResult] = {}
    started = datetime.now(timezone.utc)
    fingerprints, errors = run_fingerprint_jobs(
        list(enumerate(sites)),
        sites,
        args.workers,
        base.issue,
        args.periods,
        args.timeout,
        False,
        build_host_locks(sites, {}),
        hard_timeout=args.hard_timeout,
        poll_interval=args.poll_interval,
        cycle_year=base.cycle_year,
        browser_hard_timeout=args.browser_hard_timeout,
        diagnostics=diagnostics,
    )
    finished = datetime.now(timezone.utc)
    report = build_live_report(
        sites,
        fingerprints,
        errors,
        diagnostics,
        base,
        args.minimum_history,
        source_sha256=hashlib.sha256(sites_bytes).hexdigest(),
        started_at=started,
        finished_at=finished,
        source_commit=os.environ.get("GITHUB_SHA", ""),
    )
    json_text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    text_summary = build_text_summary(report)
    commit_text_transaction(
        {
            report_path: (json_text, "utf-8"),
            summary_path: (text_summary, "utf-8-sig"),
        }
    )
    print(text_summary)

    if not report["all_attempted"]:
        return 3
    if args.require_all_current and not report["all_current_verified"]:
        return 4
    if args.require_all_history and not report["all_history_complete"]:
        return 5
    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="只读验证所有正式站点的当期、方向、来源边界和连续历史。"
    )
    parser.add_argument("--period", type=int, default=None, help="目标期数；默认东京今天的年内序号")
    parser.add_argument("--cycle-year", type=int, default=None, help="期数周期年份；默认东京当前年份")
    parser.add_argument("--periods", type=int, default=10, help="尝试验证的连续历史窗口")
    parser.add_argument("--minimum-history", type=int, default=6, help="判重完整所需的最少连续历史")
    parser.add_argument("--sites", default=SITES_FILE, help="站点配置文件")
    parser.add_argument("--output-dir", default=DEFAULT_REPORT_DIR, help="只读验收报告目录")
    parser.add_argument("--workers", type=int, default=12, help="总并发上限")
    parser.add_argument("--timeout", type=int, default=15, help="站内协作网络超时")
    parser.add_argument("--hard-timeout", type=float, default=45.0, help="HTTP站完整任务硬超时")
    parser.add_argument("--browser-hard-timeout", type=float, default=90.0, help="浏览器站完整任务硬超时")
    parser.add_argument("--poll-interval", type=float, default=0.1, help=argparse.SUPPRESS)
    parser.add_argument("--require-all-current", action="store_true", help="任一站当期未通过则返回非零")
    parser.add_argument("--require-all-history", action="store_true", help="任一站连续历史不足则返回非零")
    return parser


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(run(build_arg_parser().parse_args()))


if __name__ == "__main__":
    main()
