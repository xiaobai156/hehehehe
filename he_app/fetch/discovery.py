import base64
import re

import requests

from he_app.domain.models import SourceDocument

from .http import fetch_text


SCRIPT_RE = re.compile(r"<script\b[^>]*\bsrc=[\"']([^\"']+)[\"']", re.I)
STRDECODE_RE = re.compile(r"strdecode\(\s*[\"']([^\"']+)[\"']\s*\)", re.I)


def decode_strdecode_blocks(text: str) -> list[str]:
    decoded: list[str] = []
    for match in STRDECODE_RE.finditer(text):
        value = match.group(1)
        try:
            padded = value + ("=" * (-len(value) % 4))
            raw = base64.b64decode(padded)
            decoded.append(raw.decode("utf-8", errors="ignore"))
        except Exception:
            continue
    return decoded


def collect_page_documents(
    session: requests.Session,
    url: str,
    timeout: int,
    decode_inline: bool = False,
) -> tuple[list[str], str]:
    page_html = fetch_text(session, url, timeout)
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
    page_decoded = decode_strdecode_blocks(page_html) if decode_inline else []
    if page_decoded:
        documents.extend(
            SourceDocument(
                text,
                source_url=url,
                fetch_kind="http-decoded",
                document_type="decoded",
                parent_url=url,
                authority_id=f"http:{url}:decoded:{index}",
                document_id=f"http:{url}:decoded:{index}",
            )
            for index, text in enumerate(page_decoded)
        )
    return documents, page_html


def collect_documents(session: requests.Session, url: str, timeout: int) -> list[str]:
    documents, _page_html = collect_page_documents(session, url, timeout)
    return documents
