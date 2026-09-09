"""Regression coverage for the 2026-09 audit repairs. No real websites are used."""
import argparse
import base64
import codecs
import importlib
import json
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

from he_app.config.sites import load_sites, serialize_sites
from he_app.domain.errors import FingerprintCacheError, SiteConfigError, SiteScrapeFailure
from he_app.domain.models import Site, SourceDocument
from he_app.domain.policies import normalize_pick
from he_app.fetch import discovery, http
from he_app.fetch.browser import BrowserClient
from he_app.fetch.url_policy import ResolvedOrigin, StrictNetworkPolicy, same_origin_url, resolve_public_origin, verify_public_peer
from he_app.services import crawler, duplicate_check, multi_period, runner
from he_app.services.single_period import parse_site_period
from he_app.storage.atomic_write import commit_text_transaction
from he_app.storage.failure_records import FailureRecord, parse_failure_record, parse_failure_records, serialize_failure_record
from he_app.storage.recent_cache import (
    load_recent_cache, read_validated_fingerprints, update_recent_cache_from_outcomes,
    validate_cache_freshness, validate_recent_cache_identity, write_history_fingerprints,
)
from he_app.storage.reports import merge_failure_output, merge_success_output_lines


def site(key="a", name="甲站", pick="top"):
    return Site(name, f"https://example.test/{key}", pick, site_id=key)


def cache_data(sites, period=211):
    return {"base_period": period, "periods": 10,
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "sites": [dict(json.loads(serialize_sites([s]))[0], fingerprint={str(period): "03合"})
                      for s in sites], "errors": []}


