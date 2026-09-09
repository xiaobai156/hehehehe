from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from he_app.config.settings import SITES_FILE
from he_app.config.sites import load_sites
from he_app.domain.models import Site
from he_app.domain.periods import CyclePolicy, PeriodContext, PeriodKey, serialize_period_mapping
from he_app.fetch.browser import BrowserClient
from he_app.fetch.discovery import collect_documents
from he_app.fetch.http import create_session
from he_app.fetch.network_policy import resolve_browser_pin, resolve_public_target
from he_app.runtime.process_jobs import IsolatedJobResult, run_isolated_jobs
from he_app.services.cycle_fingerprint import build_cycle_site_fingerprint, history_adapter_kind
from he_app.services.document_sources import collect_special_site_documents


@dataclass(frozen=True, slots=True)
class LiveSiteResult:
    index: int
    site_id: str
    name: str
    url: str
    pick: str
    browser: bool
    attempted: bool
    current_ok: bool
    current_value: str | None
    history_count: int
    history_required: int
    history_complete: bool
    fingerprint: dict[str, str]
    adapter: str
    error_category: str | None
    error: str | None
    elapsed: float
    source_urls: tuple[str, ...]
    authority_ids: tuple[str, ...]
    peer_evidence: dict[str, Any]


def _site_payload(index: int, site: Site, context: PeriodContext, timeout: int) -> dict[str, Any]:
    return {
        "index": index,
        "site": {
            "name": site.name,
            "url": site.url,
            "pick": site.pick,
            "browser": bool(site.browser),
            "click_first": bool(site.click_first),
            "site_id": site.site_id,
            "value_count": int(getattr(site, "value_count", 1)),
        },
        "cycle": context.current.cycle,
        "period": context.current.number,
        "cycle_mode": context.policy.mode,
        "cycle_length": context.policy.fixed_length,
        "periods": context.periods,
        "timeout": timeout,
    }


def _site_from_payload(raw: dict[str, Any]) -> Site:
    return Site(
        name=str(raw["name"]),
        url=str(raw["url"]),
        pick=str(raw["pick"]),
        browser=bool(raw["browser"]),
        click_first=bool(raw["click_first"]),
        site_id=str(raw["site_id"]),
        value_count=int(raw.get("value_count", 1)),
    )


