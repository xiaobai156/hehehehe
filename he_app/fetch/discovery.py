import base64
import re
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin

import requests

from he_app.domain.models import SourceDocument

from .http import create_session, fetch_text, host_key
from .url_policy import same_origin_url


SCRIPT_RE = re.compile(r"<script\b[^>]*\bsrc=[\"']([^\"']+)[\"']", re.I)
STRDECODE_RE = re.compile(r"strdecode\(\s*[\"']([^\"']+)[\"']\s*\)", re.I)


def decode_strdecode_blocks(text: str) -> list[str]:
    decoded: list[str] = []
    for match in STRDECODE_RE.finditer(text):
        value = match.group(1)
        try:
            padded = value + ("=" * (-len(value) % 4))
            raw = base64.b64decode(padded, validate=True)
            decoded.append(raw.decode("utf-8", errors="strict"))
        except Exception:
            continue
    return decoded


def collect_page_documents(session: requests.Session, url: str, timeout: int, *, allow_inline_decode: bool = False) -> tuple[list[str], str]:
    page_html = fetch_text(session, url, timeout)
    url = str(getattr(page_html, "final_url", url))
    page_authority = f"http:{url}:page"
    documents = [
        SourceDocument(
            page_html,
            source_url=url,
            fetch_kind="http",
            document_type="html",
            parent_url=url,
            authority_id=page_authority,
            document_id=f"{page_authority}:html",
        )
    ]
    page_decoded = decode_strdecode_blocks(page_html) if allow_inline_decode else []
    if page_decoded:
        documents.extend(
            SourceDocument(
                text,
                source_url=url,
                fetch_kind="http-decoded",
                document_type="decoded",
                parent_url=url,
                authority_id=page_authority,
                document_id=f"http:{url}:decoded:{index}",
            )
            for index, text in enumerate(page_decoded)
        )
    return documents, page_html


def collect_documents_from_page(
    session: requests.Session,
    url: str,
    timeout: int,
    documents: list[str],
    page_html: str,
    *, allow_scripts: bool = False,
) -> list[str]:
    if not allow_scripts:
        return documents
    url = str(getattr(page_html, "final_url", url))
    script_urls = [
        same_origin_url(url, script_url)
        for script_url in SCRIPT_RE.findall(page_html)
        if "/upload/script/" in urljoin(url, script_url)
    ]
    unique_urls = list(dict.fromkeys(script_urls))
    if len(unique_urls) > 8:
        raise ValueError("授权脚本数量超过8个上限")

    def fetch_script(script_url: str) -> tuple[str, list[str]]:
        worker_session = create_session(getattr(session, "_he_host_locks", {}))
        worker_session.headers.update(getattr(session, "headers", {}))
        worker_session.cookies.update(getattr(session, "cookies", {}))
        try:
            script_text = fetch_text(worker_session, script_url, timeout)
        except Exception:
            return script_url, []
        finally:
            worker_session.close()
        raw_authority = f"script:{script_url}:raw"
        script_documents = [
            SourceDocument(
                script_text,
                source_url=script_url,
                fetch_kind="script",
                document_type="script",
                parent_url=url,
                authority_id=raw_authority,
                document_id=raw_authority,
            )
        ]
        script_decoded = decode_strdecode_blocks(script_text)
        if script_decoded:
            script_documents.extend(
                SourceDocument(
                    text,
                    source_url=script_url,
                    fetch_kind="script-decoded",
                    document_type="decoded",
                    parent_url=url,
                    authority_id=raw_authority,
                    document_id=f"script:{script_url}:decoded:{index}",
                )
                for index, text in enumerate(script_decoded)
            )
        return script_url, script_documents

    fetched: dict[str, list[str]] = {}
    if unique_urls:
        host_locks = getattr(session, "_he_host_locks", {})
        has_serialized_host = any(host_key(script_url) in host_locks for script_url in unique_urls)
        max_workers = 1 if has_serialized_host else min(2, len(unique_urls))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            for script_url, script_documents in executor.map(fetch_script, unique_urls):
                fetched[script_url] = script_documents

    for script_url in unique_urls:
        documents.extend(fetched.get(script_url, []))
    return documents


def collect_documents(session: requests.Session, url: str, timeout: int,
                      *, allow_inline_decode: bool = False,
                      allow_scripts: bool = False) -> list[str]:
    documents, page_html = collect_page_documents(session, url, timeout,
                                                allow_inline_decode=allow_inline_decode)
    return collect_documents_from_page(session, url, timeout, documents, page_html,
                                       allow_scripts=allow_scripts)
