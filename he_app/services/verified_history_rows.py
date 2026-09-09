from __future__ import annotations

from collections import defaultdict

from bs4 import BeautifulSoup

from he_app.domain.models import Site
from he_app.domain.policies import normalize_digit_text, normalize_text
from he_app.parsers.common import extract_values, period_numbers_in_text
from he_app.parsers.dedicated.tables import extract_values_from_kill_sum_row, site_rule
from he_app.services.single_period import (
    _candidate_segments,
    _dedicated_document_keyword,
    _special_segment_is_bounded,
)
from he_app.validation.period import is_valid_sum_value


_BOUNDED_SPECIAL_SITE_IDS = {"s058_vkjwinyt", "s073_shuqhbq", "s085_kcvpleh"}


def _value_count(site: Site) -> int:
    count = int(getattr(site, "value_count", 1))
    return count if count in {1, 2} else 1


def _segment_values(site: Site, segment: str) -> list[str]:
    rule = site_rule(site)
    values = extract_values(segment, allow_weak=rule.allow_weak_kill_sum_keyword)
    if not values:
        values = extract_values_from_kill_sum_row(segment)
    required = _value_count(site)
    if len(values) != required or len(set(values)) != len(values):
        return []
    return values if all(is_valid_sum_value(value) for value in values) else []


def extract_verified_history_rows(site: Site, record_documents: list[str]) -> dict[int, str]:
    """Extract history only from documents already locked to current evidence.

    Each row must contain one exact period, the expected number of valid sum
    values, and a site-specific or strict kill-sum keyword.  Special table
    sites must additionally remain inside their verified section boundary.
    Conflicting values remove that period entirely.
    """

    candidates: dict[int, set[str]] = defaultdict(set)
    for document in record_documents:
        rendered = BeautifulSoup(document, "html.parser").get_text(" ", strip=True) or str(document)
        normalized_document = normalize_digit_text(normalize_text(rendered))
        periods = list(dict.fromkeys(period_numbers_in_text(normalized_document)))
        for period in periods:
            for segment, start, end in _candidate_segments(document, period):
                if start < 0 or end <= start:
                    continue
                segment_periods = period_numbers_in_text(segment)
                if not segment_periods or any(value != period for value in segment_periods):
                    continue
                values = _segment_values(site, segment)
                if not values:
                    continue
                if _dedicated_document_keyword(site, period, values, segment) is None:
                    continue
                if (
                    site.site_id in _BOUNDED_SPECIAL_SITE_IDS
                    and not _special_segment_is_bounded(
                        site.site_id,
                        normalized_document,
                        start,
                        end,
                    )
                ):
                    continue
                candidates[period].add(",".join(values))

    return {
        period: next(iter(values))
        for period, values in candidates.items()
        if len(values) == 1
    }


__all__ = ["extract_verified_history_rows"]
