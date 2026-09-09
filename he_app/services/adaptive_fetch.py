from __future__ import annotations

import json
import re
import time
from dataclasses import replace
from urllib.parse import urlparse

import requests

from he_app.domain.errors import SiteScrapeFailure
from he_app.domain.models import Site, SiteRule, SourceDocument
from he_app.domain.policies import normalize_text
from he_app.fetch.discovery import collect_documents
from he_app.fetch.http import fetch_text, fetch_text_with_curl
from he_app.fetch.url_policy import ResolvedOrigin, StrictNetworkPolicy
from he_app.parsers.common import has_kill_sum_keyword
from he_app.parsers.dedicated.kaijiangfacai import KAIJIANGFACAI_SITE_ID
from he_app.parsers.dedicated.tables import site_rule
from he_app.parsers.policies import SITE_RULES
from he_app.services.document_sources import (
    collect_special_site_documents,
    forum_api_documents_from_json,
    forum_api_url,
)
from he_app.services.single_period import evaluate_site_period


# These sites were proven by real 252 runs to contain an exact strict
# current-period/value row; only the generic page-level locator word was absent.
# Keep the period, value-count and physical top/bottom edge checks unchanged.
_RUNTIME_NO_LOCATOR_SITE_IDS = {
    "s003_topic_193293",
    "s014_topic_242281",
    "s018_mm",
    "s048_979363",
    "s067_topic_291095",
    "s074_topic_250885",
    "s075_topic_250874",
    "s086_aa_959787m_136",
}
for _site_id in _RUNTIME_NO_LOCATOR_SITE_IDS:
    _rule = SITE_RULES.get(_site_id, SiteRule())
    SITE_RULES[_site_id] = replace(
        _rule,
        require_body_locator=False,
        note=(f"{_rule.note}; " if _rule.note else "")
        + "252 live repair: strict row has no stable body locator",
    )


# The configured s131 IP endpoint was live-verified to redirect to this exact
# canonical page. The canonical page itself passed the dedicated title/table,
# target-period, direction and evidence checks. This is intentionally one
# exact URL, not a domain suffix or global redirect exception.
KAIJIANGFACAI_CONFIG_URL = "https://156.225.88.144:12098/#234432"
KAIJIANGFACAI_CANONICAL_URL = "https://84477.kjfc88b.app:2443/welcome.html"


def _verified_http_site(site: Site) -> Site:
    if site.site_id != KAIJIANGFACAI_SITE_ID:
        return site
    if site.url != KAIJIANGFACAI_CONFIG_URL:
        raise SiteScrapeFailure(
            "站点身份错误",
            f"{site.name} 配置入口已变化，拒绝沿用旧规范来源: {site.url}",
        )
    return Site(
        name=site.name,
        url=KAIJIANGFACAI_CANONICAL_URL,
        pick=site.pick,
        browser=False,
        click_first=False,
        site_id=site.site_id,
        value_count=site.value_count,
    )


def _curl_tls_fallback(site: Site, timeout: int) -> list[str]:
    """Retry curl across verified DNS addresses without weakening TLS.

    Some legacy hosts expose different certificates on different public DNS
    backends.  Each address is tried through curl ``--resolve`` so the original
    hostname/SNI and normal certificate validation remain intact.  Only curl
    transport/TLS failures may advance to another prevalidated address; a real
    HTTP response, parser error or content error is never hidden by node
    hopping.
    """

    policy = StrictNetworkPolicy(require_peer=False)
    resolved = policy.resolve(site.url)
    deadline = time.monotonic() + float(timeout)
    last_transport_error: RuntimeError | None = None
    successful_resolved: ResolvedOrigin | None = None
    text = None

    for address in resolved.addresses:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("curl多地址请求预算已耗尽")
        pinned = ResolvedOrigin(
            resolved.scheme,
            resolved.host,
            resolved.port,
            (address,),
        )
        try:
            text = fetch_text_with_curl(site.url, remaining, pinned)
        except RuntimeError as exc:
            if not str(exc).startswith("curl exit "):
                raise
            last_transport_error = exc
            continue
        successful_resolved = pinned
        break

    if text is None or successful_resolved is None:
        if last_transport_error is not None:
            raise last_transport_error
        raise RuntimeError("curl未取得可验证的公网地址响应")

    final_url = str(getattr(text, "final_url", site.url) or site.url)
    authority = f"http-curl:{final_url}:page"
    return [
        SourceDocument(
            str(text),
            source_url=final_url,
            fetch_kind="http",
            document_type="html",
            parent_url=site.url,
            authority_id=authority,
            document_id=f"{authority}:html",
            resolved_addresses=tuple(successful_resolved.addresses),
        )
    ]


def _peer_socket_unobservable(exc: SiteScrapeFailure) -> bool:
    """True only when requests cannot expose the socket peer for verification.

    Curl is then safe as a compatibility transport because ``--resolve`` pins
    the connection to the already-validated DNS address while certificate
    verification remains enabled. Other identity failures must still fail
    closed.
    """

    return (
        exc.category == "站点身份错误"
        and "无法验证实际连接地址" in exc.reason
    )


