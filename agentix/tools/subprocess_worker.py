"""``subprocess`` 沙箱档的子进程侧入口（§24.3 T2 ⑮，ADR-0007）。

**这个文件是"墙"的内侧**：父进程用 ``python <本文件绝对路径>`` 把它作为一个**独立脚本**
拉起来（不是 ``python -m``——那样会先执行 ``agentix/__init__`` 与 ``agentix/tools/__init__``，
整套业务依赖抢在资源闸之前进场），双方只通过 stdin 的一个 JSON 请求与 stdout 的一行带哨兵的
JSON 应答通信。

三条设计约束（都不是风格选择）：

1. **先落资源闸，再 import 业务代码**。模块级只 import 标准库；``resource.setrlimit``
   与降权都在 ``apply_gates()`` 里完成，之后才 ``importlib`` 目标 handler 所在模块。
   顺序反了的话，被挡住的应该是「导入期就能吃满内存」的那一类，而那恰恰是最贵的一类
   （纪律 6 ①：语法闸与资源闸各自独立存在；这里是"资源闸先于一切被隔离的代码"）。
2. **handler 必须能按 ``module`` + ``qualname`` 重建**。进程内注册的闭包 / lambda /
   绑定方法跨不过进程边界（不可 pickle 的是**执行体**，不是数据），所以这一档只服务
   "模块级可导出"的 handler；重建不出来就是响亮失败，**绝不退回父进程内执行**。
3. **返回值必须可 JSON 序列化**。这是进程边界的一部分，不是打分口径；不可序列化 ⇒
   ``stage=serialize`` 的失败信封，由父进程落成 ``ToolResult(success=False)``。

协议信封写在**带哨兵的一行**里（``SENTINEL + ' ' + json``），因为被隔离的业务代码
可能往 stdout 打日志——父进程按哨兵取那一条，而不是"读第一行"。

**但"按哨兵取"本身不够**（2026-10-25 独立审查指出的形态）：墙内任何一句
``print(SENTINEL + ' {"ok": true, ...}')`` 都能凭空造出一条"应答"来。所以协议 v2 起，
父进程每次调用下发一个 ``nonce``，**成功信封必须原样回显它**；父进程只认 nonce 匹配的那条
``ok=true``。这挡住的是"随手污染 stdout / 伪造结果"这一整类（包括业务代码自己打的日志恰好
带哨兵、孙进程晚写一行）；**它挡不住**铁了心读取 ``__main__`` 帧局部变量偷 nonce 的敌意
handler——那属 ⑰ 的威胁模型，本仓的口径是"墙内的 handler 由平台注册、不是敌意代码"，
诚实的边界写进 ``docs/architecture/运行时护栏.md`` §15.10 与 README §16。

协议 v2 的**第二道**是 ``ok`` 必须是**严格布尔**（``True`` / ``False``）：``1`` / ``1.0`` /
``"true"`` / 缺失一律按**畸形信封**拒。worker 自己只写布尔值（下面 ``ok: True`` /
``ok: False`` 两处），所以非布尔的信封只可能是墙内代码印的——父进程不把它归进失败桶，
因为失败桶的判定一旦用真值，``"ok": 1`` 就会被判成"成功"，归属凭证在这条路上压根没执行过。
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import inspect
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

#: 协议版本；父进程校验不一致直接拒绝解释。
#: v1→v2（2026-10-25）：请求新增必填 ``nonce``，成功应答必须回显——**接受规则**变了，
#: 所以版本号一并升。两侧永远同树（父进程按绝对路径起本文件），不存在兼容负担；
#: 留旧版本号反而会让"旧 worker + 新父进程"这种开发期错配静默通过协议校验。
PROTOCOL_VERSION = 2

#: 应答行哨兵（父进程据此从混杂 stdout 里取出唯一那条信封）。
SENTINEL = "@@AGENTIX_SANDBOX@@"

#: 子进程侧对**应答体积**的上限。父进程也有同尺寸的上限，两处都用同一个常量，
#: 免得"父进程肯收 8 MiB 而子进程肯写 64 MiB"这种只关一条的错配。
MAX_PAYLOAD_BYTES = 8 * 1024 * 1024

#: 失败信封里允许带回的 stderr 尾巴长度（诊断用；不是全量转述）。
STDERR_TAIL_CHARS = 2000


class WorkerError(Exception):
    """带 ``stage`` 的失败：让父进程能区分「闸落不下 / 重建不出来 / 跑炸了 / 序列化不了」。"""

    def __init__(self, stage: str, message: str) -> None:
        super().__init__(f"[{stage}] {message}")
        self.stage = stage
        self.message = message


def read_request(stdin=None) -> Dict[str, Any]:
    """从 stdin 读一条 JSON 请求；不做任何"宽容"兜底（协议错就是错）。

    ``nonce`` 是 v2 起的必填项：父进程每次调用现生成一个，成功信封必须原样回显。
    没有它，"按哨兵取应答"就等于把**伪造应答的笔**交给墙内的业务代码。
    """
    stream = stdin if stdin is not None else sys.stdin
    raw = stream.readline()
    if not raw:
        raise WorkerError("protocol", "empty stdin: no request envelope")
    try:
        request = json.loads(raw)
    except ValueError as exc:
        raise WorkerError("protocol", f"malformed request envelope: {exc}") from exc
    if not isinstance(request, dict) or request.get("protocol") != PROTOCOL_VERSION:
        raise WorkerError("protocol", f"protocol mismatch (want {PROTOCOL_VERSION})")
    for key in ("tool_name", "module", "qualname", "arguments", "limits", "nonce"):
        if key not in request:
            raise WorkerError("protocol", f"request missing key {key!r}")
    if not isinstance(request["arguments"], dict) or not isinstance(request["limits"], dict):
        raise WorkerError("protocol", "arguments/limits must be objects")
    if not isinstance(request["nonce"], str) or not request["nonce"]:
        raise WorkerError("protocol", "nonce must be a non-empty string")
    return request


def apply_gates(limits: Dict[str, Any], uid: int) -> Dict[str, Any]:
    """落资源闸（RLIMIT_*）+ 可选降权，并把**回读到的真实值**记进 caps 作为证据。

    读回而不是只写"我调用了 setrlimit"：``setrlimit`` 在某些平台上会被更高优先级
    的既有下限夹住（例如容器的 ``RLIMIT_NPROC``），回读值才是子进程真正拿到的边界。

    **进程数闸（``RLIMIT_NPROC``）排在降权之后**，这个顺序是判据的一部分而不是风格：
    那道闸计的是"调用者所属 real uid 现在有多少任务（线程也算任务）"，降权前测到的是
    父进程 uid 的计数（一台开发机上通常是几百），降权后测到的才是墙自己那个 uid 的计数。
    反过来写不会报错，
    只会让 ``no_headroom`` 永远为真——一道"看起来落了、实际在报别的数"的闸比没有闸更糟
    （纪律 6 ④：诊断口径必须与执行链的判定同源）。
    还有一条同源的坑：**降权到 uid 0 不算降权**，那一支由 ``nproc_gate_plan`` 判成"不落"
    （内核豁免 CAP_SYS_RESOURCE），所以这里不能"只要调用过 setrlimit 就当落了"。
    """
    caps: Dict[str, Any] = {
        "platform": os.name,
        "resource_module_available": False,
        "rlimit_applied": {},
        "uid": os.getuid() if hasattr(os, "getuid") else None,
        "euid": os.geteuid() if hasattr(os, "geteuid") else None,
        "pid": os.getpid(),
    }
    try:
        import resource
    except ImportError:
        resource = None  # type: ignore[assignment]
    if resource is None:
        # Windows 没有 resource 模块：这里的失败**不是**"少一个可选加固"，而是这一档
        # 承诺的隔离维度整体缺席。父进程在拉起我之前已经按能力探测拒绝过一轮，
        # 走到这里说明探测与执行不同源——必须响，不能悄悄继续。
        raise WorkerError("limits", "resource module unavailable: rlimits cannot be applied")

    want = {
        resource.RLIMIT_AS: _bytes_of(limits.get("as_mb")),
        resource.RLIMIT_CPU: _seconds_of(limits.get("cpu_s")),
        resource.RLIMIT_FSIZE: _bytes_of(limits.get("fsize_mb")),
    }
    for target, value in want.items():
        if value is None:
            continue
        soft, hard = resource.getrlimit(target)
        new_soft = value if hard in (resource.RLIM_INFINITY,) or value <= hard else hard
        # 收紧到不超过既有硬上限：setrlimit 抬高硬限要 root，夹住时如实记"实际落下的值"。
        try:
            resource.setrlimit(target, (new_soft, hard))
        except (ValueError, OSError) as exc:
            raise WorkerError("limits", f"setrlimit({target}) failed: {exc}") from exc
        caps["rlimit_applied"][resource_name(resource, target)] = {
            "requested": value,
            "effective": resource.getrlimit(target),
        }
    caps["resource_module_available"] = True

    if uid is not None and int(uid) >= 0:
        if not hasattr(os, "setuid"):
            raise WorkerError("limits", f"uid drop requested ({uid}) but os.setuid is unavailable")
        try:
            os.setgroups([])
            os.setgid(int(uid))
            os.setuid(int(uid))
        except (OSError, AttributeError) as exc:
            code = getattr(exc, "errno", None)
            hint = (
                " (uid 降权需要 root；非 root 容器里请设 TOOL_SANDBOX_SUBPROCESS_UID=-1)"
                if code == errno.EPERM
                else ""
            )
            raise WorkerError("limits", f"uid drop to {uid} failed: {exc}{hint}") from exc
        caps["uid"] = os.getuid()
        caps["euid"] = os.geteuid()
        caps["uid_dropped_to"] = int(uid)

    # 进程数闸：降权之后才谈得上（理由见本函数 docstring）。判定在
    # :func:`nproc_gate_plan` 那一份里，父进程的探测与这里落的闸共用它。
    nproc_cap = _count_of(limits.get("nproc"))
    dropped = uid is not None and int(uid) >= 0
    measured_uid = os.getuid() if hasattr(os, "getuid") else None
    ledgers = _scan_uid_ledgers(measured_uid) if dropped else None
    plan = nproc_gate_plan(
        uid=int(uid) if uid is not None else -1,
        cap=nproc_cap,
        # 只在"这道闸真会落"的那一支去读 /proc：不降权时结论与计数无关，
        # 于是默认路径每次调用少一次全 /proc 扫描。两本账由同一次扫描给出——
        # 判定读的是任务数那本，进程数那本只用于措辞（理由见 nproc_gate_plan）。
        uid_process_count=None if ledgers is None else ledgers[0],
        uid_task_count=None if ledgers is None else ledgers[1],
    )
    caps["nproc_gate"] = plan
    if plan["applies"]:
        target = getattr(resource, "RLIMIT_NPROC", None)
        if target is None:
            raise WorkerError(
                "limits",
                f"process-count gate planned for uid {int(uid)} but this platform has no "
                "RLIMIT_NPROC constant: refusing to claim a gate it cannot set",
            )
        soft, hard = resource.getrlimit(target)
        new_soft = nproc_cap if hard in (resource.RLIM_INFINITY,) or nproc_cap <= hard else hard
        try:
            resource.setrlimit(target, (new_soft, hard))
        except (ValueError, OSError) as exc:
            raise WorkerError("limits", f"setrlimit(RLIMIT_NPROC) failed: {exc}") from exc
        caps["rlimit_applied"][resource_name(resource, target)] = {
            "requested": nproc_cap,
            "effective": resource.getrlimit(target),
        }
    return caps


def nproc_gate_plan(
    uid: int,
    cap: Any,
    uid_process_count: Optional[int] = None,
    uid_task_count: Optional[int] = None,
) -> Dict[str, Any]:
    """判定「进程数闸这次到底落不落得下」——全仓唯一的这一句判点。

    为什么**不降权就不落**（这不是保守，是算术）：``RLIMIT_NPROC`` 记的是**整个 real uid
    的进程总数**，不是这个子树的进程数。``uid=-1`` 时墙里墙外同一个 uid，那个计数里已经含着
    登录会话的几百个进程 ⇒ 上限设小会让"工具偶尔起一个子进程"哪天直接 ``EAGAIN``（假拦截，
    换台机器又好了，最难归因的一类）；上限抬到计数之上才不难用，而那时它已经不是约束。
    两种都比"这道闸今天不落"贵，所以落与不落由 uid 决定，而不由"看起来更安全"决定。

    为什么**uid=0 也不落**：内核把 ``RLIMIT_NPROC`` 的检查跳过了 ``CAP_SYS_RESOURCE``
    的持有者，而 uid 0 默认持有它。容器内实测（2026-10-26，`p125_linux`）：root 把
    soft 写成 5 并回读确认，随后照样连起 12 个进程、一次都不阻塞 ⇒ "调过 setrlimit"
    与"闸生效"是两件事，前者不是后者的证据。所以显式配 ``TOOL_SANDBOX_SUBPROCESS_UID=0``
    在这里**明确不落**，而不是落一道只对别人生效的闸。（这不是"内核版本号"判据：
    降权到任何非 0 uid 时能力位随 setuid 一起清掉，"非 0"就是"没有那个豁免"的可用代理。）

    余量判定用**任务数**而不是进程数——内核的计费单位是任务（线程），这在容器里量过
    （2026-10-26，`p125_linux`，uid 60000）：同一个 uid 下"1 个进程起 5 个线程"回读到
    **进程 1 / 任务 6**；再把 ``RLIMIT_NPROC`` soft 设为 cap、先起线程后 fork，
    (cap, 预起线程, fork 成功) = (8, 0, 7) / (8, 4, 3) / (8, 7, 0) / (16, 10, 5)——
    fork 预算恰好等于 `cap − 当时的任务数`，而那一刻的**进程数始终是 1**。
    所以只看进程数会报"余量充足"，而墙里第 0 次 fork 就 ``EAGAIN``：那种形态最难归因，
    所有观测面都说闸落好了，只有实际业务报错。两个计数都回传，判定取可见值的较大者。

    ``uid_process_count`` / ``uid_task_count`` 只影响**余量与措辞**，不影响 ``applies``：
    判定不许依赖 ``/proc`` 可不可读（否则两台机器上同一个配置得到不同结论，运维看到的
    闸状态就不再等于墙里的）。两个数都是**本 PID 命名空间可见的下限**，见
    :func:`count_uid_processes` 与 :func:`count_uid_tasks`。
    """
    try:
        uid_value = int(uid)
    except (TypeError, ValueError):
        uid_value = -1
    try:
        cap_value: Optional[int] = int(cap) if cap is not None else None
    except (TypeError, ValueError):
        cap_value = None
    shared_shape = {
        "applies": False,
        "cap": cap_value,
        "uid_process_count": uid_process_count,
        "uid_task_count": uid_task_count,
        "no_headroom": False,
    }
    if cap_value is None or cap_value <= 0:
        return {
            **shared_shape,
            "reason": "no positive nproc cap was requested; the process-count gate stays off",
        }
    if uid_value < 0:
        return {
            **shared_shape,
            "reason": (
                "RLIMIT_NPROC is charged to the whole real uid across the kernel (not to this "
                "process subtree and not to this PID namespace); running under the parent's uid "
                "(TOOL_SANDBOX_SUBPROCESS_UID=-1) would compare the cap against every task that "
                "uid already owns - so it either false-blocks a legitimate fork or stops being "
                "a bound. Set TOOL_SANDBOX_SUBPROCESS_UID to a dedicated uid to get the "
                "process-count gate."
            ),
        }
    if uid_value == 0:
        return {
            **shared_shape,
            # 单独一条 reason（不与 uid<0 合并）：把"我配了 root"和"我忘了配 uid"混成
            # 一句话，运维就会去补一个本来已经配了的键，而不是改掉那个真正错的配置。
            "reason": (
                "uid 0 is exempt from RLIMIT_NPROC: the kernel skips the process-count check for "
                "a task holding CAP_SYS_RESOURCE, which uid 0 has by default (measured: soft=5 "
                "written and read back as 5, 12 forks succeeded). A cap charged to uid 0 is "
                "written but never enforced, so the wall declines it - set "
                "TOOL_SANDBOX_SUBPROCESS_UID to a non-zero uid dedicated to the sandbox."
            ),
        }
    # 计费单位是任务（线程）：任一可见计数达到 cap 就按"没有余量"处理（取较大值，None 忽略）。
    counts = [n for n in (uid_process_count, uid_task_count) if n is not None]
    charged = max(counts) if counts else None
    # 要说清报的是哪本账：只有任务数是内核真正计费的那本。任务数量不出来时只能拿进程数当
    # **下界**，沿用"tasks"的措辞会让运维把"这里看到 8 个进程"读成"内核数到 8 个任务"。
    charged_label = "task" if uid_task_count is not None else "process"
    no_headroom = charged is not None and charged >= cap_value
    reason = f"charged to uid {uid_value} (at most {cap_value} tasks holdable by that uid)"
    if charged is None:
        reason += (
            "; that uid's live task/process count was not measurable here (no readable /proc), "
            "so headroom is unknown"
        )
    elif no_headroom:
        reason += (
            f"; that uid already owns {charged} {charged_label}(s) here "
            f"(tasks: {uid_task_count}, processes: {uid_process_count}), so the wall cannot "
            "fork at all - pick a uid nobody else runs under on this kernel"
        )
    else:
        # 下限要说在这条路上，而不是只写在 docstring 里：运维读的是日志。
        # 实测（2026-10-26）：容器里 /proc 数到 uid 65534 为 0 个进程，而 soft=8 的闸在
        # 第 0 次 fork 就 EAGAIN——内核那份计数跨整机，不认 PID 命名空间的墙。
        reason += (
            f"; {charged} {charged_label}(s) of that uid are visible in this PID namespace "
            f"(tasks: {uid_task_count}, processes: {uid_process_count}), a lower bound - the "
            "kernel charges that uid across the whole host and every thread counts, so "
            "no_headroom=false does not guarantee a fork succeeds"
        )
    return {
        "applies": True,
        "cap": cap_value,
        "uid_process_count": uid_process_count,
        "uid_task_count": uid_task_count,
        "no_headroom": no_headroom,
        "reason": reason,
    }


def _scan_uid_ledgers(uid: Any) -> Optional[tuple[int, int]]:
    """一次扫描读某个 uid 的**两本账**：可见进程数与可见任务数（线程也是任务）。

    返回 ``(processes, tasks)``；量不到返回 ``None``，**不返回 (0, 0)**：0 会被下游读成
    "这个 uid 很干净、余量充足"，于是"测量失败"被翻译成"一切正常"。先读一次
    ``/proc/self/status``——它读不到说明这个 /proc 根本不可用（hidepid、非 Linux），
    后面那两个数就不该存在。

    走 ``/proc/<pid>/task/<tid>/status`` 而不是只读 ``/proc/<pid>/status``：后者一个进程
    一条，天生数不出线程。两本账必须同源一次读，否则"进程数"和"任务数"来自两个时刻的
    快照，比错的还难查。
    """
    try:
        target_uid = int(uid)
    except (TypeError, ValueError):
        return None
    if target_uid < 0 or not os.path.isdir("/proc"):
        return None
    try:
        with open("/proc/self/status", "rb") as handle:
            if not handle.read():
                return None
    except OSError:
        return None
    try:
        entries = os.listdir("/proc")
    except OSError:
        return None
    processes = 0
    tasks = 0
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            tids = os.listdir(f"/proc/{entry}/task")
        except OSError:
            continue  # 进程刚好退出 / 无权读：不是"这个 uid 没有进程"，跳过而非计 0
        mine = 0
        for tid in tids:
            try:
                with open(f"/proc/{entry}/task/{tid}/status", "rb") as handle:
                    raw = handle.read()
            except OSError:
                continue
            for line in raw.decode("utf-8", "errors=replace").splitlines():
                if line.startswith("Uid:"):
                    fields = line.split()
                    if len(fields) > 1 and fields[1].isdigit() and int(fields[1]) == target_uid:
                        mine += 1
                    break
        if mine:
            processes += 1
            tasks += mine
    return processes, tasks


def count_uid_processes(uid: Any) -> Optional[int]:
    """数某个 uid 当前有多少**进程**（Linux；线程不计在这里，见 :func:`count_uid_tasks`）。

    ⚠️ 这本账**不是** ``RLIMIT_NPROC`` 计费的那本，所以它不是判定输入，只是可见性措辞。
    实测（2026-10-26，`p125_linux`，uid 60000）：一个进程起 5 个线程 ⇒ 进程数 1、任务数 6；
    而 soft=8 时"预起 7 个线程"就把 fork 预算压到 0（(cap, 预起线程, fork 成功) 四组实测见
    :func:`nproc_gate_plan`）。只按进程数判余量 ⇒ 报"余量充足"，墙里第 0 次 fork 就 ``EAGAIN``。

    三条各自实测过的原因，使这里的数只是**下限**：

    1. 内核那份按 uid 的计数**不认 PID 命名空间**。容器里 /proc 数到 uid 65534 有 0 个
       进程，而同一个 uid 在 ``RLIMIT_NPROC`` soft=8 下第 0 次 fork 就 ``EAGAIN``——
       挤满那份计数的进程住在宿主机上，容器看不见（``/sys/fs/cgroup/pids.max`` 当时是
       ``max``，排除了"cgroup 替我挡了"这种解释）。⇒ 只有**这个 uid 在整个内核上独占**
       时，"余量充足"才成立；共用 uid 时这里报的小数字没有任何保证力。
    2. 计费单位是任务而不是进程（上一条实测就是它的证据）。
    3. 扫描期间进程在进出：某个 /proc 条目读不到就跳过（那是"看不清"，不是"没有"），
       所以同一个数两次跑会漂。取证时先固定环境（重启容器）再量，别拿单次值当基准。
    """
    ledgers = _scan_uid_ledgers(uid)
    return None if ledgers is None else ledgers[0]


def count_uid_tasks(uid: Any) -> Optional[int]:
    """数某个 uid 当前有多少**任务**（Linux：线程也算）——``RLIMIT_NPROC`` 计费的那本账。

    与 :func:`count_uid_processes` 同生共死：同一次扫描的两个读数，同样的"量不到就
    ``None``"，同样的下限性质（跨 PID 命名空间计费、扫描期间会漂）。判定取两本账里
    可见的较大者，所以这里数不到时进程数仍然会把结论托住（保守方向是"报没余量"）。
    """
    ledgers = _scan_uid_ledgers(uid)
    return None if ledgers is None else ledgers[1]


def resource_name(resource_module, target: int) -> str:
    """把 rlimit 常量翻成可读名（失败信封与 caps 都要给人看）。"""
    for name in (
        "RLIMIT_AS",
        "RLIMIT_CPU",
        "RLIMIT_FSIZE",
        "RLIMIT_NPROC",
        "RLIMIT_CORE",
        "RLIMIT_NOFILE",
    ):
        if getattr(resource_module, name, None) == target:
            return name
    return str(target)


def _bytes_of(mb: Any) -> int | None:
    try:
        value = int(mb)
    except (TypeError, ValueError):
        return None
    return value * 1024 * 1024 if value > 0 else None


def _seconds_of(seconds: Any) -> int | None:
    try:
        value = int(seconds)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _count_of(value: Any) -> Optional[int]:
    """进程数上限的取整：``None``/非正 ⇒ 不落闸（与 ``_bytes_of`` 同一条"没有值就别落"）。

    刻意不复用 ``_bytes_of``：那把尺子会乘 1024×1024，而进程数是要**逐字**落进
    ``setrlimit`` 的个数——乘错一次就得到一道形同虚设的闸（32 变 32MiB 个进程）。
    """
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def reconstruct(module: str, qualname: str):
    """按 ``module`` + ``qualname`` 在子进程里重建 handler；重建不出来 ⇒ 响亮失败。

    拒的三种形态各有现实事故：``<locals>`` 是闭包/嵌套函数（进程外不存在）；
    ``__main__`` 是"只在父进程解释器里存在"的对象；取不到属性是声明漂移（模块改名、
    函数被搬走）——三者都不能"那就进程内跑吧"，那等于把安全声明当装饰。
    """
    if not module or module == "__main__":
        raise WorkerError("reconstruct", f"handler module {module!r} is not importable")
    if "<" in qualname:
        raise WorkerError(
            "reconstruct",
            f"handler {qualname!r} is a lambda/closure/local function and cannot be rebuilt "
            "in a child process (declare a module-level handler for sandbox='subprocess')",
        )
    try:
        obj: Any = __import__(module, fromlist=["_"])
    except BaseException as exc:  # noqa: BLE001 - 导入失败必须以信封回传，不能只打栈
        raise WorkerError(
            "reconstruct", f"import {module!r} failed: {type(exc).__name__}: {exc}"
        ) from exc
    for part in qualname.split("."):
        try:
            obj = getattr(obj, part)
        except AttributeError as exc:
            raise WorkerError("reconstruct", f"attribute {part!r} missing in {module!r}") from exc
    if not callable(obj):
        raise WorkerError("reconstruct", f"{module}.{qualname} is not callable")
    # 与父进程侧 `_describe_handler` **同一条**形状判据：按名字重建出来的是类还是别的
    # 对象时，"调用它"就不是在跑那个工具，而是在新建对象。两侧不同源迟早只关一条。
    if not (inspect.isfunction(obj) or inspect.isbuiltin(obj)):
        raise WorkerError(
            "reconstruct",
            f"{module}.{qualname} resolved to a {type(obj).__name__}, not a module-level function",
        )
    return obj


async def _resolve_outcome(value: Any) -> Any:
    """与 ``invoke_in_process`` 同口径：能拿到可 await 的结果就 await。"""
    if asyncio.iscoroutine(value) or hasattr(value, "__await__"):
        return await value
    if hasattr(value, "__aiter__"):
        raise WorkerError(
            "invoke", "handler returned an async generator; the port awaits one value"
        )
    return value


def run_request(request: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """跑一次请求，返回 (应答信封, caps)。任何失败都带 ``stage``。"""
    caps = apply_gates(request["limits"], int(request.get("uid", -1)))
    handler = reconstruct(request["module"], request["qualname"])
    arguments = dict(request["arguments"])
    try:
        outcome = handler(arguments)
        data = asyncio.run(_resolve_outcome(outcome))
    except WorkerError:
        raise
    except BaseException as exc:  # noqa: BLE001 - 业务异常要变成信封，不能让父进程只看到 exit code
        raise WorkerError("invoke", f"{type(exc).__name__}: {exc}") from exc
    return (
        {
            "protocol": PROTOCOL_VERSION,
            "ok": True,
            "nonce": request["nonce"],
            "tool_name": request["tool_name"],
            "data": data,
            "caps": caps,
        },
        caps,
    )


def emit(envelope: Dict[str, Any], stdout=None) -> None:
    """把信封写成一行哨兵前缀 JSON；超过上限就改写成失败信封（体积闸两处同尺寸）。

    超限改写时**照抄原信封的 nonce**：失败信封本来就只许让调用更响，不该因为
    少了回显而被父进程判成"应答不可信"，那会把"结果太大"这个真实原因盖掉。
    """
    stream = stdout if stdout is not None else sys.stdout
    payload = json.dumps(envelope, ensure_ascii=False)
    if len(payload.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        payload = json.dumps(
            {
                "protocol": PROTOCOL_VERSION,
                "ok": False,
                "nonce": envelope.get("nonce"),
                "stage": "serialize",
                "error": f"result larger than MAX_PAYLOAD_BYTES ({len(payload)} bytes)",
            },
            ensure_ascii=False,
        )
    stream.write(f"{SENTINEL} {payload}\n")
    stream.flush()


def failure_envelope(
    stage: str, message: str, stderr_tail: str = "", nonce: Optional[str] = None
) -> Dict[str, Any]:
    """失败信封。``nonce`` 在请求都没解析出来时确实拿不到（协议阶段就错了），留空即可——
    父进程对**失败**信封不校验 nonce，只拿它把失败原因说清楚；校验只对 ``ok=true`` 生效。
    """
    envelope: Dict[str, Any] = {
        "protocol": PROTOCOL_VERSION,
        "ok": False,
        "stage": stage,
        "error": message,
    }
    if nonce:
        envelope["nonce"] = nonce
    if stderr_tail:
        envelope["stderr_tail"] = stderr_tail
    return envelope


def _force_utf8_streams() -> None:
    """把 stdout/stderr 钉成 UTF-8（父进程也传了 `PYTHONIOENCODING`，两处都做）。

    为什么两侧都做：信封是 `ensure_ascii=False` 的 JSON，而区域编码（中文 Windows 的
    cp936）装不下箭头和中文——写出时 `UnicodeEncodeError` 会把"墙里的失败原因"降级成
    一个裸 exit code。协议出口比被隔离的业务代码更该确定，所以它不依赖环境恰好设了什么。
    """
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(Exception):  # noqa: BLE001 - 重配失败仍要继续跑，信封优先
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="replace")


def _pin_sys_path() -> None:
    """按脚本方式启动时，把 ``sys.path`` 的第一位从「本文件所在目录」换成仓库根。

    父进程是以 ``python <绝对路径>/subprocess_worker.py`` 起我的，解释器因此把
    ``agentix/tools/`` 放在 ``sys.path[0]``——那里躺着 ``sandbox.py``、``definitions.py``、
    ``registry_init.py``，handler 里任何一句裸名 ``import definitions`` 都会命中本目录的同名
    文件，而不是它本来要的那个模块。重建 handler 真正需要的是**仓库根**，这里自己反推
    （本文件上两级），不依赖调用方是否补了 ``PYTHONPATH``——两条都靠环境白名单里那一条时，
    漏配 PYTHONPATH 的故障会伪装成「这条工具坏了」，而这里一步就能自愈。
    """
    here = Path(__file__).resolve()
    tools_dir = str(here.parent)
    repo_root = str(here.parents[2])
    if sys.path and str(Path(sys.path[0] or ".").resolve()) == tools_dir:
        sys.path.pop(0)
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)


def main(argv=None) -> int:
    """子进程主入口。返回 0 表示"信封已写出"（内容可能是失败），非 0 表示连信封都没写出来。"""
    del argv  # 协议全在 stdin；命令行只用于定位本文件
    _force_utf8_streams()
    _pin_sys_path()
    try:
        request = read_request()
    except WorkerError as exc:
        emit(failure_envelope(exc.stage, exc.message))
        return 0
    # 请求解析出来之后，失败信封就要带上 nonce：父进程对失败不校验它，但带上能让
    # "这条应答属于这次调用"在排障时可核（尤其 stdout 上还混着别的哨兵行的时候）。
    nonce = request.get("nonce")
    try:
        envelope, _caps = run_request(request)
    except WorkerError as exc:
        emit(failure_envelope(exc.stage, exc.message, nonce=nonce))
        return 0
    except BaseException as exc:  # noqa: BLE001 - 未预期崩溃也要留信封，别让父进程猜退出码
        emit(failure_envelope("crash", f"{type(exc).__name__}: {exc}", nonce=nonce))
        return 0
    try:
        emit(envelope)
    except (TypeError, ValueError) as exc:
        emit(failure_envelope("serialize", f"result_not_json_serializable: {exc}", nonce=nonce))
    return 0


if __name__ == "__main__":
    sys.exit(main())
