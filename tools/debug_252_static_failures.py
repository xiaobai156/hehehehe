from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from he_app.config.sites import load_sites
from he_app.fetch.http import create_session
from he_app.parsers.dedicated.tables import site_rule
from he_app.services.adaptive_fetch import collect_http_documents, collect_special_documents
from he_app.services.single_period import evaluate_site_period

TARGET_IDS = {
    "s001_topic_206535", "s005_topic_453944", "s007_topic_206633",
    "s012_topic_451629", "s016_topic_497780", "s018_mm",
    "s022_topic_678629", "s036_topic_205722", "s037_topic_252205",
    "s043_topic_252215", "s048_979363", "s051_topic_702074",
    "s054_topic_205701", "s059_topic_324760", "s064_topic_268622",
    "s065_topic_437721", "s068_topic_253482", "s076_topic_224258",
    "s086_aa_959787m_136", "s087_enpcjg", "s097_topic_250886",
    "s100_topic_205318", "s105_topic_213777", "s106_topic_281519",
    "s107_topic_453800", "s111_mm_737799b_art_gsb_8149",
    "s116_topic_440127", "s118_topic_1024655", "s119_topic_1024654",
    "s126_topic_930870", "s098_topic_227257", "s101_topic_274142",
    "s104_topic_245375", "s108_topic_1024380", "s121_topic_224339",
    "s131_kjfc_234432",
}

PERIOD_RE = re.compile(r"(?<!\d)252\s*期")


def snippets(text: str, limit: int = 5) -> list[str]:
    normalized = re.sub(r"\s+", " ", text)
    items = []
    for match in PERIOD_RE.finditer(normalized):
        start = max(0, match.start() - 140)
        end = min(len(normalized), match.end() + 260)
        item = normalized[start:end]
        if item not in items:
            items.append(item)
        if len(items) >= limit:
            break
    return items


def inspect(site):
    result = {
        "id": site.site_id,
        "name": site.name,
        "url": site.url,
        "pick": site.pick,
        "browser": site.browser,
        "rule": {
            "require_body_locator": site_rule(site).require_body_locator,
            "anchor_text": site_rule(site).anchor_text,
        },
        "special_error": None,
        "generic_error": None,
        "documents": [],
        "strict_result": None,
        "strict_category": None,
        "strict_detail": None,
    }
    documents = []
    with create_session() as session:
        try:
            special = collect_special_documents(session, site, 20, 252)
            if special:
                documents = special
        except Exception as exc:
            result["special_error"] = f"{type(exc).__name__}: {exc}"
        if not documents:
            try:
                documents = collect_http_documents(session, site, 20)
            except Exception as exc:
                result["generic_error"] = f"{type(exc).__name__}: {exc}"

    for document in documents:
        text = str(document)
        result["documents"].append({
            "source_url": str(getattr(document, "source_url", "") or ""),
            "fetch_kind": str(getattr(document, "fetch_kind", "") or ""),
            "record_id": str(getattr(document, "record_id", "") or ""),
            "length": len(text),
            "contains_252": bool(PERIOD_RE.search(text)),
            "snippets": snippets(text),
        })
    if documents:
        try:
            value, detail, values, category = evaluate_site_period(site, 252, documents)
            result["strict_result"] = value
            result["strict_category"] = category
            result["strict_detail"] = detail[:1000]
            result["strict_values"] = values
        except Exception as exc:
            result["strict_category"] = "exception"
            result["strict_detail"] = f"{type(exc).__name__}: {exc}"
    return result


def main():
    sites = [site for site in load_sites(Path("sites.json")) if site.site_id in TARGET_IDS]
    results = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(inspect, site): site for site in sites}
        for future in as_completed(futures):
            site = futures[future]
            try:
                item = future.result()
            except Exception as exc:
                item = {"id": site.site_id, "name": site.name, "fatal": f"{type(exc).__name__}: {exc}"}
            results.append(item)
            print(site.site_id, item.get("strict_result"), item.get("strict_category"), flush=True)
    results.sort(key=lambda item: item["id"])
    Path("debug-252-static").mkdir(exist_ok=True)
    Path("debug-252-static/report.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    summary = {
        "count": len(results),
        "strict_success": sum(bool(item.get("strict_result")) for item in results),
        "contains_252": sum(any(doc.get("contains_252") for doc in item.get("documents", [])) for item in results),
        "no_252": [item["id"] for item in results if item.get("documents") and not any(doc.get("contains_252") for doc in item.get("documents", []))],
        "fetch_failed": [item["id"] for item in results if not item.get("documents")],
    }
    Path("debug-252-static/summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
