import re

from bs4 import BeautifulSoup

from he_app.domain.models import Candidate, FailureInfo, Site
from he_app.domain.policies import normalize_digit_text, normalize_pick, normalize_text
from he_app.parsers.policies import (
    BODY_LOCATOR_RE,
    PERIOD_RE,
    REVERSE_VALUE_RE,
    STRICT_KILL_SUM_RE,
    STRICT_SUCCESS_VALUE_RE,
    TWO_VALUE_KILL_SUM_RE,
    VALUE_RE,
    WEAK_KILL_SUM_RE,
)


def document_text_lines(document: str) -> list[str]:
    text = BeautifulSoup(document, "html.parser").get_text("\n", strip=True) or document
    return [line for line in text.splitlines() if normalize_text(line)]


def candidate_chunks(document: str, period: int) -> list[str]:
    chunks: list[str] = []
    soup = BeautifulSoup(document, "html.parser")

    for tag in soup.find_all(["p", "div", "td", "tr", "li", "font", "span", "b", "strong"]):
        text = normalize_text(tag.get_text(" ", strip=True))
        if text:
            chunks.append(text)

    lines = [normalize_text(line) for line in soup.get_text("\n", strip=True).splitlines()]
    lines = [line for line in lines if line]
    target_re = re.compile(rf"(?<!\d){re.escape(str(period))}\s*期")
    for index, line in enumerate(lines):
        chunks.append(line)
        if target_re.search(line):
            chunks.append(normalize_text(" ".join(lines[index : index + 6])))
            window_start = max(0, index - 3)
            window_end = min(len(lines), index + 1)
            chunks.append(normalize_text(" ".join(lines[window_start:window_end])))

    all_text = normalize_text(soup.get_text(" ", strip=True) or document)
    chunks.extend(split_period_segments(all_text, period))
    return chunks


def split_period_segments(text: str, period: int) -> list[str]:
    text = normalize_text(text)
    matches = list(PERIOD_RE.finditer(text))
    segments: list[str] = []

    for index, match in enumerate(matches):
        if int(match.group(1)) != period:
            continue
        start = match.start()
        next_start = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        end = min(next_start, match.start() + 220)
        segment = normalize_text(text[start:end])
        if segment:
            segments.append(segment)

    return segments


def split_all_period_segments(text: str) -> list[str]:
    text = normalize_text(text)
    matches = list(PERIOD_RE.finditer(text))
    segments: list[str] = []

    for index, match in enumerate(matches):
        start = match.start()
        next_start = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        end = min(next_start, match.start() + 220)
        segment = normalize_text(text[start:end])
        if segment:
            segments.append(segment)

    return segments


def has_kill_sum_keyword(text: str, allow_weak: bool = False, value_count: int = 1) -> bool:
    normalized = normalize_digit_text(normalize_text(text))
    strict = STRICT_KILL_SUM_RE.search(normalized) is not None
    if value_count == 2:
        strict = strict or TWO_VALUE_KILL_SUM_RE.search(normalized) is not None
    return bool(strict or (allow_weak and WEAK_KILL_SUM_RE.search(normalized)))


def extract_values(text: str, allow_weak: bool = False, value_count: int = 1) -> list[str]:
    normalized = normalize_digit_text(normalize_text(text))
    for marker in ("澳彩合数属性", "合数属性", "属性:", "属性：", "★★", "博彩必备", "站长宣言"):
        marker_index = normalized.find(marker)
        if marker_index >= 0:
            normalized = normalized[:marker_index]
    normalized = re.split(r"\s0?1\s*[-－]\s*0?6\s*合|\s0?1\s*合\s*[:：]", normalized, maxsplit=1)[0]

    if not has_kill_sum_keyword(normalized, allow_weak, value_count):
        return []

    if has_out_of_range_value(normalized):
        return []

    values: list[str] = []
    matches: list[tuple[re.Match[str], bool]] = []
    matches.extend((match, False) for match in VALUE_RE.finditer(normalized))
    matches.extend((match, True) for match in REVERSE_VALUE_RE.finditer(normalized))
    for match, is_reverse in sorted(matches, key=lambda item: item[0].start()):
        if is_reverse:
            tail = normalized[match.end() : match.end() + 2]
            if tail.startswith(("头", "尾")):
                continue
        value = int(normalize_digit_text(match.group(1)))
        if 1 <= value <= 13:
            item = f"{value:02d}合"
            if item not in values:
                values.append(item)

    return values


