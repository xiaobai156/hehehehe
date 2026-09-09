import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from he_app.domain.models import Site, SourceDocument
from he_app.parsers.dedicated.history import BATCH_NEW_DEDICATED_SITE_IDS
from he_app.parsers.registry import REGISTRY
from he_app.services import document_sources, runner
from he_app.services.single_period import (
    _cross_authority_conflict,
    build_document_evidence,
    parse_site_period,
)
from he_app.storage.atomic_write import commit_text_transaction_unlocked
from he_app.storage.recent_cache import update_recent_cache_from_outcomes
from he_app.validation.record_boundary import find_unique_record
from he_app.domain.errors import SiteScrapeFailure


def site(site_id: str, name: str = "测试站", pick: str = "top") -> Site:
    return Site(name, "https://example.test/topic/1.html", pick, False, False, site_id)


def rows(items: list[tuple[int, int]]) -> str:
    return "\n".join(f"{period}期 绝杀一合 [{value:02d}合] 开00准" for period, value in items)


@pytest.mark.parametrize(
    "site_id",
    sorted(
        site_id
        for site_id in BATCH_NEW_DEDICATED_SITE_IDS
        if REGISTRY._parsers.get(site_id).__name__ == "_parse_batch"
    ),
)
def test_every_batch_parser_rejects_top_target_after_fixed_window(site_id: str) -> None:
    result = parse_site_period(site(site_id), 211, [rows([(214, 1), (213, 2), (212, 3), (211, 4)])])
    assert not result.success
    assert result.failure and result.failure.category == "方向范围外"


@pytest.mark.parametrize(
    "site_id",
    sorted(
        site_id
        for site_id in BATCH_NEW_DEDICATED_SITE_IDS
        if REGISTRY._parsers.get(site_id).__name__ == "_parse_batch"
    ),
)
def test_every_batch_parser_rejects_bottom_target_before_fixed_window(site_id: str) -> None:
    result = parse_site_period(
        site(site_id, pick="bottom"),
        211,
        [rows([(211, 4), (212, 3), (213, 2), (214, 1)])],
    )
    assert not result.success
    assert result.failure and result.failure.category == "方向范围外"


def test_batch_parser_accepts_target_inside_fixed_window() -> None:
    result = parse_site_period(site("s097_topic_250886"), 211, [rows([(211, 4), (210, 3), (209, 2)])])
    assert result.success and result.value == "04合 测试站"


def test_batch_parser_does_not_read_value_from_attribute_table() -> None:
    document = "\n".join(
        [
            "209期绝杀一合→[合10]开:08中",
            "210期绝杀一合→[合08]开:49中",
            "211期绝杀一合→[合13]开:0000中",
            "澳彩合数属性: 01合:01.10 02合:02.11.20",
        ]
    )
    result = parse_site_period(site("s104_topic_245375", "号令如山", "bottom"), 211, [document])
    assert result.success and result.value == "13合 号令如山"


def test_tongtian_rejects_top_target_after_fixed_window() -> None:
    document = "\n".join(
        [
            "澳门综合杀 杀合",
            "217期 [01合] 开00准",
            "216期 [02合] 开00准",
            "215期 [03合] 开00准",
            "214期 [04合] 开00准",
            "213期 [05合] 开00准",
            "212期 [06合] 开00准",
            "211期 [07合] 开00准",
        ]
    )
    result = parse_site_period(site("s058_vkjwinyt", "通天", "top"), 211, [document])
    assert not result.success
    assert result.failure and result.failure.category == "方向范围外"


def test_toutian_rejects_bottom_target_before_fixed_window() -> None:
    document = "\n".join(
        [
            "214期:(偷天换日)",
            "[稳杀一合]",
            "偷天换日发表",
            "211期:[稳杀一合][01合]开:00准",
            "212期:[稳杀一合][02合]开:00准",
            "213期:[稳杀一合][03合]开:00准",
            "214期:[稳杀一合][04合]开:00准",
            "合数属性",
        ]
    )
    result = parse_site_period(site("s094_topic_727508", "偷天换日", "bottom"), 211, [document])
    assert not result.success
    assert result.failure and result.failure.category == "方向范围外"


