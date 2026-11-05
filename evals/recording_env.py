"""录制基线时的「本机录制档」环境装配 + 录制前自检。

为什么需要这个模块（不是便利，是**可复现性**）：
仓库的 ``.env`` 是**容器视角**写的——``DATABASE_URL`` / ``NEO4J_URI`` / ``REDIS_URL``
指向 compose 内网名（``postgres`` / ``neo4j`` / ``redis``），这些名字在宿主机上解析
不到；而 ``--record`` 恰恰是在宿主机上跑的（LM Studio 在 127.0.0.1:1234，重依赖以
容器端口映射暴露）。同一条命令在容器内和宿主机上的依赖图并不相同，于是「录制基线」
这件事需要一套显式的装配规则，而不是每次手敲 20 个环境变量——手敲的口径不可复现，
而基线的全部价值就在可复现（门禁只比较两次同口径的录制）。

优先级（高→低）：**进程环境变量 > ``.env.local``（git 忽略，机器私有） >
本模块的录制档默认 > ``.env``**。最后一档只对「值里没有 compose 内网名」的键生效：
含内网名的一律换成本机端口映射，否则每条 kb 问题都会在观测面上撞
``Error 11001``（实测），退化成一份「全都兜底答案」的基线。

两条设计约束：

* **不打印任何凭据**：默认值里的 API key、连接串里的 user:password 一律脱敏后
  才进 notes。本模块刻意**不读** ``docker compose config`` 的渲染结果——它会把
  宿主机 ``.env`` 整个展开到 stdout（约定 7 记过两次案发）。
* **端口是推导的，不是抄的**：主机端口来自 ``docker-compose.yml`` 的 ``ports``
  映射（``${VAR:-default}`` 取 default，刻意不叠加宿主机 ``.env``），改 compose
  不必回来改这里。

用法：由 ``evals/run_eval.py --local-llm`` 调用（见该文件 ``run_live``），
必须在任何 ``from agentix...`` 之前——``settings`` 与 ``database.engine``
都是导入期对象。
"""

from __future__ import annotations

import json
import os
import re
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO_ROOT: Path = Path(__file__).resolve().parent.parent
COMPOSE_PATH: Path = REPO_ROOT / "docker-compose.yml"
LOCAL_ENV_PATH: Path = REPO_ROOT / ".env.local"

#: LM Studio 的 OpenAI 兼容端点（本机默认，非仓库假设——可用 .env.local 覆盖）。
LM_STUDIO_BASE_URL: str = "http://127.0.0.1:1234/v1"
#: 录制档默认聊天模型（本机 LM Studio 已加载的 id；`.env` 里的远端模型名不会被采纳）。
DEFAULT_CHAT_MODEL: str = "qwen/qwen3-8b"
#: 录制档默认 embedding 模型 id（同上，仅当 `.env` 的 embedding 端点已是本机回环时
#: 才优先采纳 `.env` 的值）。
DEFAULT_EMBEDDING_MODEL: str = "text-embedding-bge-m3"

#: 录制用的法律语料换域声明。出厂 ``SCOPE_DESCRIPTION``
#: （``agentix/application/agents/constants.py``）是「测试数据集」口径，而
#: ``data/kb/legal_corpus.txt`` 是真法条；KB 子图 guardrails 按旧域判越界，实测
#: L009/L012 直接回「超出测试数据集知识库处理范围」且 0 sources——那是一份
#: 「每条 kb 题都拒答」的基线，等于没有基线。``general_handler.create_kb_query``
#: 把这句透传成 ``scope_description``（``settings.KB_SCOPE_DESCRIPTION``），
#: 所以录制必须显式声明。
LEGAL_SCOPE_DESCRIPTION: str = (
    "法律法规知识库问答助手服务范围：基于已收录的《宪法》《民法典》《刑法》"
    "《劳动合同法》等法律条文及其章节结构、关键词、对应案由与司法解释，提供："
    "① 条文原文与条号查询；② 条文所属章节与效力层级说明；"
    "③ 条文之间的引用关系；④ 罪名/案由与适用条文的匹配；"
    "⑤ 相关司法解释的对应关系。法律条文的适用解释与普法问答属于服务范围内的"
    "正常提问；具体个案代理意见、诉讼策略、政治敏感、医疗健康诊断与隐私类问题"
    "不在范围内。"
)


