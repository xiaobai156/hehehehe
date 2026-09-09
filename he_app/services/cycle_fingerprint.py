from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Mapping

from bs4 import BeautifulSoup

from he_app.domain.models import CandidateEvidence, Site
from he_app.domain.periods import PeriodContext, PeriodKey
from he_app.domain.policies import normalize_digit_text, normalize_text
from he_app.parsers.common import (
    build_latest_candidate_parts,
    extract_values,
    period_numbers_in_text,
)
from he_app.parsers.dedicated.history import BATCH_NEW_DEDICATED_SITE_IDS
from he_app.parsers.dedicated.kaijiangfacai import KAIJIANGFACAI_SITE_ID
from he_app.parsers.dedicated.structured import DAJIAFA_SITE_ID, YIAIZHIMING_SITE_ID
from he_app.parsers.dedicated.tables import site_rule
from he_app.parsers.dedicated.ttss import TTSS_SITE_IDS
from he_app.parsers.registry import REGISTRY
from he_app.services.fingerprint import _strict_bulk_values
from he_app.services.single_period import parse_site_period
from he_app.validation.period import is_valid_sum_value


@dataclass(frozen=True, slots=True)
class CycleFingerprintResult:
    current_value: str
    fingerprint: dict[PeriodKey, str]
    adapter: str
    history_error: str | None = None
    evidence: tuple[CandidateEvidence, ...] = ()


def _value_count(site: Site) -> int:
    count = getattr(site, "value_count", 1)
    return count if count in {1, 2} else 1


def _valid_value_text(site: Site, value: str) -> bool:
    values = [item.strip() for item in str(value).split(",") if item.strip()]
    return (
        len(values) == _value_count(site)
        and len(set(values)) == len(values)
        and all(is_valid_sum_value(item) for item in values)
    )


def history_adapter_kind(site: Site) -> str:
    site_id = site.site_id
    if site_id in TTSS_SITE_IDS:
        return "ttss-article"
    if site_id == YIAIZHIMING_SITE_ID:
        return "manager-record"
    if site_id == DAJIAFA_SITE_ID:
        return "structured-topic"
    if site_id == KAIJIANGFACAI_SITE_ID:
        return "kaijiang-table"
    if site_id == "s073_shuqhbq":
        return "jiuxiao-table"
    if site_id == "s085_kcvpleh":
        return "woman-flavor-table"
    if site_id in {"s025_topic_435508", "s109_topic_222783"}:
        return "dedicated-history"
    if site_id in BATCH_NEW_DEDICATED_SITE_IDS:
        return "dedicated-batch-block"
    if site_id in REGISTRY._parsers:
        return "verified-dedicated-block"
    return "verified-generic-block"


def _matching_authority_documents(
    documents: list[str],
    evidence: tuple[CandidateEvidence, ...],
) -> list[str]:
    if not evidence:
        return []
    first = evidence[0]
    authority = first.authority_id
    record_id = first.article_id
    matching = [
        document
        for document in documents
        if (
            authority
            and authority != "legacy"
            and str(getattr(document, "authority_id", "")) == authority
        )
        or (
            record_id
            and str(getattr(document, "record_id", "") or "") == record_id
        )
    ]
    if matching:
        return matching
    document_id = first.document_id
    matching = [
        document
        for document in documents
        if str(getattr(document, "document_id", "")) == document_id
    ]
    if matching:
        return matching
    # Legacy fixture documents have no source metadata.  They remain isolated
    # by the caller and are never combined with a live physical authority.
    return documents[:1] if documents else []


def _candidate_value(site: Site, line: str, allow_weak: bool) -> str | None:
    values = extract_values(line, allow_weak)
    if _value_count(site) == 1:
        if len(values) != 1:
            return None
        value = values[0]
    else:
        if len(values) != 2:
            return None
        value = ",".join(values)
    return value if _valid_value_text(site, value) else None


def _plain_text_record_slice(site: Site, document: str, current_line: str) -> str:
    normalized = normalize_digit_text(
        normalize_text(BeautifulSoup(document, "html.parser").get_text(" ", strip=True) or document)
    )
    rule = site_rule(site)
    anchor = normalize_digit_text(normalize_text(rule.anchor_text))
    if anchor:
        positions = [match.start() for match in re.finditer(re.escape(anchor), normalized)]
        current_position = normalized.find(normalize_digit_text(normalize_text(current_line)))
        eligible = [position for position in positions if current_position < 0 or position <= current_position]
        if eligible:
            normalized = normalized[max(eligible) :]
    stop = re.search(
        r"(?:上一篇|下一篇|Copyright|澳彩合数属性|澳门六合彩合数属性|"
        r"另一个资料块|另一(?:个|组|版)资料)",
        normalized,
    )
    return normalized[: stop.start()] if stop is not None else normalized


