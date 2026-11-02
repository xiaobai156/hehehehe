# -*- coding: utf-8 -*-
"""回归测试：禁止跨期借值（命中下一期不得进当期成功 TXT）。

背景（2026-10-25 代码审查确认）：he_app/services/crawler.py 的 scrape_site 在当期
严格解析失败后会以 period+1 再跑一次，命中即当作成功返回，仅在 metadata 里写
``__matched_period__:<期号>``。原 build_current_only_results 只看 result 是否为真，
导致下一期的值被写进当期成功 TXT 与排行榜（跨期借值）。

修复：build_current_only_results 识别该 metadata，把这类结果降级为失败。
"""
from __future__ import annotations

from he_app.domain.models import Site
from he_app.storage.reports import build_current_only_results


def make_site(name: str, site_id: str) -> Site:
    return Site(name, f"https://{site_id}.example/topic/1.html", "bottom", False, False, site_id, 1)


def test_next_period_hit_goes_to_failures_not_success():
    site = make_site("展翅飞翔", "s149_zhanchi")
    outcomes = {
        0: (site, "06合 展翅飞翔", "298期 … 绝杀一合 [06合] 开", None, ["06合"], "__matched_period__:298"),
    }
    success, fail, ranking, _categories = build_current_only_results([site], outcomes, 297)

    assert success == [], f"下一期结果不得进成功TXT，实际: {success}"
    assert ranking == [], f"下一期结果不得进排行榜，实际: {ranking}"
    assert len(fail) == 1
    assert "禁止跨期借值" in fail[0]
    assert "无当期" in fail[0]


def test_same_period_hit_still_succeeds():
    site = make_site("展翅飞翔", "s149_zhanchi")
    outcomes = {
        0: (site, "06合 展翅飞翔", "297期 … 绝杀一合 [06合] 开", None, ["06合"], None),
    }
    success, fail, ranking, _categories = build_current_only_results([site], outcomes, 297)

    assert success == ["06合 展翅飞翔"]
    assert ranking == ["06合"]
    assert fail == []


def test_matched_period_marker_for_same_period_is_not_downgraded():
    """marker 期号等于指定期时属于正常命中，不应降级。"""
    site = make_site("某站", "s001_x")
    outcomes = {
        0: (site, "03合 某站", "297期 … 开", None, ["03合"], "__matched_period__:297"),
    }
    success, _fail, _ranking, _categories = build_current_only_results([site], outcomes, 297)
    assert success == ["03合 某站"]


def test_normal_failure_unaffected():
    site = make_site("某站", "s002_y")
    outcomes = {
        0: (site, None, "候选数据靠后位置里没找到297期", None, [], "无当期"),
    }
    success, fail, ranking, _categories = build_current_only_results([site], outcomes, 297)
    assert success == []
    assert ranking == []
    assert len(fail) == 1
    assert "没找到297期" in fail[0]
