from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def write(path: str, text: str) -> None:
    (ROOT / path).write_text(text, encoding="utf-8", newline="")


def patch_cycle_cache() -> None:
    path = "he_app/storage/cycle_cache.py"
    text = read(path)
    old = '''def _site_from_item(item: Mapping[str, object]) -> Site:\n    try:\n        return Site(\n            name=str(item["name"]).strip(),\n            url=str(item["url"]).strip(),\n            pick=str(item["pick"]).strip(),\n            browser=bool(item["browser"]),\n            click_first=bool(item["click_first"]),\n            site_id=str(item["id"]).strip(),\n            value_count=int(item.get("value_count", 1)),\n        )\n'''
    new = '''def _site_from_item(item: Mapping[str, object]) -> Site:\n    try:\n        site_id = str(item["id"]).strip()\n        raw_value_count = item.get("value_count")\n        value_count = (\n            2\n            if raw_value_count is None and site_id == "s085_kcvpleh"\n            else int(raw_value_count if raw_value_count is not None else 1)\n        )\n        return Site(\n            name=str(item["name"]).strip(),\n            url=str(item["url"]).strip(),\n            pick=str(item["pick"]).strip(),\n            browser=bool(item["browser"]),\n            click_first=bool(item["click_first"]),\n            site_id=site_id,\n            value_count=value_count,\n        )\n'''
    if old in text:
        text = text.replace(old, new, 1)
    elif "raw_value_count = item.get" not in text:
        raise RuntimeError("cycle cache site migration point not found")
    write(path, text)


