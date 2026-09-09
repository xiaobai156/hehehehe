import re

from bs4 import BeautifulSoup

from he_app.domain.models import CandidateEvidence, FailureInfo, ParseResult, Site
from he_app.domain.policies import normalize_digit_text, normalize_text, normalize_pick
from he_app.parsers.common import (
    build_candidate_parts,
    build_latest_candidate_parts,
    extract_values,
    has_kill_sum_keyword,
    split_period_segments,
)
from he_app.parsers.dedicated.history import BATCH_NEW_DEDICATED_SITE_IDS
from he_app.parsers.dedicated.kaijiangfacai import (
    KAIJIANGFACAI_HEADERS,
    KAIJIANGFACAI_SITE_ID,
    KAIJIANGFACAI_TITLE_RE,
    extract_kaijiangfacai_kill_sum_period_values,
)
from he_app.parsers.dedicated.tables import site_rule
from he_app.parsers.dedicated.ttss import (
    TTSS_SITE_IDS,
    is_ttss_kill_sum_row,
    is_ttss_target_title,
)
from he_app.parsers.registry import evaluate_site_documents
from he_app.validation.period import contains_exact_period, is_valid_sum_value
from he_app.validation.record_boundary import record_id_from_url


Evaluation = tuple[str | None, str, list[str], str | None]


def _contains_sum_value(text: str, value: str) -> bool:
    number = int(value[:2])
    token = rf"0?{number}" if number < 10 else str(number)
    return bool(
        re.search(
            rf"(?:合\s*{token}(?!\d)|(?<!\d){token}\s*合|[\[【(（]\s*{token}\s*[\]】)）])",
            text,
        )
    )


def _sum_number_pattern(value: str) -> str:
    number = int(value[:2])
    return rf"0?{number}" if number < 10 else str(number)


def _dedicated_document_keyword(site: Site, period: int, values: list[str], text: str) -> str | None:
    if site.site_id in TTSS_SITE_IDS and len(values) == 1:
        if is_ttss_kill_sum_row(text, site.name, period, values[0]):
            return "列表文章绝杀一合"
        if is_ttss_target_title(text, site.name):
            return "列表文章绝杀一合"

    if site.site_id == "s093_a_909922_article_aspx_id_3694545" and len(values) == 1:
        value = _sum_number_pattern(values[0])
        row_re = re.compile(
            rf"(?<!\d){period}\s*期\s*杀\s*[:：]\s*[\[【]\s*{value}\s*合\s*[\]】]\s*[开開]"
        )
        if row_re.search(text):
            return "亮劍原创公式杀合"

    if site.site_id == "s073_shuqhbq" and len(values) == 1:
        anchor_re = re.compile(r"(?:绝|絕)\s*(?:杀|殺)\s*①\s*段\s*①\s*合")
        anchor = anchor_re.search(text)
        if anchor is None:
            return None
        section = text[anchor.start():]
        next_anchor = re.search(r"(?:绝|絕)\s*(?:杀|殺)\s*三\s*尾", section[anchor.end() - anchor.start():])
        if next_anchor is not None:
            section = section[: anchor.end() - anchor.start() + next_anchor.start()]
        value = _sum_number_pattern(values[0])
        row_re = re.compile(
            rf"(?<!\d){period}\s*期\s*(?:杀|殺)\s*\[\s*{value}\s*合\s*\]\s*(?:开|開)"
        )
        return "绝杀①段①合" if row_re.search(section) else None

    if site.site_id == "s085_kcvpleh" and len(values) == 2:
        anchor_re = re.compile(
            r"(?:澳门|澳門)?\s*女人味[^期]{0,50}(?:绝|絕)\s*(?:杀|殺)\s*一\s*尾[^期]{0,20}二\s*合"
        )
        anchor = anchor_re.search(text)
        if anchor is None:
            return None
        first = _sum_number_pattern(values[0])
        second = _sum_number_pattern(values[1])
        row_re = re.compile(
            rf"(?<!\d){period}\s*期\s*[;:]?\s*(?:绝|絕)\s*(?:杀|殺)"
            rf"[^期]{{0,40}}?\[\s*\d{{1,2}}\s*尾\s*\][^期]{{0,20}}?"
            rf"\[\s*{first}\s*合\s*[-－]\s*{second}\s*合\s*\]\s*(?:开|開)"
        )
        return "女人味绝杀一尾二合" if row_re.search(text[anchor.start():]) else None

    if site.site_id in BATCH_NEW_DEDICATED_SITE_IDS and len(values) == 1:
        value = _sum_number_pattern(values[0])
        abbreviated_row_re = re.compile(
            rf"(?<!\d){period}\s*期\s*[:：]?\s*杀\s*[:：]?\s*"
            rf"[\[【\(（][^期]{{0,20}}?{value}\s*合[^期]{{0,12}}?[\]】\)）][^期]{{0,20}}?[开開]"
        )
        if abbreviated_row_re.search(text):
            return "专属绝杀一合栏目"

    if has_kill_sum_keyword(text, allow_weak=True):
        return "绝杀一合"
    if "杀合" in text:
        return "杀合"
    return None


