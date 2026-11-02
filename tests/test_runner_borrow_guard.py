# -*- coding: utf-8 -*-
"""回归测试：runner 层的跨期借值防护（计数、缓存分流、失败清单合并）。

审查指出的缺口：原测试只覆盖解析层与 reports 层，没有覆盖
- success_count 是否按降级后统计；
- _split_cache_outcomes_by_matched_period 是否拒绝把异期值写进当期缓存；
- merge_failure_output 下借值站是否保留在失败清单。
"""
from __future__ import annotations

from he_app.domain.models import Site
from he_app.services.runner import _split_cache_outcomes_by_matched_period
from he_app.storage.reports import (
    build_current_only_results,
    format_failure_output,
    merge_failure_output,
)
from he_app.validation.period import MATCHED_PERIOD_PREFIX


def make_site(name: str, site_id: str) -> Site:
    return Site(name, f"https://{site_id}.example/topic/1.html", "bottom", False, False, site_id, 1)


def marker(period: int) -> str:
    return f"{MATCHED_PERIOD_PREFIX}{period}"


def test_cache_split_rejects_next_period_from_requested():
    """命中下一期：当期缓存必须是 miss，值只归档到下一期。"""
    site = make_site("展翅飞翔", "s149_zhanchi")
    outcomes = {
        0: (site, "06合 展翅飞翔", "298期 … 开", None, ["06合"], marker(298)),
    }
    requested, next_sites, next_outcomes, next_period = _split_cache_outcomes_by_matched_period(
        [site], outcomes, 297
    )
    assert next_period == 298
    assert requested[0][1] is None, "当期缓存不得写入下一期的值"
    assert requested[0][4] == []
    assert len(next_sites) == 1
    assert next_outcomes[0][1] == "06合 展翅飞翔"


def test_cache_split_rejects_far_period_entirely():
    """命中 period+2：既不能写当期缓存，也不进下一期归档（防止异期值乱写）。"""
    site = make_site("某站", "s001_x")
    outcomes = {
        0: (site, "07合 某站", "300期 … 开", None, ["07合"], marker(300)),
    }
    requested, next_sites, next_outcomes, _next_period = _split_cache_outcomes_by_matched_period(
        [site], outcomes, 297
    )
    assert requested[0][1] is None, "period+2 不得按当期写缓存"
    assert requested[0][4] == []
    assert next_sites == [], "period+2 不应被当成下一期归档"
    assert next_outcomes == {}


def test_cache_split_keeps_same_period_success():
    site = make_site("某站", "s002_y")
    outcomes = {
        0: (site, "03合 某站", "297期 … 开", None, ["03合"], None),
    }
    requested, next_sites, _next_outcomes, _p = _split_cache_outcomes_by_matched_period(
        [site], outcomes, 297
    )
    assert requested[0][1] == "03合 某站"
    assert next_sites == []


def test_failure_list_keeps_borrowed_site_and_drops_real_success():
    ok = make_site("真成功", "s010_ok")
    borrowed = make_site("借值站", "s011_borrow")
    outcomes = {
        0: (ok, "03合 真成功", "297期 … 开", None, ["03合"], None),
        1: (borrowed, "06合 借值站", "298期 … 开", None, ["06合"], marker(298)),
    }
    success, fail, _ranking, categories = build_current_only_results(
        [ok, borrowed], outcomes, 297
    )
    assert success == ["03合 真成功"]
    assert len(fail) == 1 and "借值站" in fail[0]

    existing = "\n\n".join(
        [
            "失败 借值站 https://s011_borrow.example/topic/1.html 站点ID: s011_borrow "
            "方向: bottom 期数: 297 阶段: 指定期数校验 失败类型: 无当期 具体原因: 旧记录",
        ]
    )
    merged = merge_failure_output(existing, fail, {"s010_ok"})
    assert "借值站" in merged, "借值站必须保留在失败清单"
    assert "真成功" not in merged


def test_failure_stats_contain_borrow_category():
    site = make_site("借值站", "s011_borrow")
    outcomes = {0: (site, "06合 借值站", "298期 … 开", None, ["06合"], marker(298))}
    _s, fail, _r, categories = build_current_only_results([site], outcomes, 297)
    text = format_failure_output(fail, categories)
    assert "禁止跨期借值" in text
    assert "失败分类统计" in text
