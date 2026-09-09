import time

from he_app.runtime.process_jobs import run_isolated_jobs


def test_hard_timeout_starts_before_worker_completion() -> None:
    started = time.monotonic()
    results = run_isolated_jobs(
        [("slow", 5.0)],
        worker_path="time:sleep",
        max_workers=1,
        hard_timeout=0.25,
        poll_interval=0.02,
    )
    elapsed = time.monotonic() - started
    assert len(results) == 1
    assert not results[0].ok
    assert results[0].timed_out
    assert "hard deadline" in (results[0].error or "")
    assert elapsed < 3.0


def test_successful_isolated_job_returns_value() -> None:
    results = run_isolated_jobs(
        [("value", -7)],
        worker_path="operator:neg",
        max_workers=1,
        hard_timeout=2.0,
    )
    assert len(results) == 1
    assert results[0].ok
    assert results[0].value == 7
