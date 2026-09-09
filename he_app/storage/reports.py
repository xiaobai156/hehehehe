import re
from collections import Counter

from he_app.domain.models import Site
from he_app.parsers.common import classify_failure, format_failure_result
from he_app.validation.period import is_valid_sum_value
from he_app.storage.failure_records import parse_failure_record, parse_failure_records, serialize_failure_record


Outcome = tuple[Site, str | None, str, str | None, list[str], str | None]
SUCCESS_LINE_RE = re.compile(
    r"^(?P<values>(?:0[1-9]|1[0-3])合(?:,(?:0[1-9]|1[0-3])合)?)\s+(?P<name>\S.*)$"
)
FAILURE_SITE_ID_RE = re.compile(r"站点ID:\s*(\S+)")
FAILURE_CATEGORY_RE = re.compile(r"失败类型:\s*(.*?)\s+具体原因:")
SUCCESS_RANKING_HEADERS = {"当前合数排行", "内容\t次数\t排名"}


def extract_current_values_from_success(result: str) -> list[str]:
    current_part = result.split(maxsplit=1)[0]
    return [value for value in re.findall(r"(?:0[1-9]|1[0-3])合", current_part) if is_valid_sum_value(value)]


def build_success_output_lines(success_lines: list[str], current_ranking_values: list[str]) -> list[str]:
    return success_lines + build_tabular_ranking_lines(current_ranking_values)


def build_tabular_ranking_lines(ranking_values: list[str]) -> list[str]:
    counts = Counter(ranking_values)
    ranking = sorted(counts.items(), key=lambda item: (-item[1], int(item[0][:2])))
    lines = ["", "内容\t次数\t排名"]
    rank = 0
    previous_count = None
    for value, count in ranking:
        if count != previous_count:
            rank += 1
            previous_count = count
        lines.append(f"{value}\t{count}\t{rank}")
    return lines


def _success_line_name(line: str) -> str:
    match = SUCCESS_LINE_RE.fullmatch(line.strip())
    if match is None:
        raise ValueError(f"成功TXT存在无法识别的数据行: {line}")
    values = match.group("values").split(",")
    if len(set(values)) != len(values) or not all(is_valid_sum_value(value) for value in values):
        raise ValueError(f"成功TXT存在非法合数行: {line}")
    return match.group("name").strip()


def _existing_success_lines(text: str) -> list[str]:
    lines: list[str] = []
    names: set[str] = set()
    for raw_line in text.lstrip("\ufeff").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line in SUCCESS_RANKING_HEADERS:
            break
        name = _success_line_name(line)
        if name in names:
            raise ValueError(f"成功TXT包含重复站点: {name}")
        names.add(name)
        lines.append(line)
    return lines


def merge_success_output_lines(existing_text: str, current_lines: list[str]) -> list[str]:
    merged = _existing_success_lines(existing_text)
    names = {_success_line_name(line) for line in merged}
    appended = False
    for line in current_lines:
        name = _success_line_name(line)
        if name in names:
            previous = next(item for item in merged if _success_line_name(item) == name)
            if extract_current_values_from_success(previous) != extract_current_values_from_success(line):
                raise ValueError(f"已有成功TXT与本次同名站结果冲突: {name}")
            continue
        names.add(name)
        merged.append(line)
        appended = True
    if existing_text.strip() and not appended:
        return existing_text.lstrip("\ufeff").splitlines()
    ranking_values = [
        value
        for line in merged
        for value in extract_current_values_from_success(line)
    ]
    return merged + build_tabular_ranking_lines(ranking_values)


def build_current_only_results(
    sites: list[Site],
    outcomes: dict[int, Outcome],
    period: int,
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


def _failure_record_identity(record: str) -> tuple[str, str]:
    parsed = parse_failure_record(record)
    if not parsed.site_id:
        raise ValueError(f"失败TXT记录缺少站点ID: {record}")
    return parsed.site_id, parsed.category


def _existing_failure_records(text: str) -> list[str]:
    return [serialize_failure_record(item) for item in parse_failure_records(text)]


def merge_failure_output(
    existing_text: str,
    current_fail_lines: list[str],
    successful_site_ids: set[str],
) -> str:
    order: list[str] = []
    records: dict[str, tuple[str, str]] = {}
    for record in _existing_failure_records(existing_text):
        site_id, category = _failure_record_identity(record)
        if site_id in records:
            raise ValueError(f"失败TXT包含重复站点ID: {site_id}")
        order.append(site_id)
        records[site_id] = (record, category)

    for site_id in successful_site_ids:
        records.pop(site_id, None)
    for record in current_fail_lines:
        site_id, category = _failure_record_identity(record)
        if site_id not in order:
            order.append(site_id)
        records[site_id] = (record, category)

    kept = [records[site_id] for site_id in order if site_id in records]
    return format_failure_output(
        [record for record, _category in kept],
        [category for _record, category in kept],
    )
