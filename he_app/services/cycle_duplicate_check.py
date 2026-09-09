from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Mapping
from zoneinfo import ZoneInfo

from he_app.config.settings import DEFAULT_DUPLICATE_FINGERPRINT_CACHE, SITES_FILE
from he_app.config.sites import load_sites
from he_app.domain.models import Site
from he_app.domain.periods import (
    CyclePolicy,
    PeriodContext,
    PeriodKey,
    deserialize_period_mapping,
    longest_equal_consecutive_run,
)
from he_app.services.live_validation import (
    LiveSiteResult,
    build_live_report,
    run_live_validation,
    write_live_report,
)
from he_app.storage.atomic_write import commit_text_transaction
from he_app.storage.cycle_cache import (
    DEFAULT_CYCLE_CACHE,
    CycleCacheState,
    load_cycle_cache,
    merge_cycle_fingerprints,
    write_cycle_cache,
)


@dataclass(frozen=True, slots=True)
class CyclePairMatch:
    left_index: int
    right_index: int
    periods: tuple[PeriodKey, ...]
    values: tuple[str, ...]

    @property
    def length(self) -> int:
        return len(self.periods)


def find_cycle_pair_matches(
    sites: list[Site],
    fingerprints: Mapping[int, Mapping[PeriodKey, str]],
    context: PeriodContext,
    min_length: int = 3,
) -> list[CyclePairMatch]:
    matches: list[CyclePairMatch] = []
    indexes = sorted(index for index, value in fingerprints.items() if value)
    for position, left_index in enumerate(indexes):
        for right_index in indexes[position + 1 :]:
            if sites[left_index].url == sites[right_index].url:
                continue
            periods, values = longest_equal_consecutive_run(
                fingerprints[left_index],
                fingerprints[right_index],
                context,
            )
            if len(periods) >= min_length:
                matches.append(
                    CyclePairMatch(
                        left_index,
                        right_index,
                        periods,
                        values,
                    )
                )
    return sorted(
        matches,
        key=lambda item: (-item.length, item.left_index, item.right_index),
    )


def split_cycle_matches(
    matches: list[CyclePairMatch],
) -> tuple[list[CyclePairMatch], list[CyclePairMatch]]:
    return (
        [match for match in matches if match.length >= 6],
        [match for match in matches if 3 <= match.length <= 5],
    )


def _fresh_from_live(
    results: list[LiveSiteResult],
    context: PeriodContext,
) -> tuple[dict[int, dict[PeriodKey, str]], dict[int, str]]:
    fresh: dict[int, dict[PeriodKey, str]] = {}
    errors: dict[int, str] = {}
    for result in results:
        if not result.current_ok:
            errors[result.index] = result.error or result.error_category or "current validation failed"
            continue
        try:
            fingerprint = deserialize_period_mapping(
                result.fingerprint,
                context=context,
            )
        except ValueError as exc:
            errors[result.index] = f"invalid live fingerprint: {exc}"
            continue
        if context.current not in fingerprint:
            errors[result.index] = "live result did not contain the requested current period"
            continue
        fresh[result.index] = fingerprint
    return fresh, errors


def _match_output(
    title: str,
    matches: list[CyclePairMatch],
    sites: list[Site],
) -> str:
    lines: list[str] = []
    for group_number, match in enumerate(matches, start=1):
        if lines:
            lines.append("")
        values = " ".join(
            f"{period.token()}={value}"
            for period, value in zip(match.periods, match.values)
        )
        lines.append(
            f"{title}{group_number} 连续{match.length}期 {values}"
        )
        for index in (match.left_index, match.right_index):
            site = sites[index]
            lines.append(f"{site.site_id} {site.name} {site.url}")
    return "\n".join(lines) + ("\n" if lines else "")


def _failure_output(
    sites: list[Site],
    errors: Mapping[int, str],
    context: PeriodContext,
) -> str:
    records = []
    for index, error in sorted(errors.items()):
        if not 0 <= index < len(sites):
            continue
        site = sites[index]
        records.append(
            " ".join(
                (
                    "失败",
                    site.name,
                    site.url,
                    f"站点ID: {site.site_id}",
                    f"方向: {site.pick}",
                    f"期数: {context.current.number}",
                    "失败类型: 实时验证失败",
                    f"具体原因: {str(error).replace(chr(10), ' ')[:1000]}",
                )
            )
        )
    return "\n\n".join(records) + ("\n" if records else "")


def _raw_output(
    sites: list[Site],
    fingerprints: Mapping[int, Mapping[PeriodKey, str]],
    errors: Mapping[int, str],
    context: PeriodContext,
) -> str:
    lines = [
        f"周期模式: {context.policy.mode}",
        f"基准期: {context.current.token()}",
        f"窗口: {' '.join(key.token() for key in context.window)}",
        "",
    ]
    for index, site in enumerate(sites):
        fingerprint = fingerprints.get(index, {})
        if fingerprint:
            values = " ".join(
                f"{key.token()}:{fingerprint[key]}"
                for key in context.window
                if key in fingerprint
            )
            lines.append(f"{index + 1:03d}. {site.site_id} {site.name} {values}")
        else:
            lines.append(
                f"{index + 1:03d}. {site.site_id} {site.name} 失败: "
                f"{errors.get(index, '没有可验证指纹')}"
            )
    return "\n".join(lines) + "\n"


