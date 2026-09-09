def format_progress_prefix(index: int, total: int) -> str:
    return f"[{index + 1}/{total}]"


def format_completion_progress(
    completed: int,
    total: int,
    success_count: int,
    fail_count: int,
    site_name: str,
    elapsed_seconds: float,
) -> str:
    percent = int((completed / total) * 100) if total else 100
    return (
        f"[进度 {completed}/{total} {percent}% 成功 {success_count} "
        f"失败 {fail_count} 用时 {elapsed_seconds:.1f}s] 当前: {site_name}"
    )


def build_slow_site_lines(
    timings: list[tuple[str, float, bool]],
    limit: int = 10,
) -> list[str]:
    if not timings:
        return []

    lines = ["", f"慢站耗时TOP{min(limit, len(timings))}"]
    for rank, (name, elapsed_seconds, success) in enumerate(
        sorted(timings, key=lambda item: item[1], reverse=True)[:limit],
        start=1,
    ):
        status = "成功" if success else "失败"
        lines.append(f"{rank}. {name} {elapsed_seconds:.1f}s {status}")
    return lines