def test_woman_flavor_screenshot_layout_reads_top_target() -> None:
    document = """
    <div>澳门女人味【绝杀一尾二合】全年小错</div>
    <div>213期;绝杀【6尾】【01合-02合】开:0000准</div>
    <div>212期;绝杀【5尾】【07合-01合】开:06准</div>
    <div>211期;绝杀【3尾】【04合-02合】开:01准</div>
    """
    result = parse_site_period(site("s085_kcvpleh", "女人味", "top"), 213, [document])
    assert result.success and result.value == "01合,02合 女人味"


def test_toutian_screenshot_layout_reads_bottom_target() -> None:
    document = """
    <div>213期：【偷天换日】每期实战【稳杀一合】</div>
    <div>偷天换日 发表 于 08月01日</div>
    <div>191期：【稳杀一合】【01合】开:虎29中</div>
    <div>212期：【稳杀一合】【01合】开:牛06中</div>
    <div>213期：【稳杀一合】【04合】开:0000中</div>
    <div>合数属性</div>
    """
    result = parse_site_period(site("s094_topic_727508", "偷天换日", "bottom"), 213, [document])
    assert result.success and result.value == "04合 偷天换日"


def test_chunhua_screenshot_layout_uses_top_window() -> None:
    document = """
    <div>213期：【春花烂漫】每期实战【绝杀一合】</div>
    <div>春花烂漫 发表 于 08月01日</div>
    <div>213期：【绝杀一合】【11合】开:0000准</div>
    <div>212期：【绝杀一合】【08合】开:06准</div>
    <div>211期：【绝杀一合】【01合】开:01准</div>
    <div>210期：【绝杀一合】【05合】开:49准</div>
    <div>209期：【绝杀一合】【02合】开:08准</div>
    """
    result = parse_site_period(site("s066_topic_463139", "春花烂漫", "top"), 213, [document])
    assert result.success and result.value == "11合 春花烂漫"


def test_ruyimutan_reports_target_outside_bottom_window() -> None:
    document = """
    <div class="topic-author">如蚁慕膻 发表于</div>
    <div class="topic-content">
    210期绝杀一合 (01合)开
    211期绝杀一合 (02合)开
    212期绝杀一合 (03合)开
    213期绝杀一合 (04合)开
    214期绝杀一合 (05合)开
    215期绝杀一合 (06合)开
    216期绝杀一合 (07合)开
    </div>
    """
    outside = parse_site_period(site("s109_topic_222783", "如蚁慕膻", "bottom"), 213, [document])
    inside = parse_site_period(site("s109_topic_222783", "如蚁慕膻", "bottom"), 216, [document])
    assert not outside.success
    assert outside.failure and outside.failure.category == "方向范围外"
    assert inside.success and inside.value == "07合 如蚁慕膻"


def test_structured_direction_failure_beats_unstructured_browser_text() -> None:
    body = SourceDocument(
        rows([(210, 1), (211, 2), (212, 3), (213, 4), (214, 5), (215, 6), (216, 7)]),
        fetch_kind="browser",
        document_type="body-text",
        authority_id="browser:state:0",
        document_id="browser:state:0:body",
    )
    source = SourceDocument(
        """
        <div class="topic-author">如蚁慕膻 发表于</div>
        <div class="topic-content">
        210期绝杀一合 (01合)开 211期绝杀一合 (02合)开 212期绝杀一合 (03合)开
        213期绝杀一合 (04合)开 214期绝杀一合 (05合)开 215期绝杀一合 (06合)开
        216期绝杀一合 (07合)开
        </div>
        """,
        fetch_kind="browser",
        document_type="page-source",
        authority_id="browser:state:0",
        document_id="browser:state:0:source",
    )
    result = parse_site_period(site("s109_topic_222783", "如蚁慕膻", "bottom"), 213, [body, source])
    assert not result.success
    assert result.failure and result.failure.category == "方向范围外"