# ---------------------------------------------------------------------------
# 值读取：进程 env > .env.local > 模块默认 > .env（仅非内网名）
# ---------------------------------------------------------------------------


def _read_dotenv(path: Path) -> Dict[str, str]:
    """极简 ``KEY=VALUE`` 读取（跳过注释/空行，剥一层引号）。

    不用 ``dotenv_values``：本模块要的是「哪些键存在」这一薄层事实，pydantic-settings
    自己会去解析 ``.env``；这里只负责决定写进 ``os.environ`` 的值，解析口径越窄越好
    （多行值/插值交给 settings，别在这里造第二个实现）。
    """
    values: Dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip('"').strip("'")
        if key.strip():
            values[key.strip()] = value
    return values


def mask(value: str) -> str:
    """脱敏：连接串里的 user:password → user:***，其余原样。"""
    if not value:
        return "(空)"
    return re.sub(r"://([^:/@]+):[^@]*@", r"://\1:***@", value)


def _is_loopback(url: str) -> bool:
    """端点是否指向本机（决定 embedding 模型名能否连端点一起采纳）。"""
    host = (urllib.parse.urlsplit(url).netloc or url).split("@")[-1].split(":")[0]
    return host in {"127.0.0.1", "localhost", "::1", "0.0.0.0"}


# ---------------------------------------------------------------------------
# compose 端口推导（主机端口是生成物，不是抄本——约定 7）
# ---------------------------------------------------------------------------

# ${VAR:-default} / ${VAR} 的取值：只认 default，不叠加宿主机 .env。
_VAR_DEFAULT = re.compile(r"^\$\{[A-Za-z_][A-Za-z0-9_]*(?::-([^}]*))?\}$")


def _literal_port(token: str) -> Optional[str]:
    token = token.strip().strip('"').strip("'")
    if not token:
        return None
    match = _VAR_DEFAULT.match(token)
    if match:
        return match.group(1)  # 无 default 的 ${VAR} ⇒ None：这台机器没渲染过就不猜
    return token if token.isdigit() else None


def published_host_port(service: str, container_port: int) -> Optional[int]:
    """从 ``docker-compose.yml`` 推导 ``service`` 的 ``container_port`` 对应主机端口。

    支持 ``HOST:CONTAINER`` / ``IP:HOST:CONTAINER`` 两种写法（值里的
    ``${VAR:-default}`` 取 default）。找不到映射返回 ``None``，由调用方决定
    回落还是报错——不静默编一个端口。
    """
    import yaml

    compose: Dict[str, Any] = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))
    services = compose.get("services") or {}
    entries = (services.get(service) or {}).get("ports") or []
    for entry in entries:
        parts = str(entry).split(":")
        if len(parts) not in (2, 3):
            continue
        host_seg, container_seg = parts[-2], parts[-1]
        # container 侧也可能是 ${VAR:-port} 写法
        resolved_container = _literal_port(container_seg.split("/")[0])
        if resolved_container is None:
            m = re.match(r"^(\d+)", container_seg)
            resolved_container = m.group(1) if m else None
        if resolved_container != str(container_port):
            continue
        host = _literal_port(host_seg)
        if host is not None:
            return int(host)
    return None


def compose_service_environment(service: str) -> Dict[str, str]:
    """读取 compose 中某服务的 ``environment``（列表或字典两种写法都支持）。"""
    import yaml

    compose: Dict[str, Any] = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))
    services = compose.get("services") or {}
    raw = (services.get(service) or {}).get("environment") or []
    out: Dict[str, str] = {}
    if isinstance(raw, dict):
        items = raw.items()
    else:
        items = []
        for item in raw:
            if "=" not in str(item):
                continue
            key, _, value = str(item).partition("=")
            items.append((key, value))
    for key, value in items:
        # ${VAR:-default} ⇒ default；纯 ${VAR} ⇒ 跳过（未渲染的不猜）
        if isinstance(value, str):
            match = _VAR_DEFAULT.match(value.strip())
            if match:
                value = match.group(1) or ""
        out[str(key)] = str(value)
    return out


# ---------------------------------------------------------------------------
# 主机名重映射
# ---------------------------------------------------------------------------


