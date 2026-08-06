import pytest

from he_app.domain.models import Site, SourceDocument
from he_app.parsers.common import (
    candidate_window_key,
    candidate_window_keys,
    candidate_window_lines,
    classify_failure,
    collect_period_lines,
    conflict_values_from_trusted_parts,
    failure_stage,
    find_candidate,
    ordered_candidate_window_parts,
    ordered_candidate_window_periods,
    select_candidate_from_trusted_parts,
    selected_latest_period_candidate,
)
from he_app.parsers.dedicated.structured import (
    find_dajiafa_kill_sum_candidate,
    find_yiaizhimin_kill_sum_candidate,
)
from he_app.parsers.registry import REGISTRY
from he_app.services.single_period import parse_site_period
from he_app.validation.direction import directional_window


def site(site_id: str, pick: str) -> Site:
    return Site("测试站", "https://example.test/topic/1.html", pick, False, False, site_id)


def rows(items: list[tuple[int, int]]) -> str:
    return "\n".join(
        f"{period}期 绝杀一合 [{value:02d}合] 开00准" for period, value in items
    )


def test_directional_window_uses_physical_edge_not_period_order() -> None:
    items = [("001", "first"), ("365", "middle"), ("214", "last")]

    assert directional_window(items, "top", 3) == [items[0]]
    assert directional_window(items, "bottom", 3) == [items[-1]]


def test_directional_window_rejects_invalid_size_and_handles_empty() -> None:
    with pytest.raises(ValueError, match="方向窗口必须大于0"):
        directional_window([1], "top", 0)
    assert directional_window([], "bottom") == []


def test_window_helper_api_exposes_only_the_physical_edge() -> None:
    parts = [
        ("214期 绝杀一合 [02合] 开00准", True),
        ("213期 绝杀一合 [01合] 开00准", True),
    ]

    assert ordered_candidate_window_parts(parts, "top", size=9) == [parts[0]]
    assert ordered_candidate_window_parts(parts, "bottom", size=9) == [parts[-1]]
    assert ordered_candidate_window_periods(parts, "top") == {214}
    assert candidate_window_lines(parts, "bottom") == {parts[-1][0]}
    assert candidate_window_keys(parts, "top") == {(214, "02合")}
    assert candidate_window_key("只有说明文字") is None


def test_common_candidate_helpers_preserve_edge_and_diagnostics() -> None:
    document = "作者:测试站\n" + rows([(214, 2), (213, 1)])

    assert selected_latest_period_candidate([document], "top").values == "02合"
    assert selected_latest_period_candidate(["无数据"], "top") is None
    assert find_candidate([document], 214, "top").values == "02合"
    assert find_candidate([document], 213, "top") is None
    assert collect_period_lines([document], 214) == [
        "214期 绝杀一合 [02合] 开00准"
    ]

    raw_parts = [
        ("214期 绝杀一合 [02合] 开00准", True),
        ("214期 绝杀一合 [06合] 开00准", True),
        ("无效候选", True),
    ]
    values, lines = conflict_values_from_trusted_parts(raw_parts)
    assert values == ["02合", "06合"] and len(lines) == 2
    assert select_candidate_from_trusted_parts(raw_parts, "top").values == "02合"
    assert select_candidate_from_trusted_parts(raw_parts, "bottom").values == "06合"
    assert select_candidate_from_trusted_parts([], "top") is None


@pytest.mark.parametrize(
    "detail,error,category,stage",
    [
        ("", "TimeoutError: slow", "请求失败", "网络请求"),
        ("未执行", None, "未执行", "任务执行"),
        ("测试站 没找到作者锚点", None, "锚点缺失", "目标定位"),
        ("找到214期但数量校验失败", None, "数据不完整", "数据校验"),
        ("页面里没找到214期", None, "无当期", "指定期数校验"),
    ],
)
def test_failure_classification_keeps_stage_contract(
    detail: str, error: str | None, category: str, stage: str
) -> None:
    failure = classify_failure(214, detail, error)
    assert failure.category == category
    assert failure_stage(failure.category) == stage


@pytest.mark.parametrize("pick,target", [("top", 213), ("bottom", 213)])
def test_generic_direction_requires_target_at_block_edge(pick: str, target: int) -> None:
    document = "作者:测试站\n" + rows([(214, 1), (213, 2), (212, 3)])

    result = parse_site_period(site("s001_topic_206535", pick), target, [document])

    assert not result.success
    assert result.failure and result.failure.category in {"方向范围外", "超出范围"}


@pytest.mark.parametrize("pick,target", [("top", 213), ("bottom", 213)])
def test_batch_direction_requires_target_at_block_edge(pick: str, target: int) -> None:
    site_id = "s097_topic_250886"
    assert REGISTRY._parsers[site_id].__name__ == "_parse_batch"
    document = rows([(214, 1), (213, 2), (212, 3)])

    result = parse_site_period(site(site_id, pick), target, [document])

    assert not result.success
    assert result.failure and result.failure.category in {"方向范围外", "超出范围"}