def test_same_http_authority_documents_cannot_form_one_direction_window() -> None:
    source = "http:https://example.test/topic/1.html"
    page = SourceDocument(
        rows([(211, 1), (212, 2), (213, 3), (214, 4)]),
        source_url="https://example.test/topic/1.html",
        fetch_kind="http",
        document_type="html",
        authority_id=source,
        document_id="page",
    )
    decoded = SourceDocument(
        rows([(211, 5), (210, 6), (209, 7)]),
        source_url="https://example.test/topic/1.html",
        fetch_kind="http-decoded",
        document_type="decoded",
        authority_id=source,
        document_id="decoded",
    )
    result = parse_site_period(site("s097_topic_250886", pick="bottom"), 211, [page, decoded])
    assert not result.success
    assert result.failure and result.failure.category == "方向范围外"


def test_empty_page_cannot_rescue_yingba_from_script() -> None:
    page = SourceDocument(
        "页面空壳，无目标资料",
        fetch_kind="http",
        document_type="html",
        authority_id="page",
        document_id="page",
    )
    script = SourceDocument(
        "盈把之木\n209期:绝杀一合[01合]开00准\n210期:绝杀一合[02合]开00准\n211期:绝杀一合[03合]开00准",
        fetch_kind="script",
        document_type="script",
        authority_id="script",
        document_id="script",
    )
    result = parse_site_period(site("s013_topic_226261", "盈把之木", "bottom"), 211, [page, script])
    assert not result.success


def test_empty_page_cannot_rescue_shushen_from_script() -> None:
    page = SourceDocument(
        "页面空壳，无目标资料",
        fetch_kind="http",
        document_type="html",
        authority_id="page",
        document_id="page",
    )
    script = SourceDocument(
        "束身自修\n209期:绝杀一合[04合]开00准\n210期:绝杀一合[05合]开00准\n211期:绝杀一合[06合]开00准",
        fetch_kind="script",
        document_type="script",
        authority_id="script",
        document_id="script",
    )
    result = parse_site_period(site("s057_topic_227386", "束身自修", "bottom"), 211, [page, script])
    assert not result.success


