import json
import re
from urllib.parse import parse_qs, urldefrag, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

from he_app.domain.errors import SiteScrapeFailure
from he_app.domain.models import Site, SourceDocument
from he_app.domain.policies import normalize_digit_text, normalize_text
from he_app.fetch.discovery import SCRIPT_RE, collect_documents, collect_page_documents, decode_strdecode_blocks
from he_app.fetch.http import fetch_text
from he_app.parsers.common import (
    extract_values,
    has_kill_sum_keyword,
    is_strict_period_candidate,
    period_numbers_in_text,
)
from he_app.parsers.dedicated.structured import (
    YIAIZHIMING_SITE_ID,
    manager_record_id_from_url,
    yiaizhimin_manager_documents_from_json,
)
from he_app.parsers.dedicated.ttss import (
    TTSS_SITE_IDS,
    find_ttss_article_links,
    find_ttss_next_page_url,
    ttss_article_title_period,
)


DYNAMIC_HOME_TOPIC_SITE_IDS = {"s108_topic_1024380", "s118_topic_1024655", "s119_topic_1024654"}
COLUMN_DYNAMIC_TOPIC_SITE_IDS = {"s118_topic_1024655", "s119_topic_1024654"}
CURRENT_OR_NEXT_TOPIC_SITE_IDS = {"s108_topic_1024380"}


def dynamic_home_url(url: str) -> str:
    parsed = urlparse(url)
    return urlunparse((parsed.scheme, parsed.netloc, "/", "", "", ""))


def dynamic_topic_index_url(site: Site) -> str:
    return site.url if site.site_id in COLUMN_DYNAMIC_TOPIC_SITE_IDS else dynamic_home_url(site.url)


def find_dynamic_home_topic_url(site: Site, documents: list[str], period: int | None = None) -> str | None:
    eligible_links: list[tuple[int, str]] = []
    for document in documents:
        soup = BeautifulSoup(document, "html.parser")
        for link in soup.find_all("a", href=True):
            text = normalize_digit_text(normalize_text(link.get_text(" ", strip=True)))
            compact = re.sub(r"\s+", "", text)
            if site.name not in compact or "绝杀一合" not in compact:
                continue
            if period is not None and site.site_id in CURRENT_OR_NEXT_TOPIC_SITE_IDS:
                periods = [int(value) for value in re.findall(r"(?<!\d)(\d{1,4})期", compact)]
                matching = [value for value in periods if value in {period, period + 1}]
                if matching:
                    eligible_links.append(
                        (min(abs(value - period) for value in matching), urljoin(dynamic_topic_index_url(site), str(link["href"])))
                    )
                continue
            if period is not None and f"{period}期" not in compact:
                continue
            return urljoin(dynamic_topic_index_url(site), str(link["href"]))
    return min(eligible_links, key=lambda item: item[0])[1] if eligible_links else None


def collect_dynamic_home_topic_documents(
    session: requests.Session, site: Site, timeout: int, period: int | None = None
) -> list[str]:
    index_url = dynamic_topic_index_url(site)
    home_documents = collect_documents(session, index_url, timeout)
    topic_url = find_dynamic_home_topic_url(site, home_documents, period)
    if topic_url is None:
        page_label = "栏目页" if site.site_id in COLUMN_DYNAMIC_TOPIC_SITE_IDS else "主页"
        period_text = f"{period}期" if period is not None else ""
        raise SiteScrapeFailure(
            "主页找帖失败",
            f"{site.name} {page_label}未找到{period_text}标题包含{site.name}且包含绝杀一合的帖子链接",
        )
    return collect_documents(session, topic_url, timeout)


def collect_liangjian_documents(session: requests.Session, url: str, timeout: int) -> list[str]:
    if "list.aspx" not in url.lower():
        return collect_documents(session, url, timeout)
    for document in collect_documents(session, url, timeout):
        soup = BeautifulSoup(document, "html.parser")
        for link in soup.find_all("a", href=True):
            if "绝杀一合" in normalize_digit_text(normalize_text(link.get_text(" ", strip=True))):
                return collect_documents(session, urljoin(url, str(link["href"])), timeout)
    raise SiteScrapeFailure("主页找帖失败", "亮劍 列表未找到标题包含绝杀一合的文章链接")