def remap_compose_host(
    url: str, service: str, container_port: int, *, include_port: bool = True
) -> str:
    """把 ``url`` 里的 compose 内网名换成 ``127.0.0.1[:主机端口]``。

    只动 host:port，scheme、userinfo（含密码）、path（含 db 序号）原样保留——
    重映射的是「这台机器怎么够到它」，不是「它是什么」。裸主机名（``MILVUS_HOST``
    这类 host 与 port 分两个键的写法）也支持，此时用 ``include_port=False``。
    幂等：已经是 IP/localhost 的值原样返回，反复 apply 不会越换越远。
    """
    if not url:
        return url
    host_port = published_host_port(service, container_port)
    if host_port is None:
        raise RuntimeError(
            f"docker-compose.yml 里找不到 {service}:{container_port} 的端口映射，"
            "无法在宿主机重映射——请显式在 .env.local 里给出该服务的地址。"
        )
    new_host = f"127.0.0.1:{host_port}" if include_port else "127.0.0.1"

    if "://" not in url:
        hostname = url.strip().split(":")[0]
        return new_host if hostname == service else url

    parsed = urllib.parse.urlsplit(url)
    netloc = parsed.netloc
    if "@" in netloc:
        userinfo, _, hostpart = netloc.rpartition("@")
    else:
        userinfo, hostpart = "", netloc
    if hostpart.split(":")[0] != service:
        return url
    if userinfo:
        new_host = f"{userinfo}@{new_host}"
    return urllib.parse.urlunsplit(
        (parsed.scheme, new_host, parsed.path, parsed.query, parsed.fragment)
    )


# ---------------------------------------------------------------------------
# 装配
# ---------------------------------------------------------------------------


