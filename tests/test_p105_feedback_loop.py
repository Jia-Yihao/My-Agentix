"""P105：满意度反馈闭环（P2-1）测试。

覆盖：
① 后端端点契约：up/down 落 EventPort（memory / jsonl 两后端一致）、
   事件 payload 字段族、指标 feedback_total{rating} 打点
② 归属校验：跨用户提交回 404（防存在性枚举），匿名（无身份声明）直通
③ flag-off：路由不注册 = 404，与基线等价（聚合点源级棘轮）
④ PII 脱敏：reason 中的电话/邮箱落盘前脱敏（FEEDBACK_PII_REDACT 默认开）
⑤ 导出脚本：down → feedback_ingest 输入格式（error_code=user_feedback_down）、
   up 不回流、事件日志读取、损坏行容忍
⑥ 五处同步棘轮 + p100 白名单含 rating
⑦ 探活端点：GET /feedback/status → {"enabled": true}（前端自动发现接线）
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentix.config import settings

PROJECT_ROOT = Path(__file__).resolve().parents[1]


# ---------- 装配辅助 ----------

def _feedback_app() -> FastAPI:
    """轻量 app：只挂 feedback router（不走 api_router 聚合——其 flag 判定
    发生在 import 时刻，测试用直挂方式覆盖 flag-on 行为面）。"""
    from agentix.infrastructure.core.database import get_db as real_get_db
    from agentix.interfaces.http.v1 import feedback

    app = FastAPI()
    app.include_router(feedback.router, prefix="/api/v1/feedback")
    app.dependency_overrides[real_get_db] = lambda: None  # 归属校验被 monkeypatch，DB 不触达
    return app


def _client() -> TestClient:
    return TestClient(_feedback_app(), raise_server_exceptions=False)


@pytest.fixture()
def memory_port():
    """干净的进程内 EventPort（测试隔离）。"""
    from agentix.agent.events.adapter import InMemoryEventBus
    from agentix.agent.events import port as event_port_mod

    bus = InMemoryEventBus()
    event_port_mod._event_port = bus
    yield bus
    event_port_mod._event_port = None


@pytest.fixture()
def fake_ownership(monkeypatch):
    """会话归属打桩：session sess-1 属 owner-a（不触真实 DB）。"""
    from agentix.application.services.chat_service import get_chat_service

    svc = get_chat_service()
    monkeypatch.setattr(svc, "session_exists", lambda db, sid: sid == "sess-1")
    monkeypatch.setattr(svc, "get_session_owner", lambda db, sid: "owner-a")


# ---------- ① 端点契约 ----------

class TestFeedbackEndpoint:
    def test_down_feedback_publishes_event(self, memory_port, fake_ownership):
        resp = _client().post(
            "/api/v1/feedback",
            json={"session_id": "sess-1", "rating": "down", "reason": "答案不对"},
            headers={"X-Device-ID": "owner-a"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "queued"
        assert body["event_type"] == "message_feedback"
        recent = memory_port.list_recent("message_feedback")
        assert len(recent) == 1
        payload = recent[0]["payload"]
        assert payload["rating"] == "down"
        assert payload["session_id"] == "sess-1"
        assert payload["user"] == "owner-a"
        assert payload["reason"] == "答案不对"

    def test_up_feedback_publishes_event(self, memory_port, fake_ownership):
        resp = _client().post(
            "/api/v1/feedback",
            json={"session_id": "sess-1", "rating": "up"},
            headers={"X-Device-ID": "owner-a"},
        )
        assert resp.status_code == 200
        recent = memory_port.list_recent("message_feedback")
        assert len(recent) == 1
        assert recent[0]["payload"]["rating"] == "up"

    def test_rating_is_bounded(self, memory_port, fake_ownership):
        """rating 越界（非 up|down）→ 422 校验拒绝，不落任何事件。"""
        resp = _client().post(
            "/api/v1/feedback",
            json={"session_id": "sess-1", "rating": "meh"},
            headers={"X-Device-ID": "owner-a"},
        )
        assert resp.status_code == 422
        assert memory_port.list_recent("message_feedback") == []

    def test_jsonl_backend_publishes_consistently(self, tmp_path, fake_ownership, monkeypatch):
        """jsonl 后端与 memory 后端同契约：publish 落盘可回放。"""
        from agentix.agent.events.jsonl import JsonlEventBus
        from agentix.agent.events import port as event_port_mod

        log_path = tmp_path / "event_log.jsonl"
        event_port_mod._event_port = JsonlEventBus(log_path)
        try:
            resp = _client().post(
                "/api/v1/feedback",
                json={"session_id": "sess-1", "rating": "down", "reason": "引用错误"},
                headers={"X-Device-ID": "owner-a"},
            )
            assert resp.status_code == 200
            # 落盘行可解析且含 publish 快照
            lines = [json.loads(x) for x in log_path.read_text(encoding="utf-8").splitlines() if x.strip()]
            pub = [e for e in lines if e.get("op") == "publish"]
            assert len(pub) == 1
            assert pub[0]["event_type"] == "message_feedback"
            assert pub[0]["payload"]["rating"] == "down"
        finally:
            event_port_mod._event_port = None

    def test_metric_recorded(self, memory_port, fake_ownership):
        from agentix.observability.metrics import FEEDBACK_TOTAL, record_feedback

        before = FEEDBACK_TOTAL.labels(rating="down")._value.get()
        resp = _client().post(
            "/api/v1/feedback",
            json={"session_id": "sess-1", "rating": "down"},
            headers={"X-Device-ID": "owner-a"},
        )
        assert resp.status_code == 200
        assert FEEDBACK_TOTAL.labels(rating="down")._value.get() == before + 1
        # 越界 rating 不记账（record_feedback 直接调用面）
        record_feedback("sideways")
        assert "sideways" not in FEEDBACK_TOTAL._metrics


# ---------- ② 归属校验 ----------

class TestOwnership:
    def test_cross_user_404(self, memory_port, fake_ownership):
        resp = _client().post(
            "/api/v1/feedback",
            json={"session_id": "sess-1", "rating": "down"},
            headers={"X-Device-ID": "device-b"},  # owner-a ≠ device-b
        )
        assert resp.status_code == 404
        assert memory_port.list_recent("message_feedback") == []

    def test_owner_allowed(self, memory_port, fake_ownership):
        resp = _client().post(
            "/api/v1/feedback",
            json={"session_id": "sess-1", "rating": "up"},
            headers={"X-Device-ID": "owner-a"},
        )
        assert resp.status_code == 200

    def test_anonymous_caller_passes(self, memory_port, fake_ownership):
        """无身份声明（strangler 匿名域）= 既有行为，不判归属。"""
        resp = _client().post("/api/v1/feedback", json={"session_id": "sess-1", "rating": "up"})
        assert resp.status_code == 200


# ---------- ③ flag-off ----------

class TestFlagOff:
    def test_default_off_router_not_registered(self):
        """默认 FEEDBACK_ENABLED=False：api_router 聚合不含 /feedback 路由。"""
        from agentix.interfaces.http.v1 import api_router

        paths = {getattr(r, "path", "") for r in api_router.routes}
        assert not any(p.startswith("/feedback") for p in paths)

    def test_aggregation_guard_source_level(self):
        """源级棘轮：聚合点必须按 flag 条件 include（防被人随手改成无条件）。"""
        src = (PROJECT_ROOT / "agentix/interfaces/http/v1/__init__.py").read_text(encoding="utf-8")
        assert "if settings.FEEDBACK_ENABLED:" in src

    def test_settings_default_off(self):
        assert settings.FEEDBACK_ENABLED is False
        assert settings.FEEDBACK_PII_REDACT is True


# ---------- ④ PII 脱敏 ----------

class TestPiiRedact:
    def test_reason_redacted_by_default(self, memory_port, fake_ownership):
        resp = _client().post(
            "/api/v1/feedback",
            json={"session_id": "sess-1", "rating": "down",
                  "reason": "联系我 13812345678 或 a@b.com"},
            headers={"X-Device-ID": "owner-a"},
        )
        assert resp.status_code == 200
        payload = memory_port.list_recent("message_feedback")[0]["payload"]
        assert "13812345678" not in payload["reason"]
        assert "[PHONE]" in payload["reason"]
        assert "[EMAIL]" in payload["reason"]

    def test_redact_disabled_keeps_raw(self, memory_port, fake_ownership, monkeypatch):
        monkeypatch.setattr(settings, "FEEDBACK_PII_REDACT", False)
        resp = _client().post(
            "/api/v1/feedback",
            json={"session_id": "sess-1", "rating": "down", "reason": "13812345678"},
            headers={"X-Device-ID": "owner-a"},
        )
        assert resp.status_code == 200
        payload = memory_port.list_recent("message_feedback")[0]["payload"]
        assert payload["reason"] == "13812345678"


# ---------- ⑤ 导出脚本 ----------

class TestFeedbackExport:
    def test_to_ingest_record_down(self):
        from evals.feedback_export import USER_FEEDBACK_DOWN_CODE, to_ingest_record

        rec = to_ingest_record({
            "event_type": "message_feedback",
            "payload": {"rating": "down", "reason": "答非所问",
                        "session_id": "s1", "trace_id": "t1", "user": "d1"},
        })
        assert rec is not None
        assert rec["error_code"] == USER_FEEDBACK_DOWN_CODE
        assert rec["message"] == "答非所问"
        assert rec["session_id"] == "s1"
        # 不臆造：question/route_type 不在事件里就留空
        assert "question" not in rec

    def test_to_ingest_record_up_not_exported(self):
        from evals.feedback_export import to_ingest_record

        assert to_ingest_record({"payload": {"rating": "up"}}) is None

    def test_load_events_from_file_tolerates_corrupt_lines(self, tmp_path):
        from evals.feedback_export import load_events_from_file

        log = tmp_path / "event_log.jsonl"
        good = {"op": "publish", "event_id": "e1", "event_type": "message_feedback",
                "payload": {"rating": "down"}}
        log.write_text(
            json.dumps(good, ensure_ascii=False) + "\n" + "{corrupt half line\n",
            encoding="utf-8",
        )
        events = load_events_from_file(log)
        assert len(events) == 1
        assert events[0]["event_id"] == "e1"

    def test_end_to_end_ingest_pipeline(self, tmp_path):
        """导出产物可直接喂 feedback_ingest（回流管道零新增的契约验证）。"""
        import io
        from contextlib import redirect_stdout

        from evals import feedback_export, feedback_ingest

        log = tmp_path / "event_log.jsonl"
        lines = [
            json.dumps({
                "op": "publish", "event_id": f"e{i}", "event_type": "message_feedback",
                "payload": {"rating": "down", "reason": f"bad-{i}", "session_id": f"s{i}"},
            }, ensure_ascii=False)
            for i in range(2)
        ]
        log.write_text("\n".join(lines) + "\n", encoding="utf-8")
        out = tmp_path / "down.jsonl"
        # 缺省题集 legal_golden.jsonl 已于 2026-10-26 删除，feedback_ingest 现在
        # 在"没传 --golden 且默认文件不存在"时响亮退出（T3）⇒ 这里显式给一份**空的**
        # 现有评估集：候选 id 起始序号仍是 C001，与本用例原本要验的管道行为一致。
        existing_golden = tmp_path / "existing_golden.jsonl"
        existing_golden.write_text("", encoding="utf-8")
        buf = io.StringIO()
        with redirect_stdout(buf):
            assert feedback_export.run(log, out) == 0
            # 导出产物 → feedback_ingest 主流程（应产出候选文件而非报错）
            rc = feedback_ingest.main(
                [
                    "--input",
                    str(out),
                    "--out-dir",
                    str(tmp_path / "cand"),
                    "--golden",
                    str(existing_golden),
                ]
            )
        assert rc == 0
        candidates = list((tmp_path / "cand").glob("*.candidates.jsonl"))
        assert len(candidates) == 1
        rows = [json.loads(x) for x in candidates[0].read_text(encoding="utf-8").splitlines() if x.strip()]
        assert len(rows) == 2
        assert all(r["_needs_review"] for r in rows)


# ---------- ⑥ 同步棘轮 ----------

class TestSyncRatchet:
    def test_five_place_sync(self):
        """settings / selfcheck / .env.example / 配置管理.md / p41 清单。"""
        from agentix.config import selfcheck

        for key in ("FEEDBACK_ENABLED", "FEEDBACK_PII_REDACT"):
            assert hasattr(settings, key)
            assert key in selfcheck.DEFAULT_IGNORED_MISSING
            assert key in (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8-sig")
            assert f"| `{key}`" in (
                PROJECT_ROOT / "docs/architecture/配置管理.md"
            ).read_text(encoding="utf-8")
            assert f'"{key}"' in (
                PROJECT_ROOT / "tests/test_p41_doc_consistency.py"
            ).read_text(encoding="utf-8")

    def test_p100_allows_rating_label(self):
        from tests.test_p100_genai_observability import ALLOWED_METRIC_LABELS

        assert "rating" in ALLOWED_METRIC_LABELS


# ---------- ⑦ 探活端点（前端自动发现接线） ----------

class TestStatusEndpoint:
    def test_status_reports_enabled(self, memory_port, fake_ownership):
        """flag-on（router 直挂 = 等价 flag-on 行为面）→ 200 {"enabled": true}。"""
        resp = _client().get("/api/v1/feedback/status")
        assert resp.status_code == 200
        assert resp.json() == {"enabled": True}

    def test_status_route_registered_source_level(self):
        """源级棘轮：status 路由必须挂在 feedback router 内（跟随同一 flag 门），
        防止被人挪到无条件聚合点破坏 flag-off 等价性。"""
        src = (PROJECT_ROOT / "agentix/interfaces/http/v1/feedback.py").read_text(
            encoding="utf-8"
        )
        assert '@router.get("/status"' in src
        assert "FeedbackStatusResponse" in src
