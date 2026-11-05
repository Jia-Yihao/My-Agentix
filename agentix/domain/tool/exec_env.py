"""子进程执行环境白名单（§24.3 T2 ⑮ 复用 G2 的那一份判据，ADR-0007）。

为什么单独成模块：``agentix/capabilities/mcp/client.py``（G2：MCP stdio 子进程不继承
密钥）与 ``agentix/tools/sandbox.py`` 的 ``subprocess`` 档要的是**同一条**判据——
「哪些环境变量可以跨过进程边界」。两处各写一份白名单，迟早只关一条（`docs/architecture/运行时护栏.md` §15.9
的同族形态），所以白名单住在这里，两个调用方都从这里取。

分层：本模块**只依赖标准库**，不 import ``agentix`` 的任何其它层，也不读 ``settings``
（extra_keys 由调用方传进来）。``tests/test_p66_arch_guards.py`` 允许
``tools/**`` 与 ``capabilities/**`` 依赖域层，反过来不行。
"""

from __future__ import annotations

import os
from typing import Dict, Iterable, Mapping, Optional

__all__ = ["OS_ENV_ALLOWLIST", "normalize_env_keys", "scrub_env"]

#: 只保留「起进程 + 找到本包 + 正确编码 + TLS 出网」所需的最小集合。
#: ⚠️ 这条白名单**只覆盖环境变量这一个维度**：它挡得住 ``API_KEY`` 之类的密钥流出去，
#: 挡不住子进程用自己的权限读文件、连网络（约定 6 ①：白名单只声明它覆盖的那个维度）。
OS_ENV_ALLOWLIST = frozenset(
    {
        # 进程与解释器本身
        "PATH",
        "PATHEXT",
        "COMSPEC",
        "SYSTEMROOT",
        "WINDIR",
        "TEMP",
        "TMP",
        "PYTHONPATH",
        "PYTHONHOME",
        "PYTHONIOENCODING",
        "PYTHONUTF8",
        "VIRTUAL_ENV",
        # POSIX 运行时
        "HOME",
        "USER",
        "SHELL",
        "LANG",
        "LC_ALL",
        "TZ",
        "TERM",
        "LD_LIBRARY_PATH",
        # 证书与代理（TLS/出网必需）。⚠️ **"不含凭据"这句不能写**：代理 URL 的
        # `http://user:pass@corp:8080` 形态本身就是凭据，一旦被设进父进程环境就会跟着
        # 过界（2026-10-25 独立审查点名）。这里刻意**不**默默改写或剔除该值——那会
        # 悄悄弄坏"经鉴权代理出网"的既有部署（属行为变更，约定 5 要先点头），
        # 也刻意**不**把它留在"白名单不含凭据"这类与代码相反的描述里。
        # 口径登记在 docs/architecture/运行时护栏.md §15.10 与 README §16，
        # 并由 tests/test_p125_subprocess_sandbox.py 的特征化用例钉住现状。
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "NODE_EXTRA_CA_CERTS",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
    }
)


def normalize_env_keys(raw: str | Iterable[str] | None) -> frozenset[str]:
    """把「逗号分隔字符串」或「键集合」统一成大写 frozenset（两个调用方都要做这一步）。"""
    if raw is None:
        return frozenset()
    if isinstance(raw, str):
        return frozenset(key.strip().upper() for key in raw.split(",") if key.strip())
    return frozenset(str(key).strip().upper() for key in raw if str(key).strip())


def scrub_env(
    source: Optional[Mapping[str, str]] = None,
    *,
    extra_keys: str | Iterable[str] | None = None,
    project_root: Optional[str] = None,
    allowlist: frozenset[str] = OS_ENV_ALLOWLIST,
) -> Dict[str, str]:
    """把要交给子进程的环境裁到 ``allowlist ∪ extra_keys``；白名单外的键一律不进子进程。

    两条兜底不是修饰，而是「裁完就起不来」的现实约束，因此留在判据本身里：
    ① ``PATH`` 被裁掉时按原值补回（否则连解释器都找不到）；
    ② 补 ``PYTHONPATH`` 到仓库根（否则 ``import agentix`` 直接失败，而失败会伪装成
    「这条工具坏了」而不是「环境裁过头了」）。
    """
    base = dict(os.environ if source is None else source)
    keep = allowlist | normalize_env_keys(extra_keys)
    scrubbed = {k: v for k, v in base.items() if k.upper() in keep}
    if "PATH" not in scrubbed and "PATH" in base:
        scrubbed["PATH"] = base["PATH"]
    if "PYTHONPATH" not in scrubbed and project_root:
        scrubbed["PYTHONPATH"] = project_root
    return scrubbed