def _document_sources(documents: list[str]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    urls: list[str] = []
    authorities: list[str] = []
    for document in documents:
        source = str(getattr(document, "source_url", "") or "")
        authority = str(getattr(document, "authority_id", "") or "")
        if source and source not in urls:
            urls.append(source)
        if authority and authority not in authorities:
            authorities.append(authority)
    return tuple(urls), tuple(authorities)


def validate_live_site(payload: dict[str, Any]) -> dict[str, Any]:
    """Picklable child-process worker for one configured site."""

    started = time.monotonic()
    index = int(payload["index"])
    site = _site_from_payload(dict(payload["site"]))
    policy = CyclePolicy(
        mode=str(payload["cycle_mode"]),
        fixed_length=(
            None
            if payload.get("cycle_length") in (None, "")
            else int(payload["cycle_length"])
        ),
    )
    context = PeriodContext(
        PeriodKey(int(payload["cycle"]), int(payload["period"])),
        policy,
        int(payload["periods"]),
    )
    timeout = int(payload["timeout"])
    browser: BrowserClient | None = None
    documents: list[str] = []
    peer_evidence: dict[str, Any] = {}
    try:
        target = resolve_public_target(site.url)
        peer_evidence["entry_dns"] = list(target.addresses)
        if site.browser:
            pinned_host, pinned_ip = resolve_browser_pin(site.url)
            os.environ["HE_BROWSER_PINNED_HOST"] = pinned_host
            os.environ["HE_BROWSER_PINNED_IP"] = pinned_ip
            peer_evidence["browser_pin"] = {
                "host": pinned_host,
                "ip": pinned_ip,
            }

        with create_session() as session:
            documents = collect_special_site_documents(
                session,
                site,
                timeout,
                context.current.number,
            ) or []
            if not documents:
                if site.browser:
                    browser = BrowserClient(headless=True)
                    browser.start()
                    documents = browser.get_documents(
                        site.url,
                        context.current.number,
                        site.click_first,
                        timeout,
                    )
                else:
                    documents = collect_documents(session, site.url, timeout)
            session_evidence = getattr(session, "last_network_evidence", None)
            if isinstance(session_evidence, dict):
                peer_evidence["last_http"] = dict(session_evidence)

        result = build_cycle_site_fingerprint(site, documents, context)
        source_urls, authority_ids = _document_sources(documents)
        return asdict(
            LiveSiteResult(
                index=index,
                site_id=site.site_id,
                name=site.name,
                url=site.url,
                pick=site.pick,
                browser=site.browser,
                attempted=True,
                current_ok=True,
                current_value=result.current_value,
                history_count=len(result.fingerprint),
                history_required=context.periods,
                history_complete=len(result.fingerprint) >= context.periods,
                fingerprint=serialize_period_mapping(
                    result.fingerprint,
                    context=context,
                ),
                adapter=result.adapter,
                error_category=("历史不足" if result.history_error else None),
                error=result.history_error,
                elapsed=time.monotonic() - started,
                source_urls=source_urls,
                authority_ids=authority_ids,
                peer_evidence=peer_evidence,
            )
        )
    finally:
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass
        os.environ.pop("HE_BROWSER_PINNED_HOST", None)
        os.environ.pop("HE_BROWSER_PINNED_IP", None)


def _failed_result(
    key: tuple[int, str],
    site: Site,
    context: PeriodContext,
    isolated: IsolatedJobResult,
) -> LiveSiteResult:
    category = "硬超时" if isolated.timed_out else "执行失败"
    error = (isolated.error or "isolated worker returned no result").strip()
    return LiveSiteResult(
        index=key[0],
        site_id=site.site_id,
        name=site.name,
        url=site.url,
        pick=site.pick,
        browser=site.browser,
        attempted=True,
        current_ok=False,
        current_value=None,
        history_count=0,
        history_required=context.periods,
        history_complete=False,
        fingerprint={},
        adapter=history_adapter_kind(site),
        error_category=category,
        error=error,
        elapsed=isolated.elapsed,
        source_urls=(),
        authority_ids=(),
        peer_evidence={},
    )


def run_live_validation(
    sites: list[Site],
    context: PeriodContext,
    *,
    workers: int,
    timeout: int,
    hard_timeout: float,
    progress: bool = True,
) -> list[LiveSiteResult]:
    jobs = [
        ((index, site.site_id), _site_payload(index, site, context, timeout))
        for index, site in enumerate(sites)
    ]
    site_by_key = {
        (index, site.site_id): site for index, site in enumerate(sites)
    }

    def started(key: tuple[int, str]) -> None:
        if progress:
            site = site_by_key[key]
            print(f"[LIVE START] {key[0] + 1}/{len(sites)} {site.site_id} {site.name}", flush=True)

    def finished(result: IsolatedJobResult) -> None:
        if not progress:
            return
        site = site_by_key[result.key]
        status = "OK" if result.ok else "FAIL"
        print(
            f"[LIVE {status}] {site.site_id} {site.name} {result.elapsed:.2f}s"
            + (f" {result.error.splitlines()[0]}" if result.error else ""),
            flush=True,
        )

    isolated_results = run_isolated_jobs(
        jobs,
        worker_path="he_app.services.live_validation:validate_live_site",
        max_workers=max(1, workers),
        hard_timeout=hard_timeout,
        poll_interval=0.1,
        on_started=started,
        on_finished=finished,
    )
    by_key = {result.key: result for result in isolated_results}
    results: list[LiveSiteResult] = []
    for key, _payload in jobs:
        isolated = by_key.get(key)
        site = site_by_key[key]
        if isolated is None:
            isolated = IsolatedJobResult(
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
    if len(results) != len(sites) or {item.site_id for item in results} != {site.site_id for site in sites}:
        raise RuntimeError("live validation did not account for every configured site")
    return sorted(results, key=lambda item: item.index)


def build_live_report(
    sites: list[Site],
    context: PeriodContext,
    results: list[LiveSiteResult],
) -> dict[str, Any]:
    categories = Counter(item.error_category or "成功" for item in results)
    adapters = Counter(item.adapter for item in results)
    return {
        "schema_version": 1,
        "generated_at": datetime.now(ZoneInfo("Asia/Tokyo")).isoformat(),
        "cycle_mode": context.policy.mode,
        "cycle_length": context.policy.fixed_length,
        "cycle": context.current.cycle,
        "period": context.current.number,
        "period_key": context.current.token(),
        "history_required": context.periods,
        "site_count": len(sites),
        "attempted_count": sum(item.attempted for item in results),
        "current_success_count": sum(item.current_ok for item in results),
        "history_complete_count": sum(item.history_complete for item in results),
        "hard_timeout_count": sum(item.error_category == "硬超时" for item in results),
        "categories": dict(sorted(categories.items())),
        "adapters": dict(sorted(adapters.items())),
        "results": [asdict(item) for item in results],
    }


def _markdown_report(report: dict[str, Any]) -> str:
    lines = [
        "# 全站真实验收报告",
        "",
        f"- 生成时间：`{report['generated_at']}`",
        f"- 目标周期/期数：`{report['period_key']}`",
        f"- 配置站点：`{report['site_count']}`",
        f"- 已实际尝试：`{report['attempted_count']}`",
        f"- 当期验证成功：`{report['current_success_count']}`",
        f"- 完整历史达标：`{report['history_complete_count']}` / `{report['site_count']}`",
        f"- 硬超时：`{report['hard_timeout_count']}`",
        "",
        "| 序号 | 站点ID | 名称 | 当期 | 历史 | 适配器 | 状态/原因 |",
        "|---:|---|---|---|---:|---|---|",
    ]
    for item in report["results"]:
        status = "成功" if item["current_ok"] else (item["error_category"] or "失败")
        reason = str(item.get("error") or "").replace("|", "\\|").replace("\n", " ")[:240]
        lines.append(
            f"| {item['index'] + 1} | `{item['site_id']}` | {item['name']} | "
            f"{item['current_value'] or '-'} | {item['history_count']}/{item['history_required']} | "
            f"{item['adapter']} | {status}{(': ' + reason) if reason else ''} |"
        )
    return "\n".join(lines) + "\n"


def write_live_report(output_dir: Path, report: dict[str, Any]) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "live-validation.json"
    markdown_path = output_dir / "live-validation.md"
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    markdown_path.write_text(_markdown_report(report), encoding="utf-8")
    return json_path, markdown_path


def _default_cycle_period() -> tuple[int, int]:
    now = datetime.now(ZoneInfo("Asia/Tokyo"))
    return now.year, now.timetuple().tm_yday


def build_arg_parser() -> argparse.ArgumentParser:
    default_cycle, default_period = _default_cycle_period()
    parser = argparse.ArgumentParser(
        description="逐个真实访问全部配置站点，验证当期、来源、历史和硬超时；不写正式输出或缓存。"
    )
    parser.add_argument("--period", type=int, default=default_period)
    parser.add_argument("--cycle", type=int, default=default_cycle)
    parser.add_argument("--cycle-mode", choices=("year", "fixed"), default="year")
    parser.add_argument("--cycle-length", type=int, default=None)
    parser.add_argument("--periods", type=int, default=10)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--timeout", type=int, default=20)
    parser.add_argument("--hard-timeout", type=float, default=75.0)
    parser.add_argument("--sites", default=SITES_FILE)
    parser.add_argument("--output-dir", default="live-validation")
    parser.add_argument(
        "--require-all-current",
        action="store_true",
        help="外部站点任一当期失败时返回非零；报告无论如何都会完整写出",
    )
    parser.add_argument(
        "--require-all-history",
        action="store_true",
        help="任一站历史不足指定数量时返回非零",
    )
    return parser


def run(args: argparse.Namespace) -> int:
    if args.cycle_mode == "fixed" and args.cycle_length is None:
        raise SystemExit("fixed cycle mode requires --cycle-length")
    policy = CyclePolicy(args.cycle_mode, args.cycle_length)
    context = PeriodContext(
        PeriodKey(args.cycle, args.period),
        policy,
        args.periods,
    )
    sites = load_sites(Path(args.sites).resolve())
    results = run_live_validation(
        sites,
        context,
        workers=args.workers,
        timeout=args.timeout,
        hard_timeout=args.hard_timeout,
    )
    report = build_live_report(sites, context, results)
    json_path, markdown_path = write_live_report(Path(args.output_dir), report)
    print(
        f"[LIVE DONE] attempted={report['attempted_count']}/{report['site_count']} "
        f"current={report['current_success_count']} history_complete={report['history_complete_count']} "
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


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(run(build_arg_parser().parse_args()))


__all__ = [
    "LiveSiteResult",
    "build_arg_parser",
    "build_live_report",
    "main",
    "run",
    "run_live_validation",
    "validate_live_site",
    "write_live_report",
]
