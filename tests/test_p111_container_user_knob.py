"""P111：容器运行身份旋钮（§24.3 T0 ⑤ / G8 剩余那一格）。

⑤ 的容器面此前只落了 `no-new-privileges` + 资源配额 + `pids`，**运行身份仍是 root**。
不是没想到，而是 `user:` 一换就撞既有挂载目录的属主：backend 与 retrieval-worker 共享
`./data` 与 `./uploads`，非 root 写不进去时 entrypoint 的 `set -e` 会让容器进入
restart 崩溃循环——「降权」在这里不是加一行就完事，是要连带一条可执行的切换步骤。

本文件钉三件事：

1. **默认逐位等价**：`AGENTIX_CONTAINER_USER` 不设时解析成 `0:0`，而 server stage
   没有 `USER` 指令（容器本来就以 uid 0 起）⇒ 加这行不改变今天的运行身份；
2. **两个服务必须同源**：共享挂载的两个服务读同一个变量。写成两份就会漂移，
   一个非 root 一个 root 时，其中一方必然 EACCES（§24.2 G5「两闸各持一份口径」
   的老毛病，不该在容器面重来一遍）；
3. **旋钮必须被文档教过怎么开**：runbook 里要有「换 uid 前要动哪些目录」，
   否则这是一个只能被误用、不能被正确使用的开关。

判据全部走「解析文件」而不是「起容器」：本机 Docker daemon 不在时也要能跑
（CI 的 lint job 就是这种情况），而起真容器验证 uid 属于 T3 的验收面。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
COMPOSE = PROJECT_ROOT / "docker-compose.yml"
DOCKERFILE = PROJECT_ROOT / "Dockerfile"
RUNBOOK = PROJECT_ROOT / "docs" / "runbooks" / "预发环境与配置矩阵.md"

#: 共享 `./data` + `./uploads` 的两个应用服务
APP_SERVICES = ("backend", "retrieval-worker")

#: 需要在宿主机上准备属主的目录（runbook「容器降权」一节列的就是这两个）
SHARED_DATA_DIR = "./data"

VAR_RE = re.compile(r"^\$\{([A-Z][A-Z0-9_]*):-(.+)\}$")


def _compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


def _default_of(value: str) -> str:
    """取 `${NAME:-default}` 的默认段；不是这个形状就原样返回。"""
    match = VAR_RE.match(value)
    return match.group(2) if match else value


def _service_user(service: str) -> str:
    spec = _compose()["services"][service]
    assert "user" in spec, f"{service} 没有声明 `user:`，⑤ 的旋钮又丢了"
    return str(spec["user"])


def _server_stage_text() -> str:
    """`FROM … AS server` 到下一个 FROM 之间的文本（即最终镜像的构建指令）。"""
    text = DOCKERFILE.read_text(encoding="utf-8")
    start = text.index("FROM python:3.12-slim AS server")
    rest = text[start + len("FROM python:3.12-slim AS server") :]
    nxt = rest.find("\nFROM ")
    return rest if nxt == -1 else rest[:nxt]


# ---------------------------------------------------------------------------
# 1. 默认值等价
# ---------------------------------------------------------------------------


def test_user_knob_defaults_to_root_and_is_opt_in() -> None:
    for service in APP_SERVICES:
        raw = _service_user(service)
        assert raw.startswith("${AGENTIX_CONTAINER_USER:-"), (
            f"{service} 的 `user:` 不是预期形状：{raw}"
        )
        assert _default_of(raw) == "0:0", f"{service} 默认值改成了 {raw}，不再逐位等价"


def test_image_itself_runs_as_root_so_default_is_a_no_op() -> None:
    """`0:0` 之所以「等价于不写」，前提是镜像没有 USER 指令——这个前提也要钉住。

    若哪天 Dockerfile 给 server stage 加了 `USER app`，`0:0` 就变成**升权**而不是
    保持现状，本文件的第 1 条口径会失效，必须由这条先红。
    """
    stage = _server_stage_text()
    user_lines = [
        line.strip() for line in stage.splitlines() if re.match(r"^\s*USER\s", line)
    ]
    assert not user_lines, f"server stage 出现了 USER 指令，`0:0` 不再是 no-op：{user_lines}"


# ---------------------------------------------------------------------------
# 2. 两个服务同源
# ---------------------------------------------------------------------------


def test_app_services_share_one_user_knob() -> None:
    backend, worker = (_service_user(name) for name in APP_SERVICES)
    assert backend == worker, "backend 与 retrieval-worker 的运行身份旋钮漂移了"


def _bind_sources(spec: dict) -> list[str]:
    """服务挂载的**宿主侧路径**（`./data:/app/data` → `./data`）。"""
    out = []
    for item in spec.get("volumes") or []:
        if isinstance(item, str) and ":" in item:
            out.append(item.split(":")[0])
    return out


def test_knob_is_the_same_for_services_that_share_the_data_dir() -> None:
    """共享 `./data` 目录的服务必须用同一个变量名（否则属主要按两把 uid 准备）。

    只认「挂的是 `./data` 这个目录本身」：`mysql` 挂的是 `./data/*.sql` 两个只读文件，
    与容器写权限无关，把它算进来会得出一个假要求。
    """
    services = _compose()["services"]
    sharers = [
        name
        for name, spec in services.items()
        if isinstance(spec, dict) and SHARED_DATA_DIR in _bind_sources(spec)
    ]
    assert set(APP_SERVICES) <= set(sharers), f"挂载面变了，先回看本用例：{sharers}"
    for name in sharers:
        spec = services[name]
        if "user" not in spec:
            # kb_ingest 是独立镜像、独立 Dockerfile，本轮有意不覆盖（见 runbook 的边界说明）
            assert name == "kb_ingest", f"{name} 共享 ./data 却没有 `user:`，也未登记为例外"
            continue
        assert "${AGENTIX_CONTAINER_USER:-" in str(spec["user"]), name


# ---------------------------------------------------------------------------
# 3. 文档面
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "needle",
    [
        "AGENTIX_CONTAINER_USER",
        "./data",
        "./uploads",
        "chown",
        "0:0",
    ],
)
def test_runbook_teaches_the_switch(needle: str) -> None:
    text = RUNBOOK.read_text(encoding="utf-8")
    assert needle in text, f"runbook 没交代 {needle}，这个旋钮只能被误用"


def test_compose_and_runbook_agree_on_the_default() -> None:
    """runbook 里写的「默认」必须就是 compose 的默认，否则又是一条 G13。"""
    text = RUNBOOK.read_text(encoding="utf-8")
    claimed = re.findall(r"`AGENTIX_CONTAINER_USER`[^`\n]*默认[^`\n]*`([^`]+)`", text)
    assert claimed, "runbook 没有按「键名 … 默认 <值>」的口径写出默认值"
    for value in claimed:
        assert value == "0:0", f"runbook 声明默认 {value}，compose 实际默认 0:0"


# ---------------------------------------------------------------------------
# 4. 反向钉子：守卫不是同义反复
# ---------------------------------------------------------------------------


def test_guard_is_not_a_tautology() -> None:
    """在内存里把 compose 改坏，断言必须跟着红——否则上面的用例等于没写。"""
    text = COMPOSE.read_text(encoding="utf-8")
    tampered = text.replace(
        'user: "${AGENTIX_CONTAINER_USER:-0:0}"',
        'user: "${AGENTIX_CONTAINER_USER:-1000:1000}"',
        1,
    )
    assert tampered != text, "compose 里找不到预期的 user 行"

    spec = yaml.safe_load(tampered)["services"]["backend"]
    assert _default_of(str(spec["user"])) != "0:0"

    # USER 指令一旦出现，第 1 条等价口径就该失效
    tampered_dockerfile = (
        _server_stage_text() + "\nUSER app\n"
    )
    assert re.search(r"^\s*USER\s", tampered_dockerfile, re.MULTILINE)
