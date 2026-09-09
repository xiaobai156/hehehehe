from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def write(path: str, text: str) -> None:
    target = ROOT / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8", newline="")


def replace_required(text: str, old: str, new: str, label: str) -> str:
    if old in text:
        return text.replace(old, new, 1)
    if new in text:
        return text
    raise RuntimeError(f"cannot locate integration point: {label}")


def patch_process_jobs() -> None:
    path = "he_app/runtime/process_jobs.py"
    text = read(path)
    text = replace_required(
        text,
        "    pending = list(jobs)\n    if not pending:\n",
        "    job_list = list(jobs)\n    pending = list(job_list)\n    if not pending:\n",
        "materialize job order",
    )
    text = replace_required(
        text,
        "    results: dict[int, IsolatedJobResult] = {}\n",
        "    results: dict[Any, IsolatedJobResult] = {}\n",
        "result map type",
    )
    old_finish = '''    def finish(result: IsolatedJobResult) -> None:\n        results[int(result.key[0]) if isinstance(result.key, tuple) and result.key and isinstance(result.key[0], int) else len(results)] = result\n        if on_finished is not None:\n            on_finished(result)\n'''
    new_finish = '''    def finish(result: IsolatedJobResult) -> None:\n        if result.key in results:\n            raise RuntimeError(f"duplicate isolated job result key: {result.key!r}")\n        results[result.key] = result\n        if on_finished is not None:\n            on_finished(result)\n'''
    text = replace_required(text, old_finish, new_finish, "result storage")
    old_end = '''    # Preserve caller order rather than process completion order.\n    ordered_keys = [key for key, _payload in jobs] if not isinstance(jobs, list) else [key for key, _payload in jobs]\n    by_key = {result.key: result for result in results.values()}\n    return [by_key[key] for key in ordered_keys if key in by_key]\n'''
    new_end = '''    # Preserve caller order rather than process completion order.\n    ordered_keys = [key for key, _payload in job_list]\n    return [results[key] for key in ordered_keys if key in results]\n'''
    text = replace_required(text, old_end, new_end, "ordered result return")
    write(path, text)


def patch_http() -> None:
    path = "he_app/fetch/http.py"
    text = read(path)
    if "from he_app.fetch.network_policy import" not in text:
        anchor = "from he_app.domain.models import Site\n"
        import_block = (
            "from he_app.domain.models import Site\n"
            "from he_app.fetch.network_policy import (\n"
            "    NetworkPolicySession,\n"
            "    strict_network_policy_enabled,\n"
            ")\n"
        )
        text = replace_required(text, anchor, import_block, "network policy import")
    text = text.replace("session = requests.Session()", "session = NetworkPolicySession()")
    text = text.replace("verify=False", "verify=True")
    if '"-k",\n' in text:
        text = text.replace('        "-k",\n', "")
    fallback = "            return fetch_text_with_curl(url, timeout)"
    strict_fallback = (
        "            if strict_network_policy_enabled():\n"
        "                raise\n"
        "            return fetch_text_with_curl(url, timeout)"
    )
    if fallback in text and strict_fallback not in text:
        text = text.replace(fallback, strict_fallback, 1)
    if "NetworkPolicySession()" not in text:
        raise RuntimeError("HTTP session was not switched to NetworkPolicySession")
    write(path, text)


def patch_browser() -> None:
    path = "he_app/fetch/browser.py"
    text = read(path)
    if not text.startswith("import os\n") and "\nimport os\n" not in text[:200]:
        text = "import os\n" + text
    marker = "        options = Options()\n"
    pin_block = '''        options = Options()\n        pinned_host = os.environ.get("HE_BROWSER_PINNED_HOST", "").strip()\n        pinned_ip = os.environ.get("HE_BROWSER_PINNED_IP", "").strip()\n        if bool(pinned_host) != bool(pinned_ip):\n            raise RuntimeError("browser DNS pin requires both host and IP")\n        if pinned_host and pinned_ip:\n            options.add_argument(\n                f"--host-resolver-rules=MAP {pinned_host} {pinned_ip},EXCLUDE localhost"\n            )\n'''
    if "HE_BROWSER_PINNED_HOST" not in text:
        text = replace_required(text, marker, pin_block, "browser DNS pin")
    insecure = '        options.add_argument("--ignore-certificate-errors")\n'
    gated = '''        if os.environ.get("HE_ALLOW_INSECURE_BROWSER_TLS", "") == "1":\n            options.add_argument("--ignore-certificate-errors")\n'''
    if insecure in text:
        text = text.replace(insecure, gated, 1)
    write(path, text)


