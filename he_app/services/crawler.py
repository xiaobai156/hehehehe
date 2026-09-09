from threading import Lock

import requests

from he_app.domain.errors import SiteScrapeFailure
from he_app.domain.models import Site
from he_app.fetch.browser import BrowserClient, BrowserPool, close_browser_safely
from he_app.fetch.http import create_session
from he_app.fetch.url_policy import same_origin_url
from he_app.services.adaptive_fetch import (
    collect_http_documents,
    collect_special_documents,
    try_http_current,
)
from he_app.services.browser_policy import runtime_click_first, runtime_requires_browser
from he_app.services.document_sources import find_dynamic_home_topic_url, requires_browser
from he_app.services.single_period import evaluate_site_period


Outcome = tuple[int, Site, str | None, str, str | None, list[str], str | None]
MATCHED_PERIOD_PREFIX = "__matched_period__:"
_RENDER_SPECIAL_DETAIL_SITE_IDS = frozenset(
    {
        "s118_topic_1024655",  # 和风细雨
        "s119_topic_1024654",  # 新人旧梦
    }
)


def matched_period_from_reason(requested_period: int, reason: str | None) -> int:
    """Return the strict period that produced a successful result.

    Existing callers use the final tuple field as failure metadata. Successful
    exact-period results still keep it as ``None``. A successful next-period
    fallback records one private marker here so cache sync can write the value
    under its real issue number without changing the public success TXT format.
    """

    if not reason or not reason.startswith(MATCHED_PERIOD_PREFIX):
        return requested_period
    raw = reason[len(MATCHED_PERIOD_PREFIX):].strip()
    if not raw.isdigit():
        return requested_period
    matched = int(raw)
    return matched if matched == requested_period + 1 else requested_period


def build_mirror_urls(site: Site, all_sites: list[Site], limit: int) -> list[str]:
    if limit:
        raise ValueError("自动拼接镜像已停用，请在正式配置中使用已验证URL")
    return []


def build_mirror_url_map(sites: list[Site], limit: int) -> dict[int, list[str]]:
    return {i: build_mirror_urls(site, sites, limit) for i, site in enumerate(sites)}


def _runtime_browser_required(site: Site) -> bool:
    return runtime_requires_browser(site, requires_browser)


def _render_discovered_special_detail(
    browser: BrowserClient,
    site: Site,
    period: int,
    timeout: int,
    documents: list[str],
) -> list[str] | None:
    """Render one exact same-origin detail discovered by a strict collector.

    和风细雨/新人旧梦 have a dynamic column page. The HTTP collector already
    proves the unique current article URL, but that detail can itself be a
    client-rendered shell. In that case render that *same discovered URL*;
    never fall back to a different article, domain, or an interior period.
    """

    if site.site_id not in _RENDER_SPECIAL_DETAIL_SITE_IDS:
        return None
    targets: set[str] = set()
    for document in documents:
        raw = str(getattr(document, "source_url", "") or "").strip()
        if not raw:
            continue
        targets.add(same_origin_url(site.url, raw))
    if len(targets) != 1:
        raise SiteScrapeFailure(
            "候选冲突",
            f"{site.name} 动态详情来源数量={len(targets)}，必须唯一",
        )
    target = next(iter(targets))
    return browser.get_documents(target, period, False, timeout)


def _render_dynamic_index_detail(
    browser: BrowserClient,
    site: Site,
    period: int,
    timeout: int,
) -> list[str] | None:
    """Browser-render the configured dynamic column and select one exact post.

    This fallback is intentionally limited to the two live-proven dynamic
    columns. It repeats the same strict selector used by the HTTP collector:
    configured site name + exact issue + 绝杀一合 + one unique same-origin
    link. It never searches an interior issue or a different domain.
    """

    if site.site_id not in _RENDER_SPECIAL_DETAIL_SITE_IDS:
        return None
    index_documents = browser.get_documents(site.url, period, False, timeout)
    target = find_dynamic_home_topic_url(site, index_documents, period)
    if target is None:
        raise SiteScrapeFailure(
            "主页找帖失败",
            f"{site.name} 浏览器栏目页未找到{period}期唯一同源绝杀一合文章",
        )
    target = same_origin_url(site.url, target)
    return browser.get_documents(target, period, False, timeout)


