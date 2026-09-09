from __future__ import annotations

import re
from dataclasses import dataclass
from re import Pattern
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from he_app.domain.errors import DedicatedCandidateConflict
from he_app.domain.models import Candidate, Site
from he_app.domain.policies import normalize_digit_text, normalize_text
from he_app.validation.period import is_valid_sum_value


ARTICLE_CONTENT_SITE_IDS = frozenset(
    {
        "s139_article_140_tid_8",
        "s140_article_245_tid_16",
        "s141_article_359_tid_23",
        "s142_article_377_tid_24",
        "s143_article_392_tid_25",
        "s144_article_435_tid_29",
        "s145_article_546_tid_35",
        "s146_article_600_tid_40",
        "s147_article_654_tid_44",
    }
)


@dataclass(frozen=True)
class ArticleContentRule:
    site_id: str
    article_id: str
    tid: str
    anchor_re: Pattern[str]
    row_re: Pattern[str]
    stop_re: Pattern[str]


_VALUE = r"(?P<value>0?[1-9]|1[0-3])"
_ROW_SUFFIX = rf"\[\s*{_VALUE}\s*合\s*\]\s*(?:开|開)"
_STOP = (
    r"上一篇|下一篇|点击查看新港澳台彩图大全|記住網址|记住网址|"
    r"澳门官方独家资料库|澳门六合彩合数属性|澳彩合数属性|合数属性|"
    r"(?<!\d)\d{1,4}\s*期\s*[:：].*(?:绝杀|絕殺|禁合|杀一合|殺一合)"
)


def _rule(
    site_id: str,
    article_id: str,
    tid: str,
    anchor: str,
    row: str,
) -> ArticleContentRule:
    return ArticleContentRule(
        site_id=site_id,
        article_id=article_id,
        tid=tid,
        anchor_re=re.compile(anchor, re.I),
        row_re=re.compile(row, re.I),
        stop_re=re.compile(_STOP, re.I),
    )


ARTICLE_CONTENT_RULES: dict[str, ArticleContentRule] = {
    "s139_article_140_tid_8": _rule(
        "s139_article_140_tid_8",
        "140",
        "8",
        r"\d{1,4}\s*期\s*:\s*赛玛荟独家.*?绝杀一合",
        rf"(?<!\d)(?P<period>\d{{1,4}})\s*期\s*杀\s*:\s*{_ROW_SUFFIX}",
    ),
    "s140_article_245_tid_16": _rule(
        "s140_article_245_tid_16",
        "245",
        "16",
        r"\d{1,4}\s*期\s*:\s*黄大仙.*?稳禁一合",
        rf"(?<!\d)(?P<period>\d{{1,4}})\s*期\s*禁\s*:\s*{_ROW_SUFFIX}",
    ),
    "s141_article_359_tid_23": _rule(
        "s141_article_359_tid_23",
        "359",
        "23",
        r"\d{1,4}\s*期\s*:\s*原创资料.*?绝杀一合",
        rf"(?<!\d)(?P<period>\d{{1,4}})\s*期.*?⑥合彩开奖资料\s*\[\s*绝杀一合\s*\]\s*:\s*{_ROW_SUFFIX}",
    ),
    "s142_article_377_tid_24": _rule(
        "s142_article_377_tid_24",
        "377",
        "24",
        r"\d{1,4}\s*期\s*:\s*精选供参料.*?死禁一合",
        rf"(?<!\d)(?P<period>\d{{1,4}})\s*期\s*香港禁合\s*:\s*{_ROW_SUFFIX}",
    ),
    "s143_article_392_tid_25": _rule(
        "s143_article_392_tid_25",
        "392",
        "25",
        r"\d{1,4}\s*期\s*:\s*镇坛之宝.*?禁合今錯一",
        rf"(?<!\d)(?P<period>\d{{1,4}})\s*期\s*杀==\s*:\s*{_ROW_SUFFIX}",
    ),
    "s144_article_435_tid_29": _rule(
        "s144_article_435_tid_29",
        "435",
        "29",
        r"\d{1,4}\s*期\s*:\s*彩坛至尊.*?九陇禁合",
        rf"(?<!\d)(?P<period>\d{{1,4}})\s*期\s*杀\s*:\s*{_ROW_SUFFIX}",
    ),
    "s145_article_546_tid_35": _rule(
        "s145_article_546_tid_35",
        "546",
        "35",
        r"\d{1,4}\s*期\s*:\s*杀一合.*?长期跟踪",
        rf"(?<!\d)(?P<period>\d{{1,4}})\s*期.*?屠龙刀.*?杀一合\s*:\s*{_ROW_SUFFIX}",
    ),
    "s146_article_600_tid_40": _rule(
        "s146_article_600_tid_40",
        "600",
        "40",
        r"\d{1,4}\s*期\s*:\s*金明世家.*?无错禁合料",
        rf"(?<!\d)(?P<period>\d{{1,4}})\s*期\s*杀\s*:\s*★★★★\s*:\s*{_ROW_SUFFIX}",
    ),
    "s147_article_654_tid_44": _rule(
        "s147_article_654_tid_44",
        "654",
        "44",
        r"\d{1,4}\s*期\s*:\s*澳门好彩.*?绝杀一合",
        rf"(?<!\d)(?P<period>\d{{1,4}})\s*期\s*杀一合\s*:\s*{_ROW_SUFFIX}",
    ),
}