def test_yidianhong_browser_mode_uses_complete_rendered_document(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    http_site = Site(
        "一点红",
        "https://example.test/topic/246762.html",
        "bottom",
        False,
        False,
        "s070_topic_246762",
    )
    browser_site = Site(
        http_site.name,
        http_site.url,
        http_site.pick,
        True,
        http_site.click_first,
        http_site.site_id,
    )
    monkeypatch.setattr(document_sources, "collect_yidianhong_documents", lambda *_args: ["script"])
    assert document_sources.collect_special_site_documents(None, http_site, 1, 213) == ["script"]
    assert document_sources.collect_special_site_documents(None, browser_site, 1, 213) is None


def test_dynamic_column_missing_target_topic_is_explicit_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dynamic_site = Site(
        "动态栏目",
        "https://example.test/topic/1024655.html",
        "top",
        False,
        False,
        "s118_topic_1024655",
    )
    monkeypatch.setattr(document_sources, "collect_documents", lambda *_args: [])
    with pytest.raises(SiteScrapeFailure, match="栏目页未找到213期"):
        document_sources.collect_dynamic_home_topic_documents(None, dynamic_site, 1, 213)


def test_forum_api_requires_a_record_object() -> None:
    payload = json.dumps({"draw": 213, "topic": "绝杀一合", "content": "[05合]"})
    assert document_sources.forum_api_documents_from_json(payload) == ["213期:绝杀一合\n[05合]"]
    with pytest.raises(RuntimeError, match="unsupported payload"):
        document_sources.forum_api_documents_from_json("null")


def test_tongtian_does_not_cross_table_boundary_for_evidence() -> None:
    document = "\n".join(
        [
            "澳门综合杀 杀合",
            "其他栏目",
            "211期 [07合] 开00准",
        ]
    )
    result = parse_site_period(site("s058_vkjwinyt", "通天", "top"), 211, [document])
    assert not result.success


def test_same_browser_state_documents_cannot_form_one_direction_window() -> None:
    body = SourceDocument(
        rows([(211, 1), (212, 2), (213, 3), (214, 4)]),
        fetch_kind="browser",
        document_type="body-text",
        authority_id="browser:state:0",
        document_id="body",
    )
    source = SourceDocument(
        rows([(211, 5), (210, 6), (209, 7)]),
        fetch_kind="browser",
        document_type="page-source",
        authority_id="browser:state:0",
        document_id="source",
    )
    result = parse_site_period(site("s097_topic_250886", pick="bottom"), 211, [body, source])
    assert not result.success
    assert result.failure and result.failure.category == "方向范围外"


def test_batch_parser_accepts_abbreviated_row_only_under_strict_header() -> None:
    document = "绝杀一合 专属栏目\n211期:杀 [(10合)] 开:01准\n210期:杀 [(07合)] 开:49准"
    result = parse_site_period(site("s112_dd_62782b_bbs_10699", "师出长安"), 211, [document])
    assert result.success and result.value == "10合 师出长安"


def test_interior_same_period_duplicate_does_not_override_edge() -> None:
    result = parse_site_period(
        site("s097_topic_250886"),
        211,
        ["211期 绝杀一合 [01合] 开00准\n211期 绝杀一合 [02合] 开00准\n210期 绝杀一合 [03合] 开00准"],
    )
    assert result.success and result.value == "01合 测试站"


def test_dedicated_parser_does_not_fall_back_to_generic_formula() -> None:
    result = parse_site_period(
        site("s071_topic_768615", "木南少年", "bottom"),
        211,
        ["211期 公式杀合 [03合] 开00准"],
    )
    assert not result.success


def test_saima_requires_structured_archive() -> None:
    result = parse_site_period(
        site("s060_topic_589491", "赛码会", "bottom"),
        211,
        ["211期 绝杀一合 [03合] 开00准"],
    )
    assert not result.success
    assert result.failure and result.failure.category == "锚点缺失"


def test_saima_rejects_bottom_target_before_fixed_window() -> None:
    document = """
    <div class="box-theme01d"><div class="title">214期【绝杀一合】</div>
    <div class="topic-author">作者:会变化的网址</div><div class="topic-content">
    <p>211期:绝杀一合【01合】开:00准</p><p>212期:绝杀一合【02合】开:00准</p>
    <p>213期:绝杀一合【03合】开:00准</p><p>214期:绝杀一合【04合】开:00准</p>
    </div></div>
    """
    result = parse_site_period(site("s060_topic_589491", "赛码会", "bottom"), 211, [document])
    assert not result.success
    assert result.failure and result.failure.category == "方向范围外"


def test_generic_parser_rejects_target_after_fixed_top_window() -> None:
    document = "作者:测试站\n" + rows([(214, 1), (213, 2), (212, 3), (211, 4)])
    result = parse_site_period(site("s001_topic_206535"), 211, [document])
    assert not result.success
    assert result.failure and result.failure.category == "超出范围"


def test_duplicate_target_rows_use_first_for_top_and_last_for_bottom() -> None:
    document = "\n".join(
        [
            "作者:测试站",
            "215期 绝杀一合 [03合] 开00准",
            "214期 绝杀一合 [02合] 开00准",
            "215期 绝杀一合 [03合] 开01准",
        ]
    )
    top = parse_site_period(site("s001_topic_206535", pick="top"), 215, [document])
    bottom = parse_site_period(site("s001_topic_206535", pick="bottom"), 215, [document])
    assert top.success and top.value == "03合 测试站"
    assert bottom.success and bottom.value == "03合 测试站"


def test_same_selected_browser_row_is_deduplicated_across_renderings() -> None:
    body = SourceDocument(
        rows([(213, 10), (212, 2), (211, 3)]),
        fetch_kind="browser",
        document_type="body-text",
        authority_id="browser:state:0",
        document_id="browser:state:0:body",
    )
    source = SourceDocument(
        rows([(213, 10), (212, 2), (211, 3)]),
        fetch_kind="browser",
        document_type="page-source",
        authority_id="browser:state:0",
        document_id="browser:state:0:source",
    )
    result = parse_site_period(site("s097_topic_250886"), 213, [body, source])
    assert result.success and result.value == "10合 测试站"


def test_unselected_rows_do_not_create_cross_authority_conflict() -> None:
    body = SourceDocument(
        rows([(213, 10), (212, 2), (211, 3), (213, 8)]),
        fetch_kind="browser",
        authority_id="browser:body",
        document_id="browser:body",
    )
    source = SourceDocument(
        rows([(213, 10), (212, 2), (211, 3), (213, 6)]),
        fetch_kind="browser",
        authority_id="browser:source",
        document_id="browser:source",
    )
    result = parse_site_period(site("s097_topic_250886"), 213, [body, source])
    assert result.success and result.value == "10合 测试站"


def test_different_selected_rows_still_create_cross_authority_conflict() -> None:
    body = SourceDocument(
        rows([(213, 10), (212, 2), (211, 3), (213, 8)]),
        fetch_kind="browser",
        authority_id="browser:body",
        document_id="browser:body",
    )
    source = SourceDocument(
        rows([(213, 8), (212, 2), (211, 3), (213, 10)]),
        fetch_kind="browser",
        authority_id="browser:source",
        document_id="browser:source",
    )
    result = parse_site_period(site("s097_topic_250886"), 213, [body, source])
    assert not result.success
    assert result.failure and result.failure.category == "候选冲突"


@pytest.mark.parametrize("period,value,success", [(214, "01合", True), (213, "02合", False), (212, "03合", False)])
def test_target_and_adjacent_periods_are_read_independently(
    period: int, value: str, success: bool
) -> None:
    result = parse_site_period(
        site("s097_topic_250886"),
        period,
        [rows([(214, 1), (213, 2), (212, 3)])],
    )
    assert result.success is success
    if success:
        assert result.value == f"{value} 测试站"


def test_missing_period_is_not_filled_from_adjacent_rows() -> None:
    result = parse_site_period(
        site("s097_topic_250886"),
        999,
        [rows([(214, 1), (213, 2), (212, 3)])],
    )
    assert not result.success


def test_chunhua_uses_configured_top_direction() -> None:
    document = "春花烂漫\n" + rows([(213, 4), (214, 3), (215, 2), (216, 1)])
    result = parse_site_period(site("s066_topic_463139", "春花烂漫", "top"), 213, [document])
    assert result.success and result.value == "04合 春花烂漫"


def test_unconfigured_script_cannot_rescue_direction_failure() -> None:
    documents = [
        SourceDocument(
            rows([(214, 2), (213, 2), (212, 2), (211, 1)]),
            source_url="https://example.test/",
            fetch_kind="http",
            authority_id="page",
            document_id="page:html",
        ),
        SourceDocument(
            rows([(211, 5), (210, 2), (209, 3)]),
            source_url="https://example.test/data.js",
            fetch_kind="script",
            authority_id="script",
            document_id="script:decoded",
        ),
    ]
    result = parse_site_period(site("s097_topic_250886"), 211, documents)
    assert not result.success


def test_cross_authority_same_multi_value_candidate_is_not_a_conflict() -> None:
    evaluation = ("01合,06合 测试站", "211期 绝杀一尾二合 [01合-06合] 开", ["01合", "06合"], None)
    evaluations = [
        ([], evaluation, set(), []),
        ([], evaluation, set(), []),
    ]
    assert _cross_authority_conflict(site("audit"), 211, evaluations) is None


def test_unconfigured_decoded_document_cannot_rescue_old_document() -> None:
    old_document = SourceDocument(
        rows([(214, 1), (213, 2), (212, 3)]),
        fetch_kind="http",
        authority_id="old",
        document_id="old",
    )
    current_document = SourceDocument(
        rows([(211, 4), (210, 3), (209, 2)]),
        fetch_kind="http-decoded",
        authority_id="current",
        document_id="current",
    )
    result = parse_site_period(site("s097_topic_250886"), 211, [old_document, current_document])
    assert not result.success


def test_record_boundary_rejects_nested_duplicate_id() -> None:
    payload = {"data": [{"id": "target", "content": {"recordId": "target"}}]}
    with pytest.raises(SiteScrapeFailure, match="找到2条"):
        find_unique_record(payload, "target")


def test_browser_states_cannot_form_one_candidate_block() -> None:
    documents = [
        SourceDocument(
            "211期",
            fetch_kind="browser",
            authority_id="browser:state:0",
            document_id="browser:state:0:body",
        ),
        SourceDocument(
            "绝杀一合 [03合] 开00准",
            fetch_kind="browser",
            authority_id="browser:state:1",
            document_id="browser:state:1:body",
        ),
    ]
    assert not parse_site_period(site(""), 211, documents).success


def test_evidence_rejects_cross_section_assembly() -> None:
    evidence = build_document_evidence(
        site("audit"),
        211,
        ["03合"],
        "211期 绝杀一合 [03合] 开",
        ["211期 作者区\n另一个栏目 绝杀一合\n第三个栏目 [03合] 开00准"],
    )
    assert evidence == ()


def test_special_table_evidence_rejects_row_outside_bounded_section() -> None:
    evidence = build_document_evidence(
        site("s073_shuqhbq", "九肖杀", "top"),
        211,
        ["02合"],
        "211期:九肖杀 绝杀①段①合 211期杀[02合]开",
        "其他栏目\n211期杀[02合]开\n绝杀①段①合\n绝杀三尾",
    )
    assert evidence == ()


def test_same_row_evidence_records_real_source_identity() -> None:
    document = SourceDocument(
        "211期 绝杀一合 [03合] 开00准",
        source_url="https://cdn.test/a.js",
        fetch_kind="script-decoded",
        document_type="decoded",
        authority_id="script:a",
        document_id="script:a:1",
    )
    evidence = build_document_evidence(site("s097_topic_250886"), 211, ["03合"],
                                       str(document), [document])
    assert evidence
    assert evidence[0].source_url == "https://cdn.test/a.js"
    assert evidence[0].authority_id == "script:a"
    assert evidence[0].block_start >= 0
    assert not parse_site_period(site("s097_topic_250886"), 211, [document]).success


def test_cache_layer_writes_current_value_on_conflict(tmp_path: Path) -> None:
    target = tmp_path / "recent.json"
    test_site = site("audit")
    target.write_text(
        json.dumps(
            {
                "base_period": 211,
                "periods": 10,
                "sites": [
                    {
                        "id": "audit",
                        "name": test_site.name,
                        "url": test_site.url,
                        "pick": "top",
                        "browser": False,
                        "click_first": False,
                        "fingerprint": {"211": "03合"},
                    }
                ],
                "errors": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    update_recent_cache_from_outcomes(
        target,
        [test_site],
        {0: (test_site, "04合 测试站", "211期 绝杀一合 [04合] 开", None, ["04合"], None)},
        211,
    )
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["sites"][0]["fingerprint"]["211"] == "04合"
    assert payload["errors"] == []


def test_cache_layer_removes_failed_current_period_but_keeps_older_history(tmp_path: Path) -> None:
    target = tmp_path / "recent.json"
    test_site = site("audit")
    target.write_text(
        json.dumps(
            {
                "base_period": 211,
                "periods": 10,
                "sites": [
                    {
                        "id": "audit",
                        "name": test_site.name,
                        "url": test_site.url,
                        "pick": "top",
                        "browser": False,
                        "click_first": False,
                        "fingerprint": {"210": "02合", "211": "03合"},
                    }
                ],
                "errors": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    update_recent_cache_from_outcomes(
        target,
        [test_site],
        {0: (test_site, None, "没有找到211期", None, [], "无当期")},
        211,
    )

    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["sites"][0]["fingerprint"] == {"210": "02合"}
    assert payload["errors"][0]["id"] == "audit"


def test_cache_layer_creates_missing_cache_from_current_result(tmp_path: Path) -> None:
    target = tmp_path / "recent.json"
    test_site = site("audit")

    update_recent_cache_from_outcomes(
        target,
        [test_site],
        {0: (test_site, "04合 测试站", "211期 绝杀一合 [04合] 开", None, ["04合"], None)},
        211,
    )

    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["sites"][0]["fingerprint"] == {"211": "04合"}


def test_report_and_cache_transaction_rolls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    first = tmp_path / "success.txt"
    second = tmp_path / "cache.json"
    first.write_text("old success", encoding="utf-8")
    second.write_text("old cache", encoding="utf-8")
    original_replace = Path.replace
    raised = False

    def fail_second_once(path: Path, target: Path):
        nonlocal raised
        if Path(target) == second and not raised:
            raised = True
            raise PermissionError("simulated cache failure")
        return original_replace(path, target)

    monkeypatch.setattr(Path, "replace", fail_second_once)
    with pytest.raises(PermissionError):
        commit_text_transaction_unlocked(
            {first: ("new success", "utf-8"), second: ("new cache", "utf-8")}
        )
    assert first.read_text(encoding="utf-8") == "old success"
    assert second.read_text(encoding="utf-8") == "old cache"


def test_runner_cache_conflict_does_not_change_live_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    test_site = site("audit")
    cache_path = tmp_path / "recent.json"
    success_path = tmp_path / "success.txt"
    fail_path = tmp_path / "fail.txt"
    sites_path = tmp_path / "sites.json"
    cache_path.write_text(
        json.dumps(
            {
                "base_period": 211,
                "periods": 10,
                "sites": [
                    {
                        "id": "audit",
                        "name": test_site.name,
                        "url": test_site.url,
                        "pick": "top",
                        "browser": False,
                        "click_first": False,
                        "fingerprint": {"211": "03合"},
                    }
                ],
                "errors": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(runner, "load_sites", lambda _path: [test_site])
    monkeypatch.setattr(
        runner,
        "scrape_parallel_site",
        lambda index, current_site, *_args, **_kwargs: (
            index,
            current_site,
            "04合 测试站",
            "211期 绝杀一合 [04合] 开",
            None,
            ["04合"],
            None,
        ),
    )
    args = SimpleNamespace(
        period=211,
        success=str(success_path),
        fail=str(fail_path),
        timeout=1,
        workers=1,
        retries=0,
        delay=0.0,
        show_browser=False,
        cache="",
        mirror_limit=0,
        sites=str(sites_path),
        cache_max_age_hours=24.0,
        fingerprint_cache=str(cache_path),
        no_fingerprint_cache_sync=False,
        preserve_unconfigured_cache_sites=False,
        no_isolation=True,
        cycle_year=2026,
    )
    assert runner.run(args) == 0
    success_text = success_path.read_text(encoding="utf-8-sig")
    assert "04合 测试站" in success_text
    assert "所有目录耗时统计" not in success_text
    assert not fail_path.exists()
    payload = json.loads(cache_path.read_text(encoding="utf-8"))
    assert payload["sites"][0]["fingerprint"]["2026-211"] == "04合"


def test_runner_preserves_live_txt_when_cache_update_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    test_site = site("audit")
    cache_path = tmp_path / "recent.json"
    success_path = tmp_path / "success.txt"
    fail_path = tmp_path / "fail.txt"
    sites_path = tmp_path / "sites.json"
    cache_path.write_text("{broken", encoding="utf-8")
    monkeypatch.setattr(runner, "load_sites", lambda _path: [test_site])
    monkeypatch.setattr(
        runner,
        "scrape_parallel_site",
        lambda index, current_site, *_args, **_kwargs: (
            index,
            current_site,
            "04合 测试站",
            "211期 绝杀一合 [04合] 开",
            None,
            ["04合"],
            None,
        ),
    )
    args = SimpleNamespace(
        period=211,
        success=str(success_path),
        fail=str(fail_path),
        timeout=1,
        workers=1,
        retries=0,
        delay=0.0,
        show_browser=False,
        cache="",
        mirror_limit=0,
        sites=str(sites_path),
        cache_max_age_hours=24.0,
        fingerprint_cache=str(cache_path),
        no_fingerprint_cache_sync=False,
        preserve_unconfigured_cache_sites=False,
        no_isolation=True,
        cycle_year=2026,
    )

    assert runner.run(args) == 1
    assert "04合 测试站" in success_path.read_text(encoding="utf-8-sig")
    assert not fail_path.exists()
    assert cache_path.read_text(encoding="utf-8") == "{broken"


def test_runner_cli_defaults_and_future_exception_fallback() -> None:
    args = runner.build_arg_parser().parse_args(["--period", "211"])
    test_site = site("audit")

    class FailedFuture:
        def result(self):
            raise RuntimeError("boom")

    fallback = runner.consume_site_future(FailedFuture(), test_site, 7)

    assert args.workers == 8
    assert fallback[:3] == (7, test_site, None)
    assert fallback[4] == "RuntimeError: boom"
