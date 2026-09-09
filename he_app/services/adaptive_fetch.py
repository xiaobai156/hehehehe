from __future__ import annotations

import requests

from he_app.domain.models import Site
from he_app.fetch.discovery import collect_documents
from he_app.parsers.dedicated.tables import site_rule
from he_app.services.single_period import evaluate_site_period


STRONG_FAILURE_CATEGORIES = {
    "候选冲突",
    "数据不完整",
}


def collect_http_documents(
    session: requests.Session,
    site: Site,
    timeout: int,
) -> list[str]:
    """Fetch the configured URL without enabling unapproved derived sources."""

    rule = site_rule(site)
    return collect_documents(
        session,
        site.url,
        timeout,
        allow_inline_decode="http-decoded" in rule.allowed_fetch_kinds,
    )


def try_http_current(
    session: requests.Session,
    site: Site,
    period: int,
    timeout: int,
) -> tuple[list[str], tuple[str | None, str, list[str], str | None]] | None:
    """Use HTTP only when the exact configured period/direction validates.

    Browser-configured sites are probed through their exact same URL first.
    A HTTP result is accepted only after the normal strict parser validates the
    requested period, direction, value count, and evidence.  Any unsuccessful
    probe returns ``None`` and the caller may use the already-configured browser
    path.  This never widens top/bottom boundaries or adopts adjacent periods.
    """

    documents = collect_http_documents(session, site, timeout)
    evaluation = evaluate_site_period(site, period, documents)
    if evaluation[0] is None:
        return None
    return documents, evaluation


__all__ = ["collect_http_documents", "try_http_current"]
