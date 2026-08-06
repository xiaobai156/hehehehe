import re

from he_app.domain.policies import normalize_digit_text


SUM_VALUE_RE = re.compile(r"^(?:0[1-9]|1[0-3])合$")


def is_valid_sum_value(value: str) -> bool:
    return bool(SUM_VALUE_RE.fullmatch(normalize_digit_text(value.strip())))


def contains_exact_period(text: str, period: int) -> bool:
    return bool(re.search(rf"(?<!\d){re.escape(str(period))}\s*期", normalize_digit_text(text)))

