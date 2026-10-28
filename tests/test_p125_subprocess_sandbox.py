"""P125：执行沙箱的 ``subprocess`` 档（§24.3 T2 ⑮ 落地回归，ADR-0007）。

这一档是阶梯上**第一个真的算隔离边界**的档位，所以本文件的断言重心不是"能跑通"，
而是四件容易各说一套的事必须同时成立：

1. **纪律 1**：``TOOL_SANDBOX_SUBPROCESS_ENABLED=false`` 时这一档**仍然不存在**
   （差集里照旧有 ``subprocess``，声明它照旧在执行点被拒），且与 ⑭ 落地时逐位等价；
2. **fail-closed 的形状**：本机落不下资源闸（Windows 没有 ``resource``）⇒ 响亮拒绝，
   **一次 handler 都不调用、一个子进程都不起**，绝不「那就进程内跑吧」；
3. **判据单源**：环境变量白名单与 G2（MCP stdio）共用 ``domain/tool/exec_env.py``
   那一份——两处各写一份迟早只关一条；而两个受众的 ``extra_keys`` **不许互相放宽**；
4. **墙只挡住它声明过的那几个维度**（纪律 6 ①）：资源闸、环境、可杀进程组、体积闸
   各是一道**独立**的闸；文件权限与出网**不在**这一档的承诺里（属 ⑰），所以本文件
   不写「沙箱里读不到文件」这类它没承诺过的断言。

分层（缺一层都算没验完）：

- L1 默认姿态与开关面（含纪律 4 的五处同步、compose 转发、staging **不**钉开、矩阵生成器、
  `SECURITY POSTURE` 清单**不**点名这两位）
- L2 环境白名单单源 + 两个受众互不串供
- L3 能力探测与 fail-closed（本机=Windows 必拒；探测不过 ⇒ 不起进程）
- L4 handler 可重建性（跨进程边界的五类真实事故，父进程侧与子进程侧同源）
- L5 协议与体积闸（纯函数 + 假子进程，覆盖父进程侧每一个出口）
- L6 真子进程（跨平台一条：探测说谎时**子进程自己**仍拒 ⇒ 两道闸不共用一个判断；
  POSIX 一组：结果等价、环境面、rlimit 回读、内存闸、取消杀进程组、ADR 验收句）

POSIX 专属用例在本机（Windows）以 ``skip`` 响亮地不跑，不写成"永远绿"；
CI 的 Linux job 会真跑它们。
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import glob
import json
import os
import re
import subprocess
import sys
import tempfile
from functools import partial
from pathlib import Path, PurePosixPath

import pytest
import yaml

from agentix.config import settings
from agentix.domain.tool.exec_env import OS_ENV_ALLOWLIST, normalize_env_keys, scrub_env
from agentix.domain.tool.sandbox import SANDBOX_LADDER, SandboxUnavailable
from agentix.tools import sandbox as sandbox_module
from agentix.tools import subprocess_worker as worker
from agentix.tools.definitions import ToolSpec
from agentix.tools.gateway.executor import ToolExecutor
from agentix.tools.sandbox import (
    MAX_REQUEST_BYTES,
    RLIMIT_AS_MB,
    SubprocessSandbox,
    SubprocessSandboxError,
    _repo_root,
    implemented_kinds,
    probe_subprocess_caps,
    resolve_sandbox,
    subprocess_tier_enabled,
)
from tests import subprocess_probe_handlers as probes

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PORT_KEY = "EXECUTION_SANDBOX_ENABLED"
TIER_KEY = "TOOL_SANDBOX_SUBPROCESS_ENABLED"
UID_KEY = "TOOL_SANDBOX_SUBPROCESS_UID"
EXTRA_KEY = "TOOL_SANDBOX_SUBPROCESS_ENV_EXTRA_KEYS"
#: **本文件不许有第二个"这台机器是不是 POSIX"的判点**（第 3 轮独立审查的一般项）：
#: 旧写法在模块顶部另立 `POSIX = os.name == "posix"`，与探测各算各的——探测的 posix
#: 判据一旦加宽（例如要求同时有 `resource`），下面那几处 `if POSIX` 就会与主判点分家，
#: 而纪律 6 ④要的正是「诊断口径与执行链的判定同源」。所有 POSIX 判定一律读 `_CAPS`。
_CAPS = probe_subprocess_caps()
#: **skip 判据必须与执行点的判定同源**（纪律 6 ④："诊断口径必须与执行链的判定同源"）。
#: 旧写法是 `not POSIX`——在"POSIX 但解释器没有 `resource` 扩展"这种机器上，那批
#: 专属用例不会 skip，而是各自撞上 `SandboxUnavailable` 变成**失败**：那不是代码缺陷，
#: 是那台机器本来就兑现不了这一档承诺的隔离维度。判据换成 `probe_subprocess_caps()`
#: 的真值，reason 里带上 `missing`，读跳过的人不必再去猜差哪一件。
#: 单独起个名字是为了让"判据 = 探测真值"这条关系可被用例钉住（见
#: ``test_skip_predicate_is_the_probe_itself_not_a_second_guess``）。
_SKIP_BECAUSE_NO_WALL = not _CAPS["runnable"]
POSIX_ONLY = pytest.mark.skipif(
    _SKIP_BECAUSE_NO_WALL,
    reason=f"资源闸/setuid/进程组 kill 要本机探测通过才谈得上；missing={_CAPS['missing']}",
)
NEW_KEYS = (TIER_KEY, UID_KEY, EXTRA_KEY)


def _spec(name: str, *, sandbox: str = "subprocess", **kw: object) -> ToolSpec:
    return ToolSpec(
        name=name,
        description=f"p125 probe 何时用：测试；何时不能用：生产（{name}）",
        risk_level="L0",
        sandbox=sandbox,  # type: ignore[arg-type]
        **kw,  # type: ignore[arg-type]
    )


def _runnable_caps() -> dict:
    """把探测假扮成「这台机器落得下闸」，好让父进程侧每一条出口都能被走到。"""
    return {
        "posix": _CAPS["posix"],
        "resource": True,
        "setuid": True,
        "runnable": True,
        "missing": [],
        # 与真探测同形（`test_probe_reports_its_own_basis_not_a_bare_boolean` 钉住键集），
        # 但刻意取"落得下"的那一支：这一组用例要测的是父进程侧的分支，不是进程数闸本身。
        "nproc_gate": {
            "applies": True,
            "cap": sandbox_module.RLIMIT_NPROC,
            "uid_process_count": 1,
            "uid_task_count": 1,
            "no_headroom": False,
            "reason": "forged by the fixture",
        },
    }


# ---------------------------------------------------------------------------
# 假子进程：让父进程侧的分支在**任何平台**都可测（真进程那批在 L6 与 POSIX 组）
# ---------------------------------------------------------------------------


class _Stream:
    def __init__(self, data: bytes = b"") -> None:
        self._data = data

    async def read(self, _n: int = -1) -> bytes:
        data, self._data = self._data, b""
        return data


class _InfiniteStream:
    """永远有货的 stdout：用来钉住「应答体积闸真的会响」。"""

    def __init__(self) -> None:
        self.chunks = 0

    async def read(self, _n: int = -1) -> bytes:
        self.chunks += 1
        return b"x" * (64 * 1024)


class _Stdin:
    def __init__(self) -> None:
        self.payload = b""

    def write(self, data: bytes) -> None:
        self.payload += data

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        return None


class _FakeProc:
    """最小可用的子进程替身。

    pid 取一个**不存在**的大数：POSIX 上 ``os.getpgid(该 pid)`` 必然抛错并被 suppress，
    绝不可能真杀到跑测试的进程自己。
    """

    def __init__(self, stdout, stderr=None, exit_code: int = 0) -> None:
        self.pid = 2_000_000
        self.returncode = None
        self.stdin = _Stdin()
        self.stdout = stdout
        self.stderr = stderr if stderr is not None else _Stream()
        self.exit_code = exit_code
        self.killed = False

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int:
        self.returncode = self.exit_code
        return self.exit_code


class _LazyEnvelopeStream:
    """stdout 替身：**读的那一刻**才拼信封，因为 nonce 是父进程起完进程才写进 stdin 的。

    真 worker 的行为是"从请求里拿到 nonce 并回显"，替身必须同样迟到，否则父进程侧
    一切正常路径都会被判成"应答不可信"——那这条夹具就只是在制造假失败。
    ``echo_nonce=False`` 用来扮演"墙内代码自己印了一条 ok 信封"这一伪造形态。
    """

    def __init__(self, proc: "_FakeProc", payload: dict, *, echo_nonce: bool) -> None:
        self._proc = proc
        self._payload = payload
        self._echo_nonce = echo_nonce
        self._text: bytes | None = None

    def _render(self) -> bytes:
        envelope = dict(self._payload)
        if self._echo_nonce and envelope.get("ok") is True:
            request = json.loads(self._proc.stdin.payload.decode("utf-8") or "{}")
            envelope.setdefault("nonce", request.get("nonce"))
        return f"{worker.SENTINEL} {json.dumps(envelope, ensure_ascii=False)}\n".encode()

    async def read(self, _n: int = -1) -> bytes:
        if self._text is None:
            # 等到父进程把请求写进 stdin 为止（`asyncio.gather` 里 drain 与 feed 的
            # 先后不由我们决定；真子进程天然等得到，替身必须自己等，否则 nonce 取不到
            # 就成了"替身在制造假失败"）。
            for _ in range(64):
                if self._proc.stdin.payload:
                    break
                await asyncio.sleep(0)
            self._text = self._render()
        data, self._text = self._text, b""
        return data


def _envelope_proc(
    payload: dict, *, stderr: bytes = b"", exit_code: int = 0, echo_nonce: bool = True
) -> _FakeProc:
    proc = _FakeProc(_Stream(), _Stream(stderr), exit_code=exit_code)
    proc.stdout = _LazyEnvelopeStream(proc, payload, echo_nonce=echo_nonce)
    return proc


@pytest.fixture
def tier_on(monkeypatch):
    """开端口 + 开档位（默认关的那批用例不共用本 fixture）。"""
    monkeypatch.setattr(settings, PORT_KEY, True)
    monkeypatch.setattr(settings, TIER_KEY, True)
    monkeypatch.setattr(settings, UID_KEY, -1)
    monkeypatch.setattr(settings, EXTRA_KEY, "")
    return SubprocessSandbox()


@pytest.fixture
def probe_ok(monkeypatch):
    """放行父进程侧探测（**不影响子进程侧**：真子进程照旧按本机真值落闸）。"""
    monkeypatch.setattr(sandbox_module, "probe_subprocess_caps", _runnable_caps)
    return _runnable_caps()


@pytest.fixture
def fake_spawn(monkeypatch) -> dict:
    """记录 ``create_subprocess_exec`` 的入参，并回一条可编排的信封。"""
    captured: dict = {
        "result": {
            "protocol": worker.PROTOCOL_VERSION,
            "ok": True,
            "data": {"echo": True},
            "caps": {},
        }
    }

    async def _spawn(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        proc = _envelope_proc(
            captured["result"],
            stderr=captured.get("stderr", b""),
            exit_code=int(captured.get("exit_code", 0)),
            echo_nonce=bool(captured.get("echo_nonce", True)),
        )
        captured["proc"] = proc
        return proc

    monkeypatch.setattr(sandbox_module.asyncio, "create_subprocess_exec", _spawn)
    return captured


@pytest.fixture
def spawn_tracker(monkeypatch) -> list:
    """记录**每一次**起进程；在应当拒绝的路径上断言它保持为空。"""
    calls: list = []

    async def _recording(*args, **kwargs):
        calls.append((args, kwargs))
        return _envelope_proc(
            {"protocol": worker.PROTOCOL_VERSION, "ok": True, "data": {}, "caps": {}}
        )

    monkeypatch.setattr(sandbox_module.asyncio, "create_subprocess_exec", _recording)
    return calls


# ---------------------------------------------------------------------------
# L1 默认姿态与开关面
# ---------------------------------------------------------------------------


def test_tier_flag_and_its_two_companions_default_off_or_inert() -> None:
    """三个新键的出厂值：关、不降权、不加白名单——**没有一个是「顺手就开」**。"""
    fields = type(settings).model_fields
    assert fields[TIER_KEY].default is False
    assert fields[UID_KEY].default == -1, "-1 才是不降权；填 0 等于出厂以 root 跑 handler"
    assert fields[EXTRA_KEY].default == ""


def test_the_two_sandbox_keys_are_deliberately_not_posture_flags() -> None:
    """执行位置端口与 ⑮ 档位**刻意不进** `SECURITY POSTURE` 的点名清单。

    判据是"关着会不会让实例变得不该对外"：这两位的关与开**都不改变边界**——关着时
    声明 `subprocess` 拿到的是执行点拒绝（fail-closed），不是"静默在进程内跑"。把它们
    列进去，等于宣称"开了就有墙"，而墙只在逐条 `ToolSpec.sandbox` 声明了的那几件上存在。
    反向锚点：`MCP_SUBPROCESS_ENV_SCRUB` 关着时子进程继承全部环境（真·边界变弱），
    它必须在清单里——否则本条断言只是"清单里没有新键"的空话。

    为什么用 AST 读源码而不是 ``import agentix.main``：`main.py` 顶层 `import uvicorn`，
    把整条 web 装配拉起来才有这个清单可读；而这条守卫判的是**清单里列了谁**，最小 Linux
    容器（没有 uvicorn，见取证 runbook 第 2 步）也得能跑它。
    """
    tree = ast.parse((PROJECT_ROOT / "agentix" / "main.py").read_text(encoding="utf-8"))
    flags: list[str] = []
    for node in tree.body:
        targets = (
            [node.target]
            if isinstance(node, ast.AnnAssign)
            else list(node.targets) if isinstance(node, ast.Assign) else []
        )
        if any(isinstance(t, ast.Name) and t.id == "_INSECURE_GATE_FLAGS" for t in targets):
            flags = [e.value for e in node.value.elts] if isinstance(node.value, ast.Tuple) else []
    assert flags, "防空真：`_INSECURE_GATE_FLAGS` 的字面量没解析到（写法变了还是文件没了？）"
    assert PORT_KEY not in flags
    assert TIER_KEY not in flags
    assert "MCP_SUBPROCESS_ENV_SCRUB" in flags, "反向锚点失效：清单被改空了"


def test_flag_off_keeps_the_tier_out_of_the_registry(monkeypatch) -> None:
    monkeypatch.setattr(settings, TIER_KEY, False)
    assert subprocess_tier_enabled() is False
    assert "subprocess" not in implemented_kinds()
    assert "subprocess" in SANDBOX_LADDER, "词汇表不变：差集可见才是 ⑮ 的落地证据"
    with pytest.raises(SandboxUnavailable, match="switched off") as caught:
        resolve_sandbox("subprocess")
    assert TIER_KEY in str(caught.value), "报错要点名开关，否则运维只能猜"


@pytest.mark.asyncio
async def test_tier_off_refuses_exactly_like_pre_tier(monkeypatch, spawn_tracker) -> None:
    """纪律 1 的反证形态：端口开着、**档位关着** ⇒ 与 ⑮ 不存在时逐位相同的拒绝。

    与 p114 的 ``test_declaration_is_inert_when_the_flag_is_off`` 同族但钉的是另一侧：
    那条钉"端口关着声明惰性（照旧在进程内跑）"，这条钉"端口开着而档位关着时，
    拒绝的**形态**（reason / retryable / 不调 handler / 不起进程）与 ⑮ 之前一模一样"。
    新键关着时必须与「这个键还不存在」逐位等价，而不是「读不到就当开」。
    """
    monkeypatch.setattr(settings, PORT_KEY, True)
    monkeypatch.setattr(settings, TIER_KEY, False)
    calls: list[dict] = []

    def handler(args: dict) -> dict:
        calls.append(args)
        return {"did": "work"}

    name = "p125_tier_off"
    ex = ToolExecutor()
    ex.register_handler(name, handler, spec=_spec(name))
    result = await ex.execute(name, {"x": 1})
    assert calls == [], "拒绝后又回进程内跑 = 安全声明成了装饰"
    assert result.success is False
    assert result.metadata["reason"] == "sandbox_unavailable"
    assert result.metadata["sandbox"] == "subprocess"
    assert result.retryable is False, "重试换不来一个实现"
    assert "switched off" in (result.error or "")
    assert spawn_tracker == []


@pytest.mark.asyncio
async def test_port_off_declarations_stay_inert_with_the_tier_on(
    monkeypatch, spawn_tracker
) -> None:
    """新键**不能越过自己的总闸**：端口关着时，哪怕档位开着也一个字都不查 `spec.sandbox`。"""
    monkeypatch.setattr(settings, PORT_KEY, False)
    monkeypatch.setattr(settings, TIER_KEY, True)
    calls: list[dict] = []

    def handler(args: dict) -> dict:
        calls.append(args)
        return {"did": "work"}

    name = "p125_port_off"
    ex = ToolExecutor()
    ex.register_handler(name, handler, spec=_spec(name))
    result = await ex.execute(name, {"x": 1})
    assert result.success is True and result.data == {"did": "work"}
    assert calls == [{"x": 1}]
    assert spawn_tracker == [], "端口关着却起了子进程 ⇒ 新键绕过了 EXECUTION_SANDBOX_ENABLED"


def test_new_keys_are_synced_in_the_five_places() -> None:
    """纪律 4 的五处同步（settings / selfcheck / ``.env.example`` / 配置文档 / p41 手清单）。

    这里**逐处现读**，而不是信任 p41：p41 只保证「文档提到过」，本条还保证 selfcheck 的
    忽略清单与 ``.env.example`` 的赋值行都在——少一处就是「下一位加键的人照着一份
    残缺样板继续漏」。
    """
    from agentix.config.selfcheck import DEFAULT_IGNORED_MISSING

    env_example = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
    config_doc = (PROJECT_ROOT / "docs/architecture/配置管理.md").read_text(encoding="utf-8")
    p41_source = (PROJECT_ROOT / "tests/test_p41_doc_consistency.py").read_text(encoding="utf-8")
    for key in NEW_KEYS:
        assert key in type(settings).model_fields, f"{key} 没进 Settings"
        assert key in DEFAULT_IGNORED_MISSING, f"{key} 没进 selfcheck 忽略清单 ⇒ 缺省自检会红"
        assert re.search(rf"^{key}=", env_example, re.MULTILINE), f"{key} 不在 .env.example"
        assert f"`{key}`" in config_doc, f"{key} 未在配置文档 §5 的表里"
        assert f'"{key}"' in p41_source, f"{key} 没进 p41 手清单"


def test_compose_forwards_the_tier_off_by_default() -> None:
    doc = yaml.safe_load((PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    env = doc["services"]["backend"]["environment"]
    forwarded = {
        str(item).split("=", 1)[0]: str(item).split("=", 1)[1] for item in env if "=" in str(item)
    }
    assert forwarded[TIER_KEY] == f"${{{TIER_KEY}:-False}}"
    assert forwarded[UID_KEY] == f"${{{UID_KEY}:--1}}"
    assert forwarded[EXTRA_KEY] == f"${{{EXTRA_KEY}:-}}"


def test_staging_override_does_not_claim_the_tier_on() -> None:
    """预发**没有**钉开这一档 ⇒ 生成物矩阵里也不许出现这些行（G13 的案发形态）。

    这不是偷懒，是口径：⑮ 的落地证据来自 Linux 容器里的真跑（见 runbook），不是来自
    「staging 顺手打开」。想让 staging 打开，得同时改 override 并重跑生成器。
    """
    override = yaml.safe_load(
        (PROJECT_ROOT / "docker/compose.override.staging.yml").read_text(encoding="utf-8")
    )
    env = (override.get("services", {}).get("backend", {}) or {}).get("environment") or {}
    matrix = (PROJECT_ROOT / "docs/runbooks/预发环境与配置矩阵.md").read_text(encoding="utf-8")
    for key in NEW_KEYS:
        assert key not in set(env), f"{key} 被 staging 钉了却不进生成物矩阵 ⇒ 表与部署物开始分叉"
        assert (
            f"| `{key}`" not in matrix
        ), f"矩阵里有 {key} 这一行，但 override 没设它、也没登记 MANUAL_KEYS"


def test_matrix_generator_still_agrees_with_the_deployables() -> None:
    """改了 compose 就得跑生成器：这条把「漂移当场红」钉成机制，而不是纪律条文。"""
    outcome = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts/gen_staging_matrix.py"), "--check"],
        capture_output=True,
        # 生成器打印的是中文；Windows 上 `text=True` 会按 cp936 解码，管道线程里直接抛
        # UnicodeDecodeError（警告吞掉异常 ⇒ 断言照旧读不到内容）。解码口径必须写死。
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(PROJECT_ROOT),
    )
    assert outcome.returncode == 0, f"配置矩阵漂移：\n{outcome.stdout}\n{outcome.stderr}"


#: 文档里声明"本文件一共多少条用例"时**必须**用的短语（真源见 runbook 的数字行）。
#: 短语刻意带 `共`：历史口径块写的是"当时 N 条"，不会被这条守卫当成当前声明去核。
P125_COUNT_CLAIM = re.compile(r"p125 共 (\d+) 条")

#: 会复述本文件用例数的文档。**为什么要有守卫**：2026-10-25 第 3 轮独立审查登记的一般项——
#: 套件总数有 `TestTestCountHasSingleSource` 盯着，**单文件条数没有**，于是本文件从 74 涨到
#: 77 时，README / CHANGELOG / runbook / PROJECT_SPEC / ADR 里那六处手抄数字同时腐烂，
#: 而全量回归一个字都没红。纪律 7 的口径是"被当核对基准的表必须是生成物，不是抄本"，
#: 这条就是把它落到用例计数上：数字由**这次真实的采集**现算，文档只能写成同一个数。
DOCS_CLAIMING_P125_COUNT = (
    "README.md",
    "CHANGELOG.md",
    "docs/runbooks/子进程沙箱取证.md",
    "docs/architecture/运行时护栏.md",
    "docs/architecture/adr/0007-执行沙箱端口.md",
    "docs/项目实现手册.md",
)


def _collected_file_name(item: object) -> str:
    r"""采集项所属**文件**的名字，只认采集侧的 `nodeid`。

    为什么不用 `item.location[0]`（2026-10-26 第 4 轮独立审查的 S1）：`location` 是从
    **code object 的 `co_filename`** 派生的，而 `co_filename` 是编译时写进 `.pyc` 的那条
    路径。bind-mount 让容器复用宿主 Windows 编的字节码时，同一个用例的 `location[0]` 是
    `D:\Code\…\tests\x.py`、采集侧路径却是 `/repo/tests/x.py` ⇒ 两者分家。更要命的是
    POSIX 上反斜杠**不是**分隔符，`Path(r"D:\…\x.py").name` 返回**整条字符串**，于是本文件
    被数成 0 条：守卫红在"取证流程"上，而不是它本该核的"文档"上。
    `nodeid` 由 pytest 用 `/` 拼出、相对 rootdir，两平台同形 ⇒ 只有一个身份源（纪律 6 ④）。
    """
    return PurePosixPath(str(item.nodeid).split("::", 1)[0]).name  # type: ignore[attr-defined]


def test_the_case_count_identity_source_survives_a_stale_pyc() -> None:
    """钉住 S1：身份源必须是采集侧，不许是"编译时写进字节码的那条路径"。

    这条用例的价值在于**不需要真陈旧 `.pyc` 也能复现案发形态**：直接构造一个
    "nodeid 是 POSIX 相对路径、location 是 Windows 绝对路径"的采集项——那正是容器复用宿主
    字节码时 pytest 手里真实拿着的两份身份。
    """
    own = Path(__file__).name

    class _CollectedItem:
        def __init__(self, nodeid: str, location: tuple) -> None:
            self.nodeid = nodeid
            self.location = location

    stale = _CollectedItem(
        nodeid=f"tests/{own}::test_x",
        location=(rf"D:\Code\My-Agentix\tests\{own}", 7, "test_x"),
    )
    assert _collected_file_name(stale) == own, "身份源随平台变 ⇒ 同一个用例在两边认得不一样"
    if _CAPS["posix"]:
        # 旧写法在 POSIX 上的真实下场：整条 Windows 路径被当成"文件名"，于是本文件数成 0 条。
        assert (
            Path(stale.location[0]).name != own
        ), "旧身份源在这台机器上会把本文件数空（S1 的机制）"


def test_docs_declaring_this_files_case_count_match_the_live_collection(
    request,
) -> None:
    """写进文档的本文件用例数，必须等于**这一次采集**真正收到的条数。

    取值口径刻意不依赖 `-rs` 的输出，也不另跑一遍 `--collect-only` 子进程：
    `session.items` 就是产出终端那行"N passed / M skipped"的同一份清单，
    用它现算 ⇒ 文档核的是"执行链真用到的东西"，而不是第二套算法（纪律 6 ④）。

    采集被人为缩窄时**响亮跳过**并说明原因：`-k` / 指定 nodeid / `--lf` / `--deselect`
    都会让 `session.items` 只剩局部，那种时候拿它当基准去核文档反而是错的
    （`--deselect` 是第 4 轮审查的 N1：漏了它，一次局部复跑会把"人为缩窄"报成"文档腐烂"）。
    `-m "not integration"` 不在跳过之列——本文件不挂任何 marker，缩窄不了它的条数。
    ⚠️ **`--ignore` 掉本文件时这条守卫根本不会执行**（真空，不是绿）：所以"文档核过"这句话
    只在"本文件整份被采集"的那一轮里成立，读日志时先看 collected 行数。
    """
    config = request.session.config
    keyword = getattr(config.option, "keyword", None)
    initial_args = list(getattr(config.option, "file_or_dir", None) or [])
    last_failed = getattr(config.option, "lf", False)
    deselected = list(getattr(config.option, "deselect", None) or [])
    if keyword or last_failed or deselected or any("::" in str(arg) for arg in initial_args):
        pytest.skip("本次采集被 -k / nodeid / --lf / --deselect 缩窄过，session.items 不是全量基准")

    own_name = Path(__file__).name
    live = sum(1 for item in request.session.items if _collected_file_name(item) == own_name)
    assert live > 0, f"本文件一条都没采到（{own_name}），没法核文档"

    offenders: list[str] = []
    claims = 0
    for rel in DOCS_CLAIMING_P125_COUNT:
        path = PROJECT_ROOT / rel
        if not path.exists():
            offenders.append(f"{rel} 不存在")
            continue
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            for match in P125_COUNT_CLAIM.finditer(line):
                claims += 1
                if int(match.group(1)) != live:
                    offenders.append(f"{rel}:{line_no} 写 {match.group(1)}，本轮实采 {live}")
    assert claims, (
        f"一条 `p125 共 N 条` 的声明都没找到——守卫范围或文档短语失效了；"
        f"实采 {live} 条，真源应写在 runbook 的数字行"
    )
    assert (
        not offenders
    ), "文档里的本文件用例数与本轮实采不符（改用例就要同改这几处）：\n  " + "\n  ".join(offenders)


def test_every_ladder_rung_is_still_declareable() -> None:
    """纪律 6 ④：词汇表成员**可以声明**，可用性由注册表管——否则 ⑮ 落地当天要改一批声明。"""
    for kind in SANDBOX_LADDER:
        assert _spec(f"p125_declare_{kind}", sandbox=kind).sandbox == kind


# ---------------------------------------------------------------------------
# L2 环境白名单：单源 + 两个受众互不串供
# ---------------------------------------------------------------------------


def test_allowlist_has_exactly_one_source() -> None:
    """``client.py`` 里那份必须**是**域层那一份对象，不是抄本（抄本迟早只关一条）。"""
    from agentix.capabilities.mcp import client as mcp_client

    assert mcp_client.OS_ENV_ALLOWLIST is OS_ENV_ALLOWLIST
    assert {"PATH", "PYTHONPATH", "SYSTEMROOT", "LANG"} <= set(OS_ENV_ALLOWLIST)


def test_scrub_drops_secrets_and_keeps_what_a_child_needs() -> None:
    source = {
        "PATH": os.defpath,
        "SYSTEMROOT": "C:\\Windows",
        "DATABASE_URL": "postgresql://u:pw@h/db",
        "LLM_API_KEY": "sk-secret",
        "TEXT2SQL_DATABASE_URL": "mysql://u:pw@h/db",
        "NEO4J_PASSWORD": "pw",
    }
    out = scrub_env(source, project_root="/repo")
    assert out["PATH"] == os.defpath and out["SYSTEMROOT"] == "C:\\Windows"
    assert out["PYTHONPATH"] == "/repo"
    for key in ("DATABASE_URL", "LLM_API_KEY", "TEXT2SQL_DATABASE_URL", "NEO4J_PASSWORD"):
        assert key not in out, f"{key} 跨过了进程边界"


def test_scrub_is_case_insensitive_on_keys_and_honours_extra() -> None:
    out = scrub_env({"path": "/x", "MY_SIDE_CAR": "1"}, extra_keys="my_side_car")
    assert set(out) == {"path", "MY_SIDE_CAR"}
    assert normalize_env_keys(" a , B ,, c ") == frozenset({"A", "B", "C"})


def test_mcp_extras_do_not_widen_the_sandbox_wall() -> None:
    """变异钉子：给 MCP 加一个透传键，**沙箱侧必须不受影响**（两个受众不互相授权）。"""
    source = {"PATH": os.defpath, "MCP_ONLY": "1", "SANDBOX_ONLY": "2"}
    via_mcp = scrub_env(source, extra_keys="MCP_ONLY")
    via_sandbox = scrub_env(source, extra_keys="")
    assert "MCP_ONLY" in via_mcp and "MCP_ONLY" not in via_sandbox
    assert "SANDBOX_ONLY" not in via_sandbox


def test_proxy_variables_cross_the_wall_and_can_carry_credentials() -> None:
    """**特征化用例（钉现状，不钉愿望）**：`HTTP(S)_PROXY` 在白名单里，而代理 URL 的
    ``http://user:pass@corp:8080`` 形态本身就是凭据 ⇒ 它会跟着过界。

    这条是 2026-10-25 独立审查点名的一般项。处理口径刻意**不是**默默改写或剔除该值——
    那会让"经鉴权代理出网"的既有部署当场坏掉，属行为变更（纪律 5 要先点头）；
    也刻意**不是**继续在注释里写"白名单不含凭据"（那就是与代码相反的声明）。
    现状：注释与文档（`docs/architecture/运行时护栏.md` §15.10、README §16）
    都按"已知边界"陈述，本条保证任何一侧偷偷漂移都会红——把代理键从白名单摘掉、
    或把值改写，都得先让这条用例署名。
    """
    assert "HTTPS_PROXY" in OS_ENV_ALLOWLIST, "白名单里到底有没有代理键，是本条口径的前提"
    out = scrub_env(
        {"PATH": os.defpath, "HTTPS_PROXY": "http://user:s3cr3t@corp:8080", "LLM_API_KEY": "sk-x"},
        project_root="/repo",
    )
    assert out["HTTPS_PROXY"] == "http://user:s3cr3t@corp:8080", "现状：不改写、不剔除"
    assert "LLM_API_KEY" not in out, "与此同时，真正的密钥类键照旧被裁掉"


@pytest.mark.asyncio
async def test_child_environment_is_the_scrubbed_one_not_the_real_one(
    tier_on, probe_ok, fake_spawn, monkeypatch
) -> None:
    """父进程**真的**把裁过的环境交给子进程（不是"文档说裁了"）。"""
    for key in probes.PROBE_SECRET_ENV_KEYS:
        monkeypatch.setenv(key, probes.PROBE_SECRET_VALUE)
    await tier_on.run(probes.echo, {"a": 1})
    env = fake_spawn["kwargs"]["env"]
    for key in probes.PROBE_SECRET_ENV_KEYS:
        assert key not in env
    assert env["PYTHONPATH"] == _repo_root()
    assert env["PYTHONIOENCODING"] == "utf-8"
    assert {k.upper() for k in env} <= set(OS_ENV_ALLOWLIST) | {"PYTHONIOENCODING"}
    # 启动形态本身是承诺的一部分：**脚本路径**而不是 ``-m``。``-m`` 会先执行父包
    # ``__init__``，于是整套业务依赖抢在资源闸之前进场（见下面 L6 的 import 面用例）。
    entry = fake_spawn["args"][1]
    assert entry not in ("-m", "agentix.tools.subprocess_worker")
    assert os.path.isabs(entry) and os.path.basename(entry) == "subprocess_worker.py"
    assert fake_spawn["args"][0] == sys.executable
    assert fake_spawn["kwargs"]["cwd"] == _repo_root()


@pytest.mark.asyncio
async def test_declared_extra_keys_reach_the_child(
    tier_on, probe_ok, fake_spawn, monkeypatch
) -> None:
    monkeypatch.setattr(settings, EXTRA_KEY, "P125_SIDE_CAR")
    monkeypatch.setenv("P125_SIDE_CAR", "yes")
    await tier_on.run(probes.echo, {})
    assert fake_spawn["kwargs"]["env"].get("P125_SIDE_CAR") == "yes"


@pytest.mark.asyncio
async def test_request_carries_the_declared_limits_and_identity(
    tier_on, probe_ok, fake_spawn
) -> None:
    """请求信封必须带着**这一档承诺的闸值**与可重建身份，且参数逐字过去——
    否则子进程无从落闸，"声明了 512MiB"就只是父进程 docstring 里的一句话。"""
    await tier_on.run(probes.async_echo, {"a": 1, "nested": {"b": [1, 2]}})
    request = json.loads(fake_spawn["proc"].stdin.payload.decode("utf-8"))
    assert request["protocol"] == worker.PROTOCOL_VERSION
    assert request["module"] == "tests.subprocess_probe_handlers"
    assert request["qualname"] == "async_echo"
    assert request["arguments"] == {"a": 1, "nested": {"b": [1, 2]}}
    assert request["limits"] == {
        "as_mb": RLIMIT_AS_MB,
        "cpu_s": 30,
        "fsize_mb": 32,
        "nproc": sandbox_module.RLIMIT_NPROC,
    }
    assert request["uid"] == -1
    assert isinstance(request["nonce"], str) and len(request["nonce"]) >= 16, (
        "nonce 是应答归属凭证：没有它，父进程就只能靠「哨兵行」认应答，"
        "而哨兵字符串是写在仓库里的公开常量"
    )


@pytest.mark.asyncio
async def test_nonce_is_fresh_per_call(tier_on, probe_ok, fake_spawn) -> None:
    """每次调用现生成 ⇒ 上一条应答里混进来的 nonce 复用到不了下一次。"""
    await tier_on.run(probes.echo, {})
    first = json.loads(fake_spawn["proc"].stdin.payload.decode("utf-8"))["nonce"]
    await tier_on.run(probes.echo, {})
    second = json.loads(fake_spawn["proc"].stdin.payload.decode("utf-8"))["nonce"]
    assert first != second


@pytest.mark.asyncio
async def test_nonzero_exit_never_becomes_a_success_even_with_an_envelope(
    tier_on, probe_ok, fake_spawn
) -> None:
    """**审查点名的严重形态**：墙内先印一条 ok 信封，再被资源闸打成 rc≠0。

    旧写法是 ``if rc != 0 and SENTINEL not in stdout_text``——两个条件是 **and**，
    于是"stdout 里有哨兵"就把非 0 退出码赦免了，伪造的载荷被当**成功工具结果**返回，
    crash 彻底消失。而这一档卖的就是那三道闸：闸生效反而让伪造得逞不可接受。
    现在的规则是 **rc≠0 一律不采信信封**（``echo_nonce=False`` 扮演"信封是被隔离代码自己印的"）。
    """
    fake_spawn["result"] = {
        "protocol": worker.PROTOCOL_VERSION,
        "ok": True,
        "data": {"pwned": True},
        "caps": {},
    }
    fake_spawn["exit_code"] = 137
    fake_spawn["echo_nonce"] = False
    fake_spawn["stderr"] = b"killed by SIGKILL (rlimit cpu)"
    with pytest.raises(SubprocessSandboxError, match="worker crashed") as caught:
        await tier_on.run(probes.echo, {"a": 1})
    assert "不采信" in str(
        caught.value
    ), "诊断要点明「stdout 上有哨兵行但没当应答」，否则运维会以为是协议坏了"
    assert "137" in str(caught.value)


# ---------------------------------------------------------------------------
# L3 能力探测与 fail-closed
# ---------------------------------------------------------------------------


def test_probe_reports_its_own_basis_not_a_bare_boolean() -> None:
    caps = probe_subprocess_caps()
    assert set(caps) == {"posix", "resource", "setuid", "runnable", "missing", "nproc_gate"}
    assert caps["runnable"] is (not caps["missing"]), "missing 非空却 runnable ⇒ 探测自己在说谎"
    if not _CAPS["posix"]:
        assert caps["posix"] is False and caps["resource"] is False
        assert "resource.setrlimit(RLIMIT_AS/CPU/FSIZE)" in caps["missing"]
        assert "process-group kill (start_new_session)" in caps["missing"]


def test_skip_predicate_is_the_probe_itself_not_a_second_guess() -> None:
    """skip 判据必须**就是**能力探测的真值，而不是另一份"看起来等价"的判断。

    这条是纪律 6 ④在测试侧的样子：探测与执行点同源，测试的跳过判据也要同源。
    判据一旦分家（当年写的是 ``not POSIX``），就会出现"探测说跑不了、测试却当它能跑"
    的机器，于是那批 POSIX 专属用例从"响亮地跳过"退化成"各自失败"——失败会被误读成
    代码缺陷，而真因是那台机器兑现不了这一档承诺的隔离维度。
    """
    fresh = probe_subprocess_caps()
    assert _CAPS == fresh, "探测结果必须是现测的，不能是导入时的陈值"
    assert _SKIP_BECAUSE_NO_WALL == (not fresh["runnable"]), "skip 判据与探测真值分家了"
    if not _CAPS["posix"]:
        assert _SKIP_BECAUSE_NO_WALL is True, "本机没有资源闸 ⇒ POSIX 用例必须跳过"
    assert (
        _CAPS["missing"] or not _SKIP_BECAUSE_NO_WALL
    ), "没有缺口却跳过（会误伤真的能跑那台机器），或缺口非空却照跑（会变成失败）"


#: 「读一下就等于自算平台」的属性名：模块 → 属性。纪律 6 ④ 要的是"平台判定只有一个源"，
#: 而源可以穿好几件马甲（别名导入 / getattr / from-import），所以这里按**模块+属性**二元组抓。
_PLATFORM_MODULES: dict[str, set[str]] = {"os": {"name"}, "sys": {"platform"}}


def _find_platform_predicates(source: str) -> list[int]:
    """返回源码里自算平台判点的行号（第 4 轮独立审查的 N2：旧守卫只认字面 `os.name`）。

    抓的是**读平台属性这个动作本身**（按"模块 + 属性"二元组，不是按字符串匹配整行），所以
    这几件马甲都跑不掉：字面 `os.name` / `sys.platform`；别名导入后改读（`import os as _os`
    再 `_os.name`）；`getattr(os, "name")` 把属性名塞进字符串；`from os import name` 干脆不走
    属性访问。先读进变量、隔一百行再比较也抓得住——判的是"读"，不是"读挨着比"。

    **不覆盖**（第 5 轮独立审查实测登记；别把这条守卫当成密不透风的闸）：
    `o = __import__("os")` 之后 `o.name`（`o` 不是已知的模块名，AST 层面判不出它是谁）、
    `os.__getattribute__("name")`、`operator.attrgetter("name")(os)`、
    `getattr(os, "na" + "me")`（属性名不是字面常量）。这四种要绕得**故意**，成本高于收益：
    本守卫的口径是"顺手写的平台判点跑不掉"，不是"对抗性隐藏也跑不掉"。把它写成后者，
    下一个读它的人就会拿它当证据放行真正该拦的东西。
    """
    tree = ast.parse(source)
    offenders: list[int] = []
    # 默认把 `os` / `sys` 这两个名字本身当作那两个模块（片段里没有 import 也要抓得住），
    # 再叠加别名导入（`import os as _os`）——第 4 轮 N2 要的正是"换个马甲也抓"。
    aliases: dict[str, str] = {root: root for root in _PLATFORM_MODULES}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in _PLATFORM_MODULES:
                    aliases[alias.asname or root] = root
        elif isinstance(node, ast.ImportFrom) and node.module in _PLATFORM_MODULES:
            for alias in node.names:
                if alias.name in _PLATFORM_MODULES[node.module]:
                    offenders.append(node.lineno)
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            holder = node.value
            if (
                isinstance(holder, ast.Name)
                and holder.id in aliases
                and node.attr in _PLATFORM_MODULES[aliases[holder.id]]
            ):
                offenders.append(node.lineno)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id != "getattr" or len(node.args) < 2:
                continue
            holder, attr = node.args[0], node.args[1]
            if (
                isinstance(holder, ast.Name)
                and holder.id in aliases
                and isinstance(attr, ast.Constant)
                and attr.value in _PLATFORM_MODULES[aliases[holder.id]]
            ):
                offenders.append(node.lineno)
    return sorted(set(offenders))


def test_the_platform_predicate_guard_catches_the_evasive_forms() -> None:
    """守卫自己得拦住"换个写法"（第 4 轮 N2）——否则它是一条**只会盯今天这一种形态**的假闸。

    两个方向都要钉：六种绕法**都必须被抓**，而这个文件真正在用的合法读取（`os.getuid()` /
    `os.defpath` / `os.path.*`）**都不许被误抓**——误抓会逼人把判点藏得更深。
    """
    forbidden = [
        'POSIX = os.name == "posix"',
        'POSIX = getattr(os, "name") == "posix"',
        'import os as _os\nPOSIX = _os.name == "posix"',
        'POSIX = sys.platform == "linux"',
        'POSIX = getattr(sys, "platform") == "linux"',
        'from os import name\nPOSIX = name == "posix"',
    ]
    for src in forbidden:
        assert _find_platform_predicates(src), f"这段自算平台判点的源码没被抓到：{src!r}"
    allowed = [
        'import os\nprint(os.getuid(), os.defpath, os.path.join("a", "b"))',
        "import sys\nprint(sys.version_info)",
        'POSIX = probe_subprocess_caps()["posix"]',
    ]
    for src in allowed:
        assert _find_platform_predicates(src) == [], f"合法读取被误抓：{src!r}"


def test_this_file_declares_no_second_platform_predicate() -> None:
    """本文件**不许自算平台判点**——所有 POSIX 判定只能读探测的返回值。

    第 3 轮独立审查的一般项：模块顶部曾经另立 ``POSIX = os.name == "posix"``，与
    ``probe_subprocess_caps()`` 各算各的。今天两者恰好等值、没有假绿，但那是**巧合承重**：
    探测的 posix 判据一旦加宽（例如要求同时有 ``resource``），那几处 ``if POSIX`` 就会
    与主判点分家——而纪律 6 ④要的正是「诊断口径与执行链的判定同源」。

    判据从"人记得住"变成机器可判：AST 里没有任何"读平台属性"的动作 ⇒ 平台判定只可能来自
    探测。注释与 docstring 里提 POSIX 不算判点（它们不是被解析出来的表达式）。形态覆盖面
    由 ``_find_platform_predicates`` 承担，它自己有一条用例钉住六种绕法与三种合法读法。
    """
    offenders = _find_platform_predicates(Path(__file__).read_text(encoding="utf-8"))
    assert (
        not offenders
    ), f"本文件在第 {offenders} 行自算了平台判点；请改读 probe_subprocess_caps() 的返回值"


# ---------------------------------------------------------------------------
# L3.1 进程数闸（RLIMIT_NPROC）：判据是纯函数，父进程探测与子进程落闸共用一份
# ---------------------------------------------------------------------------


def _nproc_verdict(plan: dict) -> tuple:
    """取判定面三个字段。

    刻意不比整个 dict：``uid_process_count`` / ``uid_task_count`` 是**回显的测量值**（本来就
    随调用方给什么而变），把它一并比进去就成了"回声不许有回声"的假断言，下一个人无从判断
    这条守卫在守什么。
    """
    return (plan["applies"], plan["no_headroom"], plan["reason"])


def test_the_process_count_gate_declines_while_the_uid_is_shared() -> None:
    """不降权 ⇒ 进程数闸**刻意不落**，而且要说清为什么不是"忘了落"。

    这条不是保守，是算术：``RLIMIT_NPROC`` 记的是**整个 real uid 的任务总数（线程也算）**，
    不是这个子树的进程数。``TOOL_SANDBOX_SUBPROCESS_UID=-1`` 时墙里墙外是同一个 uid，那个计数里
    已经含着登录会话的几百个进程 ⇒ 把上限设成 32 会让"工具偶尔起一个子进程"在某些天
    直接 EAGAIN（假拦截，换个时间又好，最难查的那种）；把上限抬到计数之上才不难用，
    而那上限就已经不构成任何约束了（真拦截形同虚设）。两种都比重复一次握手贵。
    """
    plan = worker.nproc_gate_plan(uid=-1, cap=sandbox_module.RLIMIT_NPROC, uid_process_count=None)
    assert plan["applies"] is False, "共享 uid 却报'落得下' ⇒ 探测在替一扇没关的门背书"
    assert "whole real uid" in plan["reason"], plan
    assert "dedicated uid" in plan["reason"], "拒绝必须给出可行的出路，不止说'不行'"
    # 计数拿不拿得到，都不该改变这个分支的**结论**（判定不许依赖 /proc 的可读性）。
    # 两本账各自喂一遍：只测一本的话，"另一本被喂进判定"这条违规就没人管。
    assert _nproc_verdict(plan) == _nproc_verdict(
        worker.nproc_gate_plan(uid=-1, cap=sandbox_module.RLIMIT_NPROC, uid_process_count=9999)
    ), "同 uid 下结论随 /proc 可读性漂移 ⇒ 两条机器上口径不一样"
    assert _nproc_verdict(plan) == _nproc_verdict(
        worker.nproc_gate_plan(
            uid=-1, cap=sandbox_module.RLIMIT_NPROC, uid_process_count=None, uid_task_count=9999
        )
    ), "任务账喂进共享 uid 那一支的判定 ⇒ 该分支的结论开始依赖测量"


def test_the_process_count_gate_applies_under_a_dedicated_uid_and_reports_headroom() -> None:
    """降权到独立 uid ⇒ 闸落得下；但该 uid 已经占着进程时，必须如实报"没有余量"。

    ``no_headroom`` 不是装饰：选了 ``65534``（``nobody``）这类共享 uid 时，计数里含着
    别人已经在跑的进程，闸落下去的效果是"墙里一个子进程都起不了"。那是**可用性**问题
    （安全方向照旧是收紧的），所以要让它可见，而不是让它伪装成"闸工作正常"。

    ⚠️ 反方向的读法更危险，所以这条同时钉住措辞：容器里数到的计数是**下限**
    （2026-10-26 实测：容器内 /proc 数到 uid 65534 为 0，而 soft=8 的闸在第 0 次 fork
    就 EAGAIN——内核那份计数跨整机，不认 PID 命名空间）。``no_headroom=false`` 因此
    **不是**"这里还起得来进程"的保证，reason 里必须带上这句话，否则日志会被读成担保。

    ⚠️ 还有一本账的问题：内核记的是**任务**（线程也算），不是进程。同一时刻"进程 1 /
    任务 6"是实测存在的形态（1 个进程起 5 个线程），所以只看进程账会报"余量充足"而墙里
    一次 fork 都拿不到。这条把两本账分别喂进判定，并钉住措辞里报的是哪一本。
    """
    cap = sandbox_module.RLIMIT_NPROC
    plan = worker.nproc_gate_plan(uid=65534, cap=cap, uid_process_count=1)
    assert plan["applies"] is True and plan["no_headroom"] is False, plan
    assert "uid 65534" in plan["reason"] and str(cap) in plan["reason"], plan
    assert "lower bound" in plan["reason"], "只报「数到 1 个」却不报下限 ⇒ 余量被读成担保"
    # 账本要说清是哪一本：只有进程账时冒充"tasks"，运维会把"这里看到 1 个进程"读成
    # "内核那一刻只记了 1 个任务"——而那正是判定要防的假绿。
    assert "1 process(s)" in plan["reason"], plan["reason"]
    both = worker.nproc_gate_plan(uid=65534, cap=cap, uid_process_count=1, uid_task_count=9)
    assert "9 task(s)" in both["reason"] and "processes: 1" in both["reason"], both["reason"]
    tight = worker.nproc_gate_plan(uid=65534, cap=cap, uid_process_count=cap + 5)
    assert tight["applies"] is True, "共享了别人的 uid 不构成放松闸的理由：收紧方向不许反悔"
    assert tight["no_headroom"] is True and "cannot fork at all" in tight["reason"], tight
    # 进程账还很空、任务账已到 cap ⇒ 判"没有余量"：这是这本账唯一说了算的一支。
    tasks_full = worker.nproc_gate_plan(uid=65534, cap=cap, uid_process_count=1, uid_task_count=cap)
    assert tasks_full["applies"] is True and tasks_full["no_headroom"] is True, tasks_full
    assert "cannot fork at all" in tasks_full["reason"], tasks_full
    assert (
        worker.nproc_gate_plan(uid=65534, cap=0, uid_process_count=None)["applies"] is False
    ), "cap<=0 却照落 ⇒ '关掉这个闸'变成'落一个 0 的闸'，语义完全不同"
    # 计数拿不到时不许编一个"余量充足"：None 的措辞必须与"数到 1 个"不同。
    unmeasured = worker.nproc_gate_plan(uid=65534, cap=cap, uid_process_count=None)
    assert unmeasured["applies"] is True and unmeasured["no_headroom"] is False, unmeasured
    assert "was not readable" in unmeasured["reason"] or "not measurable" in unmeasured["reason"]
    assert "1 process" not in unmeasured["reason"], "没测到却回显出数字 ⇒ 无中生有"
    # 一本有、一本没有：结论由拿得到的那本托住（保守方向是"报没余量"），措辞降级为进程账。
    partial = worker.nproc_gate_plan(uid=65534, cap=cap, uid_process_count=cap, uid_task_count=None)
    assert partial["no_headroom"] is True, partial
    assert "process(s)" in partial["reason"] and "tasks: None" in partial["reason"], partial


def test_the_process_count_gate_declines_for_uid_zero_because_root_is_exempt() -> None:
    """``TOOL_SANDBOX_SUBPROCESS_UID=0`` 是一支**空闸**，必须显式拒绝，不能"落了再说"。

    内核把 ``RLIMIT_NPROC`` 的检查跳过了持有 ``CAP_SYS_RESOURCE`` 的任务，而 uid 0 默认
    持有它。容器内实测（2026-10-26，`p125_linux`，root）：soft 写成 5 并回读确认为 5，
    随后照样连起 12 个进程、一次都没阻塞 ⇒ "调过 setrlimit"不是"闸生效"的证据。

    这与 ``uid=-1`` 那支是两种缺口（共享 uid 会**假拦**，uid 0 是**永不拦**），
    所以 reason 必须各说各的：把两者并成一条文案，运维就会把"我配了 root"读成"我忘了配 uid"。
    """
    cap = sandbox_module.RLIMIT_NPROC
    plan = worker.nproc_gate_plan(uid=0, cap=cap, uid_process_count=None)
    assert plan["applies"] is False, "uid 0 报'落得下' ⇒ 探测在替一道内核根本不检查的闸背书"
    assert "CAP_SYS_RESOURCE" in plan["reason"] and "uid 0" in plan["reason"], plan
    assert "non-zero" in plan["reason"], "拒绝必须给出可行的出路，不止说'不行'"
    assert (
        plan["reason"] != worker.nproc_gate_plan(uid=-1, cap=cap, uid_process_count=None)["reason"]
    ), "两支缺口共用一条文案 ⇒ 配置错误与未配置在日志里分不清"
    # 判定面在 uid 0 这一支也不许依赖 /proc：给不给计数，结论都一样。
    assert _nproc_verdict(plan) == _nproc_verdict(
        worker.nproc_gate_plan(uid=0, cap=cap, uid_process_count=9999)
    )


def test_the_two_nproc_branches_have_one_shape() -> None:
    """落与不落两支的键集必须一致：消费方一旦要靠「有没有某个键」分支，迟早只关一支。"""
    applied = worker.nproc_gate_plan(uid=1, cap=8, uid_process_count=2)
    declined = worker.nproc_gate_plan(uid=-1, cap=8, uid_process_count=None)
    root_declined = worker.nproc_gate_plan(uid=0, cap=8, uid_process_count=None)
    task_ledger = worker.nproc_gate_plan(uid=1, cap=8, uid_process_count=1, uid_task_count=7)
    assert (
        set(applied)
        == set(declined)
        == set(root_declined)
        == set(task_ledger)
        == {
            "applies",
            "cap",
            "uid_process_count",
            "uid_task_count",
            "no_headroom",
            "reason",
        }
    )


def test_probe_and_worker_decide_the_process_gate_from_one_predicate() -> None:
    """纪律 6 ④：这一闸的判定只许有一个源。

    父进程用它决定"探测里写什么"，子进程用它决定"落不落"。两份实现漂移的后果不是报错，
    而是**探测说这面墙有进程数闸、墙里其实没有**（或反过来把能落的机器判成不能落）——
    运维读到的是前者，出事的是后者。所以这里比对两侧的**输出**，不是比对措辞。
    """
    cap = sandbox_module.RLIMIT_NPROC
    fresh = probe_subprocess_caps()
    assert fresh["nproc_gate"] == worker.nproc_gate_plan(
        uid=int(settings.TOOL_SANDBOX_SUBPROCESS_UID),
        cap=cap,
        uid_process_count=None,
        uid_task_count=None,
    ), "探测与判定函数分家了：运维看到的闸状态不再等于墙里真会落的闸"
    # 进程数闸**不进** missing：它是"这一档在什么配置下多买到的一个维度"，
    # 不是"这台机器兑现不了已承诺的维度"。混进 missing 会让 runnable 随 uid 漂移，
    # 于是"开了 flag 但没配独立 uid"的机器上整档被拒——那是行为变更，不是加固。
    assert all("nproc" not in item for item in fresh["missing"]), fresh["missing"]
    if fresh["runnable"]:
        # 判点是"**非 0 且独立**的 uid"：uid 0 被内核豁免（CAP_SYS_RESOURCE），
        # 写 `>= 0` 会把那支空闸当成"落了"。
        assert fresh["nproc_gate"]["applies"] is (int(settings.TOOL_SANDBOX_SUBPROCESS_UID) > 0)


@pytest.mark.asyncio
async def test_a_missing_process_gate_is_warned_about_once(
    tier_on, probe_ok, fake_spawn, monkeypatch, caplog
) -> None:
    """墙里如实回报"这道闸没落" ⇒ 必须留下响的日志，但**不许每次调用刷一条**。

    纪律 2 要的是"降级要留痕"；一次一百万调用的路径上留痕的方式如果是刷屏，
    下一批人就会把那行日志删掉，于是痕迹和噪声一起消失。
    """
    monkeypatch.setattr(sandbox_module, "_GAP_WARNINGS", set())
    fake_spawn["result"]["caps"] = {
        "pid": 4242,
        "uid": 1000,
        "resource_module_available": True,
        "rlimit_applied": {},
        "nproc_gate": worker.nproc_gate_plan(
            uid=-1, cap=sandbox_module.RLIMIT_NPROC, uid_process_count=None
        ),
    }
    with caplog.at_level("WARNING", logger="agentix.tools.sandbox"):
        await tier_on.run(probes.echo, {})
        await tier_on.run(probes.echo, {})
    gate_lines = [r for r in caplog.records if "RLIMIT_NPROC" in r.getMessage()]
    assert len(gate_lines) == 1, f"缺口日志应恰好一条（每因一次），实际 {len(gate_lines)} 条"
    assert "dedicated uid" in gate_lines[0].getMessage()


@pytest.mark.asyncio
async def test_an_applied_process_gate_is_not_warned_about(
    tier_on, probe_ok, fake_spawn, monkeypatch, caplog
) -> None:
    """反例：闸落下了却还告警 ⇒ 告警就变成噪声，而下一次真缺口没人看。"""
    monkeypatch.setattr(sandbox_module, "_GAP_WARNINGS", set())
    fake_spawn["result"]["caps"] = {
        "pid": 4242,
        "uid": 65534,
        "resource_module_available": True,
        "rlimit_applied": {"RLIMIT_NPROC": {"requested": 32, "effective": [32, 32]}},
        "nproc_gate": worker.nproc_gate_plan(
            uid=65534, cap=sandbox_module.RLIMIT_NPROC, uid_process_count=1
        ),
    }
    with caplog.at_level("WARNING", logger="agentix.tools.sandbox"):
        await tier_on.run(probes.echo, {})
    assert not [r for r in caplog.records if "RLIMIT_NPROC" in r.getMessage()]


@pytest.mark.asyncio
async def test_non_posix_refuses_without_calling_handler(tier_on, spawn_tracker) -> None:
    """本机（Windows）的**主口径**：探测不过 ⇒ 拒绝；handler 不调、进程不起。"""
    if _CAPS["posix"]:
        pytest.skip("本机是 POSIX；拒绝路径由 test_uid_requested_without_setuid_is_refused 覆盖")
    with pytest.raises(SandboxUnavailable) as caught:
        await tier_on.run(probes.echo, {"a": 1})
    message = str(caught.value)
    assert "refusing to run" in message and "silently downgrading" in message
    assert "resource" in message and "process-group" in message, "拒绝要说出缺哪几道闸"
    assert spawn_tracker == []


@pytest.mark.asyncio
async def test_a_lying_probe_downgrades_nothing(tier_on, monkeypatch) -> None:
    """探测被假扮成"可用"时，**子进程自己**仍然拒绝 ⇒ 两道闸不共用一个判断。

    这条必须真起进程才有意义：它证明"父进程说能跑"不等于"墙里落下了闸"。
    本机（Windows）用真解释器跑真子进程，只是资源闸落不下去——而落不下去这件事
    由子进程报回来，父进程原样抛出。
    """
    if _CAPS["posix"]:
        pytest.skip("POSIX 上探测说谎就等于真可用，测不出子进程侧的独立拒绝")
    monkeypatch.setattr(sandbox_module, "probe_subprocess_caps", _runnable_caps)
    with pytest.raises(SubprocessSandboxError) as caught:
        await tier_on.run(probes.echo, {"a": 1})
    assert "[limits]" in str(caught.value)
    assert "resource module unavailable" in str(caught.value)


@pytest.mark.asyncio
async def test_uid_requested_without_setuid_is_refused(tier_on, monkeypatch, spawn_tracker) -> None:
    monkeypatch.setattr(settings, UID_KEY, 65534)
    monkeypatch.setattr(
        sandbox_module,
        "probe_subprocess_caps",
        lambda: {
            "posix": True,
            "resource": True,
            "setuid": False,
            "runnable": True,
            "missing": [],
            "nproc_gate": {
                "applies": True,
                "cap": None,
                "uid_process_count": None,
                "uid_task_count": None,
            },
        },
    )
    with pytest.raises(SandboxUnavailable, match="cannot drop to uid 65534"):
        await tier_on.run(probes.echo, {})
    assert spawn_tracker == [], "降权落不下还照常起进程 ⇒ 声明的降权成了注释"


# ---------------------------------------------------------------------------
# L4 handler 可重建性（父进程侧与子进程侧同源）
# ---------------------------------------------------------------------------


def test_module_level_handlers_describe_cleanly() -> None:
    assert sandbox_module._describe_handler(probes.echo) == (
        "tests.subprocess_probe_handlers",
        "echo",
    )
    assert sandbox_module._describe_handler(probes.async_echo) == (
        "tests.subprocess_probe_handlers",
        "async_echo",
    )


def _main_module_handler(args: dict) -> dict:
    return {"in": "main"}


@pytest.mark.asyncio
async def test_unreconstructible_handlers_are_refused_before_spawn(
    tier_on, probe_ok, spawn_tracker, monkeypatch
) -> None:
    """六类真实事故都在起进程**之前**拒：闭包 / lambda / 绑定方法 / ``__main__`` /
    运行时构造的可调用对象（partial、实例）/ 名字过得了但**形状**过不了的类。

    最后一类是分水岭：``module`` + ``qualname`` 齐全且不含 ``<`` 也不保证能重建出**那个工具**——
    按名字重建出一个类，"调用"它是在新建实例。所以判据必须同时看形状。
    """

    def closure(args: dict) -> dict:
        return args

    class Holder:
        def method(self, args: dict) -> dict:
            return args

    class Callable:
        async def __call__(self, args: dict) -> dict:
            return args

    # monkeypatch 会把 `__module__` 改回去：直接赋值会污染整个 pytest 会话。
    monkeypatch.setattr(_main_module_handler, "__module__", "__main__")
    cases = [
        ("closure", closure, "lambda/nested function/closure"),
        ("lambda", lambda args: args, "lambda/nested function/closure"),
        ("bound", Holder().method, "bound method"),
        ("main", _main_module_handler, "__main__"),
        ("partial", partial(probes.echo), "no module/qualname"),
        ("instance", Callable(), "no module/qualname"),
        ("class", probes.CallableHandler, "not a module-level function"),
    ]
    for label, handler, phrase in cases:
        with pytest.raises(SubprocessSandboxError) as caught:
            await tier_on.run(handler, {})
        assert phrase in str(caught.value), f"{label} 的拒绝措辞没点名形态：{caught.value}"
    assert spawn_tracker == [], "重建不出来却起了进程 = 把可预见的失败换成一次进程生命周期"


def test_worker_reconstruct_refuses_the_same_shapes() -> None:
    """子进程侧**同源**判据：``__main__``、带 ``<`` 的 qualname、漂移的属性名、非可调用、非函数。"""
    with pytest.raises(worker.WorkerError, match="not importable"):
        worker.reconstruct("__main__", "echo")
    with pytest.raises(worker.WorkerError, match="lambda/closure"):
        worker.reconstruct("tests.subprocess_probe_handlers", "outer.<locals>.inner")
    with pytest.raises(worker.WorkerError, match="missing"):
        worker.reconstruct("tests.subprocess_probe_handlers", "no_such_handler")
    with pytest.raises(worker.WorkerError, match="not callable"):
        worker.reconstruct("tests.subprocess_probe_handlers", "PROBE_SECRET_VALUE")
    # 形状判据在子进程侧同样生效：类能 import、能 callable，但它不是那个工具。
    with pytest.raises(worker.WorkerError, match="not a module-level function"):
        worker.reconstruct("tests.subprocess_probe_handlers", "CallableHandler")
    assert worker.reconstruct("tests.subprocess_probe_handlers", "echo") is probes.echo


def test_import_failure_in_child_is_a_named_stage() -> None:
    """模块改名 / 包没装 ⇒ ``stage=reconstruct`` 的信封，而不是父进程里一段裸栈。"""
    with pytest.raises(worker.WorkerError, match="import"):
        worker.reconstruct("agentix.no_such_module_at_all", "echo")


# ---------------------------------------------------------------------------
# L5 协议与体积闸
# ---------------------------------------------------------------------------


def _request(**kw) -> dict:
    base = {
        "protocol": worker.PROTOCOL_VERSION,
        "tool_name": "echo",
        "module": "tests.subprocess_probe_handlers",
        "qualname": "echo",
        "arguments": {"a": 1},
        "limits": {"as_mb": 512, "cpu_s": 30, "fsize_mb": 32},
        "uid": -1,
        "nonce": "p125-nonce",
    }
    base.update(kw)
    return base


class _LineStream:
    """假 stdin：只给一行（与真协议同形）。"""

    def __init__(self, text: str) -> None:
        self._lines = text.splitlines(keepends=True)

    def readline(self) -> str:
        return self._lines.pop(0) if self._lines else ""


@pytest.mark.parametrize(
    ("mutate", "phrase"),
    [
        (lambda r: r.pop("protocol"), "mismatch"),
        (lambda r: r.__setitem__("protocol", 99), "mismatch"),
        (lambda r: r.pop("arguments"), "missing key"),
        (lambda r: r.__setitem__("arguments", []), "must be objects"),
        (lambda r: r.pop("nonce"), "missing key"),
        (lambda r: r.__setitem__("nonce", ""), "nonce must be"),
        (lambda r: r.__setitem__("nonce", 7), "nonce must be"),
    ],
)
def test_request_protocol_is_strict(mutate, phrase) -> None:
    request = _request()
    mutate(request)
    with pytest.raises(worker.WorkerError) as caught:
        worker.read_request(_LineStream(json.dumps(request)))
    assert caught.value.stage == "protocol" and phrase in caught.value.message


def test_malformed_and_empty_requests_fail_loudly() -> None:
    with pytest.raises(worker.WorkerError, match="malformed"):
        worker.read_request(_LineStream("{not json"))
    with pytest.raises(worker.WorkerError, match="empty stdin"):
        worker.read_request(_LineStream(""))


def test_envelope_roundtrip_and_size_gate() -> None:
    class Sink:
        def __init__(self) -> None:
            self.text = ""

        def write(self, s: str) -> None:
            self.text += s

        def flush(self) -> None:
            pass

    ok = Sink()
    worker.emit(
        {"protocol": worker.PROTOCOL_VERSION, "ok": True, "nonce": "n-1", "data": {"a": 1}}, ok
    )
    assert ok.text.startswith(worker.SENTINEL)
    assert json.loads(ok.text[len(worker.SENTINEL) :].strip())["ok"] is True

    big = Sink()
    oversized = {
        "protocol": worker.PROTOCOL_VERSION,
        "ok": True,
        "nonce": "n-2",
        "data": "x" * (worker.MAX_PAYLOAD_BYTES + 10),
    }
    worker.emit(oversized, big)
    parsed = json.loads(big.text[len(worker.SENTINEL) :].strip())
    assert (
        parsed["ok"] is False and parsed["stage"] == "serialize"
    ), "超限要改写成失败信封，不能截断当好结果"
    assert (
        parsed["nonce"] == "n-2"
    ), "改写失败信封必须照抄 nonce：丢了它，父进程会把「结果太大」读成「应答不可信」，真正的原因就盖掉了"


def test_parent_takes_the_trusted_sentinel_line_through_noise_and_forgery() -> None:
    """stdout 上混着业务日志与**一条伪造的 ok 信封**时，父进程取带本次 nonce 的那条。

    旧口径「取最后一条哨兵」在这里是**不安全**的：伪造行完全可以写在真信封之后
    （孙进程继承了 fd 1，晚写一行即可顶包）。安全的是"只认带本次 nonce 的、其中取最后一条"。
    """
    forged = json.dumps({"protocol": worker.PROTOCOL_VERSION, "ok": True, "data": {"pwned": True}})
    real = json.dumps(
        {"protocol": worker.PROTOCOL_VERSION, "ok": True, "nonce": "n-9", "data": {"k": 1}}
    )
    noisy_stdout = (
        "business log line 1\n"
        f"{worker.SENTINEL} {real}\n"
        "another line that is not the envelope\n"
        f"{worker.SENTINEL} {forged}\n"
    )
    assert SubprocessSandbox._parse_envelope(noisy_stdout, 0, "", "n-9")["data"] == {"k": 1}


@pytest.mark.parametrize(
    ("body", "phrase"),
    [
        (
            json.dumps({"protocol": worker.PROTOCOL_VERSION, "ok": True, "data": {"pwned": True}}),
            "untrusted success envelope",
        ),
        (
            json.dumps(
                {"protocol": worker.PROTOCOL_VERSION, "ok": True, "nonce": "stale", "data": {}}
            ),
            "untrusted success envelope",
        ),
    ],
    ids=["no_nonce_at_all", "nonce_from_another_call"],
)
def test_wall_side_code_cannot_buy_a_success_by_printing_the_sentinel(body: str, phrase) -> None:
    """协议 v2 的存在理由：墙内代码**有 stdout 写权限**，而信封就写在 stdout 上。

    一句 ``print(SENTINEL + ' {"ok": true, ...}')`` 换不来一次成功结果——无论它有没有
    带上一次的 nonce（父进程按调用生成，跨调用不复用）。
    """
    with pytest.raises(SubprocessSandboxError, match=phrase):
        SubprocessSandbox._parse_envelope(f"{worker.SENTINEL} {body}\n", 0, "", "n-9")


#: 「信封里根本没有 ``ok`` 这个键」的占位值——与显式 ``"ok": null`` 是**两种形态**：
#: 前者是墙内代码压根没写，后者是它写了但写成了 JSON null。父进程两条都得拒成同一个
#: 理由，否则将来有人把判据放宽成 ``is not None``（把"缺失"从畸形网里放出去）就测不出。
_OMIT_OK = object()


@pytest.mark.parametrize(
    ("ok_value", "shown"),
    [
        (1, "1"),
        (1.0, "1.0"),
        ("true", "'true'"),
        (None, "None"),
        (_OMIT_OK, "None"),
    ],
    ids=["json_number", "json_float", "json_string", "json_null", "key_absent"],
)
def test_ok_must_be_a_strict_boolean_not_a_truthy_value(ok_value, shown: str) -> None:
    """``ok`` 只认 ``True`` / ``False``——``1`` / ``1.0`` / ``"true"`` / ``null`` / 缺失一律按**畸形**拒。

    这是 2026-10-25 第 2 轮独立审查点名的绕过路径：解析器若把 ``"ok": 1`` 归进"失败桶"，
    而终局判定用**真值**（``not envelope.get("ok")``），那么 ``not 1`` 是 False ⇒ 这条
    伪造信封被当**成功结果**返回，归属凭证在这一路上**从未执行过**——nonce 校验再严也
    拦不住一个"压根没进 trusted 桶"的值。

    worker 自己只写布尔值（``ok: True`` / ``ok: False``），所以非布尔的信封只可能是墙内
    代码印的；把它归拢进"失败桶"等于放它进一条**会被真值判成成功**的路。

    「缺失」与「显式 null」也钉在同一张网上（第 3 轮审查的一般项）：判据若写成
    ``e.get("ok") is not None`` 而不是「既不是 ``True`` 也不是 ``False``」，这两条就会
    从畸形网掉进"压根没有可解析应答"那一桶——**照样被拒，但拒因换了**，而换掉的这个
    理由会把「墙内写了个怪东西」和「worker 没写出应答」两种故障混成一谈。断言锁的是
    拒因，不是"反正都红"。
    """
    envelope = {"protocol": worker.PROTOCOL_VERSION, "data": {"pwned": True}}
    if ok_value is not _OMIT_OK:
        envelope["ok"] = ok_value
    body = json.dumps(envelope)
    with pytest.raises(SubprocessSandboxError, match="malformed response envelope") as caught:
        SubprocessSandbox._parse_envelope(f"{worker.SENTINEL} {body}\n", 0, "", "n-9")
    message = str(caught.value)
    assert shown in message, message
    assert "pwned" not in message, "伪造载荷不该出现在任何成功面上"


@pytest.mark.asyncio
async def test_final_verdict_is_strict_boolean_even_if_the_parser_were_sloppy(
    tier_on, probe_ok, fake_spawn, monkeypatch
) -> None:
    """终局判定必须是**严格布尔**，不能是"真值"——解析器之外的第二道闸。

    刻意把解析器**桩成宽的**（返回 ``"ok": 1``），只验终局那一行。解析器自己拒这类
    信封由上面那条用例盖着，但"最后一道关卡自己松了"这件事必须被**独立**钉住：
    否则把 ``is not True`` 改回 ``not envelope.get("ok")`` 不会有任何用例变红——
    那正是第 2 轮审查指出的"绕过的另一半"。
    """
    monkeypatch.setattr(
        SubprocessSandbox,
        "_parse_envelope",
        staticmethod(lambda *_args, **_kwargs: {"ok": 1, "data": {"pwned": True}}),
    )
    with pytest.raises(SubprocessSandboxError):
        await tier_on.run(probes.echo, {"a": 1})


def test_failure_envelope_without_nonce_is_still_honored() -> None:
    """失败**不**校验 nonce，这是刻意的不对称。

    协议阶段就错时 worker 还没解析出请求，确实拿不到 nonce；对失败硬校验只会把
    "缺 key / 协议版本不符"这种可诊断的真相，盖成"没有应答"。失败信封最多让调用更响，
    伪造它换不到任何数据——**代价是要说清的**：墙内代码可以印一条伪造的失败信封
    **顶替**真实失败原因（只影响诊断，不产生数据）。
    """
    body = json.dumps(
        {
            "protocol": worker.PROTOCOL_VERSION,
            "ok": False,
            "stage": "protocol",
            "error": "request missing key 'nonce'",
        }
    )
    envelope = SubprocessSandbox._parse_envelope(f"{worker.SENTINEL} {body}\n", 0, "", "n-9")
    assert envelope["ok"] is False and envelope["stage"] == "protocol"


def test_real_worker_envelope_carries_the_nonce_the_parent_will_check(monkeypatch) -> None:
    """生产侧闭环：**真** ``run_request`` 造出来的信封，要能被**真**解析器按本次 nonce 认下。

    补这一条的理由是替身的固有盲区：本文件其余用例在 Windows 上走的是「替身 stdout」，
    而替身的 nonce 是我们自己写进去的——把 worker 里回显那行改成常量，替身用例照样全绿。
    归属凭证是两侧契约，就必须有一层「一侧真造、另一侧真验」。
    这里只把 ``apply_gates`` 换成桩（它是 POSIX 专属，真值由 ``*_read_back``
    与容器取证覆盖），``reconstruct`` / invoke / 信封构造全部走真码。
    """
    monkeypatch.setattr(worker, "apply_gates", lambda limits, uid: {"stubbed_gates": True})
    request = {
        "protocol": worker.PROTOCOL_VERSION,
        "tool_name": "echo",
        "module": "tests.subprocess_probe_handlers",
        "qualname": "echo",
        "arguments": {"text": "hi"},
        "limits": {},
        "nonce": "contract-nonce",
    }
    envelope, _caps = worker.run_request(request)
    assert (
        envelope["nonce"] == "contract-nonce"
    ), "worker 不回显 nonce ⇒ 父进程那句「只认带本次 nonce 的应答」根本无米下锅"
    assert envelope["ok"] is True
    assert envelope["tool_name"] == "echo"

    line = f"{worker.SENTINEL} {json.dumps(envelope, ensure_ascii=False)}\n"
    accepted = SubprocessSandbox._parse_envelope(line, 0, "", "contract-nonce")
    assert accepted["data"] == envelope["data"], "真信封应被真解析器原样收下"
    # 反向半句：同一条应答换一个 nonce 去校验必须被拒，否则上面那句"认下"是无条件放行
    with pytest.raises(SubprocessSandboxError, match="untrusted success envelope"):
        SubprocessSandbox._parse_envelope(line, 0, "", "some-other-call")


@pytest.mark.parametrize(
    ("stdout_text", "phrase"),
    [
        ("no envelope at all here\n", "no response envelope"),
        (f"{worker.SENTINEL} {{bad json\n", "malformed response envelope"),
        (f'{worker.SENTINEL} {json.dumps({"protocol": 42, "ok": True})}\n', "protocol mismatch"),
    ],
)
def test_parent_rejects_every_broken_envelope_shape(stdout_text: str, phrase: str) -> None:
    with pytest.raises(SubprocessSandboxError, match=phrase):
        SubprocessSandbox._parse_envelope(stdout_text, 0, "stderr tail", "n-9")


@pytest.mark.asyncio
async def test_child_that_wrote_no_envelope_is_reported_with_stderr(
    tier_on, probe_ok, monkeypatch
) -> None:
    """子进程连信封都没写出来（信号杀 / 段错误）⇒ 报错带 exit code 与 stderr 尾巴。"""

    async def _spawn(*_args, **_kwargs):
        return _FakeProc(_Stream(), _Stream(b"Segmentation fault inside the wall"), exit_code=139)

    monkeypatch.setattr(sandbox_module.asyncio, "create_subprocess_exec", _spawn)
    with pytest.raises(SubprocessSandboxError) as caught:
        await tier_on.run(probes.echo, {})
    assert "exit 139" in str(caught.value)
    assert "Segmentation fault" in str(caught.value)


@pytest.mark.asyncio
async def test_failure_envelope_stage_is_carried_into_the_error(
    tier_on, probe_ok, fake_spawn
) -> None:
    fake_spawn["result"] = {
        "protocol": worker.PROTOCOL_VERSION,
        "ok": False,
        "stage": "invoke",
        "error": "ValueError: boom",
    }
    with pytest.raises(SubprocessSandboxError, match=r"\[invoke\] ValueError: boom"):
        await tier_on.run(probes.echo, {})


@pytest.mark.asyncio
async def test_request_over_the_wire_cap_is_refused_without_spawn(
    tier_on, probe_ok, spawn_tracker
) -> None:
    """参数体积闸：超上限 ⇒ 不起进程（否则一次调用先把父进程内存吃满）。"""
    huge = {"blob": "x" * (MAX_REQUEST_BYTES + 10)}
    with pytest.raises(SubprocessSandboxError, match="request_too_large"):
        await tier_on.run(probes.echo, huge)
    assert spawn_tracker == []


@pytest.mark.asyncio
async def test_non_serializable_arguments_fail_before_spawn(
    tier_on, probe_ok, spawn_tracker
) -> None:
    with pytest.raises(SubprocessSandboxError, match="request_not_json_serializable"):
        await tier_on.run(probes.echo, {"obj": object()})
    assert spawn_tracker == []


@pytest.mark.asyncio
async def test_child_stdout_over_the_response_cap_is_cut_off(
    tier_on, probe_ok, monkeypatch
) -> None:
    """应答体积闸：超上限 ⇒ 明确错因并清场，而不是把父进程内存吃满。"""

    async def _spawn(*_args, **_kwargs):
        return _FakeProc(_InfiniteStream())

    monkeypatch.setattr(sandbox_module.asyncio, "create_subprocess_exec", _spawn)
    with pytest.raises(SubprocessSandboxError, match="stdout_too_large"):
        await tier_on.run(probes.echo, {})


# ---------------------------------------------------------------------------
# L6 真子进程
# ---------------------------------------------------------------------------


def test_worker_module_level_imports_are_stdlib_only() -> None:
    """墙内侧**先落闸再 import 业务代码** ⇒ worker 模块级不许出现 ``agentix``（纪律 6 ①）。"""
    tree = ast.parse(
        (PROJECT_ROOT / "agentix/tools/subprocess_worker.py").read_text(encoding="utf-8")
    )
    top_imports = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            top_imports += [alias.name.split(".")[0] for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            top_imports.append((node.module or "").split(".")[0])
    assert "agentix" not in top_imports, "模块级 import agentix ⇒ 业务依赖抢在资源闸之前被拉起来"
    assert "resource" not in top_imports, "resource 必须在 apply_gates 里 import，否则闸落晚了一步"


def test_worker_entrypoint_is_a_sibling_script_not_a_module_form(monkeypatch) -> None:
    """启动形态的**入口本身**：绝对路径、与端口同目录、文件真实存在。

    与下面那条 import 面用例配对——一条钉"起的是哪个文件"，一条钉"起来之后进程里有什么"。
    """
    entry = sandbox_module._worker_entrypoint()
    assert entry.is_file(), f"worker 入口不存在：{entry}"
    assert entry.parent == Path(sandbox_module.__file__).resolve().parent
    assert entry.name == "subprocess_worker.py"
    assert os.path.isabs(str(entry))


@pytest.mark.asyncio
@POSIX_ONLY
async def test_the_wall_does_not_import_the_platform(tier_on) -> None:
    """**运行时**证据：墙里落闸之前没有整套业务包（POSIX 专属——本机 Windows 上这一档
    按设计直接拒绝执行，起不了进程，所以这条只能在 POSIX 主机/CI 上取证；跨平台那半由
    ``test_worker_module_level_imports_are_stdlib_only`` 与入口路径用例钉住）。

    AST 用例只能证明"worker 模块级没写 ``import agentix``"，证明不了启动形态——
    ``python -m agentix.tools.subprocess_worker`` 里那句 import 是解释器替父包做的，
    代码里看不见，代价是每次调用先付一遍平台装配（容器内实测 747ms）且"闸先于业务代码"
    当场失效。所以这里问的是子进程**此刻的 ``sys.modules``**，而不是源码文本。
    """
    face = await tier_on.run(probes.import_face, {})
    assert face["agentix"] == [], f"落闸前就拉起了业务包：{face['agentix']}"
    assert (
        face["heavy_third_party"] == []
    ), f"重依赖（DB 驱动/LLM SDK）进了墙：{face['heavy_third_party']}"


def _without_pid(payload: dict) -> dict:
    return {k: v for k, v in payload.items() if k != "pid"}


def _apply_gates_in_child(limits: dict, uid: int) -> dict:
    """在**另一个进程**里落闸并回读，返回 ``{"ok": bool, ...}``。

    为什么不能在测试进程里直接调 ``apply_gates``：soft limit **只能再收紧**，
    ``cpu_s=7`` 会在 7 个 CPU 秒后给整个 pytest 进程发 SIGXCPU，``as_mb=128`` 则让后面的
    用例随机 MemoryError——被测的是「子进程拿到什么边界」，所以证据必须来自子进程的回读，
    而不是把跑测试的进程自己关进闸里（这是 ⑮ 落地时踩到的第一个坑）。
    """
    script = (
        "import json, sys\n"
        "from agentix.tools import subprocess_worker as w\n"
        "limits = json.loads(sys.argv[1]); uid = int(sys.argv[2])\n"
        "try:\n"
        "    out = {'ok': True, 'caps': w.apply_gates(limits, uid)}\n"
        "except w.WorkerError as exc:\n"
        "    out = {'ok': False, 'stage': exc.stage, 'message': exc.message}\n"
        "sys.stdout.write(w.SENTINEL + ' ' + json.dumps(out) + chr(10))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script, json.dumps(limits), str(uid)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(PROJECT_ROOT),
    )
    for line in proc.stdout.splitlines():
        if line.startswith(worker.SENTINEL):
            return json.loads(line[len(worker.SENTINEL) :].strip())
    raise AssertionError(
        f"落闸子进程没有回读信封：rc={proc.returncode} "
        f"stdout={proc.stdout[-400:]!r} stderr={proc.stderr[-400:]!r}"
    )


@pytest.mark.asyncio
@POSIX_ONLY
async def test_real_child_returns_the_same_value_as_in_process(tier_on) -> None:
    """同一条 handler，进程内与墙里**结果等价**（pid 除外）——墙只改「在哪跑」。"""
    from agentix.tools.sandbox import invoke_in_process

    inside = await invoke_in_process(probes.echo, {"a": 1})
    outside = await tier_on.run(probes.echo, {"a": 1})
    assert _without_pid(inside) == _without_pid(outside)
    assert outside["pid"] != os.getpid(), "pid 相同 ⇒ 根本没出进程，这一档白开"

    inside_async = await invoke_in_process(probes.async_echo, {"b": 2})
    outside_async = await tier_on.run(probes.async_echo, {"b": 2})
    assert _without_pid(inside_async) == _without_pid(outside_async)


@pytest.mark.asyncio
@POSIX_ONLY
async def test_secrets_do_not_cross_the_boundary_in_a_real_child(tier_on, monkeypatch) -> None:
    for key in probes.PROBE_SECRET_ENV_KEYS:
        monkeypatch.setenv(key, probes.PROBE_SECRET_VALUE)
    face = await tier_on.run(probes.env_face, {})
    assert all(v is None for v in face["secret_values"].values()), face["secret_values"]
    assert face["has_path"] is True
    assert set(face["visible_keys"]) <= set(OS_ENV_ALLOWLIST) | {"PYTHONPATH", "PYTHONIOENCODING"}


@pytest.mark.asyncio
@POSIX_ONLY
async def test_acceptance_sentence_holds_in_a_real_child(tier_on) -> None:
    """ADR-0007 ⑮ 的验收句：「沙箱内 ``open(os.environ['DATABASE_URL'])`` 必须失败」。"""
    outcome = await tier_on.run(probes.secret_opener, {})
    assert outcome["opened"] is False, f"墙里还能读到连接串指向的文件 ⇒ 这一档没兑现承诺：{outcome}"


@pytest.mark.asyncio
@POSIX_ONLY
async def test_wall_side_code_cannot_buy_a_success_by_printing_the_envelope(tier_on) -> None:
    """真子进程里，墙内代码自己印一条"成功信封" ⇒ 调用必须**响亮失败**。

    这是协议 v2（nonce 归属凭证）在**真实进程边界**上的验收：旧口径「取最后一条哨兵」
    会让这条伪造件直接变成成功结果（父进程返回 ``{"pwned": True}``），而墙内代码只
    需要 ``print`` 一行。父进程若照单收下，这一档就成了"墙里说了算"。
    """
    with pytest.raises(SubprocessSandboxError) as caught:
        await tier_on.run(probes.forged_success, {"tool_name": "echo"})
    message = str(caught.value)
    assert "untrusted success envelope" in message, message
    assert "pwned" not in message, "伪造载荷不该出现在任何成功面上"


@pytest.mark.asyncio
@POSIX_ONLY
async def test_the_file_size_gate_actually_bites(tier_on) -> None:
    """``RLIMIT_FSIZE`` 必须**真拦得住**写大文件——这一档卖的正是那三道闸。

    旧口径只回读限值数值（"我调过 setrlimit"不是证据），**触发与否没人验过**——
    runbook 数字表里那一格曾经写着"触发靠 p125 的用例"，而仓里并没有触发的那条。
    写 64MiB 进 32MiB 的闸，两种结局都算闸生效：
    ① 进程被 ``SIGXFSZ`` 打死 ⇒ 父进程看到 rc≠0（"worker crashed"）；
    ② 写操作返回 ``EFBIG`` ⇒ handler 如实回报 ``{"wrote": False, "errno": 27}``。
    唯一**不算**闸生效的形态是"64MiB 写成功了"。
    """
    try:
        try:
            result = await tier_on.run(probes.file_writer, {"mb": 64})
        except SubprocessSandboxError as exc:
            # 形态 ①：SIGXFSZ 把墙内进程打死了 ⇒ 父进程看到非 0 退出码
            assert "crashed" in str(exc), f"闸生效的形态不对：{exc}"
            return
        # 形态 ②：写操作被闸拒了（EFBIG=27），没写满
        assert result.get("wrote") is False, f"闸没拦住：{result}"
        assert result.get("errno") == 27, result
    finally:
        # 形态 ① 下进程被 SIGXFSZ 打死 ⇒ handler 的 finally 清理没跑，残留在这儿
        for path in glob.glob(os.path.join(tempfile.gettempdir(), ".tmp_p125_fsize_*")):
            with contextlib.suppress(OSError):
                os.remove(path)


@pytest.mark.asyncio
@POSIX_ONLY
async def test_wall_side_code_cannot_buy_a_success_with_a_non_boolean_ok(tier_on) -> None:
    """真子进程里，墙内代码印一条 ``"ok": 1`` 的伪造件 ⇒ 调用必须**响亮失败**。

    这是第 2 轮独立审查点名的绕过在**真实进程边界**上的验收：旧写法把 ``"ok": 1``
    归进"失败桶"，而终局判定用真值 ⇒ ``not 1`` 是 False ⇒ 父进程把这条伪造件当成
    **成功结果**返回，nonce 归属凭证在这一路上从未执行过。真实边界上更刺眼：墙内
    代码只要 ``print`` 一行带哨兵前缀的 JSON，调用方就拿到了 ``{"pwned": true}``。
    """
    with pytest.raises(SubprocessSandboxError) as caught:
        await tier_on.run(probes.forged_success, {"tool_name": "echo", "ok_value": 1})
    message = str(caught.value)
    assert "malformed response envelope" in message, message
    assert "pwned" not in message, "伪造载荷不该出现在任何成功面上"


@pytest.mark.asyncio
@POSIX_ONLY
async def test_forged_envelope_plus_a_crash_is_still_not_a_result(tier_on) -> None:
    """「先印伪造件、再被打死」这一串在旧口径下最危险：崩了会被当成"信封没写完"。

    旧写法 ``if rc != 0 and SENTINEL not in stdout_text`` 恰好把这条当成功返回——
    这一档卖的正是那三道资源闸，闸一生效（RLIMIT_CPU 打成 SIGKILL）反而让伪造得逞，
    是不能接受的形态。退出码非 0 ⇒ 一律不采信 stdout 上的任何信封。
    """
    with pytest.raises(SubprocessSandboxError) as caught:
        await tier_on.run(probes.forged_success, {"tool_name": "echo", "exit_code": 137})
    message = str(caught.value)
    assert "exit 137" in message, message
    assert "不采信" in message, "rc≠0 时必须说清「stdout 上的信封一律不采信」"
    assert "pwned" not in message


@pytest.mark.asyncio
@POSIX_ONLY
async def test_rlimits_are_actually_applied_and_read_back(tier_on) -> None:
    caps = await tier_on.run(probes.caps, {})
    assert caps["resource_available"] is True
    soft, _hard = caps["rlimits"]["RLIMIT_AS"]
    assert soft == RLIMIT_AS_MB * 1024 * 1024, f"回读的 RLIMIT_AS 不是请求值：{caps['rlimits']}"
    assert caps["rlimits"]["RLIMIT_CPU"][0] == 30
    assert caps["uid"] == os.getuid(), "不降权时必须如实报告还是父进程 uid"


@pytest.mark.asyncio
@POSIX_ONLY
async def test_memory_gate_binds_inside_the_wall(tier_on) -> None:
    """``RLIMIT_AS`` 不是摆设：申请 4GiB 必须在墙里被拦下，而不是把机器吃满。"""
    outcome = await tier_on.run(probes.memory_hog, {"mb": 4096})
    assert outcome["blocked_by_rlimit"] is True, outcome


@pytest.mark.asyncio
@POSIX_ONLY
async def test_the_process_count_gate_is_left_alone_without_a_dedicated_uid(tier_on) -> None:
    """默认配置（不降权）下，墙里的进程数上限**必须还是墙外那个数**。

    这一条钉的是"顺手落一道按 uid 计数的闸"这个诱惑：那个闸的计数含整个用户的进程，
    落下去的后果是同用户的其它工具**哪天起不来子进程、换台机器又好了**。反方向也要防：
    真落了闸却在回报里写"没落"。两个方向由同一句判据覆盖——墙内回读 == 墙外回读。
    """
    import resource

    parent_soft = resource.getrlimit(resource.RLIMIT_NPROC)[0]
    face = await tier_on.run(probes.caps, {})
    assert (
        face["rlimits"]["RLIMIT_NPROC"][0] == parent_soft
    ), f"墙内软限被改过（{face['rlimits']['RLIMIT_NPROC']}），但默认配置承诺的是不动它"
    outcome = _apply_gates_in_child({"as_mb": 128, "cpu_s": 7, "fsize_mb": 16, "nproc": 8}, -1)
    assert outcome["ok"] is True, outcome
    gate = outcome["caps"]["nproc_gate"]
    assert gate["applies"] is False and gate["cap"] == 8, gate
    assert (
        "RLIMIT_NPROC" not in outcome["caps"]["rlimit_applied"]
    ), "判定说不落，落闸面却记了它 ⇒ 两个源各说一套，运维读哪一个都会读错"


@pytest.mark.asyncio
@POSIX_ONLY
async def test_the_process_count_gate_bites_once_the_uid_is_dedicated(tier_on, monkeypatch) -> None:
    """进程数闸的**真触发证据**（对齐 ``memory_hog`` 那条的口径：回读数不等于闸生效）。

    root 容器腿：降权到独立 uid ⇒ 墙里 ``fork()`` 到上限后必须 ``EAGAIN``，且软限回读
    等于请求值。非 root 腿：降权落不下 ⇒ ``[limits]`` 响亮失败，**绝不**"按父权限跑起来
    并且悄悄没有这道闸"（那正是这条闸最危险的假绿形态）。两条腿都有断言，换镜像时看得见
    自己在跑哪一条。

    ⚠️ ``spawned`` 的下界刻意是 **0** 而不是 1：``nproc_gate_plan`` 报的计数是**容器可见的
    下限**（内核那份按 uid 的计数跨整机），所以"独占"必须是**整个内核上**独占。取证实测
    （2026-10-26）：``65534``/``nobody`` 与宿主 uid ``1000`` 在容器里数到 0 个进程，却在
    soft=8 下第 0 次 fork 就 EAGAIN；换新建的 ``60000``/``6001`` 才连起 7 个（= soft−1）。
    于是这条断言的含义是"越不过软限"，**不是**"软限之内一定起得来"——后者要独立且独占的
    uid，是部署前提而不是代码能保证的事。取证 uid 走 ``SANDBOX_EVIDENCE_UID``，默认 ``60000``
    （Linux 下 uid 不必存在于 ``/etc/passwd`` 也能降权，所以这个默认值在任何容器里都可用，
    而它比 ``nobody`` 更可能在整个内核上独占 ⇒ 拿到的 ``spawned`` 数字才有解释力）。
    """
    evidence_uid = int(os.environ.get("SANDBOX_EVIDENCE_UID", "60000"))
    monkeypatch.setattr(settings, UID_KEY, evidence_uid)
    cap = sandbox_module.RLIMIT_NPROC
    if os.geteuid() == 0:
        face = await tier_on.run(probes.process_counter, {"attempts": cap + 8})
        assert face["uid"] == evidence_uid, f"没降权就别谈独立 uid 的进程数闸：{face}"
        assert face["nproc_soft"] == cap, f"墙里回读到的软限不是请求值：{face}"
        assert face["blocked"] is True and face["errno"] == 11, f"fork 没有被闸拒：{face}"
        assert 0 <= face["spawned"] <= cap, f"越过软限还在起进程：{face}"
        # "容器可见计数是下限"这句话的经验基础：降权后这个计数至少含墙自己一个进程，
        # 而它**远小于**内核计费的那个数（上一条断言里 spawned==0 的机器就是证据）。
        assert face["count_before"] >= 1, f"墙内数自己这个 uid 连自己都数不到：{face}"
        # spawned==0 且用的是共享 uid（宿主上有人在跑，例如 nobody/1000）时，"软限之内起不来"
        # 是**内核级计数**的结果，不是这道闸的功劳也不是它的过失：那条事实由 runbook 的取证
        # 步骤负责说清，这里不把它做成失败——环境属性不该让测试轮变红
        # （纪律 6 ④「只告警不设卡」的同一条）。
    else:
        with pytest.raises(SubprocessSandboxError, match=r"\[limits\].*uid drop"):
            await tier_on.run(probes.process_counter, {"attempts": 2})


@pytest.mark.asyncio
@POSIX_ONLY
async def test_threads_are_charged_to_the_ledger_the_gate_reads(tier_on, monkeypatch) -> None:
    """差分实测：预起 K 个线程 ⇒ fork 预算恰好少 K（内核计费的是任务，不是进程）。

    为什么是差分而不是绝对值：``spawned == soft - 1`` 只在"这个 uid 在整个内核上独占"时
    成立，而那是**部署前提**、不是代码能保证的事（上一条的 ``spawned`` 下界因此取 0）。
    同一台机器上连跑两次做差，公共项（那一刻内核已经记了多少）自己消掉 ⇒ 断言钉的是
    **计费单位**，在共用 uid 的机器上同样成立，不会因环境属性把测试轮变红（纪律 6 ④）。

    取证实测（2026-10-26，`p125_linux`，uid 60000，同一 uid 下 1 个进程起 5 个线程时
    /proc 报"进程 1 / 任务 6"）：(soft, 预起线程, fork 成功) = (8, 0, 7) / (8, 4, 3) /
    (8, 7, 0) / (16, 10, 5)——fork 预算 = soft − 当时的**任务数**，而那一刻的进程数始终是 1。
    所以只看进程账的余量判定会报"充足"，而墙里第 0 次 fork 就 EAGAIN。

    只有 root 腿能降权到取证 uid：非 root 时降权在落闸点就响亮失败（上一条的 else 分支）。
    """
    evidence_uid = int(os.environ.get("SANDBOX_EVIDENCE_UID", "60000"))
    monkeypatch.setattr(settings, UID_KEY, evidence_uid)
    if os.geteuid() != 0:
        pytest.skip("本条要能降权到独立 uid：非 root 容器里 uid 降不下去，见上一条的 else 分支")
    soft = 8
    extra_threads = 3
    baseline = await tier_on.run(probes.process_counter, {"attempts": soft + 6, "set_soft": soft})
    assert baseline["uid"] == evidence_uid, baseline
    assert baseline["threads_started"] == 0, f"基线腿不该起线程：{baseline}"
    if baseline["spawned"] < extra_threads:
        pytest.skip(
            f"这个 uid 在内核上已被占掉大半（soft={soft} 只起了 {baseline['spawned']} 个进程），"
            "差分没有可用预算——那是部署前提不成立，不是计费单位的问题"
        )
    threaded = await tier_on.run(
        probes.process_counter,
        {"attempts": soft + 6, "set_soft": soft, "threads_first": extra_threads},
    )
    assert threaded["uid"] == evidence_uid, threaded
    assert (
        threaded["threads_started"] == extra_threads
    ), f"线程没按请求起起来，差分没有公共项：{threaded}"
    assert (
        threaded["spawned"] == baseline["spawned"] - extra_threads
    ), f"预起 {extra_threads} 个线程没有少掉 {extra_threads} 次 fork 的预算 ⇒ 计费单位不是任务：{threaded} vs {baseline}"
    assert threaded["blocked"] is True and threaded["errno"] == 11, threaded
    # 同一时刻两本账分开：线程把**任务账**顶起来，而进程账还是那一个墙进程。
    # 这条就是"余量判定不许只看进程账"的可复现形态（读的是 at-fork 那一对：线程起完之后）。
    assert (
        threaded["task_at_fork"] >= threaded["count_at_fork"] + threaded["threads_started"]
    ), f"起了线程却没让任务账比进程账多出来：{threaded}"
    assert (
        threaded["count_at_fork"] == threaded["count_before"]
    ), f"进程账在起线程前后就变了，这条差分的公共项不成立：{threaded}"
    assert (
        threaded["status_threads"] is not None
        and threaded["status_threads"] >= threaded["threads_started"] + 1
    ), f"本进程回读的 Threads 少于起起来的线程数：{threaded}"


@POSIX_ONLY
def test_setting_the_uid_to_zero_declines_the_gate_instead_of_installing_an_empty_one() -> None:
    """``TOOL_SANDBOX_SUBPROCESS_UID=0``（"我就是要 root"）这一支必须**报缺口**而不是报闸。

    判据与后果都在容器里量过：root 写 ``RLIMIT_NPROC`` soft=5 并回读到 5，随后照样连起 12
    个进程、一次都不阻塞（内核豁免 ``CAP_SYS_RESOURCE``）。所以配 0 时"落了闸"的回报是**假
    绿**：运维读到 nproc_gate.applies=true 会以为 fork 炸弹被钉住了，实际上一个都没挡。
    非 root 腿则是另一种响亮：降权到 0 直接失败（EPERM），也不会静默按父权限跑。
    """
    outcome = _apply_gates_in_child({"as_mb": 128, "cpu_s": 7, "fsize_mb": 16, "nproc": 8}, 0)
    if os.geteuid() == 0:
        assert outcome["ok"] is True, outcome
        caps = outcome["caps"]
        gate = caps["nproc_gate"]
        assert gate["applies"] is False, gate
        assert "CAP_SYS_RESOURCE" in gate["reason"], "拒绝的理由必须说清是内核豁免"
        assert (
            "RLIMIT_NPROC" not in caps["rlimit_applied"]
        ), "判定说不落，落闸面却记了它 ⇒ 两个源各说一套（纪律 6 ④）"
        assert caps["uid"] == 0, caps
    else:
        assert outcome["ok"] is False and outcome["stage"] == "limits", outcome
        assert "uid drop" in outcome["message"]


@pytest.mark.asyncio
@POSIX_ONLY
async def test_root_never_gets_an_enforced_process_gate(tier_on, monkeypatch) -> None:
    """墙按 uid 0 跑时：软限**没被动过**，而且自己写进去的软限也挡不住 fork——两侧都是实测。

    这条是上一条的**执行侧**证据：上一条钉"判定说不落"，这一条钉"确实没落、而且落了也没用"。
    只有 root 腿能跑（非 root 时降权到 0 会在执行点响亮失败，那是另一条用例的事实）。
    """
    import resource

    monkeypatch.setattr(settings, UID_KEY, 0)
    if os.geteuid() != 0:
        pytest.skip("本条要 root 腿：非 root 时 uid=0 在执行点就被拒，见上一条用例的 else 分支")
    parent_soft = resource.getrlimit(resource.RLIMIT_NPROC)[0]
    face = await tier_on.run(probes.process_counter, {"attempts": 12, "set_soft": 5})
    assert face["uid"] == 0, face
    assert face["nproc_soft"] == 5, f"handler 自己写的软限没生效？回读：{face['nproc_soft']}"
    assert (
        face["blocked"] is False and face["spawned"] == 12
    ), f"root 被闸挡住了——那说明豁免判据错了，得回来改代码：{face}"
    untouched = await tier_on.run(probes.caps, {})
    assert (
        untouched["rlimits"]["RLIMIT_NPROC"][0] == parent_soft
    ), f"墙按 uid 0 跑却改了软限：{untouched['rlimits']['RLIMIT_NPROC']} != {parent_soft}"


def test_count_uid_processes_declines_instead_of_guessing() -> None:
    """没有 ``/proc``（本机 Windows）⇒ 返回 ``None``，**不许**返回 0。

    0 会被下游读成"这个 uid 一个进程都没有，余量充足"，于是一个"测量失败"被翻译成
    "一切正常"——那是把观测缺位当证据，和这条守卫要防的所有形态同一个病。

    两本账（进程 / 任务）由同一次扫描给出，所以这里一起钉：判据必须一致，读数必须
    ``tasks >= processes``（每个进程至少一个任务；反过来说明两本账不同源或整进程漏了）。
    """
    own_uid = os.getuid() if hasattr(os, "getuid") else 0
    count = worker.count_uid_processes(own_uid)
    tasks = worker.count_uid_tasks(own_uid)
    if not os.path.isdir("/proc"):
        assert count is None, f"没有 /proc 却给出了计数 {count!r}"
        assert tasks is None, f"没有 /proc 却给出了任务数 {tasks!r}"
        return
    assert isinstance(count, int) and count >= 1, f"Linux 上自己这个 uid 至少有一个进程：{count}"
    assert isinstance(tasks, int) and tasks >= count, f"任务账少于进程账：{tasks} < {count}"
    assert worker.count_uid_processes(-7) is None, "非法 uid 必须报'量不出来'，不是报 0"
    assert worker.count_uid_tasks(-7) is None, "非法 uid 的任务账同上：两本账同生同死"


@pytest.mark.asyncio
@POSIX_ONLY
async def test_business_failure_and_unserializable_result_come_back_as_named_stages(
    tier_on,
) -> None:
    with pytest.raises(SubprocessSandboxError, match=r"\[invoke\].*p125_deliberate_failure"):
        await tier_on.run(probes.boom, {})
    with pytest.raises(SubprocessSandboxError, match=r"\[serialize\]"):
        await tier_on.run(probes.nonserializable, {})


@pytest.mark.asyncio
@POSIX_ONLY
async def test_noisy_business_stdout_does_not_break_the_protocol(tier_on) -> None:
    assert await tier_on.run(probes.noisy, {}) == {"noisy": True}


def _live_worker_processes() -> list[str]:
    """列出还在跑的墙内子进程，**不依赖外部二进制**，也**不靠整行字符串匹配**。

    两处都不是修饰：
    ① 第一版这里调 `ps -eo args`，然后在 `python:3.12-slim` 里当场炸（镜像不带 procps），
       而"取消之后有没有留下孤儿"恰恰是要在容器里验的事 ⇒ 判据不能建在运维镜像里
       可能不存在的命令上，Linux 直接读 ``/proc/<pid>/cmdline``，没有 ``/proc`` 才退回 ``ps``；
    ② 匹配认 **argv 里的一个 token**，不认命令行里的子串——本文件用 `python -c` 起的辅助进程
       的代码文本里就含 "subprocess_worker"，按子串匹配会把无关进程算成孤儿（假阳性），
       反过来 `-m` 与脚本路径两种启动形态也各写各的形态 ⇒ 标记从被测实现本身取。
    """
    worker_path = str(sandbox_module._worker_entrypoint())
    module_form = "agentix.tools.subprocess_worker"

    def _is_worker(argv: list[str]) -> bool:
        return any(token in (worker_path, module_form) for token in argv)

    live: list[str] = []
    if os.path.isdir("/proc"):
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                with open(f"/proc/{entry}/cmdline", "rb") as handle:
                    raw = handle.read()
            except OSError:
                continue  # 进程刚退出 / 读不到：不是我们要找的对象
            argv = [t for t in raw.decode("utf-8", "replace").split("\x00") if t]
            if _is_worker(argv):
                live.append(f"{entry}: {' '.join(argv)}")
        return live
    listing = subprocess.run(
        ["ps", "-eo", "pid=,args="],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    ).stdout
    for line in listing.splitlines():
        argv = line.strip().split()
        if _is_worker(argv):
            live.append(line.strip())
    return live


@pytest.mark.asyncio
@POSIX_ONLY
async def test_cancellation_kills_the_child_and_reaps(tier_on) -> None:
    """取消 ⇒ 杀掉并 reap：这一档是阶梯里第一个让超时可以**真生效**的位置。"""
    task = asyncio.ensure_future(tier_on.run(probes.sleeper, {"seconds": 30}))
    await asyncio.sleep(1.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.5)
    live = _live_worker_processes()
    assert not live, f"取消后墙里还留着活着的子进程（{live}）= 资源闸挡不住无人回收的进程"


@pytest.mark.asyncio
@POSIX_ONLY
async def test_executor_runs_a_declared_subprocess_tool_for_real(tier_on) -> None:
    """端到端：``ToolSpec.sandbox='subprocess'`` 真走墙里（handler 必须模块级可重建）。"""
    name = "p125_real_subprocess_tool"
    ex = ToolExecutor()
    ex.register_handler(name, probes.echo, spec=_spec(name))
    result = await ex.execute(name, {"x": 1})
    assert result.success is True, result.error
    assert result.data["echo"] == {"x": 1}
    assert result.data["pid"] != os.getpid()


@pytest.mark.asyncio
@POSIX_ONLY
async def test_declared_uid_either_drops_or_refuses_loudly(tier_on, monkeypatch) -> None:
    """声明了降权的**三种可能结局**里，只有一种是禁止的：按父权限跑完并回成功信封。

    root ⇒ 子进程如实报告 uid=65534（闸与降权同时生效）；非 root ⇒ ``[limits]`` 阶段的
    响亮失败。父进程侧判据是 ``hasattr(os, "setuid")``（POSIX 恒真），所以非 root 时
    拒绝点落在子进程侧——晚一步，但仍然终局、仍然不静默。
    """
    monkeypatch.setattr(settings, UID_KEY, 65534)
    if os.geteuid() == 0:
        caps = await tier_on.run(probes.caps, {})
        assert caps["uid"] == 65534 and caps["euid"] == 65534, f"声明降权却没降：{caps}"
        assert caps["rlimits"]["RLIMIT_AS"][0] == RLIMIT_AS_MB * 1024 * 1024
    else:
        with pytest.raises(SubprocessSandboxError, match=r"\[limits\].*uid drop"):
            await tier_on.run(probes.caps, {})


@POSIX_ONLY
def test_setuid_path_needs_privilege_and_says_so() -> None:
    """降权两条腿都要有证据：root 容器里**真降到 65534**，非 root 时**点名需要 root**。

    静默按父权限继续跑是这一档最坏的失败形态——声明了降权却没降，比不声明更危险。
    分支由容器身份决定，两支都写死断言，运维换镜像时看得到是哪一支在跑。
    """
    outcome = _apply_gates_in_child({"as_mb": 512, "cpu_s": 30, "fsize_mb": 32}, 65534)
    if os.geteuid() == 0:
        assert outcome["ok"] is True, outcome
        caps = outcome["caps"]
        assert caps["uid_dropped_to"] == 65534 and caps["uid"] == 65534 and caps["euid"] == 65534
    else:
        assert outcome["ok"] is False and outcome["stage"] == "limits", outcome
        assert "uid drop" in outcome["message"]
        assert "TOOL_SANDBOX_SUBPROCESS_UID=-1" in outcome["message"], "拒绝要点名可行的出路"


@POSIX_ONLY
def test_apply_gates_reports_what_it_actually_set() -> None:
    """回读而不是"我调过 setrlimit"：``effective`` 才是子进程真正拿到的边界。"""
    outcome = _apply_gates_in_child({"as_mb": 128, "cpu_s": 7, "fsize_mb": 16}, -1)
    assert outcome["ok"] is True, outcome
    caps = outcome["caps"]
    applied = caps["rlimit_applied"]
    assert set(applied) == {"RLIMIT_AS", "RLIMIT_CPU", "RLIMIT_FSIZE"}
    for name, entry in applied.items():
        assert entry["effective"][0] <= entry["requested"], f"{name} 回读值比请求值还宽：{entry}"
    assert caps["resource_module_available"] is True
    assert "uid_dropped_to" not in caps, "uid=-1 表示不降权，不许留下降权痕迹"
    assert caps["uid"] == os.getuid(), "不降权时必须如实报告还是父进程 uid"
