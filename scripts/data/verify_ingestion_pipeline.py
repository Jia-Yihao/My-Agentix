#!/usr/bin/env python3
"""
My Agentix 法律法规领域 —— 灌库链路就绪探测器 (Ingestion Pipeline Readiness Probe)

用途：
    在不实际启动任何 Docker 服务、不连接任何远端服务的前提下，探测当前环境
    是否满足各灌库阶段的前置条件，并打印一张分阶段状态表。

探测项（仅只读探测，绝不启动 docker / 连接服务）：
    (a) Docker 守护进程：`docker info` 的 returncode
    (b) TCP 端口 7687 / 7474 / 5432 / 19530 是否在 127.0.0.1 监听（socket 连接，超时 1s）
    (c) LM Studio：`http://localhost:1234/v1/models` 是否可达（urllib，超时 3s）

阶段判定：
    数据→CSV       —— 离线，始终 ✅（无外部依赖）
    Neo4j 导入      —— 需 docker 守护进程 + 127.0.0.1:7687 监听
    kb 向量库导入   —— 需 docker 守护进程 + (127.0.0.1:5432 或 127.0.0.1:19530) 监听
    LightRAG 导入   —— 需 LM Studio (127.0.0.1:1234) 可达
    边抽取-LLM      —— 需 LM Studio (127.0.0.1:1234) 可达（加载 qwen/qwen3.5-9b 后由后续脚本调用）

退出码：始终 0（即便所有阶段均为 ⛔，也正常退出）。

运行：
    python scripts/data/verify_ingestion_pipeline.py
    python scripts/data/verify_ingestion_pipeline.py --csv-dir data/legal/csv
"""

from __future__ import annotations

import argparse
import socket
import subprocess
import sys
import urllib.request
from typing import Dict, List, Tuple

# --------------------------------------------------------------------------- #
# 常量配置
# --------------------------------------------------------------------------- #
DEFAULT_HOST: str = "127.0.0.1"
DEFAULT_CSV_DIR: str = "data/legal/csv"
LM_STUDIO_URL: str = "http://localhost:1234/v1/models"
DOCKER_TIMEOUT: float = 8.0
TCP_TIMEOUT: float = 1.0
LM_TIMEOUT: float = 3.0

# 需探测的 TCP 端口：(端口, 服务说明)
TCP_PORTS: List[Tuple[int, str]] = [
    (7687, "Neo4j Bolt"),
    (7474, "Neo4j HTTP/Browser"),
    (3306, "MySQL"),
    (19530, "Milvus"),
]

# 状态枚举
STATUS_READY = "✅ READY"      # 依赖全部满足，阶段可立即执行
STATUS_WAITING = "⏳ WAITING"  # docker 已起但目标端口尚未监听（服务启动中）
STATUS_BLOCKED = "⛔ BLOCKED"  # 关键依赖（docker/端口/服务）未就绪

# 凭据（与 agentix/config/settings.py 保持一致；Neo4j 已关闭 auth）
NEO4J_URI = "bolt://neo4j:7687"
NEO4J_USER = None
NEO4J_PASSWORD = None


# --------------------------------------------------------------------------- #
# 探测函数（纯只读）
# --------------------------------------------------------------------------- #
def probe_docker() -> Tuple[bool, str]:
    """探测 Docker 守护进程是否可用。

    Returns:
        (是否可用, 说明)。不可用时返回 (False, 原因)。
    """
    try:
        result = subprocess.run(
            ["docker", "info"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=DOCKER_TIMEOUT,
        )
        ok = result.returncode == 0
        return ok, "docker info returncode=0" if ok else f"docker info returncode={result.returncode}"
    except FileNotFoundError:
        return False, "docker 未安装或不在 PATH"
    except subprocess.TimeoutExpired:
        return False, f"docker info 超时（>{int(DOCKER_TIMEOUT)}s）"
    except Exception as exc:  # pragma: no cover - 兜底
        return False, f"docker 探测异常：{exc}"


def probe_tcp_port(host: str, port: int, timeout: float = TCP_TIMEOUT) -> bool:
    """探测指定 TCP 端口是否在 host 监听。

    Args:
        host: 目标主机（默认 127.0.0.1）。
        port: 目标端口。
        timeout: 连接超时（秒）。

    Returns:
        端口可连接返回 True，否则 False。
    """
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, ConnectionError, TimeoutError):
        return False


