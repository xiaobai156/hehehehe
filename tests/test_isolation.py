import time

from he_app.domain.models import Site
from he_app.services.isolation import run_isolated_site_jobs


def _site(site_id: str) -> Site:
    return Site(
        site_id,
        f"https://{site_id}.example.test/path",
        "top",
        False,
        False,
        site_id,
    )


def _recovering_worker(index, site, browser, host_locks):
    del index, browser, host_locks
    if site.site_id == "slow":
        time.sleep(30)
    return site.site_id


def test_hard_timeout_kills_stuck_process_and_slot_recovers():
    slow = _site("slow")
    fast = _site("fast")
    started = time.monotonic()
    results = run_isolated_site_jobs(
        [(0, slow), (1, fast)],
        workers=1,
        worker_callable=_recovering_worker,
        needs_browser=lambda _site: False,
        hard_timeout=1.5,
        poll_interval=0.02,
    )
    elapsed = time.monotonic() - started
    assert results[0].timed_out is True
    assert "已终止进程树" in (results[0].error or "")
    assert results[1].error is None and results[1].value == "fast"
    assert elapsed < 12


def test_duplicate_indexes_are_rejected_before_process_start():
    target = _site("same")
    try:
        run_isolated_site_jobs(
            [(1, target), (1, target)],
            workers=1,
            worker_callable=_recovering_worker,
            needs_browser=lambda _site: False,
            hard_timeout=1,
        )
    except ValueError as exc:
        assert "重复站点索引" in str(exc)
    else:
        raise AssertionError("duplicate indexes were accepted")
