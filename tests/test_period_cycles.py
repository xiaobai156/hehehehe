from he_app.domain.periods import (
    CyclePolicy,
    PeriodContext,
    PeriodKey,
    deserialize_period_mapping,
    longest_equal_consecutive_run,
    serialize_period_mapping,
)


def test_year_window_crosses_001_to_previous_365() -> None:
    context = PeriodContext(PeriodKey(2026, 1), CyclePolicy("year"), 4)
    assert [key.token() for key in context.window] == [
        "2026:001",
        "2025:365",
        "2025:364",
        "2025:363",
    ]


def test_leap_year_previous_cycle_uses_366() -> None:
    context = PeriodContext(PeriodKey(2025, 1), CyclePolicy("year"), 3)
    assert [key.token() for key in context.window] == [
        "2025:001",
        "2024:366",
        "2024:365",
    ]


def test_fixed_cycle_does_not_assume_365() -> None:
    context = PeriodContext(PeriodKey(8, 1), CyclePolicy("fixed", 49), 3)
    assert [key.token() for key in context.window] == ["8:001", "7:049", "7:048"]


def test_cross_cycle_equal_run_is_contiguous() -> None:
    context = PeriodContext(PeriodKey(2026, 2), CyclePolicy("year"), 5)
    left = {
        PeriodKey(2026, 2): "01合",
        PeriodKey(2026, 1): "02合",
        PeriodKey(2025, 365): "03合",
        PeriodKey(2025, 364): "04合",
    }
    right = dict(left)
    periods, values = longest_equal_consecutive_run(left, right, context)
    assert [key.token() for key in periods] == [
        "2026:002",
        "2026:001",
        "2025:365",
        "2025:364",
    ]
    assert values == ("01合", "02合", "03合", "04合")


def test_serialized_keys_preserve_cycle_identity() -> None:
    context = PeriodContext(PeriodKey(2026, 1), CyclePolicy("year"), 3)
    original = {
        PeriodKey(2026, 1): "01合",
        PeriodKey(2025, 365): "02合",
    }
    encoded = serialize_period_mapping(original, context=context)
    assert encoded == {"2026:001": "01合", "2025:365": "02合"}
    assert deserialize_period_mapping(encoded, context=context) == original


def test_legacy_numeric_keys_map_to_the_requested_window() -> None:
    context = PeriodContext(PeriodKey(2026, 1), CyclePolicy("year"), 3)
    decoded = deserialize_period_mapping(
        {"1": "01合", "365": "02合"},
        context=context,
    )
    assert decoded == {
        PeriodKey(2026, 1): "01合",
        PeriodKey(2025, 365): "02合",
    }
