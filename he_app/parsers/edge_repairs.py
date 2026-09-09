from __future__ import annotations

import re

from bs4 import BeautifulSoup, Tag

from he_app.domain.models import Candidate, Site
from he_app.domain.policies import normalize_digit_text, normalize_pick, normalize_text
from he_app.parsers.common import (
    current_candidates_outside_window,
    trusted_candidate_with_conflict,
)
from he_app.parsers.dedicated.tables import format_success_result


_TAXUE_SITE_ID = "s032_topic_309383"
_TONGTIAN_SITE_ID = "s058_vkjwinyt"
_TONGTIAN_HEADING = "澳门通天报㊣澳门综合杀"
_TONGTIAN_HEADERS = ("期数", "杀肖", "杀合", "杀半头", "开奖")


def _success(site: Site, period: int, candidate: Candidate):
    return (
        format_success_result(site, period, candidate),
        candidate.line,
        candidate.values.split(","),
        None,
    )


def _taxue_history_blocks(document: str, site_name: str) -> list[str]:
    """Find the smallest rendered article container holding the real history.

    The page repeats the site name in navigation/decoration. A valid container
    must therefore contain the name plus at least six distinct issue numbers
    and six strict kill-sum rows. Choosing the first such ancestor around a
    name occurrence keeps the physical top/bottom boundary tied to the whole
    history block rather than to one convenient target row.
    """

    soup = BeautifulSoup(document, "html.parser")
    blocks: list[str] = []
    signatures: set[str] = set()
    for text_node in soup.find_all(string=True):
        node_text = normalize_text(str(text_node))
        if site_name not in node_text or len(node_text) > 120:
            continue
        parent = text_node.parent
        for _depth in range(9):
            if not isinstance(parent, Tag):
                break
            text = normalize_digit_text(normalize_text(parent.get_text(" ", strip=True)))
            periods = {
                int(value)
                for value in re.findall(r"(?<!\d)(\d{1,4})\s*期", text)
            }
            strict_rows = len(
                re.findall(
                    r"(?<!\d)\d{1,4}\s*期[^期]{0,100}?(?:绝|絕)\s*(?:杀|殺)\s*一\s*合",
                    text,
                )
            )
            if len(periods) >= 6 and strict_rows >= 6:
                html = str(parent)
                signature = normalize_text(text[:1000])
                if signature not in signatures:
                    signatures.add(signature)
                    blocks.append(html)
                break
            parent = parent.parent
    return blocks


def _parse_taxue(site: Site, period: int, documents: list[str]):
    pick = normalize_pick(site.pick)
    candidates: list[Candidate] = []
    outside = False
    found_block = False
    conflict_values: set[str] = set()
    conflict_lines: list[str] = []

    for document in documents:
        for block in _taxue_history_blocks(str(document), site.name):
            found_block = True
            candidate, values, lines = trusted_candidate_with_conflict(
                [block],
                period,
                pick,
                require_body_locator=False,
                allow_weak=False,
            )
            if values:
                conflict_values.update(values)
                conflict_lines.extend(lines)
                continue
            if candidate is not None:
                candidates.append(candidate)
            elif current_candidates_outside_window(
                [block], period, pick, False, False
            ):
                outside = True

    all_values = {candidate.values for candidate in candidates} | conflict_values
    if len(all_values) > 1:
        return (
            None,
            f"{site.name} {period}期专属历史块候选冲突: {' / '.join(sorted(all_values))}；"
            f"候选: {' | '.join(conflict_lines[:5])}",
            [],
            "候选冲突",
        )
    if candidates:
        return _success(site, period, candidates[0])
    if outside:
        return (
            None,
            f"{site.name} {period}期存在，但不是配置的物理{pick.upper()}边界历史行",
            [],
            "方向范围外",
        )
    if found_block:
        return None, f"{site.name} 专属历史块里没找到{period}期严格绝杀一合", [], "无当期"
    return None, f"{site.name} 没找到包含完整历史的专属文章块", [], "锚点缺失"


def _tongtian_blocks(document: str) -> list[list[tuple[int, Candidate]]]:
    soup = BeautifulSoup(document, "html.parser")
    tables = soup.find_all("table")
    blocks: list[list[tuple[int, Candidate]]] = []
    for index, heading_table in enumerate(tables):
        heading = normalize_text(heading_table.get_text(" ", strip=True))
        if _TONGTIAN_HEADING not in heading or "综合绝杀" in heading:
            continue
        # The live page places the title and its data in adjacent tables.
        # Search only the next two physical tables and require the exact header.
        for data_table in tables[index + 1 : index + 3]:
            rows = data_table.find_all("tr")
            if not rows:
                continue
            header_cells = tuple(
                normalize_text(cell.get_text(" ", strip=True))
                for cell in rows[0].find_all(["th", "td"], recursive=False)
            )
            if header_cells != _TONGTIAN_HEADERS:
                continue
            block: list[tuple[int, Candidate]] = []
            for order, row in enumerate(rows[1:]):
                cells = [
                    normalize_digit_text(normalize_text(cell.get_text(" ", strip=True)))
                    for cell in row.find_all(["th", "td"], recursive=False)
                ]
                if len(cells) != len(_TONGTIAN_HEADERS):
                    continue
                period_match = re.fullmatch(r"(\d{1,4})\s*期", cells[0])
                value_match = re.fullmatch(r"(0?[1-9]|1[0-3])\s*合", cells[2])
                if period_match is None or value_match is None:
                    continue
                row_period = int(period_match.group(1))
                value = f"{int(value_match.group(1)):02d}合"
                raw_row = normalize_text(" ".join(cells))
                block.append(
                    (
                        row_period,
                        Candidate(
                            values=value,
                            line=normalize_text(
                                f"{row_period}期:通天澳门综合杀 杀合[{value}] {raw_row}"
                            ),
                            score=130,
                            order=order,
                        ),
                    )
                )
            if block:
                blocks.append(block)
            break
    return blocks