def collect_ttss_list_article_documents(
    session: requests.Session,
    site: Site,
    timeout: int,
    period: int | None = None,
) -> list[str]:
    """Page through id=79 and fetch only the exact same-name article."""

    page_url = site.url
    visited: set[str] = set()
    for _page_count in range(1, 101):
        page_url = urldefrag(page_url)[0]
        if page_url in visited:
            break
        visited.add(page_url)
        page_html = fetch_text(session, page_url, timeout)
        article_links = find_ttss_article_links(page_html, page_url, site.name, period)
        if len(article_links) > 1:
            raise SiteScrapeFailure(
                "候选冲突",
                f"{site.name} 列表页同一期同栏目命中多个目标文章: {' / '.join(article_links)}",
            )
        if article_links:
            article_url = article_links[0]
            page_host = urlparse(page_url).netloc
            article_parts = urlparse(article_url)
            if article_parts.netloc != page_host:
                raise SiteScrapeFailure(
                    "站点身份错误",
                    f"{site.name} 目标文章跨域: {article_url}",
                )
            record_ids = parse_qs(article_parts.query).get("id", [])
            if len(record_ids) != 1 or not record_ids[0].strip():
                raise SiteScrapeFailure(
                    "记录ID缺失",
                    f"{site.name} 目标文章URL缺少唯一id: {article_url}",
                )
            article_id = record_ids[0].strip()
            article_html = fetch_text(session, article_url, timeout)
            article_period = ttss_article_title_period(article_html, site.name)
            if article_period is None or (period is not None and article_period != period):
                expected = f"{period}期" if period is not None else "目标期"
                raise SiteScrapeFailure(
                    "期数不一致",
                    f"{site.name} 列表目标文章 {article_url} 详情标题未确认{expected}",
                )
            authority = f"ttss:{article_id}:{article_url}"
            return [
                SourceDocument(
                    article_html,
                    source_url=article_url,
                    fetch_kind="http",
                    document_type="html",
                    parent_url=page_url,
                    record_id=article_id,
                    authority_id=authority,
                    document_id=f"{authority}:html",
                )
            ]

        next_url = find_ttss_next_page_url(page_html, page_url)
        if next_url is None:
            break
        page_url = next_url

    period_text = f"{period}期" if period is not None else "目标期"
    raise SiteScrapeFailure(
        "主页找帖失败",
        f"{site.name} 列表分页结束仍未找到{period_text}【绝杀一合】目标文章",
    )


def collect_yidianhong_documents(session: requests.Session, url: str, timeout: int) -> list[str]:
    documents, page_html = collect_page_documents(session, url, timeout)
    for script_url in SCRIPT_RE.findall(page_html):
        full_url = urljoin(url, script_url)
        if "/upload/script/" not in full_url:
            continue
        try:
            script_text = fetch_text(session, full_url, min(timeout, 8))
        except Exception:
            continue
        raw_authority = f"script:{full_url}:raw"
        documents.append(
            SourceDocument(
                script_text,
                source_url=full_url,
                fetch_kind="script",
                document_type="script",
                parent_url=url,
                authority_id=raw_authority,
                document_id=raw_authority,
            )
        )
        decoded = decode_strdecode_blocks(script_text)
        if decoded:
            documents.extend(
                SourceDocument(
                    text,
                    source_url=full_url,
                    fetch_kind="script-decoded",
                    document_type="decoded",
                    parent_url=url,
                    authority_id=f"script:{full_url}:decoded:{index}",
                    document_id=f"script:{full_url}:decoded:{index}",
                )
                for index, text in enumerate(decoded)
            )
    return documents


def forum_api_url(url: str) -> str | None:
    parsed = urlparse(url)
    base_url = f"{parsed.scheme}://{parsed.netloc}"
    user_match = re.search(r"(?:^|/)users/(\d+)(?:$|[/?#])", parsed.fragment)
    if user_match:
        return f"{base_url}/api/v1/users/{user_match.group(1)}/forums/history"
    forum_match = re.search(r"(?:^|/)forums/(\d+)(?:$|[/?#])", parsed.fragment)
    return f"{base_url}/api/v1/forums/{forum_match.group(1)}" if forum_match else None