def test_table_direction_requires_target_at_block_edge() -> None:
    document = "\n".join(
        [
            "澳门综合杀 杀合",
            "214期 [01合] 开00准",
            "213期 [02合] 开00准",
            "212期 [03合] 开00准",
        ]
    )

    top = parse_site_period(site("s058_vkjwinyt", "top"), 213, [document])
    bottom = parse_site_period(site("s058_vkjwinyt", "bottom"), 213, [document])

    assert not top.success
    assert not bottom.success


def test_generic_top_ignores_later_duplicate_target_outside_edge() -> None:
    document = "作者:测试站\n" + rows(
        [
            (214, 2),
            (213, 1),
            (212, 12),
            (222, 11),
            (221, 9),
            (220, 7),
            (219, 8),
            (218, 4),
            (217, 13),
            (216, 8),
            (215, 5),
            (214, 6),
        ]
    )

    result = parse_site_period(site("s003_topic_193293", "top"), 214, [document])

    assert result.success and result.value == "02合 测试站"


def test_batch_uses_only_first_row_when_target_repeats_at_top() -> None:
    document = rows([(214, 2), (214, 6), (213, 1)])

    result = parse_site_period(site("s097_topic_250886", "top"), 214, [document])

    assert result.success and result.value == "02合 测试站"


def test_table_uses_only_first_row_when_target_repeats_at_top() -> None:
    document = "\n".join(
        [
            "澳门综合杀 杀合",
            "214期 [02合] 开00准",
            "214期 [06合] 开00准",
            "213期 [01合] 开00准",
        ]
    )

    result = parse_site_period(site("s058_vkjwinyt", "top"), 214, [document])

    assert result.success and result.value == "02合 测试站"


def test_manager_rows_use_physical_edge_before_same_period_conflict() -> None:
    document = """
    <article data-manager-record-id="6a1f8c61b67e3ff2e4c4ecbb"
             data-manager-author="以爱之名" data-manager-section="mainarticle">
      <div data-manager-body="1">
        <p>214期:[以爱之名]绝杀一合[02]开:</p>
        <p>213期:[以爱之名]绝杀一合[01]开:</p>
        <p>214期:[以爱之名]绝杀一合[06]开:</p>
      </div>
    </article>
    """

    top, top_outside = find_yiaizhimin_kill_sum_candidate([document], 214, "top")
    bottom, bottom_outside = find_yiaizhimin_kill_sum_candidate(
        [document], 214, "bottom"
    )

    assert top and top.values == "02合" and not top_outside
    assert bottom and bottom.values == "06合" and not bottom_outside


def test_structured_cycles_select_only_the_direction_edge_cycle() -> None:
    document = """
    <div class="title">214期:[绝杀一合]甲作者</div>
    <div class="topic-author">作者:甲作者</div>
    <div class="topic-content">
      <p>214期:绝杀一合[杀02合]开:00准</p>
      <p>213期:绝杀一合[杀01合]开:00准</p>
    </div>
    <div class="title">214期:[绝杀一合]乙作者</div>
    <div class="topic-author">作者:乙作者</div>
    <div class="topic-content">
      <p>214期:绝杀一合[杀06合]开:00准</p>
      <p>213期:绝杀一合[杀05合]开:00准</p>
    </div>
    """

    top, top_outside = find_dajiafa_kill_sum_candidate([document], 214, "top")
    bottom, bottom_outside = find_dajiafa_kill_sum_candidate(
        [document], 213, "bottom"
    )

    assert top and top.values == "02合" and not top_outside
    assert bottom and bottom.values == "05合" and not bottom_outside


def test_generic_bottom_ignores_earlier_duplicate_target_outside_edge() -> None:
    document = "作者:测试站\n" + rows(
        [(214, 6), (211, 3), (212, 4), (213, 5), (214, 2)]
    )

    result = parse_site_period(site("s003_topic_193293", "bottom"), 214, [document])

    assert result.success and result.value == "02合 测试站"


def test_direction_outside_source_cannot_conflict_with_qualified_edge() -> None:
    body = SourceDocument(
        "作者:测试站\n" + rows([(214, 2), (213, 1), (212, 12)]),
        fetch_kind="browser",
        document_type="body-text",
        authority_id="browser:body",
        document_id="browser:body",
    )
    old_source = SourceDocument(
        "作者:测试站\n" + rows([(222, 11), (221, 9), (220, 7), (214, 6)]),
        fetch_kind="browser",
        document_type="page-source",
        authority_id="browser:source",
        document_id="browser:source",
    )

    result = parse_site_period(
        site("s003_topic_193293", "top"), 214, [body, old_source]
    )

    assert result.success and result.value == "02合 测试站"