def _html_record_candidates(site: Site, document: str, current_line: str) -> list[str]:
    soup = BeautifulSoup(document, "html.parser")
    if soup.find() is None:
        return [_plain_text_record_slice(site, document, current_line)]
    target = normalize_digit_text(normalize_text(current_line))
    containers: list[str] = []
    for node in soup.find_all(["tr", "p", "li", "div", "article", "section", "table"]):
        node_text = normalize_digit_text(normalize_text(node.get_text(" ", strip=True)))
        if not node_text or target not in node_text:
            continue
        period_count = len(set(period_numbers_in_text(node_text)))
        if period_count < 2:
            continue
        if len(node_text) > 30000:
            continue
        containers.append(node_text)
    if containers:
        containers.sort(key=lambda text: (len(text), -len(set(period_numbers_in_text(text)))))
        return [containers[0]]
    return [_plain_text_record_slice(site, document, current_line)]


def _generic_verified_history(
    site: Site,
    documents: list[str],
    evidence: tuple[CandidateEvidence, ...],
) -> dict[int, str]:
    if not evidence:
        return {}
    rule = site_rule(site)
    current_line = evidence[0].raw_line
    selected_documents = _matching_authority_documents(documents, evidence)
    if not selected_documents:
        return {}

    values: dict[int, str] = {}
    conflicts: set[int] = set()
    for document in selected_documents:
        for record_text in _html_record_candidates(site, document, current_line):
            parts = build_latest_candidate_parts(
                [record_text],
                allow_weak=rule.allow_weak_kill_sum_keyword,
            )
            if rule.require_body_locator:
                parts = [part for part in parts if part[1]]
            for line, _has_locator in parts:
                periods = period_numbers_in_text(line)
                if not periods or any(number != periods[0] for number in periods):
                    continue
                period = periods[0]
                value = _candidate_value(
                    site,
                    line,
                    rule.allow_weak_kill_sum_keyword,
                )
                if value is None or period in conflicts:
                    continue
                previous = values.get(period)
                if previous is None:
                    values[period] = value
                elif previous != value:
                    values.pop(period, None)
                    conflicts.add(period)
    return values


def _merge_numeric_history(
    sources: Iterable[Mapping[int, str]],
) -> tuple[dict[int, str], set[int]]:
    merged: dict[int, str] = {}
    conflicts: set[int] = set()
    for source in sources:
        for period, value in source.items():
            if period in conflicts:
                continue
            previous = merged.get(period)
            if previous is None:
                merged[period] = value
            elif previous != value:
                merged.pop(period, None)
                conflicts.add(period)
    return merged, conflicts


def build_cycle_site_fingerprint(
    site: Site,
    documents: list[str],
    context: PeriodContext,
) -> CycleFingerprintResult:
    parsed = parse_site_period(site, context.current.number, documents)
    if not parsed.success or not parsed.value or not parsed.evidence:
        reason = parsed.failure.reason if parsed.failure is not None else "current period did not validate"
        raise ValueError(reason)

    current_values = tuple(parsed.evidence[0].values)
    current_value = ",".join(current_values)
    if not _valid_value_text(site, current_value):
        raise ValueError(f"invalid current value count for {site.site_id}: {current_value}")

    same_authority = _matching_authority_documents(documents, parsed.evidence)
    dedicated = _strict_bulk_values(site, same_authority or documents)
    generic = _generic_verified_history(site, same_authority or documents, parsed.evidence)
    numeric_history, conflicts = _merge_numeric_history((dedicated, generic))
    if context.current.number in conflicts:
        raise ValueError(
            f"current period {context.current.number} has conflicting history values"
        )
    existing_current = numeric_history.get(context.current.number)
    if existing_current is not None and existing_current != current_value:
        raise ValueError(
            f"current parser/history mismatch: {current_value} != {existing_current}"
        )
    numeric_history[context.current.number] = current_value

    fingerprint: dict[PeriodKey, str] = {}
    missing: list[str] = []
    for key in context.window:
        value = numeric_history.get(key.number)
        if value is None:
            missing.append(key.token())
            continue
        if not _valid_value_text(site, value):
            continue
        fingerprint[key] = value

    history_error = None
    if len(fingerprint) < context.periods:
        history_error = (
            f"verified history {len(fingerprint)}/{context.periods}; "
            f"missing: {', '.join(missing[:10])}"
        )
    return CycleFingerprintResult(
        current_value=current_value,
        fingerprint=fingerprint,
        adapter=history_adapter_kind(site),
        history_error=history_error,
        evidence=parsed.evidence,
    )


__all__ = [
    "CycleFingerprintResult",
    "build_cycle_site_fingerprint",
    "history_adapter_kind",
]