def write_cache(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


@pytest.mark.parametrize("name", ["he_crawler", "he_duplicate_checker", "he_multi_period_crawler", "he_app.api"])
def test_public_entrypoints_import(name):
    assert importlib.import_module(name)


@pytest.mark.parametrize("value", ["", " ", "invalid", "tpo", None])
def test_no_silent_bottom_default(value):
    with pytest.raises(ValueError):
        normalize_pick(value)


@pytest.mark.parametrize("value,expected", [("顶部", "top"), ("上", "top"), ("尾部", "bottom"), ("后", "bottom")])
def test_direction_aliases_preserved(value, expected):
    assert normalize_pick(value) == expected


def test_two_value_site_config_and_cache_round_trip(tmp_path):
    target = Site("女人味", "https://example.test/woman", "top", site_id="s085_kcvpleh")
    assert target.value_count == 2
    config = tmp_path / "sites.json"
    config.write_text(serialize_sites([target]), encoding="utf-8")
    assert load_sites(config) == [target]
    path = tmp_path / "cache.json"
    update_recent_cache_from_outcomes(path, [target], {
        0: (target, "01合,02合 女人味", "211期", None, ["01合", "02合"], None)}, 211)
    assert read_validated_fingerprints(path, 211, 10)[1] == {0: {211: "01合,02合"}}


@pytest.mark.parametrize("change", [{"pick": ""}, {"value_count": 2}, {"top_period_exception": 211}, {"browser": "false"}])
def test_invalid_config_rejected(tmp_path, change):
    data = json.loads(serialize_sites([site()]))
    data[0].update(change)
    path = tmp_path / "sites.json"
    write_cache(path, data)
    with pytest.raises(SiteConfigError):
        load_sites(path)


def test_failure_records_preserve_exact_fields():
    record = FailureRecord("a", "甲站", "https://example.test/a", "top", 211,
                           "无当期", "指定期数校验", "没有找到211期")
    assert parse_failure_record(serialize_failure_record(record)) == record
    text = "失败 甲站 https://example.test/a 方向: top 期数: 211 阶段: 数据解析 原因: 未找到"
    parsed = parse_failure_records(text, [site()])[0]
    assert parsed.site_id == "a" and parsed.reason == "未找到"
    assert parsed.category == "历史记录"  # Never mistake a stage for a category.
    mixed = serialize_failure_record(record) + "\n\n失败 乙站 https://example.test/b 方向: bottom 期数: 211 阶段: 数据解析 原因: 未找到\n\n失败分类统计\n无当期 2条\n"
    records = parse_failure_records(mixed, [site(), site("b", "乙站", "bottom")])
    assert [r.site_id for r in records] == ["a", "b"]


def test_failure_record_identity_cannot_be_spoofed():
    text = "失败 别人 https://evil.test/ 站点ID: a 方向: top 期数: 211 失败类型: 无当期 具体原因: 无"
    with pytest.raises(ValueError):
        parse_failure_record(text, [site()])


def test_duplicate_failure_id_rejected():
    record = FailureRecord("a", "甲站", "https://example.test/a", "top", 211, "无当期", "数据解析", "无")
    with pytest.raises(ValueError):
        parse_failure_records((serialize_failure_record(record) + "\n\n") * 2)


def test_failure_merge_removes_repaired_id():
    a = FailureRecord("a", "甲站", "https://example.test/a", "top", 211, "无当期", "数据解析", "无")
    b = FailureRecord("b", "乙站", "https://example.test/b", "bottom", 211, "请求失败", "网络请求", "超时")
    merged = merge_failure_output(serialize_failure_record(a) + "\n\n" + serialize_failure_record(b), [], {"a"})
    assert [r.site_id for r in parse_failure_records(merged)] == ["b"]


def test_append_does_not_silently_keep_conflicting_success():
    with pytest.raises(ValueError, match="冲突"):
        merge_success_output_lines("03合 甲站\n", ["04合 甲站"])


def test_retry_old_report_end_to_end(tmp_path, monkeypatch):
    a, b = site(), site("b", "乙站", "bottom")
    config, success, failure = [tmp_path / name for name in ("sites.json", "success.txt", "fail.txt")]
    config.write_text(serialize_sites([a, b]), encoding="utf-8")
    success.write_text("03合 甲站\n\n内容\t次数\t排名\n03合\t1\t1\n", encoding="utf-8-sig")
    failure.write_text("失败 乙站 https://example.test/b 方向: bottom 期数: 211 阶段: 指定期数校验 原因: 无当期\n", encoding="utf-8-sig")
    called = []
    def scrape(index, current_site, *args, **kwargs):
        called.append(current_site.site_id)
        return index, current_site, "04合 乙站", "211期绝杀一合[04合]", None, ["04合"], None
    monkeypatch.setattr(runner, "scrape_parallel_site", scrape)
    args = runner.build_arg_parser().parse_args([
        "--period", "211", "--sites", str(config), "--success", str(success),
        "--fail", str(failure), "--retry-failures", "--no-fingerprint-cache-sync",
        "--no-isolation", "--cycle-year", "2026"])
    assert runner.run(args) == 0
    assert called == ["b"]
    text = success.read_text(encoding="utf-8-sig")
    assert "03合 甲站" in text and "04合 乙站" in text
    assert not failure.exists()
    assert runner.run(args) == 0 and called == ["b"]


def test_cache_reordering_is_not_identity_change():
    a, b = site(), site("b", "乙站")
    data = cache_data([b, a])
    validate_recent_cache_identity(data, [a, b])
    data["sites"][0]["pick"] = "bottom"
    with pytest.raises(FingerprintCacheError):
        validate_recent_cache_identity(data, [a, b])


@pytest.mark.parametrize("value", ["00合", "14合", "3合", "03合,04合", 3])
def test_invalid_cached_values_rejected(tmp_path, value):
    data = cache_data([site()]); data["sites"][0]["fingerprint"]["211"] = value
    path = tmp_path / "cache.json"; write_cache(path, data)
    with pytest.raises(FingerprintCacheError):
        read_validated_fingerprints(path, 211, 10)


def test_failed_current_history_is_deleted_not_hidden(tmp_path):
    a, b = site(), site("b", "乙站")
    data = cache_data([a, b])
    data["sites"][0]["fingerprint"]["210"] = "02合"
    path = tmp_path / "cache.json"; write_cache(path, data)
    write_history_fingerprints(path, [a, b], {1: {211: "04合", 210: "05合"}}, {0: "现场失败"}, 211, 10)
    result = load_recent_cache(path)
    assert result["sites"][0]["fingerprint"] == {"210": "02合"}
    assert result["errors"][0]["id"] == "a"
    assert result["sites"][1]["fingerprint"] == {"211": "04合", "210": "05合"}


def test_partial_update_preserves_empty_unattempted_site(tmp_path):
    a, b = site(), site("b", "乙站")
    data = cache_data([a, b]); data["sites"][1]["fingerprint"] = {}
    path = tmp_path / "cache.json"; write_cache(path, data)
    update_recent_cache_from_outcomes(path, [a], {0: (a, "04合 甲站", "211期", None, ["04合"], None)}, 211, preserve_unconfigured_sites=True)
    result = load_recent_cache(path)
    assert [s["id"] for s in result["sites"]] == ["a", "b"]
    validate_recent_cache_identity(result, [a, b])


def test_partial_run_cannot_create_incomplete_official_cache(tmp_path):
    a = site()
    with pytest.raises(FingerprintCacheError):
        update_recent_cache_from_outcomes(tmp_path / "new.json", [a],
            {0: (a, "03合 甲站", "211期", None, ["03合"], None)}, 211, preserve_unconfigured_sites=True)


@pytest.mark.parametrize("stamp", ["old", "2000-01-01 00:00:00", "2099-01-01 00:00:00", None])
def test_stale_invalid_future_caches_rejected(stamp):
    with pytest.raises(FingerprintCacheError):
        validate_cache_freshness({"updated_at": stamp})


def test_current_cache_freshness():
    validate_cache_freshness(cache_data([site()]))


@pytest.mark.parametrize("url", ["http://127.0.0.1/a", "http://[::1]/a", "http://192.168.1.1/", "https://other.test/a", "file:///tmp/a", "https://user:password@example.test/a"])
def test_unsafe_discovered_urls_rejected(url):
    with pytest.raises(SiteScrapeFailure):
        same_origin_url("https://example.test/index", url)


def test_relative_and_spa_urls_preserve_identity():
    assert same_origin_url("https://example.test/list", "/#/forums/42") == "https://example.test/#/forums/42"


class Response:
    def __init__(self, url, status=200, headers=None, body=b"ok"):
        self.url, self.status_code, self.headers, self.body = url, status, headers or {}, body
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def iter_content(self, chunk_size): yield self.body


class Session:
    def __init__(self, responses): self.responses, self.calls = iter(responses), []
    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return next(self.responses)


def test_http_validates_redirect_before_second_request():
    session = Session([Response("https://example.test/", 302, {"Location": "https://other.test/"})])
    with pytest.raises(SiteScrapeFailure):
        http.fetch_text_fast(session, "https://example.test/", 10)
    assert len(session.calls) == 1
    assert session.calls[0][1]["allow_redirects"] is False
    assert session.calls[0][1].get("verify") is not False


def test_http_final_url_and_status_are_preserved():
    session = Session([Response("https://example.test/", 302, {"Location": "/article"}),
                       Response("https://example.test/article", body="正确".encode())])
    text = http.fetch_text_fast(session, "https://example.test/", 10)
    assert text == "正确" and text.final_url == "https://example.test/article" and text.status_code == 200


@pytest.mark.parametrize("exc", [ValueError("decode"), RuntimeError("HTTP Error 500"), requests.exceptions.SSLError("certificate")])
def test_non_transport_errors_never_trigger_curl(monkeypatch, exc):
    def fail(*args): raise exc
    monkeypatch.setattr(http, "fetch_text_fast", fail)
    monkeypatch.setattr(http, "fetch_text_with_curl", lambda *args: pytest.fail("must not retry"))
    with pytest.raises(type(exc)):
        http.fetch_text(SimpleNamespace(), "https://example.test/", 10)


def test_curl_status_is_required(monkeypatch):
    monkeypatch.setattr(http, "curl_command_variants", lambda *args: [["curl", "https://example.test/"]])
    monkeypatch.setattr(http.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=b"looks valid", stderr=b""))
    with pytest.raises(RuntimeError):
        http.fetch_text_with_curl("https://example.test/", 5, ResolvedOrigin("https", "example.test", 443, ("93.184.216.34",)))


