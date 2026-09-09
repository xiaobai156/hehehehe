import json
from pathlib import Path
from urllib.parse import urlparse

from he_app.domain.errors import SiteConfigError
from he_app.domain.models import Site
from he_app.domain.policies import normalize_pick
from he_app.storage.atomic_write import write_text_atomic


REQUIRED_KEYS = {"id", "name", "url", "pick", "browser", "click_first"}


def load_sites(path: Path) -> list[Site]:
    if not path.exists():
        raise SiteConfigError(f"站点配置不存在: {path}")

    try:
        raw_sites = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SiteConfigError(f"站点配置无法读取: {path}: {exc}") from exc

    if not isinstance(raw_sites, list) or not raw_sites:
        raise SiteConfigError(f"站点配置没有有效站点: {path}")

    sites: list[Site] = []
    used_ids: set[str] = set()
    used_identities: set[tuple[str, str, str]] = set()
    for index, item in enumerate(raw_sites, start=1):
        if not isinstance(item, dict) or not REQUIRED_KEYS.issubset(item):
            raise SiteConfigError(f"站点配置第{index}条不是完整对象: {path}")
        if not all(isinstance(item[key], str) for key in ("id", "name", "url", "pick")):
            raise SiteConfigError(f"站点配置第{index}条文本字段类型错误: {path}")
        if type(item["browser"]) is not bool or type(item["click_first"]) is not bool:
            raise SiteConfigError(f"站点配置第{index}条布尔字段类型错误: {path}")

        site_id = item["id"].strip()
        name = item["name"].strip()
        url = item["url"].strip()
        raw_pick = item["pick"].strip()
        try:
            pick = normalize_pick(raw_pick)
        except ValueError as exc:
            raise SiteConfigError(f"站点配置第{index}条 pick 非法: {raw_pick}") from exc
        parsed_url = urlparse(url)
        if not site_id or not name or parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            raise SiteConfigError(f"站点配置第{index}条缺少合法 id/name/url: {path}")
        if pick not in {"top", "bottom"}:
            raise SiteConfigError(f"站点配置第{index}条 pick 非法: {raw_pick}")
        if site_id in used_ids:
            raise SiteConfigError(f"站点配置第{index}条重复 id: {site_id}")

        identity = (name, url, pick)
        if identity in used_identities:
            raise SiteConfigError(f"站点配置第{index}条重复站点身份: {name} {url} {pick}")
        used_ids.add(site_id)
        used_identities.add(identity)
        expected_count = 2 if site_id == "s085_kcvpleh" else 1
        value_count = item.get("value_count", expected_count)
        if type(value_count) is not int or value_count != expected_count:
            raise SiteConfigError(f"站点配置第{index}条 value_count 必须为{expected_count}")
        if item.get("top_period_exception") is not None:
            raise SiteConfigError("不支持期数方向例外 top_period_exception")
        sites.append(Site(name=name, url=url, pick=pick, browser=item["browser"],
                          click_first=item["click_first"], site_id=site_id,
                          value_count=value_count))

    return sites


def serialize_sites(sites: list[Site]) -> str:
    payload = [
        {
            "id": site.site_id,
            "name": site.name,
            "url": site.url,
            "pick": site.pick,
            "browser": site.browser,
            "click_first": site.click_first,
            "value_count": site.value_count,
        }
        for site in sites
    ]
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def write_sites_config(path: Path, sites: list[Site]) -> None:
    write_text_atomic(path, serialize_sites(sites), encoding="utf-8")