def probe_lm_studio(url: str, timeout: float = LM_TIMEOUT) -> bool:
    """探测 LM Studio OpenAI 兼容端点是否可达。

    Args:
        url: 模型列表端点（默认 http://localhost:1234/v1/models）。
        timeout: 请求超时（秒）。

    Returns:
        端点成功响应返回 True，否则 False。
    """
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # nosec B310 - URL 由操作员配置提供
            return 200 <= resp.status < 400
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# 阶段评估
# --------------------------------------------------------------------------- #
def evaluate_stages(
    docker_ok: bool,
    port_status: Dict[int, bool],
    lm_ok: bool,
    csv_dir: str,
) -> List[Dict[str, str]]:
    """根据探测结果评估各灌库阶段状态。

    Returns:
        阶段字典列表，字段：name / status / basis / action / command。
    """
    neo4j_bolt = port_status.get(7687, False)
    milvus_ok = port_status.get(19530, False)

    # 1) 数据→CSV：离线，始终就绪
    data_csv_action = (
        "无需外部依赖，离线即可运行（见下方命令）。"
    )
    stages: List[Dict[str, str]] = [
        {
            "name": "数据→CSV",
            "status": STATUS_READY,
            "basis": "离线脚本，无外部依赖",
            "action": data_csv_action,
            "command": (
                f"python scripts/legal_kg_to_csv.py "
                f"--seed-json data/legal/legal_seed.json --output-dir {csv_dir}"
            ),
        }
    ]

    # 2) Neo4j 导入
    if docker_ok and neo4j_bolt:
        neo4j_status = STATUS_READY
        neo4j_basis = "docker 已起且 127.0.0.1:7687(Bolt) 监听"
    elif docker_ok and not neo4j_bolt:
        neo4j_status = STATUS_WAITING
        neo4j_basis = "docker 已起，但 127.0.0.1:7687 尚未监听（neo4j 启动中）"
    else:
        neo4j_status = STATUS_BLOCKED
        neo4j_basis = "docker 守护进程未运行"
    neo4j_action = (
        "① 启动 Docker Desktop；② `docker-compose up -d neo4j`；"
        "③ 确保 127.0.0.1:7687/7474 监听（注意 compose 当前宿主映射为 17687/17474，"
        "需改映射或在容器内执行）；凭据 NEO4J_URI="
        f"{NEO4J_URI}，auth 已关闭(user/password 为空)；"
        "④ CSV 由 Dockerfile `neo4j_seeded` 阶段自动导入。"
    )
    stages.append(
        {
            "name": "Neo4j 导入",
            "status": neo4j_status,
            "basis": neo4j_basis,
            "action": neo4j_action,
            "command": (
                f"# 先生成 CSV：\npython scripts/legal_kg_to_csv.py "
                f"--seed-json data/legal/legal_seed.json --output-dir {csv_dir}\n"
                "# 再由 docker-compose 的 neo4j(neo4j_seeded) 服务导入上述 CSV"
            ),
        }
    )

    # 3) kb 向量库导入
    if milvus_ok:
        kb_status = STATUS_READY
        kb_basis = "向量后端已监听（milvus:19530）"
    elif docker_ok:
        kb_status = STATUS_WAITING
        kb_basis = "docker 已起，但 19530 未监听（向量服务启动中）"
    else:
        kb_status = STATUS_BLOCKED
        kb_basis = "docker 守护进程未运行"
    kb_action = (
        "① 启动 Docker Desktop；② `docker-compose up -d milvus`；"
        "③ 确认 127.0.0.1:19530 监听；"
        "④ 运行 `kb_ingest` 服务导入 `data/kb/legal_corpus.txt`"
        "（KB_USE_LLM / KB_EMBEDDING_* 等见 settings.py）。"
    )
    stages.append(
        {
            "name": "kb 向量库导入",
            "status": kb_status,
            "basis": kb_basis,
            "action": kb_action,
            "command": "# 启动 kb_ingest 服务（读取 data/kb/legal_corpus.txt）\ndocker-compose up -d kb_ingest",
        }
    )

    # 4) LightRAG 导入
    if lm_ok:
        lr_status = STATUS_READY
        lr_basis = "LM Studio (127.0.0.1:1234) 可达"
    else:
        lr_status = STATUS_BLOCKED
        lr_basis = "LM Studio (127.0.0.1:1234) 不可达"
    lr_action = (
        "① 启动 LM Studio 并加载嵌入/LLM 模型，监听 1234；"
        "② 运行 `python scripts/init_lightrag.py --source json "
        "--json-path data/legal/legal_lightrag_corpus.json`"
        "（LIGHTRAG_WORKING_DIR 见 settings.py）。"
    )
    stages.append(
        {
            "name": "LightRAG 导入",
            "status": lr_status,
            "basis": lr_basis,
            "action": lr_action,
            "command": (
                "python scripts/init_lightrag.py --source json "
                "--json-path data/legal/legal_lightrag_corpus.json"
            ),
        }
    )

    # 5) 边抽取-LLM
    if lm_ok:
        edge_status = STATUS_READY
        edge_basis = "LM Studio (127.0.0.1:1234) 可达"
    else:
        edge_status = STATUS_BLOCKED
        edge_basis = "LM Studio (127.0.0.1:1234) 不可达"
    edge_action = (
        "① 启动 LM Studio 并加载 `qwen/qwen3.5-9b`；"
        "② 运行后续 LLM 抽取脚本，对法条做语义抽取 `引用`/`对应案由`/`有司法解释` 边。"
        "（规则化抽 `引用` 在本数据集已证实为 0，属数据限制，需 LLM 语义抽取。）"
    )
    stages.append(
        {
            "name": "边抽取-LLM",
            "status": edge_status,
            "basis": edge_basis,
            "action": edge_action,
            "command": "# 加载 qwen/qwen3.5-9b 后，由后续 LLM 抽取脚本调用",
        }
    )

    return stages


