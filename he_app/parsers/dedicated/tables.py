import re

# ruff: noqa: F403,F405

from bs4 import BeautifulSoup

from he_app.domain.errors import DedicatedCandidateConflict
from he_app.domain.models import Candidate, Site, SiteRule
from he_app.domain.policies import normalize_digit_text, normalize_pick, normalize_text
from he_app.parsers.common import *
from he_app.parsers.policies import SITE_RULES, VALUE_RE
from he_app.validation.conflict import select_unique_candidate
from he_app.validation.direction import directional_window


_TABLE_SECTION_STOP_RE = re.compile(
    r"澳门六合彩合数属性|澳彩合数属性|合数属性|其他栏目|上一篇|下一篇|Copyright"
)


def clean_open_text(text: str) -> str:
    return re.sub(r"[\s:：,，。;；\[\]()（）【】{}<>《》]+", "", text).strip()


def parse_open_status(line: str) -> tuple[str | None, str | None]:
    line = normalize_text(line)
    if "开" not in line:
        return None, None
    tail = line.split("开", 1)[1]
    status_match = re.search(r"(错|准|对|中)", tail)
    if status_match is None:
        open_text = clean_open_text(tail)
        return (open_text or None), None
    status = status_match.group(1)
    open_text = clean_open_text(tail[: status_match.start()])
    return (open_text or None), status


def has_valid_open_text(open_text: str | None) -> bool:
    if not open_text:
        return False
    compact = normalize_digit_text(open_text)
    if compact in {"0", "00", "000", "0000", "?", "??", "???", "？", "？？", "？？？"}:
        return False
    if set(compact) <= {"?", "？", "0", "０"}:
        return False
    return bool(re.search(r"\d|[鼠牛虎兔龙蛇马羊猴鸡狗猪]", compact))


def extract_values_from_kill_sum_row(text: str) -> list[str]:
    normalized = normalize_digit_text(normalize_text(text))
    values: list[str] = []
    for match in VALUE_RE.finditer(normalized):
        value = int(normalize_digit_text(match.group(1)))
        if 1 <= value <= 13:
            item = f"{value:02d}合"
            if item not in values:
                values.append(item)
    return values


def format_success_result(site: Site, period: int, current: Candidate) -> str:
    return f"{current.values} {site.name}"


def site_rule(site: Site) -> SiteRule:
    return SITE_RULES.get(site.site_id, SiteRule())


def select_dedicated_candidate(
    matches: list[Candidate],
    pick: str,
    detect_conflict: bool = True,
) -> Candidate | None:
    return select_unique_candidate(matches, pick, detect_conflict)


def select_directional_candidate_group(groups: list[list[Candidate]], pick: str) -> Candidate | None:
    nonempty_groups = [group for group in groups if group]
    if not nonempty_groups:
        return None
    selected = directional_window(nonempty_groups, pick, 1)[0]
    edge = directional_window(selected, pick, 1)
    return select_dedicated_candidate(edge, pick, detect_conflict=False)


def has_period_cycle_boundary(text: str) -> bool:
    if re.search(r"分隔|另一个资料块|另一(?:个|组|版)资料", text):
        return True
    periods = [int(value) for value in re.findall(r"(?<!\d)(\d{1,4})\s*期", text)]
    return any(
        abs(current - previous) > 180
        for previous, current in zip(periods, periods[1:], strict=False)
    )


TableBlock = list[tuple[int, Candidate]]


def _tongtian_table_blocks(documents: list[str]) -> list[TableBlock]:
    blocks: list[TableBlock] = []
    order = 0
    for document in documents:
        soup = BeautifulSoup(document, "html.parser")
        tables = soup.find_all("table")
        table_texts = [table.get_text(" ", strip=True) for table in tables]
        if not table_texts:
            table_texts = [soup.get_text(" ", strip=True) if soup.find() else document]

        for table_text in table_texts:
            normalized = normalize_digit_text(normalize_text(table_text))
            marker_index = normalized.find("澳门综合杀")
            if marker_index < 0 or "杀合" not in normalized[marker_index : marker_index + 80]:
                continue
            section = normalized[marker_index:]
            stop_match = _TABLE_SECTION_STOP_RE.search(section)
            if stop_match is not None:
                section = section[: stop_match.start()]

            block: TableBlock = []
            for segment in split_all_period_segments(section):
                periods = period_numbers_in_text(segment)
                values = extract_values_from_kill_sum_row(segment)
                if len(periods) != 1 or len(values) != 1:
                    continue
                row_period = periods[0]
                block.append(
                    (
                        row_period,
                        Candidate(
                            values=values[0],
                            line=normalize_text(
                                f"{row_period}期:通天综合杀 绝杀一合[{values[0]}] {segment}"
                            ),
                            score=120,
                            order=order,
                        ),
                    )
                )
                order += 1
            if block:
                blocks.append(block)
    return _deduplicate_table_blocks(blocks)


def find_tongtian_kill_sum_candidate_with_direction(
    documents: list[str], period: int, pick: str = "top"
) -> tuple[Candidate | None, bool]:
    return _select_table_period_candidate(_tongtian_table_blocks(documents), period, pick)


def _deduplicate_table_blocks(blocks: list[TableBlock]) -> list[TableBlock]:
    unique: list[TableBlock] = []
    signatures: set[tuple[tuple[int, str], ...]] = set()
    for block in blocks:
        signature = tuple((period, candidate.values) for period, candidate in block)
        if not signature or signature in signatures:
            continue
        signatures.add(signature)
        unique.append(block)
    return unique