def _parse_tongtian(site: Site, period: int, documents: list[str]):
    pick = normalize_pick(site.pick)
    all_blocks: list[list[tuple[int, Candidate]]] = []
    for document in documents:
        all_blocks.extend(_tongtian_blocks(str(document)))
    if not all_blocks:
        return None, f"{site.name} 没找到标题+精确表头绑定的澳门综合杀表格", [], "锚点缺失"

    # Multiple duplicate renders are allowed only when their full period/value
    # signatures agree. Different tables are an explicit conflict.
    signatures = {
        tuple((row_period, candidate.values) for row_period, candidate in block)
        for block in all_blocks
    }
    if len(signatures) != 1:
        return None, f"{site.name} 澳门综合杀出现多个不一致表格", [], "候选冲突"
    block = all_blocks[0]
    target_exists = any(row_period == period for row_period, _candidate in block)
    edge_period, edge_candidate = block[0] if pick == "top" else block[-1]
    if edge_period == period:
        return _success(site, period, edge_candidate)
    if target_exists:
        return (
            None,
            f"{site.name} {period}期存在于澳门综合杀表，但不是配置的物理{pick.upper()}边界行",
            [],
            "方向范围外",
        )
    return None, f"{site.name} 澳门综合杀表格里没找到{period}期杀合", [], "无当期"


def _has_live_tongtian_shape(text: str) -> bool:
    heading = text.find(_TONGTIAN_HEADING)
    if heading < 0:
        return False
    return (
        re.search(r"期数\s*杀肖\s*杀合\s*杀半头\s*开奖", text[heading:])
        is not None
    )


def _tight_tongtian_bound(text: str, start: int, end: int) -> bool:
    """Bind evidence to the exact 澳门综合杀 section, not nearby tables."""

    heading = text.find(_TONGTIAN_HEADING)
    if heading < 0:
        return False
    header = text.find("期数杀肖杀合杀半头开奖", heading)
    if header < 0:
        # normalize_text may preserve spaces between table cells.
        header_match = re.search(r"期数\s*杀肖\s*杀合\s*杀半头\s*开奖", text[heading:])
        if header_match is None:
            return False
        header = heading + header_match.start()
    next_heading_match = re.search(r"《\s*澳门通天报㊣", text[header + 1 :])
    limit = (
        header + 1 + next_heading_match.start()
        if next_heading_match is not None
        else len(text)
    )
    return header <= start < end <= limit


def apply_remaining_edge_parser_repairs() -> None:
    """Install narrow parsers after the normal registry has been built."""

    from he_app.parsers import registry as registry_module
    from he_app.services import single_period as single_period_module

    registry_module.REGISTRY._parsers[_TAXUE_SITE_ID] = _parse_taxue

    current_tongtian = registry_module.REGISTRY._parsers[_TONGTIAN_SITE_ID]
    if not getattr(current_tongtian, "_remaining_edge_combined", False):
        legacy_tongtian = current_tongtian

        def combined_tongtian(site: Site, period: int, documents: list[str]):
            live = _parse_tongtian(site, period, documents)
            # Exact live heading+header evidence always wins. If that structure
            # is absent, retain the original parser for established legacy/plain
            # fixtures and older page shapes rather than changing old semantics.
            if live[3] != "锚点缺失":
                return live
            return legacy_tongtian(site, period, documents)

        combined_tongtian._remaining_edge_combined = True  # type: ignore[attr-defined]
        registry_module.REGISTRY._parsers[_TONGTIAN_SITE_ID] = combined_tongtian

    registry_module.WINDOW_PRECHECK_EXEMPT.update({_TAXUE_SITE_ID, _TONGTIAN_SITE_ID})

    original = single_period_module._special_segment_is_bounded
    if getattr(original, "_remaining_edge_wrapped", False):
        return

    def bounded(site_id: str, text: str, start: int, end: int) -> bool:
        if site_id == _TONGTIAN_SITE_ID and _has_live_tongtian_shape(text):
            return _tight_tongtian_bound(text, start, end)
        return original(site_id, text, start, end)

    bounded._remaining_edge_wrapped = True  # type: ignore[attr-defined]
    single_period_module._special_segment_is_bounded = bounded


__all__ = ["apply_remaining_edge_parser_repairs"]
