# -*- coding: utf-8 -*-
"""回归测试：候选行尾部粘连合数对照表 / 标签残渣时的边界判定。

背景（2026-10-24，展翅飞翔 s149_zhanchi 297 期）：
页面在当期杀合行后面紧跟一张合数对照表（01合：01 10、02合：02 11 20 …），
同时上一期文本尾部粘着 `< li class='X2026297'>` 标签残渣。原逻辑会：
  1) 把对照表吞进当期候选行；
  2) 让上一期出现一条“干净副本”并被追加到候选末尾；
结果 bottom 方向的最后一条候选变成上一期，当期被判「超出范围」。

修复：`strip_count_reference_table` 截掉尾部对照表；
`is_contained_duplicate_candidate` 增加反向包含去重。
"""
from __future__ import annotations

from he_app.parsers.common import (
    build_latest_candidate_parts,
    current_candidates_outside_window,
    is_contained_duplicate_candidate,
    strip_count_reference_table,
)

COUNT_TABLE = (
    "<font color=\"#800000\">01合：01 10<br>"
    "02合：02 11 20<br>"
    "03合：03 12 21 30<br>"
    "04合：04 13 22 31 40<br>"
    "05合：05 14 23 32 41<br>"
    "06合：06 15 24 33 42<br>"
    "07合：07 16 25 34 43<br>"
    "08合：08 17 26 35 44<br>"
    "09合：09 18 27 36 45<br>"
    "10合：19 28 37 46<br>"
    "11合：29 38 47<br>"
    "12合：39 48<br>"
    "13合：49</font>"
)


def build_document() -> str:
    rows = "\n".join(
        f"<li class='X2026{issue}'>{issue}期：❀绝杀一合❀ ⇨【{value}】开:猴23中</li>"
        for issue, value in ((294, "08"), (295, "02"), (296, "03"))
    )
    current = (
        "<li class='X2026297'>297期：❀绝杀一合❀ ⇨【06合】开:發00中</li>"
        f"<br>{COUNT_TABLE}"
    )
    return f"<div>绝杀一合</div>{rows}\n{current}"


def test_strip_count_reference_table_truncates_tail():
    line = "297期: ❀绝杀一合❀ ⇨[06合] 开:發00中 " + COUNT_TABLE
    cleaned = strip_count_reference_table(line)
    assert "01合：01" not in cleaned
    assert "13合：49" not in cleaned
    assert "297期" in cleaned
    assert "06合" in cleaned


def test_strip_keeps_normal_candidate_untouched():
    line = "297期: ❀绝杀一合❀ ⇨[06合] 开:發00中"
    assert strip_count_reference_table(line) == line


def test_reverse_contained_duplicate_is_rejected():
    """先到的长行带标签残渣，后到的干净副本也要算重复。"""
    dirty = "296期: ❀绝杀一合❀ ⇨[03合] 开:猴23 中 < li class='X2026297'>"
    clean = "296期: ❀绝杀一合❀ ⇨[03合] 开:猴23 中"
    assert is_contained_duplicate_candidate(clean, ["03合"], [dirty])


def test_bottom_boundary_reaches_current_period():
    document = build_document()
    parts = build_latest_candidate_parts([document], False, value_count=1)
    assert parts, "候选不应为空"
    assert parts[-1][0].startswith("297期"), f"最后一条候选应为297期，实际: {parts[-1][0][:60]}"
    # 修复前这里是 True（判超出范围），修复后应为 False。
    assert current_candidates_outside_window(
        [document], 297, "bottom", True, False, 1
    ) is False
