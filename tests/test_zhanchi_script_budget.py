from he_app.domain.models import Site
from he_app.services import document_sources

SHELL_PAGE = (
    '<html><body><script src="https://api.example.test/js-111-10381?v=1">'
    "</script></body></html>"
)
DECODED_SCRIPT = "展翅飞翔 273期: ❀绝杀一合❀ ⇨[01合] 开:發00中"


def _zhanchi_site() -> Site:
    return Site(
        "展翅飞翔",
        "https://example.test/nmWigH7bLp.html",
        "bottom",
        False,
        False,
        "s149_zhanchi",
    )


def test_zhanchi_decoded_script_gets_full_timeout_budget(monkeypatch):
    seen: list[tuple[str, int]] = []

    monkeypatch.setattr(
        document_sources, "collect_page_documents", lambda *a, **k: ([], SHELL_PAGE)
    )
    monkeypatch.setattr(
        document_sources, "decode_jgr_blocks", lambda text: [DECODED_SCRIPT]
    )

    def fake_fetch_text(session, url, timeout):
        seen.append((url, timeout))
        return DECODED_SCRIPT

    monkeypatch.setattr(document_sources, "fetch_text", fake_fetch_text)

    documents = document_sources.collect_zhanchi_documents(
        None, _zhanchi_site(), 20
    )

    assert seen == [("https://api.example.test/js-111-10381?v=1", 20)]
    assert len(documents) == 1
    assert DECODED_SCRIPT in documents[0]


def test_zhanchi_shell_only_page_yields_no_data_document(monkeypatch):
    monkeypatch.setattr(
        document_sources, "collect_page_documents", lambda *a, **k: ([], SHELL_PAGE)
    )
    monkeypatch.setattr(document_sources, "decode_jgr_blocks", lambda text: [])

    def fake_fetch_text(session, url, timeout):
        return "var no='273';"

    monkeypatch.setattr(document_sources, "fetch_text", fake_fetch_text)

    assert document_sources.collect_zhanchi_documents(None, _zhanchi_site(), 20) == []
