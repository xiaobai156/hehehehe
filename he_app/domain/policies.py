import html
import re


def normalize_text(text: str) -> str:
    replacements = {
        "\r": "\n",
        "\xa0": " ",
        "\u3000": " ",
        "（": "(",
        "）": ")",
        "{": "(",
        "}": ")",
        "【": "[",
        "】": "]",
        "［": "[",
        "］": "]",
        "〔": "[",
        "〕": "]",
        "〖": "[",
        "〗": "]",
        "｛": "{",
        "｝": "}",
        "，": ",",
        "：": ":",
        "；": ";",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def normalize_digit_text(text: str) -> str:
    return text.translate(str.maketrans("０１２３４５６７８９", "0123456789"))


def normalize_pick(pick: str) -> str:
    normalized = normalize_text(str(pick or "")).lower()
    if normalized in {"top", "顶部", "上", "上部", "前", "前面"}:
        return "top"
    if normalized in {"bottom", "尾部", "底部", "下", "下部", "后", "后面"}:
        return "bottom"
    raise ValueError(f"非法方向: {pick!r}")

