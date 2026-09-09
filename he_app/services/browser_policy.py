from __future__ import annotations

from collections.abc import Callable

from he_app.domain.models import Site


# Live 252 diagnostics proved these pages return an HTTP shell with no usable
# body while the same configured URL renders the strict article in Chromium.
# This is a transport-only override; site identity, period and top/bottom
# parsing rules remain unchanged.
BROWSER_RENDER_REQUIRED_SITE_IDS = frozenset(
    {
        "s051_topic_702074",  # 坐收其利
        "s065_topic_437721",  # 鸡飞蛋打
        "s108_topic_1024380",  # 命中劫 dynamic home
    }
)

# The dynamic 命中劫 home page must navigate to one unique same-origin link
# containing the exact issue and strict kill-sum keyword before parsing detail.
BROWSER_STRICT_CLICK_SITE_IDS = frozenset({"s108_topic_1024380"})


def runtime_requires_browser(
    site: Site,
    base_requires_browser: Callable[[Site], bool] | None = None,
) -> bool:
    if site.site_id in BROWSER_RENDER_REQUIRED_SITE_IDS:
        return True
    if base_requires_browser is None:
        from he_app.services.document_sources import requires_browser

        base_requires_browser = requires_browser
    return bool(base_requires_browser(site))


def runtime_click_first(site: Site) -> bool:
    return bool(site.click_first or site.site_id in BROWSER_STRICT_CLICK_SITE_IDS)


__all__ = [
    "BROWSER_RENDER_REQUIRED_SITE_IDS",
    "BROWSER_STRICT_CLICK_SITE_IDS",
    "runtime_click_first",
    "runtime_requires_browser",
]