def has_out_of_range_value(text: str) -> bool:
    normalized = normalize_digit_text(normalize_text(text))
    matches: list[tuple[re.Match[str], bool]] = []
    matches.extend((match, False) for match in VALUE_RE.finditer(normalized))
    matches.extend((match, True) for match in REVERSE_VALUE_RE.finditer(normalized))
    for match, is_reverse in sorted(matches, key=lambda item: item[0].start()):
        if is_reverse:
            tail = normalized[match.end() : match.end() + 2]
            if tail.startswith(("头", "尾")):
                continue
        value = int(normalize_digit_text(match.group(1)))
        if not 1 <= value <= 13:
            return True
    return False


def is_valid_success_value(value: str) -> bool:
    return bool(STRICT_SUCCESS_VALUE_RE.fullmatch(normalize_digit_text(value.strip())))


def score_candidate(line: str, values: list[str]) -> int:
    score = 0
    normalized = normalize_text(line)
    if re.search(r"绝\s*杀\s*一\s*合|絕\s*殺\s*一\s*合", normalized):
        score += 100
    if re.search(r"公式\s*杀\s*合|公式\s*殺\s*合", normalized):
        score += 90
    if re.search(r"绝\s*杀\s*合|絕\s*殺\s*合", normalized):
        score += 80
    if WEAK_KILL_SUM_RE.search(normalized):
        score += 40
    if "开" in normalized:
        score += 10
    if "属性" in normalized:
        score -= 20
    score += len(values)
    score -= min(len(line), 300) // 100
    return score


def is_table_kill_sum_chunk(text: str) -> bool:
    normalized = normalize_text(text)
    return "期数" in normalized and "杀合" in normalized


def has_body_locator(text: str) -> bool:
    return bool(BODY_LOCATOR_RE.search(normalize_text(text)))


def build_table_period_parts(chunk: str, period: int) -> list[str]:
    segments = split_period_segments(chunk, period)
    if not segments:
        return []

    match = PERIOD_RE.search(normalize_text(chunk))
    if not match:
        return segments

    header = normalize_text(chunk[: match.start()])
    if not header:
        return segments

    return [normalize_text(f"{header} {segment}") for segment in segments]


def period_numbers_in_text(text: str) -> list[int]:
    return [
        int(match.group(1))
        for match in PERIOD_RE.finditer(normalize_digit_text(normalize_text(text)))
    ]


def is_strict_current_candidate(
    line: str, period: int, allow_weak: bool = False, value_count: int = 1
) -> bool:
    normalized = normalize_digit_text(normalize_text(line))
    periods = period_numbers_in_text(normalized)
    if not periods or periods[0] != period:
        return False
    if any(item != period for item in periods):
        return False
    if not has_kill_sum_keyword(normalized, allow_weak, value_count):
        return False
    values = extract_values(normalized, allow_weak, value_count)
    return len(values) == value_count and all(is_valid_success_value(value) for value in values)


def is_strict_period_candidate(line: str, allow_weak: bool = False, value_count: int = 1) -> bool:
    normalized = normalize_digit_text(normalize_text(line))
    periods = period_numbers_in_text(normalized)
    if not periods:
        return False
    if any(item != periods[0] for item in periods):
        return False
    if not has_kill_sum_keyword(normalized, allow_weak, value_count):
        return False
    values = extract_values(normalized, allow_weak, value_count)
    return len(values) == value_count and all(is_valid_success_value(value) for value in values)


def is_contained_duplicate_candidate(
    line: str,
    values: list[str],
    existing: list[str],
    allow_weak: bool = False,
    value_count: int = 1,
) -> bool:
    normalized = normalize_digit_text(normalize_text(line))
    for earlier in existing:
        earlier_normalized = normalize_digit_text(normalize_text(earlier))
        if earlier_normalized == normalized:
            return True
        if earlier_normalized in normalized and extract_values(
            earlier_normalized, allow_weak, value_count
        ) == values:
            return True
    return False