def test_bom_and_meta_encoding_are_respected():
    assert http.decode_response_bytes(codecs.BOM_UTF8 + "正确".encode()) == "正确"
    text = '<meta charset="big5">測試'
    assert http.decode_response_bytes(text.encode("big5")) == text
    with pytest.raises(ValueError):
        http.decode_response_bytes(b"\xff", "text/html; charset=utf-8")


def test_inline_decode_requires_explicit_authorization(monkeypatch):
    raw = "211期 绝杀一合 [03合]"
    html = f'<script>strdecode("{base64.b64encode(raw.encode()).decode()}")</script>'
    monkeypatch.setattr(discovery, "fetch_text", lambda *args: html)
    assert len(discovery.collect_page_documents(None, "https://example.test/", 1)[0]) == 1
    docs, _ = discovery.collect_page_documents(None, "https://example.test/", 1, allow_inline_decode=True)
    assert len(docs) == 2 and docs[0].authority_id == docs[1].authority_id
    assert discovery.decode_strdecode_blocks('strdecode("###")') == []


def test_external_script_rejected_before_request(monkeypatch):
    monkeypatch.setattr(discovery, "fetch_text", lambda *args: pytest.fail("must not fetch"))
    with pytest.raises(SiteScrapeFailure):
        discovery.collect_documents_from_page(None, "https://example.test/", 1, [],
            '<script src="https://other.test/upload/script/a.js"></script>', allow_scripts=True)


