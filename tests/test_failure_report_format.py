from pathlib import Path

from he_app.domain.models import Site
from he_app.parsers.common import format_failure_result
from he_app.services.multi_period import parse_failure_lines
from he_app.storage.reports import (
    build_current_only_results,
    build_failure_stats_lines,
    build_success_output_lines,
    format_failure_output,
)


def test_failure_line_uses_requested_single_line_contract() -> None:
    site = Site(
        "飞龙在天",
        "https://anhomo.n03wh-m2skn-wssphn.xyz/",
        "bottom",
        site_id="failure-format",
    )

    result = format_failure_result(
        213,
        site,
        "飞龙在天 没有找到213期数据",
        None,
        "无当期",
    )

    assert result == (
        "失败 飞龙在天 https://anhomo.n03wh-m2skn-wssphn.xyz/ "
        "方向: bottom 期数: 213 阶段: 指定期数校验 原因: 没有找到213期数据"
    )


def test_failure_output_has_one_failure_record_per_line() -> None:
    lines = [
        "失败 甲站 https://example.test/a 方向: top 期数: 213 阶段: 指定期数校验 原因: 缺少213期",
        "失败 乙站 https://example.test/b 方向: bottom 期数: 213 阶段: 数据校验 原因: 合数不完整",
    ]

    assert format_failure_output(
        lines,
        ["网络失败", "方向失败"],
    ) == (
        "\n\n".join(lines)
        + "\n\n失败分类统计\n"
        + "方向失败 1条\n"
        + "网络失败 1条\n"
    )


def test_failure_output_counts_every_actual_category_dynamically() -> None:
    lines = ["失败 甲", "失败 乙", "失败 丙", "失败 丁"]

    output = format_failure_output(
        lines,
        ["网络失败", "网络失败", "专属解析未命中", "未来新增分类"],
    )

    assert output.endswith(
        "失败分类统计\n"
        "网络失败 2条\n"
        "专属解析未命中 1条\n"
        "未来新增分类 1条\n"
    )
    assert "方向失败" not in output


def test_current_results_preserve_explicit_categories_and_classify_errors() -> None:
    sites = [
        Site("成功站", "https://example.test/success", "top", site_id="success"),
        Site("新分类站", "https://example.test/new", "bottom", site_id="new"),
        Site("超时站", "https://example.test/timeout", "top", site_id="timeout"),
    ]
    outcomes = {
        0: (sites[0], "03合 成功站", "214期 03合", None, ["03合"], None),
        1: (sites[1], None, "专属解析未命中", None, [], "未来新增分类"),
        2: (sites[2], None, "", "Timeout: read timed out", [], "不应覆盖错误分类"),
    }

    success, failures, ranking_values, categories = build_current_only_results(sites, outcomes, 214)

    assert success == ["03合 成功站"]
    assert len(failures) == 2
    assert ranking_values == ["03合"]
    assert categories == ["未来新增分类", "请求失败"]
    assert build_success_output_lines(success, ranking_values)[-1] == "1. 03合 1次"


def test_empty_failure_output_has_no_summary() -> None:
    assert build_failure_stats_lines([]) == []
    assert format_failure_output([], []) == ""


def test_multi_period_reader_accepts_new_failure_line(tmp_path: Path) -> None:
    path = tmp_path / "213期-合-失败.txt"
    path.write_text(
        "失败 飞龙在天 https://anhomo.n03wh-m2skn-wssphn.xyz/ "
        "方向: bottom 期数: 213 阶段: 指定期数校验 原因: 没有找到213期数据\n",
        encoding="utf-8-sig",
    )

    failures = parse_failure_lines(path, 213)

    assert failures["飞龙在天", "https://anhomo.n03wh-m2skn-wssphn.xyz/"].reason == "没有找到213期数据"