# --------------------------------------------------------------------------- #
# 打印
# --------------------------------------------------------------------------- #
def _print_env_snapshot(
    docker_ok: bool,
    docker_msg: str,
    port_status: Dict[int, bool],
    lm_ok: bool,
) -> None:
    """打印环境探测快照。"""
    print("=" * 72)
    print("My Agentix 灌库链路就绪探测 —— 环境快照")
    print("=" * 72)
    print(f"  Docker 守护进程      : {'✅' if docker_ok else '❌'}  ({docker_msg})")
    for port, desc in TCP_PORTS:
        mark = "✅" if port_status.get(port, False) else "❌"
        print(f"  TCP {DEFAULT_HOST}:{port:<5} ({desc:<22}): {mark}")
    print(f"  LM Studio (1234)     : {'✅' if lm_ok else '❌'}  ({LM_STUDIO_URL})")
    print("=" * 72)


def _print_stage_table(stages: List[Dict[str, str]]) -> None:
    """打印分阶段状态表。"""
    print("My Agentix 灌库链路分阶段状态表")
    print("=" * 72)
    # 表头
    header = f"  {'阶段':<14} | {'状态':<12} | {'判定依据':<40}"
    print(header)
    print("  " + "-" * 68)
    for stage in stages:
        print(f"  {stage['name']:<13} | {stage['status']:<12} | {stage['basis']}")
    print("=" * 72)
    print("各阶段「需先做什么才通」与复制即用命令：")
    print("-" * 72)
    for idx, stage in enumerate(stages, start=1):
        print(f"[{idx}] {stage['name']}  ——  {stage['status']}")
        print(f"    说明：{stage['action']}")
        print("    命令：")
        for line in stage["command"].split("\n"):
            print(f"        {line}")
        print()


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def parse_args(argv: List[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="My Agentix 灌库链路就绪探测器（只读探测，不启动/不连接任何服务）"
    )
    parser.add_argument(
        "--csv-dir",
        type=str,
        default=DEFAULT_CSV_DIR,
        help=f"CSV 输出目录（仅用于打印命令，默认 {DEFAULT_CSV_DIR}）",
    )
    parser.add_argument(
        "--host",
        type=str,
        default=DEFAULT_HOST,
        help=f"TCP 探测主机（默认 {DEFAULT_HOST}）",
    )
    parser.add_argument(
        "--lm-url",
        type=str,
        default=LM_STUDIO_URL,
        help=f"LM Studio 探测 URL（默认 {LM_STUDIO_URL}）",
    )
    return parser.parse_args(argv)


def main(argv: List[str] | None = None) -> int:
    """主函数：探测并打印状态表，始终返回 0。"""
    args = parse_args(argv)

    # 1) 环境探测
    docker_ok, docker_msg = probe_docker()
    port_status: Dict[int, bool] = {
        port: probe_tcp_port(args.host, port) for port, _ in TCP_PORTS
    }
    lm_ok = probe_lm_studio(args.lm_url)

    # 2) 阶段评估
    stages = evaluate_stages(docker_ok, port_status, lm_ok, args.csv_dir)

    # 3) 打印
    _print_env_snapshot(docker_ok, docker_msg, port_status, lm_ok)
    _print_stage_table(stages)

    # 统计
    ready = sum(1 for s in stages if s["status"] == STATUS_READY)
    print(f"就绪阶段：{ready}/{len(stages)}。本探测器仅做只读探测，未启动任何服务。")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
