import requests

from he_app.domain.errors import SiteScrapeFailure
from he_app.domain.models import Site
from he_app.domain.periods import CyclePolicy, PeriodContext, PeriodKey
from he_app.services import adaptive_fetch, live_validation_scheduled


def _context():
    return PeriodContext(PeriodKey(2026, 252), CyclePolicy("year", None), 10)


def test_live_validation_partitions_browser_sites_from_http_sites():
    sites = [
        Site("HTTP", "https://example.test/a", "top", False, False, "http"),
        Site("Browser", "https://example.test/b", "top", True, False, "browser"),
    ]
    http_jobs, browser_jobs = live_validation_scheduled._partition_jobs(
        sites, _context(), 20
    )
    assert [key for key, _payload in http_jobs] == [(0, "http")]
    assert [key for key, _payload in browser_jobs] == [(1, "browser")]


def test_peer_socket_unobservable_is_the_only_identity_error_allowed_to_fallback():
    allowed = SiteScrapeFailure(
        "站点身份错误", "无法验证实际连接地址: 203.0.113.10"
    )
    forbidden = SiteScrapeFailure(
        "站点身份错误", "实际连接地址不在请求前DNS结果中"
    )
    assert adaptive_fetch._peer_socket_unobservable(allowed)
    assert not adaptive_fetch._peer_socket_unobservable(forbidden)


def test_collect_http_documents_retries_only_unobservable_peer(monkeypatch):
    site = Site("测试", "https://example.test/a", "top", False, False, "x")
    monkeypatch.setattr(
        adaptive_fetch,
        "collect_documents",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            SiteScrapeFailure("站点身份错误", "无法验证实际连接地址: 203.0.113.10")
        ),
    )
    monkeypatch.setattr(adaptive_fetch, "_curl_tls_fallback", lambda *_args: ["ok"])
    assert adaptive_fetch.collect_http_documents(object(), site, 20) == ["ok"]


def test_collect_http_documents_does_not_bypass_other_identity_failures(monkeypatch):
    site = Site("测试", "https://example.test/a", "top", False, False, "x")
    monkeypatch.setattr(
        adaptive_fetch,
        "collect_documents",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            SiteScrapeFailure("站点身份错误", "实际连接地址不在请求前DNS结果中")
        ),
    )
    try:
        adaptive_fetch.collect_http_documents(object(), site, 20)
    except SiteScrapeFailure as exc:
        assert "不在请求前DNS结果" in exc.reason
    else:
        raise AssertionError("identity mismatch must not fall back")


def test_tls_error_still_uses_certificate_verifying_curl_path(monkeypatch):
    site = Site("测试", "https://example.test/a", "top", False, False, "x")
    monkeypatch.setattr(
        adaptive_fetch,
        "collect_documents",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(requests.exceptions.SSLError("bad cert")),
    )
    monkeypatch.setattr(adaptive_fetch, "_curl_tls_fallback", lambda *_args: ["strict-curl"])
    assert adaptive_fetch.collect_http_documents(object(), site, 20) == ["strict-curl"]
