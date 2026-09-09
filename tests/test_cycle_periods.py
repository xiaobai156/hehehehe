from datetime import datetime, timezone

import pytest

from he_app.domain.periods import (
    PeriodKey,
    current_tokyo_period,
    issue_map_for_window,
    issues_in_cycle,
    period_window,
    periods_are_consecutive_descending,
)


def test_period_key_crosses_calendar_cycle_without_collision():
    end = PeriodKey(2026, 365)
    start = end.next()
    assert start == PeriodKey(2027, 1)
    assert start.previous() == end
    assert end.cache_key == "2026-365"
    assert start.cache_key == "2027-001"
    assert periods_are_consecutive_descending(start, end)


def test_leap_cycle_has_366_and_crosses_cleanly():
    assert issues_in_cycle(2028) == 366
    assert PeriodKey(2028, 366).next() == PeriodKey(2029, 1)
    with pytest.raises(ValueError):
        PeriodKey(2027, 366)


def test_cross_cycle_window_maps_bare_page_numbers_to_years():
    base = PeriodKey(2027, 3)
    assert period_window(base, 6) == (
        PeriodKey(2027, 3),
        PeriodKey(2027, 2),
        PeriodKey(2027, 1),
        PeriodKey(2026, 365),
        PeriodKey(2026, 364),
        PeriodKey(2026, 363),
    )
    mapping = issue_map_for_window(base, 6)
    assert mapping[1] == PeriodKey(2027, 1)
    assert mapping[365] == PeriodKey(2026, 365)


def test_period_tokens_are_explicit_and_validated():
    assert PeriodKey.parse("2027-001") == PeriodKey(2027, 1)
    assert PeriodKey.parse("1", 2027) == PeriodKey(2027, 1)
    with pytest.raises(ValueError):
        PeriodKey.parse("next")


def test_current_tokyo_period_uses_tokyo_date():
    instant = datetime(2026, 12, 31, 15, 30, tzinfo=timezone.utc)
    assert current_tokyo_period(instant) == PeriodKey(2027, 1)


def _cache_site():
    from he_app.domain.models import Site

    return Site(
        "测试站",
        "https://example.test/topic/1.html",
        "top",
        False,
        False,
        "audit",
    )


def _cache_item(site, fingerprint):
    return {
        "id": site.site_id,
        "name": site.name,
        "url": site.url,
        "pick": site.pick,
        "browser": site.browser,
        "click_first": site.click_first,
        "fingerprint": fingerprint,
    }


def test_stale_v1_cache_migrates_before_new_window_trimming(tmp_path):
    import json

    from he_app.storage.recent_cache import update_recent_cache_from_outcomes

    site = _cache_site()
    path = tmp_path / "recent.json"
    path.write_text(
        json.dumps(
            {
                "base_period": 218,
                "periods": 10,
                "updated_at": "2026-08-06 00:54:00",
                "sites": [_cache_item(site, {"218": "01合", "217": "02合"})],
                "errors": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    update_recent_cache_from_outcomes(
        path,
        [site],
        {0: (site, "03合 测试站", "252期绝杀一合[03合]", None, ["03合"], None)},
        252,
        cycle_year=2026,
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 2
    assert payload["base_period_key"] == "2026-252"
    assert payload["sites"][0]["fingerprint"] == {"2026-252": "03合"}


def test_cycle_cache_update_retains_previous_year_history(tmp_path):
    import json

    from he_app.storage.recent_cache import update_recent_cache_from_outcomes

    site = _cache_site()
    path = tmp_path / "recent.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "cycle_year": 2026,
                "base_period": 365,
                "base_period_key": "2026-365",
                "periods": 10,
                "updated_at": "2026-12-31 23:00:00",
                "sites": [
                    _cache_item(
                        site,
                        {"2026-365": "01合", "2026-364": "02合"},
                    )
                ],
                "errors": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    update_recent_cache_from_outcomes(
        path,
        [site],
        {0: (site, "03合 测试站", "1期绝杀一合[03合]", None, ["03合"], None)},
        1,
        cycle_year=2027,
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["base_period_key"] == "2027-001"
    assert payload["sites"][0]["fingerprint"] == {
        "2027-001": "03合",
        "2026-365": "01合",
        "2026-364": "02合",
    }


def test_duplicate_run_crosses_cycle_boundary():
    from he_app.services.duplicate_check import longest_equal_consecutive_run

    keys = period_window(PeriodKey(2027, 2), 6)
    left = {key: f"{index + 1:02d}合" for index, key in enumerate(keys)}
    right = dict(left)
    periods, values = longest_equal_consecutive_run(left, right)
    assert periods == keys
    assert len(values) == 6


def test_generic_history_maps_001_and_365_to_distinct_cycles():
    from he_app.domain.models import Site
    from he_app.services.fingerprint import build_site_period_fingerprint

    site = Site(
        "测试站",
        "https://example.test/topic/1.html",
        "top",
        False,
        False,
        "unregistered-generic",
    )
    document = "作者:测试站\n" + "\n".join(
        [
            "3期 绝杀一合 [01合] 开00准",
            "2期 绝杀一合 [02合] 开00准",
            "1期 绝杀一合 [03合] 开00准",
            "365期 绝杀一合 [04合] 开00准",
            "364期 绝杀一合 [05合] 开00准",
            "363期 绝杀一合 [06合] 开00准",
        ]
    )
    fingerprint = build_site_period_fingerprint(
        site, [document], PeriodKey(2027, 3), 6
    )
    assert list(fingerprint) == list(period_window(PeriodKey(2027, 3), 6))
    assert fingerprint[PeriodKey(2027, 1)] == "03合"
    assert fingerprint[PeriodKey(2026, 365)] == "04合"