def _scrape_exact_site(
    session: requests.Session,
    site: Site,
    period: int,
    timeout: int,
    browser: BrowserClient | None = None,
) -> tuple[str | None, str, list[str], str | None]:
    """Run the unchanged strict parser for exactly one issue number."""

    special_failure: SiteScrapeFailure | None = None
    try:
        documents = collect_special_documents(session, site, timeout, period)
    except (TimeoutError, requests.exceptions.Timeout):
        # The two dynamic authors share one column URL. Under concurrent live
        # runs the strict HTTP discovery can exhaust its request budget even
        # though the rendered column is healthy. Render the same configured
        # column and repeat the exact name+issue+keyword+same-origin selection.
        if browser is not None and site.site_id in _RENDER_SPECIAL_DETAIL_SITE_IDS:
            rendered = _render_dynamic_index_detail(browser, site, period, timeout)
            if rendered is not None:
                return evaluate_site_period(site, period, rendered)
        raise
    except SiteScrapeFailure as exc:
        if browser is not None and site.site_id in _RENDER_SPECIAL_DETAIL_SITE_IDS:
            # Never hide a real identity/candidate conflict. A plain "no post"
            # result may be an HTTP shell, so only that category can be
            # re-evaluated from the fully rendered same column.
            if exc.category == "主页找帖失败":
                rendered = _render_dynamic_index_detail(browser, site, period, timeout)
                if rendered is not None:
                    return evaluate_site_period(site, period, rendered)
            if exc.category == "候选冲突":
                return None, exc.reason, [], exc.category
        # Other verified client-rendered pages may expose only an HTTP shell.
        # When the scheduler supplied a browser for the exact site ID, retain
        # the HTTP failure only as fallback and render the same configured URL.
        if browser is not None and _runtime_browser_required(site):
            special_failure = exc
            documents = None
        else:
            return None, exc.reason, [], exc.category

    browser_required = _runtime_browser_required(site)
    if documents is not None:
        evaluation = evaluate_site_period(site, period, documents)
        if evaluation[0] is not None:
            return evaluation
        # The dynamic collector may successfully identify the exact same-origin
        # article while its HTTP body is only a stale shell. Render only that
        # verified detail; do not rediscover or loosen the article identity.
        if browser is not None and browser_required:
            rendered = _render_discovered_special_detail(
                browser, site, period, timeout, documents
            )
            if rendered is not None:
                return evaluate_site_period(site, period, rendered)
        return evaluation

    if browser_required and browser is not None:
        # Browser-capable sites still probe HTTP first unless their dedicated
        # HTTP collector already proved that the page shell cannot locate the
        # target. HTTP is accepted only after the unchanged strict parser
        # proves exact period + configured physical direction.
        if special_failure is None:
            try:
                probed = try_http_current(session, site, period, min(timeout, 8))
            except Exception:
                probed = None
            if probed is not None:
                _documents, evaluation = probed
                return evaluation

        documents = browser.get_documents(
            site.url,
            period,
            runtime_click_first(site),
            timeout,
        )
    else:
        # Direct library/offline callers may intentionally provide no browser.
        # Keep strict HTTP evaluation available in that context. Production
        # scheduling still routes runtime-required site IDs through the browser
        # pool, so this does not disable the live stale-shell fallback.
        if special_failure is not None:
            return None, special_failure.reason, [], special_failure.category
        documents = collect_http_documents(session, site, timeout)
    return evaluate_site_period(site, period, documents)


def scrape_site(
    session: requests.Session,
    site: Site,
    period: int,
    timeout: int,
    browser: BrowserClient | None = None,
) -> tuple[str | None, str, list[str], str | None]:
    """Accept the requested issue, or only its immediate next issue at the same edge.

    The strict parser is executed independently for each issue. Therefore a
    ``top`` site can accept ``period + 1`` only when that issue is the physical
    top boundary; a ``bottom`` site can accept it only when it is the physical
    bottom boundary. Interior rows are never searched as a rescue path.
    """

    requested = _scrape_exact_site(session, site, period, timeout, browser)
    if requested[0] is not None:
        return requested

    next_period = period + 1
    following = _scrape_exact_site(session, site, next_period, timeout, browser)
    if following[0] is None:
        return requested

    result, detail, values, _reason = following
    return result, detail, values, f"{MATCHED_PERIOD_PREFIX}{next_period}"