def _select_table_period_candidate(
    blocks: list[TableBlock],
    period: int,
    pick: str,
) -> tuple[Candidate | None, bool]:
    normalized_pick = normalize_pick(pick)
    target_found = any(row_period == period for block in blocks for row_period, _candidate in block)
    nonempty_blocks = [block for block in blocks if block]
    if not nonempty_blocks:
        return None, target_found

    # A table block is an independent candidate sequence. Direction selects
    # the first/last block, then only that block's first/last valid row can be
    # eligible. Interior rows cannot conflict with or rescue the edge row.
    selected_block = directional_window(nonempty_blocks, normalized_pick, 1)[0]
    edge = directional_window(selected_block, normalized_pick, 1)
    matches = [
        candidate
        for row_period, candidate in edge
        if row_period == period
    ]
    return select_dedicated_candidate(matches, normalized_pick, detect_conflict=False), target_found and not matches


def _table_period_values(blocks: list[TableBlock]) -> dict[int, str]:
    values: dict[int, str] = {}
    lines: dict[int, list[str]] = {}
    for block in blocks:
        for period, candidate in block:
            existing = values.get(period)
            if existing is not None and existing != candidate.values:
                raise DedicatedCandidateConflict(
                    sorted({existing, candidate.values}),
                    [*lines.get(period, []), candidate.line],
                )
            values[period] = candidate.values
            lines.setdefault(period, []).append(candidate.line)
    return values


def _jiuxiao_table_blocks(documents: list[str]) -> list[TableBlock]:
    anchor_re = re.compile(r"(?:绝|絕)\s*(?:杀|殺)\s*①\s*段\s*①\s*合")
    next_anchor_re = re.compile(r"(?:绝|絕)\s*(?:杀|殺)\s*三\s*尾")
    row_re = re.compile(
        r"(?<!\d)(\d{1,4})\s*期\s*(?:杀|殺)\s*\[\s*(0?[1-9]|1[0-3])\s*合\s*\]\s*(?:开|開)",
        re.I,
    )
    blocks: list[TableBlock] = []
    for document in documents:
        soup = BeautifulSoup(document, "html.parser")
        text = soup.get_text(" ", strip=True) if soup.find() else document
        text = normalize_digit_text(normalize_text(text))
        anchor = anchor_re.search(text)
        if anchor is None:
            continue
        section = text[anchor.start():]
        next_anchor = next_anchor_re.search(section, anchor.end() - anchor.start())
        if next_anchor is not None:
            section = section[: next_anchor.start()]

        block: TableBlock = []
        for match in row_re.finditer(section):
            period = int(match.group(1))
            value = f"{int(match.group(2)):02d}合"
            block.append(
                (
                    period,
                    Candidate(
                        values=value,
                        line=normalize_text(f"{period}期:九肖杀 绝杀①段①合 {match.group(0)}"),
                        score=120,
                        order=len(block),
                    ),
                )
            )
        blocks.append(block)
    return _deduplicate_table_blocks(blocks)


def find_jiuxiao_kill_sum_candidate_with_direction(
    documents: list[str],
    period: int,
    pick: str = "top",
) -> tuple[Candidate | None, bool]:
    return _select_table_period_candidate(_jiuxiao_table_blocks(documents), period, pick)


def extract_jiuxiao_kill_sum_period_values(documents: list[str]) -> dict[int, str]:
    return _table_period_values(_jiuxiao_table_blocks(documents))


def _woman_flavor_table_blocks(documents: list[str]) -> list[TableBlock]:
    anchor_re = re.compile(
        r"(?:澳门|澳門)?\s*女人味[^期]{0,50}(?:绝|絕)\s*(?:杀|殺)\s*一\s*尾[^期]{0,20}二\s*合",
        re.I,
    )
    row_re = re.compile(
        r"(?<!\d)(\d{1,4})\s*期\s*[;:]?\s*(?:绝|絕)\s*(?:杀|殺)"
        r"[^期]{0,40}?\[\s*\d{1,2}\s*尾\s*\][^期]{0,20}?"
        r"\[\s*(0?[1-9]|1[0-3])\s*合\s*[-－]\s*(0?[1-9]|1[0-3])\s*合\s*\]\s*(?:开|開)",
        re.I,
    )
    blocks: list[TableBlock] = []
    for document in documents:
        soup = BeautifulSoup(document, "html.parser")
        text = soup.get_text(" ", strip=True) if soup.find() else document
        text = normalize_digit_text(normalize_text(text))
        anchor = anchor_re.search(text)
        if anchor is None:
            continue
        section = text[anchor.start():]
        stop_match = _TABLE_SECTION_STOP_RE.search(section)
        if stop_match is not None:
            section = section[: stop_match.start()]

        block: TableBlock = []
        for match in row_re.finditer(section):
            period = int(match.group(1))
            values = [f"{int(match.group(2)):02d}合", f"{int(match.group(3)):02d}合"]
            block.append(
                (
                    period,
                    Candidate(
                        values=",".join(values),
                        line=normalize_text(f"{period}期:女人味 绝杀一尾二合 {match.group(0)}"),
                        score=120,
                        order=len(block),
                    ),
                )
            )
        blocks.append(block)
    return _deduplicate_table_blocks(blocks)


def find_woman_flavor_sum_candidate_with_direction(
    documents: list[str],
    period: int,
    pick: str = "top",
) -> tuple[Candidate | None, bool]:
    return _select_table_period_candidate(_woman_flavor_table_blocks(documents), period, pick)


def extract_woman_flavor_sum_period_values(documents: list[str]) -> dict[int, str]:
    return _table_period_values(_woman_flavor_table_blocks(documents))



__all__ = [name for name, value in globals().items() if callable(value) and getattr(value, "__module__", None) == __name__]
