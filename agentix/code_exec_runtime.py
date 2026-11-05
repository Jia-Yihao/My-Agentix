"""运行时写代码执行——**墙内侧**运行时（只允许标准库）。

这个模块会被**墙内子进程** import：``agentix/tools/subprocess_worker.py`` 按
``module`` + ``qualname`` 重建 handler 时，重建的就是本模块的
:func:`run_python_code`。所以本文件的 **import 面本身就是安全边界的一部分**：

- **只 import 标准库**：import ``agentix.config.settings`` 会把宿主机 ``.env``
  里的密钥读进墙里——那正是这道墙要挡住的东西；
- **不 import ``agentix.tools.*``**：那一层会把整套业务依赖（DB 驱动、loguru、
  LLM SDK）一起拉进来，"资源闸先于业务代码"这条承诺当场失效；
- **不 import ``resource``**（模块级）：Windows 没有这个模块，模块级 import 会让
  本文件在 Windows 上根本导不进来。它在 :func:`sandbox_gates_active` 的函数体
  内 import。

两道闸**各自独立**（约定 6 ①：白名单只覆盖它声明过的那一个维度）：

1. **位置闸**（:func:`in_sandbox_wall`）：当前解释器是不是墙内进程。判据用
   ``sys.argv[0]`` 的 basename——**刻意不用环境变量**：``.env`` 里加一行就能伪造
   一个"我在墙里"的环境变量，而 argv[0] 的 basename 不是配置能伪造的。
2. **资源闸**（:func:`sandbox_gates_active`）：``RLIMIT_AS`` / ``RLIMIT_CPU`` /
   ``RLIMIT_FSIZE`` 三道闸**回读**之后是否真的在生效。回读而不是"我调用过
   ``setrlimit``"——``setrlimit`` 可能被更高优先级的既有下限夹住，回读值才是
   真实边界。

两道闸都不成立 ⇒ **fail-closed**（抛异常，绝不退回进程内执行）：没有墙时执行
用户代码，等于把用户代码放进了 Agent 宿主进程。
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import os
import shutil
import signal
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, Tuple

__all__ = [
    "RLIMIT_AS_MB",
    "RLIMIT_CPU_S",
    "RLIMIT_FSIZE_MB",
    "MAX_CODE_CHARS",
    "MAX_OUTPUT_BYTES",
    "MAX_OUTPUT_CHARS",
    "DEFAULT_TIMEOUT_S",
    "MIN_TIMEOUT_S",
    "MAX_TIMEOUT_S",
    "WALL_ENTRY_NAME",
    "in_sandbox_wall",
    "sandbox_gates_active",
    "parent_death_signal_supported",
    "run_python_code",
]

#: 资源闸的**声明值**——全仓唯一一份定义处。``agentix/tools/sandbox.py`` 从这里
#: 反向 import，墙内 worker 按同一组数字落闸：两侧各写一份，迟早只关一道。
RLIMIT_AS_MB = 512
RLIMIT_CPU_S = 30
RLIMIT_FSIZE_MB = 32

#: 代码正文上限（65 536 个**字符**）：源码进墙内 worker 时要走一遍 JSON 信封，
#: 正文体积得在入口先有一道闸。协议层另有一道**字节**闸（``MAX_REQUEST_BYTES``
#: = 1 MiB，见 ``agentix/tools/sandbox.py`` 的请求体积校验）——两道闸各管各的
#: 量纲（约定 6 ①：闸只覆盖它声明过的那个维度）：入口这道按**字符数**拒，
#: 最坏情形（4 字节/字符 ⇒ 256 KiB）也仍在协议层的字节预算之内。
MAX_CODE_CHARS = 64 * 1024

#: 输出**字节**上限：单次执行回灌给模型/落进轨迹的 stdout+stderr 总量有界。
MAX_OUTPUT_BYTES = 64 * 1024

#: 输出**字符**上限：字节上限挡住"一个字符占 4 字节"那一面，字符上限挡住
#: "全是多字节字符"那一面。只设一道，另一面就漏了。
MAX_OUTPUT_CHARS = 20_000

#: 代码级超时（秒）：缺省值 / 夹紧区间。
DEFAULT_TIMEOUT_S = 20.0
MIN_TIMEOUT_S = 1.0
MAX_TIMEOUT_S = 120.0

#: 墙内入口脚本名：``in_sandbox_wall`` 的判据（argv[0] 的 basename）。
WALL_ENTRY_NAME = "subprocess_worker.py"

#: 杀完之后 reap 的等待上限。它**不是**业务超时，只保证"我发起了 kill"之后
#: 不会永远卡在 reap 上——真到点也不管了，kill 已经发出去了。
REAP_TIMEOUT_S = 5.0

#: 输出被截断时的尾标记：让读的人知道"这不是全部"，而不是把半截输出当结论。
TRUNCATION_MARK = "\n…[output truncated]"

#: ``prctl(PR_SET_PDEATHSIG, ...)`` 的第一个参数（Linux）。
_PR_SET_PDEATHSIG = 1

#: SIGKILL 的编号：Windows 上 ``signal.SIGKILL`` 不存在，取不到就用 9
#: （那一支本来就不会被调用，但常量不该在 import 期炸）。
_SIGKILL = getattr(signal, "SIGKILL", 9)

#: 本进程是不是 POSIX（资源闸、进程组、``prctl`` 都是 POSIX 专属）。
_IS_POSIX = os.name == "posix"


def _load_libc() -> Any | None:
    """取 libc 并把 ``prctl`` 的签名钉好；取不到就返回 ``None``。

    为什么必须在**模块加载时**取好：``preexec_fn`` 在 fork 之后、exec 之前运行，
    此时再 ``ctypes.CDLL(...)`` 会在子进程里做一次 dlopen——fork 之后带着
    loader 锁做 dlopen 有死锁风险（父进程那一刻正持有那把锁）。所以这里在模块
    级取一次，``preexec_fn`` 里只调用**已加载好的函数对象**。

    macOS 没有 ``prctl`` ⇒ 返回 ``None``（静默降级，见
    :func:`_set_parent_death_signal` 的说明）。
    """
    if not _IS_POSIX:
        return None
    try:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        libc.prctl.argtypes = [
            ctypes.c_int,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_ulong,
        ]
        libc.prctl.restype = ctypes.c_int
    except (OSError, AttributeError):
        return None
    return libc


#: 已加载好的 libc（Windows / macOS 上为 ``None``）。
_LIBC = _load_libc()


def in_sandbox_wall() -> bool:
    """当前解释器是不是沙箱墙内进程。

    判据：``sys.argv[0]`` 的 basename == :data:`WALL_ENTRY_NAME`。

    **刻意不用环境变量做标记**：环境变量是配置，``.env`` 里加一行就能伪造一个
    "我在墙里"——那等于把隔离的判据交给被隔离方保管。argv[0] 的 basename 不是
    配置能伪造的。
    """
    if not sys.argv:
        return False
    try:
        return Path(sys.argv[0]).name == WALL_ENTRY_NAME
    except (OSError, ValueError):  # pragma: no cover - 极端 argv 形态
        return False


def _declared_limits() -> Tuple[Tuple[str, int], ...]:
    """三道闸的 ``(名字, 声明值)``：AS / FSIZE 按字节，CPU 按秒。"""
    return (
        ("RLIMIT_AS", RLIMIT_AS_MB * 1024 * 1024),
        ("RLIMIT_CPU", RLIMIT_CPU_S),
        ("RLIMIT_FSIZE", RLIMIT_FSIZE_MB * 1024 * 1024),
    )


def sandbox_gates_active() -> Tuple[bool, str]:
    """判定墙内两道闸是否都成立，返回 ``(ok, reason)``。

    ``ok is False`` 时 ``reason`` 说明**缺哪一道**（失败要可观察，不吞原因）。
    判定顺序刻意是"平台 → 位置 → 资源闸"：平台都不对时，后面两条没有意义。
    """
    if not _IS_POSIX:
        return (False, "subprocess tier is POSIX-only")
    if not in_sandbox_wall():
        return (
            False,
            "not running inside the sandbox wall; refusing to execute code without "
            "the declared isolation",
        )
    try:
        import resource
    except ImportError:  # pragma: no cover - POSIX 上恒有
        return (False, "resource module unavailable")
    for name, declared in _declared_limits():
        target = getattr(resource, name, None)
        if target is None:  # pragma: no cover - 该平台没有这道闸
            return (False, f"{name} not in effect (unsupported on this platform)")
        try:
            soft, _hard = resource.getrlimit(target)
        except (ValueError, OSError) as exc:
            return (False, f"{name} not in effect (getrlimit failed: {exc})")
        # 回读值才是真实边界："我调用过 setrlimit"不是证据（可能被既有下限夹住）。
        if soft == resource.RLIM_INFINITY or soft > declared:
            return (False, f"{name} not in effect (soft={soft}, declared<={declared})")
    return (True, "gates active")


def _child_env(workdir: str) -> Dict[str, str]:
    """被隔离代码的**最小环境**：显式构造，不继承父进程环境。

    为什么**不**继承：父进程环境里有宿主机 ``.env`` 带下来的密钥（连接串、
    API key）。被隔离代码不需要它们，而"能读到"本身就是泄露面。

    为什么**不**放任何 ``PYTHON*``：``-I``（isolated 模式）会忽略它们——放了
    等于对自己撒谎（"我显式控制了环境"，实际有一整族变量被忽略）。

    ``HOME`` / ``TMPDIR`` 指到工作目录：被隔离代码要写临时文件时，写在自己的
    目录里，而不是宿主的 ``/tmp``。
    """
    return {
        "PATH": os.defpath,
        "HOME": workdir,
        "TMPDIR": workdir,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }


def _set_parent_death_signal() -> None:
    """POSIX：让孙进程在墙内 worker 死亡时立刻被杀（``PR_SET_PDEATHSIG``）。

    为什么必须有：墙被上层超时杀掉时，上层的 ``killpg`` 只覆盖**墙自己的进程
    组**，而孙进程 ``start_new_session`` 自成一组 ⇒ 够不着它 ⇒ 一次超时会留一个
    孤儿在机器上跑（而资源闸挡不住无人回收的进程）。

    取不到 libc 时**静默降级**（靠 worker 侧 finally 兜底）：这一条是"锦上
    添花"，缺了它不该让整个功能不可用。

    ``prctl`` 的**返回值不被检查**：调用失败（内核拒绝 / 权限不足）同样会被
    ``contextlib.suppress`` 吞掉 ⇒ 这一条是「**尽力而为**」而不是「**保证**」，
    兑现与否取决于运行时那一次调用是否真的成功。
    """
    if _LIBC is None:
        return
    # 降级而不是抛：取不到就靠 worker 侧 finally 兜底（contextlib.suppress 表达
    # 的正是"这一条是锦上添花，缺了不该让整个功能不可用"）。
    with contextlib.suppress(Exception):  # noqa: BLE001 - 降级路径：失败只丢这一条加固
        _LIBC.prctl(_PR_SET_PDEATHSIG, _SIGKILL, 0, 0, 0)


def parent_death_signal_supported() -> Tuple[bool, str]:
    """本平台**有没有能力**兑现「墙被杀 ⇒ 孙进程随之被杀」这条承诺。

    返回 ``(ok, reason)``；``ok is False`` 时 reason 说清**缺哪一样**（失败要可观察，
    不能是空串）。

    **"有没有能力" ≠ "兑现了没有"**：判据只是「``prctl`` 这个符号在不在」，
    而 :func:`_set_parent_death_signal` **丢弃 ``prctl`` 的返回值**、异常被
    ``contextlib.suppress`` 吞掉 ⇒ 探测说"可以设"时，运行时那一次调用仍可能
    静默失败（失败即静默降级，靠 worker 侧兜底）。所以本函数回答的是
    「**平台有没有这个能力**」，不是「**这次兑现了没有**」。

    判据取自**执行点自己**——模块加载期取好的 ``_LIBC``（有没有 ``prctl``）。
    ``prctl`` 是 Linux 独有的：macOS 上 ``libc.so.6`` 取不到 ⇒
    :func:`_set_parent_death_signal` 静默降级 ⇒ 这条承诺在 macOS 上**本就不成立**
    （口径 §5 明写"Linux 才有"）。

    为什么单列一个公开探测口：守卫的 skip 判据必须与执行点**同源**（约定 6 ④）。
    在别处另写一份"是不是 Linux"的判断，判点一分家就会出现"守卫在容器里被数成 0 条"
    那类形态——判据分家之后，两边各说各话，谁也不知道自己测的是不是同一条承诺。
    """
    if not _IS_POSIX:
        return (
            False,
            "not a POSIX platform: prctl(PR_SET_PDEATHSIG) is unavailable, so the "
            "parent-death signal cannot be set at all",
        )
    if _LIBC is None:
        return (
            False,
            "libc.prctl is unavailable (libc.so.6 could not be loaded): "
            "PR_SET_PDEATHSIG cannot be set, so the parent-death signal silently "
            "degrades away. libc.so.6 is the glibc soname: on musl-based images "
            "(e.g. Alpine) it cannot be loaded, so the parent-death signal is "
            "conservatively treated as unsupported and degrades silently",
        )
    return (
        True,
        "libc.prctl is available: PR_SET_PDEATHSIG can be requested on the "
        "user-code process (the prctl return value is not checked; a runtime "
        "failure degrades silently and relies on the worker's own cleanup)",
    )


def _kill_process_group(proc: Any) -> None:
    """杀掉被隔离代码的**整个进程组**（不是只杀直接子进程）。

    ``start_new_session=True`` 让它自成组长，于是它自己再 fork 出来的东西也在
    同一个组里——只杀直接子进程会留下一棵还活着的子树。组杀失败时回落
    ``proc.kill()``：两条路只关一条，就是"取消了但墙里还在跑"。
    """
    if proc is None or proc.returncode is not None:
        return
    killed = False
    if _IS_POSIX:
        with contextlib.suppress(OSError, AttributeError):
            os.killpg(os.getpgid(proc.pid), _SIGKILL)
            killed = True
    if not killed:  # pragma: no cover - 非 POSIX 或组杀失败
        with contextlib.suppress(OSError, AttributeError):
            proc.kill()


def _read_capped(path: Path) -> Tuple[str, bool]:
    """读一份输出文件：字节上限 + 字符上限，超限即截断并置标记。

    读不到（文件被清掉 / 权限问题）⇒ 返回空串而不是抛：输出是**观测面**，
    读不到不该把一次已经跑完的执行报成失败（但也不假装有输出）。
    """
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_OUTPUT_BYTES + 1)
    except OSError:
        return "", False
    truncated = False
    if len(raw) > MAX_OUTPUT_BYTES:
        raw = raw[:MAX_OUTPUT_BYTES]
        truncated = True
    text = raw.decode("utf-8", errors="replace")
    if len(text) > MAX_OUTPUT_CHARS:
        text = text[:MAX_OUTPUT_CHARS]
        truncated = True
    return text, truncated


def _resolve_timeout(raw: Any) -> float:
    """取代码级超时：取不到用缺省；转 float 失败即拒；随后**夹到**区间内。

    夹紧而不是拒绝：超时是"最多跑多久"的安全边界，调用方给个 999 只是不知道
    上限——夹住既守住了边界，又不必让模型因为一个数字重试。
    """
    if raw is None:
        return DEFAULT_TIMEOUT_S
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ValueError("timeout_s must be a number") from None
    if value != value:  # NaN：比较恒假，先于夹紧把它挑出来
        raise ValueError("timeout_s must be a number")
    return max(MIN_TIMEOUT_S, min(MAX_TIMEOUT_S, value))


async def run_python_code(arguments: Dict[str, Any]) -> Dict[str, Any]:
    """在墙内再起一个**独立进程**跑用户代码，返回可 JSON 序列化的结果字典。

    必须是**模块级函数**：墙内 worker 按 ``module`` + ``qualname`` 重建 handler，
    闭包 / lambda 在另一个进程里不存在（见 ``agentix/tools/sandbox.py`` 的
    ``_describe_handler``）。

    输出**不接管道**而是各写一个临时文件：stdout 上有协议信封（带哨兵），
    用户的输出混进去既是伪造面也是噪声面。文件读回来还有体积闸。
    """
    ok, reason = sandbox_gates_active()
    if not ok:
        # fail-closed：绝不退回进程内执行——那等于把用户代码放进宿主进程。
        raise RuntimeError(f"code execution refused: {reason}")

    code = arguments.get("code")
    if not isinstance(code, str):
        raise ValueError("code must be a string")
    if not code.strip():
        raise ValueError("code must be a non-empty string")
    if len(code) > MAX_CODE_CHARS:
        raise ValueError(f"code exceeds {MAX_CODE_CHARS} chars")
    timeout_s = _resolve_timeout(arguments.get("timeout_s"))

    workdir = tempfile.mkdtemp(prefix="agentix-code-")
    script_path = Path(workdir) / "main.py"
    stdout_path = Path(workdir) / "stdout.log"
    stderr_path = Path(workdir) / "stderr.log"
    proc: Any = None
    timed_out = False
    exit_code: int | None = None
    stdout_text = ""
    stderr_text = ""
    stdout_truncated = False
    stderr_truncated = False
    truncated = False
    try:
        script_path.write_text(code, encoding="utf-8")
        # 输出文件用 with 打开：``with`` 块一退出，父进程这两份句柄就关了——
        # 子进程拿到的是自己的副本（dup），父进程留着只会让"输出文件还被打开"
        # 这件事变得无法判断。
        with open(stdout_path, "wb") as stdout_file, open(stderr_path, "wb") as stderr_file:
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                "-I",  # 隔离模式：忽略 PYTHON* 与用户 site-packages
                "-u",  # 无缓冲：输出随写随落盘
                "-X",
                "utf8",  # UTF-8 模式：输出不因区域编码而丢字
                str(script_path),
                cwd=workdir,
                env=_child_env(workdir),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=stdout_file,
                stderr=stderr_file,
                start_new_session=_IS_POSIX,  # 自成一个进程组，超时时能整组杀
                preexec_fn=_set_parent_death_signal if _IS_POSIX else None,
            )
        try:
            await asyncio.wait_for(asyncio.shield(proc.wait()), timeout=timeout_s)
        except asyncio.TimeoutError:
            timed_out = True
            _kill_process_group(proc)
            with contextlib.suppress(Exception):  # noqa: BLE001 - reap 尽力而为
                await asyncio.wait_for(proc.wait(), timeout=REAP_TIMEOUT_S)
        exit_code = proc.returncode
        stdout_text, stdout_truncated = _read_capped(stdout_path)
        stderr_text, stderr_truncated = _read_capped(stderr_path)
        truncated = stdout_truncated or stderr_truncated
    finally:
        # 双保险：无论正常结束、超时还是抛异常，都再杀一次进程组并清掉工作目录。
        _kill_process_group(proc)
        shutil.rmtree(workdir, ignore_errors=True)

    if stdout_truncated:
        stdout_text += TRUNCATION_MARK
    if stderr_truncated:
        stderr_text += TRUNCATION_MARK
    return {
        # 被信号杀掉时是负数（如 -9 = SIGKILL）——如实返回，不折成 0，
        # 否则"被杀"与"正常退出"在下游看起来一模一样。
        "exit_code": exit_code,
        "stdout": stdout_text,
        "stderr": stderr_text,
        "timed_out": timed_out,
        "truncated": truncated,
        "timeout_s": timeout_s,
    }
