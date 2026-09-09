"""Optional Selenium check using local HTML only; never loads configured sites."""
import os
from urllib.parse import quote

import pytest

from he_app.fetch.browser import BrowserClient


@pytest.mark.browser
@pytest.mark.skipif(os.environ.get("HE_TEST_BROWSER") != "1", reason="set HE_TEST_BROWSER=1 to start actual Chrome")
def test_real_browser_starts_and_reads_local_html():
    browser = BrowserClient(headless=True)
    try:
        browser.start()
        browser.driver.get("data:text/html;charset=utf-8," + quote("<html><body><p>211期 绝杀一合 [03合] 开00准</p></body></html>"))
        body_len, page_len = browser.get_document_state()
        assert body_len > 20 and page_len > 50
        assert "03合" in browser.driver.find_element("tag name", "body").text
    finally:
        browser.close()
    assert browser.driver is None
