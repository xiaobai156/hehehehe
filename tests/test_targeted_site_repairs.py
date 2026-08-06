from pathlib import Path

from he_app.config.sites import load_sites
from he_app.domain.models import Site
from he_app.parsers.dedicated.history import (
    extract_ruyimutan_kill_sum_period_values,
    ruyimutan_author_content_blocks,
)
from he_app.services.single_period import parse_site_period


def _ruyi_row(period: int, value: int) -> str:
    return f"{period}期绝杀一合 ({value:02d}合)开00准"


def _ruyi_two_cycle_document() -> str:
    first_cycle = list(range(210, 366)) + list(range(1, 210))
    first_rows = [_ruyi_row(period, (period % 13) + 1) for period in first_cycle]
    current_rows = [_ruyi_row(period, value) for period, value in ((210, 6), (211, 7), (212, 3), (213, 2))]
    return (
        '<div class="topic-author">作者:如蚁慕膻</div>'
        '<div class="topic-content">'
        + " ".join(first_rows + current_rows)
        + " 澳彩合数属性"
        + "</div>"
    )


def test_ruyimutan_keeps_current_cycle_for_bottom_window() -> None:
    document = _ruyi_two_cycle_document()

    blocks = ruyimutan_author_content_blocks([document])
    result = parse_site_period(
        Site("如蚁慕膻", "https://example.test/topic/222783.html", "bottom", site_id="s109_topic_222783"),
        213,
        [document],
    )

    assert len(blocks) == 2
    assert [row.line.split("期", 1)[0] for row in blocks[-1]] == ["210", "211", "212", "213"]
    assert result.success and result.value == "02合 如蚁慕膻"


def test_ruyimutan_fingerprint_uses_current_cycle_value() -> None:
    values = extract_ruyimutan_kill_sum_period_values([_ruyi_two_cycle_document()])

    assert values[213] == "02合"


def test_ruyimutan_bottom_does_not_fall_back_to_previous_cycle() -> None:
    result = parse_site_period(
        Site("如蚁慕膻", "https://example.test/topic/222783.html", "bottom", site_id="s109_topic_222783"),
        209,
        [_ruyi_two_cycle_document()],
    )

    assert not result.success
    assert result.failure and result.failure.category == "方向范围外"


def test_chunhua_configured_direction_is_top() -> None:
    sites = load_sites(Path(__file__).resolve().parents[1] / "sites.json")
    spring = next(site for site in sites if site.site_id == "s066_topic_463139")

    assert spring.pick == "top"


def test_three_reported_sites_use_top_direction() -> None:
    sites = load_sites(Path(__file__).resolve().parents[1] / "sites.json")
    by_id = {site.site_id: site for site in sites}

    assert by_id["s043_topic_252215"].pick == "top"
    assert by_id["s059_topic_324760"].pick == "top"
    assert by_id["s101_topic_274142"].pick == "top"


def test_s101_parser_does_not_need_site_url_as_anchor() -> None:
    document = "\n".join(
        [
            "216期:【绝杀一合】—118060d.com",
            "作者:118060d.com",
            "216期：【绝杀一合】[05合] 开:0000准",
            "215期：【绝杀一合】[04合] 开:蛇14准",
            "214期：【绝杀一合】[08合] 开:兔04准",
        ]
    )
    site = Site(
        "没名字二",
        "https://must-not-be-used-as-anchor.example/topic/274142.html",
        "top",
        site_id="s101_topic_274142",
    )

    result = parse_site_period(site, 216, [document])

    assert result.success and result.value == "05合 没名字二"
