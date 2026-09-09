import pytest

from he_app.domain.errors import SiteScrapeFailure
from he_app.domain.models import Site
from he_app.parsers import edge_repairs
from he_app.parsers.dedicated.tables import site_rule
from he_app.services import crawler
from he_app.services.browser_policy import (
    BROWSER_RENDER_REQUIRED_SITE_IDS,
    runtime_requires_browser,
)


REMAINING_30_IDS = {
    "s001_topic_206535",
    "s005_topic_453944",
    "s007_topic_206633",
    "s012_topic_451629",
    "s016_topic_497780",
    "s022_topic_678629",
    "s032_topic_309383",
    "s036_topic_205722",
    "s037_topic_252205",
    "s043_topic_252215",
    "s054_topic_205701",
    "s058_vkjwinyt",
    "s059_topic_324760",
    "s064_topic_268622",
    "s068_topic_253482",
    "s076_topic_224258",
    "s087_enpcjg",
    "s097_topic_250886",
    "s100_topic_205318",
    "s105_topic_213777",
    "s106_topic_281519",
    "s107_topic_453800",
    "s116_topic_440127",
    "s118_topic_1024655",
    "s119_topic_1024654",
    "s126_topic_930870",
    "s098_topic_227257",
    "s101_topic_274142",
    "s104_topic_245375",
    "s121_topic_224339",
}

NO_LOCATOR_IDS = {
    "s001_topic_206535",
    "s005_topic_453944",
    "s012_topic_451629",
    "s022_topic_678629",
    "s036_topic_205722",
    "s054_topic_205701",
    "s076_topic_224258",
}


def _site(site_id: str, name: str = "测试站", pick: str = "top") -> Site:
    return Site(
        name=name,
        url="https://example.test/topic/1.html",
        pick=pick,
        browser=False,
        click_first=False,
        site_id=site_id,
    )


def test_all_live_proven_remaining_sites_are_runtime_browser_capable() -> None:
    assert REMAINING_30_IDS <= BROWSER_RENDER_REQUIRED_SITE_IDS
    for site_id in REMAINING_30_IDS:
        assert runtime_requires_browser(_site(site_id), lambda _site: False)


def test_only_live_proven_generic_sites_drop_body_locator_not_edge_rules() -> None:
    for site_id in NO_LOCATOR_IDS:
        assert not site_rule(_site(site_id)).require_body_locator
    # A neighbouring remaining site keeps the original body-locator rule.
    assert site_rule(_site("s007_topic_206633")).require_body_locator


def _taxue_html() -> str:
    rows = [
        (247, "12合"),
        (248, "13合"),
        (249, "01合"),
        (250, "04合"),
        (251, "03合"),
        (252, "02合"),
    ]
    body = "".join(
        f"<p>{period}期:【绝杀一合】【{value}】开0000准</p>"
        for period, value in rows
    )
    return (
        "<html><body><nav>踏雪无痕网 导航</nav>"
        f"<article><h1>踏雪无痕网</h1>{body}</article>"
        "<footer>踏雪无痕网</footer></body></html>"
    )


def test_taxue_uses_real_complete_history_bottom_edge_only() -> None:
    site = _site("s032_topic_309383", "踏雪无痕网", "bottom")
    ok = edge_repairs._parse_taxue(site, 252, [_taxue_html()])
    assert ok[0] == "02合 踏雪无痕网"
    assert ok[2] == ["02合"]

    interior = edge_repairs._parse_taxue(site, 251, [_taxue_html()])
    assert interior[0] is None
    assert interior[3] in {"方向范围外", "超出范围"}


def _tongtian_html() -> str:
    return """
    <html><body>
      <table><tr><td>《澳门通天报㊣综合绝杀》 450121e.com</td></tr></table>
      <table>
        <tr><td>253期综合杀【水行.1头.蓝单.小单】</td></tr>
        <tr><td>252期综合杀【水行.1头.蓝单.小单】</td></tr>
      </table>
      <table><tr><td>《澳门通天报㊣澳门综合杀》 450121e.com</td></tr></table>
      <table>
        <tr><td>期数</td><td>杀肖</td><td>杀合</td><td>杀半头</td><td>开奖</td></tr>
        <tr><td>252期</td><td>牛鼠</td><td>04合</td><td>2头单</td><td>0000准</td></tr>
        <tr><td>251期</td><td>兔虎</td><td>08合</td><td>4头双</td><td>牛30准</td></tr>
        <tr><td>250期</td><td>虎牛</td><td>04合</td><td>3头单</td><td>蛇14准</td></tr>
      </table>
      <table><tr><td>《澳门通天报㊣三期四肖》 450121e.com</td></tr></table>
    </body></html>
    """


def test_tongtian_binds_exact_heading_to_adjacent_exact_header_table() -> None:
    site = _site("s058_vkjwinyt", "通天", "top")
    ok = edge_repairs._parse_tongtian(site, 252, [_tongtian_html()])
    assert ok[0] == "04合 通天"
    assert ok[2] == ["04合"]

    interior = edge_repairs._parse_tongtian(site, 251, [_tongtian_html()])
    assert interior[0] is None
    assert interior[3] == "方向范围外"

    unrelated_253 = edge_repairs._parse_tongtian(site, 253, [_tongtian_html()])
    assert unrelated_253[0] is None
    assert unrelated_253[3] == "无当期"


class _SourceDocument(str):
    def __new__(cls, text: str, source_url: str):
        obj = str.__new__(cls, text)
        obj.source_url = source_url
        return obj


class _Browser:
    def __init__(self):
        self.calls = []

    def get_documents(self, url, period, click_first, timeout):
        self.calls.append((url, period, click_first, timeout))
        return ["rendered detail"]


def test_dynamic_special_detail_renders_only_unique_same_origin_url() -> None:
    site = Site(
        name="和风细雨",
        url="https://example.test/article/admin/",
        pick="top",
        browser=False,
        click_first=False,
        site_id="s118_topic_1024655",
    )
    browser = _Browser()
    documents = [_SourceDocument("shell", "https://example.test/topic/1090741.html")]

    rendered = crawler._render_discovered_special_detail(
        browser, site, 252, 20, documents
    )

    assert rendered == ["rendered detail"]
    assert browser.calls == [("https://example.test/topic/1090741.html", 252, False, 20)]


@pytest.mark.parametrize(
    "source_urls",
    [
        ["https://evil.test/topic/1.html"],
        [
            "https://example.test/topic/1.html",
            "https://example.test/topic/2.html",
        ],
    ],
)
def test_dynamic_special_detail_refuses_cross_origin_or_multiple_targets(source_urls) -> None:
    site = Site(
        name="新人旧梦",
        url="https://example.test/article/admin/",
        pick="top",
        browser=False,
        click_first=False,
        site_id="s119_topic_1024654",
    )
    browser = _Browser()
    documents = [_SourceDocument("shell", url) for url in source_urls]

    with pytest.raises((SiteScrapeFailure, ValueError)):
        crawler._render_discovered_special_detail(browser, site, 252, 20, documents)

    assert browser.calls == []