def _default_cycle() -> int:
    return datetime.now(ZoneInfo("Asia/Tokyo")).year


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="跨周期最近M期绝杀一合指纹检测；全部现场任务使用可强杀子进程。"
    )
    parser.add_argument("--period", type=int, required=True)
    parser.add_argument("--cycle", type=int, default=_default_cycle())
    parser.add_argument("--cycle-mode", choices=("year", "fixed"), default="year")
    parser.add_argument("--cycle-length", type=int, default=None)
    parser.add_argument("--periods", type=int, default=10)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--timeout", type=int, default=20)
    parser.add_argument("--hard-timeout", type=float, default=75.0)
    parser.add_argument("--sites", default=SITES_FILE)
    parser.add_argument("--success", default=None)
    parser.add_argument("--review", default=None)
    parser.add_argument("--fail", default=None)
    parser.add_argument("--raw", default=None)
    parser.add_argument("--fingerprint-cache", default=DEFAULT_CYCLE_CACHE)
    parser.add_argument("--legacy-cache", default=DEFAULT_DUPLICATE_FINGERPRINT_CACHE)
    parser.add_argument("--no-cache-write", action="store_true")
    parser.add_argument("--show-browser", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--retries", type=int, default=0, help=argparse.SUPPRESS)
    parser.add_argument("--cache", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--cache-max-age-hours", type=float, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--mirror-limit", type=int, default=0, help=argparse.SUPPRESS)
    parser.add_argument("--no-browser-fallback", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--write-fingerprint-cache", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--compare-cache", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--live-report-dir", default=None)
    return parser


def run(args: argparse.Namespace) -> int:
    if args.periods < 1:
        raise SystemExit("--periods 必须大于等于1")
    if args.cycle_mode == "fixed" and args.cycle_length is None:
        raise SystemExit("fixed cycle mode requires --cycle-length")
    if args.compare_cache:
        raise SystemExit(
            "跨周期新增站比较请先生成独立候选配置并使用同一 --fingerprint-cache；"
            "旧 --compare-cache 模式不再允许绕过周期身份"
        )
    policy = CyclePolicy(args.cycle_mode, args.cycle_length)
    context = PeriodContext(
        PeriodKey(args.cycle, args.period),
        policy,
        args.periods,
    )
    sites = load_sites(Path(args.sites).resolve())
    cache_path = Path(args.fingerprint_cache).resolve()
    state = load_cycle_cache(
        cache_path,
        context=context,
        sites=sites,
        legacy_path=Path(args.legacy_cache).resolve(),
    )

    live_results = run_live_validation(
        sites,
        context,
        workers=max(1, args.workers),
        timeout=max(1, args.timeout),
        hard_timeout=max(1.0, args.hard_timeout),
    )
    fresh, live_errors = _fresh_from_live(live_results, context)
    fingerprints, errors = merge_cycle_fingerprints(
        state,
        fresh,
        live_errors,
    )
    state = CycleCacheState(context, sites, fingerprints, errors)

    matches = find_cycle_pair_matches(sites, fingerprints, context)
    reject_matches, review_matches = split_cycle_matches(matches)
    success_path = Path(args.success or f"{args.period}期重复网站.txt").resolve()
    review_path = Path(args.review or f"{args.period}期疑似重复网站.txt").resolve()
    fail_path = Path(args.fail or f"{args.period}期重复检测失败.txt").resolve()
    changes: dict[Path, tuple[str, str] | None] = {
        success_path: (_match_output("重复组", reject_matches, sites), "utf-8-sig"),
        review_path: (_match_output("疑似组", review_matches, sites), "utf-8-sig"),
        fail_path: (_failure_output(sites, errors, context), "utf-8-sig"),
    }
    raw_path = None
    if args.raw:
        raw_path = Path(args.raw).resolve()
        changes[raw_path] = (
            _raw_output(sites, fingerprints, errors, context),
            "utf-8-sig",
        )
    if len(set(changes)) != len(changes):
        raise SystemExit("判重输出路径不能互相覆盖")
    commit_text_transaction(changes)

    if not args.no_cache_write:
        write_cycle_cache(
            cache_path,
            state,
            fingerprints=fingerprints,
            errors=errors,
        )

    if args.live_report_dir:
        report = build_live_report(sites, context, live_results)
        write_live_report(Path(args.live_report_dir), report)

    history_complete = sum(
        len(fingerprints.get(index, {})) >= context.periods
        for index in range(len(sites))
    )
    print(
        f"完成检测 {len(sites)} 个；当期成功 {len(fresh)}；"
        f"完整{context.periods}期历史 {history_complete}；"
        f"重复拒收 {len(reject_matches)} 组；疑似 {len(review_matches)} 组；"
        f"失败 {len(errors)} 个"
    )
    print(f"重复拒收: {success_path}")
    print(f"疑似审核: {review_path}")
    print(f"失败报告: {fail_path}")
    if raw_path is not None:
        print(f"原始指纹: {raw_path}")
    if not args.no_cache_write:
        print(f"跨周期缓存: {cache_path}")
    return 0


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(run(build_arg_parser().parse_args()))


__all__ = [
    "CyclePairMatch",
    "build_arg_parser",
    "find_cycle_pair_matches",
    "main",
    "run",
    "split_cycle_matches",
]
