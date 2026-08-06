from he_app.domain.models import Site
from he_app.parsers.dedicated.history import (
    extract_hushuobadao_kill_sum_period_values,
    extract_kill_sum_period_values,
    extract_ruyimutan_kill_sum_period_values,
)
from he_app.parsers.dedicated.structured import (
    DAJIAFA_SITE_ID,
    YIAIZHIMING_SITE_ID,
    extract_dajiafa_kill_sum_period_values,
    extract_yiaizhimin_kill_sum_period_values,
)
from he_app.parsers.dedicated.tables import site_rule
from he_app.parsers.dedicated.kaijiangfacai import (
    KAIJIANGFACAI_SITE_ID,
    extract_kaijiangfacai_kill_sum_period_values,
)
from he_app.parsers.dedicated.tables import (
    extract_jiuxiao_kill_sum_period_values,
    extract_woman_flavor_sum_period_values,
)
from he_app.parsers.dedicated.ttss import (
    TTSS_SITE_IDS,
    extract_ttss_kill_sum_period_values,
    find_ttss_history_candidate,
)
from he_app.parsers.registry import REGISTRY
from he_app.services.single_period import build_document_evidence, parse_site_period
from he_app.validation.period import is_valid_sum_value


Fingerprint = dict[int, str]


def _strict_bulk_values(site: Site, documents: list[str]) -> dict[int, str]:
    if site.site_id == "s025_topic_435508":
        return extract_hushuobadao_kill_sum_period_values(documents)
    if site.site_id == "s109_topic_222783":
        return extract_ruyimutan_kill_sum_period_values(documents)
    if site.site_id == YIAIZHIMING_SITE_ID:
        return extract_yiaizhimin_kill_sum_period_values(documents)
    if site.site_id == DAJIAFA_SITE_ID:
        return extract_dajiafa_kill_sum_period_values(documents)
    if site.site_id == "s073_shuqhbq":
        return extract_jiuxiao_kill_sum_period_values(documents)
    if site.site_id == "s085_kcvpleh":
        return extract_woman_flavor_sum_period_values(documents)
    if site.site_id == KAIJIANGFACAI_SITE_ID:
        return extract_kaijiangfacai_kill_sum_period_values(documents)
    if site.site_id in TTSS_SITE_IDS:
        return extract_ttss_kill_sum_period_values(documents, site.name)
    if site.site_id not in REGISTRY._parsers:
        rule = site_rule(site)
        return extract_kill_sum_period_values(
            documents,
            pick=site.pick,
            allow_weak=rule.allow_weak_kill_sum_keyword,
        )
    return {}


def build_site_fingerprint(site: Site, documents: list[str], period: int, periods: int) -> Fingerprint:
    if periods < 1:
        raise ValueError("periods 必须大于等于 1")

    fingerprint: Fingerprint = {}
    bulk_values = _strict_bulk_values(site, documents)
    for current_period in range(period, period - periods, -1):
        value = bulk_values.get(current_period)
        values = value.split(",") if value is not None else []
        if values and all(is_valid_sum_value(item) for item in values):
            if site.site_id in TTSS_SITE_IDS and len(values) == 1:
                history_candidate = find_ttss_history_candidate(
                    documents, current_period, site.name
                )
                if history_candidate is not None:
                    evidence = build_document_evidence(
                        site,
                        current_period,
                        values,
                        history_candidate.line,
                        documents,
                    )
                    if evidence:
                        fingerprint[current_period] = value
                        continue
            detail = f"{current_period}期严格历史指纹 {value}"
            if site.site_id == KAIJIANGFACAI_SITE_ID:
                detail = (
                    f"{current_period}期开奖发财 综合杀料 杀合[{value}]"
                )
            evidence = build_document_evidence(
                site,
                current_period,
                values,
                detail,
                documents,
            )
            if evidence:
                fingerprint[current_period] = value
                continue

        parsed = parse_site_period(site, current_period, documents)
        if not parsed.success or not parsed.evidence:
            continue
        fingerprint[current_period] = ",".join(parsed.evidence[0].values)
    return fingerprint