def _authority_groups(documents: list[str]) -> list[list[str]]:
    """Keep each physical document as an independent candidate authority."""
    return [[document] for document in documents]


def _allowed_documents(site: Site, documents: list[str]) -> list[str]:
    allowed_fetch_kinds = site_rule(site).allowed_fetch_kinds
    if not allowed_fetch_kinds:
        allowed_fetch_kinds = ("legacy", "http", "browser", "api")
        if site.site_id == "s070_topic_246762" and not site.browser:
            allowed_fetch_kinds += ("http-decoded", "script", "script-decoded")
    return [
        document
        for document in documents
        if str(getattr(document, "fetch_kind", "legacy") or "legacy") in allowed_fetch_kinds
    ]


def _target_values(documents: list[str], period: int, allow_weak: bool) -> tuple[set[str], list[str]]:
    values: set[str] = set()
    lines: list[str] = []
    for line, _has_locator in build_candidate_parts(documents, period, allow_weak):
        extracted = extract_values(line, allow_weak)
        if len(extracted) != 1:
            continue
        values.add(extracted[0])
        lines.append(line)
    return values, lines


def _has_complete_records(documents: list[str], allow_weak: bool) -> bool:
    return bool(build_latest_candidate_parts(documents, allow_weak))


def _candidate_segments(document: str, period: int) -> list[tuple[str, int, int]]:
    soup = BeautifulSoup(document, "html.parser")
    text = soup.get_text(" ", strip=True) or document
    normalized_document = normalize_digit_text(normalize_text(text))
    chunks: list[str] = []
    for tag in soup.find_all(["p", "div", "td", "tr", "li", "font", "span", "b", "strong"]):
        tag_text = normalize_digit_text(normalize_text(tag.get_text(" ", strip=True)))
        if tag_text:
            chunks.append(tag_text)
    line_text = soup.get_text("\n", strip=True) if soup.find() else str(document)
    chunks.extend(
        normalize_digit_text(normalize_text(line))
        for line in line_text.splitlines()
        if normalize_text(line)
    )

    segments: list[tuple[str, int, int]] = []
    seen: set[tuple[str, int, int]] = set()
    for chunk in chunks:
        candidates = split_period_segments(chunk, period)
        if not candidates and contains_exact_period(chunk, period):
            candidates = [chunk]
        for segment in candidates:
            if len(segment) > 500:
                continue
            for occurrence in re.finditer(re.escape(segment), normalized_document):
                key = (segment, occurrence.start(), occurrence.end())
                if key not in seen:
                    seen.add(key)
                    segments.append(key)
    return segments


def _detail_is_self_contained(site: Site, period: int, values: list[str], detail: str) -> bool:
    normalized = normalize_digit_text(normalize_text(detail))
    if not contains_exact_period(normalized, period):
        return False
    if _dedicated_document_keyword(site, period, values, normalized) is None:
        return False
    return all(_contains_sum_value(normalized, value) for value in values)


