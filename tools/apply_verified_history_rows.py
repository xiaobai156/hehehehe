from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    target = ROOT / "he_app/services/cycle_fingerprint.py"
    text = target.read_text(encoding="utf-8")
    import_anchor = "from he_app.services.single_period import parse_site_period\n"
    import_line = "from he_app.services.verified_history_rows import extract_verified_history_rows\n"
    if import_line not in text:
        if import_anchor not in text:
            raise RuntimeError("cycle fingerprint import anchor not found")
        text = text.replace(import_anchor, import_anchor + import_line, 1)

    old = '''    dedicated = _strict_bulk_values(site, same_authority or documents)\n    generic = _generic_verified_history(site, same_authority or documents, parsed.evidence)\n    numeric_history, conflicts = _merge_numeric_history((dedicated, generic))\n'''
    new = '''    locked_documents = same_authority or documents\n    dedicated = _strict_bulk_values(site, locked_documents)\n    generic = _generic_verified_history(site, locked_documents, parsed.evidence)\n    verified_rows = extract_verified_history_rows(site, locked_documents)\n    numeric_history, conflicts = _merge_numeric_history(\n        (dedicated, generic, verified_rows)\n    )\n'''
    if old in text:
        text = text.replace(old, new, 1)
    elif "verified_rows = extract_verified_history_rows" not in text:
        raise RuntimeError("cycle fingerprint history merge anchor not found")
    target.write_text(text, encoding="utf-8", newline="")

    notes = ROOT / "REPAIR_NOTES.md"
    note_text = notes.read_text(encoding="utf-8")
    marker = "锁定记录逐行历史验证"
    if marker not in note_text:
        note_text += (
            "\n- 锁定记录逐行历史验证：当期解析先确定真实 authority/文章/区块；"
            "再在同一记录中逐行验证历史期。每行必须只有一个期数、合法合数数量和"
            "该站专属或严格关键词；特殊表格还必须位于专属区块内；同期不同值不入缓存。\n"
        )
        notes.write_text(note_text, encoding="utf-8", newline="")

    print("verified history row integration applied")


if __name__ == "__main__":
    main()
