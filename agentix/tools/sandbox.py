"""``ExecutionSandbox`` 端口的 infrastructure 实现（§24.3 T2 ⑭，ADR-0007）。

⚠️ **前两档不构成隔离，第三档构成——但只在 POSIX 上，而且只挡住它声明过的那几个维度**：

- :class:`InProcessSandbox` —— 与 §24.2 G1 之前的现状逐位相同（含
  ``TOOL_SYNC_OFFLOAD_ENABLED`` 的那条下放分支，全项目只有这一份实现）；
- :class:`ThreadSandbox` —— 无条件把 handler 丢进 ``asyncio.to_thread``。买到的是
  「别占住循环」，买不到的是「坏了波及多远」：同进程、同 GIL、同一套
  ``os.environ`` 与文件权限——所以它**不是**安全边界，只是资源边界的雏形；
- :class:`SubprocessSandbox` —— §24.3 T2 ⑮ 的落点，默认**关**
  （``TOOL_SANDBOX_SUBPROCESS_ENABLED=false`` ⇒ 这一档仍然"在词汇表里但跑不了"，
  与 ⑭ 落地时逐位等价）。开着时买到的是：独立地址空间、``RLIMIT_AS``/``RLIMIT_CPU``/
  ``RLIMIT_FSIZE``、环境变量白名单（与 G2 同一份判据）、超时可**真杀**、输出体积有闸，
  以及**应答必须带本次调用的 nonce、且 ``ok`` 必须是严格布尔，才作数**（墙内有 stdout
  写权限，见
  :meth:`SubprocessSandbox._parse_envelope` 的四条规则）。
  **另外买一件，但有前置**：进程数闸 ``RLIMIT_NPROC`` 只在 ``TOOL_SANDBOX_SUBPROCESS_UID``
  配了一个**独立且非 0** 的 uid 时落下——那道闸按整个 uid 计数而不是按子树计数，与父进程
  共用 uid 时它要么假拦要么形同虚设，而 uid 0 被内核豁免（持有 CAP_SYS_RESOURCE 就跳过
  进程数检查，容器内实测 soft=5 照样连起 12 个进程）；判据与理由见
  ``subprocess_worker.nproc_gate_plan``。没落时墙里会回传 ``nproc_gate.applies=false``
  并响亮告警一次，不静默。⚠️ 反过来也别把"落了"读成"挡住了"：内核按**任务（线程也算）**
  计费而不是按进程，容器里能看到的两个计数都只是**下限**（那份计数跨整机，不认 PID
  命名空间；实测 soft=8 时先起 7 个线程，fork 预算就归 0），所以这一档要求独立 uid 是
  "整个内核上独占"的独立 uid。
  买不到的也必须写在这句话里：**文件权限与出网能力不变**（同 uid 能读的还是能读，
  只读根 / 指定可写目录 / 网络默认拒绝属 ⑰），所以它是"墙"，但不是"笼子"。

``container`` 是 T3 ⑰ 的落点，**这里没有实现**。
:func:`resolve_sandbox` 对它抛 :class:`~agentix.domain.tool.sandbox.SandboxUnavailable`。
「词汇表」与「当前真能跑的集合」的差集必须**可见**：那正是 ADR-0007
要被推翻时用的接口。``subprocess`` 在差集里**与否**由 flag 决定——这是纪律 1
「新开关默认关 + flag-off 逐位等价」在阶梯上的具体形态。
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import logging
import os
import secrets
import signal
import sys
from pathlib import Path

#: ``subprocess`` 档的三道常驻资源闸（``RLIMIT_AS`` / ``RLIMIT_CPU`` / ``RLIMIT_FSIZE``）
#: **以 ``agentix.code_exec_runtime`` 为唯一来源**（第四道进程数闸 ``RLIMIT_NPROC`` 条件生效：
#: 仅 ``TOOL_SANDBOX_SUBPROCESS_UID`` 配成**独立且非 0** 的 uid 时才落，默认 ``-1`` ⇒ 不落只告警，
#: 理由见模块 docstring 与 ``subprocess_worker.nproc_gate_plan``）：
#: 墙内侧的代码执行运行时与父侧的下发/探测必须引用同一份数字——两份拷贝一旦
#: 漂移，"墙里落的闸"与"运维看到的闸"就不是同一道闸，而这种漂移在出事那天
#: 才看得见。**被 import 的那个模块**（``agentix.code_exec_runtime``）模块级只 import
#: 标准库 ⇒ 不会把业务平台拖进墙里。
from agentix.code_exec_runtime import RLIMIT_AS_MB, RLIMIT_CPU_S, RLIMIT_FSIZE_MB
from agentix.config import settings
from agentix.domain.tool.exec_env import scrub_env
from agentix.domain.tool.sandbox import (
    SANDBOX_DEFAULT,
    SANDBOX_LADDER,
    ExecutionSandbox,
    Handler,
    SandboxUnavailable,
)
from agentix.tools import subprocess_worker as _worker

__all__ = [
    "SANDBOX_LADDER",
    "SandboxUnavailable",
    "SubprocessSandboxError",
    "invoke_in_process",
    "InProcessSandbox",
    "ThreadSandbox",
    "SubprocessSandbox",
    "probe_subprocess_caps",
    "implemented_kinds",
    "resolve_sandbox",
]

logger = logging.getLogger(__name__)

#: ``subprocess`` 档的资源闸（**首批不给成旋钮**：它们定义的是这一档承诺的隔离强度，
#: 不是部署偏好。要按工具调宽，请先把它做成 Settings 键并补纪律 4 的五处同步。）
#: 这三个值以 ``agentix.code_exec_runtime`` 为唯一来源（见顶部 import 处的说明），
#: 名字与取值保持不变 ⇒ ``from agentix.tools.sandbox import RLIMIT_AS_MB`` 仍然可用。
#: 进程数闸的上限。**只在配置了独立且非 0 的 uid 时才落**（判据见
#: :func:`agentix.tools.subprocess_worker.nproc_gate_plan`，父进程探测与子进程落闸共用那一份；
#: uid 0 被内核豁免，落了也不生效，所以那一支同样不落）。
#: 取 32 的理由：合法工具在墙里"起一个子进程再等它退出"是常态（2~3 个），32 给了一整个
#: 数量级的余量；而 fork 炸弹的价值恰恰在于**它必须能指数增长**，被钉在 32 个任务上（进程
#: 或线程，内核按任务计费）就只是"多占了一点内存"，不再是"把机器拖死"。再小会误伤合法工具，
#: 再大就挡不住扩散。
RLIMIT_NPROC = 32

#: 「这道闸没落下」的告警去重集：按 reason 去重，一次进程生命周期里同一缺口只响一条。
#: 刷屏的下场不是"烦"，是下一批人把那行日志删掉——于是痕迹和噪声一起消失。
_GAP_WARNINGS: set[str] = set()

#: 跨进程边界的两个体积闸。请求侧由这里的 ``MAX_REQUEST_BYTES`` 定；**应答侧**由子进程
#: 侧的 ``subprocess_worker.MAX_PAYLOAD_BYTES``（8 MiB）定，信封本体两侧同一尺寸。
#: 父进程读 stdout 的上限刻意留 **2×** 余量（见 ``run`` 里的 ``_drain``）：stdout 上除了
#: 信封还可能有被隔离代码自己打的日志，留等值就会把"业务代码打了个长日志"误判成
#: "应答超限"，而真正的应答超限由子进程侧改写失败信封，父进程照样读得到。
MAX_REQUEST_BYTES = 1024 * 1024
MAX_STDERR_CHARS = 4096

#: 杀完之后 reap 的等待上限。它**不是**业务超时（端口不管超时），只保证"我发起了
#: 取消"之后不会永远卡在 reap 上——真到点也不管了，孤儿的 kill 已经发出去了。
REAP_TIMEOUT_S = 5.0


async def invoke_in_process(handler: Handler, arguments: dict) -> object:
    """G1 的那条口径，全项目唯一一份（``ToolExecutor._invoke`` 也走这里）。

    同步 handler 在自己的栈帧里跑时事件循环被占住，外层 ``asyncio.wait_for``
    根本没有机会取消它——``timeout_ms`` 形同虚设。打开 ``TOOL_SYNC_OFFLOAD_ENABLED``
    后把它下放线程，循环空得出来，超时才真生效。
    """
    if settings.TOOL_SYNC_OFFLOAD_ENABLED and not asyncio.iscoroutinefunction(handler):
        outcome = await asyncio.to_thread(handler, arguments)
        if asyncio.iscoroutine(outcome):
            outcome = await outcome
        return outcome
    outcome = handler(arguments)
    if asyncio.iscoroutine(outcome):
        outcome = await outcome
    return outcome


class InProcessSandbox:
    """默认档：在调用它的那个事件循环里 await handler（= 今天的生产行为）。"""

    __slots__ = ()
    kind = SANDBOX_DEFAULT

    async def run(self, handler: Handler, arguments: dict) -> object:
        return await invoke_in_process(handler, arguments)


class ThreadSandbox:
    """``thread`` 档：非协程 handler 一律下放线程，协程 handler 就地 await。

    与 ``in_process`` + ``TOOL_SYNC_OFFLOAD_ENABLED=true`` 的区别只有一件事：
    下放不再由**全局 flag** 决定，而是由**这条工具的声明**决定。这正是阶梯里
    ``thread`` 那一档存在的意义——为「这个 handler 会占住循环」单独表态，
    而不是为了它把全实例的循环亲和语义都改掉。
    """

    __slots__ = ()
    kind = "thread"

    async def run(self, handler: Handler, arguments: dict) -> object:
        if asyncio.iscoroutinefunction(handler):
            return await handler(arguments)
        outcome = await asyncio.to_thread(handler, arguments)
        # 类形式的 async ``__call__`` 不是 iscoroutinefunction(实例)，但在 worker
        # 线程里造出来的协程对象仍然回到本循环 await——语义与 in_process 一致。
        if asyncio.iscoroutine(outcome):
            outcome = await outcome
        return outcome


class SubprocessSandboxError(RuntimeError):
    """子进程侧以信封回报的失败（``stage`` 区分闸/重建/执行/序列化）。

    与 :class:`SandboxUnavailable` 的分工：**档位本身跑不了**才用后者（执行点终局、
    不进重试）；"这一条工具在这面墙里没跑成"用本类，落普通 failed 结果。
    """


def _repo_root() -> str:
    """子进程要能 ``import agentix``：仓库根 = 本文件上两级（``agentix/tools/sandbox.py``）。"""
    return str(Path(__file__).resolve().parents[2])


def _worker_entrypoint() -> Path:
    """墙内侧入口的**绝对文件路径**（与本文件同目录）。

    启动形态选脚本路径而不是 ``python -m agentix.tools.subprocess_worker``，理由见
    ``SubprocessSandbox.run`` 里的注释：``-m`` 会先把整套业务包拉进子进程，既违反
    「资源闸先于业务代码」又让每次调用多付 ~600ms。路径按 ``__file__`` 解析，因此
    与 ``_repo_root()`` 一样不依赖安装位置。
    """
    return Path(__file__).resolve().with_name("subprocess_worker.py")


def probe_subprocess_caps() -> dict:
    """能力探测（⑯ 的先声）：**这一档承诺的隔离维度能不能真落下去**。

    判据刻意与 ``subprocess_worker.apply_gates`` 同源——父进程用探测决定"要不要拒绝"，
    子进程用真值决定"到底落到了什么"。两处判据不同源就会出现"探测说能跑、进去跑不动"
    或更糟的"探测说能跑、进去静默跳过资源闸"。

    ``nproc_gate`` 是**进程数闸的状态**，不是 ``runnable`` 的一部分：那一道闸只在配置了独立
    且非 0 的 uid 时兑现（判据同样是 worker 那一份，不在这里另算一次——包括"uid 0 被内核
    豁免所以不落"那一支），而"没配独立 uid"从来不在这一档
    的承诺里，所以它**不进 ``missing``**——混进去的后果是"开了 flag 但没配 uid"的机器整档被拒，
    那是行为变更，不是加固。
    """
    import importlib.util

    posix = os.name == "posix"
    resource_ok = posix and importlib.util.find_spec("resource") is not None
    missing = []
    if not resource_ok:
        missing.append("resource.setrlimit(RLIMIT_AS/CPU/FSIZE)")
    if not posix:
        missing.append("process-group kill (start_new_session)")
    return {
        "posix": posix,
        "resource": resource_ok,
        "setuid": hasattr(os, "setuid"),
        "runnable": not missing,
        "missing": missing,
        # 两本账都留 None：判定不依赖它们（依赖就会让"探测结论"随 /proc 可读性漂移），
        # 真需要计数的地方是子进程落闸那一刻，那里现测。
        "nproc_gate": _worker.nproc_gate_plan(
            uid=int(settings.TOOL_SANDBOX_SUBPROCESS_UID),
            cap=RLIMIT_NPROC,
            uid_process_count=None,
            uid_task_count=None,
        ),
    }


def _describe_handler(handler: Handler) -> tuple[str, str]:
    """把 handler 翻成 ``(module, qualname)``；跨不过进程边界的形态在这里就拒。

    拒的是四类真实事故：闭包/lambda（``qualname`` 里有 ``<``，进程外不存在）、
    绑定方法（丢了 ``self`` 就等于换了个实现）、``__main__`` 里的对象（只有父进程
    解释器认识它）、**非函数对象**（类形式的 ``async __call__`` 按名字重建出来的是
    **类**本身，``Cls(arguments)`` 是在新建对象而不是在跑那个工具）。
    这里拒比让子进程拒更省：一次进程生命周期都不用起。
    """
    module = getattr(handler, "__module__", None)
    qualname = getattr(handler, "__qualname__", None)
    if getattr(handler, "__self__", None) is not None:
        raise SubprocessSandboxError(
            "reconstruct: bound method handlers lose their instance across the process "
            "boundary; declare a module-level handler for sandbox='subprocess'"
        )
    if not module or not qualname:
        raise SubprocessSandboxError(
            f"reconstruct: handler {type(handler).__name__} exposes no module/qualname "
            "(lambdas, partials and callables built at runtime cannot be rebuilt in a child process)"
        )
    if module == "__main__":
        raise SubprocessSandboxError("reconstruct: handler lives in __main__ and is not importable")
    if "<" in qualname:
        raise SubprocessSandboxError(
            f"reconstruct: handler {qualname!r} is a lambda/nested function/closure and cannot be rebuilt"
        )
    if not (inspect.isfunction(handler) or inspect.isbuiltin(handler)):
        raise SubprocessSandboxError(
            f"reconstruct: handler {module}.{qualname} is a {type(handler).__name__}, not a "
            "module-level function; the subprocess tier rebuilds by name, and rebuilding a "
            "class/instance would call the constructor instead of the tool"
        )
    return str(module), str(qualname)


class SubprocessSandbox:
    """``subprocess`` 档：一次调用一个进程 + 资源闸 + 环境白名单 + 可杀超时。

    **不管超时**（端口的既有边界：截断仍由 executor 外层的 ``pooled_wait_for`` 负责），
    但与前两档不同的是：这里的取消**真的能停下被隔离的代码**——协程被取消时本类
    杀掉整个进程组并 reap，不靠 handler 自己让出 GIL（§24.2 G1 那条"超时形同虚设"
    在这一档不成立）。
    """

    __slots__ = ()
    kind = "subprocess"

    @staticmethod
    async def _drain(stream, *, limit: int, what: str) -> str:
        """带上限地读子进程输出：超上限 ⇒ 杀掉并报错，而不是把父进程内存吃满。"""
        if stream is None:
            return ""
        chunks: list[bytes] = []
        size = 0
        while True:
            piece = await stream.read(64 * 1024)
            if not piece:
                break
            size += len(piece)
            if size > limit:
                raise SubprocessSandboxError(
                    f"{what}_too_large: child exceeded {limit} bytes; output gate tripped"
                )
            chunks.append(piece)
        return b"".join(chunks).decode("utf-8", errors="replace")

    @staticmethod
    async def _feed(stdin, payload: str) -> None:
        if stdin is None:
            return
        stdin.write(payload.encode("utf-8"))
        await stdin.drain()
        stdin.close()

    @staticmethod
    async def _kill_and_reap(proc) -> None:
        """取消/异常路径上的清场：杀**进程组**（不是只杀直接子进程）再 reap。

        ``start_new_session=True`` 让子进程自成组长，于是它自己再 fork 出来的东西
        也在同一个组里——只 ``proc.kill()`` 会留下一棵还活着的子树，那是这面墙最不该
        有的漏（资源闸挡不住无人回收的进程）。
        """
        if proc.returncode is not None:
            return
        killed = False
        if os.name == "posix":
            with contextlib.suppress(Exception):
                # 组杀失败（进程已退出 / pid 被复用）时**必须**回落 proc.kill()：
                # 两条路只关一条，就是"取消了但墙里还在跑"。
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                killed = True
        if not killed:  # pragma: no cover - 非 POSIX 或组杀失败
            with contextlib.suppress(Exception):
                proc.kill()
        with contextlib.suppress(BaseException):
            # reap 尽力而为：拿不到 reap 也不能用它改写原始失败（原始异常由调用方继续抛）。
            # shield 是因为这里通常正处在被取消的上下文里，裸 await 会被同一个取消打断。
            await asyncio.wait_for(asyncio.shield(proc.wait()), timeout=REAP_TIMEOUT_S)

    async def run(self, handler: Handler, arguments: dict) -> object:
        caps = probe_subprocess_caps()
        if not caps["runnable"]:
            raise SandboxUnavailable(
                "execution sandbox kind 'subprocess' cannot honour its declared isolation on "
                f"this platform (missing: {', '.join(caps['missing'])}); refusing to run instead "
                "of silently downgrading to in-process (ADR-0007 ⑮; see §24.3)"
            )

        module, qualname = _describe_handler(handler)
        uid = int(settings.TOOL_SANDBOX_SUBPROCESS_UID)
        if uid >= 0 and not caps["setuid"]:
            raise SandboxUnavailable(
                f"execution sandbox kind 'subprocess' cannot drop to uid {uid} on this platform "
                "(os.setuid unavailable); refusing to run at the parent's privilege instead of "
                "silently ignoring the declared TOOL_SANDBOX_SUBPROCESS_UID"
            )
        request = {
            "protocol": _worker.PROTOCOL_VERSION,
            # 端口签名是 `run(handler, arguments)`，**没有"工具名"这个入参**，
            # 而 qualname 是这一层唯一给出的调用标识，所以 `tool_name` 装的是它。
            # 这个字段只用于排障（信封里回显），**不参与任何判定**——判据在 nonce 上。
            "tool_name": qualname,
            "module": module,
            "qualname": qualname,
            "arguments": arguments,
            "limits": {
                "as_mb": RLIMIT_AS_MB,
                "cpu_s": RLIMIT_CPU_S,
                "fsize_mb": RLIMIT_FSIZE_MB,
                # 进程数闸的值**总是**下发：落不落由子进程按 uid 判（同一份判据）。
                # 在父进程这里"不配 uid 就不发"是第二个判点，两处漂移的代价是
                # "墙里的闸与运维看到的闸不是同一道"。
                "nproc": RLIMIT_NPROC,
            },
            "uid": uid,
            # 每次调用现生成的应答归属凭证（协议 v2）。墙内的业务代码有 stdout 的写权限，
            # 而信封就写在 stdout 上——没有这个 nonce，`print("@@AGENTIX_SANDBOX@@ {ok:true}")`
            # 就能凭空造出一条"成功结果"，父进程照单全收。见 worker 模块 docstring。
            "nonce": secrets.token_hex(16),
        }
        try:
            payload = json.dumps(request, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            # 参数编不出 JSON 是**调用方的错**，且在这里就注定失败：不起进程、不占资源闸，
            # 直接把可预见的失败说清楚（否则要等一次进程生命周期 + 子进程 import 才知道）。
            raise SubprocessSandboxError(f"request_not_json_serializable: {exc}") from exc
        if len(payload.encode("utf-8")) > MAX_REQUEST_BYTES:
            raise SubprocessSandboxError(
                f"request_too_large: arguments exceed {MAX_REQUEST_BYTES} bytes on the wire"
            )

        root = _repo_root()
        env = scrub_env(
            project_root=root,
            extra_keys=settings.TOOL_SANDBOX_SUBPROCESS_ENV_EXTRA_KEYS,
        )
        # 信封是 `ensure_ascii=False` 的 JSON：子进程 stdout 若按**区域编码**落地
        # （中文 Windows 默认 cp936），一条含非 GBK 字符的失败信封就会在写出时
        # UnicodeEncodeError，于是"墙里发生了什么"只剩 exit code。显式钉 UTF-8，
        # 而不是信任被裁过的环境里恰好有 PYTHONIOENCODING。
        env["PYTHONIOENCODING"] = "utf-8"
        # 启动形态是刻意的**脚本路径**而不是 ``python -m agentix.tools.subprocess_worker``：
        # ``-m`` 会先执行 ``agentix/__init__`` 与 ``agentix/tools/__init__``（后者把整套业务
        # 工具、DB 驱动、loguru 都拉起来——容器内实测 747ms），于是"资源闸先于业务代码"这条
        # 承诺在 import 阶段就破了，而且墙里跑一个 handler 要先付一整套平台的代价。
        # 按脚本路径起则进程边界前只加载标准库（worker 模块级判据由 p125 的 AST 用例钉住）。
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            str(_worker_entrypoint()),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=root,
            start_new_session=caps["posix"],
        )
        try:
            stdout_text, stderr_text, _ = await asyncio.gather(
                self._drain(proc.stdout, limit=_worker.MAX_PAYLOAD_BYTES * 2, what="stdout"),
                self._drain(proc.stderr, limit=MAX_STDERR_CHARS, what="stderr"),
                self._feed(proc.stdin, payload),
            )
            rc = await proc.wait()
        except BaseException:
            # 上层超时/取消、体积闸触发、任何异常：墙里不能留孤儿。
            await self._kill_and_reap(proc)
            raise
        if rc != 0:
            # **退出码非 0 就一律不采信信封**（2026-10-25 独立审查的严重项）。
            # worker 所有写出信封的分支都 `return 0`，所以 rc≠0 意味着它根本没写完协议——
            # 此时 stdout 上任何带哨兵的行都只能是被隔离代码自己印的。旧写法是
            # `if rc != 0 and SENTINEL not in stdout_text`，于是"先印一条 ok 信封、再被
            # RLIMIT_CPU 打成 SIGKILL"这一串能把伪造的载荷当**成功结果**返回，crash 消失。
            # 这一档自己卖的就是那三道常驻资源闸（外加条件生效的进程数闸，见模块 docstring），
            # 闸生效反而让伪造得逞是不可接受的形态。
            forged_hint = (
                "（stdout 里有哨兵行，但 rc≠0 ⇒ 不采信）" if _worker.SENTINEL in stdout_text else ""
            )
            raise SubprocessSandboxError(
                f"worker crashed (exit {rc}){forged_hint}; stderr tail: {stderr_text[-512:]!r}"
            )
        envelope = self._parse_envelope(stdout_text, rc, stderr_text, request["nonce"])
        if envelope.get("ok") is not True:
            # **严格布尔**，不是真值判断（2026-10-25 第 2 轮独立审查点名的绕过路径）。
            # 真值写法 ``not envelope.get("ok")`` 会把 ``"ok": 1``（JSON 数字）当成成功：
            # ``not 1`` 是 False，于是这条信封被当**成功结果**返回，而归属凭证在这一路上
            # 从未执行过——nonce 校验再严也拦不住一个"压根没进 trusted 桶"的值。
            # 这是解析器之外的第二道闸：解析器自己拒这类信封（见 ``_parse_envelope``），
            # 但"最后一道关卡自己松了"必须被独立钉住，否则把它改回去不会有任何用例变红。
            stage = envelope.get("stage", "unknown")
            raise SubprocessSandboxError(
                f"[{stage}] {envelope.get('error', 'no error given')}"
                + (f"; stderr tail: {stderr_text[-512:]!r}" if stderr_text else "")
            )
        self._log_caps(envelope.get("caps") or {})
        return envelope.get("data")

    @staticmethod
    def _parse_envelope(stdout_text: str, rc: int, stderr_text: str, nonce: str) -> dict:
        """从混杂 stdout 里取出**这次调用**的信封。

        四条规则，每条都对应一个真实形态（2026-10-25 独立审查点名后定稿）：

        1. **成功必须 nonce 匹配**：墙内代码有 stdout 写权限，随手印一条
           ``SENTINEL + {"ok": true, ...}`` 就能凭空造结果；孙进程晚写一行同理。
           nonce 只在这次调用的请求里，业务代码拿不到（要偷得先读 ``__main__`` 的帧
           局部变量——那是 ⑰ 的威胁模型，本档的口径是"墙内 handler 由平台注册"）。
        2. **失败信封不校验 nonce**：失败只会让调用更响，不会伪造出结果；协议阶段就错时
           worker 确实还拿不到 nonce，硬校验会把真实原因（"缺 key""协议版本不符"）盖成
           "没有应答"。**代价要说清**：墙内代码可以印一条伪造的失败信封**顶替**真实失败
           原因（只影响诊断，不产生数据）——真实原因会被盖住，但绝不会凭空多出结果。
        3. **取匹配者里的最后一条**，而不是取第一条：真信封写在业务日志之后，
           先匹配先得会读到噪声。

        第四条是**严格布尔**（2026-10-25 第 2 轮独立审查点名的绕过路径）：``ok`` 只认
        ``True`` / ``False`` 两个值。``1`` / ``1.0`` / ``"true"`` / 缺失一律按**畸形
        信封**拒掉，**刻意不归拢进失败桶**——失败桶的信封会被父进程按"失败"读出去，
        而"失败"的判定若用真值，``"ok": 1`` 就会被真值判成"成功"，归属凭证在这条路上
        压根没执行过。worker 自己只会写布尔值（``ok: True`` / ``ok: False``），
        所以非布尔的信封只可能是墙内代码印的。
        """
        candidates = [
            line for line in stdout_text.splitlines() if line.startswith(_worker.SENTINEL)
        ]
        if not candidates:
            raise SubprocessSandboxError(
                f"no response envelope from worker (exit {rc}); stderr tail: {stderr_text[-512:]!r}"
            )
        parsed: list[dict] = []
        first_defect = ""
        for line in candidates:
            body = line[len(_worker.SENTINEL) :].strip()
            try:
                envelope = json.loads(body)
            except ValueError as exc:
                first_defect = first_defect or f"malformed response envelope: {exc}"
                continue
            if not isinstance(envelope, dict) or envelope.get("protocol") != (
                _worker.PROTOCOL_VERSION
            ):
                first_defect = first_defect or "response protocol mismatch"
                continue
            parsed.append(envelope)
        trusted = [e for e in parsed if e.get("ok") is True and e.get("nonce") == nonce]
        if trusted:
            return trusted[-1]
        if any(e.get("ok") is True for e in parsed):
            raise SubprocessSandboxError(
                "untrusted success envelope: no response carries this call's nonce; a "
                "sentinel line without the request nonce can only have been printed by the "
                "code inside the wall, so it is not treated as a result "
                f"(stderr tail: {stderr_text[-512:]!r})"
            )
        failures = [e for e in parsed if e.get("ok") is False]
        if failures:
            return failures[-1]
        # ``ok`` 既不是 ``True`` 也不是 ``False``（``1`` / ``1.0`` / ``"true"`` / 缺失）：
        # worker 自己只写布尔值（见 ``subprocess_worker`` 的 ``ok: True`` / ``ok: False``），
        # 所以这种信封只可能是墙内代码印的。按**畸形信封**拒——**不能**归拢进失败桶：
        # 失败桶的信封会被父进程按"失败"读出去，而"失败"的判定用的是真值，
        # ``"ok": 1`` 于是被真值判成"成功"，归属凭证在这一路上从未被执行过。
        non_boolean = [e for e in parsed if e.get("ok") is not True and e.get("ok") is not False]
        if non_boolean:
            raise SubprocessSandboxError(
                "malformed response envelope: 'ok' must be a boolean, got "
                f"{non_boolean[-1].get('ok')!r}; a sentinel line whose 'ok' is not a "
                "boolean can only have been printed by the code inside the wall, so it is "
                f"not treated as a result (stderr tail: {stderr_text[-512:]!r})"
            )
        raise SubprocessSandboxError(
            f"{first_defect or 'no parseable response envelope'} "
            f"(exit {rc}; stderr tail: {stderr_text[-512:]!r})"
        )

    @staticmethod
    def _log_caps(caps: dict) -> None:
        """把子进程**回读到的**真实边界打出来——"我调过 setrlimit"不是证据，回读才是。

        顺带把"这一档没落下的维度"报响一次：``nproc_gate.applies is False`` 是墙里自己
        回传的判定，不是父进程猜的。纪律 2：观测失败绝不影响主链路，所以整段自吞。
        """
        try:
            applied = caps.get("rlimit_applied") or {}
            logger.info(
                "subprocess sandbox executed: pid=%s uid=%s resource=%s rlimits=%s nproc_gate=%s",
                caps.get("pid"),
                caps.get("uid"),
                caps.get("resource_module_available"),
                {k: v.get("effective") for k, v in applied.items()},
                (caps.get("nproc_gate") or {}).get("applies"),
            )
            gate = caps.get("nproc_gate")
            # 只认墙里**明确说没落**的那一支：`gate` 缺失（假子进程、旧信封）不等于缺口，
            # 否则观测面的缺位会被读成"这道闸没落下"，又是一次把 None 当证据。
            if isinstance(gate, dict) and gate.get("applies") is False:
                reason = str(gate.get("reason") or "no reason reported")
                if reason not in _GAP_WARNINGS:
                    _GAP_WARNINGS.add(reason)
                    logger.warning(
                        "subprocess sandbox dimension not enforced: RLIMIT_NPROC stayed off "
                        "because %s",
                        reason,
                    )
        except Exception:  # noqa: BLE001 - 观测降级，不改结果
            pass


#: 档位名 → 单例（无状态，进程级共享；``__slots__ = ()`` 保证它真的没有状态）
_SANDBOXES: dict[str, ExecutionSandbox] = {
    InProcessSandbox.kind: InProcessSandbox(),
    ThreadSandbox.kind: ThreadSandbox(),
}

#: ``subprocess`` **不进** ``_SANDBOXES``：它由 ``TOOL_SANDBOX_SUBPROCESS_ENABLED``
#: 单独决定（默认关）。纪律 1 的形态是"关着的时候，阶梯上这一格仍然是空的"——
#: 所以它必须在两处查询点（``implemented_kinds`` / ``resolve_sandbox``）都被 flag 挡住，
#: 而不是把实现塞进注册表再指望调用方不去拿它。
_SUBPROCESS_SANDBOX = SubprocessSandbox()


def subprocess_tier_enabled() -> bool:
    """这一档当前是否"在注册表里"——**每次调用都读 flag**（热切换语义与
    ``EXECUTION_SANDBOX_ENABLED`` 一致；把它缓存成 import 期常量，就等于给门禁
    装了个"改了不生效"的开关）。"""
    return bool(settings.TOOL_SANDBOX_SUBPROCESS_ENABLED)


def implemented_kinds() -> tuple[str, ...]:
    """当前**真能跑**的档位（与 ``SANDBOX_LADDER`` 的差集就是 T2 ⑮/⑰ 的余量）。

    ⚠️ 这里的"能跑"是**注册意义上的能跑**：flag 开着只说明"这一档有实现并且会被
    解析到"，不代表本机真能把资源闸落下去——后者由 :func:`probe_subprocess_caps`
    在执行点判定，探测不过仍然 ``SandboxUnavailable``。差集因此可能同时出现
    "flag 开 + 本机跑不了"，这不是矛盾，是两个不同问题（声明可达性 vs 平台能力）。
    """
    kinds = set(_SANDBOXES)
    if subprocess_tier_enabled():
        kinds.add(SubprocessSandbox.kind)
    return tuple(sorted(kinds))


def resolve_sandbox(kind: str) -> ExecutionSandbox:
    """把声明的档位翻成实现；不可用就抛，**绝不静默退回 in_process**。

    两类不可用分开措辞，因为修法不同：``not_in_ladder`` 是拼写错/词汇表外，
    ``not_implemented`` 是声明了一个还没落地的隔离档位（此时该改声明，或开
    ``TOOL_SANDBOX_SUBPROCESS_ENABLED``）；第三类 ``unavailable_on_platform`` 由
    :meth:`SubprocessSandbox.run` 在执行点抛——档位有实现，但这台机器落不下它
    承诺的闸，那也不是"退回进程内"的理由。
    """
    sandbox = _SANDBOXES.get(kind)
    if sandbox is not None:
        return sandbox
    if kind == SubprocessSandbox.kind and subprocess_tier_enabled():
        return _SUBPROCESS_SANDBOX
    if kind not in SANDBOX_LADDER:
        raise SandboxUnavailable(
            f"unknown execution sandbox kind {kind!r}: not in the ladder "
            f"({', '.join(SANDBOX_LADDER)})"
        )
    if kind == SubprocessSandbox.kind:
        raise SandboxUnavailable(
            f"execution sandbox kind {kind!r} is in the ladder but its implementation is "
            "switched off (TOOL_SANDBOX_SUBPROCESS_ENABLED=false; §24.3 T2 ⑮, ADR-0007); "
            f"implemented kinds: {', '.join(implemented_kinds())}"
        )
    raise SandboxUnavailable(
        f"execution sandbox kind {kind!r} is in the ladder but not implemented "
        "(§24.3 T3 ⑰; see ADR-0007); implemented kinds: "
        f"{', '.join(implemented_kinds())}"
    )
