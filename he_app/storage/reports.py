import re
from collections import Counter

from he_app.domain.models import Site
from he_app.parsers.common import classify_failure, format_failure_result
from he_app.validation.period import is_valid_sum_value


Outcome = tuple[Site, str | None, str, str | None, list[str], str | None]


def extract_current_values_from_success(result: str) -> list[str]:
    current_part = result.split(maxsplit=1)[0]
    return [value for value in re.findall(r"(?:0[1-9]|1[0-3])合", current_part) if is_valid_sum_value(value)]


def build_ranking_lines(ranking_values: list[str], title: str = "合数排行") -> list[str]:
    counts = Counter(ranking_values)
    if not counts:
        return ["", title, "无"]
    ranking = sorted(counts.items(), key=lambda item: (-item[1], int(item[0][:2])))
    return ["", title, *[f"{rank}. {value} {count}次" for rank, (value, count) in enumerate(ranking, 1)]]


def build_previous_fail_stats(reasons: list[str]) -> list[str]:
    counts = Counter(reasons)
    lines = ["", "前一期失败统计"]
    if not counts:
        return [*lines, "无"]
    order = ["前一期错", "前一期没找到", "前一期没开奖号", "前一期没对错"]
    lines.extend(f"{reason} {counts[reason]}次" for reason in order if counts.get(reason))
    lines.extend(f"{reason} {count}次" for reason, count in sorted(counts.items()) if reason not in order)
    return lines


def build_success_output_lines(success_lines: list[str], current_ranking_values: list[str]) -> list[str]:
    return success_lines + build_ranking_lines(current_ranking_values, "当前合数排行")


def build_current_only_results(
    sites: list[Site],
    outcomes: dict[int, Outcome],
    period: int,
    existing_success: dict[str, str] | None = None,
) -> tuple[list[str], list[str], list[str], list[str]]:
    success_lines: list[str] = []
    fail_lines: list[str] = []
    failure_categories: list[str] = []
    ranking_values: list[str] = []
    for index, site in enumerate(sites):
        _site, result, detail, error, _rank_values, failure_category = outcomes.get(
            index, (site, None, "", "未执行", [], None)
        )
        if result:
            success_lines.append(result)
            ranking_values.extend(extract_current_values_from_success(result))
        else:
            fail_lines.append(format_failure_result(period, site, detail, error, failure_category))
            category = failure_category if failure_category and not error else classify_failure(period, detail, error).category
            failure_categories.append(category)
    return success_lines, fail_lines, ranking_values, failure_categories


def build_failure_stats_lines(failure_categories: list[str]) -> list[str]:
    if not failure_categories:
        return []
    counts = Counter(failure_categories)
    lines = ["失败分类统计"]
    lines.extend(
        f"{category} {count}条"
        for category, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    )
    return lines


def format_failure_output(fail_lines: list[str], failure_categories: list[str]) -> str:
    if not fail_lines:
        return ""
    stats_lines = build_failure_stats_lines(failure_categories)
    return "\n\n".join(fail_lines) + "\n\n" + "\n".join(stats_lines) + "\n"
