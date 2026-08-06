from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from he_app.domain.models import Site
from he_app.services import runner
from he_app.storage.recent_cache import cache_update_allowed


def _site(index: int) -> Site:
    return Site(
        f"站点{index}",
        f"https://example.test/{index}",
        "top",
        False,
        False,
        f"threshold-{index}",
    )


def _args(tmp_path: Path, cache_path: Path, success_path: Path, fail_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        period=211,
        success=str(success_path),
        fail=str(fail_path),
        timeout=1,
        workers=1,
        retries=0,
        delay=0.0,
        show_browser=False,
        cache="",
        mirror_limit=0,
        sites=str(tmp_path / "sites.json"),
        cache_max_age_hours=24.0,
        fingerprint_cache=str(cache_path),
        no_fingerprint_cache_sync=False,
        preserve_unconfigured_cache_sites=False,
    )


def _write_cache(path: Path, sites: list[Site]) -> None:
    path.write_text(
        json.dumps(
            {
                "base_period": 211,
                "periods": 10,
                "updated_at": "old",
                "sites": [
                    {
                        "id": site.site_id,
                        "name": site.name,
                        "url": site.url,
                        "pick": site.pick,
                        "browser": False,
                        "click_first": False,
                        "fingerprint": {"210": "02合", "211": "03合"},
                    }
                    for site in sites
                ],
                "errors": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def test_cache_update_threshold_is_strictly_more_than_85_percent() -> None:
    assert cache_update_allowed(0, 0) is False
    assert cache_update_allowed(85, 100) is False
    assert cache_update_allowed(86, 100) is True
    assert cache_update_allowed(17, 20) is False
    assert cache_update_allowed(18, 20) is True


def test_runner_skips_success_fingerprint_update_but_marks_failures_at_or_below_threshold(
    tmp_path: Path, monkeypatch
) -> None:
    sites = [_site(1), _site(2)]
    cache_path = tmp_path / "recent.json"
    success_path = tmp_path / "success.txt"
    fail_path = tmp_path / "fail.txt"
    _write_cache(cache_path, sites)
    monkeypatch.setattr(runner, "load_sites", lambda _path: sites)

    def scrape(index, current_site, *_args, **_kwargs):
        if index == 0:
            return index, current_site, "04合 站点1", "211期 绝杀一合 [04合] 开", None, ["04合"], None
        return index, current_site, None, "211期抓取失败", None, [], "请求失败"

    monkeypatch.setattr(runner, "scrape_parallel_site", scrape)
    args = _args(tmp_path, cache_path, success_path, fail_path)

    assert runner.run(args) == 0

    payload = json.loads(cache_path.read_text(encoding="utf-8"))
    assert payload["base_period"] == 211
    assert payload["sites"][0]["fingerprint"] == {"210": "02合", "211": "03合"}
    assert payload["sites"][1]["fingerprint"] == {"210": "02合"}
    assert payload["errors"] == [
        {
            "id": "threshold-2",
            "name": "站点2",
            "url": "https://example.test/2",
            "period": 211,
            "status": "失败",
            "error": "211期抓取失败",
            "updated_at": payload["errors"][0]["updated_at"],
        }
    ]


def test_runner_updates_cache_and_marks_failed_sites_above_threshold(tmp_path: Path, monkeypatch) -> None:
    sites = [_site(index) for index in range(1, 8)]
    cache_path = tmp_path / "recent.json"
    success_path = tmp_path / "success.txt"
    fail_path = tmp_path / "fail.txt"
    monkeypatch.setattr(runner, "load_sites", lambda _path: sites)

    def scrape(index, current_site, *_args, **_kwargs):
        if index == 0:
            return index, current_site, None, "211期抓取失败", None, [], "请求失败"
        value = f"{index:02d}合"
        return index, current_site, f"{value} {current_site.name}", f"211期 [绝杀一合] [{value}] 开", None, [value], None

    monkeypatch.setattr(runner, "scrape_parallel_site", scrape)
    args = _args(tmp_path, cache_path, success_path, fail_path)

    assert runner.run(args) == 0

    payload = json.loads(cache_path.read_text(encoding="utf-8"))
    assert payload["base_period"] == 211
    assert len(payload["sites"]) == 6
    assert all(item["fingerprint"] == {"211": f"{index:02d}合"} for index, item in enumerate(payload["sites"], 1))
    assert payload["errors"][0]["id"] == "threshold-1"
    assert payload["errors"][0]["status"] == "失败"
    assert payload["errors"][0]["period"] == 211