def _special_segment_is_bounded(site_id: str, text: str, start: int, end: int) -> bool:
    if site_id == "s058_vkjwinyt":
        anchor = re.search(r"澳门综合杀", text)
        if anchor is None:
            return False
        section = text[anchor.start() :]
        stop = re.search(r"澳门六合彩合数属性|澳彩合数属性|合数属性|其他栏目|上一篇|下一篇|Copyright", section)
        limit = anchor.start() + stop.start() if stop is not None else len(text)
        return anchor.start() <= start < end <= limit

    if site_id == "s073_shuqhbq":
        anchor = re.search(r"绝杀\s*①\s*段\s*①\s*合", text)
        if anchor is None:
            return False
        section = text[anchor.end() :]
        stop = re.search(r"绝杀\s*三\s*尾", section)
        limit = anchor.end() + stop.start() if stop is not None else len(text)
        return anchor.start() <= start < end <= limit

    if site_id == "s085_kcvpleh":
        anchor = re.search(r"(?:澳门|澳門)?\s*女人味", text)
        if anchor is None:
            return False
        section = text[anchor.start() :]
        stop = re.search(r"澳门六合彩合数属性|澳彩合数属性|合数属性|上一篇|下一篇|Copyright", section)
        limit = anchor.start() + stop.start() if stop is not None else len(text)
        return anchor.start() <= start < end <= limit

    return False


def _kaijiangfacai_evidence_bounds(
    document: str, period: int, value: str
) -> tuple[int, int] | None:
    extracted = extract_kaijiangfacai_kill_sum_period_values([document])
    if extracted.get(period) != value:
        return None

    soup = BeautifulSoup(document, "html.parser")
    normalized_document = normalize_digit_text(
        normalize_text(soup.get_text(" ", strip=True) or document)
    )
    expected_number = int(value[:2])
    for title in soup.select(".list-title"):
        title_text = normalize_text(title.get_text(" ", strip=True))
        if KAIJIANGFACAI_TITLE_RE.fullmatch(title_text) is None:
            continue
        table = title.find_next("table")
        if table is None:
            continue
        header = table.find("tr")
        headers = tuple(
            normalize_text(cell.get_text(" ", strip=True))
            for cell in header.find_all(["th", "td"], recursive=False)
        ) if header is not None else ()
        if headers != KAIJIANGFACAI_HEADERS:
            continue
        for row in table.find_all("tr"):
            cells = [
                normalize_digit_text(normalize_text(cell.get_text(" ", strip=True)))
                for cell in row.find_all(["th", "td"], recursive=False)
            ]
            if len(cells) != len(KAIJIANGFACAI_HEADERS):
                continue
            period_match = re.fullmatch(r"(\d{1,4})\s*期", cells[0])
            value_match = re.fullmatch(r"(0?[1-9]|1[0-3])", cells[3])
            if (
                period_match is None
                or value_match is None
                or int(period_match.group(1)) != period
                or int(value_match.group(1)) != expected_number
            ):
                continue
            title_start = normalized_document.find(normalize_text(title_text))
            row_text = normalize_digit_text(normalize_text(row.get_text(" ", strip=True)))
            row_start = normalized_document.find(row_text, max(title_start, 0))
            if row_start >= 0:
                return row_start, row_start + len(row_text)
    return None