@dataclass(frozen=True)
class ArticleContentRow:
    period: int
    value: str
    line: str
    order: int


@dataclass(frozen=True)
class ArticleContentBlock:
    source_url: str
    document_id: str
    authority_id: str
    rows: tuple[ArticleContentRow, ...]


def article_content_rule(site_id: str) -> ArticleContentRule | None:
    return ARTICLE_CONTENT_RULES.get(site_id)


def _expected_url(site: Site, rule: ArticleContentRule) -> bool:
    parsed = urlparse(site.url)
    match = re.fullmatch(
        r"/Article/ar_content/id/(\d+)/tid/(\d+)\.html",
        parsed.path,
        re.I,
    )
    return bool(
        match
        and match.group(1) == rule.article_id
        and match.group(2) == rule.tid
        and parsed.query == ""
        and parsed.fragment == ""
    )


def _eligible_document(site: Site, document: str) -> bool:
    source_url = str(getattr(document, "source_url", "") or site.url)
    if source_url != site.url:
        return False
    fetch_kind = str(getattr(document, "fetch_kind", "legacy") or "legacy")
    return fetch_kind in {"legacy", "http"}


def _normalized_lines(document: str) -> list[str]:
    soup = BeautifulSoup(str(document), "html.parser")
    return [
        normalize_digit_text(normalize_text(line))
        for line in soup.get_text("\n", strip=True).splitlines()
        if normalize_text(line)
    ]


def _collect_blocks(site: Site, documents: list[str]) -> list[ArticleContentBlock]:
    rule = article_content_rule(site.site_id)
    if rule is None or not _expected_url(site, rule):
        return []

    blocks: list[ArticleContentBlock] = []
    seen: set[tuple[str, tuple[tuple[int, str, str], ...]]] = set()
    for document in documents:
        if not _eligible_document(site, document):
            continue
        lines = _normalized_lines(document)
        anchors = [index for index, line in enumerate(lines) if rule.anchor_re.search(line)]
        for anchor_index in anchors:
            stop_index = len(lines)
            for index in range(anchor_index + 1, len(lines)):
                if rule.stop_re.search(lines[index]):
                    stop_index = index
                    break
            rows: list[ArticleContentRow] = []
            for order, line in enumerate(lines[anchor_index + 1 : stop_index]):
                match = rule.row_re.search(line)
                if match is None:
                    continue
                value = f"{int(normalize_digit_text(match.group('value'))):02d}合"
                if not is_valid_sum_value(value):
                    continue
                rows.append(
                    ArticleContentRow(
                        int(match.group("period")), value, line, order
                    )
                )
            if not rows:
                continue
            source_url = str(getattr(document, "source_url", "") or site.url)
            document_id = str(
                getattr(document, "document_id", "") or f"article:{rule.article_id}:{rule.tid}"
            )
            authority_id = str(
                getattr(document, "authority_id", "")
                or f"article-content:{rule.article_id}:{rule.tid}"
            )
            key = (source_url, tuple((row.period, row.value, row.line) for row in rows))
            if key in seen:
                continue
            seen.add(key)
            blocks.append(
                ArticleContentBlock(source_url, document_id, authority_id, tuple(rows))
            )
    return blocks


