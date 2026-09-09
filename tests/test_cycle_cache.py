import json
from pathlib import Path

from he_app.domain.models import Site
from he_app.domain.periods import CyclePolicy, PeriodContext, PeriodKey
from he_app.storage.cycle_cache import (
    CycleCacheState,
    load_cycle_cache,
    merge_cycle_fingerprints,
    write_cycle_cache,
)


def _site() -> Site:
    return Site(
        "测试站",
        "https://example.test/topic/1.html",
        "top",
        False,
        False,
        "site-1",
        1,
    )


def test_legacy_cache_migrates_001_and_previous_365(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy.json"
    cycle = tmp_path / "cycle.json"
    site = _site()
    legacy.write_text(
        json.dumps(
            {
                "base_period": 1,
                "periods": 10,
                "updated_at": "2026-01-01 00:10:00",
                "sites": [
                    {
                        "id": site.site_id,
                        "name": site.name,
                        "url": site.url,
                        "pick": site.pick,
                        "browser": False,
                        "click_first": False,
                        "fingerprint": {"1": "01合", "365": "02合"},
                    }
                ],
                "errors": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    context = PeriodContext(PeriodKey(2026, 1), CyclePolicy("year"), 10)
    state = load_cycle_cache(
        cycle,
        context=context,
        sites=[site],
        legacy_path=legacy,
    )
    assert state.fingerprints[0] == {
        PeriodKey(2026, 1): "01合",
        PeriodKey(2025, 365): "02合",
    }


def test_failed_current_is_removed_but_previous_cycle_history_remains() -> None:
    site = _site()
    context = PeriodContext(PeriodKey(2026, 1), CyclePolicy("year"), 10)
    state = CycleCacheState(
        context,
        [site],
        {
            0: {
                PeriodKey(2026, 1): "01合",
                PeriodKey(2025, 365): "02合",
            }
        },
        {},
    )
    fingerprints, errors = merge_cycle_fingerprints(
        state,
        fresh={},
        errors={0: "timeout"},
    )
    assert fingerprints[0] == {PeriodKey(2025, 365): "02合"}
    assert errors == {0: "timeout"}


def test_cycle_cache_round_trip_preserves_explicit_keys(tmp_path: Path) -> None:
    target = tmp_path / "cycle.json"
    site = _site()
    context = PeriodContext(PeriodKey(2026, 1), CyclePolicy("year"), 10)
    fingerprints = {
        0: {
            PeriodKey(2026, 1): "01合",
            PeriodKey(2025, 365): "02合",
        }
    }
    state = CycleCacheState(context, [site], fingerprints, {})
    write_cycle_cache(target, state, fingerprints=fingerprints, errors={})
    reloaded = load_cycle_cache(target, context=context, sites=[site])
    assert reloaded.fingerprints == fingerprints