def scrape_site_with_browser(
    session: requests.Session, site: Site, period: int, timeout: int, browser: BrowserClient
) -> tuple[str | None, str, list[str], str | None]:
    return scrape_site(session, site, period, timeout, browser)


def scrape_http_site(
    index: int,
    site: Site,
    period: int,
    timeout: int,
    host_locks: dict[str, Lock] | None = None,
    mirror_urls: list[str] | None = None,
) -> Outcome:
    try:
        with create_session(host_locks) as session:
            result = detail = None
            rank_values, previous_reason = [], None
            for url in [site.url, *(mirror_urls or [])]:
                attempt = site if url == site.url else Site(site.name, url, site.pick, False, site.click_first, site.site_id)
                result, detail, rank_values, previous_reason = scrape_site(session, attempt, period, timeout)
                if result:
                    break
        return index, site, result, detail, None, rank_values, previous_reason
    except Exception as exc:
        return index, site, None, "", f"{type(exc).__name__}: {exc}", [], None


def scrape_parallel_site(
    index: int,
    site: Site,
    period: int,
    timeout: int,
    show_browser: bool,
    host_locks: dict[str, Lock] | None = None,
    mirror_urls: list[str] | None = None,
    browser_pool: BrowserPool | None = None,
) -> Outcome:
    try:
        if _runtime_browser_required(site):
            owns_pool = browser_pool is None
            pool = browser_pool or BrowserPool(1, headless=not show_browser)
            if owns_pool:
                pool.start()
            try:
                with create_session(host_locks) as session:
                    result, detail, error, rank_values, previous_reason = pool.run(
                        lambda browser: invoke_browser_scrape(session, site, period, timeout, browser)
                    )
            finally:
                if owns_pool:
                    pool.close()
            return index, site, result, detail, error, rank_values, previous_reason
        return scrape_http_site(index, site, period, timeout, host_locks, mirror_urls)
    except Exception as exc:
        return index, site, None, "", f"{type(exc).__name__}: {exc}", [], None


def _is_renderer_timeout(exc: BaseException) -> bool:
    return (
        type(exc).__name__ == "TimeoutException"
        and "Timed out receiving message from renderer" in str(exc)
    )


def _restart_browser(browser: BrowserClient) -> None:
    close_browser_safely(browser)
    browser.start()


def scrape_browser_site(
    session: requests.Session, site: Site, period: int, timeout: int, browser: BrowserClient
) -> tuple[str | None, str, str | None, list[str], str | None]:
    for attempt in range(2):
        try:
            result, detail, rank_values, previous_reason = scrape_site_with_browser(
                session, site, period, timeout, browser
            )
            return result, detail, None, rank_values, previous_reason
        except Exception as exc:
            # Chrome occasionally reports a renderer IPC timeout on a healthy
            # page. Only this exact transport failure gets one clean-browser
            # retry. Parser/direction/identity failures are never retried or
            # relaxed, and partial DOM from the failed renderer is discarded.
            if attempt == 0 and _is_renderer_timeout(exc):
                try:
                    _restart_browser(browser)
                except Exception as restart_exc:
                    return (
                        None,
                        "",
                        f"{type(restart_exc).__name__}: {restart_exc}",
                        [],
                        None,
                    )
                continue
            return None, "", f"{type(exc).__name__}: {exc}", [], None
    return None, "", "RuntimeError: 浏览器重试状态异常", [], None


def invoke_browser_scrape(
    session: requests.Session, site: Site, period: int, timeout: int, browser: BrowserClient
) -> tuple[str | None, str, str | None, list[str], str | None]:
    return scrape_browser_site(session, site, period, timeout, browser)


def print_outcome(site: Site, result: str | None, detail: str, error: str | None, progress_prefix: str = "") -> None:
    prefix = f"{progress_prefix} " if progress_prefix else ""
    if result:
        print(f"{prefix}[OK] {result}")
        print(f"     {detail}")
    elif error:
        print(f"{prefix}[FAIL] {site.name}: {error}")
    else:
        print(f"{prefix}[FAIL] {site.name}: {detail}")