import json

from he_app.domain.models import Site
from he_app.services import crawler
from he_app.services.runner import _split_cache_outcomes_by_matched_period
from he_app.storage.recent_cache import load_recent_cache, update_recent_cache_from_outcomes


def _site(pick: str, site_id: str = "s001_topic_206535", name: str = "测试站") -> Site:
    return Site(name, "https://example.test/topic/1.html", pick, False, False, site_id)


def _rows(items: list[tuple[int, int]]) -> str:
    return "作者:测试站\n" + "\n".join(
        f"{period}期 绝杀一合 [{value:02d}合] 开00准"
        for period, value in items
    )


def _run(monkeypatch, pick: str, items: list[tuple[int, int]]):
    site = _site(pick)
    document = _rows(items)
    monkeypatch.setattr(crawler, "collect_special_documents", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(crawler, "collect_http_documents", lambda *_args, **_kwargs: [document])
    return crawler.scrape_site(object(), site, 252, 1)


def test_top_accepts_253_only_when_253_is_physical_top(monkeypatch) -> None:
    result, detail, values, metadata = _run(
        monkeypatch,
        "top",
        [(253, 3), (252, 2), (251, 1)],
    )

    assert result == "03合 测试站"
    assert "253期" in detail
    assert values == ["03合"]
    assert crawler.matched_period_from_reason(252, metadata) == 253


def test_bottom_accepts_253_only_when_253_is_physical_bottom(monkeypatch) -> None:
    result, detail, values, metadata = _run(
        monkeypatch,
        "bottom",
        [(251, 1), (252, 2), (253, 3)],
    )

    assert result == "03合 测试站"
    assert "253期" in detail
    assert values == ["03合"]
    assert crawler.matched_period_from_reason(252, metadata) == 253


def test_top_never_skips_254_boundary_to_rescue_interior_253(monkeypatch) -> None:
    result, _detail, values, metadata = _run(
        monkeypatch,
        "top",
        [(254, 4), (253, 3), (252, 2)],
    )

    assert result is None
    assert values == []
    assert crawler.matched_period_from_reason(252, metadata) == 252


def test_bottom_never_skips_254_boundary_to_rescue_interior_253(monkeypatch) -> None:
    result, _detail, values, metadata = _run(
        monkeypatch,
        "bottom",
        [(252, 2), (253, 3), (254, 4)],
    )

    assert result is None
    assert values == []
    assert crawler.matched_period_from_reason(252, metadata) == 252


def test_requested_252_edge_always_wins_before_253_fallback(monkeypatch) -> None:
    result, detail, values, metadata = _run(
        monkeypatch,
        "top",
        [(252, 2), (253, 3), (251, 1)],
    )

    assert result == "02合 测试站"
    assert "252期" in detail
    assert values == ["02合"]
    assert metadata is None


def test_cache_split_never_writes_253_value_under_252(tmp_path) -> None:
    first = _site("top", "site-a", "甲站")
    second = _site("bottom", "site-b", "乙站")
    sites = [first, second]
    outcomes = {
        0: (first, "02合 甲站", "252期 绝杀一合 [02合] 开", None, ["02合"], None),
        1: (
            second,
            "03合 乙站",
            "253期 绝杀一合 [03合] 开",
            None,
            ["03合"],
            f"{crawler.MATCHED_PERIOD_PREFIX}253",
        ),
    }

    requested, next_sites, next_outcomes, next_period = (
        _split_cache_outcomes_by_matched_period(sites, outcomes, 252)
    )
    assert requested[1][1] is None
    assert next_period == 253
    assert next_sites == [second]
    assert next_outcomes[0][1] == "03合 乙站"

    path = tmp_path / "cache.json"
    update_recent_cache_from_outcomes(
        path,
        sites,
        requested,
        252,
        10,
        False,
        cycle_year=2026,
    )
    update_recent_cache_from_outcomes(
        path,
        next_sites,
        next_outcomes,
        253,
        10,
        True,
        cycle_year=2026,
    )

    cache = load_recent_cache(path)
    by_id = {item["id"]: item for item in cache["sites"]}
    assert cache["base_period_key"] == "2026-253"
    assert by_id["site-a"]["fingerprint"]["2026-252"] == "02合"
    assert "2026-253" not in by_id["site-a"]["fingerprint"]
    assert by_id["site-b"]["fingerprint"]["2026-253"] == "03合"
    assert "2026-252" not in by_id["site-b"]["fingerprint"]

    # The serialized cache itself must never contain a mislabeled site-b 252 value.
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw_by_id = {item["id"]: item for item in raw["sites"]}
    assert raw_by_id["site-b"]["fingerprint"] == {"2026-253": "03合"}
