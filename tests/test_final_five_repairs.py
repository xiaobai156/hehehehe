import pytest

from he_app.domain.errors import SiteScrapeFailure
from he_app.domain.models import Site
from he_app.fetch.http import FetchedText
from he_app.fetch.url_policy import ResolvedOrigin
from he_app.services import adaptive_fetch, crawler
from he_app.services.browser_policy import (
    BROWSER_RENDER_REQUIRED_SITE_IDS,
    runtime_click_first,
    runtime_requires_browser,
)
from he_app.services.isolation import _runtime_needs_browser


def _site(site_id: str, *, browser: bool = False, click_first: bool = False) -> Site:
    return Site(
        name={
            "s051_topic_702074": "坐收其利",
            "s065_topic_437721": "鸡飞蛋打",
            "s108_topic_1024380": "命中劫",
            "s124_22_868393c_art_zhuanqu_8129": "月落乌江",
            "s129_topic_732154": "大家发",
        }.get(site_id, "测试站"),
        url="https://example.test/topic/1.html",
        pick="top",
        browser=browser,
        click_first=click_first,
        site_id=site_id,
    )


@pytest.mark.parametrize(
    "site_id",
    ["s051_topic_702074", "s065_topic_437721", "s108_topic_1024380"],
)
def test_verified_shell_sites_are_browser_runtime_only(site_id: str) -> None:
    site = _site(site_id, browser=False)
    assert site_id in BROWSER_RENDER_REQUIRED_SITE_IDS
    assert runtime_requires_browser(site, lambda _site: False)
    assert _runtime_needs_browser(site, lambda _site: False)


def test_normal_nonbrowser_site_is_not_promoted() -> None:
    site = _site("normal", browser=False)
    assert not runtime_requires_browser(site, lambda _site: False)
    assert not _runtime_needs_browser(site, lambda _site: False)


def test_mingzhongjie_forces_strict_click_only() -> None:
    assert runtime_click_first(_site("s108_topic_1024380", click_first=False))
    assert not runtime_click_first(_site("s051_topic_702074", click_first=False))
    assert runtime_click_first(_site("normal", click_first=True))


class _FakeBrowser:
    def __init__(self, documents=None):
        self.documents = list(documents or ["rendered"])
        self.calls = []
        self.closed = 0
        self.started = 0

    def get_documents(self, url, period, click_first, timeout):
        self.calls.append((url, period, click_first, timeout))
        return self.documents

    def close(self):
        self.closed += 1

    def start(self):
        self.started += 1


def test_shell_topic_keeps_http_first_then_browser_strict(monkeypatch) -> None:
    site = _site("s051_topic_702074", browser=False)
    browser = _FakeBrowser()
    monkeypatch.setattr(crawler, "collect_special_documents", lambda *_args: None)
    monkeypatch.setattr(crawler, "try_http_current", lambda *_args: None)
    monkeypatch.setattr(
        crawler,
        "evaluate_site_period",
        lambda got_site, period, documents: (
            "04合 坐收其利",
            "252期:【绝杀一合】【04合】开:000准",
            ["04合"],
            None,
        ),
    )

    result = crawler._scrape_exact_site(object(), site, 252, 20, browser)

    assert result[0] == "04合 坐收其利"
    assert browser.calls == [(site.url, 252, False, 20)]


def test_dynamic_shell_http_failure_falls_back_to_unique_strict_browser_link(monkeypatch) -> None:
    site = _site("s108_topic_1024380", browser=False)
    browser = _FakeBrowser()

    def fail_special(*_args):
        raise SiteScrapeFailure("主页找帖失败", "HTTP主页为空壳")

    monkeypatch.setattr(crawler, "collect_special_documents", fail_special)
    monkeypatch.setattr(
        crawler,
        "evaluate_site_period",
        lambda got_site, period, documents: (
            "03合 命中劫",
            "252期:绝杀一合[03合]开",
            ["03合"],
            None,
        ),
    )

    result = crawler._scrape_exact_site(object(), site, 252, 20, browser)

    assert result[0] == "03合 命中劫"
    assert browser.calls == [(site.url, 252, True, 20)]


