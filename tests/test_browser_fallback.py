import requests
from unittest.mock import patch

from he_app.domain.models import Site
from he_app.services import crawler


class FakeBrowser:
    def get_documents(self, *args, **kwargs):
        return ["rendered"]


def test_dynamic_site_uses_browser_after_http_timeout():
    site = Site("命中劫", "https://example.invalid/", "top", True, False, "s108_topic_1024380")
    expected = ("08合", "ok", ["08合"], None)
    with patch.object(crawler, "collect_special_documents", side_effect=requests.exceptions.ReadTimeout()), \
         patch.object(crawler, "_render_dynamic_index_detail", return_value=["rendered"]) as render, \
         patch.object(crawler, "evaluate_site_period", return_value=expected):
        assert crawler._scrape_exact_site(requests.Session(), site, 265, 10, FakeBrowser()) == expected
    render.assert_called_once()


def test_browser_site_uses_direct_page_after_connection_failure():
    site = Site("怒气冲冲", "https://example.invalid/topic/1.html", "bottom", True, False, "s106_topic_281519")
    expected = ("04合", "ok", ["04合"], None)
    with patch.object(crawler, "collect_special_documents", side_effect=requests.exceptions.ConnectionError()), \
         patch.object(crawler, "try_http_current", return_value=None), \
         patch.object(crawler, "evaluate_site_period", return_value=expected):
        assert crawler._scrape_exact_site(requests.Session(), site, 265, 10, FakeBrowser()) == expected


def test_forum_api_site_uses_browser_after_tls_failure():
    site = Site("学无止境", "https://example.invalid/#/users/116149", "top", True, True, "s020_fklgrq")
    expected = ("05合", "ok", ["05合"], None)
    with patch.object(crawler, "collect_special_documents", side_effect=requests.exceptions.SSLError()), \
         patch.object(crawler, "try_http_current", return_value=None), \
         patch.object(crawler, "evaluate_site_period", return_value=expected):
        assert crawler._scrape_exact_site(requests.Session(), site, 265, 10, FakeBrowser()) == expected
