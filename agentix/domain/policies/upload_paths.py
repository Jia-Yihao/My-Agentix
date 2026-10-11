"""上传路径的包含性判定（纯函数，零第三方依赖）。

**为什么要单独抽出来**：`agentix` 里有三处需要回答同一个问题——
"这个客户端给的路径，是不是在允许的根目录里？"

1. ``interfaces/http/v1/upload.py``：上传落盘、下载读取；
2. ``application/agents/file_handler.py``：文件问答要 ``read_text`` 之前；
3. ``application/agents/image_handler.py``：图片处理要 ``Image.open`` 之前。

三处分属 interfaces 与 application 两层。按架构守卫（test_p17 规则 3/4），
application 不得 import interfaces，所以"用 interfaces 里的 `_safe_resolve`"
这条路走不通。放到 ``domain/policies`` 是唯一同时满足分层与复用位置——
它天然是"给定输入 → 决策"的纯判定，正好是本包的定义。

**为什么这里的判定必须用路径语义而不是字符串前缀**：``str.startswith``
会把 ``/app/uploads_evil`` 判成"在 ``/app/uploads`` 里"。
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["resolve_within_root", "is_within_root"]


def resolve_within_root(base: str | Path, candidate: str | Path) -> Path | None:
    """把 ``candidate`` 解析到绝对路径；若落在 ``base`` 之内则返回它，否则返回 ``None``。

    调用方拿到 ``None`` 必须**拒绝**（fail-closed），而不是"退回去用原路径"。

    ⚠️ 两条不能省的细节：

    1. **必须先 ``resolve()``**。``Path.is_relative_to`` 是纯词法比较，
       未解析时 ``<root>/../../etc/x`` 依然"看起来"在 root 里
       （``Path('a/b/../../x').is_relative_to('a')`` 为真）。
       真正干活的是 ``resolve()``——它才把 ``..`` 与符号链接消掉。
    2. **不能只用 ``str.startswith``**。那会把前缀相同的兄弟目录
       （``/app/uploads`` vs ``/app/uploads_evil``）误判为"在里面"。

    注意本函数**只做包含性判定**，不做文件名净化（去分隔符/控制字符）。
    净化由调用方按各自场景处理——例如"上传落盘"需要构造一个安全文件名，
    而"读已存在的路径"只需要判定它能不能读。
    """
    if candidate is None or str(candidate).strip() == "":
        return None
    try:
        root = Path(base).resolve()
    except OSError:
        return None
    try:
        target = Path(candidate).resolve()
    except OSError:
        return None
    return target if target.is_relative_to(root) else None


def is_within_root(base: str | Path, candidate: str | Path) -> bool:
    """``resolve_within_root`` 的布尔形态（读起来更顺的调用点用它）。"""
    return resolve_within_root(base, candidate) is not None
