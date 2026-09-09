import pytest

from he_app.domain.errors import SiteScrapeFailure
from he_app.domain.models import Site
from he_app.services import crawler


class _DynamicBrowser:
    def __init__(self, index_html: str):
        self.index_html = index_html
        self.calls = []

    def get_documents(self, url, period, click_first, timeout):
        self.calls.append((url, period, click_first, timeout))
        if len(self.calls) == 1:
            return [self.index_html]
        return [f"{period}期:绝杀一合 [03合] 开0000准"]


def _site(name="和风细雨", site_id="s118_topic_1024655"):
    return Site(
        name=name,
        url="https://example.test/topic/257907.html",
        pick="top",
        browser=False,
        click_first=False,
        site_id=site_id,
    )


def test_browser_dynamic_column_selects_exact_name_issue_keyword_and_same_origin() -> None:
    browser = _DynamicBrowser(
        """
        <html><body>
          <a href="/topic/1090741.html">252期 和风细雨 绝杀一合</a>
          <a href="/topic/1090742.html">252期 新人旧梦 绝杀一合</a>
          <a href="/topic/999.html">251期 和风细雨 绝杀一合</a>
        </body></html>
        """
    )

    documents = crawler._render_dynamic_index_detail(browser, _site(), 252, 30)

    assert documents == ["252期:绝杀一合 [03合] 开0000准"]
    assert browser.calls == [
        ("https://example.test/topic/257907.html", 252, False, 30),
        ("https://example.test/topic/1090741.html", 252, False, 30),
    ]


def test_browser_dynamic_column_fails_closed_on_two_matching_articles() -> None:
    browser = _DynamicBrowser(
        """
        <a href="/topic/a.html">252期 和风细雨 绝杀一合</a>
        <a href="/topic/b.html">252期 和风细雨 绝杀一合</a>
        """
    )

    with pytest.raises(SiteScrapeFailure, match="多个不同文章"):
        crawler._render_dynamic_index_detail(browser, _site(), 252, 30)

    assert len(browser.calls) == 1


def test_http_budget_timeout_uses_same_strict_browser_column_selector(monkeypatch) -> None:
    browser = _DynamicBrowser(
        '<a href="/topic/1090741.html">252期 和风细雨 绝杀一合</a>'
    )

    def timeout_collector(*_args, **_kwargs):
        raise TimeoutError("HTTP请求预算已耗尽")

    def strict_evaluator(site, period, documents):
        assert site.site_id == "s118_topic_1024655"
        assert period == 252
        assert documents == ["252期:绝杀一合 [03合] 开0000准"]
        return "03合 和风细雨", documents[0], ["03合"], None

    monkeypatch.setattr(crawler, "collect_special_documents", timeout_collector)
    monkeypatch.setattr(crawler, "evaluate_site_period", strict_evaluator)

    result = crawler._scrape_exact_site(object(), _site(), 252, 30, browser)

    assert result[0] == "03合 和风细雨"
    assert browser.calls[-1][0] == "https://example.test/topic/1090741.html"