def collect_http_documents(
    session: requests.Session,
    site: Site,
    timeout: int,
) -> list[str]:
    """Fetch one verified HTTP source without enabling unapproved derivatives."""

    request_site = _verified_http_site(site)
    rule = site_rule(site)
    try:
        return collect_documents(
            session,
            request_site.url,
            timeout,
            allow_inline_decode="http-decoded" in rule.allowed_fetch_kinds,
        )
    except requests.exceptions.SSLError:
        # Transport compatibility only: curl still verifies TLS and is pinned
        # to the already-validated public address. No -k or unverified host.
        return _curl_tls_fallback(request_site, timeout)
    except SiteScrapeFailure as exc:
        if _peer_socket_unobservable(exc):
            return _curl_tls_fallback(request_site, timeout)
        raise


def collect_forum_documents_by_url_identity(
    session: requests.Session,
    site: Site,
    timeout: int,
    period: int,
) -> list[str]:
    """Select one forum record by URL user/forum identity and exact issue.

    A configured ``#/users/<id>`` URL is stronger identity evidence than a
    mutable display nickname. When the API exposes an owner id that matches
    the URL, a changed nickname must not reject the correct record. If the API
    omits owner ids entirely, the configured display name remains the required
    fallback identity check.
    """

    api_url = forum_api_url(site.url)
    if api_url is None:
        raise SiteScrapeFailure("记录ID缺失", "forum api url not found")
    try:
        payload = json.loads(fetch_text(session, api_url, timeout))
    except json.JSONDecodeError as exc:
        raise SiteScrapeFailure("接口响应无效", "论坛API不是有效JSON") from exc
    records = payload if isinstance(payload, list) else [payload]
    if len(records) > 200:
        raise SiteScrapeFailure("接口响应无效", "论坛API记录数量超过200条上限")

    fragment = urlparse(site.url).fragment
    user_match = re.search(r"(?:^|/)users/(\d+)(?:$|[/?#])", fragment)
    forum_match = re.search(r"(?:^|/)forums/(\d+)(?:$|[/?#])", fragment)
    matches: list[SourceDocument] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        topic = str(record.get("topic", ""))
        if not has_kill_sum_keyword(topic):
            continue
        draw = str(record.get("draw", ""))
        if not draw.isdigit() or int(draw) != period:
            continue
        raw_id = record.get("id", record.get("_id"))
        if type(raw_id) not in {str, int} or not str(raw_id).strip():
            raise SiteScrapeFailure("记录ID缺失", "论坛目标对象没有可验证的帖子ID")
        record_id = str(raw_id).strip()
        if forum_match and record_id != forum_match.group(1):
            continue

        user = record.get("user") if isinstance(record.get("user"), dict) else {}
        owner_ids = {
            str(value)
            for value in (record.get("user_id"), record.get("userId"), user.get("id"))
            if value is not None and str(value).strip()
        }
        author = normalize_text(str(record.get("authorNickname") or user.get("nickname") or ""))
        if user_match:
            expected_user = user_match.group(1)
            if owner_ids:
                if expected_user not in owner_ids:
                    continue
            elif author != site.name:
                continue
        elif author and author != site.name:
            continue

        content = record.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        text = forum_api_documents_from_json(json.dumps(record, ensure_ascii=False))[0]
        authority = f"api:{api_url}:record:{record_id}"
        matches.append(
            SourceDocument(
                text,
                source_url=api_url,
                fetch_kind="api",
                document_type="json-record",
                parent_url=site.url,
                record_id=record_id,
                authority_id=authority,
                document_id=authority,
            )
        )

    if len(matches) != 1:
        category = "候选冲突" if len(matches) > 1 else "无当期"
        raise SiteScrapeFailure(
            category,
            f"论坛{period}期URL身份目标记录数量={len(matches)}，必须唯一",
        )
    return matches


def collect_special_documents(
    session: requests.Session,
    site: Site,
    timeout: int,
    period: int,
) -> list[str] | None:
    if forum_api_url(site.url) is not None:
        return collect_forum_documents_by_url_identity(session, site, timeout, period)
    try:
        return collect_special_site_documents(session, site, timeout, period)
    except requests.exceptions.SSLError:
        # Only generic same-record detail pages can safely use the strict curl
        # transport fallback here. Complex list/API selectors retain their
        # dedicated collector and fail closed.
        if site.site_id == "s093_a_909922_article_aspx_id_3694545":
            raise
        return _curl_tls_fallback(_verified_http_site(site), timeout)


def try_http_current(
    session: requests.Session,
    site: Site,
    period: int,
    timeout: int,
) -> tuple[list[str], tuple[str | None, str, list[str], str | None]] | None:
    """Use HTTP only when the exact configured period/direction validates."""

    documents = collect_http_documents(session, site, timeout)
    evaluation = evaluate_site_period(site, period, documents)
    if evaluation[0] is None:
        return None
    return documents, evaluation


__all__ = [
    "KAIJIANGFACAI_CANONICAL_URL",
    "collect_forum_documents_by_url_identity",
    "collect_http_documents",
    "collect_special_documents",
    "try_http_current",
]