class TimeoutException(Exception):
    pass


def test_renderer_ipc_timeout_restarts_browser_once(monkeypatch) -> None:
    site = _site("s129_topic_732154", browser=True)
    browser = _FakeBrowser()
    calls = {"count": 0}

    def scrape(*_args):
        calls["count"] += 1
        if calls["count"] == 1:
            raise TimeoutException("timeout: Timed out receiving message from renderer: -0.001")
        return "08合 大家发", "252期:绝杀一合[杀08合]开", ["08合"], None

    monkeypatch.setattr(crawler, "scrape_site_with_browser", scrape)

    result = crawler.scrape_browser_site(object(), site, 252, 20, browser)

    assert result[0] == "08合 大家发"
    assert result[2] is None
    assert calls["count"] == 2
    assert browser.closed == 1
    assert browser.started == 1


def test_other_browser_error_is_not_retried(monkeypatch) -> None:
    site = _site("s129_topic_732154", browser=True)
    browser = _FakeBrowser()
    calls = {"count": 0}

    def scrape(*_args):
        calls["count"] += 1
        raise RuntimeError("parse or identity error")

    monkeypatch.setattr(crawler, "scrape_site_with_browser", scrape)

    result = crawler.scrape_browser_site(object(), site, 252, 20, browser)

    assert result[0] is None
    assert "parse or identity error" in result[2]
    assert calls["count"] == 1
    assert browser.closed == 0
    assert browser.started == 0


def test_non_renderer_timeout_is_not_retried(monkeypatch) -> None:
    site = _site("s129_topic_732154", browser=True)
    browser = _FakeBrowser()
    calls = {"count": 0}

    def scrape(*_args):
        calls["count"] += 1
        raise TimeoutException("page load timed out without renderer IPC message")

    monkeypatch.setattr(crawler, "scrape_site_with_browser", scrape)

    result = crawler.scrape_browser_site(object(), site, 252, 20, browser)

    assert result[0] is None
    assert calls["count"] == 1
    assert browser.closed == 0
    assert browser.started == 0


class _TwoAddressPolicy:
    def resolve(self, _url):
        return ResolvedOrigin(
            "https",
            "example.test",
            443,
            ("1.1.1.1", "8.8.8.8"),
        )


def test_tls_fallback_tries_next_prevalidated_address_only_after_transport_error(monkeypatch) -> None:
    site = _site("s124_22_868393c_art_zhuanqu_8129")
    calls = []
    monkeypatch.setattr(
        adaptive_fetch,
        "StrictNetworkPolicy",
        lambda require_peer=False: _TwoAddressPolicy(),
    )

    def fetch(url, timeout, resolved):
        calls.append(resolved.addresses[0])
        if len(calls) == 1:
            raise RuntimeError("curl exit 35: certificate expired on this backend")
        return FetchedText("<html>ok</html>", final_url=url, status_code=200)

    monkeypatch.setattr(adaptive_fetch, "fetch_text_with_curl", fetch)

    documents = adaptive_fetch._curl_tls_fallback(site, 20)

    assert calls == ["1.1.1.1", "8.8.8.8"]
    assert len(documents) == 1
    assert documents[0].resolved_addresses == ("8.8.8.8",)


def test_tls_fallback_does_not_hide_real_http_error_by_switching_backend(monkeypatch) -> None:
    site = _site("s124_22_868393c_art_zhuanqu_8129")
    calls = []
    monkeypatch.setattr(
        adaptive_fetch,
        "StrictNetworkPolicy",
        lambda require_peer=False: _TwoAddressPolicy(),
    )

    def fetch(_url, _timeout, resolved):
        calls.append(resolved.addresses[0])
        raise RuntimeError("HTTP Error 404: curl未取得有效2xx响应")

    monkeypatch.setattr(adaptive_fetch, "fetch_text_with_curl", fetch)

    with pytest.raises(RuntimeError, match="HTTP Error 404"):
        adaptive_fetch._curl_tls_fallback(site, 20)

    assert calls == ["1.1.1.1"]