def build_document_evidence(
    site: Site,
    period: int,
    values: list[str],
    detail: str,
    documents: list[str],
) -> tuple[CandidateEvidence, ...]:
    if not _detail_is_self_contained(site, period, values, detail):
        return ()

    article_id = record_id_from_url(site.url)
    synthetic_detail_sites = {"s058_vkjwinyt", "s073_shuqhbq", "s085_kcvpleh"}
    evidence: list[CandidateEvidence] = []
    for index, document in enumerate(documents):
        soup = BeautifulSoup(document, "html.parser")
        observed_id = getattr(document, "record_id", None)
        if article_id is not None:
            records = soup.select("article[data-manager-record-id]")
            observed_ids = {str(node.get("data-manager-record-id")) for node in records}
            if observed_id:
                observed_ids.add(str(observed_id))
            if observed_ids != {article_id}:
                continue
            observed_id = article_id
        text = soup.get_text(" ", strip=True) or document
        normalized_document = normalize_digit_text(normalize_text(text))
        keyword = _dedicated_document_keyword(site, period, values, normalized_document)
        if keyword is None:
            continue
        document_article_id = str(observed_id) if observed_id else None

        if site.site_id == KAIJIANGFACAI_SITE_ID and len(values) == 1:
            bounds = _kaijiangfacai_evidence_bounds(document, period, values[0])
            if bounds is None:
                continue
            start, end = bounds
            source_url = str(getattr(document, "source_url", "") or site.url)
            authority_id = str(getattr(document, "authority_id", "legacy") or "legacy")
            document_id = str(getattr(document, "document_id", "") or f"document:{index}")
            actual_position = len(
                re.findall(r"(?<!\d)\d{1,4}\s*期", normalized_document[:start])
            )
            evidence.append(
                CandidateEvidence(
                    site_id=site.site_id,
                    period=period,
                    values=tuple(values),
                    source_url=source_url,
                    document_id=document_id,
                    row_order=actual_position,
                    raw_line=detail,
                    article_id=document_article_id,
                    keyword="开奖发财综合杀料表杀合列",
                    fetch_kind=str(getattr(document, "fetch_kind", "legacy")),
                    document_type=str(getattr(document, "document_type", "text")),
                    parent_url=str(getattr(document, "parent_url", "") or site.url),
                    authority_id=authority_id,
                    block_id=f"{authority_id}:{start}:{end}",
                    block_start=start,
                    block_end=end,
                    actual_position=actual_position,
                )
            )
            continue

        matched_segment: tuple[str, int, int] | None = None
        segments = _candidate_segments(document, period)
        normalized_detail = normalize_digit_text(normalize_text(detail))
        exact = [item for item in segments if item[0] == normalized_detail]
        if exact:
            segments = exact
        segments.sort(key=lambda item: (item[1], -len(item[0])),
                      reverse=normalize_pick(site.pick) == "bottom")
        for segment, start, end in segments:
            if start < 0 or end <= start:
                continue
            if not all(_contains_sum_value(segment, value) for value in values):
                continue
            if site.site_id not in synthetic_detail_sites:
                if _dedicated_document_keyword(site, period, values, segment) is None:
                    continue
                segment_values = extract_values(segment, allow_weak=True)
                if segment_values and set(segment_values) != set(values):
                    continue
            elif not _special_segment_is_bounded(site.site_id, normalized_document, start, end):
                continue
            matched_segment = (segment, start, end)
            break
        if matched_segment is None:
            continue

        segment, start, end = matched_segment
        source_url = str(getattr(document, "source_url", "") or site.url)
        authority_id = str(getattr(document, "authority_id", "legacy") or "legacy")
        document_id = str(getattr(document, "document_id", "") or f"document:{index}")
        actual_position = len(re.findall(r"(?<!\d)\d{1,4}\s*期", normalized_document[:max(start, 0)]))
        evidence.append(
            CandidateEvidence(
                site_id=site.site_id,
                period=period,
                values=tuple(values),
                source_url=source_url,
                document_id=document_id,
                row_order=actual_position,
                raw_line=detail,
                article_id=document_article_id,
                keyword=keyword,
                fetch_kind=str(getattr(document, "fetch_kind", "legacy")),
                document_type=str(getattr(document, "document_type", "text")),
                parent_url=str(getattr(document, "parent_url", "") or site.url),
                authority_id=authority_id,
                block_id=f"{authority_id}:{max(start, 0)}:{end}",
                block_start=max(start, 0),
                block_end=end,
                actual_position=actual_position,
            )
        )
    return tuple(evidence)


def _cross_authority_conflict(
    site: Site,
    period: int,
    evaluations: list[tuple[list[str], Evaluation, set[str], list[str]]],
) -> FailureInfo | None:
    signatures_by_authority: list[tuple[set[tuple[str, ...]], list[str]]] = []
    for _documents, evaluation, _strict_values, _strict_lines in evaluations:
        result, detail, result_values, _reason = evaluation
        signatures: set[tuple[str, ...]] = set()
        lines: list[str] = []
        if result is not None and result_values:
            signatures.add(tuple(result_values))
            lines.append(detail)
        if signatures:
            signatures_by_authority.append((signatures, lines))
    if len(signatures_by_authority) < 2:
        return None
    unique_signatures = {
        signature
        for signatures, _lines in signatures_by_authority
        for signature in signatures
    }
    if len(unique_signatures) <= 1:
        return None
    unique_values = sorted(
        {
            value
            for signature in unique_signatures
            for value in signature
        },
        key=lambda value: int(value[:2]),
    )
    conflict_lines = [line for _signatures, lines in signatures_by_authority for line in lines]
    return FailureInfo(
        "候选冲突",
        f"{site.name} {period}期跨来源候选结果冲突: {' / '.join(unique_values)}；"
        f"候选: {' | '.join(conflict_lines[:5])}",
    )


