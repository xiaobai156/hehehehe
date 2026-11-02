# -*- coding: utf-8 -*-
"""回归测试：跨期借值判定的公共语义（crawler 与 reports 必须一致）。

背景（2026-10-25 审查复核）：
- reports 曾用「期号 != period 即借值」，crawler 只认 period+1，两边语义分叉；
  出现 period+2 时会出现「reports 判借值、crawler 按当期写缓存」的隐患。
- 现已统一到 he_app.validation.period.parse_matched_period。

同时锁住：失败原因要保留真实命中期号，不能写死 period+1。
"""
from __future__ import annotations

from he_app.domain.models import Site
from he_app.services.crawler import matched_period_from_reason
from he_app.storage.reports import build_current_only_results, borrowed_period
from he_app.validation.period import (
    INVALID_MATCHED_PERIOD,
    MATCHED_PERIOD_PREFIX,
    matched_period_or_requested,
    parse_matched_period,
)


def make_site(name: str = "展翅飞翔", site_id: str = "s149_zhanchi") -> Site:
    return Site(name, f"https://{site_id}.example/topic/1.html", "bottom", False, False, site_id, 1)


def marker(period: int) -> str:
    return f"{MATCHED_PERIOD_PREFIX}{period}"


def test_parse_matched_period_basic():
    assert parse_matched_period(marker(298)) == 298
    assert parse_matched_period(None) is None
    assert parse_matched_period("无当期") is None
    assert parse_matched_period("候选冲突") is None
    # 有前缀但内容非法：返回哨兵 0（fail-closed，按借值处理），不能当成「无标记」放行
    assert parse_matched_period(marker("abc")) == INVALID_MATCHED_PERIOD
    assert parse_matched_period(marker("")) == INVALID_MATCHED_PERIOD
    assert parse_matched_period(marker("-5")) == INVALID_MATCHED_PERIOD
    assert parse_matched_period(marker("0")) == INVALID_MATCHED_PERIOD


def test_invalid_marker_is_fail_closed():
    """标记非法时不得按当期成功放行。"""
    site = make_site()
    outcomes = {
        0: (site, "06合 展翅飞翔", "…", None, ["06合"], marker("abc")),
    }
    success, fail, ranking, _cats = build_current_only_results([site], outcomes, 297)
    assert success == []
    assert ranking == []
    assert len(fail) == 1
    assert "命中标记非法" in fail[0]


def test_superscript_digit_does_not_raise():
    """'²'.isdigit() 为 True 但 int() 会抛错，必须被兜住而不是让整批崩。"""
    assert parse_matched_period(marker("²")) == INVALID_MATCHED_PERIOD
    assert parse_matched_period(marker("9" * 5000)) == INVALID_MATCHED_PERIOD


def test_crawler_and_reports_agree_on_far_future_period():
    """period+2 时两边都必须判为借值（修复前 crawler 会当当期）。"""
    requested = 297
    far = 300
    # crawler 侧：真实期号应为 300，而不是回退成 297
    assert matched_period_from_reason(requested, marker(far)) == far
    assert matched_period_or_requested(requested, marker(far)) == far
    # reports 侧：判为借值
    assert borrowed_period(requested, marker(far)) == far


def test_same_period_marker_is_not_borrowed():
    assert borrowed_period(297, marker(297)) is None
    assert matched_period_from_reason(297, marker(297)) == 297


def test_failure_detail_keeps_real_matched_period():
    site = make_site()
    outcomes = {
        0: (site, "06合 展翅飞翔", "300期 … 开", None, ["06合"], marker(300)),
    }
    success, fail, ranking, _cats = build_current_only_results([site], outcomes, 297)
    assert success == []
    assert ranking == []
    assert len(fail) == 1
    assert "仅命中300期" in fail[0], f"应保留真实期号300，实际: {fail[0]}"
    assert "298期" not in fail[0].replace("297期", "")


def test_next_period_still_blocked():
    site = make_site()
    outcomes = {
        0: (site, "06合 展翅飞翔", "298期 … 开", None, ["06合"], marker(298)),
    }
    success, fail, ranking, _cats = build_current_only_results([site], outcomes, 297)
    assert success == []
    assert ranking == []
    assert "仅命中298期" in fail[0]


def test_normal_success_and_failure_unaffected():
    site_ok = make_site("甲", "s001_a")
    site_bad = make_site("乙", "s002_b")
    outcomes = {
        0: (site_ok, "03合 甲", "297期 … 开", None, ["03合"], None),
        1: (site_bad, None, "没找到297期", None, [], "无当期"),
    }
    success, fail, ranking, _cats = build_current_only_results(
        [site_ok, site_bad], outcomes, 297
    )
    assert success == ["03合 甲"]
    assert ranking == ["03合"]
    assert len(fail) == 1