def test_local_locator_does_not_leak_between_dom_sections():
    doc = '<div><div class="article-a">作者:甲</div><div class="other-column">211期绝杀一合[03合]开</div></div>'
    assert not parse_site_period(site("unregistered"), 211, [doc]).success


def test_bottom_evidence_uses_last_physical_occurrence():
    raw = "211期绝杀一合[03合]开"
    doc = "作者:甲\n" + raw + "\n210期绝杀一合[02合]开\n" + raw
    result = parse_site_period(site("unregistered", pick="bottom"), 211, [doc])
    assert result.success and result.value == "03合 甲站"
    assert result.evidence[0].block_start > doc.index("210期")


def test_same_snapshot_complete_boundary_cannot_be_rescued():
    full = SourceDocument("212期绝杀一合[01合]开\n211期绝杀一合[03合]开", fetch_kind="browser", document_type="page-source", authority_id="same", document_id="full")
    partial = SourceDocument("211期绝杀一合[03合]开", fetch_kind="browser", document_type="body-text", authority_id="same", document_id="partial")
    assert not parse_site_period(site("s097_topic_250886"), 211, [partial, full]).success


def test_browser_returns_final_source_not_navigation_snapshots(monkeypatch):
    class NoopBrowserNetworkPolicy:
        def resolve(self, url): return ResolvedOrigin("https", "example.test", 443, ("93.184.216.34",))
        def verify_address(self, value, **kwargs): return value
    client = BrowserClient(network_policy=NoopBrowserNetworkPolicy())
    class Driver:
        current_url = "https://example.test/list"
        page_source = "211期绝杀一合[03合]开"
        def set_page_load_timeout(self, value): pass
        def get(self, url): self.current_url = url
        def find_element(self, *args): return SimpleNamespace(text=self.page_source)
        def get_log(self, kind):
            return [{"message": json.dumps({"message": {"method": "Network.responseReceived", "params": {"type": "Document", "response": {"url": self.current_url, "remoteIPAddress": "93.184.216.34"}}}})}]
    client.driver = Driver()
    monkeypatch.setattr(client, "wait_for_document", lambda *args: None)
    monkeypatch.setattr(client, "try_click_labels", lambda: False)
    def click(period): client.driver.current_url = "https://example.test/article"; return True
    monkeypatch.setattr(client, "try_click_best_post", click)
    docs = client.get_documents("https://example.test/list", 211, True, 5)
    assert len(docs) == 1
    assert docs[0].document_type == "page-source"
    assert docs[0].source_url == "https://example.test/article"
    assert docs[0].parent_url == "https://example.test/list"


def test_dynamic_links_are_unique_and_period_bounded():
    from he_app.services.document_sources import find_dynamic_home_topic_url
    target = site("s118_topic_1024655", "甲站")
    html = '<a href="/old">1211期甲站绝杀一合</a><a href="/current">211期甲站绝杀一合</a>'
    assert find_dynamic_home_topic_url(target, [html], 211) == "https://example.test/current"
    with pytest.raises(SiteScrapeFailure, match="多个"):
        find_dynamic_home_topic_url(target, [html + '<a href="/other">211期甲站绝杀一合</a>'], 211)


def test_arbitrary_mirror_synthesis_is_disabled():
    assert crawler.build_mirror_urls(site(), [], 0) == []
    with pytest.raises(ValueError):
        crawler.build_mirror_urls(site(), [site("b")], 1)


def test_atomic_transaction_rolls_back_and_cleans_staging(tmp_path, monkeypatch):
    import he_app.storage.atomic_write as aw
    a, b = tmp_path / "a.txt", tmp_path / "b.txt"
    a.write_text("old-a"); b.write_text("old-b")
    def fail(*args): raise OSError("fsync failure")
    monkeypatch.setattr(aw.os, "fsync", fail)
    with pytest.raises(OSError):
        commit_text_transaction({a: ("new-a", "utf-8"), b: ("new-b", "utf-8")})
    assert a.read_text() == "old-a" and b.read_text() == "old-b"
    assert sorted(x.name for x in tmp_path.iterdir()) == ["a.txt", "b.txt"]