def build_candidate_parts(
    documents: list[str], period: int, allow_weak: bool = False, value_count: int = 1
) -> list[tuple[str, bool]]:
    parts: list[tuple[str, bool]] = []
    seen: set[tuple[str, bool]] = set()
    accepted_lines: list[str] = []
    for document in documents:
        if not has_kill_sum_keyword(document, allow_weak, value_count):
            continue
        document_has_locator = has_body_locator(document)
        for chunk in candidate_chunks(document, period):
            chunk_has_locator = document_has_locator or has_body_locator(chunk) or is_table_kill_sum_chunk(chunk)
            if is_table_kill_sum_chunk(chunk):
                candidates = build_table_period_parts(chunk, period) or [chunk]
            else:
                candidates = split_period_segments(chunk, period) or [chunk]

            for candidate in candidates:
                line = normalize_text(candidate)
                if not line or len(line) > 260:
                    continue
                if not is_strict_current_candidate(line, period, allow_weak, value_count):
                    continue
                values = extract_values(line, allow_weak, value_count)
                if is_contained_duplicate_candidate(
                    line, values, accepted_lines, allow_weak, value_count
                ):
                    continue
                key = (line, chunk_has_locator)
                if key in seen:
                    continue
                seen.add(key)
                accepted_lines.append(line)
                parts.append((line, chunk_has_locator))
    return parts


def latest_candidate_chunks(document: str) -> list[str]:
    chunks: list[str] = []
    soup = BeautifulSoup(document, "html.parser")

    for tag in soup.find_all(["p", "div", "td", "tr", "li", "font", "span", "b", "strong"]):
        text = normalize_text(tag.get_text(" ", strip=True))
        if text:
            chunks.append(text)

    lines = [normalize_text(line) for line in soup.get_text("\n", strip=True).splitlines()]
    chunks.extend(line for line in lines if line)

    all_text = normalize_text(soup.get_text(" ", strip=True) or document)
    chunks.extend(split_all_period_segments(all_text))
    return chunks


def build_latest_candidate_parts(
    documents: list[str],
    allow_weak: bool = False,
    stop_after: int | None = None,
    value_count: int = 1,
) -> list[tuple[str, bool]]:
    parts: list[tuple[str, bool]] = []
    seen: set[tuple[str, bool]] = set()
    accepted_lines: list[str] = []
    for document in documents:
        if not has_kill_sum_keyword(document, allow_weak, value_count):
            continue
        document_has_locator = has_body_locator(document)
        for chunk in latest_candidate_chunks(document):
            chunk_has_locator = document_has_locator or has_body_locator(chunk) or is_table_kill_sum_chunk(chunk)
            candidates = split_all_period_segments(chunk) or [chunk]

            for candidate in candidates:
                line = normalize_text(candidate)
                if not line or len(line) > 260:
                    continue
                if not is_strict_period_candidate(line, allow_weak, value_count):
                    continue
                values = extract_values(line, allow_weak, value_count)
                if is_contained_duplicate_candidate(
                    line, values, accepted_lines, allow_weak, value_count
                ):
                    continue
                key = (line, chunk_has_locator)
                if key in seen:
                    continue
                seen.add(key)
                accepted_lines.append(line)
                parts.append((line, chunk_has_locator))
                if stop_after is not None and len(parts) >= stop_after:
                    return parts
    return parts


def directional_candidate_window_parts(
    all_parts: list[tuple[str, bool]],
    pick: str,
    size: int = 1,
) -> list[tuple[str, bool]]:
    """Select only the positional edge row from one candidate block.

    The legacy ``size`` argument is retained for API compatibility.  A
    direction is a hard boundary now: rows after a ``top`` target (or before
    a ``bottom`` target) cannot make that target eligible.
    """
    pick = normalize_pick(pick)
    if not all_parts:
        return []
    return [all_parts[0]] if pick == "top" else [all_parts[-1]]


def candidate_window_key(
    line: str, allow_weak: bool = False, value_count: int = 1
) -> tuple[int, str] | None:
    periods = period_numbers_in_text(line)
    values = extract_values(line, allow_weak, value_count)
    if not periods or len(values) != value_count:
        return None
    return periods[0], ",".join(values)


