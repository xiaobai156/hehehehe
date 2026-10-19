from types import SimpleNamespace

import pytest

from he_app.domain.errors import SiteScrapeFailure
from he_app.fetch.browser import BrowserClient


def browser_at(actual_url):
    browser = BrowserClient()
    browser._page = SimpleNamespace(
        url=actual_url,
        locator=lambda selector: SimpleNamespace(inner_text=lambda: '292期 绝杀一合【04合】'),
        content=lambda: '<body>292期 绝杀一合【04合】</body>',
    )
    return browser


@pytest.mark.parametrize('actual', [
    'https://other.example/#dq',
    'http://forum.example/topic/1.html',
    'https://forum.example:17455/topic/1.html',
    'about:blank',
])
def test_browser_rejects_unrelated_origin_before_parsing(actual):
    browser = browser_at(actual)
    with pytest.raises(SiteScrapeFailure) as error:
        browser._collect_documents_owner('https://forum.example/topic/1.html')
    assert error.value.category == '站点身份错误'
    assert browser.capture_sequence == 0


def test_browser_records_actual_document_and_requested_parent():
    requested = 'https://forum.example/list.html'
    actual = 'https://forum.example/topic/1.html#data'
    documents = browser_at(actual)._collect_documents_owner(requested)
    assert len(documents) == 2
    assert all(document.source_url == actual for document in documents)
    assert all(document.parent_url == requested for document in documents)
    assert all(actual in document.authority_id for document in documents)
    assert documents[0].authority_id != documents[1].authority_id


def test_browser_collection_without_parent_still_records_actual_source():
    actual = 'https://forum.example/topic/1.html'
    documents = browser_at(actual)._collect_documents_owner()
    assert all(document.source_url == actual for document in documents)


def test_browser_rejects_navigation_during_capture():
    url = 'https://forum.example/topic/1.html'
    browser = browser_at(url)
    def navigating_content():
        browser._page.url = 'https://forum.example/topic/2.html'
        return '<body>unrelated document</body>'
    browser._page.content = navigating_content
    with pytest.raises(SiteScrapeFailure, match='同一文档'):
        browser._collect_documents_owner(url)
    assert browser.capture_sequence == 0