def test_multiple_periods_continue_and_never_read_failed_runs_old_success(tmp_path, monkeypatch):
    a, b = site(), site("b", "乙站")
    config = tmp_path / "sites.json"; config.write_text(serialize_sites([a, b]))
    success_dir, fail_dir = tmp_path / "ok", tmp_path / "bad"
    success_dir.mkdir(); fail_dir.mkdir()
    (success_dir / "211期-合.txt").write_text("03合 甲站\n", encoding="utf-8-sig")
    calls = []
    def run(command, **kwargs):
        period = int(command[command.index("--period") + 1]); calls.append(period)
        assert "--no-fingerprint-cache-sync" in command
        if period == 211:
            return SimpleNamespace(returncode=1)
        Path(command[command.index("--success") + 1]).write_text("04合 乙站\n", encoding="utf-8-sig")
        record = FailureRecord("a", a.name, a.url, a.pick, period, "无当期", "数据解析", "无")
        Path(command[command.index("--fail") + 1]).write_text(serialize_failure_record(record), encoding="utf-8-sig")
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(multi_period.subprocess, "run", run)
    args = multi_period.build_arg_parser().parse_args(["211", "212", "--sites", str(config), "--output-dir", str(success_dir), "--failure-output-dir", str(fail_dir)])
    assert multi_period.run_periods(args) == 1 and calls == [211, 212]
    text = (fail_dir / "211-212期-多期全部失败.txt").read_text(encoding="utf-8-sig")
    assert "甲站" in text and "乙站" not in text and "未采用旧TXT" in text


@pytest.mark.parametrize("site_id,header", [
    ("s097_topic_250886", ""),
    ("unregistered", "作者:甲站\n"),
    ("s015_topic_206515", "作者:返回来个\n"),
])
def test_history_collects_ten_rows_without_relaxing_current_edge(site_id, header):
    from he_app.services.fingerprint import build_site_fingerprint
    document = header + "\n".join(f"{p}期绝杀一合[{(p % 13) + 1:02d}合]开" for p in range(211, 201, -1))
    target = site(site_id)
    assert len(build_site_fingerprint(target, [document], 211, 10)) == 10
    assert build_site_fingerprint(target, [document], 210, 10) == {}


def test_fingerprint_cannot_assemble_disjoint_histories():
    from he_app.services.fingerprint import build_site_fingerprint
    first = "211期绝杀一合[03合]开\n210期绝杀一合[02合]开"
    second = "211期绝杀一合[03合]开\n209期绝杀一合[01合]开"
    fingerprint = build_site_fingerprint(site("s097_topic_250886"), [first, second], 211, 10)
    assert len(fingerprint) == 2


def test_cross_source_historical_conflict_is_not_a_success():
    from he_app.services.fingerprint import build_site_fingerprint
    first = "211期绝杀一合[03合]开\n210期绝杀一合[02合]开"
    second = "211期绝杀一合[03合]开\n210期绝杀一合[01合]开"
    with pytest.raises(SiteScrapeFailure, match="冲突"):
        build_site_fingerprint(site("s097_topic_250886"), [first, second], 211, 10)


def test_cycle_rollover_does_not_silently_keep_old_baseline(tmp_path):
    a = site(); path = tmp_path / "cache.json"
    write_cache(path, cache_data([a], 365))
    before = path.read_bytes()
    with pytest.raises(FingerprintCacheError, match="期数回绕"):
        update_recent_cache_from_outcomes(path, [a], {0: (a, "04合 甲站", "1期", None, ["04合"], None)}, 1)
    assert path.read_bytes() == before


def test_forum_api_proves_record_and_owner(monkeypatch):
    from he_app.services import document_sources as ds
    target = Site("甲站", "https://example.test/#/users/42", "top", site_id="forum-test")
    record = {"id": 99, "user_id": 42, "draw": 211, "topic": "绝杀一合", "content": "211期绝杀一合[03合]开"}
    monkeypatch.setattr(ds, "fetch_text", lambda *args: json.dumps([record]))
    docs = ds.collect_forum_api_documents(None, target.url, 1, site=target, period=211)
    assert len(docs) == 1 and docs[0].record_id == "99"
    record["user_id"] = 41
    with pytest.raises(SiteScrapeFailure, match="所属用户"):
        ds.collect_forum_api_documents(None, target.url, 1, site=target, period=211)