def parse_site_period(site: Site, period: int, documents: list[str]) -> ParseResult:
    rule = site_rule(site)
    documents = _allowed_documents(site, documents)
    evaluations: list[tuple[list[str], Evaluation, set[str], list[str]]] = []
    for authority_documents in _authority_groups(documents):
        evaluation = evaluate_site_documents(site, period, authority_documents)
        strict_values, strict_lines = _target_values(
            authority_documents, period, rule.allow_weak_kill_sum_keyword
        )
        evaluations.append((authority_documents, evaluation, strict_values, strict_lines))

    for source_docs, source_eval, _values, _lines in evaluations:
        source = source_docs[0]
        authority = getattr(source, "authority_id", "legacy")
        if (authority == "legacy" or getattr(source, "document_type", "") not in
                {"html", "page-source", "manager-record", "json-record"}):
            continue
        if source_eval[0] is None and source_eval[3] in {"方向范围外", "超出范围", "候选冲突"}:
            if any(item[1][0] is not None and
                   getattr(item[0][0], "authority_id", "legacy") == authority
                   for item in evaluations):
                return ParseResult(False, failure=FailureInfo(source_eval[3], source_eval[1]))

    conflict = _cross_authority_conflict(site, period, evaluations)
    if conflict is not None:
        return ParseResult(False, failure=conflict)

    selected: tuple[list[str], Evaluation, set[str], list[str]] | None = None
    for item in evaluations:
        if item[1][0] is not None:
            selected = item
            break
    if selected is None:
        for item in evaluations:
            _authority_documents, evaluation, _strict_values, _strict_lines = item
            _result, _detail, _values, reason = evaluation
            if reason in {"方向范围外", "超出范围", "候选冲突", "数据不完整"}:
                selected = item
                break
    if selected is None:
        for item in evaluations:
            authority_documents, _evaluation, _strict_values, _strict_lines = item
            if _has_complete_records(authority_documents, rule.allow_weak_kill_sum_keyword):
                selected = item
                break
    if selected is None and evaluations:
        selected = next((item for item in evaluations if item[1][0] is not None), evaluations[-1])
    if selected is None:
        return ParseResult(False, failure=FailureInfo("未找到目标", f"{site.name} 没有可解析文档"))

    authority_documents, evaluation, _strict_values, _strict_lines = selected
    result, detail, values, reason = evaluation
    if result is None:
        return ParseResult(False, failure=FailureInfo(reason or "未找到目标", detail))
    expected_count = 2 if site.site_id == "s085_kcvpleh" else site.value_count
    if (len(values) != expected_count or len(set(values)) != len(values)
            or any(not is_valid_sum_value(value) for value in values)):
        return ParseResult(False, failure=FailureInfo("字段校验未通过", f"{site.name} 返回了无效合数"))

    evidence = build_document_evidence(site, period, values, detail, authority_documents)
    if not evidence:
        return ParseResult(
            False,
            failure=FailureInfo(
                "证据校验未通过",
                f"{site.name} 解析成功但无法在同一原始候选行验证期数、关键词和合数",
            ),
        )
    return ParseResult(True, value=result, evidence=evidence)


def evaluate_site_period(site: Site, period: int, documents: list[str]) -> tuple[str | None, str, list[str], str | None]:
    parsed = parse_site_period(site, period, documents)
    if not parsed.success:
        assert parsed.failure is not None
        return None, parsed.failure.reason, [], parsed.failure.category
    first = parsed.evidence[0]
    return parsed.value, first.raw_line, list(first.values), None
