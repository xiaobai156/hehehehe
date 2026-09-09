from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

from he_app.domain.models import Site, SiteRule
from he_app.parsers.edge_repairs import apply_remaining_edge_parser_repairs
from he_app.parsers.policies import SITE_RULES


# Live 252/253 diagnostics proved these configured pages can return a stale or
# incomplete HTTP shell while Chromium renders the current strict article.
# This is transport-only: the same configured site identity, period and
# physical top/bottom parser still decide success. HTTP remains the first probe
# and wins when it independently passes all strict checks.
BROWSER_RENDER_REQUIRED_SITE_IDS = frozenset(
    {
        # Earlier live-proven shells.
        "s051_topic_702074",  # 坐收其利
        "s065_topic_437721",  # 鸡飞蛋打
        "s108_topic_1024380",  # 命中劫 dynamic home
        # Remaining-thirty live verification, 2026-252/253.
        "s001_topic_206535",  # 电子排版
        "s005_topic_453944",  # 朝三暮四
        "s007_topic_206633",  # 达罗环宇
        "s012_topic_451629",  # 无適无莫
        "s016_topic_497780",  # 阳光明媚
        "s022_topic_678629",  # 错过中原
        "s032_topic_309383",  # 踏雪无痕网
        "s036_topic_205722",  # 伤心人家
        "s037_topic_252205",  # 残丝断魂
        "s043_topic_252215",  # 作舍道旁
        "s054_topic_205701",  # 稳打稳中
        "s058_vkjwinyt",  # 通天
        "s059_topic_324760",  # 春风化雨
        "s064_topic_268622",  # 兴高采烈
        "s068_topic_253482",  # 叹为观止
        "s076_topic_224258",  # 追根查源
        "s087_enpcjg",  # 嫦娥奔月
        "s097_topic_250886",  # 东益散人
        "s100_topic_205318",  # 没名字一
        "s105_topic_213777",  # 丰墙硗下
        "s106_topic_281519",  # 怒气冲冲
        "s107_topic_453800",  # 挂牌
        "s116_topic_440127",  # 没名字五
        "s118_topic_1024655",  # 和风细雨 dynamic detail
        "s119_topic_1024654",  # 新人旧梦 dynamic detail
        "s126_topic_930870",  # 流浪高手
        "s098_topic_227257",  # 雷锋第一版
        "s101_topic_274142",  # 没名字二
        "s104_topic_245375",  # 号令如山
        "s121_topic_224339",  # 亡羊补牢
    }
)

# Seven rendered pages have an exact strict period/绝杀一合/value row at the
# configured physical edge, but no stable generic body-locator token. Removing
# only that locator requirement does NOT relax period, keyword, value-count,
# uniqueness or top/bottom edge validation.
_RENDERED_STRICT_NO_LOCATOR_SITE_IDS = frozenset(
    {
        "s001_topic_206535",  # 电子排版
        "s005_topic_453944",  # 朝三暮四
        "s012_topic_451629",  # 无適无莫
        "s022_topic_678629",  # 错过中原
        "s036_topic_205722",  # 伤心人家
        "s054_topic_205701",  # 稳打稳中
        "s076_topic_224258",  # 追根查源
    }
)
for _site_id in _RENDERED_STRICT_NO_LOCATOR_SITE_IDS:
    _rule = SITE_RULES.get(_site_id, SiteRule())
    SITE_RULES[_site_id] = replace(
        _rule,
        require_body_locator=False,
        note=(f"{_rule.note}; " if _rule.note else "")
        + "live rendered strict edge has no stable generic body locator",
    )

# Install two narrow dedicated repairs after the normal parser registry exists:
# 踏雪无痕网 binds to its complete rendered history block; 通天 binds the
# exact 澳门综合杀 heading to the immediately following exact-header table.
apply_remaining_edge_parser_repairs()


# Dynamic 命中劫 home page must navigate to one unique same-origin link
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
