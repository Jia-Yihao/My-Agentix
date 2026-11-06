#!/usr/bin/env python
"""版本号同步脚本（单一事实来源维护）。

用法：
    python scripts/bump_version.py 0.2.0          # 同步三处版本号
    python scripts/bump_version.py --check        # 仅校验一致性（CI 使用）

同步目标（四处必须一致）：
    1. pyproject.toml         [project].version
    2. agentix/__init__.py   __version__   ← 权威来源
    3. web/package.json       version
    4. .env.example           APP_VERSION   （可选文件，存在才校验）

设计依据：「单一版本事实来源」。
CI 的 version-consistency 作业以 --check 模式运行，不一致即失败。

第 4 处是批次 13 补上的：`settings.APP_VERSION` 现由 `agentix.__version__`
惰性派生，但 `.env` / `.env.example` 仍可用 `APP_VERSION=` **覆盖**它。
历史事故：`.env.example` 里写 `APP_VERSION=0.1.1` 而包是 0.1.2 ——
运行期被覆盖后健康检查对外报 0.1.1，而旧版 `--check` 完全看不见该漂移
（它只比对前三处）。故把 `.env.example` 也纳入校验。

注意：**不校验本地 `.env`**（个人文件、已 gitignore），
其字段漂移由 `agentix.config.selfcheck` 在启动时告警。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Windows 默认 GBK 控制台打不出 ✅/❌ 等字符，会 UnicodeEncodeError 非零退出
# （tests/test_p35_config_governance.py 的 --check 回归因此在 zh-CN Windows 上必红）。
# 强制 stdout/stderr 走 UTF-8，失败则降级为 replace，绝不因打印崩溃。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - reconfigure 在部分环境不可用
        pass

PYPROJECT = ROOT / "pyproject.toml"
PKG_INIT = ROOT / "agentix" / "__init__.py"
WEB_PKG = ROOT / "web" / "package.json"
ENV_EXAMPLE = ROOT / ".env.example"

# SemVer 2.0.0（含可选预发布/构建元数据）
SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-((?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)"
    r"(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?"
    r"(?:\+([0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*))?$"
)


def _read_pyproject_version() -> str:
    text = PYPROJECT.read_text(encoding="utf-8")
    # 只匹配 [project] 段内的 version（避免误伤 tool 段）
    section = text.split("[project]", 1)[1]
    m = re.search(r'^version\s*=\s*"([^"]+)"', section, re.MULTILINE)
    if not m:
        raise SystemExit(f"[bump] 无法在 {PYPROJECT} 的 [project] 段找到 version")
    return m.group(1)


def _read_init_version() -> str:
    text = PKG_INIT.read_text(encoding="utf-8")
    m = re.search(r'^__version__\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if not m:
        raise SystemExit(f"[bump] 无法在 {PKG_INIT} 找到 __version__")
    return m.group(1)


def _read_web_version() -> str:
    data = json.loads(WEB_PKG.read_text(encoding="utf-8"))
    return data["version"]


def _read_env_example_version() -> str | None:
    """读取 `.env.example` 中的 APP_VERSION；文件不存在或未声明则返回 None。

    本地 `.env` **不纳入**校验（个人文件、gitignore），
    其字段集合漂移由 `agentix.config.selfcheck` 在启动时告警。
    """
    if not ENV_EXAMPLE.exists():
        return None
    m = re.search(
        r"^APP_VERSION\s*=\s*\"?([^\"\s#]+)\"?",
        ENV_EXAMPLE.read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    return m.group(1) if m else None


def collect() -> dict[str, str]:
    versions = {
        "pyproject.toml": _read_pyproject_version(),
        "agentix/__init__.py": _read_init_version(),
        "web/package.json": _read_web_version(),
    }
    env_version = _read_env_example_version()
    if env_version is not None:
        versions[".env.example"] = env_version
    return versions


def check() -> int:
    versions = collect()
    unique = set(versions.values())
    if len(unique) == 1:
        print(f"[bump] ✅ 版本一致: {unique.pop()}")
        return 0
    print("[bump] ❌ 版本号不一致：")
    for path, ver in versions.items():
        print(f"    {path:<24} {ver}")
    print("\n  修复：python scripts/bump_version.py <version>")
    return 1


def _write_pyproject(new: str) -> None:
    text = PYPROJECT.read_text(encoding="utf-8")
    head, sep, tail = text.partition("[project]")
    tail_new = re.sub(r'(^version\s*=\s*")[^"]+(")', rf"\g<1>{new}\g<2>", tail, count=1, flags=re.MULTILINE)
    PYPROJECT.write_text(head + sep + tail_new, encoding="utf-8")


def _write_init(new: str) -> None:
    text = PKG_INIT.read_text(encoding="utf-8")
    text_new = re.sub(r'(^__version__\s*=\s*")[^"]+(")', rf"\g<1>{new}\g<2>", text, count=1, flags=re.MULTILINE)
    PKG_INIT.write_text(text_new, encoding="utf-8")


def _write_web(new: str) -> None:
    data = json.loads(WEB_PKG.read_text(encoding="utf-8"))
    data["version"] = new
    WEB_PKG.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _write_env_example(new: str) -> bool:
    """同步 `.env.example` 的 APP_VERSION；文件不存在则跳过。"""
    if not ENV_EXAMPLE.exists():
        return False
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    text_new, n = re.subn(
        r"(^APP_VERSION\s*=\s*)\"?[^\"\s#]+\"?",
        rf"\g<1>{new}",
        text,
        count=1,
        flags=re.MULTILINE,
    )
    if n:
        ENV_EXAMPLE.write_text(text_new, encoding="utf-8")
    return bool(n)


def bump(new: str) -> int:
    if not SEMVER_RE.match(new):
        raise SystemExit(f"[bump] 非法 SemVer 版本号: {new!r}（期望 MAJOR.MINOR.PATCH[-prerelease]）")
    _write_pyproject(new)
    _write_init(new)
    _write_web(new)
    if _write_env_example(new):
        print(f"[bump] ✅ 已同步四处版本号 → {new}")
        for path in ("pyproject.toml", "agentix/__init__.py", "web/package.json", ".env.example"):
            print(f"    {path}")
    else:
        print(f"[bump] ✅ 已同步三处版本号 → {new}")
        for path in ("pyproject.toml", "agentix/__init__.py", "web/package.json"):
            print(f"    {path}")
        print("    （未找到 .env.example 或未声明 APP_VERSION，已跳过）")
    print("\n  后续：更新 CHANGELOG.md 的 [Unreleased] 段并打标签")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="My Agentix 版本号同步")
    parser.add_argument("version", nargs="?", help="新的版本号（SemVer）")
    parser.add_argument("--check", action="store_true", help="仅校验一致性")
    args = parser.parse_args(argv)

    if args.check:
        return check()
    if not args.version:
        parser.print_help()
        return 1
    return bump(args.version)


if __name__ == "__main__":
    sys.exit(main())
