from pathlib import Path

from he_app.config.sites import load_sites
from he_app.domain.periods import CyclePolicy, PeriodContext, PeriodKey
from he_app.services.cycle_fingerprint import history_adapter_kind
from he_app.services.live_validation import LiveSiteResult, build_live_report


def test_every_configured_site_has_a_history_adapter() -> None:
    sites = load_sites(Path(__file__).resolve().parents[1] / "sites.json")
    assert sites
    adapters = {site.site_id: history_adapter_kind(site) for site in sites}
    assert len(adapters) == len(sites)
    assert all(value.strip() for value in adapters.values())


def test_live_report_counts_every_attempted_site() -> None:
    sites = load_sites(Path(__file__).resolve().parents[1] / "sites.json")[:2]
    context = PeriodContext(PeriodKey(2026, 252), CyclePolicy("year"), 10)
    results = [
        LiveSiteResult(
            index=index,
            site_id=site.site_id,
            name=site.name,
            url=site.url,
            pick=site.pick,
            browser=site.browser,
            attempted=True,
            current_ok=index == 0,
            current_value="01合" if index == 0 else None,
            history_count=10 if index == 0 else 0,
            history_required=10,
            history_complete=index == 0,
            fingerprint={"2026:252": "01合"} if index == 0 else {},
            adapter=history_adapter_kind(site),
            error_category=None if index == 0 else "执行失败",
            error=None if index == 0 else "external site unavailable",
            elapsed=0.1,
            source_urls=(),
            authority_ids=(),
            peer_evidence={},
        )
        for index, site in enumerate(sites)
    ]
    report = build_live_report(sites, context, results)
    assert report["site_count"] == 2
    assert report["attempted_count"] == 2
    assert report["current_success_count"] == 1
    assert report["history_complete_count"] == 1
