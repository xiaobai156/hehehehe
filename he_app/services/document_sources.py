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
from he_app.fetch.url_policy import same_origin_url
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
    eligible: dict[str, int] = {}
    for document in documents:
        for link in BeautifulSoup(document, "html.parser").find_all("a", href=True):
            text = normalize_digit_text(normalize_text(link.get_text(" ", strip=True)))
            compact = re.sub(r"\s+", "", text)
            if re.sub(r"\s+", "", site.name) not in compact or "绝杀一合" not in compact:
                continue
            title_periods = [int(value) for value in re.findall(r"(?<!\d)(\d{1,4})期", compact)]
            allowed = {period, period + 1} if period is not None and site.site_id in CURRENT_OR_NEXT_TOPIC_SITE_IDS else {period}
            matched = [value for value in title_periods if value in allowed]
            if period is not None and not matched:
                continue
            target = same_origin_url(dynamic_topic_index_url(site), str(link["href"]))
            score = min(abs(value - period) for value in matched) if matched else 0
            eligible[target] = min(eligible.get(target, score), score)
    if not eligible:
        return None
    best_score = min(eligible.values())
    targets = [url for url, score in eligible.items() if score == best_score]
    if len(targets) != 1:
        raise SiteScrapeFailure("候选冲突", f"{site.name} 标题匹配多个不同文章: {' / '.join(targets)}")
    return targets[0]


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


def collect_liangjian_documents(session: requests.Session, url: str, timeout: int,
                                period: int | None = None) -> list[str]:
    if "list.aspx" not in url.lower():
        return collect_documents(session, url, timeout)
    links: set[str] = set()
    for document in collect_documents(session, url, timeout):
        for link in BeautifulSoup(document, "html.parser").find_all("a", href=True):
            text = normalize_digit_text(normalize_text(link.get_text(" ", strip=True)))
            if "绝杀一合" not in text:
                continue
            title_periods = [int(value) for value in re.findall(r"(?<!\d)(\d{1,4})\s*期", text)]
            if period is not None and title_periods and period not in title_periods:
                continue
            links.add(same_origin_url(url, str(link["href"])))
    if len(links) != 1:
        category = "候选冲突" if links else "主页找帖失败"
        raise SiteScrapeFailure(category, f"亮劍 列表目标文章不唯一，数量={len(links)}")
    # Some verified lists have no issue in the title; uniqueness plus the
    # dedicated detail parser still must prove the requested issue at its edge.
    return collect_documents(session, links.pop(), timeout)


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
            article_url = same_origin_url(page_url, article_links[0])
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
    documents, page_html = collect_page_documents(session, url, timeout, allow_inline_decode=True)
    url = str(getattr(page_html, "final_url", url))
    for script_url in SCRIPT_RE.findall(page_html):
        full_url = same_origin_url(url, script_url)
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
                    authority_id=raw_authority,
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


def collect_forum_api_documents(session: requests.Session, url: str, timeout: int,
                                *, site: Site | None = None,
                                period: int | None = None) -> list[str]:
    if site is None or period is None:
        raise SiteScrapeFailure("记录字段不符", "论坛API采集必须提供站点身份和指定期数")
    api_url = forum_api_url(url)
    if api_url is None:
        raise SiteScrapeFailure("记录ID缺失", "forum api url not found")
    try:
        payload = json.loads(fetch_text(session, api_url, timeout))
    except json.JSONDecodeError as exc:
        raise SiteScrapeFailure("接口响应无效", "论坛API不是有效JSON") from exc
    records = payload if isinstance(payload, list) else [payload]
    if len(records) > 200:
        raise SiteScrapeFailure("接口响应无效", "论坛API记录数量超过200条上限")
    fragment = urlparse(url).fragment
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
            raise SiteScrapeFailure("记录ID不一致", "论坛API帖子ID与URL不一致")
        user = record.get("user") if isinstance(record.get("user"), dict) else {}
        owner_ids = {str(value) for value in
                     [record.get("user_id"), record.get("userId"), user.get("id")]
                     if value is not None}
        author = normalize_text(str(record.get("authorNickname") or user.get("nickname") or ""))
        if user_match:
            if owner_ids and owner_ids != {user_match.group(1)}:
                raise SiteScrapeFailure("记录字段不符", "论坛目标帖所属用户与URL不一致")
            if not owner_ids and author != site.name:
                raise SiteScrapeFailure("记录字段不符", "论坛目标帖缺少可核验作者身份")
        if author and author != site.name:
            raise SiteScrapeFailure("记录字段不符", f"论坛目标作者不是{site.name}")
        content = record.get("content")
        if not isinstance(content, str) or not content.strip():
            raise SiteScrapeFailure("接口字段无效", "论坛目标记录正文为空")
        text = forum_api_documents_from_json(json.dumps(record, ensure_ascii=False))[0]
        matches.append(SourceDocument(text, source_url=api_url, fetch_kind="api",
            document_type="json-record", parent_url=url, record_id=record_id,
            authority_id=f"api:{api_url}:record:{record_id}",
            document_id=f"api:{api_url}:record:{record_id}"))
    if len(matches) != 1:
        category = "候选冲突" if matches else "无当期"
        raise SiteScrapeFailure(category, f"论坛{period}期同作者目标记录数量={len(matches)}，必须唯一")
    return matches


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
        return collect_liangjian_documents(session, site.url, timeout, period)
    if site.site_id in DYNAMIC_HOME_TOPIC_SITE_IDS:
        return collect_dynamic_home_topic_documents(session, site, timeout, period)
    if site.site_id == "s070_topic_246762" and not site.browser:
        return collect_yidianhong_documents(session, site.url, timeout)
    if forum_api_url(site.url) is not None:
        return collect_forum_api_documents(session, site.url, timeout, site=site, period=period)
    return None


def requires_browser(site: Site) -> bool:
    """Known API/list collectors do not need a driver even in legacy browser configs."""
    if not site.browser:
        return False
    http_sources = TTSS_SITE_IDS | DYNAMIC_HOME_TOPIC_SITE_IDS | {
        YIAIZHIMING_SITE_ID, "s093_a_909922_article_aspx_id_3694545",
    }
    return site.site_id not in http_sources and forum_api_url(site.url) is None
