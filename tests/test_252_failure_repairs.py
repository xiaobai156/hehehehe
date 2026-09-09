import json

import pytest

from he_app.domain.errors import SiteScrapeFailure
from he_app.domain.models import Site
from he_app.parsers.dedicated.tables import site_rule
from he_app.services import adaptive_fetch


def _site(site_id: str, *, name: str = "测试站", url: str = "https://example.test/topic/1.html", browser: bool = False):
    return Site(name, url, "top", browser, False, site_id)


def test_three_verified_sites_disable_only_body_locator_requirement() -> None:
    for site_id in ("s018_mm", "s048_979363", "s086_aa_959787m_136"):
        rule = site_rule(_site(site_id))
        assert rule.require_body_locator is False
        assert rule.anchor_pick == ""
        assert rule.allowed_fetch_kinds == ()


def test_forum_url_user_id_is_stronger_than_mutable_nickname(monkeypatch) -> None:
    site = _site(
        "s020_fklgrq",
        name="学无止境",
        url="https://forum.example.test/#/users/116149",
    )
    payload = [
        {
            "id": "post-252",
            "draw": 252,
            "topic": "252期 绝杀一合",
            "content": "252期 绝杀一合 [03合] 开00准",
            "user_id": 116149,
            "authorNickname": "昵称已经变化",
        }
    ]
    monkeypatch.setattr(adaptive_fetch, "fetch_text", lambda *_args, **_kwargs: json.dumps(payload, ensure_ascii=False))

    documents = adaptive_fetch.collect_forum_documents_by_url_identity(None, site, 1, 252)
    assert len(documents) == 1
    assert documents[0].record_id == "post-252"
    assert "252期" in documents[0]


def test_forum_wrong_url_user_id_is_rejected(monkeypatch) -> None:
    site = _site(
        "s020_fklgrq",
        name="学无止境",
        url="https://forum.example.test/#/users/116149",
    )
    payload = [
        {
            "id": "post-252",
            "draw": 252,
            "topic": "252期 绝杀一合",
            "content": "252期 绝杀一合 [03合] 开00准",
            "user_id": 999999,
            "authorNickname": "学无止境",
        }
    ]
    monkeypatch.setattr(adaptive_fetch, "fetch_text", lambda *_args, **_kwargs: json.dumps(payload, ensure_ascii=False))

    with pytest.raises(SiteScrapeFailure, match="数量=0"):
        adaptive_fetch.collect_forum_documents_by_url_identity(None, site, 1, 252)


def test_http_first_requires_the_normal_strict_evaluator(monkeypatch) -> None:
    site = _site("browser-site", browser=True)
    monkeypatch.setattr(adaptive_fetch, "collect_http_documents", lambda *_args, **_kwargs: ["raw"])
    monkeypatch.setattr(
        adaptive_fetch,
        "evaluate_site_period",
        lambda *_args, **_kwargs: (None, "252期不是top第一条", [], "方向范围外"),
    )
    assert adaptive_fetch.try_http_current(None, site, 252, 1) is None

    strict = ("03合 测试站", "252期 绝杀一合 [03合]", ["03合"], None)
    monkeypatch.setattr(adaptive_fetch, "evaluate_site_period", lambda *_args, **_kwargs: strict)
    probed = adaptive_fetch.try_http_current(None, site, 252, 1)
    assert probed is not None
    assert probed[1] == strict