def compact_history_kill_sum_document(draw: str, topic: str, content: str, max_rows: int = 30) -> str:
    text = BeautifulSoup(content, "html.parser").get_text("\n", strip=True) or content
    lines = [normalize_digit_text(normalize_text(line)) for line in text.splitlines()]
    lines = [line for line in lines if line]
    rows: list[str] = []
    index = 0
    while index < len(lines) and len(rows) < max_rows:
        line = lines[index]
        if not re.search(r"(?<!\d)\d{1,4}\s*期", line) or not has_kill_sum_keyword(line):
            if rows:
                break
            index += 1
            continue
        row = line
        next_index = index + 1
        if next_index < len(lines) and not re.search(r"(?<!\d)\d{1,4}\s*期", lines[next_index]):
            row = normalize_text(f"{row} {lines[next_index]}")
            next_index += 1
        if len(extract_values(row)) == 1 and is_strict_period_candidate(row):
            rows.append(row)
        elif rows:
            break
        index = next_index
    return "\n".join(rows) if rows else f"{draw}期:{topic}\n{content}"


def fklgrq_history_documents_from_json(api_text: str) -> list[str]:
    try:
        payload = json.loads(api_text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"fklgrq api json decode failed: {exc}") from exc
    if not isinstance(payload, list):
        raise RuntimeError("fklgrq api returned non-list payload")
    documents: list[str] = []
    for item in payload[:15]:
        if not isinstance(item, dict):
            continue
        compacted = compact_history_kill_sum_document(
            str(item.get("draw", "")), str(item.get("topic", "")), str(item.get("content", ""))
        )
        documents.append(compacted)
        if len(set(period_numbers_in_text(compacted))) >= 10:
            break
    return documents


def forum_api_documents_from_json(api_text: str) -> list[str]:
    try:
        payload = json.loads(api_text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"forum api json decode failed: {exc}") from exc
    if isinstance(payload, list):
        return fklgrq_history_documents_from_json(api_text)
    if not isinstance(payload, dict):
        raise RuntimeError("forum api returned unsupported payload")
    return [f"{payload.get('draw', '')}期:{payload.get('topic', '')}\n{payload.get('content', '')}"]


def collect_forum_api_documents(session: requests.Session, url: str, timeout: int) -> list[str]:
    api_url = forum_api_url(url)
    if api_url is None:
        raise RuntimeError("forum api url not found")
    return [
        SourceDocument(
            document,
            source_url=api_url,
            fetch_kind="api",
            document_type="json-record",
            parent_url=url,
            authority_id=f"api:{api_url}:record:{index}",
            document_id=f"api:{api_url}:record:{index}",
        )
        for index, document in enumerate(forum_api_documents_from_json(fetch_text(session, api_url, timeout)))
    ]


def collect_manager_documents(session: requests.Session, site: Site, timeout: int) -> list[str]:
    record_id = manager_record_id_from_url(site.url)
    if record_id is None:
        raise SiteScrapeFailure("记录ID缺失", f"{site.name} URL没有有效manager文章ID")
    parsed = urlparse(site.url)
    api_url = f"{parsed.scheme}://{parsed.netloc}/api/proxy/manager-articles/{record_id}"
    return [
        SourceDocument(
            document,
            source_url=api_url,
            fetch_kind="api",
            document_type="manager-record",
            parent_url=site.url,
            record_id=record_id,
            authority_id=f"manager:{record_id}",
            document_id=f"manager:{record_id}:{index}",
        )
        for index, document in enumerate(
            yiaizhimin_manager_documents_from_json(fetch_text(session, api_url, timeout), site)
        )
    ]


def collect_special_site_documents(
    session: requests.Session, site: Site, timeout: int, period: int | None = None
) -> list[str] | None:
    if site.site_id == YIAIZHIMING_SITE_ID:
        return collect_manager_documents(session, site, timeout)
    if site.site_id in TTSS_SITE_IDS:
        return collect_ttss_list_article_documents(session, site, timeout, period)
    if site.site_id == "s093_a_909922_article_aspx_id_3694545":
        return collect_liangjian_documents(session, site.url, timeout)
    if site.site_id in DYNAMIC_HOME_TOPIC_SITE_IDS:
        return collect_dynamic_home_topic_documents(session, site, timeout, period)
    if site.site_id == "s070_topic_246762" and not site.browser:
        return collect_yidianhong_documents(session, site.url, timeout)
    if forum_api_url(site.url) is not None:
        return collect_forum_api_documents(session, site.url, timeout)
    return None