def window_basis_parts(
    all_parts: list[tuple[str, bool]],
    current_parts: list[tuple[str, bool]],
    period: int,
    allow_weak: bool = False,
) -> list[tuple[str, bool]]:
    # Candidate order is the evidence.  Never sort or filter by period number
    # when constructing the direction basis.  ``current_parts`` is used only
    # when the full document produced no candidate rows at all.
    return list(all_parts) if all_parts else list(current_parts)


def current_candidates_outside_window(
    documents: list[str],
    period: int,
    pick: str,
    require_body_locator: bool = True,
    allow_weak: bool = False,
    value_count: int = 1,
) -> bool:
    current_parts = build_candidate_parts(documents, period, allow_weak, value_count)
    all_parts = build_latest_candidate_parts(documents, allow_weak, value_count=value_count)
    if require_body_locator:
        current_parts = [(line, has_locator) for line, has_locator in current_parts if has_locator]
        all_parts = [(line, has_locator) for line, has_locator in all_parts if has_locator]
    all_parts = window_basis_parts(all_parts, current_parts, period, allow_weak)
    if not current_parts or not all_parts:
        return False
    edge = directional_candidate_window_parts(all_parts, pick, 1)
    edge_key = candidate_window_key(edge[0][0], allow_weak, value_count) if edge else None
    return edge_key is None or edge_key[0] != period


def pick_region_label(pick: str) -> str:
    pick = normalize_pick(pick)
    if pick == "top":
        return "候选数据靠前位置"
    if pick == "bottom":
        return "候选数据靠后位置"
    return "候选数据区域"


def directional_three_label(pick: str) -> str:
    return "第一条" if normalize_pick(pick) == "top" else "最后一条"


def find_candidate(
    documents: list[str],
    period: int,
    pick: str,
    require_body_locator: bool = True,
    allow_weak: bool = False,
    value_count: int = 1,
) -> Candidate | None:
    parts = trusted_current_candidate_parts(
        documents, period, pick, require_body_locator, allow_weak, value_count
    )
    return select_candidate_from_trusted_parts(parts, pick, allow_weak, value_count)


def select_candidate_from_trusted_parts(
    parts: list[tuple[str, bool]],
    pick: str,
    allow_weak: bool = False,
    value_count: int = 1,
) -> Candidate | None:
    pick = normalize_pick(pick)
    candidates: list[Candidate] = []
    seen: set[tuple[str, str]] = set()
    order = 0
    for line, _ in parts:
        values = extract_values(line, allow_weak, value_count)
        if len(values) != value_count:
            continue

        values_text = ",".join(values)
        key = (values_text, line)
        if key in seen:
            continue
        seen.add(key)

        candidates.append(
            Candidate(
                values=values_text,
                line=line,
                score=score_candidate(line, values),
                order=order,
            )
        )
        order += 1

    if not candidates:
        return None

    if pick == "top":
        return min(candidates, key=lambda item: item.order)
    if pick == "bottom":
        return max(candidates, key=lambda item: item.order)
    best_score = max(candidate.score for candidate in candidates)
    good = [candidate for candidate in candidates if candidate.score >= best_score - 15]
    return max(good, key=lambda item: item.order)


def conflict_values_from_trusted_parts(
    parts: list[tuple[str, bool]],
    allow_weak: bool = False,
    value_count: int = 1,
) -> tuple[list[str], list[str]]:
    values_by_line: list[tuple[str, str]] = []
    seen_lines: set[str] = set()
    for line, _ in parts:
        if line in seen_lines:
            continue
        seen_lines.add(line)
        values = extract_values(line, allow_weak, value_count)
        if len(values) != value_count:
            continue
        values_by_line.append((",".join(values), line))
    unique_values = sorted({value for value, _ in values_by_line}, key=lambda value: int(value[:2]))
    if len(unique_values) <= 1:
        return [], []
    return unique_values, [line for _, line in values_by_line]


def trusted_candidate_with_conflict(
    documents: list[str],
    period: int,
    pick: str,
    require_body_locator: bool = True,
    allow_weak: bool = False,
    value_count: int = 1,
) -> tuple[Candidate | None, list[str], list[str]]:
    parts = trusted_current_candidate_parts(
        documents, period, pick, require_body_locator, allow_weak, value_count
    )
    conflict_values, conflict_lines = conflict_values_from_trusted_parts(
        parts, allow_weak, value_count
    )
    if conflict_values:
        return None, conflict_values, conflict_lines
    return select_candidate_from_trusted_parts(parts, pick, allow_weak, value_count), [], []


