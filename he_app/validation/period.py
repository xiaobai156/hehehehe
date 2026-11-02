import re

from he_app.domain.policies import normalize_digit_text


SUM_VALUE_RE = re.compile(r"^(?:0[1-9]|1[0-3])合$")


def is_valid_sum_value(value: str) -> bool:
    return bool(SUM_VALUE_RE.fullmatch(normalize_digit_text(value.strip())))


def contains_exact_period(text: str, period: int) -> bool:
    return bool(re.search(rf"(?<!\d){re.escape(str(period))}\s*期", normalize_digit_text(text)))


# 「命中非指定期」的私有标记，由 crawl 层的下一期兜底写入 outcome 的第 6 个字段。
# 该字段平时承载失败分类/元数据，因此用前缀严格区分，避免与失败分类字符串混淆。
MATCHED_PERIOD_PREFIX = "__matched_period__:"

# 标记存在但无法解析时使用的哨兵期号：保证「标记坏了」被判为跨期借值（fail-closed），
# 而不是被当成「没有标记」按当期成功放行。
INVALID_MATCHED_PERIOD = 0


def has_matched_period_marker(reason: str | None) -> bool:
    return bool(reason) and reason.startswith(MATCHED_PERIOD_PREFIX)


def parse_matched_period(reason: str | None) -> int | None:
    """解析 outcome 元数据里的真实命中期号。

    返回值约定：
    - ``None``：没有标记，说明是当期正常命中；
    - 正整数：真实的非指定期期号；
    - ``INVALID_MATCHED_PERIOD``（0）：有标记但内容非法，调用方必须按借值/失败处理。

    这是跨期借值判定的唯一入口：crawler（缓存分流）、runner（计数与失败清单）
    与 storage/reports（成功 TXT 落盘）必须共用它，否则会出现一边判借值、另一边
    按当期写缓存的情况。
    """
    if not has_matched_period_marker(reason):
        return None
    raw = reason[len(MATCHED_PERIOD_PREFIX):].strip()
    # isdecimal 而非 isdigit：'²'.isdigit() 为 True 但 int('²') 会抛异常。
    if not raw.isdecimal():
        return INVALID_MATCHED_PERIOD
    try:
        matched = int(raw)
    except (TypeError, ValueError):  # 超长数字等极端输入
        return INVALID_MATCHED_PERIOD
    return matched if matched >= 1 else INVALID_MATCHED_PERIOD


def matched_period_or_requested(requested_period: int, reason: str | None) -> int:
    """返回结果真实所属期号；无标记时视为指定期（当期正常命中）。"""
    matched = parse_matched_period(reason)
    return requested_period if matched is None else matched