def _unique_rows(site: Site, documents: list[str]) -> dict[int, ArticleContentRow]:
    by_period: dict[int, list[ArticleContentRow]] = {}
    for block in _collect_blocks(site, documents):
        for row in block.rows:
            by_period.setdefault(row.period, []).append(row)

    unique: dict[int, ArticleContentRow] = {}
    for period, rows in by_period.items():
        values = {row.value for row in rows}
        if len(values) > 1:
            raise DedicatedCandidateConflict(
                sorted(values), [row.line for row in rows]
            )
        unique[period] = rows[0]
    return unique


def find_article_content_candidate_with_direction(
    site: Site,
    documents: list[str],
    period: int,
    pick: str,
) -> tuple[Candidate | None, bool]:
    blocks = _collect_blocks(site, documents)
    target_rows = [
        row
        for block in blocks
        for row in block.rows
        if row.period == period
    ]
    target_values = {row.value for row in target_rows}
    if len(target_values) > 1:
        raise DedicatedCandidateConflict(
            sorted(target_values), [row.line for row in target_rows]
        )
    edge_rows = [block.rows[0] if pick == "top" else block.rows[-1] for block in blocks]
    target_edges = [row for row in edge_rows if row.period == period]
    if target_edges:
        values = {row.value for row in target_edges}
        if len(values) > 1:
            raise DedicatedCandidateConflict(
                sorted(values), [row.line for row in target_edges]
            )
        row = target_edges[0]
        return Candidate(row.value, row.line, 0, row.order), False

    all_rows = [row for block in blocks for row in block.rows]
    return None, any(row.period == period for row in all_rows)


def extract_article_content_period_values(
    site: Site, documents: list[str]
) -> dict[int, str]:
    return {period: row.value for period, row in sorted(_unique_rows(site, documents).items(), reverse=True)}


def find_article_content_history_candidate(
    site: Site, documents: list[str], period: int
) -> ArticleContentRow | None:
    return _unique_rows(site, documents).get(period)


def is_article_content_row(
    site: Site, text: str, period: int, values: list[str]
) -> bool:
    rule = article_content_rule(site.site_id)
    if rule is None or len(values) != 1:
        return False
    normalized = normalize_digit_text(normalize_text(text))
    for match in rule.row_re.finditer(normalized):
        value = f"{int(normalize_digit_text(match.group('value'))):02d}合"
        if int(match.group("period")) == period and value == values[0]:
            return True
    return False


def article_content_segment_is_bounded(
    site_id: str, text: str, start: int, end: int
) -> bool:
    rule = article_content_rule(site_id)
    if rule is None:
        return False
    normalized = normalize_digit_text(normalize_text(text))
    anchor = rule.anchor_re.search(normalized)
    if anchor is None:
        return False
    stop = rule.stop_re.search(normalized, anchor.end())
    limit = stop.start() if stop is not None else len(normalized)
    if not anchor.end() <= start < end <= limit:
        return False
    return rule.row_re.search(normalized[start:end]) is not None