def patch_entrypoints() -> None:
    write(
        "he_crawler.py",
        '''"""Single-period command entry and backwards-compatible public facade."""\n\n# ruff: noqa: F401,F403\n\nfrom he_app.api import *\nfrom he_app.services.hard_runner import main\n\n\nif __name__ == "__main__":\n    main()\n''',
    )
    write(
        "he_duplicate_checker.py",
        '''"""Cycle-aware duplicate detection entry with backwards-compatible exports."""\n\n# ruff: noqa: F401,F403\n\nfrom he_app import api as he_crawler\nfrom he_app.services.duplicate_check import *\nfrom he_app.services.cycle_duplicate_check import main as _main\n\n\nif __name__ == "__main__":\n    _main()\n''',
    )


def patch_gitignore() -> None:
    path = ".gitignore"
    text = read(path) if (ROOT / path).exists() else ""
    additions = [
        "live-validation/",
        "duplicate-live-validation/",
        "outputs/recent_10_cycle_cache.json",
        "*.legacy-cycle-backup.json",
    ]
    lines = text.splitlines()
    for item in additions:
        if item not in lines:
            lines.append(item)
    write(path, "\n".join(lines).rstrip() + "\n")


def patch_notes() -> None:
    path = "REPAIR_NOTES.md"
    text = read(path)
    heading = "## 2026-09-09 第二阶段：生产补全"
    if heading in text:
        return
    text += f'''\n\n{heading}\n\n- 新增显式 `周期:期数` 身份与年周期/自定义固定周期模型；`001` 可与上一周期末期连续比较，缓存不再用裸整数混淆不同周期。\n- 新增 `outputs/recent_10_cycle_cache.json` 版本化侧车缓存；旧缓存只读迁移，站点身份按 ID 校验，失败删除当期但保留合法历史。\n- 正式单期入口改走每站独立子进程；硬超时从进程启动前计时，并终止该站产生的浏览器/子进程树。\n- HTTP 默认关闭环境代理，预解析全部 DNS，拒绝私网/环回/保留地址；实际 socket 对端必须属于预解析集合；重定向逐跳同源验证；响应体有硬上限。\n- 浏览器站在启动参数中绑定已验证的主机/IP，并默认不忽略 TLS 证书错误。\n- 每个配置站点均有历史适配路径：专属历史提取优先，其余只在当期严格解析锁定的同一记录/同一 authority 内提取，不跨文章或浏览器状态补历史。\n- 新增只读 `he_live_validator.py`，逐站实际访问并输出 JSON/Markdown 验收报告；报告必须覆盖配置中的每个站点，外部不可达也作为真实失败记录，而不是伪装成功。\n- 正式判重入口改用跨周期指纹和同一套硬隔离现场抓取；连续 3–5 期为疑似、6 期及以上拒收的规则不变。\n\n真实验收报告是某次运行环境和目标期的证据，不保证外部网站以后持续在线。站点不可达、证书失效、当期尚未发布或历史不足会明确出现在报告中；不得伪造数据把它们改成成功。\n'''
    write(path, text)


def patch_agents() -> None:
    path = "AGENTS.md"
    text = read(path)
    marker = "- 现有缓存只有裸整数期号"
    if "recent_10_cycle_cache.json" not in text:
        text += '''\n\n## 12. 跨周期与生产验收\n\n- 跨周期正式身份使用 `周期:三位期数`；不得再用裸 `001` 覆盖上一周期的 `001`。\n- 正式跨周期缓存为 `outputs\\recent_10_cycle_cache.json`；旧缓存仅作迁移输入，不得反向覆盖新缓存。\n- `he_live_validator.py` 只读运行，必须逐一记录全部配置站点；外部失败保留真实原因，不得写入正式成功文件。\n- 正式网络任务必须在可终止子进程中运行；DNS、TLS、浏览器及解析都属于同一硬超时范围。\n- 默认拒绝私网/环回/保留地址、DNS 对端变化、跨域重定向和无限响应体。\n'''
    write(path, text)


def main() -> None:
    patch_process_jobs()
    patch_http()
    patch_browser()
    patch_entrypoints()
    patch_gitignore()
    patch_notes()
    patch_agents()
    print("completion integration applied")


if __name__ == "__main__":
    main()
