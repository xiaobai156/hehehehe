from datetime import datetime, timezone

from he_app.domain.models import Site
from he_app.domain.periods import PeriodKey
from he_app.services.isolation import IsolatedJobResult
from he_app.services.live_audit import (
    build_live_report,
    build_text_summary,
    consecutive_history_count,
)


def _site(index: int) -> Site:
    return Site(
        f"站点{index}",
        f"https://example{index}.test/path",
        "top",
        False,
        False,
        f"s{index}",
    )


def test_consecutive_history_crosses_cycle_boundary() -> None:
    base = PeriodKey(2027, 2)
    values = {
        PeriodKey(2027, 2): "01合",
        PeriodKey(2027, 1): "02合",
        PeriodKey(2026, 365): "03合",
        PeriodKey(2026, 364): "04合",
    }
    assert consecutive_history_count(values, base) == 4


def test_live_report_distinguishes_verified_incomplete_failed_and_timeout() -> None:
    sites = [_site(index) for index in range(4)]
    base = PeriodKey(2026, 252)
    complete = {base.previous(offset): "01合" for offset in range(6)}
    incomplete = {base.previous(offset): "02合" for offset in range(2)}
    diagnostics = {
        0: IsolatedJobResult(0, sites[0], None, None, 1.0),
        1: IsolatedJobResult(1, sites[1], None, None, 2.0),
        2: IsolatedJobResult(2, sites[2], None, "bad", 3.0),
        3: IsolatedJobResult(3, sites[3], None, "timeout", 4.0, True),
    }
    now = datetime(2026, 9, 9, tzinfo=timezone.utc)
    report = build_live_report(
        sites,
        {0: complete, 1: incomplete},
        {2: "bad", 3: "timeout"},
        diagnostics,
        base,
        6,
        source_sha256="abc",
        started_at=now,
        finished_at=now,
        source_commit="deadbeef",
    )
    assert [row["status"] for row in report["results"]] == [
        "verified",
        "history_incomplete",
        "failed",
        "timeout",
    ]
    assert report["all_attempted"] is True
    assert report["current_verified_count"] == 2
    assert report["history_complete_count"] == 1
    assert "deadbeef" in build_text_summary(report)