def trusted_current_candidate_parts(
    documents: list[str],
    period: int,
    pick: str,
    require_body_locator: bool = True,
    allow_weak: bool = False,
    value_count: int = 1,
) -> list[tuple[str, bool]]:
    pick = normalize_pick(pick)
    parts = build_candidate_parts(documents, period, allow_weak, value_count)
    if require_body_locator:
        parts = [(line, has_locator) for line, has_locator in parts if has_locator]
    all_parts = build_latest_candidate_parts(documents, allow_weak, value_count=value_count)
    if require_body_locator:
        all_parts = [(line, has_locator) for line, has_locator in all_parts if has_locator]
    all_parts = window_basis_parts(all_parts, parts, period, allow_weak)
    edge = directional_candidate_window_parts(all_parts, pick, 1)
    if not edge:
        return []
    edge_key = candidate_window_key(edge[0][0], allow_weak, value_count)
    if edge_key is None or edge_key[0] != period:
        return []

    # Prefer the target-period representation from ``parts`` so evidence and
    # diagnostics retain the original row text.  Require the same normalized
    # line as the edge row when possible; matching only on period would let an
    # interior duplicate masquerade as the edge candidate.
    edge_line = normalize_digit_text(normalize_text(edge[0][0]))
    matching = [
        part
        for part in parts
        if normalize_digit_text(normalize_text(part[0])) == edge_line
    ]
    return matching[:1] if matching else edge[:1]


def diagnose_candidate_state(
    documents: list[str], period: int, allow_weak: bool = False, value_count: int = 1
) -> dict[str, bool]:
    state = {
        "has_period": False,
        "has_keyword": False,
        "has_valid_value": False,
        "has_multiple_values": False,
        "has_strict_candidate": False,
        "has_locator": False,
    }

    for document in documents:
        document_has_locator = has_body_locator(document)
        for chunk in candidate_chunks(document, period):
            chunk_has_locator = document_has_locator or has_body_locator(chunk) or is_table_kill_sum_chunk(chunk)
            candidates = split_period_segments(chunk, period) or [chunk]
            for candidate in candidates:
                line = normalize_digit_text(normalize_text(candidate))
                periods = period_numbers_in_text(line)
                if period not in periods:
                    continue
                state["has_period"] = True
                if not has_kill_sum_keyword(line, allow_weak, value_count):
                    continue
                state["has_keyword"] = True
                values = extract_values(line, allow_weak, value_count)
                if len(values) > 1:
                    state["has_multiple_values"] = True
                if len(values) == value_count and all(is_valid_success_value(value) for value in values):
                    state["has_valid_value"] = True
                if is_strict_current_candidate(line, period, allow_weak, value_count):
                    state["has_strict_candidate"] = True
                    if chunk_has_locator:
                        state["has_locator"] = True

    return state


def analyze_missing_reason(
    documents: list[str],
    period: int,
    pick: str = "",
    allow_weak: bool = False,
    value_count: int = 1,
) -> tuple[str, str]:
    parts = build_candidate_parts(documents, period, allow_weak, value_count)
    region = pick_region_label(pick)
    if not parts:
        state = diagnose_candidate_state(documents, period, allow_weak)
        if not state["has_period"]:
            return "无当期", f"{region}里没找到{period}期"
        if not state["has_keyword"]:
            return "无关键词", f"{region}找到{period}期，但没匹配到严格抓取词"
        if state["has_multiple_values"]:
            return "数据不完整", f"{region}找到{period}期和严格抓取词，但数量校验失败"
        if not state["has_valid_value"]:
            return "数据不完整", f"{region}找到{period}期和严格抓取词，但没提取到有效合数"
        return "未找到目标", f"{region}里没找到{period}期的严格杀合内容"

    if not any(has_locator for _, has_locator in parts):
        return "无正文定位", f"{region}找到{period}期，但没匹配到正文定位词"

    quantity_failed = False
    for line, _ in parts:
        values = extract_values(line, allow_weak, value_count)
        if len(values) > 1:
            quantity_failed = True

    if quantity_failed:
        return "数据不完整", f"{region}找到{period}期、正文定位词和严格抓取词，但数量校验失败"

    return "数据不完整", f"{region}找到{period}期、正文定位词和严格抓取词，但没提取到有效合数"


