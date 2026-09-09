from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from he_app.config.sites import load_sites
from he_app.domain.periods import CyclePolicy, PeriodContext, PeriodKey
from he_app.fetch.browser import BrowserClient
from he_app.fetch.http import create_session
from he_app.fetch.network_policy import resolve_browser_pin, resolve_public_target
from he_app.runtime.process_jobs import IsolatedJobResult, run_isolated_jobs
from he_app.services import live_validation_adaptive as base
from he_app.services.adaptive_fetch import collect_http_documents, collect_special_documents
from he_app.services.cycle_fingerprint import build_cycle_site_fingerprint
from he_app.services.document_sources import requires_browser
from he_app.services.live_validation import LiveSiteResult


def validate_live_site(payload):
    started = time.monotonic()
    index = int(payload["index"])
    site = base._site_from_payload(dict(payload["site"]))
    policy = CyclePolicy(
        mode=str(payload["cycle_mode"]),
        fixed_length=(None if payload.get("cycle_length") in (None, "") else int(payload["cycle_length"])),
    )
    context = PeriodContext(
        PeriodKey(int(payload["cycle"]), int(payload["period"])),
        policy,
        int(payload["periods"]),
    )
    timeout = int(payload["timeout"])
    browser = None
    peer_evidence = {}
    documents = []
    try:
        target = resolve_public_target(site.url)
        peer_evidence["entry_dns"] = list(target.addresses)
        with create_session() as session:
            documents = collect_special_documents(
                session,
                site,
                timeout,
                context.current.number,
            ) or []
            if documents:
                result = build_cycle_site_fingerprint(site, documents, context)
                peer_evidence["fetch_path"] = "special-http"
            elif site.browser:
                probe_error = None
                try:
                    http_documents = collect_http_documents(session, site, min(timeout, 8))
                    http_result = build_cycle_site_fingerprint(site, http_documents, context)
                except Exception as exc:
                    probe_error = f"{type(exc).__name__}: {exc}"
                    http_documents = []
                    http_result = None
                if http_result is not None:
                    documents = http_documents
                    result = http_result
                    peer_evidence["fetch_path"] = "http-first"
                else:
                    if probe_error:
                        peer_evidence["http_probe_error"] = probe_error[:500]
                    if not requires_browser(site):
                        raise RuntimeError("special HTTP collector returned no validated document")
                    pinned_host, pinned_ip = resolve_browser_pin(site.url)
                    os.environ["HE_BROWSER_PINNED_HOST"] = pinned_host
                    os.environ["HE_BROWSER_PINNED_IP"] = pinned_ip
                    peer_evidence["browser_pin"] = {"host": pinned_host, "ip": pinned_ip}
                    browser = BrowserClient(headless=True)
                    browser.start()
                    documents = browser.get_documents(
                        site.url,
                        context.current.number,
                        site.click_first,
                        timeout,
                    )
                    result = build_cycle_site_fingerprint(site, documents, context)
                    peer_evidence["fetch_path"] = "browser-fallback"
            else:
                documents = collect_http_documents(session, site, timeout)
                result = build_cycle_site_fingerprint(site, documents, context)
                peer_evidence["fetch_path"] = "http"

            session_evidence = getattr(session, "last_network_evidence", None)
            if isinstance(session_evidence, dict):
                peer_evidence["last_http"] = dict(session_evidence)
        return base._success_payload(
            index,
            site,
            context,
            result,
            documents,
            started,
            peer_evidence,
        )
    finally:
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass
        os.environ.pop("HE_BROWSER_PINNED_HOST", None)
        os.environ.pop("HE_BROWSER_PINNED_IP", None)


def run_live_validation(sites, context, *, workers, timeout, hard_timeout, progress=True):
    jobs = [
        ((index, site.site_id), base._site_payload(index, site, context, timeout))
        for index, site in enumerate(sites)
    ]
    site_by_key = {(index, site.site_id): site for index, site in enumerate(sites)}

    def started(key):
        if progress:
            site = site_by_key[key]
            print(f"[LIVE START] {key[0] + 1}/{len(sites)} {site.site_id} {site.name}", flush=True)

    def finished(item):
        if progress:
            site = site_by_key[item.key]
            status = "OK" if item.ok else "FAIL"
            suffix = f" {item.error.splitlines()[0]}" if item.error else ""
            print(f"[LIVE {status}] {site.site_id} {site.name} {item.elapsed:.2f}s{suffix}", flush=True)

    isolated_results = run_isolated_jobs(
        jobs,
        worker_path="he_app.services.live_validation_adaptive_v2:validate_live_site",
        max_workers=max(1, workers),
        hard_timeout=hard_timeout,
        poll_interval=0.1,
        on_started=started,
        on_finished=finished,
    )
    by_key = {item.key: item for item in isolated_results}
    results = []
    for key, _payload in jobs:
        site = site_by_key[key]
        isolated = by_key.get(key) or IsolatedJobResult(
            key=key,
            ok=False,
            error="RuntimeError: validation scheduler lost the site result",
        )
        if not isolated.ok or not isinstance(isolated.value, dict):
            results.append(base._failed_result(key, site, context, isolated))
            continue
        try:
            raw = dict(isolated.value)
            raw["source_urls"] = tuple(raw.get("source_urls", ()))
            raw["authority_ids"] = tuple(raw.get("authority_ids", ()))
            results.append(LiveSiteResult(**raw))
        except (TypeError, ValueError) as exc:
            results.append(base._failed_result(
                key,
                site,
                context,
                IsolatedJobResult(
                    key=key,
                    ok=False,
                    error=f"invalid worker result: {type(exc).__name__}: {exc}",
                    elapsed=isolated.elapsed,
                ),
            ))
    if len(results) != len(sites) or {item.site_id for item in results} != {site.site_id for site in sites}:
        raise RuntimeError("live validation did not account for every configured site")
    return sorted(results, key=lambda item: item.index)


def run(args):
    if args.cycle_mode == "fixed" and args.cycle_length is None:
        raise SystemExit("fixed cycle mode requires --cycle-length")
    context = PeriodContext(
        PeriodKey(args.cycle, args.period),
        CyclePolicy(args.cycle_mode, args.cycle_length),
        args.periods,
    )
    sites = load_sites(Path(args.sites).resolve())
    results = run_live_validation(
        sites,
        context,
        workers=args.workers,
        timeout=args.timeout,
        hard_timeout=args.hard_timeout,
    )
    report = base.build_live_report(sites, context, results)
    json_path, markdown_path = base.write_live_report(Path(args.output_dir), report)
    print(
        f"[LIVE DONE] attempted={report['attempted_count']}/{report['site_count']} "
        f"current={report['current_success_count']} history_complete={report['history_complete_count']} "
        f"hard_timeouts={report['hard_timeout_count']}",
        flush=True,
    )
    print(f"[LIVE REPORT] {json_path.resolve()}")
    print(f"[LIVE REPORT] {markdown_path.resolve()}")
    if report["attempted_count"] != report["site_count"]:
        return 2
    if args.require_all_current and report["current_success_count"] != report["site_count"]:
        return 3
    if args.require_all_history and report["history_complete_count"] != report["site_count"]:
        return 4
    return 0


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(run(base.build_arg_parser().parse_args()))


__all__ = ["main", "run", "run_live_validation", "validate_live_site"]