def build_local_recording_env(
    *, process_env: Optional[Dict[str, str]] = None
) -> Tuple[Dict[str, str], Dict[str, str], List[str]]:
    """构造录制档环境变量，返回 ``(待写入的 env, 已有 env 快照, 说明清单)``。

    ``process_env`` 只用于测试注入；默认取真实 ``os.environ``。
    """
    current: Dict[str, str] = dict(
        process_env if process_env is not None else os.environ
    )
    local = _read_dotenv(LOCAL_ENV_PATH)
    dotenv = _read_dotenv(REPO_ROOT / ".env")

    resolved: Dict[str, str] = {}
    notes: List[str] = []

    def put(key: str, value: str, source: str) -> None:
        resolved[key] = value
        notes.append(f"{key} = {mask(value)}  [{source}]")

    def resolve(key: str, fallback: str, *, use_dotenv: bool = False) -> Tuple[str, str]:
        """按优先级取值并说明来源，不写 os.environ（由 put 负责）。

        链：进程环境 > ``.env.local`` > （``use_dotenv`` 时）``.env`` > 录制档默认。
        ``.env`` 默认**不在**链里：它是容器视角写的（远端模型 API + 内网名），
        压不过「本机录制」这一档语义；需要它的值赢，就把它写进 ``.env.local``
        （机器私有、git 忽略）——本机 Neo4j 凭据就是这么采纳的。
        """
        if current.get(key):
            return current[key], "进程环境"
        if local.get(key):
            return local[key], ".env.local"
        if use_dotenv and dotenv.get(key):
            return dotenv[key], ".env"
        return fallback, "录制档默认"

    def pick(key: str, fallback: str, *, use_dotenv: bool = False) -> None:
        value, source = resolve(key, fallback, use_dotenv=use_dotenv)
        put(key, value, source)

    def remap(key: str, service: str, container_port: int, fallback: str) -> None:
        """采纳已有值（含 ``.env``）并把内网名换成本机端口。"""
        value, source = resolve(key, fallback, use_dotenv=True)
        if source == ".env":
            source = ".env(内网名重映射)"
        put(key, remap_compose_host(value, service, container_port), source)

    # --- 语言模型：本地 LM Studio。
    # `.env` 的 LLM_BASE_URL 是远端 API（实测 host=api.deepseek.com），与「模型一律
    # 用本地 lmstudio」冲突 ⇒ 这一组**不采纳 .env**，只认进程环境 / .env.local。
    chat_base, chat_base_src = resolve("LLM_BASE_URL", LM_STUDIO_BASE_URL)
    chat_model, chat_model_src = resolve("LLM_MODEL", DEFAULT_CHAT_MODEL)
    for key, value, source in (
        ("LLM_PROVIDER", "openai", "录制档默认"),
        ("LLM_BASE_URL", chat_base, chat_base_src),
        ("LLM_MODEL", chat_model, chat_model_src),
        ("LLM_API_KEY", "lm-studio", "录制档默认"),
        # 少数组件（LightRAG 线）只读 OPENAI_*，一并指到同一本地端点
        ("OPENAI_API_KEY", "lm-studio", "录制档默认"),
        ("KB_LLM_PROVIDER", "openai", "录制档默认"),
        ("KB_LLM_BASE_URL", chat_base, chat_base_src),
        ("KB_LLM_MODEL", chat_model, chat_model_src),
        ("KB_LLM_API_KEY", "lm-studio", "录制档默认"),
    ):
        put(key, value, source)

    # --- Embedding：模型名与端点必须成对采纳。
    # `.env` 的 embedding 端点已是本机回环（LM Studio）⇒ 连模型名一起采纳；
    # 若它指向远端，则整套换成录制档默认——**不能**只换端点留模型名，
    # 那会录出一串「端点没有这个模型」的 404。
    emb_base, emb_base_src = resolve("EMBEDDING_BASE_URL", LM_STUDIO_BASE_URL, use_dotenv=True)
    if _is_loopback(emb_base):
        emb_model, emb_model_src = resolve(
            "EMBEDDING_MODEL", dotenv.get("EMBEDDING_MODEL") or DEFAULT_EMBEDDING_MODEL,
            use_dotenv=True,
        )
    else:
        emb_model, emb_model_src = resolve("EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL)
    for key, value, source in (
        ("EMBEDDING_PROVIDER", "openai", "录制档默认"),
        ("EMBEDDING_BASE_URL", emb_base, emb_base_src),
        ("EMBEDDING_MODEL", emb_model, emb_model_src),
        ("EMBEDDING_API_KEY", "lm-studio", "录制档默认"),
    ):
        put(key, value, source)

    # --- 知识库语料：SQLite + FAISS（local_kb_demo seed 出来的那份真法条）。
    # `.env` 的 DATABASE_URL 指向 compose 的 postgres（宿主机解析不到，且那份库里
    # 没有本次语料）⇒ 强制指向 seed 过的演示库，否则录出来是「检索恒空」。
    demo_db = REPO_ROOT / "data" / "demo" / "agentix_demo.db"
    pick("DATABASE_URL", f"sqlite:///{demo_db.as_posix()}")
    pick("VECTOR_STORE_BACKEND", "faiss")
    pick("KB_SCOPE_DESCRIPTION", LEGAL_SCOPE_DESCRIPTION)

    # --- Text2SQL 目标库：`.env` 没有这个键，从 compose 的 mysql 服务推导
    mysql_env = compose_service_environment("mysql")
    missing = [k for k in ("MYSQL_USER", "MYSQL_PASSWORD", "MYSQL_DATABASE") if not mysql_env.get(k)]
    if missing:
        raise RuntimeError(
            f"docker-compose.yml 的 mysql 服务缺少 {missing}，无法推导 "
            "TEXT2SQL_DATABASE_URL——请在 .env.local 里显式给出。"
        )
    pick(
        "TEXT2SQL_DATABASE_URL",
        "mysql+pymysql://{u}:{p}@127.0.0.1:{port}/{db}".format(
            u=mysql_env["MYSQL_USER"],
            p=mysql_env["MYSQL_PASSWORD"],
            port=published_host_port("mysql", 3306),
            db=mysql_env["MYSQL_DATABASE"],
        ),
    )

    # --- 图谱 / 缓存 / 队列：采纳已有值（含 .env 的凭据），只把内网名换成本机端口
    remap("NEO4J_URI", "neo4j", 7687, "bolt://127.0.0.1:7687")
    pick("NEO4J_USER", "neo4j", use_dotenv=True)
    pick("NEO4J_PASSWORD", "legalpass", use_dotenv=True)
    for key, fallback in (
        ("REDIS_URL", "redis://127.0.0.1:6379/0"),
        ("CACHE_REDIS_URL", "redis://127.0.0.1:6379/1"),
        ("REDIS_CHECKPOINT_URL", "redis://127.0.0.1:6379/2"),
        ("REDIS_CELERY_URL", "redis://127.0.0.1:6379/3"),
        ("CELERY_BROKER_URL", "redis://127.0.0.1:6379/4"),
        ("CELERY_RESULT_BACKEND", "redis://127.0.0.1:6379/5"),
    ):
        remap(key, "redis", 6379, fallback)
    # MILVUS_* 是 host/port 分家的两个键，所以重映射时不带端口。
    # Milvus 镜像不在本机、通道注定不可用——这里仍换成本机回环而不是留内网名，
    # 因为「解析不到主机名」和「连不上端口」在日志里是两种诊断，前者会误导人。
    milvus_host, milvus_src = resolve("MILVUS_HOST", "127.0.0.1", use_dotenv=True)
    put(
        "MILVUS_HOST",
        remap_compose_host(milvus_host, "milvus", 19530, include_port=False),
        milvus_src,
    )
    pick("MILVUS_PORT", str(published_host_port("milvus", 19530) or 19530))

    # --- kb_ingest 通道：**不能**沿用 .env 的 localhost:8000。
    # 本机 8000 上是另一个项目的服务（实测 /openapi.json 报 TicketPilot），
    # 它的 /api/search 只会回 404——录制打到它是「跨项目串台」，不是「通道不可用」。
    # kb_ingest 自己的映射端口从 compose 推导（KB_INGEST_PORT，默认 8100）。
    kb_port = published_host_port("kb_ingest", 8000) or 8100
    pick("INGEST_SERVICE_URL", f"http://127.0.0.1:{kb_port}")

    # --- 噪音控制：宿主机 .env 常开 DEBUG ⇒ SQLAlchemy 把每条 SQL 打到 stdout
    pick("LOG_LEVEL", "WARNING")
    pick("DB_ECHO", "false")

    return resolved, current, notes


def apply_local_recording_env() -> List[str]:
    """把录制档写进 ``os.environ``，返回「改了什么」清单（脱敏，不留暗改）。"""
    resolved, _old, notes = build_local_recording_env()
    os.environ.update(resolved)
    return notes


# ---------------------------------------------------------------------------
# 录制前自检
# ---------------------------------------------------------------------------


def _http_json(url: str, timeout: float = 5.0) -> Optional[Any]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception:  # noqa: BLE001 - 自检要把「够不到」如实报出来，不外抛细节
        return None


def preflight_local_recording(*, golden_rows: int) -> Tuple[List[str], List[str]]:
    """录制前把每条依赖实测一遍，返回 ``(ok_lines, problems)``。

    为什么要自检而不是直接跑：一次录制要发 ``golden_rows`` 条真实请求（本地 8B
    模型，单条 17–92 s），任何一头没接上，代价是「跑完两小时才发现全是兜底答案」。
    自检只花几秒，且把「通道不可用」（可接受，指标口径已容忍 degraded）与
    「根本够不到模型/图谱/SQL」（不可接受，基线没有意义）分开报。
    """
    ok: List[str] = []
    problems: List[str] = []

    if golden_rows <= 0:
        problems.append("金标集为空——没有题可录。")
    else:
        ok.append(f"金标题数 = {golden_rows}")

    # ① LM Studio：模型列表里必须真有配置的 chat / embedding 模型 id
    models = _http_json(
        os.environ.get("LLM_BASE_URL", LM_STUDIO_BASE_URL).rstrip("/") + "/models"
    )
    if not models or not isinstance(models, dict) or "data" not in models:
        problems.append(
            f"LLM 端点不可达："
            f"{mask(os.environ.get('LLM_BASE_URL', ''))}（LM Studio 未启动？）"
        )
    else:
        ids = {str(item.get("id")) for item in models.get("data", [])}
        for key in ("LLM_MODEL", "EMBEDDING_MODEL"):
            want = os.environ.get(key, "")
            if want in ids:
                ok.append(f"{key} 在端点上可用")
            else:
                problems.append(
                    f"{key}={want!r} 不在端点模型列表里（现有 {len(ids)} 个 id，"
                    "请在 .env.local 里改成实际加载的 id）"
                )

    # ② 知识库语料（SQLite）：文档/分块行数决定 citation 是否有据可查
    db_url = os.environ.get("DATABASE_URL", "")
    if db_url.startswith("sqlite"):
        # 用字符串切而不是 urlsplit：`sqlite:///D:/x/y.db` 经 urlsplit 得到的是
        # `/D:/x/y.db`，在 Windows 上是个开不了的绝对路径（实测把自检点炸成了
        # 「读不到表」，而真因只是解析）。
        raw = db_url.split("///", 1)[-1] if "///" in db_url else db_url.split("//", 1)[-1]
        path = Path(raw)
        if not path.is_absolute():
            path = REPO_ROOT / path
        if not path.exists():
            problems.append(f"知识库 SQLite 文件不存在：{path}（先跑 local_kb_demo seed）")
        else:
            import sqlite3

            try:
                con = sqlite3.connect(str(path))
                tables = {
                    str(row[0])
                    for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
                }
                # 表名按 SQLModel 的实际形态探（本仓是单数 document/chunk；
                # 复数是常见的另一种写法）。写死一种 ⇒ 换 ORM 约定就误报「库是空的」。
                docs_table = next((t for t in ("document", "documents") if t in tables), None)
                chunks_table = next((t for t in ("chunk", "chunks") if t in tables), None)
                if docs_table is None or chunks_table is None:
                    problems.append(
                        f"知识库缺少 document/chunk 表（现有 {len(tables)} 张表，"
                        f"如 {sorted(tables)[:3]}）"
                    )
                else:
                    docs = con.execute(f"SELECT COUNT(*) FROM {docs_table}").fetchone()[0]
                    chunks = con.execute(f"SELECT COUNT(*) FROM {chunks_table}").fetchone()[0]
                    if docs == 0 or chunks == 0:
                        problems.append(f"知识库为空：{docs_table}={docs} {chunks_table}={chunks}")
                    else:
                        ok.append(f"知识库 {docs_table}={docs} {chunks_table}={chunks}")
                con.close()
            except Exception as exc:  # noqa: BLE001 - 表结构不符也要如实报
                problems.append(f"知识库读取失败：{type(exc).__name__}: {exc}")
    else:
        ok.append("知识库走非 SQLite 后端，跳过行数核对")

    # ③ Neo4j：能认证 + 有节点
    try:
        from neo4j import GraphDatabase

        driver = GraphDatabase.driver(
            os.environ.get("NEO4J_URI"),
            auth=(
                os.environ.get("NEO4J_USER", "neo4j"),
                os.environ.get("NEO4J_PASSWORD", ""),
            ),
        )
        with driver.session() as session:
            n = session.run("MATCH (n) RETURN count(n) AS c").single()["c"]
        driver.close()
        (ok if n > 0 else problems).append(
            f"Neo4j 节点数={n}" if n > 0 else "Neo4j 可达但图为空（先导入图谱 CSV）"
        )
    except Exception as exc:  # noqa: BLE001 - 够不到图谱就是问题，不外抛
        problems.append(f"Neo4j 不可用：{type(exc).__name__}")

    # ④ Text2SQL 目标库：条文总数决定 text2sql 题的答案对不对
    try:
        from sqlalchemy import create_engine, text

        engine = create_engine(os.environ.get("TEXT2SQL_DATABASE_URL", ""), future=True)
        with engine.begin() as conn:
            total = conn.execute(text("SELECT COUNT(*) FROM articles")).scalar()
        engine.dispose()
        (ok if total else problems).append(
            f"Text2SQL articles={total}"
            if total
            else "Text2SQL 目标库 articles 为空（先跑 scripts/data/backfill_legal_sql.py --apply）"
        )
    except Exception as exc:  # noqa: BLE001
        problems.append(f"Text2SQL 目标库不可用：{type(exc).__name__}")

    # ⑤ Redis（观测面 + checkpointer）
    try:
        import redis

        client = redis.Redis.from_url(os.environ.get("REDIS_URL", ""), socket_connect_timeout=3)
        client.ping()
        ok.append("Redis 可 ping")
    except Exception as exc:  # noqa: BLE001
        problems.append(f"Redis 不可用：{type(exc).__name__}（观测数据会丢）")

    # ⑥ kb_ingest 通道：不可用是**已知限制**，不是问题——但必须说出来，
    # 否则「为什么每条 kb 题都 degraded」要重新考古一遍。
    ingest = os.environ.get("INGEST_SERVICE_URL", "")
    health = _http_json(ingest.rstrip("/") + "/health", timeout=3.0) if ingest else None
    if health is None:
        ok.append(
            f"kb_ingest 通道不可用（{mask(ingest)}）⇒ kb 路由记为 degraded，"
            "指标口径已容忍（『单纯的 degraded 不算误拒』）"
        )
    else:
        ok.append(f"kb_ingest 通道可用：{json.dumps(health, ensure_ascii=False)[:80]}")

    return ok, problems
