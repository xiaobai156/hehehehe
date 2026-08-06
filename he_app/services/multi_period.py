import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from he_app.config.settings import DEFAULT_FAILURE_OUTPUT_DIR, DEFAULT_OUTPUT_DIR, SITES_FILE
from he_app.config.sites import load_sites
from he_app.domain.models import Site
from he_app.storage.atomic_write import write_text_atomic
from he_app.storage.success_cache import normalize_site_name


@dataclass(frozen=True)
class PeriodFailure:
    category: str
    reason: str


def period_success_path(output_dir: Path, period: int) -> Path:
    return output_dir / f"{period}期-合.txt"


def period_fail_path(output_dir: Path, period: int) -> Path:
    return output_dir / f"{period}期-合-失败.txt"


def default_summary_path(output_dir: Path, periods: list[int]) -> Path:
    period_text = "-".join(str(period) for period in periods)
    return output_dir / f"{period_text}期-多期全部失败.txt"


def parse_success_names(path: Path) -> set[str]:
    if not path.exists():
        return set()

    names: set[str] = set()
    for line in path.read_text(encoding="utf-8-sig", errors="ignore").splitlines():
        match = re.match(r"^(?:0[1-9]|1[0-3])合(?:,(?:0[1-9]|1[0-3])合)*\s+(.+?)\s*$", line.strip())
        if match:
            names.add(normalize_site_name(match.group(1)))
    return names


def parse_failure_lines(path: Path, period: int) -> dict[tuple[str, str], PeriodFailure]:
    if not path.exists():
        return {}

    failures: dict[tuple[str, str], PeriodFailure] = {}
    new_pattern = re.compile(
        r"^失败\s+(.+?)\s+(https?://\S+)\s+方向:\s*(\S+)\s+期数:\s*(\d+)\s+"
        r"阶段:\s*(\S+)\s+原因:\s*(.*)$"
    )
    old_pattern = re.compile(
        rf"^{re.escape(str(period))}期\s+(.+?)\s+(https?://\S+)\s+失败类型:\s*(\S+)\s+具体原因:\s*(.*)$"
    )
    for line in path.read_text(encoding="utf-8-sig", errors="ignore").splitlines():
        normalized_line = line.strip()
        match = new_pattern.match(normalized_line)
        if match:
            name, url, _pick, line_period, category, reason = match.groups()
            if int(line_period) != period:
                continue
        else:
            match = old_pattern.match(normalized_line)
            if not match:
                continue
            name, url, category, reason = match.groups()
        failures[(normalize_site_name(name), url)] = PeriodFailure(category, reason)
    return failures


def build_multi_period_failure_lines(
    sites: list[Site],
    periods: list[int],
    success_by_period: dict[int, set[str]],
    failures_by_period: dict[int, dict[tuple[str, str], PeriodFailure]],
) -> list[str]:
    lines = ["多期全部失败汇总", f"期数: {' '.join(str(period) for period in periods)}", ""]
    failed_count = 0

    for site in sites:
        normalized_name = normalize_site_name(site.name)
        if any(normalized_name in success_by_period.get(period, set()) for period in periods):
            continue

        failed_count += 1
        lines.append(f"{failed_count}. {site.site_id} {site.name} {site.url}")
        for period in periods:
            failure = failures_by_period.get(period, {}).get((normalized_name, site.url))
            if failure is None:
                lines.append(f"  {period}期: 失败类型: 未执行 具体原因: 本期未生成失败原因")
            else:
                lines.append(f"  {period}期: 失败类型: {failure.category} 具体原因: {failure.reason}")
        lines.append("")

    if failed_count == 0:
        lines.append("无")
    return lines


def count_all_failed_sites(
    sites: list[Site],
    periods: list[int],
    success_by_period: dict[int, set[str]],
) -> int:
    count = 0
    for site in sites:
        normalized_name = normalize_site_name(site.name)
        if any(normalized_name in success_by_period.get(period, set()) for period in periods):
            continue
        count += 1
    return count


def build_single_period_command(
    period: int,
    output_dir: Path,
    failure_output_dir: Path,
    workers: int,
    timeout: int,
    sites_path: Path,
    show_browser: bool,
) -> list[str]:
    crawler_entry = Path(__file__).resolve().parents[2] / "he_crawler.py"
    command = [
        sys.executable,
        str(crawler_entry),
        "--period",
        str(period),
        "--success",
        str(period_success_path(output_dir, period)),
        "--fail",
        str(period_fail_path(failure_output_dir, period)),
        "--workers",
        str(workers),
        "--timeout",
        str(timeout),
        "--sites",
        str(sites_path),
        "--no-fingerprint-cache-sync",
    ]
    if show_browser:
        command.append("--show-browser")
    return command


def run_periods(args: argparse.Namespace) -> int:
    output_dir = Path(args.output_dir).resolve()
    failure_output_dir = Path(args.failure_output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    failure_output_dir.mkdir(parents=True, exist_ok=True)
    sites_path = Path(args.sites).resolve()
    sites = load_sites(sites_path)

    for period in args.periods:
        command = build_single_period_command(
            period,
            output_dir,
            failure_output_dir,
            max(1, args.workers),
            max(1, args.timeout),
            sites_path,
            args.show_browser,
        )
        print(f"\n[多期] 开始抓取 {period}期", flush=True)
        subprocess.run(command, check=True)

    success_by_period = {
        period: parse_success_names(period_success_path(output_dir, period))
        for period in args.periods
    }
    failures_by_period = {
        period: parse_failure_lines(period_fail_path(failure_output_dir, period), period)
        for period in args.periods
    }
    summary_path = (
        Path(args.summary).resolve()
        if args.summary
        else default_summary_path(failure_output_dir, args.periods).resolve()
    )
    lines = build_multi_period_failure_lines(sites, args.periods, success_by_period, failures_by_period)
    write_text_atomic(summary_path, "\n".join(lines) + "\n", encoding="utf-8-sig")
    failed_count = count_all_failed_sites(sites, args.periods, success_by_period)
    print(f"\n[多期] 全部失败目录 {failed_count} 个，汇总保存到: {summary_path}")
    print("[多期] 已强制关闭 recent_10_cache.json 更新")
    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="多期抓取：每期单独生成原成功/失败 txt，并汇总所有期都失败的目录。")
    parser.add_argument("periods", nargs="+", type=int, help="要抓取的多个期数，例如 187 188 189 190")
    parser.add_argument("--workers", type=int, default=8, help="单期抓取并发数量，默认 8")
    parser.add_argument("--timeout", type=int, default=20, help="单个请求超时秒数，默认 20")
    parser.add_argument("--sites", default=SITES_FILE, help="站点配置 JSON，默认 sites.json")
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR, help="输出目录")
    parser.add_argument("--failure-output-dir", default=DEFAULT_FAILURE_OUTPUT_DIR, help="失败输出目录")
    parser.add_argument("--summary", default=None, help="多期全部失败汇总文件，默认按期数自动生成")
    parser.add_argument("--show-browser", action="store_true", help="显示浏览器窗口")
    return parser


def main() -> None:
    parser = build_arg_parser()
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(run_periods(args))


if __name__ == "__main__":
    main()
