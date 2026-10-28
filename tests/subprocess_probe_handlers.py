"""``subprocess`` 沙箱档的**子进程侧探针 handler**（tests/test_p125_subprocess_sandbox.py 用）。

为什么单独成模块、而且只用标准库：跨进程边界的 handler 必须能按
``module`` + ``qualname`` 被**另一个解释器**重建（见 `agentix/tools/subprocess_worker.py`），
所以：

1. 它不能住在测试文件里 —— 子进程的系统 `sys.path` 只有仓库根（``-m`` 会把 cwd 放进
   ``sys.path``，环境白名单再把 ``PYTHONPATH`` 补成仓库根），而 pytest 是以
   ``test_p125_subprocess_sandbox`` 这个**顶层模块名**导入测试文件的，子进程按这个名字
   导不回来。放在 ``tests/`` 包路径下，`import tests.subprocess_probe_handlers` 才在两侧
   都成立。
2. 它只 import 标准库 —— 探针要测的是"墙本身"，不该顺带把 ``agentix`` 的配置装配、
   第三方 SDK 拉进子进程；那会让"沙箱起不来"与"某个依赖装不上"两种故障分不开。

每个 handler 都对应一个**独立的断言**，名字即口径。唯一的例外是 ``CallableHandler``：
它不是 handler，而是「名字过得了、形状过不了」那条判据的靶子。
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import threading
import time

#: 探针用的假密钥变量名与值。**刻意不像真密钥**（不含 KEY/SECRET 字样也照样该被裁掉），
#: 判据是"白名单外的键不进子进程"，不是"名字看起来像密钥的键不进子进程"。
PROBE_SECRET_ENV_KEYS = (
    "DATABASE_URL",
    "LLM_API_KEY",
    "TEXT2SQL_DATABASE_URL",
)
PROBE_SECRET_VALUE = "should-never-cross-the-process-boundary"


def echo(args: dict) -> dict:
    """同步 handler：结果与进程内跑必须**逐位相同**（墙不改变返回值，只改变在哪跑）。"""
    return {"echo": args, "pid": os.getpid()}


class CallableHandler:
    """模块级的「可调用类」——父/子两侧**形状判据**的靶子。

    为什么需要它：判据若只看名字（``module`` + ``qualname`` 存在、不含 ``<``），这个类
    全都过得了，但按名字重建出来的是**类对象**，"调用它"是在新建实例而不是在跑那个工具。
    所以两侧都还必须看形状（``inspect.isfunction``）。放在模块级是刻意的：闭包里的
    ``<locals>`` 会先被点名判据拦掉，就撞不到形状判据了。
    """

    def __call__(self, args: dict) -> dict:  # pragma: no cover - 只作为被拒靶子，永不被调用
        return {"echo": args}


async def async_echo(args: dict) -> dict:
    """协程 handler：同一口径的另一条腿（端口对两类 handler 的结果语义一致）。"""
    return {"echo": args, "pid": os.getpid()}


def env_face(args: dict) -> dict:
    """把子进程**真的看见的**环境报回来——白名单的唯一可信证据。"""
    return {
        "visible_keys": sorted(os.environ),
        "secret_values": {k: os.environ.get(k) for k in PROBE_SECRET_ENV_KEYS},
        "has_path": bool(os.environ.get("PATH")),
        "pythonpath": os.environ.get("PYTHONPATH"),
        "cwd": os.getcwd(),
        "argv_module": sys.argv[-1] if sys.argv else "",
    }


def import_face(args: dict) -> dict:
    """报告墙里**此刻的 import 面**：证明资源闸落下去之前没人顺手把整套平台拉起来。

    判据不是"这次调用快了多少毫秒"（那是机器噪声），而是「``agentix.*`` 与第三方重依赖
    有没有出现在 ``sys.modules`` 里」。以脚本路径起 worker 时它们不该在；以
    ``python -m agentix.tools.subprocess_worker`` 起时 ``agentix.tools`` **必然**在——那是
    父包 ``__init__`` 干的，跟 handler 无关，代价是每次调用先付一遍平台装配（实测 +600ms），
    而且"闸先于业务代码"这条承诺当场就破了。
    """
    heavy = ("sqlalchemy", "redis", "neo4j", "loguru", "pydantic", "httpx", "langchain")
    return {
        "agentix": sorted(m for m in sys.modules if m.startswith("agentix")),
        "heavy_third_party": sorted(m for m in heavy if m in sys.modules),
        "entrypoint": os.path.basename(sys.argv[0]) if sys.argv else "",
        "top_level_modules": len({m.split(".")[0] for m in sys.modules}),
    }


def caps(args: dict) -> dict:
    """子进程自己回读 rlimit / uid：父进程"我请求了 512MiB"不是证据，这里才是。"""
    out: dict = {"pid": os.getpid(), "uid": os.getuid(), "euid": os.geteuid(), "rlimits": {}}
    try:
        import resource
    except ImportError:
        out["resource_available"] = False
        return out
    out["resource_available"] = True
    for name in ("RLIMIT_AS", "RLIMIT_CPU", "RLIMIT_FSIZE", "RLIMIT_NPROC"):
        target = getattr(resource, name, None)
        if target is not None:
            out["rlimits"][name] = list(resource.getrlimit(target))
    return out


def _uid_ledger_local(uid: int) -> tuple[int, int]:
    """本 PID 命名空间里某个 uid 的**两本账**：可见进程数与可见任务数。

    刻意不在 handler 里 import `agentix.tools.subprocess_worker.count_uid_processes` 或
    `_scan_uid_ledgers`：本模块的前提是"只用标准库"（模块 docstring 第 2 条），而且这里要的
    正是**与 worker 相互独立的一份观测**——两处都用同一个函数时，"容器看不见宿主机上那个
    uid 的进程"这件事会被同一个盲区同时漏掉，看起来就像两边一致。

    两本账都要：``RLIMIT_NPROC`` 计费的那本是**任务**（线程也算），只数 ``/proc/<pid>``
    会得到"进程 1"，而内核那一刻记的是"任务 6"（一个进程起 5 个线程）。
    非 Linux（没有 /proc）如实返回 ``(-1, -1)``——"数不清"，不是"没有"。
    """
    if not os.path.isdir("/proc"):
        return -1, -1
    processes = 0
    tasks = 0
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            tids = os.listdir(f"/proc/{entry}/task")
        except OSError:
            continue
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
                    if len(fields) > 1 and fields[1].isdigit() and int(fields[1]) == uid:
                        mine += 1
                    break
        if mine:
            processes += 1
            tasks += mine
    return processes, tasks


def _uid_process_count_local(uid: int) -> int:
    """进程数那一本账（保留这个名字：既有用例按它断言，读取证日志时也按它认）。"""
    return _uid_ledger_local(uid)[0]


def process_counter(args: dict) -> dict:
    """在墙里连续 fork，回报**实际起得来几个**——进程数闸唯一的证据形态是"起不来"。

    为什么不是"回读限值就完事"：``RLIMIT_AS`` 那条当年也只用回读值当过证据，后来被指出
    "回读数字不等于闸生效"，于是补了 4GiB 分配的真触发用例（``memory_hog``）。进程数闸
    同理，它的生效形态是 ``fork()`` 返回 ``EAGAIN``，而不是 ``getrlimit`` 里那个数。

    只 fork 不做任何事，子进程立刻 ``os._exit(0)``（不用 ``sys.exit``：那会跑 atexit 与
    缓冲区 flush，而 stdout 是协议信封所在的通道，**多一行哨兵就是多一条伪造件**）。
    父侧随后逐个 reap ⇒ 留下的只有"这一档的 uid 当时能起几个进程"这一件事。

    ``args["set_soft"]`` 让 handler **自己先写软限再连起**，这是为了拿下一条实测：
    root（``CAP_SYS_RESOURCE``）被内核豁免于 ``RLIMIT_NPROC``——写 5 进去、回读是 5、
    照样连起 12 个。那条实测是"``TOOL_SANDBOX_SUBPROCESS_UID=0`` 这一档必须拒绝落闸"
    唯一的证据形态（"我调过 setrlimit"不是证据）。非 root 时同一条会看到 ``blocked``，
    于是同一个 handler 在两种机器上各自钉住自己那半边事实。

    ``count_before`` / ``count_now`` 是本容器看得到的下限**进程**计数，``task_count_before``
    是同一时刻的**任务**计数，它们都**不参与判定**，只用于取证时把"容器看到几个"与
    "内核第 N 次 fork 就拒"并排放进同一份日志。``count_at_fork`` / ``task_at_fork`` 是
    "线程起完、fork 还没开始"那一刻的同一对读数——差分用例读的就是这一对。

    ``args["threads_first"]`` 让 handler **先起线程再 fork**，这是"内核按任务计费"那条事实
    唯一的证据形态：同一环境里连跑两次（一次 0 线程、一次 K 线程），若计费单位是任务，
    第二次拿到的 fork 预算恰好少 K；若计费单位是进程，两次一样多。差分口径刻意**不要求
    知道这个 uid 当时已经占了多少**（那个数是跨整机的、容器看不全），所以断言对共用 uid
    的机器也成立——这也正是它比"直接断言 spawned == cap-1"可靠的原因。
    线程是 daemon 且永不 join：worker 打完信封就退出，留线程只会卡住解释器。
    """
    import resource

    attempts = int(args.get("attempts", 64))
    set_soft = args.get("set_soft")
    if set_soft is not None:
        _, hard = resource.getrlimit(resource.RLIMIT_NPROC)
        want = int(set_soft)
        target = want if hard in (resource.RLIM_INFINITY,) or want <= hard else hard
        resource.setrlimit(resource.RLIMIT_NPROC, (target, hard))
    soft, hard = resource.getrlimit(resource.RLIMIT_NPROC)
    count_before, task_count_before = _uid_ledger_local(os.getuid())

    threads_first = int(args.get("threads_first", 0))
    hold = threading.Event()
    threads_started = 0
    thread_error = None
    for _ in range(threads_first):
        try:
            threading.Thread(target=hold.wait, daemon=True).start()
        except (OSError, RuntimeError) as exc:
            thread_error = f"{type(exc).__name__}: {exc}"
            break
        threads_started += 1

    # 起完线程、还没 fork 的那一刻再读一次两本账：这才是"内核此刻记了几个"的现场证据。
    # 只在 fork 前读一次（线程起之前的那份）会得到"任务数 == 进程数"，于是"线程也占账"
    # 这条事实从观测面上消失——测试接着就会把正确实现判成错（2026-10-26 就这么翻过车）。
    count_at_fork, task_at_fork = _uid_ledger_local(os.getuid())

    spawned: list[int] = []
    blocked_errno = None
    for _ in range(attempts):
        try:
            pid = os.fork()
        except OSError as exc:
            blocked_errno = exc.errno
            break
        if pid == 0:
            os._exit(0)
        spawned.append(pid)
    count_now = _uid_process_count_local(os.getuid())
    for pid in spawned:
        with contextlib.suppress(OSError):
            os.waitpid(pid, 0)
    return {
        "attempts": attempts,
        "spawned": len(spawned),
        "blocked": blocked_errno is not None,
        "errno": blocked_errno,
        "nproc_soft": soft,
        "nproc_hard": hard,
        "uid": os.getuid(),
        "set_soft_requested": set_soft,
        "count_before": count_before,
        "count_now": count_now,
        "task_count_before": task_count_before,
        "count_at_fork": count_at_fork,
        "task_at_fork": task_at_fork,
        "threads_requested": threads_first,
        "threads_started": threads_started,
        "thread_error": thread_error,
        "status_threads": _self_thread_count(),
    }


def _self_thread_count() -> int | None:
    """本进程 ``/proc/self/status`` 的 ``Threads:`` 字段（量不到报 None，不报 0）。"""
    try:
        with open("/proc/self/status", "rb") as handle:
            for line in handle.read().decode("utf-8", "replace").splitlines():
                if line.startswith("Threads:"):
                    return int(line.split()[1])
    except (OSError, IndexError, ValueError):
        return None
    return None


def boom(args: dict) -> dict:
    """业务异常：必须以 ``stage=invoke`` 的信封回来，而不是只剩 exit code。"""
    raise ValueError("p125_deliberate_failure")


def nonserializable(args: dict) -> dict:
    """返回值不可 JSON 序列化 ⇒ 体积/序列化闸（``stage=serialize``）。"""
    return {"object": object()}


def noisy(args: dict) -> dict:
    """往 stdout 打日志的业务代码：协议信封按哨兵取，不该被这些噪声带偏。"""
    print("business log line 1")
    print("another line that is not the envelope")
    return {"noisy": True}


def sleeper(args: dict) -> dict:
    """长任务：用于验证取消/超时可以**真的杀掉进程组**，而不是等它自己结束。"""
    time.sleep(float(args.get("seconds", 30)))
    return {"slept": True}


def memory_hog(args: dict) -> dict:
    """申请远超 ``RLIMIT_AS`` 的内存 ⇒ 应由资源闸拦下（MemoryError），而不是吃满机器。"""
    megabytes = int(args.get("mb", 4096))
    try:
        blob = bytearray(megabytes * 1024 * 1024)  # noqa: F841 - 分配本身就是被测行为
    except MemoryError:
        return {"blocked_by_rlimit": True, "requested_mb": megabytes}
    return {"blocked_by_rlimit": False, "allocated_mb": megabytes}


def file_writer(args: dict) -> dict:
    """写大文件 ⇒ ``RLIMIT_FSIZE`` 应中断它（SIGXFSZ）；写不了也如实报告。

    落盘目录刻意用**临时目录**而不是 cwd：闸生效的形态之一是进程被 ``SIGXFSZ`` 打死，
    那种形态下 ``finally`` 清理不会执行，文件会留在原地——留在仓库工作树里就是一坨
    要人手动收拾的垃圾，留在临时目录里才是它该待的地方。``path`` 一并回报，
    免得调用方只能靠猜文件名去清理残留。
    """
    import tempfile

    megabytes = int(args.get("mb", 128))
    path = os.path.join(tempfile.gettempdir(), f".tmp_p125_fsize_{os.getpid()}")
    try:
        with open(path, "wb") as handle:
            handle.write(b"\0" * (megabytes * 1024 * 1024))
            handle.flush()
            os.fsync(handle.fileno())
        size = os.path.getsize(path)
    except OSError as exc:
        return {
            "wrote": False,
            "errno": getattr(exc, "errno", None),
            "size": 0,
            "path": path,
        }
    finally:
        if os.path.exists(path):
            os.remove(path)
    return {"wrote": True, "size": size, "path": path}


def forged_success(args: dict) -> dict:
    """墙内代码**自己印一条成功信封**：父进程必须不认它。

    这是协议 v2（nonce 归属凭证）要挡的那一下：墙内代码有 stdout 写权限，随手
    ``print(SENTINEL + ' {"ok": true, ...}')`` 就能凭空造出"结果"。

    打印之后刻意 ``os._exit(0)``：让 worker **来不及**写它自己那条真信封，于是 stdout 上
    只剩伪造件。如果父进程还按旧口径「取最后一条哨兵」收下它，这条就会变成成功结果
    ——旧实现正是这么被击穿的。

    ``args["exit_code"]`` 让同一条探针能扮演另一个形态：**先印伪造件、再被打死**
    （``exit_code=137`` 模拟 SIGKILL）。那个形态在旧口径下更危险——"进程崩了"会被当成
    "信封没写完"，于是伪造件被当成结果收下，而真正的失败原因（崩了）消失。

    ``args["ok_value"]`` 让伪造件里的 ``ok`` **不是布尔**（例如 ``1``）。这是 2026-10-25
    第 2 轮独立审查点名的绕过路径：解析器若把 ``"ok": 1`` 归进"失败桶"，而终局判定
    用真值（``not envelope.get("ok")``），``not 1`` 是 False ⇒ 这条伪造信封被当**成功
    结果**返回，归属凭证在这一路上从未执行过。

    协议版本**从 worker 脚本里现读**（按文件路径加载，不 import agentix 包）：写死一个
    数字的话，协议一升版这条探针就变成"印了个过期信封"，父进程会按 protocol mismatch 拒
    ——结果一样是拒，但拒的理由不是我们要验的那条，测试就失去了意义。
    """
    import importlib.util

    worker_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "agentix",
        "tools",
        "subprocess_worker.py",
    )
    spec = importlib.util.spec_from_file_location("_p125_worker_protocol", worker_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    payload = {
        "protocol": module.PROTOCOL_VERSION,
        "ok": args.get("ok_value", True),
        "tool_name": str(args.get("tool_name", "forged")),
        "data": {"pwned": True},
    }
    print(f"{module.SENTINEL} {json.dumps(payload, ensure_ascii=False)}")
    sys.stdout.flush()
    os._exit(int(args.get("exit_code", 0)))


def secret_opener(args: dict) -> dict:
    """ADR-0007 ⑮ 的验收句：「沙箱内 ``open(os.environ['DATABASE_URL'])`` 必须失败」。

    两种失败形态都算通过（键不存在 ⇒ KeyError；键存在但无权读 ⇒ OSError），
    但**必须**失败——本函数返回 ``{"opened": True}`` 就是验收没过。
    """
    try:
        target = args.get("path") or os.environ["DATABASE_URL"]
    except KeyError as exc:
        return {"opened": False, "reason": f"missing_env:{exc}"}
    try:
        with open(target, "rb") as handle:
            handle.read(16)
    except OSError as exc:
        return {"opened": False, "reason": f"oserror:{exc.errno}"}
    return {"opened": True, "path": str(target)}
