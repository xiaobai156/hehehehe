# -*- coding: utf-8 -*-
"""逐站写入回归测试（用户 2026-10-20 指令）。

验证三件事：
1. 缓存缺站点 / 站点身份变化时，不再整批拒绝写入；
2. 单站结果异常只降级该站为失败，其余成功站点照常写入；
3. 成功率低于 85% 时成功站点依然写入缓存。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from he_app.domain.models import Site
from he_app.storage.recent_cache import (
    build_recent_cache_payload,
    update_recent_cache_from_outcomes,
    validate_recent_cache_identity,
)


def make_site(name: str, url: str, site_id: str, pick: str = "top") -> Site:
    return Site(name, url, pick, False, False, site_id, 1)


def base_cache(sites: list[Site], period: int = 291) -> dict:
    return {
        "base_period": period,
        "periods": 10,
        "updated_at": "2026-10-19 18:00:00",
        "sites": [
            {
                "id": site.site_id,
                "name": site.name,
                "url": site.url,
                "pick": site.pick,
                "browser": site.browser,
                "click_first": site.click_first,
                "fingerprint": {},
                "updated_at": "2026-10-19 18:00:00",
            }
            for site in sites
        ],
        "errors": [],
        "schema_version": 2,
        "cycle_year": 2026,
        "base_period_key": f"2026-{period}",
    }


def outcomes_for(sites: list[Site], successes: dict[int, str], failures: set[int]):
    outcomes = {}
    for index, site in enumerate(sites):
        if index in successes:
            value = successes[index]
            outcomes[index] = (site, f"{value} {site.name}", "", None, [value], None)
        elif index in failures:
            outcomes[index] = (site, None, "模拟失败", "TimeoutError: 模拟", [], None)
        else:
            outcomes[index] = (site, None, "", "未执行", [], None)
    return outcomes


def test_missing_configured_site_no_longer_blocks_write():
    """配置有 2 站、缓存只有 1 站：应补建缺失站点并正常写入。"""
    a = make_site("甲", "https://a.example/topic/1.html", "s001_a")
    b = make_site("乙", "https://b.example/topic/2.html", "s002_b")
    cache = base_cache([a])  # 缺 b

    validate_recent_cache_identity(cache, [a, b], cycle_year=2026)

    payload = json.loads(
        build_recent_cache_payload(
            cache, [a, b], outcomes_for([a, b], {0: "05合", 1: "07合"}, set()), 292, 10, False,
            cycle_year=2026,
        )
    )
    ids = {item["id"] for item in payload["sites"]}
    assert ids == {"s001_a", "s002_b"}
    assert payload["base_period_key"] == "2026-292"
    by_id = {item["id"]: item for item in payload["sites"]}
    assert by_id["s001_a"]["fingerprint"]["2026-292"] == "05合"
    assert by_id["s002_b"]["fingerprint"]["2026-292"] == "07合"


def test_changed_site_identity_no_longer_blocks_write():
    """站点换了 URL（换址）：不应抛错，按新身份写入。"""
    old = make_site("甲", "https://old.example/topic/1.html", "s001_a")
    new = make_site("甲", "https://new.example/topic/1.html", "s001_a")
    cache = base_cache([old])

    validate_recent_cache_identity(cache, [new], cycle_year=2026)

    payload = json.loads(
        build_recent_cache_payload(
            cache, [new], outcomes_for([new], {0: "09合"}, set()), 292, 10, False, cycle_year=2026
        )
    )
    item = payload["sites"][0]
    assert item["url"] == "https://new.example/topic/1.html"
    assert item["fingerprint"]["2026-292"] == "09合"


def test_single_bad_result_does_not_abort_batch():
    """一个站数据格式坏掉：只该站记失败，其他成功站照写。"""
    good = make_site("甲", "https://a.example/topic/1.html", "s001_a")
    bad = make_site("乙", "https://b.example/topic/2.html", "s002_b")
    cache = base_cache([good, bad])

    outcomes = outcomes_for([good, bad], {0: "05合"}, set())
    # 乙 声称成功但给了两个值（非法）
    outcomes[1] = (bad, "01合,02合 乙", "", None, ["01合", "02合"], None)

    payload = json.loads(
        build_recent_cache_payload(cache, [good, bad], outcomes, 292, 10, False, cycle_year=2026)
    )
    by_id = {item["id"]: item for item in payload["sites"]}
    assert by_id["s001_a"]["fingerprint"]["2026-292"] == "05合"
    assert "2026-292" not in by_id["s002_b"]["fingerprint"]
    failed_ids = {item["id"] for item in payload["errors"]}
    assert failed_ids == {"s002_b"}


def test_low_success_rate_still_writes_successes(tmp_path: Path):
    """成功率只有 20%：成功站照样写入缓存，不再被 85% 门禁拦下。"""
    sites = [
        make_site(f"站{i}", f"https://s{i}.example/topic/{i}.html", f"s00{i}_x")
        for i in range(5)
    ]
    cache_path = tmp_path / "recent_10_cache.json"
    cache_path.write_text(
        json.dumps(base_cache(sites), ensure_ascii=False, indent=2), encoding="utf-8"
    )

    update_recent_cache_from_outcomes(
        cache_path,
        sites,
        outcomes_for(sites, {0: "03合"}, {1, 2, 3, 4}),
        292,
        10,
        False,
        cycle_year=2026,
    )

    payload = json.loads(cache_path.read_text(encoding="utf-8-sig"))
    by_id = {item["id"]: item for item in payload["sites"]}
    assert by_id["s000_x"]["fingerprint"]["2026-292"] == "03合"
    for index in (1, 2, 3, 4):
        assert "2026-292" not in by_id[f"s00{index}_x"]["fingerprint"]
    assert payload["base_period_key"] == "2026-292"
    assert len(payload["errors"]) == 4


def test_structural_corruption_still_fails():
    """重复站点 ID 属于真损坏，仍必须报错。"""
    a = make_site("甲", "https://a.example/topic/1.html", "s001_a")
    cache = base_cache([a, a])
    with pytest.raises(Exception):
        validate_recent_cache_identity(cache, [a], cycle_year=2026)
