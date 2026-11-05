"""§24.3 T1 ⑪：`/settings/{kb_id}` 的租户归属 + admin 升格（`SETTINGS_AUTHZ_ENABLED`）。

**为什么需要它**（项目实现手册 §24.2 G9 的后半段）：G9 把响应里的 `*_api_key` 掩成 `***`，
治的是「读回显」；这条路上「谁能写」一直没人管——路由只挂 `Depends(get_current_user)`，
于是任何登录用户可以读改**任意** `kb_id` 的配置，租户归属一概不看。可写字段里有
`llm_base_url` / `embedding_base_url`：把它们换成自己的端点，之后那个租户的每一条提问
和每一段召回上下文都会发过去，**全程不需要拿到任何密钥**。掩码对此无能为力——
掩码管的是「值不外流」，这里利用的是「值可以被换掉」。

口径与既有实现保持一致，本文件不引入新的授权语义：
读 → `authz.py::require_kb_in_tenant`（不存在 404 / 跨租户 403，不泄露存在性）；
写 → 其上再加 `rbac.py::require_admin`。

**为什么走真实 HTTP**：越权这件事的产物是**状态码 + 响应体**，前端要按它决定文案。
只断言 helper 会 raise，证明不了异常处理器接住了它，所以这里真起一个只挂 settings 路由
的 FastAPI，用 `TestClient` 逐条打过去。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# ---------------------------------------------------------------------------
# 夹具：一份**文件型** SQLite + 只挂 settings 路由的 mini app
#
# 不用内存库：SQLAlchemy 对 `sqlite://:memory:` 默认 SingletonThreadPool，连接按线程给——
# TestClient 把同步端点丢进 worker 线程后，那个线程拿到的是**另一个空的内存库**，表都不
# 存在，测出来的是连接池行为而不是授权行为。文件库走 QueuePool，配合
# connect_args 里的 check_same_thread=False，主线程建表、worker 线程读写，看到的是同一份数据。
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def engine(tmp_path_factory):
    from sqlalchemy import create_engine
    from sqlmodel import SQLModel

    import agentix.retrieval.api.routers.settings  # noqa: F401  注册 ModelConfig
    import agentix.retrieval.domain.models  # noqa: F401  注册检索域全部模型

    if SQLModel.metadata.schema is not None:
        # PG 形态下 metadata 带着 schema=retrieval，打到本地 sqlite 文件只会报
        # 「unknown database」。本套回归的前提就是离线 SQLite（tests/conftest.py）；
        # 真跑到 PG 形态的流水线应当换一份 PG 临时库，而不是在这里悄悄改口径。
        pytest.skip("SQLModel.metadata.schema 非 SQLite 形态，p112 的文件库夹具不适用")
    db_file = tmp_path_factory.mktemp("p112") / "settings_authz.db"
    eng = create_engine(
        f"sqlite:///{db_file}",
        echo=False,
        future=True,
        connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(eng)
    return eng


@pytest.fixture(scope="module")
def ids(engine):
    """两个租户：A 有 admin + member，B 只有 member；A/B 各一个 kb，外加一个无租户的存量行。"""
    from sqlmodel import Session

    from agentix.retrieval.domain.models.kb import KnowledgeBase
    from agentix.retrieval.domain.models.tenant import Tenant
    from agentix.retrieval.domain.models.user import User

    with Session(engine) as session:
        tenant_a, tenant_b = Tenant(name="p112-a"), Tenant(name="p112-b")
        session.add(tenant_a)
        session.add(tenant_b)
        session.commit()
        session.refresh(tenant_a)
        session.refresh(tenant_b)

        def _user(username: str, tenant_id: str, role: str) -> User:
            return User(
                username=username,
                email=f"{username}@p112.test",
                tenant_id=tenant_id,
                role=role,
            )

        admin_a = _user("admin-a", tenant_a.id, "admin")
        member_a = _user("member-a", tenant_a.id, "member")
        member_b = _user("member-b", tenant_b.id, "member")
        session.add_all([admin_a, member_a, member_b])

        kb_a = KnowledgeBase(name="p112-kb-a", owner_user_id=member_a.id, tenant_id=tenant_a.id)
        kb_b = KnowledgeBase(name="p112-kb-b", owner_user_id=member_b.id, tenant_id=tenant_b.id)
        # 写侧用例专用（租户 A）：不与其它用例共用同一行 model_config，
        # 这样「403 什么都没写」的断言与用例先后顺序无关
        kb_w = KnowledgeBase(name="p112-kb-w", owner_user_id=admin_a.id, tenant_id=tenant_a.id)
        # 只给「连续两次 PUT」那条用例：它断言的是整行的最终内容，必须独占一行
        kb_w2 = KnowledgeBase(name="p112-kb-w2", owner_user_id=admin_a.id, tenant_id=tenant_a.id)
        # 迁移没回填到的存量形态：tenant_id 仍为空
        kb_orphan = KnowledgeBase(name="p112-kb-orphan", owner_user_id=admin_a.id, tenant_id=None)
        session.add_all([kb_a, kb_b, kb_w, kb_w2, kb_orphan])
        session.commit()

        return {
            "admin_a": admin_a.id,
            "member_a": member_a.id,
            "member_b": member_b.id,
            "kb_a": kb_a.id,
            "kb_b": kb_b.id,
            "kb_w": kb_w.id,
            "kb_w2": kb_w2.id,
            "kb_orphan": kb_orphan.id,
        }


@pytest.fixture
def actor():
    """当前请求的「登录者」——由 dependency_overrides 注入，逐条用例切换。"""
    return {"user": None}


@pytest.fixture
def client(engine, ids, actor, monkeypatch):
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse
    from fastapi.testclient import TestClient
    from sqlmodel import Session

    from agentix.config.settings import settings as app_settings
    from agentix.retrieval.api.deps import get_current_user
    from agentix.retrieval.api.routers.settings import router
    from agentix.retrieval.api.response import ERROR_CODE_TO_STATUS, error
    from agentix.retrieval.errors import AppError
    from agentix.retrieval.infra.db.session import get_session

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: actor["user"]

    def _session():
        with Session(engine) as s:
            yield s

    app.dependency_overrides[get_session] = _session

    # 与 agentix/main.py::app_error_handler 同口径：登记过的错误码用映射状态码。
    # 不接这一层的话 AppError 会被 Starlette 收敛成 500，⑪ 的 403/404 就白设了。
    @app.exception_handler(AppError)
    async def _app_error(request, exc: AppError):
        status = ERROR_CODE_TO_STATUS.get(exc.error_code) or exc.status_code
        return JSONResponse(
            status_code=status,
            content=error(
                error_code=exc.error_code,
                message=exc.message,
                user_hint=exc.user_hint,
                request_id=getattr(request.state, "request_id", ""),
                details=exc.details,
            ),
        )

    monkeypatch.setattr(app_settings, "SETTINGS_AUTHZ_ENABLED", False)
    return TestClient(app, raise_server_exceptions=False)


def _login(actor, ids, engine, who: str) -> None:
    from sqlmodel import Session

    from agentix.retrieval.domain.models.user import User

    with Session(engine) as session:
        user = session.get(User, ids[who])
    assert user is not None, who
    actor["user"] = user


def _model_config_row(engine, kb_id: str):
    from sqlmodel import Session, select

    from agentix.retrieval.api.routers.settings import ModelConfig

    with Session(engine) as session:
        return session.exec(select(ModelConfig).where(ModelConfig.kb_id == kb_id)).first()


def _row_config(engine, kb_id: str) -> dict:
    """落库内容的快照（无行 = 空字典），用于「403 什么都没写」这类先后无关的断言。"""
    row = _model_config_row(engine, kb_id)
    return dict(row.config) if row and row.config else {}


@pytest.fixture
def armed(client, monkeypatch):
    from agentix.config.settings import settings as app_settings

    monkeypatch.setattr(app_settings, "SETTINGS_AUTHZ_ENABLED", True)
    return client


# ---------------------------------------------------------------------------
# 1. 默认姿态：关
# ---------------------------------------------------------------------------


def test_flag_defaults_off() -> None:
    from agentix.config.settings import Settings

    fields = Settings.model_fields
    assert "SETTINGS_AUTHZ_ENABLED" in fields
    assert fields["SETTINGS_AUTHZ_ENABLED"].default is False, "收紧默认关是纪律，不是偏好"


def test_flag_is_ignored_when_missing_from_env() -> None:
    """五处同步之一：允许 `.env` 不设该键（不设 = 默认关 = 与加固前逐位等价）。"""
    from agentix.config.selfcheck import DEFAULT_IGNORED_MISSING

    assert "SETTINGS_AUTHZ_ENABLED" in DEFAULT_IGNORED_MISSING


# ---------------------------------------------------------------------------
# 2. flag-off：与加固前逐位等价（越权仍然成立，正是要关掉的东西）
# ---------------------------------------------------------------------------


def test_flagoff_member_reads_another_tenants_kb(client, actor, ids, engine) -> None:
    _login(actor, ids, engine, "member_b")
    resp = client.get(f"/settings/{ids['kb_a']}")
    assert resp.status_code == 200, resp.text
    assert "llm_base_url" in resp.json()["data"], "flag-off 时连读都不设限"


def test_flagoff_read_of_nonexistent_kb_still_200(client, actor, ids, engine) -> None:
    """不存在的 kb 也 200：这条是「逐位等价」的硬证据，flag-on 时它会变成 404（见 §3）。"""
    _login(actor, ids, engine, "member_b")
    assert client.get("/settings/kb-does-not-exist").status_code == 200


def test_flagoff_member_writes_another_tenants_kb(client, actor, ids, engine) -> None:
    _login(actor, ids, engine, "member_b")
    resp = client.put(f"/settings/{ids['kb_w']}", json={"llm_base_url": "http://attacker:1/v1"})
    assert resp.status_code == 200, resp.text
    assert _row_config(engine, ids["kb_w"]).get("llm_base_url") == "http://attacker:1/v1", (
        "flag-off 仍可跨租户写——G9 的掩码管不到这里"
    )


# ---------------------------------------------------------------------------
# 3. flag-on：读收归属，写再加 admin
# ---------------------------------------------------------------------------


def test_flagon_same_tenant_member_can_read(armed, actor, ids, engine) -> None:
    _login(actor, ids, engine, "member_b")
    resp = armed.get(f"/settings/{ids['kb_b']}")
    assert resp.status_code == 200, resp.text


def test_flagon_cross_tenant_read_is_403(armed, actor, ids, engine) -> None:
    _login(actor, ids, engine, "member_b")
    resp = armed.get(f"/settings/{ids['kb_a']}")
    assert resp.status_code == 403, resp.text
    body = resp.json()
    assert body["error_code"] == "FORBIDDEN"
    assert ids["kb_a"] not in json.dumps(body, ensure_ascii=False), "403 不该把目标 kb 回显出来"


def test_flagon_missing_kb_is_404_not_403(armed, actor, ids, engine) -> None:
    """「不存在」与「存在但无权」必须可区分——这是 authz.py 定下的口径，不是这里新发明的。"""
    _login(actor, ids, engine, "member_b")
    resp = armed.get("/settings/kb-does-not-exist")
    assert resp.status_code == 404, resp.text
    assert resp.json()["error_code"] == "KB_NOT_FOUND"


def test_flagon_orphan_kb_tenant_null_is_403(armed, actor, ids, engine) -> None:
    """迁移没回填的存量行（tenant_id 为空）：一律拒绝，而不是「谁的都不是 ⇒ 谁都能读」。"""
    _login(actor, ids, engine, "admin_a")
    assert armed.get(f"/settings/{ids['kb_orphan']}").status_code == 403


def test_flagon_member_write_is_403_and_nothing_persisted(armed, actor, ids, engine) -> None:
    _login(actor, ids, engine, "member_a")
    before = _row_config(engine, ids["kb_w"])
    resp = armed.put(f"/settings/{ids['kb_w']}", json={"search_k": 9})
    assert resp.status_code == 403, resp.text
    assert resp.json()["error_code"] == "FORBIDDEN"
    assert _row_config(engine, ids["kb_w"]) == before, "403 之前不该落库半个键"


def test_flagon_admin_write_same_tenant_ok(armed, actor, ids, engine) -> None:
    _login(actor, ids, engine, "admin_a")
    resp = armed.put(f"/settings/{ids['kb_w']}", json={"search_k": 9})
    assert resp.status_code == 200, resp.text
    assert _row_config(engine, ids["kb_w"]).get("search_k") == 9


def test_second_put_to_the_same_kb_actually_persists(armed, actor, ids, engine) -> None:
    """⑪ 落文档时顺带撞出来的真 bug，记在这里当作这条回归的来源。

    旧写法是 `row.config.update(...)`——JSON 列没挂 `MutableDict` 监听，原地改字典
    不会被 SQLAlchemy 的变更检测看见。第一次保存靠 INSERT 侥幸写进去了，**对同一个 kb
    的第二次及以后保存则回 200 而一个新键都不落库**（前端显示「已保存」，刷新回到旧值）。
    改成整体重新赋值后修复；本用例连续两次 PUT 不同键，验的是最终整行内容。
    """
    _login(actor, ids, engine, "admin_a")
    assert armed.put(f"/settings/{ids['kb_w2']}", json={"search_k": 7}).status_code == 200
    assert armed.put(f"/settings/{ids['kb_w2']}", json={"chunk_size": 888}).status_code == 200
    assert _row_config(engine, ids["kb_w2"]) == {"search_k": 7, "chunk_size": 888}, (
        "第二次 PUT 必须能落库"
    )


def test_flagon_admin_of_a_cannot_write_kb_of_b(armed, actor, ids, engine) -> None:
    """admin 不是全局通行证：归属这一层先拦住，角色那一层才有意义。"""
    _login(actor, ids, engine, "admin_a")
    resp = armed.put(f"/settings/{ids['kb_b']}", json={"search_k": 9})
    assert resp.status_code == 403, resp.text
    assert _row_config(engine, ids["kb_b"]) == {}


def test_flagon_authorization_runs_before_payload_validation(armed, actor, ids, engine) -> None:
    """越权 + 未知键：必须是 403 而不是 422。

    先校验负载会把「哪些键合法」泄露给无权调用者，也让 422 变成一条探测通道。
    """
    _login(actor, ids, engine, "member_b")
    resp = armed.put(f"/settings/{ids['kb_a']}", json={"totally_unknown": 1})
    assert resp.status_code == 403, resp.text
    assert resp.json()["error_code"] == "FORBIDDEN"


# ---------------------------------------------------------------------------
# 4. 口径同源：不许在路由里再造一份判定
# ---------------------------------------------------------------------------


def test_router_reuses_shared_authz_helpers() -> None:
    """G5 的教训：两闸各持一份口径，改一处漏一处。

    反向钉子：若有人在 settings 路由里手写 `current_user.role != "admin"` 或自己比
    `tenant_id`，本用例必须红。
    """
    import inspect

    from agentix.retrieval.api.routers import settings as settings_router

    src = inspect.getsource(settings_router)
    assert "require_kb_in_tenant(" in src, "归属判定必须复用 authz.py"
    assert "require_admin(" in src, "角色判定必须复用 rbac.py"
    assert 'current_user.role != "admin"' not in src, "不许在路由里再造角色判定"
    assert "tenant_id !=" not in src, "不许在路由里再造归属判定"
    # 两个端点各一处闸门；多一处就是有人把校验挪到了 flag 之外
    assert src.count("app_settings.SETTINGS_AUTHZ_ENABLED") == 2, (
        "SETTINGS_AUTHZ_ENABLED 的读取点必须正好覆盖 GET/PUT 两处，多则失控、少则漏闸"
    )


def test_flagon_does_not_lock_out_the_admin_it_should_serve(armed, actor, ids, engine) -> None:
    """反向钉子：闸门不是无条件 403。同租户 admin 的读写在 flag-on 下照常 200。"""
    _login(actor, ids, engine, "admin_a")
    assert armed.get(f"/settings/{ids['kb_a']}").status_code == 200
    assert armed.put(f"/settings/{ids['kb_a']}", json={"chunk_size": 800}).status_code == 200


# ---------------------------------------------------------------------------
# 5. 部署面：默认关要一路关到容器里
# ---------------------------------------------------------------------------


def test_env_example_pins_it_off() -> None:
    text = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8-sig", errors="replace")
    assert "SETTINGS_AUTHZ_ENABLED=false" in text


def test_compose_forwards_it_off_by_default() -> None:
    import yaml

    doc = yaml.safe_load((PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    env = doc["services"]["backend"]["environment"]
    entries = env if isinstance(env, list) else [f"{k}={v}" for k, v in env.items()]
    line = next((x for x in entries if str(x).startswith("SETTINGS_AUTHZ_ENABLED=")), None)
    assert line is not None, "docker-compose.yml 没把 ⑪ 的开关转发给 backend"
    assert line.endswith(":-False}") or line.endswith(":-false}"), line


def test_staging_override_pins_it_false() -> None:
    """钉 false 的两个理由写在 override 注释里：宿主机插值 + 没有自助升 admin 的接口。"""
    import yaml

    doc = yaml.safe_load(
        (PROJECT_ROOT / "docker/compose.override.staging.yml").read_text(encoding="utf-8")
    )
    env = doc["services"]["backend"]["environment"]
    assert str(env.get("SETTINGS_AUTHZ_ENABLED")).strip().lower() == "false", env


def test_worker_does_not_get_a_knob_it_cannot_use() -> None:
    """retrieval-worker 不挂 settings 路由，就不该收到一个它用不到的开关。"""
    import yaml

    doc = yaml.safe_load(
        (PROJECT_ROOT / "docker/compose.override.staging.yml").read_text(encoding="utf-8")
    )
    worker_env = doc["services"]["retrieval-worker"].get("environment") or {}
    assert "SETTINGS_AUTHZ_ENABLED" not in worker_env


def test_runbook_documents_the_two_preconditions() -> None:
    """开关能不能拧，取决于运维是否知道拧之前要准备什么——文档要能被 grep 到。"""
    text = (PROJECT_ROOT / "docs/runbooks/预发环境与配置矩阵.md").read_text(encoding="utf-8")
    assert "SETTINGS_AUTHZ_ENABLED" in text
    for token in ("tenant_id", "admin", "403"):
        assert token in text, f"runbook 没交代 ⑪ 的前置：{token}"


# ---------------------------------------------------------------------------
# 6. 可发现性：关着的闸门要能在启动日志里被点名
# ---------------------------------------------------------------------------


def test_posture_warning_names_this_gate(monkeypatch) -> None:
    """⑪ 关着 = 登录用户可改任意租户的出站地址，属于「关着就不该对外」的闸门。"""
    import agentix.main as main_module
    from agentix.config.settings import settings as app_settings

    assert "SETTINGS_AUTHZ_ENABLED" in main_module._INSECURE_GATE_FLAGS

    logged: list[str] = []

    def _record(message, *args, **_kw):
        logged.append(str(message).format(*args) if args else str(message))

    monkeypatch.setattr(main_module.logger, "warning", _record)
    monkeypatch.setattr(app_settings, "APP_ENV", "")
    for flag in main_module._INSECURE_GATE_FLAGS:
        monkeypatch.setattr(app_settings, flag, False)
    main_module._warn_insecure_posture()
    assert len(logged) == 1
    assert "SETTINGS_AUTHZ_ENABLED" in logged[0], "点名清单里必须带上它"