def classify_failure(period: int, detail: str, error: str | None) -> FailureInfo:
    normalized_error = (error or "").strip()
    normalized_detail = (detail or "").strip()

    if normalized_error:
        if "未抓到可参与重复检测的数据" in normalized_error or "未找到完整数据" in normalized_error:
            return FailureInfo("未找到目标", normalized_error)
        if "curl exit" in normalized_error:
            return FailureInfo("请求失败", f"curl 连接失败: {normalized_error}")
        if "net::ERR_CONNECTION_CLOSED" in normalized_error or "net::ERR_CONNECTION_RESET" in normalized_error:
            return FailureInfo("请求失败", "浏览器访问时连接被远端关闭或重置")
        request_markers = (
            "Timeout",
            "ConnectionError",
            "CalledProcessError",
            "curl exit",
            "HTTP Error",
            "HTTPError",
            "TooManyRedirects",
            "SSLError",
            "ProxyError",
            "ChunkedEncodingError",
            "RequestException",
            "HTTPConnectionPool",
            "HTTPSConnectionPool",
            "net::ERR_",
        )
        category = "请求失败" if any(marker in normalized_error for marker in request_markers) else "执行失败"
        return FailureInfo(category, normalized_error)

    if normalized_detail == "未执行":
        return FailureInfo("未执行", "站点任务未执行")

    if "没找到作者锚点" in normalized_detail:
        return FailureInfo("锚点缺失", normalized_detail)
    if "澳门综合杀表格里没找到" in normalized_detail or "绝杀①段①合表格里没找到" in normalized_detail:
        return FailureInfo("专属缺期", normalized_detail)
    if re.search(r"里没找到\d{1,4}期", normalized_detail):
        return FailureInfo("无当期", normalized_detail)
    if "没匹配到严格抓取词" in normalized_detail:
        return FailureInfo("无关键词", normalized_detail)
    if "没匹配到正文定位词" in normalized_detail:
        return FailureInfo("无正文定位", normalized_detail)
    if "作者块下没找到" in normalized_detail:
        return FailureInfo("作者块无当期", normalized_detail)

    if normalized_detail.startswith("页面里没找到"):
        return FailureInfo("无当期", normalized_detail)
    if "数量校验失败" in normalized_detail:
        return FailureInfo("数据不完整", normalized_detail)
    if "没提取到有效合数" in normalized_detail:
        return FailureInfo("数据不完整", normalized_detail)
    if normalized_detail:
        return FailureInfo("未找到目标", normalized_detail)

    return FailureInfo("未找到目标", f"页面里没找到{period}期的严格杀合内容")


def failure_stage(category: str) -> str:
    if category in {"请求失败", "执行失败"}:
        return "网络请求"
    if category in {"锚点缺失", "无正文定位", "记录ID缺失/不一致"}:
        return "目标定位"
    if category in {"数据不完整", "候选冲突", "区块冲突", "来源冲突"}:
        return "数据校验"
    if category in {"缓存不可用", "缓存冲突"}:
        return "缓存校验"
    if category in {"重复拒收", "判重未完成"}:
        return "重复检测"
    if category == "未执行":
        return "任务执行"
    return "指定期数校验"


def format_failure_result(
    period: int,
    site: Site,
    detail: str,
    error: str | None,
    category: str | None = None,
) -> str:
    if category and not error:
        failure = FailureInfo(category, detail.strip() or f"页面里没找到{period}期的严格杀合内容")
    else:
        failure = classify_failure(period, detail, error)
    reason = " ".join(failure.reason.split())
    site_prefix = f"{site.name} "
    if reason.startswith(site_prefix):
        reason = reason[len(site_prefix) :].lstrip()
    return (
        f"失败 {site.name} {site.url} 站点ID: {site.site_id or '未配置'} "
        f"方向: {site.pick} 期数: {period} 阶段: {failure_stage(failure.category)} "
        f"失败类型: {failure.category} 具体原因: {reason}"
    )


__all__ = [
    name
    for name, value in globals().items()
    if callable(value) and getattr(value, "__module__", None) == __name__
]
