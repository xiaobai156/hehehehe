from he_app.domain.models import Site
from he_app.parsers.dedicated.history import (
    extract_anchor_kill_sum_period_values,
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
from he_app.parsers.dedicated.gucheng import (
    GUCHENG_SITE_ID,
    extract_gucheng_kill_sum_period_values,
    find_gucheng_history_candidate,
)
from he_app.parsers.dedicated.article_content import (
    ARTICLE_CONTENT_SITE_IDS,
    extract_article_content_period_values,
    find_article_content_history_candidate,
)
from he_app.parsers.dedicated.kaijiangfacai import (
    KAIJIANGFACAI_SITE_ID,
    extract_kaijiangfacai_kill_sum_period_values,
)
from he_app.parsers.dedicated.blackpepper import (
    BLACKPEPPER_SITE_ID,
    extract_blackpepper_period_values,
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
    if site.site_id == GUCHENG_SITE_ID:
        return extract_gucheng_kill_sum_period_values(site, documents)
    if site.site_id in ARTICLE_CONTENT_SITE_IDS:
        return extract_article_content_period_values(site, documents)
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
    if site.value_count == 2:
        rule = site_rule(site)
        if rule.anchor_text and rule.latest_after_anchor:
            return extract_anchor_kill_sum_period_values(site, documents)
        return extract_kill_sum_period_values(
            documents,
            pick=site.pick,
            allow_weak=rule.allow_weak_kill_sum_keyword,
            value_count=site.value_count,
        )
    if site.site_id == KAIJIANGFACAI_SITE_ID:
        return extract_kaijiangfacai_kill_sum_period_values(documents)
    if site.site_id == BLACKPEPPER_SITE_ID:
        return extract_blackpepper_period_values(site, documents)
    if site.site_id in TTSS_SITE_IDS:
        return extract_ttss_kill_sum_period_values(documents, site.name)
    if site.site_id not in REGISTRY._parsers:
        rule = site_rule(site)
        return extract_kill_sum_period_values(
            documents,
            pick=site.pick,
            allow_weak=rule.allow_weak_kill_sum_keyword,
            value_count=site.value_count,
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
            if site.site_id == GUCHENG_SITE_ID:
                history_candidate = find_gucheng_history_candidate(
                    site, documents, current_period
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
            if site.site_id in ARTICLE_CONTENT_SITE_IDS:
                history_candidate = find_article_content_history_candidate(
                    site, documents, current_period
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
            if site.value_count == 2:
                detail = f"{current_period}期绝杀二合严格历史指纹 {value}"
            if site.site_id == KAIJIANGFACAI_SITE_ID:
                detail = (
                    f"{current_period}期开奖发财 综合杀料 杀合[{value}]"
                )
            if site.site_id == BLACKPEPPER_SITE_ID:
                detail = f"{current_period}期:绝杀合数 [杀{value}] 开"
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