def patch_live_validation() -> None:
    path = "he_app/services/live_validation.py"
    text = read(path)
    old_import = "from he_app.services.document_sources import collect_special_site_documents\n"
    new_import = '''from he_app.services.document_sources import (\n    DYNAMIC_HOME_TOPIC_SITE_IDS,\n    collect_special_site_documents,\n)\nfrom he_app.parsers.dedicated.ttss import TTSS_SITE_IDS\nfrom he_app.services.single_period import parse_site_period\nfrom he_app.validation.period import is_valid_sum_value\n'''
    if old_import in text:
        text = text.replace(old_import, new_import, 1)
    elif "DYNAMIC_HOME_TOPIC_SITE_IDS" not in text:
        raise RuntimeError("live validation import point not found")

    anchor = "\n\n@dataclass(frozen=True, slots=True)\nclass LiveSiteResult:"
    helper = '''\n\nPERIOD_SPECIFIC_RECORD_SITE_IDS = (\n    set(TTSS_SITE_IDS)\n    | set(DYNAMIC_HOME_TOPIC_SITE_IDS)\n    | {"s093_a_909922_article_aspx_id_3694545"}\n)\n\n\ndef _validated_period_value(site: Site, period: int, documents: list[str]) -> tuple[str, tuple[object, ...]] | None:\n    parsed = parse_site_period(site, period, documents)\n    if not parsed.success or not parsed.evidence:\n        return None\n    values = tuple(parsed.evidence[0].values)\n    required = int(getattr(site, "value_count", 1))\n    if len(values) != required or len(set(values)) != len(values):\n        return None\n    if not all(is_valid_sum_value(value) for value in values):\n        return None\n    return ",".join(values), tuple(parsed.evidence)\n\n\n@dataclass(frozen=True, slots=True)\nclass LiveSiteResult:'''
    if "PERIOD_SPECIFIC_RECORD_SITE_IDS" not in text:
        if anchor not in text:
            raise RuntimeError("live validation class anchor not found")
        text = text.replace(anchor, helper, 1)

    old_block = '''        with create_session() as session:\n            documents = collect_special_site_documents(\n                session,\n                site,\n                timeout,\n                context.current.number,\n            ) or []\n            if not documents:\n                if site.browser:\n                    browser = BrowserClient(headless=True)\n                    browser.start()\n                    documents = browser.get_documents(\n                        site.url,\n                        context.current.number,\n                        site.click_first,\n                        timeout,\n                    )\n                else:\n                    documents = collect_documents(session, site.url, timeout)\n            session_evidence = getattr(session, "last_network_evidence", None)\n            if isinstance(session_evidence, dict):\n                peer_evidence["last_http"] = dict(session_evidence)\n\n        result = build_cycle_site_fingerprint(site, documents, context)\n        source_urls, authority_ids = _document_sources(documents)\n        return asdict(\n            LiveSiteResult(\n                index=index,\n                site_id=site.site_id,\n                name=site.name,\n                url=site.url,\n                pick=site.pick,\n                browser=site.browser,\n                attempted=True,\n                current_ok=True,\n                current_value=result.current_value,\n                history_count=len(result.fingerprint),\n                history_required=context.periods,\n                history_complete=len(result.fingerprint) >= context.periods,\n                fingerprint=serialize_period_mapping(\n                    result.fingerprint,\n                    context=context,\n                ),\n                adapter=result.adapter,\n                error_category=("历史不足" if result.history_error else None),\n                error=result.history_error,\n                elapsed=time.monotonic() - started,\n                source_urls=source_urls,\n                authority_ids=authority_ids,\n                peer_evidence=peer_evidence,\n            )\n        )\n'''
    new_block = '''        all_documents: list[str] = []\n        per_period_evidence: dict[str, list[dict[str, object]]] = {}\n        with create_session() as session:\n            documents = collect_special_site_documents(\n                session,\n                site,\n                timeout,\n                context.current.number,\n            ) or []\n            if not documents:\n                if site.browser:\n                    browser = BrowserClient(headless=True)\n                    browser.start()\n                    documents = browser.get_documents(\n                        site.url,\n                        context.current.number,\n                        site.click_first,\n                        timeout,\n                    )\n                else:\n                    documents = collect_documents(session, site.url, timeout)\n            all_documents.extend(documents)\n            result = build_cycle_site_fingerprint(site, documents, context)\n            fingerprint = dict(result.fingerprint)\n            per_period_evidence[context.current.token()] = [\n                {\n                    "source_url": item.source_url,\n                    "document_id": item.document_id,\n                    "authority_id": item.authority_id,\n                    "article_id": item.article_id,\n                    "block_id": item.block_id,\n                }\n                for item in result.evidence\n            ]\n\n            if (\n                len(fingerprint) < context.periods\n                and site.site_id in PERIOD_SPECIFIC_RECORD_SITE_IDS\n            ):\n                for key in context.window:\n                    if key in fingerprint:\n                        continue\n                    try:\n                        period_documents = collect_special_site_documents(\n                            session,\n                            site,\n                            min(timeout, 12),\n                            key.number,\n                        ) or []\n                    except Exception as exc:\n                        per_period_evidence[key.token()] = [\n                            {"error": f"{type(exc).__name__}: {exc}"}\n                        ]\n                        continue\n                    if not period_documents:\n                        continue\n                    validated = _validated_period_value(\n                        site,\n                        key.number,\n                        period_documents,\n                    )\n                    if validated is None:\n                        continue\n                    value, evidence_items = validated\n                    fingerprint[key] = value\n                    all_documents.extend(period_documents)\n                    per_period_evidence[key.token()] = [\n                        {\n                            "source_url": item.source_url,\n                            "document_id": item.document_id,\n                            "authority_id": item.authority_id,\n                            "article_id": item.article_id,\n                            "block_id": item.block_id,\n                        }\n                        for item in evidence_items\n                    ]\n\n            session_evidence = getattr(session, "last_network_evidence", None)\n            if isinstance(session_evidence, dict):\n                peer_evidence["last_http"] = dict(session_evidence)\n            peer_evidence["period_records"] = per_period_evidence\n\n        source_urls, authority_ids = _document_sources(all_documents)\n        history_error = None\n        if len(fingerprint) < context.periods:\n            missing = [key.token() for key in context.window if key not in fingerprint]\n            history_error = (\n                f"verified history {len(fingerprint)}/{context.periods}; "\n                f"missing: {', '.join(missing)}"\n            )\n        return asdict(\n            LiveSiteResult(\n                index=index,\n                site_id=site.site_id,\n                name=site.name,\n                url=site.url,\n                pick=site.pick,\n                browser=site.browser,\n                attempted=True,\n                current_ok=True,\n                current_value=result.current_value,\n                history_count=len(fingerprint),\n                history_required=context.periods,\n                history_complete=len(fingerprint) >= context.periods,\n                fingerprint=serialize_period_mapping(\n                    fingerprint,\n                    context=context,\n                ),\n                adapter=(\n                    result.adapter + "+period-records"\n                    if site.site_id in PERIOD_SPECIFIC_RECORD_SITE_IDS\n                    else result.adapter\n                ),\n                error_category=("历史不足" if history_error else None),\n                error=history_error,\n                elapsed=time.monotonic() - started,\n                source_urls=source_urls,\n                authority_ids=authority_ids,\n                peer_evidence=peer_evidence,\n            )\n        )\n'''
    if old_block in text:
        text = text.replace(old_block, new_block, 1)
    elif "per_period_evidence" not in text:
        raise RuntimeError("live validation worker block not found")
    write(path, text)


def patch_notes() -> None:
    path = "REPAIR_NOTES.md"
    text = read(path)
    marker = "动态单文章历史补全"
    if marker not in text:
        text += '''\n- 动态单文章历史补全：TTSS、动态主页/栏目及亮劍列表若当前记录不足10期，会按跨周期窗口逐期寻找对应文章；每篇文章必须独立通过期数、栏目、来源和证据校验，才允许并入历史指纹。\n'''
    write(path, text)


def main() -> None:
    patch_cycle_cache()
    patch_live_validation()
    patch_notes()
    print("history completion integration applied")


if __name__ == "__main__":
    main()
