from __future__ import annotations

import requests
import pytest

from he_app.domain.errors import DedicatedCandidateConflict, SiteScrapeFailure
from he_app.domain.models import Site
from he_app.parsers.dedicated.ttss import (
    TTSS_CHENYUAN_SITE_ID,
    TTSS_QIFENG_SITE_ID,
    TTSS_YIKAO_SITE_ID,
    extract_ttss_kill_sum_period_values,
    find_ttss_article_link,
    find_ttss_kill_sum_candidate,
    find_ttss_next_page_url,
)
from he_app.services import document_sources
from he_app.services.fingerprint import build_site_fingerprint
from he_app.services.single_period import parse_site_period


LIST_URL = "https://a.ttss.vip/list.aspx?id=79&page=1"


def ttss_site(name: str, site_id: str = TTSS_YIKAO_SITE_ID) -> Site:
    return Site(name, LIST_URL, "top", False, False, site_id)


def list_html(*links: tuple[str, str], next_href: str | None = None, page: str = "1/9") -> str:
    link_html = "".join(f'<li><a href="{href}">{text}</a></li>' for href, text in links)
    next_html = f'<a href="{next_href}">下一页</a>' if next_href is not None else ""
    return f"<div class='list'><ul>{link_html}</ul><div class='ui-page'>{next_html}<a>{page}</a></div></div>"


def article_html(name: str, rows: str, article_period: int = 218) -> str:
    return f"""
    <html><body><div class='detail'>
      <div class='big-tit'>{article_period}期: {name}【绝杀一合】已免费公开</div>
      <div class='qingchu'>提高速度,减少浏览流量，不保留大量往期记录!</div>
      {rows}
      <div class='next-prev'><a>下一篇</a></div>
    </div></body></html>
    """


def test_list_link_requires_exact_period_name_and_keyword() -> None:
    html = list_html(
        ("article.aspx?id=1", "217期: 倚靠谁光【绝杀一合】已免费公开"),
        ("article.aspx?id=2", "218期: 倚靠谁光【绝杀三尾】已免费公开"),
        ("article.aspx?id=3", "218期: 倚靠谁光【绝杀一合】已免费公开"),
    )

    assert find_ttss_article_link(html, LIST_URL, "倚靠谁光", 218) == (
        "https://a.ttss.vip/article.aspx?id=3"
    )


def test_period_is_a_runtime_parameter_not_hardcoded_to_218() -> None:
    html = list_html(("article.aspx?id=4", "217期: 倚靠谁光【绝杀一合】已免费公开"))
    document = article_html(
        "倚靠谁光",
        "<p>217期 倚靠谁光 : 【10合】 开 26 准</p>",
        article_period=217,
    )

    assert find_ttss_article_link(html, LIST_URL, "倚靠谁光", 217) == (
        "https://a.ttss.vip/article.aspx?id=4"
    )
    candidate = find_ttss_kill_sum_candidate([document], 217, "top")
    assert candidate is not None and candidate.values == "10合"


def test_list_pagination_uses_visible_next_link_and_stops_at_last_page() -> None:
    page_two = "<div class='ui-page'><a href='#'>下一页</a><a>9/9</a></div>"

    assert find_ttss_next_page_url(list_html(next_href="list.aspx?id=79&page=2"), LIST_URL) == (
        "https://a.ttss.vip/list.aspx?id=79&page=2"
    )
    assert find_ttss_next_page_url(page_two, "https://a.ttss.vip/list.aspx?id=79&page=9") is None