def test_forum_api_cannot_select_first_of_two_posts(monkeypatch):
    from he_app.services import document_sources as ds
    target = Site("甲站", "https://example.test/#/users/42", "top", site_id="forum-test")
    record = {"id": 99, "user_id": 42, "draw": 211, "topic": "绝杀一合", "content": "211期绝杀一合[03合]开"}
    monkeypatch.setattr(ds, "fetch_text", lambda *args: json.dumps([record, dict(record, id=100)]))
    with pytest.raises(SiteScrapeFailure, match="必须唯一"):
        ds.collect_forum_api_documents(None, target.url, 1, site=target, period=211)


def test_explicit_http_api_routes_do_not_launch_browser(monkeypatch):
    from he_app.services.document_sources import requires_browser
    target = Site("亮劍", "https://example.test/list.aspx?id=209", "top", True, site_id="s093_a_909922_article_aspx_id_3694545")
    assert not requires_browser(target)
    monkeypatch.setattr(crawler, "scrape_http_site", lambda *args: "http")
    assert crawler.scrape_parallel_site(0, target, 211, 1, False) == "http"


def test_case_distinct_transaction_paths_do_not_share_linux_lock(tmp_path):
    import os
    if os.name == "nt":
        pytest.skip("Windows paths are case insensitive")
    a, b = tmp_path / "a.txt", tmp_path / "A.txt"
    commit_text_transaction({a: ("a", "utf-8"), b: ("b", "utf-8")}, timeout=0.1)
    assert a.read_text() == "a" and b.read_text() == "b"


def test_insufficient_history_report_preserves_valid_current_cache(tmp_path, monkeypatch):
    import sys
    monkeypatch.chdir(tmp_path)
    target = site()
    Path("sites.json").write_text(serialize_sites([target]), encoding="utf-8")
    monkeypatch.setattr(duplicate_check, "run_fingerprint_jobs",
                        lambda *a, **k: ({0: {211: "03合"}}, {}))
    captured = {}
    def write(path, sites, fingerprints, errors, *args, **kwargs):
        captured["fingerprints"] = fingerprints
        captured["errors"] = errors
    monkeypatch.setattr(duplicate_check, "write_fingerprint_cache", write)
    # Captured stdout may not implement TextIOWrapper.reconfigure.
    monkeypatch.setattr(sys, "stdout", SimpleNamespace(reconfigure=lambda **k: None,
                       write=lambda text: len(text), flush=lambda: None))
    monkeypatch.setattr(sys, "argv", ["checker", "--period", "211", "--write-fingerprint-cache"])
    duplicate_check.main()
    assert captured["fingerprints"] == {0: {211: "03合"}}
    assert captured["errors"] == {}
    assert "历史不足" in Path("211期重复检测失败.txt").read_text(encoding="utf-8-sig")


def _isolated_test_worker(index, target, period, periods, timeout, show_browser, host_locks):
    return index, {period: "03合"}, None


def test_spawn_workers_complete_and_close_without_network():
    targets = [site(), site("b", "乙站")]
    results, errors = duplicate_check.run_fingerprint_jobs(
        list(enumerate(targets)), targets, 1, 211, 10, 1, False, {},
        hard_timeout=15, poll_interval=0.01, worker_callable=_isolated_test_worker)
    assert results == {0: {211: "03合"}, 1: {211: "03合"}}
    assert errors == {}


def test_session_ignores_environment_proxies():
    session = http.create_session()
    try:
        assert session.trust_env is False
        assert session.proxies == {}
        assert isinstance(session._he_network_policy, StrictNetworkPolicy)
    finally:
        session.close()


def test_dns_policy_rejects_private_or_mixed_answers(monkeypatch):
    import socket
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443)),
    ])
    with pytest.raises(SiteScrapeFailure, match="非公网"):
        resolve_public_origin("https://example.test/")


def test_peer_policy_rejects_dns_rebinding_to_private_address():
    class Sock:
        def getpeername(self): return ("10.0.0.5", 443)
    response = SimpleNamespace(raw=SimpleNamespace(_connection=SimpleNamespace(sock=Sock())))
    with pytest.raises(SiteScrapeFailure, match="非公网"):
        verify_public_peer(response, ResolvedOrigin("https", "example.test", 443, ("93.184.216.34",)))


def test_peer_policy_accepts_public_connected_address():
    class Sock:
        def getpeername(self): return ("93.184.216.34", 443)
    response = SimpleNamespace(raw=SimpleNamespace(_connection=SimpleNamespace(sock=Sock())))
    assert verify_public_peer(response, ResolvedOrigin("https", "example.test", 443, ("93.184.216.34",))) == "93.184.216.34"