def test_list_discovery_follows_pages_and_returns_only_same_article(monkeypatch: pytest.MonkeyPatch) -> None:
    page_one = list_html(
        ("article.aspx?id=11", "218期: 其他站【绝杀一合】已免费公开"),
        next_href="list.aspx?id=79&page=2",
        page="1/9",
    )
    page_two_url = "https://a.ttss.vip/list.aspx?id=79&page=2"
    page_two = list_html(
        ("article.aspx?id=22", "218期: 倚靠谁光【绝杀一合】已免费公开"),
        next_href="list.aspx?id=79&page=3",
        page="2/9",
    )
    article_url = "https://a.ttss.vip/article.aspx?id=22"
    payloads = {
        LIST_URL: page_one,
        page_two_url: page_two,
        article_url: article_html(
            "倚靠谁光",
            "<p>218期 倚靠谁光 : 【09合】 开 ?? 准</p><p>217期 倚靠谁光 : 【10合】 开 26 准</p>",
        ),
    }

    monkeypatch.setattr(document_sources, "fetch_text", lambda _session, url, _timeout: payloads[url])
    documents = document_sources.collect_ttss_list_article_documents(
        requests.Session(), ttss_site("倚靠谁光"), 5, 218
    )

    assert len(documents) == 1
    assert documents[0].source_url == article_url
    assert documents[0].record_id == "22"
    assert page_two_url in documents[0].parent_url


def test_list_discovery_fails_when_target_is_not_found_before_last_page(monkeypatch: pytest.MonkeyPatch) -> None:
    payloads = {
        LIST_URL: list_html(next_href="list.aspx?id=79&page=2", page="1/2"),
        "https://a.ttss.vip/list.aspx?id=79&page=2": list_html(page="2/2"),
    }
    monkeypatch.setattr(document_sources, "fetch_text", lambda _session, url, _timeout: payloads[url])

    with pytest.raises(SiteScrapeFailure, match="未找到"):
        document_sources.collect_ttss_list_article_documents(
            requests.Session(), ttss_site("尘缘难尽", TTSS_CHENYUAN_SITE_ID), 5, 218
        )


def test_ttss_top_candidate_and_bulk_history_are_from_detail_block() -> None:
    document = article_html(
        "倚靠谁光",
        """
        <p>说明文字不占方向窗口</p>
        <p>218期 倚靠谁光 : 【09合】 开 ?? 准</p>
        <p>217期 倚靠谁光 : 【10合】 开 26 准</p>
        <p>216期 倚靠谁光 : 【08合】 开 37 准</p>
        """,
    )
    site = ttss_site("倚靠谁光")

    candidate = find_ttss_kill_sum_candidate([document], 218, "top")
    assert candidate is not None
    assert candidate.values == "09合"
    assert extract_ttss_kill_sum_period_values([document], "倚靠谁光") == {
        218: "09合",
        217: "10合",
        216: "08合",
    }

    parsed = parse_site_period(site, 218, [document])
    assert parsed.success is True
    assert parsed.value == "09合 倚靠谁光"
    assert parsed.evidence[0].keyword == "列表文章绝杀一合"

    fingerprint = build_site_fingerprint(site, [document], 218, 3)
    assert fingerprint == {218: "09合", 217: "10合", 216: "08合"}


def test_ttss_top_rejects_target_period_outside_first_valid_row() -> None:
    document = article_html(
        "尘缘难尽",
        "<p>217期 尘缘难尽 : 【12合】 开 26 准</p><p>218期 尘缘难尽 : 【08合】 开 ?? 准</p>",
    )

    assert find_ttss_kill_sum_candidate(
        [document], 218, "top"
    ) is None


def test_ttss_rejects_wrong_article_name_and_multiple_values() -> None:
    wrong_name = article_html(
        "棋逢对手",
        "<p>218期 倚靠谁光 : 【06合】 开 ?? 准</p>",
    )
    two_values = article_html(
        "棋逢对手",
        "<p>218期 棋逢对手 : 【06合】【07合】 开 ?? 准</p>",
    )

    assert find_ttss_kill_sum_candidate([wrong_name], 218, "top") is None
    assert find_ttss_kill_sum_candidate([two_values], 218, "top") is None


def test_ttss_same_period_conflict_is_not_silently_resolved() -> None:
    document = article_html(
        "棋逢对手",
        "<p>218期 棋逢对手 : 【06合】 开 ?? 准</p><p>218期 棋逢对手 : 【07合】 开 ?? 准</p>",
    )

    with pytest.raises(DedicatedCandidateConflict):
        find_ttss_kill_sum_candidate([document], 218, "top")


def test_site_ids_are_distinct_for_three_names() -> None:
    assert len({TTSS_YIKAO_SITE_ID, TTSS_CHENYUAN_SITE_ID, TTSS_QIFENG_SITE_ID}) == 3
