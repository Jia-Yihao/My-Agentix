"""T17：Adaptive 七项 + token 窗口的 A/B 取证（独立入口）。

规划出处：``docs/design/审批与中断恢复优化规划.md`` T17 卡（§4）。
「七项」的唯一真源是 ``README.md`` 的 Adaptive Execution 一行（恰好 7 条）：
档位路由 / fast-path / 准入排队 / 重试预算 / 负载甩载 / 隔离舱 / 单飞合流；
本文件另加**一条非 Adaptive 键**的 A/B：token 级历史窗口
``LG_TOKEN_HISTORY_BUDGET``（T17 卡标题点名，但不属那 7 条）。

**为什么是独立脚本而不是 ``bench/targets.py`` 的静态 ``TARGETS`` 条目**：
同一先例见 ``bench/cancel_propagation.py`` / ``bench/kb_ingest_throughput.py``。
静态登记表的语义是「一个 target = 一种原接口形态」，而本卡是**七组 A/B**，
每组的入口、可测性、读数形态都不同（时延 / 计数 / 离散决策），
塞进登记表会把「目标」与「对照臂」两件事压成一个键。

**每臂一个子进程（本文件用 ``--arm`` 表达）**：``singleflight`` / ``bulkhead`` /
``admission`` / ``load_shedder`` / ``retry_budget`` / ``control_plane`` 都是
**进程级单例**，event-loop lag 是**全局量**。同一进程连跑两臂 = 污染。
本文件因此强制「一次调用只测一个臂」；臂内的 flag-off / flag-on 两个对照
另用 ``reset_*`` 严格复位（``tests/test_p97_runtime_control.py`` 的隔离清单），
并在 ``results.notes.reset_strategy`` 里逐字写明用的是哪一种。

**不复制被测逻辑（``bench/README.md`` 铁律 1）**：本文件只做 import + 调用 +
结果整形。所有「计数壳」都只包一层 ``patch.object`` 数真实现被调了几次
（先例 ``bench/targets.py`` 的 retrieval 构造臂），判定语义仍归被测对象。
唯一的本地判定是**测试台自己的**前置断言（如「flag-off 臂下必须恰好 8 次
下游调用，否则读数作废」）——那是本卡读数成立的前提，不是对被测量的重写。

**分位数、指纹、落盘全走骨架既有实现**：``bench.aggregate.pooled_summary``
（跨轮池化 → ``evals.perf_stats.nearest_rank_quantile``）、
``bench.fingerprint.collect_fingerprint``、``bench.results.write_result``。
指纹里**没有**「哪个 flag 开在哪个值」的字段 ⇒ 臂定义写进
``results.notes.arms``，且目标名带 ``.<arm>.<state>`` 后缀。

**环境闸（硬性）**：``LLM_BASE_URL`` / ``EMBEDDING_BASE_URL`` 必须解析到回环地址，
否则 ``RuntimeError`` 拒绝出数、拒绝任何外部 API。本机 ``.env`` 的
``LLM_BASE_URL`` 指向外部商用端点，因此本脚本**必须**用环境变量覆盖后才跑得动——
这条闸存在的理由就是防止「顺手跑一下」把提示词发出去。

用法::

    LLM_BASE_URL=http://127.0.0.1:1234/v1 LLM_MODEL=qwen/qwen3-8b \
    .venv/Scripts/python.exe -m bench.adaptive_ab --arm probe --repeats 5

    .venv/Scripts/python.exe -m bench.adaptive_ab --arm admission --repeats 5 --samples 5

臂清单（``--arm``）：``probe`` / ``admission`` / ``retry_budget`` /
``load_shedding`` / ``bulkhead`` / ``singleflight`` / ``tier_routing`` /
``token_window`` / ``fastpath``。

**不承诺**：本文件只给读数与实测事实，不带阈值门禁，不改任何默认值，
不推默认值（推默认属灰度类，另行裁决），也不下跨环境结论。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple
from urllib.parse import urlparse

_REPO_ROOT = str(Path(__file__).resolve().parents[1])
if _REPO_ROOT not in sys.path:  # 兼容非 -m 方式直跑
    sys.path.insert(0, _REPO_ROOT)

from bench.aggregate import pooled_summary  # noqa: E402
from bench.fingerprint import MIN_REPEATS, QUANTILE_SOURCE, collect_fingerprint  # noqa: E402
from bench.results import DEFAULT_RESULTS_DIR, write_result  # noqa: E402

TARGET_NAME = "adaptive_ab"

#: 逐字记录调的是哪个原接口（进指纹的 ``entry`` 字段）
ENTRY = (
    "七组原接口直调（无第二实现）：admission_wait / run_task / "
    "get_load_shedder().current_level()+should_shed / pooled_wait_for+pool_for_tool / "
    "retrieve_scoped_docs_cached / get_llm_client(role=...).model_name / "
    "create_message_window_node(...) ; fastpath 臂额外走 analyze_and_route_query"
)

#: 读数口径：被测对象的边界（下游引用读数时必须连着这句一起引）
MEASUREMENT_SCOPE = (
    "每个臂各自定义边界（见该臂 results.notes.what_is_measured）。共性："
    "① 全部 flag 取值由本脚本按臂注入，**不改默认值**；"
    "② 时延类读数只覆盖被测调用首尾（不含 import/预热）；"
    "③ 决策类（负载甩载/档位路由/token 窗口）是**决策面与计数**，不是延迟。"
)

#: 回环地址集合（环境闸的白名单；不做 DNS 解析——解析结果本身可被 hosts 改写）
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

#: 模型窗口臂用的历史条数：24 > MAX_HISTORY_MESSAGES(20) ⇒ 条数窗口本身会裁 4 条，
#: token 预算臂再在其中裁到更短，两个窗口都被真的触发。
_HISTORY_MESSAGES = 24

#: 单条历史正文长度（汉字数）。取值理由：默认 ``CONTEXT_REAL_TOKEN_COUNT_ENABLED=False``
#: 时 ``count_tokens`` 是 ``len(text)//4``，400 字 ≈ 100 单位；24 条 ≈ 2400 单位，
#: 与 800 的预算形成「20 条 vs 约 8 条」的可见差异，且真 prompt 不至于把本机 8B
#: 的前缀填充拖成分钟级。
_HISTORY_CHARS = 400

#: token 窗口臂的预算取值。理由：与真实换行后 prompt 量级相比，800 落在
#: 「条数窗口(20) 与全历史(24) 之间」的量级，能同时证明两件事——预算生效、
#: 且它比条数窗口更紧（否则两条臂读数会相同，无法归因）。
_TOKEN_BUDGET_ON = 800


# ---------------------------------------------------------------------------
# 环境闸 + 通用小工具
# ---------------------------------------------------------------------------


def _resolve_endpoint(env_key: str, settings_attr: str) -> str:
    """取端点：环境变量优先（pydantic-settings 语义），否则回落 settings。"""
    raw = (os.environ.get(env_key) or "").strip()
    if raw:
        return raw
    from agentix.config import settings

    return str(getattr(settings, settings_attr, "") or "").strip()


def _assert_loopback(key: str, url: str) -> str:
    if not url:
        raise RuntimeError(
            f"环境闸拒绝出数：{key} 为空，无法确认端点在回环地址上。"
            "本脚本禁止任何外部 API，请显式指向本机端点后再跑"
        )
    host = (urlparse(url).hostname or "").strip().lower()
    if host not in _LOOPBACK_HOSTS:
        raise RuntimeError(
            f"环境闸拒绝出数：{key}={url!r} 解析到 host={host!r}，不是回环地址。"
            "本脚本禁止任何外部 API（商用端点会把提示词发出去）；"
            "请用环境变量把它覆盖成本机端点，例如 "
            "LLM_BASE_URL=http://127.0.0.1:1234/v1"
        )
    return url


def _gate_endpoints() -> Dict[str, str]:
    """启动闸：端点不在回环上就拒绝出数（唯一执行点，先于任何测量）。"""
    llm = _assert_loopback("LLM_BASE_URL", _resolve_endpoint("LLM_BASE_URL", "LLM_BASE_URL"))
    emb = _assert_loopback(
        "EMBEDDING_BASE_URL", _resolve_endpoint("EMBEDDING_BASE_URL", "EMBEDDING_BASE_URL")
    )
    from agentix.config import settings

    return {
        "llm_base_url": llm,
        "embedding_base_url": emb,
        "llm_model_requested": str(getattr(settings, "LLM_MODEL", "")),
        "embedding_model_requested": str(os.environ.get("EMBEDDING_MODEL", "")),
    }


@contextmanager
def _patched_settings(**values: Any) -> Iterator[None]:
    """临时改 Settings 属性并按原值还原（本脚本不改默认值，只在臂内注入）。"""
    from agentix.config import settings

    before = {key: getattr(settings, key) for key in values}
    try:
        for key, value in values.items():
            setattr(settings, key, value)
        yield
    finally:
        for key, value in before.items():
            setattr(settings, key, value)


def _metric_value(name: str, labels: Dict[str, str]) -> float:
    from prometheus_client import REGISTRY

    return float(REGISTRY.get_sample_value(name, labels) or 0.0)


def _admission_counts() -> Dict[str, float]:
    return {
        state: _metric_value("admission_decisions_total", {"state": state})
        for state in ("admitted", "queued", "rejected")
    }


def _emit(
    arm: str,
    state: str,
    *,
    repeats: int,
    touches_model: bool,
    temperature: Optional[float],
    results: Dict[str, Any],
    notes: Dict[str, Any],
    status: str = "completed",
    skip_reason: Optional[str] = None,
) -> Dict[str, Any]:
    """组装一份**带指纹**的结果 payload（落盘闸在 ``bench.results``）。"""
    target = f"{TARGET_NAME}.{arm}.{state}"
    fingerprint = collect_fingerprint(
        target=target,
        entry=ENTRY,
        repeats=repeats,
        touches_model=touches_model,
        temperature=temperature,
    )
    return {
        "fingerprint": fingerprint,
        "target": target,
        "data_class": "benchmark_measurement",
        "measurement_status": status,
        "skip_reason": skip_reason,
        "measurement_scope": MEASUREMENT_SCOPE,
        # 臂定义（arms）/ 复位策略 / 口径 caveat 必须随读数落盘：
        # 指纹里没有"这次把哪个 flag 设成了什么"，只有 results.notes 记得住。
        "results": {**results, "notes": notes},
    }


def _pooled(key: str, samples_ms: List[float], note: str) -> Dict[str, Any]:
    return {key: pooled_summary(samples_ms), "pooled_note": note}


# ---------------------------------------------------------------------------
# 臂 0：probe —— 模型能力定点探活（决定模型依赖臂可测与否）
# ---------------------------------------------------------------------------


def _http_json(
    url: str, payload: Optional[Dict[str, Any]] = None, *, timeout_s: float = 180.0
) -> Dict[str, Any]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:  # noqa: S310
            body = json.loads(response.read().decode("utf-8"))
        return {"ok": True, "body": body, "elapsed_ms": (time.perf_counter() - started) * 1000.0}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        return {
            "ok": False,
            "error": f"HTTP {exc.code}: {raw[:800]}",
            "elapsed_ms": (time.perf_counter() - started) * 1000.0,
        }
    except Exception as exc:  # noqa: BLE001 - 探活失败本身就是读数
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "elapsed_ms": (time.perf_counter() - started) * 1000.0,
        }


_PROBE_TOOL = {
    "type": "function",
    "function": {
        "name": "route",
        "description": "Return the routing decision for the user query",
        "parameters": {
            "type": "object",
            "properties": {
                "type": {
                    "type": "string",
                    "enum": ["kb-query", "general-query", "graphrag-query"],
                },
                "logic": {"type": "string"},
            },
            "required": ["type", "logic"],
        },
    },
}


def _probe_tool_calls(base_url: str, model: str) -> Dict[str, Any]:
    """结构化输出能力：OpenAI 兼容 ``tools`` 通道能否产出 ``tool_calls``。"""
    out = _http_json(
        base_url.rstrip("/") + "/chat/completions",
        {
            "model": model,
            "messages": [{"role": "user", "content": "刑法第264条规定了什么"}],
            "tools": [_PROBE_TOOL],
            "temperature": 0.0,
            "max_tokens": 512,
        },
    )
    if not out.get("ok"):
        return {"supported": False, "evidence": out}
    choice = (out["body"].get("choices") or [{}])[0]
    message = choice.get("message") or {}
    return {
        "supported": bool(message.get("tool_calls")),
        "finish_reason": choice.get("finish_reason"),
        "tool_calls": message.get("tool_calls"),
        "usage": out["body"].get("usage"),
        "elapsed_ms": round(out["elapsed_ms"], 1),
        "evidence": "POST /v1/chat/completions with tools(tool_choice=default auto)",
    }


def _probe_multi_turn(base_url: str, model: str) -> Dict[str, Any]:
    """多轮会话：带 3 条历史的请求能否正常返回（token 窗口臂的端到端前提）。"""
    out = _http_json(
        base_url.rstrip("/") + "/chat/completions",
        {
            "model": model,
            "messages": [
                {"role": "system", "content": "你是法律问答助手，回答不超过 20 字。"},
                {"role": "user", "content": "刑法第264条讲什么？"},
                {"role": "assistant", "content": "盗窃罪。"},
                {"role": "user", "content": "那第265条呢？"},
            ],
            "temperature": 0.0,
            "max_tokens": 256,
        },
    )
    if not out.get("ok"):
        return {"supported": False, "evidence": out}
    choice = (out["body"].get("choices") or [{}])[0]
    content = (choice.get("message") or {}).get("content")
    return {
        "supported": bool(content),
        "turn2_content_head": str(content)[:200],
        "finish_reason": choice.get("finish_reason"),
        "usage": out["body"].get("usage"),
        "elapsed_ms": round(out["elapsed_ms"], 1),
        "evidence": "POST /v1/chat/completions with system+3 history messages",
    }


def _probe_structured_output() -> Dict[str, Any]:
    """生产链路的 ``with_structured_output`` 通道（router 角色，真实节点）。"""
    import asyncio as _asyncio

    from langchain_core.messages import HumanMessage

    from agentix.application.agents import router as router_module
    from agentix.orchestration.langgraph.lg_states import AgentState

    async def _run() -> Dict[str, Any]:
        started = time.perf_counter()
        try:
            state = AgentState(messages=[HumanMessage(content="刑法第264条规定了什么")])
            result = await router_module.analyze_and_route_query(state, config={})
        except Exception as exc:  # noqa: BLE001 - 失败也是探活读数
            return {
                "supported": False,
                "error": f"{type(exc).__name__}: {exc}",
                "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 1),
            }
        router_obj = result["router"]
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        logic = str(getattr(router_obj, "logic", "") or "")
        return {
            "supported": not logic.startswith("fallback"),
            "router_type": str(getattr(router_obj, "type", "")),
            "logic": logic[:300],
            # 超时被吞进兜底：这条路返回的不是分类结果，而是启发式兜底。
            "fell_back": logic.startswith("fallback"),
            "elapsed_ms": round(elapsed_ms, 1),
            "evidence": "analyze_and_route_query(state, config={})（真实节点 + 真实分类器）",
        }

    return _asyncio.run(_run())


def arm_probe(repeats: int, samples: int, temperature: Optional[float]) -> List[Dict[str, Any]]:
    """一次定点探活：端点/结构化输出/多轮。结果直接决定模型依赖臂的可测性。"""
    endpoints = _gate_endpoints()
    base_url = endpoints["llm_base_url"]
    model = endpoints["llm_model_requested"]
    listing = _http_json(base_url.rstrip("/") + "/models", timeout_s=30.0)
    served = []
    if listing.get("ok"):
        served = [str(item.get("id")) for item in (listing["body"].get("data") or [])]
    probe = {
        "models_endpoint": {
            "ok": bool(listing.get("ok")),
            "served_model_ids": served,
            "error": listing.get("error"),
        },
        "tool_calls": _probe_tool_calls(base_url, model),
        "multi_turn": _probe_multi_turn(base_url, model),
        "structured_output_via_router_node": _probe_structured_output(),
    }
    structured_ok = bool(probe["structured_output_via_router_node"].get("supported"))
    return [
        _emit(
            "probe",
            "local_endpoint",
            repeats=repeats,
            touches_model=True,
            temperature=temperature,
            status="completed" if structured_ok else "skipped_no_environment",
            skip_reason=(
                None
                if structured_ok
                else "本机模型产不出可用结构化输出（详见 results.probe），"
                "依赖分类器的 off 臂读数无效"
            ),
            results={
                "probe": probe,
                "endpoints": endpoints,
                "quantile_source": QUANTILE_SOURCE,
            },
            notes={
                "what_is_measured": "本机 OpenAI 兼容端点的四项能力探活（非性能读数）",
                "no_reading_semantics": "本臂不产出任何时延声明；重复次数只用于指纹合规",
                "endpoint_does_not_validate_model_name": (
                    "本机 LM Studio 的 /v1/models 列出可用 id，但 /chat/completions "
                    "**不校验请求里的 model 名**——实际服务的模型可能不是请求名。"
                    "指纹里的 llm_model_requested 因此是**请求名**，不是实际服务模型名"
                ),
            },
        )
    ]


# ---------------------------------------------------------------------------
# 臂 1：准入排队（三态计数 + 排队等位时延）
# ---------------------------------------------------------------------------


def _decision_state(deltas: Dict[str, float]) -> str:
    """三态归档：以被测实现自己记的指标增量为准（不是 bench 猜的分支）。"""
    if deltas.get("rejected", 0.0) > 0:
        return "rejected"
    if deltas.get("queued", 0.0) > 0:
        return "queued"
    if deltas.get("admitted", 0.0) > 0:
        return "admitted"
    return "no_decision_recorded"


async def _admission_scenario(
    *,
    max_concurrent: Optional[int],
    max_queue: Optional[int],
    hold_s: float,
) -> Dict[str, Any]:
    """一次三态判定：先占满并发，再在满位下等位，全程读真指标增量。

    ``None`` = **不覆盖**臂级配置（``limit_unset`` 臂靠这个证明「只开 flag 不设
    上限」与 flag-off 零差异）；非 ``None`` = 本场景显式设该上限。
    """
    from agentix.config import settings
    from agentix.performance.admission import admission_wait, reset_admission_controller
    from agentix.runtime.control_plane import (
        AdmissionDenied,
        get_control_plane,
        reset_control_plane,
    )

    # 每样本复位：平面登记表与等位计数都不跨样本残留（最强隔离，见 notes）。
    reset_control_plane()
    reset_admission_controller()
    if max_concurrent is not None:
        settings.ADMISSION_MAX_CONCURRENT_RUNS = max_concurrent
    if max_queue is not None:
        settings.ADMISSION_MAX_QUEUE = max_queue

    plane = get_control_plane()
    # 占位 1 个 run：max=1 时即到顶，max<=0（不限）时永远到不了顶。
    holder = plane.spawn("bench_admission_holder")

    async def _release() -> None:
        await asyncio.sleep(hold_s)
        plane.finish(holder.run_id)

    release_task = asyncio.create_task(_release())
    before = _admission_counts()
    started = time.perf_counter()
    denied = ""
    try:
        await admission_wait(kind="bench")
    except AdmissionDenied as exc:
        denied = str(exc)
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    after = _admission_counts()
    await release_task
    deltas = {key: after[key] - before[key] for key in before}
    return {
        "decision_state": _decision_state(deltas),
        "denied_reason": denied,
        "elapsed_ms": elapsed_ms,
        "metric_deltas": deltas,
        "active_at_decision": 1,
        "scenario_max_concurrent": (
            int(settings.ADMISSION_MAX_CONCURRENT_RUNS) if max_concurrent is None
            else max_concurrent
        ),
        "scenario_max_queue": (
            int(settings.ADMISSION_MAX_QUEUE) if max_queue is None else max_queue
        ),
    }


async def _admission_samples(
    *, max_concurrent: Optional[int], max_queue: Optional[int], hold_s: float, samples: int
) -> List[Dict[str, Any]]:
    return [
        await _admission_scenario(
            max_concurrent=max_concurrent, max_queue=max_queue, hold_s=hold_s
        )
        for _ in range(samples)
    ]


def _admission_arm_state(
    state: str,
    *,
    enabled: bool,
    max_concurrent: int,
    max_queue: int,
    override_limits: bool,
    repeats: int,
    samples: int,
) -> Dict[str, Any]:
    # 场景名 → (并发上限覆盖值, 排队深度覆盖值)；override_limits=False 时全为 None
    # ⇒ 三个场景都跑在臂级配置上（这正是「只开 flag」的真实形态）。
    raw = {
        "under_capacity": (2, 4),
        "at_capacity_queue_enabled": (1, 4),
        "at_capacity_queue_disabled": (1, 0),
    }
    scenarios = {
        name: pair if override_limits else (None, None) for name, pair in raw.items()
    }
    rows: List[Dict[str, Any]] = []
    with _patched_settings(
        ADMISSION_CONTROL_ENABLED=enabled,
        ADMISSION_MAX_CONCURRENT_RUNS=max_concurrent,
        ADMISSION_MAX_QUEUE=max_queue,
        CONTROL_PLANE_MAX_CONCURRENCY=0,
        LOAD_SHEDDING_ENABLED=False,
    ):
        for repeat in range(repeats):
            for scenario, (concurrency, queue) in scenarios.items():
                for record in asyncio.run(
                    _admission_samples(
                        max_concurrent=concurrency,
                        max_queue=queue,
                        hold_s=0.10,
                        samples=samples,
                    )
                ):
                    rows.append({"repeat": repeat, "scenario": scenario, **record})

    queue_wait = [
        row["elapsed_ms"] for row in rows if row["scenario"] == "at_capacity_queue_enabled"
    ]
    by_scenario: Dict[str, Any] = {}
    for scenario in scenarios:
        subset = [row for row in rows if row["scenario"] == scenario]
        by_scenario[scenario] = {
            "decision_state_counts": dict(Counter(row["decision_state"] for row in subset)),
            "sample_count": len(subset),
            "elapsed_ms": pooled_summary([row["elapsed_ms"] for row in subset]),
            "metric_delta_totals": {
                key: sum(row["metric_deltas"][key] for row in subset)
                for key in ("admitted", "queued", "rejected")
            },
        }
    return _emit(
        "admission",
        state,
        repeats=repeats,
        touches_model=False,
        temperature=None,
        results={
            "flag_values": {
                "ADMISSION_CONTROL_ENABLED": enabled,
                "ADMISSION_MAX_CONCURRENT_RUNS": max_concurrent,
                "ADMISSION_MAX_QUEUE": max_queue,
                "CONTROL_PLANE_MAX_CONCURRENCY": 0,
            },
            "scenarios": by_scenario,
            **_pooled(
                "pooled_quantiles_ms",
                queue_wait,
                "at_capacity_queue_enabled 场景的等位墙钟（flag-off 臂该值为直通，"
                "接近于 0；两臂并池无意义——分栏见 scenarios）",
            ),
            "per_sample": rows,
            "quantile_source": QUANTILE_SOURCE,
        },
        notes={
            "what_is_measured": (
                "await admission_wait(kind='bench') 的三态结果（放行/排队/拒绝）与"
                "调用首尾墙钟；并发占位由 ControlPlane.spawn 的真实 active_count 承担"
            ),
            "arms": {
                "flag_off": "ADMISSION_CONTROL_ENABLED=false（默认值）",
                "flag_on": (
                    "ADMISSION_CONTROL_ENABLED=true 且**显式**设 "
                    "ADMISSION_MAX_CONCURRENT_RUNS=1/2、ADMISSION_MAX_QUEUE=0/4"
                ),
                "limit_unset": (
                    "ADMISSION_CONTROL_ENABLED=true 但两个上限都留 0（回落 "
                    "CONTROL_PLANE_MAX_CONCURRENCY=0=不限）——用于证明"
                    "「只开 flag 不设上限两臂零差异」"
                ),
            },
            "why_limits_are_explicit": (
                "ADMISSION_MAX_CONCURRENT_RUNS=0 会回落 CONTROL_PLANE_MAX_CONCURRENCY，"
                "两者皆 0 = 不限 ⇒ 只开 flag 时 controller 恒放行，两臂读数必然相同。"
                "所以 flag_on 臂必须显式给上限，否则测的是空开关"
            ),
            "decision_state_comes_from_the_implementation": (
                "三态归档读的是被测实现自己记的 admission_decisions_total 增量，"
                "不是 bench 猜的分支。因此 flag_off 臂恒为 no_decision_recorded ——"
                "这不是漏记，而是 flag-off 直通零记录（与 tests/test_p97 同口径）"
            ),
            "hold_s": 0.10,
            "reset_strategy": (
                "臂内每样本 reset_control_plane()+reset_admission_controller()；"
                "臂之间由「一次调用只跑一个 --arm」的进程边界隔开"
            ),
            "scope_caveat": (
                "不含真实 agent run 的执行时长：占位者是 bench 造的 run 记录，"
                "只让 active_count 到顶；不是线上吞吐结论"
            ),
        },
    )


def arm_admission(repeats: int, samples: int, temperature: Optional[float]) -> List[Dict[str, Any]]:
    out = [
        _admission_arm_state(
            "flag_off", enabled=False, max_concurrent=1, max_queue=4,
            override_limits=True, repeats=repeats, samples=samples,
        ),
        _admission_arm_state(
            "flag_on", enabled=True, max_concurrent=1, max_queue=4,
            override_limits=True, repeats=repeats, samples=samples,
        ),
        _admission_arm_state(
            "limit_unset", enabled=True, max_concurrent=0, max_queue=4,
            override_limits=False, repeats=repeats, samples=samples,
        ),
    ]
    _ = temperature
    return out


# ---------------------------------------------------------------------------
# 臂 2：重试预算
# ---------------------------------------------------------------------------


async def _retry_sample(exc_factory: Callable[[], BaseException], max_attempts: int) -> Dict[str, Any]:
    """一次 run_task：执行体恒抛注入异常，数真实现发起了几次尝试。"""
    from agentix.application.services.task_plan import TaskSpec
    from agentix.application.services.task_retry import run_task
    from agentix.reliability.retry_budget import reset_retry_budget

    reset_retry_budget()
    calls = {"n": 0}

    async def _runner(_spec: TaskSpec) -> Any:
        calls["n"] += 1
        raise exc_factory()

    started = time.perf_counter()
    outcome = await run_task(
        TaskSpec(name="bench-retry-task"),
        _runner,
        max_attempts=max_attempts,
        base_delay=0.01,
        max_delay=0.01,
        jitter=False,
    )
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    from agentix.reliability.retry_budget import current_retry_budget

    budget = current_retry_budget()
    return {
        "runner_invocations": calls["n"],
        "attempts": outcome.attempts,
        "elapsed_ms": elapsed_ms,
        "retryable": bool(outcome.retryable),
        "error_type": outcome.error_type,
        "truncated": bool(outcome.truncated),
        "budget_stats": budget.stats() if budget is not None else None,
    }


_RETRY_INJECTIONS: Dict[str, Callable[[], BaseException]] = {
    "transient_connection": lambda: ConnectionError("bench injected: connection reset by peer"),
    "transient_timeout": lambda: TimeoutError("bench injected: upstream read timeout"),
    "non_transient_401": lambda: ValueError("bench injected: 401 invalid api key"),
}


def _retry_arm_state(state: str, *, enabled: bool, repeats: int, samples: int) -> Dict[str, Any]:
    max_attempts = 4
    tokens = 2
    rows: List[Dict[str, Any]] = []
    with _patched_settings(
        RETRY_BUDGET_ENABLED=enabled,
        RETRY_BUDGET_TOKENS=tokens,
        TOOL_GUARDRAILS_ENABLED=False,
        RETRY_BACKOFF_ALIGNED=False,
    ):
        for repeat in range(repeats):
            for name, factory in _RETRY_INJECTIONS.items():
                for _ in range(samples):
                    record = asyncio.run(_retry_sample(factory, max_attempts))
                    rows.append({"repeat": repeat, "injection": name, **record})

    by_injection: Dict[str, Any] = {}
    for name in _RETRY_INJECTIONS:
        subset = [row for row in rows if row["injection"] == name]
        by_injection[name] = {
            "attempts": sorted({row["attempts"] for row in subset}),
            "runner_invocations": sorted({row["runner_invocations"] for row in subset}),
            "retryable": sorted({row["retryable"] for row in subset}),
            "truncated": sorted({row["truncated"] for row in subset}),
            "sample_count": len(subset),
            "elapsed_ms": pooled_summary([row["elapsed_ms"] for row in subset]),
            "budget_stats_first": subset[0]["budget_stats"],
        }
    transient = [
        row["elapsed_ms"] for row in rows if row["injection"] == "transient_connection"
    ]
    return _emit(
        "retry_budget",
        state,
        repeats=repeats,
        touches_model=False,
        temperature=None,
        results={
            "flag_values": {
                "RETRY_BUDGET_ENABLED": enabled,
                "RETRY_BUDGET_TOKENS": tokens,
                "max_attempts": max_attempts,
                "base_delay_s": 0.01,
                "max_delay_s": 0.01,
                "jitter": False,
            },
            "by_injection": by_injection,
            **_pooled(
                "pooled_quantiles_ms",
                transient,
                "transient_connection 注入下的 run_task 总墙钟（含自注入的 10ms 退避）",
            ),
            "per_sample": rows,
            "quantile_source": QUANTILE_SOURCE,
        },
        notes={
            "what_is_measured": (
                "run_task(spec, runner, max_attempts=4) 在「执行体恒抛注入异常」下"
                "真的发起了几次尝试、总墙钟、以及共享预算桶的消耗台账"
            ),
            "arms": {
                "flag_off": "RETRY_BUDGET_ENABLED=false（默认值）",
                "flag_on": "RETRY_BUDGET_ENABLED=true 且 RETRY_BUDGET_TOKENS=2",
            },
            "why_tokens_2": (
                "预算桶要能真的卡住重试才谈得上 A/B：max_attempts=4 最多 3 次重试，"
                "默认 6 枚 token 永远花不完 ⇒ 两臂零差异。取 2 ⇒ 第 3 次尝试前预算耗尽，"
                "差异才可见。这是**注入值**，不是默认值"
            ),
            "backoff_is_injected": (
                "base_delay/max_delay=0.01s + jitter=False 是为了让读数只反映"
                "尝试次数差，不反映退避形状；总墙钟因此**不是**线上退避时延"
            ),
            "non_transient_401": (
                "ValueError('... 401 ...') 命中 is_retryable 的不可重试组 ⇒ 两臂都只跑"
                "1 次尝试（差异应恰为 0）——这是对照组，证明预算闸不会把不可重试错误"
                "变成别的行为"
            ),
            "reset_strategy": "每样本 reset_retry_budget()；臂之间走独立进程",
            "scope_caveat": (
                "只覆盖 task 层（三层共享预算的最外层消费点）；tool/retrieval 两层的"
                "计数不在本臂，未实测"
            ),
        },
    )


def arm_retry_budget(
    repeats: int, samples: int, temperature: Optional[float]
) -> List[Dict[str, Any]]:
    out = [
        _retry_arm_state("flag_off", enabled=False, repeats=repeats, samples=samples),
        _retry_arm_state("flag_on", enabled=True, repeats=repeats, samples=samples),
    ]
    _ = temperature
    return out


# ---------------------------------------------------------------------------
# 臂 3：负载甩载（决策面，不是延迟）
# ---------------------------------------------------------------------------

#: lag 采样点：覆盖 0 与阶梯的每一档边界两侧（边界值本身取实现文档里的档位起点，
#: 这里只是**输入取值**，档位判据仍归被测实现——本文件不复制阈值表）。
_LAG_POINTS = (0.0, 0.05, 0.10, 0.24, 0.25, 0.49, 0.50, 0.99, 1.00, 1.99, 2.00, 5.00, 10.0)

#: should_shed 的观测类型：四个已登记后台类型 + 一个未登记类型（对照）。
_SHED_TASK_TYPES = (
    "evolution_sleep",
    "file_import",
    "conversation_summary",
    "memory_consolidation",
    "bench_unknown_type",
)


def _load_shedding_state(state: str, *, enabled: bool, repeats: int) -> Dict[str, Any]:
    from agentix.observability import metrics as metrics_module
    from agentix.performance.load_shedder import get_load_shedder, reset_load_shedder

    sweeps: List[Dict[str, Any]] = []
    with _patched_settings(
        LOAD_SHEDDING_ENABLED=enabled,
        ADMISSION_CONTROL_ENABLED=False,
        ADMISSION_MAX_QUEUE=0,
    ), _patch_impaired(lambda: 0):
        for _ in range(repeats):
            reset_load_shedder()
            metrics_module.record_event_loop_lag(0.0)
            rows = []
            for lag_s in _LAG_POINTS:
                metrics_module.record_event_loop_lag(lag_s)
                shedder = get_load_shedder()
                level = shedder.current_level()
                rows.append(
                    {
                        "lag_s": lag_s,
                        "level": level.name,
                        "should_shed": {
                            task_type: shedder.should_shed(task_type)
                            for task_type in _SHED_TASK_TYPES
                        },
                    }
                )
            sweeps.append({"lag_sweep": rows})

    # 下游熔断维度：注入受损下游数，lag 归零，只看该输入单独的贡献。
    downstream: List[Dict[str, Any]] = []
    with _patched_settings(LOAD_SHEDDING_ENABLED=enabled):
        metrics_module.record_event_loop_lag(0.0)
        for impaired in (0, 1, 2, 3):
            reset_load_shedder()
            with _patch_impaired(lambda n=impaired: n):
                downstream.append(
                    {"impaired_downstreams": impaired,
                     "level": get_load_shedder().current_level().name}
                )

    # 队列深度维度：真的把等位计数顶起来（不是伪造输入）。
    # 必须留在 flag 注入的作用域内——否则 current_level() 会因 flag 已还原而恒 L0。
    with _patched_settings(LOAD_SHEDDING_ENABLED=enabled):
        metrics_module.record_event_loop_lag(0.0)
        live_queue = asyncio.run(_live_queue_level(enabled=enabled))

    # L6 的「礼貌拒绝」在 admission 侧（should_shed 在 L6 恒 False）：lag=5s 时必须 429 路径。
    # 准入并发限两处皆 0（= 不限），因此这里唯一可能触发的拒绝来源就是 L6。
    with _patched_settings(
        LOAD_SHEDDING_ENABLED=enabled,
        ADMISSION_CONTROL_ENABLED=True,
        ADMISSION_MAX_CONCURRENT_RUNS=0,
        ADMISSION_MAX_QUEUE=0,
        CONTROL_PLANE_MAX_CONCURRENCY=0,
    ), _patch_impaired(lambda: 0):
        l6_probe = asyncio.run(_l6_admission_probe(enabled=enabled))
    reference = sweeps[0]["lag_sweep"]
    deterministic = all(sweep["lag_sweep"] == reference for sweep in sweeps)
    return _emit(
        "load_shedding",
        state,
        repeats=repeats,
        touches_model=False,
        temperature=None,
        results={
            "flag_values": {"LOAD_SHEDDING_ENABLED": enabled},
            "lag_sweep": reference,
            "repeat_deterministic": deterministic,
            "downstream_sweep": downstream,
            "live_queue_level": live_queue,
            "l6_admission_probe": l6_probe,
            "per_repeat_levels": [sweep["lag_sweep"] for sweep in sweeps],
            "quantile_source": QUANTILE_SOURCE,
        },
        notes={
            "what_is_measured": (
                "get_load_shedder().current_level() 与 should_shed(task_type) 的"
                "**决策输出**：lag/队列深度/下游熔断三个输入各自与组合下的级别与甩脱决定"
            ),
            "not_a_latency_reading": (
                "本臂不改任何延迟，也不测量延迟——它是决策面。级别阈值是实现的内部口径，"
                "本文件**不复制阈值表**，只记录「输入 → 观测到的级别」这张表"
            ),
            "arms": {
                "flag_off": "LOAD_SHEDDING_ENABLED=false（默认值）",
                "flag_on": "LOAD_SHEDDING_ENABLED=true",
            },
            "injection_points": (
                "lag 经 record_event_loop_lag()（生产同一入口）；受损下游经 "
                "load_shedder._impaired_downstreams 的 patch（与 "
                "tests/test_p97_runtime_control.py 同一注入点）；队列深度不伪造，"
                "由真的 admission 等位者顶起来"
            ),
            "l6_is_admission_side": (
                "L6 时 should_shed 恒 False（甩脱只到 L5），拒绝发生在 "
                "admission_wait 的 L6 分支（AdmissionDenied → HTTP 429）。因此 "
                "l6_admission_probe 用真的准入闸观测，不用 should_shed 代替"
            ),
            "determinism": "重复 5 轮，逐轮 lag 表逐格比对（repeat_deterministic）",
            "reset_strategy": (
                "每轮 reset_load_shedder() + record_event_loop_lag(0.0)"
                "（lag 是全局量，不清零会跨轮污染）"
            ),
        },
    )


@contextmanager
def _patch_impaired(value: Callable[[], int]) -> Iterator[None]:
    """替换被测模块的受损下游探针（p97 的同一注入点），不动被测判定。"""
    import agentix.performance.load_shedder as load_shedder

    original = load_shedder._impaired_downstreams  # noqa: SLF001 - 注入点即测试口径
    load_shedder._impaired_downstreams = value  # noqa: SLF001
    try:
        yield
    finally:
        load_shedder._impaired_downstreams = original  # noqa: SLF001


async def _live_queue_level(*, enabled: bool) -> Dict[str, Any]:
    """真的让一个等位者挂在队列上，再读级别（队列深度维度不靠伪造）。"""
    from agentix.config import settings
    from agentix.performance.admission import admission_wait, reset_admission_controller
    from agentix.performance.load_shedder import get_load_shedder, reset_load_shedder
    from agentix.runtime.control_plane import (
        get_control_plane,
        reset_control_plane,
    )

    reset_control_plane()
    reset_admission_controller()
    reset_load_shedder()
    before = (
        settings.ADMISSION_CONTROL_ENABLED,
        settings.ADMISSION_MAX_CONCURRENT_RUNS,
        settings.ADMISSION_MAX_QUEUE,
        settings.CONTROL_PLANE_MAX_CONCURRENCY,
    )
    settings.ADMISSION_CONTROL_ENABLED = True
    settings.ADMISSION_MAX_CONCURRENT_RUNS = 1
    settings.ADMISSION_MAX_QUEUE = 8
    settings.CONTROL_PLANE_MAX_CONCURRENCY = 0
    try:
        plane = get_control_plane()
        holder = plane.spawn("bench_queue_holder")
        waiter = asyncio.create_task(admission_wait(kind="bench"))
        await asyncio.sleep(0.05)
        from agentix.performance.admission import queue_capacity, queue_depth

        level = get_load_shedder().current_level()
        payload = {
            "queue_depth": queue_depth(),
            "queue_capacity": queue_capacity(),
            "level": level.name,
            "flag_on_in_this_probe": enabled,
        }
        plane.finish(holder.run_id)
        await waiter
        return payload
    finally:
        (
            settings.ADMISSION_CONTROL_ENABLED,
            settings.ADMISSION_MAX_CONCURRENT_RUNS,
            settings.ADMISSION_MAX_QUEUE,
            settings.CONTROL_PLANE_MAX_CONCURRENCY,
        ) = before
        reset_load_shedder()


async def _l6_admission_probe(*, enabled: bool) -> Dict[str, Any]:
    """L6 的「礼貌拒绝」在 admission 侧：lag=5s 时 ``admission_wait`` 必须走 429 路径。

    ``should_shed`` 在 L6 恒 False（拒绝由准入负责），所以这一格只能用真的准入闸观测，
    不能用 ``should_shed`` 代替。准入并发两处皆 0（不限）⇒ 此处唯一可能拒绝的来源是 L6。
    """
    from agentix.observability import metrics as metrics_module
    from agentix.performance.admission import (
        admission_wait,
        reset_admission_controller,
    )
    from agentix.performance.load_shedder import get_load_shedder, reset_load_shedder
    from agentix.runtime.control_plane import AdmissionDenied, reset_control_plane

    reset_control_plane()
    reset_admission_controller()
    reset_load_shedder()
    metrics_module.record_event_loop_lag(5.0)
    try:
        reason = ""
        try:
            await admission_wait(kind="bench_l6")
        except AdmissionDenied as exc:
            denied = True
            reason = str(exc)
        else:
            denied = False
        return {
            "flag_on_in_this_probe": enabled,
            "injected_lag_s": 5.0,
            "level": get_load_shedder().current_level().name,
            "admission_denied": denied,
            "denial_reason": reason,
            "admission_limits": "max_concurrent=0, max_queue=0（不限；拒绝只可能来自 L6）",
        }
    finally:
        metrics_module.record_event_loop_lag(0.0)
        reset_load_shedder()
        reset_admission_controller()
        reset_control_plane()


def arm_load_shedding(
    repeats: int, samples: int, temperature: Optional[float]
) -> List[Dict[str, Any]]:
    out = [
        _load_shedding_state("flag_off", enabled=False, repeats=repeats),
        _load_shedding_state("flag_on", enabled=True, repeats=repeats),
    ]
    _ = samples, temperature
    return out


# ---------------------------------------------------------------------------
# 臂 4：隔离舱（原语级）
# ---------------------------------------------------------------------------

#: 并发调用数与单次工作量：size=1 时 8 × 50ms ≈ 400ms，与 size=0 的 ≈50ms 分得开。
_BULKHEAD_CONCURRENCY = 8
_BULKHEAD_WORK_S = 0.05


async def _bulkhead_sample(size: int, concurrency: int) -> Dict[str, Any]:
    from agentix.reliability.bulkhead import (
        pooled_wait_for,
        pool_for_tool,
        reset_bulkheads,
    )

    reset_bulkheads()

    async def _work() -> str:
        await asyncio.sleep(_BULKHEAD_WORK_S)
        return "done"

    pool = pool_for_tool("search")
    started = time.perf_counter()
    results = await asyncio.gather(
        *[pooled_wait_for(_work(), 30.0, pool) for _ in range(concurrency)]
    )
    elapsed_ms = (time.perf_counter() - started) * 1000.0

    # 超时释放池位：size>0 时一次超时必须把池位还回来（否则下一轮直接饿死）。
    timeout_released: Optional[bool]
    if size > 0:
        timed_out = False
        try:
            await pooled_wait_for(asyncio.sleep(0.5), 0.01, pool_for_tool("search"))
        except asyncio.TimeoutError:
            timed_out = True
        after_release = await pooled_wait_for(_work(), 30.0, pool_for_tool("search"))
        timeout_released = bool(timed_out and after_release == "done")
    else:
        timeout_released = None
    return {
        "elapsed_ms": elapsed_ms,
        "completed": sum(1 for value in results if value == "done"),
        "pool_for_search": pool,
        "timeout_released_slot": timeout_released,
    }


@contextmanager
def _patched_bulkhead_size(size: int) -> Iterator[None]:
    with _patched_settings(
        BULKHEAD_WEB_SEARCH_SIZE=size,
        BULKHEAD_NEO4J_SIZE=size,
        BULKHEAD_MCP_SIZE=size,
        BULKHEAD_RETRIEVAL_SIZE=size,
    ):
        yield


def _bulkhead_state(state: str, *, size: int, repeats: int, samples: int) -> Dict[str, Any]:
    from agentix.reliability.bulkhead import pool_for_tool, reset_bulkheads

    rows: List[Dict[str, Any]] = []
    reset_bulkheads()
    with _patched_bulkhead_size(size):
        mapping = {
            name: pool_for_tool(name)
            for name in ("search", "cypher_query", "predefined_cypher", "not_a_tool")
        }
        for repeat in range(repeats):
            for _ in range(samples):
                rows.append({"repeat": repeat, **asyncio.run(_bulkhead_sample(size, _BULKHEAD_CONCURRENCY))})
    reset_bulkheads()
    return _emit(
        "bulkhead",
        state,
        repeats=repeats,
        touches_model=False,
        temperature=None,
        results={
            "flag_values": {
                "BULKHEAD_RETRIEVAL_SIZE": size,
                "BULKHEAD_NEO4J_SIZE": size,
                "BULKHEAD_MCP_SIZE": size,
                "BULKHEAD_WEB_SEARCH_SIZE": size,
            },
            "pool_for_tool": mapping,
            "concurrency": _BULKHEAD_CONCURRENCY,
            "work_per_call_s": _BULKHEAD_WORK_S,
            **_pooled(
                "pooled_quantiles_ms",
                [row["elapsed_ms"] for row in rows],
                f"{_BULKHEAD_CONCURRENCY} 个并发 pooled_wait_for 的总墙钟（size={size}）",
            ),
            "completed_counts": sorted({row["completed"] for row in rows}),
            "timeout_released_slot": sorted(
                {str(row["timeout_released_slot"]) for row in rows}
            ),
            "per_sample": rows,
            "quantile_source": QUANTILE_SOURCE,
        },
        notes={
            "what_is_measured": (
                "pooled_wait_for(coro, 30s, pool_for_tool('search')) 在 "
                f"{_BULKHEAD_CONCURRENCY} 路并发下的总墙钟 + 池位超时是否归还"
            ),
            "arms": {
                "size_0_unlimited": "四个 BULKHEAD_*_SIZE 全为 0（默认值）= 不限",
                "size_1_serialized": "四个 BULKHEAD_*_SIZE 全为 1（注入值）",
            },
            "why_all_four": (
                "只改 web_search 也能测，但四个键同值可让 pool_for_tool 的映射表整表可比；"
                "注入值不是默认值"
            ),
            "reset_strategy": "每样本 reset_bulkheads()（信号量按尺寸惰性构建，尺寸变了必须重建）",
            "scope_caveat": (
                "端到端不可测：Neo4j / MCP stdio / 外搜都需要外部服务，本机没有 ⇒ "
                "只给原语级读数（同一 semaphore 语义），**不得**读成那三个下游的线上延迟"
            ),
        },
    )


def arm_bulkhead(
    repeats: int, samples: int, temperature: Optional[float]
) -> List[Dict[str, Any]]:
    out = [
        _bulkhead_state("size_0_unlimited", size=0, repeats=repeats, samples=samples),
        _bulkhead_state("size_1_serialized", size=1, repeats=repeats, samples=samples),
    ]
    _ = temperature
    return out


# ---------------------------------------------------------------------------
# 臂 5：单飞合流（含 embedding HTTP 往返）
# ---------------------------------------------------------------------------

#: 同参并发数：与 RETRIEVAL 工具扇出的量级同（8 路同参 miss 是缓存击穿的典型形态）。
_SINGLEFLIGHT_CONCURRENCY = 8

#: 索引：优先用仓库自带的 demo 索引（1024 维、4865 文档、与本地 bge-m3 同维）。
_SINGLEFLIGHT_INDEX = "data/demo/indexes/legal-demo"


class _CountingShell:
    """只计次的壳：包住真实现，转发全部参数与返回值（不复制被测逻辑）。"""

    def __init__(self, real: Callable[..., Any]) -> None:
        self._real = real
        self.calls = 0

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        return self._real(*args, **kwargs)


def _prepare_singleflight_fixture() -> Tuple[Any, str, str]:
    """建一次 vectorstore 并预热（构造成本不进样本），返回 (vs, 索引路径, 后端)。"""
    from agentix.retrieval.infra.vectorstore.retriever_cache import get_or_build_retriever
    from agentix.retrieval.settings import settings as retrieval_settings

    index_path = (_REPO_ROOT + "/" + _SINGLEFLIGHT_INDEX).replace("\\", "/")
    if not (Path(index_path) / "index.faiss").is_file():
        raise FileNotFoundError(
            f"{index_path} 下没有 index.faiss——singleflight 臂需要一个可加载的 FAISS 索引"
        )
    backend = str(getattr(retrieval_settings, "VECTOR_STORE_BACKEND", "faiss")).lower()
    if backend != "faiss":
        raise RuntimeError(f"VECTOR_STORE_BACKEND={backend}：本臂只测 faiss 档")
    _retriever, vectorstore = get_or_build_retriever(index_path)
    store = getattr(vectorstore, "store", None)
    if store is None or not hasattr(store, "_get_embeddings"):
        raise RuntimeError(f"vectorstore 形态异常，无法定位嵌入客户端：{vectorstore!r}")
    embeddings = store._get_embeddings()  # noqa: SLF001 - 预热：让嵌入客户端在采样前就建好
    # 再预热一次**真嵌入往返**：首次请求含模型加载/连接建立，进样本会把尾部拉爆
    # （实测出现过 3.8s 的单点），那一段属于进程启动成本、不属于被测对象。
    embeddings.embed_query("bench singleflight warmup")
    return vectorstore, index_path, backend


async def _singleflight_sample(
    vectorstore: Any,
    *,
    question: str,
    concurrency: int,
) -> Dict[str, Any]:
    """一次同参并发检索：数下游检索调用次数 + 嵌入调用次数 + 总墙钟。"""
    import agentix.retrieval.usecases.chat_service as retrieval_chat_service
    from agentix.infrastructure.cache.singleflight import reset_singleflight

    reset_singleflight()
    store = vectorstore.store
    embeddings = store._get_embeddings()  # noqa: SLF001 - 同一预热过的实例
    embed_shell = _CountingShell(embeddings.embed_query)
    real_embed = embeddings.embed_query
    embeddings.embed_query = embed_shell  # type: ignore[method-assign]
    retrieval_shell = _CountingShell(retrieval_chat_service.retrieve_docs_with_retry)
    original_retrieval = retrieval_chat_service.retrieve_docs_with_retry
    retrieval_chat_service.retrieve_docs_with_retry = retrieval_shell  # type: ignore[assignment]
    try:
        started = time.perf_counter()
        results = await asyncio.gather(
            *[
                retrieval_chat_service.retrieve_scoped_docs_cached(
                    kb_id="bench-kb",
                    question=question,
                    search_k=5,
                    metadata_filter=None,
                    vectorstore=vectorstore,
                    cache_redis=None,
                    artifact_id="bench-artifact",
                    scope_token="bench-scope",
                )
                for _ in range(concurrency)
            ]
        )
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        downstream = retrieval_shell.calls
        embed_calls = embed_shell.calls
    finally:
        embeddings.embed_query = real_embed  # type: ignore[method-assign]
        retrieval_chat_service.retrieve_docs_with_retry = original_retrieval  # type: ignore[assignment]
    doc_counts = [len(docs or []) for docs in results]
    return {
        "elapsed_ms": elapsed_ms,
        "downstream_retrievals": downstream,
        "embedding_calls": embed_calls,
        "doc_counts": doc_counts,
        "all_callers_same_doc_count": len(set(doc_counts)) == 1,
    }


def _singleflight_state(
    state: str, *, enabled: bool, repeats: int, samples: int
) -> Dict[str, Any]:
    from agentix.retrieval.settings import settings as retrieval_settings

    guards = {
        "RETRIEVAL_ADAPTIVE_ENABLED": bool(retrieval_settings.RETRIEVAL_ADAPTIVE_ENABLED),
        "RETRIEVAL_HYBRID_ENABLED": bool(retrieval_settings.RETRIEVAL_HYBRID_ENABLED),
        "RETRIEVAL_QUERY_REWRITE_ENABLED": bool(
            retrieval_settings.RETRIEVAL_QUERY_REWRITE_ENABLED
        ),
        "RETRIEVAL_HYDE_ENABLED": bool(retrieval_settings.RETRIEVAL_HYDE_ENABLED),
        "SEMANTIC_CACHE_ENABLED": bool(retrieval_settings.SEMANTIC_CACHE_ENABLED),
    }
    if any(guards.values()):
        raise RuntimeError(
            f"singleflight 臂的前置不成立：级联/混合/改写/HyDE/语义缓存必须全关，"
            f"实际 {guards}——读数会被别的召回链污染"
        )

    vectorstore, index_path, backend = _prepare_singleflight_fixture()
    rows: List[Dict[str, Any]] = []
    original = retrieval_settings.RETRIEVAL_SINGLEFLIGHT_ENABLED
    retrieval_settings.RETRIEVAL_SINGLEFLIGHT_ENABLED = enabled
    try:
        for repeat in range(repeats):
            for index in range(samples):
                # 每个样本换一条**新问法**，且带臂名：默认开着的嵌入缓存按文本命中，
                # 同一句在两臂之间会共享缓存（第二臂就零 HTTP 往返了）。
                # 文本唯一化后，两臂各自都真的付一次嵌入往返——读数才可比。
                question = (
                    f"合同违约的责任承担方式（bench {state} 样本 {repeat}-{index}）"
                )
                rows.append(
                    {
                        "repeat": repeat,
                        "sample": index,
                        "question": question,
                        **asyncio.run(
                            _singleflight_sample(
                                vectorstore,
                                question=question,
                                concurrency=_SINGLEFLIGHT_CONCURRENCY,
                            )
                        ),
                    }
                )
    finally:
        retrieval_settings.RETRIEVAL_SINGLEFLIGHT_ENABLED = original

    downstream = sorted({row["downstream_retrievals"] for row in rows})
    expected = 1 if enabled else _SINGLEFLIGHT_CONCURRENCY
    if downstream != [expected]:
        raise RuntimeError(
            f"前置不成立：flag={enabled} 时 {_SINGLEFLIGHT_CONCURRENCY} 路同参并发应恰好 "
            f"{expected} 次下游检索，实际 {downstream}——读数作废"
        )
    if any(min(row["doc_counts"]) <= 0 for row in rows):
        raise RuntimeError("有样本召回 0 条文档：检索本身没走通，读数作废")
    return _emit(
        "singleflight",
        state,
        repeats=repeats,
        touches_model=False,
        temperature=None,
        results={
            "flag_values": {"RETRIEVAL_SINGLEFLIGHT_ENABLED": enabled},
            "concurrency": _SINGLEFLIGHT_CONCURRENCY,
            "index_path": index_path,
            "vector_backend": backend,
            "downstream_retrievals": downstream,
            "embedding_calls": sorted({row["embedding_calls"] for row in rows}),
            "all_callers_same_doc_count": sorted(
                {row["all_callers_same_doc_count"] for row in rows}
            ),
            **_pooled(
                "pooled_quantiles_ms",
                [row["elapsed_ms"] for row in rows],
                f"{_SINGLEFLIGHT_CONCURRENCY} 路同参并发的总墙钟（含嵌入 HTTP 往返）",
            ),
            "per_sample": rows,
            "quantile_source": QUANTILE_SOURCE,
        },
        notes={
            "what_is_measured": (
                f"retrieve_scoped_docs_cached(...) 被 {_SINGLEFLIGHT_CONCURRENCY} 路"
                "同参并发调用时的总墙钟 + 真下游检索调用次数（计数壳只计数，不复制逻辑）"
            ),
            "arms": {
                "flag_off": "RETRIEVAL_SINGLEFLIGHT_ENABLED=false（默认值）",
                "flag_on": "RETRIEVAL_SINGLEFLIGHT_ENABLED=true（注入值）",
            },
            "reading_includes_embedding_http": (
                "读数**含** /v1/embeddings 的 HTTP 往返：每路 miss 先算 query 向量再查 "
                "FAISS。所以它既是合流收益，也是本机嵌入端点往返的混合量"
            ),
            "fresh_question_per_sample": (
                "每个样本换一条新问法，且**带臂名**：EMBEDDING_CACHE_ENABLED 默认开着，"
                "复用同一句会让后续样本（以及第二臂）零 HTTP 往返，读数就不再含嵌入往返。"
                "代价：两臂的文本不同构但同长同结构，比较的是合流行为、不是文本差异"
            ),
            "warmup": (
                "采样前预热两次：建 vectorstore 一次、真 embed_query 一次。"
                "首次嵌入请求含模型加载（实测单点 3.8s），属进程启动成本，不进样本"
            ),
            "reset_strategy": (
                "每样本 reset_singleflight()（在飞表清空），计数壳用 patch 进出严格配对"
            ),
            "scope_caveat": (
                "不含 rerank（本机 /v1/rerank 不可用，RERANK 相关 flag 默认关）；"
                "不含 Redis 缓存层（cache_redis=None）；索引是 demo 规模（4865 文档），"
                "真实索引越大 FAISS 检索占比越高"
            ),
        },
    )


def arm_singleflight(
    repeats: int, samples: int, temperature: Optional[float]
) -> List[Dict[str, Any]]:
    out = [
        _singleflight_state("flag_off", enabled=False, repeats=repeats, samples=samples),
        _singleflight_state("flag_on", enabled=True, repeats=repeats, samples=samples),
    ]
    _ = temperature
    return out


# ---------------------------------------------------------------------------
# 臂 6：档位路由（零网络，只证明换档选择）
# ---------------------------------------------------------------------------


def _tier_routing_state(state: str, *, enabled: bool, repeats: int) -> Dict[str, Any]:
    from agentix.application.agents.llm_factory import DEFAULT_PROFILES, get_llm_client

    roles = ("router", "planner", "general")
    observed: Dict[str, Any] = {}
    with _patched_settings(
        LLM_TIER_ROUTING_ENABLED=enabled,
        LLM_ROLE_TIER_MAP="router=fast,planner=reasoning",
        LLM_MODEL_FAST="bench-tier-fast",
        LLM_MODEL_STANDARD="bench-tier-standard",
        LLM_MODEL_REASONING="bench-tier-reasoning",
    ):
        stable: List[Dict[str, str]] = []
        for _ in range(repeats):
            observed = {
                role: get_llm_client(role=role).model_name  # type: ignore[arg-type]
                for role in roles
            }
            stable.append(dict(observed))
    return _emit(
        "tier_routing",
        state,
        repeats=repeats,
        touches_model=False,
        temperature=None,
        results={
            "flag_values": {
                "LLM_TIER_ROUTING_ENABLED": enabled,
                "LLM_ROLE_TIER_MAP": "router=fast,planner=reasoning",
                "LLM_MODEL_FAST": "bench-tier-fast",
                "LLM_MODEL_STANDARD": "bench-tier-standard",
                "LLM_MODEL_REASONING": "bench-tier-reasoning",
            },
            "selected_model_by_role": observed,
            "baseline_profile_model_by_role": {
                role: DEFAULT_PROFILES[role].model  # type: ignore[index]
                for role in roles
            },
            "repeat_identical": all(row == stable[0] for row in stable),
            "per_repeat": stable,
            "quantile_source": QUANTILE_SOURCE,
        },
        notes={
            "what_is_measured": (
                "get_llm_client(role=...).model_name —— 角色到档位的**选择结果**"
            ),
            "zero_network": (
                "只构造客户端、不 ainvoke（零 HTTP）。因此它**只证明换档选择**，"
                "不证明时延、不证明 token、不证明质量"
            ),
            "arms": {
                "flag_off": "LLM_TIER_ROUTING_ENABLED=false（默认值）",
                "flag_on": "LLM_TIER_ROUTING_ENABLED=true + 上表映射与三个档位模型名",
            },
            "tier_models_are_injected": (
                "三个 LLM_MODEL_* 填的是**哨兵名**（bench-tier-*），不指向任何真实模型——"
                "本臂一次请求都不发，嵌入名只为让「换了档」这件事可判读"
            ),
            "sentinel_temperature": (
                "get_llm_client 默认 temperature=0.7 触发 profile 温度语义；"
                "本臂不采样，温度不参与任何读数"
            ),
        },
    )


def arm_tier_routing(
    repeats: int, samples: int, temperature: Optional[float]
) -> List[Dict[str, Any]]:
    out = [
        _tier_routing_state("flag_off", enabled=False, repeats=repeats),
        _tier_routing_state("flag_on", enabled=True, repeats=repeats),
    ]
    _ = samples, temperature
    return out


# ---------------------------------------------------------------------------
# 臂 7：token 级历史窗口（LG_TOKEN_HISTORY_BUDGET）
# ---------------------------------------------------------------------------


def _build_history() -> List[Any]:
    """确定性历史：24 条 human/ai 交替，正文长度固定（不含随机、不含时间戳）。"""
    from langchain_core.messages import AIMessage, HumanMessage

    filler = "合同违约责任的构成要件包括违约行为、损害事实与因果关系。"
    body = (filler * ((_HISTORY_CHARS // len(filler)) + 1))[:_HISTORY_CHARS]
    messages: List[Any] = []
    for index in range(_HISTORY_MESSAGES):
        content = f"第{index + 1}轮：{body}"
        cls = HumanMessage if index % 2 == 0 else AIMessage
        messages.append(cls(content=content, id=f"bench-msg-{index}"))
    return messages


def _apply_window(messages: List[Any]) -> Dict[str, Any]:
    """跑真实窗口节点，返回「剩下多少条 / 多少 token」以及节点交回的删除集。

    两个实参**一律从 settings 读**，与生产接线同形
    （``agentix/orchestration/langgraph/lg_builder.py:481-484``）。臂值由调用方经
    ``_patched_settings(LG_TOKEN_HISTORY_BUDGET=...)`` 注入 Settings 后再进来——
    本函数**不接收字面量预算**，否则量到的是「参数注入」而不是「配置生效」，
    读数就不能代表生产路径（bench 铁律 1：走原接口，不复制逻辑）。
    """
    from langgraph.graph.message import add_messages

    from agentix.agent.context.token_count import count_tokens
    from agentix.config import settings
    from agentix.orchestration.langgraph.lg_builder import create_message_window_node
    from agentix.orchestration.langgraph.lg_states import AgentState

    node = create_message_window_node(
        settings.MAX_HISTORY_MESSAGES,
        getattr(settings, "LG_TOKEN_HISTORY_BUDGET", 0),
    )
    update = asyncio.run(node(AgentState(messages=list(messages))))
    victims = list(update.get("messages") or [])
    remaining = add_messages(list(messages), victims)

    def _text(message: Any) -> str:
        content = getattr(message, "content", "")
        return content if isinstance(content, str) else str(content)

    return {
        "removed_count": len(victims),
        "remaining_count": len(remaining),
        "remaining_tokens_len_div4": sum(count_tokens(_text(m)) for m in remaining),
        "full_history_tokens_len_div4": sum(count_tokens(_text(m)) for m in messages),
        "remaining": remaining,
    }


def _token_window_state(
    state: str, *, token_budget: int, repeats: int, with_model: bool
) -> Dict[str, Any]:
    from agentix.agent.context.token_count import count_tokens
    from agentix.config import settings
    from agentix.application.agents.llm_factory import get_llm_client
    from langchain_core.messages import SystemMessage

    history = _build_history()
    rows: List[Dict[str, Any]] = []
    #: 每轮在 patched 块里**从 settings 读到的**生效值（不是传进来的字面量）。
    effective_budget: List[int] = []
    effective_max_messages: List[int] = []
    for _ in range(repeats):
        # 臂值只在这一块内注入，出块即还原 ⇒ 不泄漏到后续轮次与后续臂。
        with _patched_settings(LG_TOKEN_HISTORY_BUDGET=token_budget):
            effective_budget.append(int(getattr(settings, "LG_TOKEN_HISTORY_BUDGET", 0)))
            effective_max_messages.append(int(settings.MAX_HISTORY_MESSAGES))
            window = _apply_window(history)
        remaining = window.pop("remaining")
        row: Dict[str, Any] = dict(window)
        if with_model:
            messages = [SystemMessage(content="用一句话回答，不超过 20 字。")] + list(remaining)
            client = get_llm_client(temperature=0.0, role="general")
            client.max_tokens = 64
            started = time.perf_counter()
            try:
                response = asyncio.run(client.ainvoke(messages))
                usage = dict(getattr(response, "usage_metadata", None) or {})
                row.update(
                    {
                        "model_call_ok": True,
                        "model_elapsed_ms": (time.perf_counter() - started) * 1000.0,
                        "prompt_tokens": usage.get("input_tokens"),
                        "output_tokens": usage.get("output_tokens"),
                    }
                )
            except Exception as exc:  # noqa: BLE001 - 失败如实登记，不补假数
                row.update(
                    {
                        "model_call_ok": False,
                        "model_elapsed_ms": (time.perf_counter() - started) * 1000.0,
                        "model_error": f"{type(exc).__name__}: {exc}",
                        "prompt_tokens": None,
                        "output_tokens": None,
                    }
                )
        rows.append(row)

    ok_rows = [row for row in rows if row.get("model_call_ok")]
    prompt_tokens = [int(row["prompt_tokens"]) for row in ok_rows if row.get("prompt_tokens")]
    # 生效配置必须在 5 轮间一致：不一致说明 _patched_settings 的注入/还原不对称，
    # 那样的读数无法归因到预算，直接判失败，不留一份看着像成功的文件。
    if len(set(effective_budget)) != 1 or len(set(effective_max_messages)) != 1:
        raise RuntimeError(
            "token 窗口臂的生效配置在重复轮之间不稳定："
            f"LG_TOKEN_HISTORY_BUDGET={effective_budget}、"
            f"MAX_HISTORY_MESSAGES={effective_max_messages}"
            "（臂内注入与还原不对称 ⇒ 读数作废）"
        )
    status = "completed"
    skip_reason = None
    if with_model and not ok_rows:
        status = "skipped_no_environment"
        skip_reason = (
            "端到端模型读数未取到：全部 "
            f"{len(rows)} 次 ainvoke 失败（原因见 results.per_repeat[].model_error）"
        )
    return _emit(
        "token_window",
        state,
        repeats=repeats,
        touches_model=with_model,
        temperature=0.0 if with_model else None,
        status=status,
        skip_reason=skip_reason,
        results={
            # 记的是「settings 实际被读到的值」，不是调用方传的字面量。
            "flag_values": {"LG_TOKEN_HISTORY_BUDGET": effective_budget[-1]},
            "history": {
                "message_count": _HISTORY_MESSAGES,
                "chars_per_message": _HISTORY_CHARS,
                "token_unit": (
                    "count_tokens：CONTEXT_REAL_TOKEN_COUNT_ENABLED="
                    f"{bool(getattr(settings, 'CONTEXT_REAL_TOKEN_COUNT_ENABLED', False))} "
                    "⇒ 默认是 len(text)//4（不是真分词器口径）"
                ),
            },
            "window": {
                # 节点拿到的是这两个 settings 值（不是调用方字面量）。
                "max_history_messages": effective_max_messages[0],
                "removed_count": sorted({row["removed_count"] for row in rows}),
                "remaining_count": sorted({row["remaining_count"] for row in rows}),
                "remaining_tokens_len_div4": sorted(
                    {row["remaining_tokens_len_div4"] for row in rows}
                ),
                "full_history_tokens_len_div4": rows[0]["full_history_tokens_len_div4"],
            },
            "model_prompt_tokens": sorted(set(prompt_tokens)),
            "model_prompt_tokens_all_repeats": prompt_tokens,
            "model_calls_ok": len(ok_rows),
            "per_repeat": rows,
            "quantile_source": QUANTILE_SOURCE,
        },
        notes={
            "what_is_measured": (
                "create_message_window_node(MAX_HISTORY_MESSAGES, LG_TOKEN_HISTORY_BUDGET) "
                "对一段 24 条确定性历史裁掉几条、剩几条、剩多少 token；"
                "model 子臂再把裁后历史真的发给模型记 usage.prompt_tokens"
            ),
            "arms": {
                "budget_0_off": "LG_TOKEN_HISTORY_BUDGET=0（默认值）= 只做条数窗口",
                "budget_800_on": (
                    f"LG_TOKEN_HISTORY_BUDGET={_TOKEN_BUDGET_ON}"
                    "（臂内 _patched_settings 注入，出块即还原）"
                ),
            },
            "budget_source": (
                "臂值经 `_patched_settings(LG_TOKEN_HISTORY_BUDGET=...)` 注入 Settings、"
                "退出即还原（生产接线同源："
                "`agentix/orchestration/langgraph/lg_builder.py:481-484` 也是从 "
                "`settings.MAX_HISTORY_MESSAGES` 与 `settings.LG_TOKEN_HISTORY_BUDGET` 取实参）；"
                "`_apply_window` 不接收字面量预算，`results.flag_values` 记的是 settings "
                "实际被读到的值"
            ),
            "why_800": (
                "默认 count_tokens 是 len//4，24 条 × 400 字 ≈ 2400 单位；"
                "800 落在「条数窗口(20 条 ≈ 2000 单位) 之下、单条之上」，"
                "于是两个窗口的作用不会互相掩盖"
            ),
            "not_end_to_end_for_pure_window": (
                "window 段（removed/remaining/token 数）是**纯函数路径**：真节点 + 真 reducer，"
                "但跳过了图编排与真实会话历史，**不是**端到端读数。"
                "端到端只由 model 子臂的 usage.prompt_tokens 承担"
            ),
            "token_unit_mismatch": (
                "窗口内部按 count_tokens（默认 len//4）记账，而模型报告的是自家分词器的 "
                "prompt_tokens ⇒ 两个数**不可互相换算**，报告里分栏列，不合并"
            ),
            "model_call_params": (
                "temperature=0.0、max_tokens=64（只为压短解码；本臂只读 prompt_tokens）"
            ),
            "reset_strategy": "无进程级单例参与；每轮重建节点闭包，历史对象只读不改",
        },
    )


def arm_token_window(
    repeats: int, samples: int, temperature: Optional[float]
) -> List[Dict[str, Any]]:
    _ = samples, temperature
    return [
        _token_window_state("budget_0_off", token_budget=0, repeats=repeats, with_model=True),
        _token_window_state(
            "budget_800_on", token_budget=_TOKEN_BUDGET_ON, repeats=repeats, with_model=True
        ),
    ]


# ---------------------------------------------------------------------------
# 臂 8：fast-path（on 臂零模型可测；off 臂依赖分类器）
# ---------------------------------------------------------------------------

#: 命中面题集（on 臂应短路）与未命中面题集（on 臂仍要调模型 ⇒ 不主张零成本）。
_FASTPATH_HIT_QUESTIONS = ("你好", "刑法第264条规定了什么")
_FASTPATH_MISS_QUESTIONS = ("刑法第264条为什么和第265条区别", "帮我写一份房屋租赁合同")


def _fastpath_on_state(repeats: int) -> Dict[str, Any]:
    """on 臂：哨兵替换 ``get_llm_client``，命中即零模型构造（p94 同一手法）。"""
    from langchain_core.messages import HumanMessage

    from agentix.application.agents import router as router_module
    from agentix.config import settings
    from agentix.orchestration.langgraph.lg_states import AgentState

    def _boom(**_kwargs: Any) -> Any:
        raise AssertionError("fast-path 未短路：调到了模型构造")

    rows: List[Dict[str, Any]] = []
    original = router_module.get_llm_client
    with _patched_settings(ROUTING_FASTPATH_ENABLED=True, ROUTING_CACHE_ENABLED=False):
        router_module.get_llm_client = _boom  # type: ignore[assignment]
        try:
            for repeat in range(repeats):
                for question in _FASTPATH_HIT_QUESTIONS + _FASTPATH_MISS_QUESTIONS:
                    started = time.perf_counter()
                    hit = True
                    router_type = ""
                    logic = ""
                    try:
                        result = asyncio.run(
                            router_module.analyze_and_route_query(
                                AgentState(messages=[HumanMessage(content=question)]), config={}
                            )
                        )
                        router_obj = result["router"]
                        router_type = str(getattr(router_obj, "type", ""))
                        logic = str(getattr(router_obj, "logic", ""))[:120]
                    except AssertionError:
                        hit = False
                    rows.append(
                        {
                            "repeat": repeat,
                            "question": question,
                            "fastpath_hit": hit,
                            "router_type": router_type,
                            "logic": logic,
                            "elapsed_ms": (time.perf_counter() - started) * 1000.0,
                            "expected_hit": question in _FASTPATH_HIT_QUESTIONS,
                        }
                    )
        finally:
            router_module.get_llm_client = original  # type: ignore[assignment]
    mismatched = [row["question"] for row in rows if row["fastpath_hit"] != row["expected_hit"]]
    if mismatched:
        raise RuntimeError(f"fast-path 命中面与预期不符：{sorted(set(mismatched))}——读数作废")
    hit_rows = [row for row in rows if row["fastpath_hit"]]
    return _emit(
        "fastpath",
        "flag_on",
        repeats=repeats,
        touches_model=False,
        temperature=None,
        results={
            "flag_values": {
                "ROUTING_FASTPATH_ENABLED": True,
                "ROUTING_CACHE_ENABLED": False,
                "ablation_router_enabled": _ablation_router_enabled(),
            },
            "questions": {
                "hit_face": list(_FASTPATH_HIT_QUESTIONS),
                "miss_face": list(_FASTPATH_MISS_QUESTIONS),
            },
            **_pooled(
                "pooled_quantiles_ms",
                [row["elapsed_ms"] for row in hit_rows],
                "命中面单次 analyze_and_route_query 的墙钟（on 臂：零模型构造）",
            ),
            "hit_face_router_types": sorted(
                {(row["question"], row["router_type"]) for row in hit_rows}
            ),
            "model_constructions": 0,
            "per_sample": rows,
            "quantile_source": QUANTILE_SOURCE,
        },
        notes={
            "what_is_measured": (
                "ROUTING_FASTPATH_ENABLED=true 时 analyze_and_route_query 在命中面"
                "是否短路（哨兵替换 get_llm_client：命中 ⇒ 哨兵不被触发 ⇒ 零模型构造）"
            ),
            "arms": {
                "flag_on": "ROUTING_FASTPATH_ENABLED=true（注入值）",
                "flag_off": "见同名臂的 flag_off 子臂（需要真分类器）",
            },
            "zero_model_claim_scope": (
                "只对**命中面**题集主张零模型调用；未命中面在 on 臂仍会走到分类器"
                "（哨兵被触发即证明），本文件不对未命中面主张任何零成本"
            ),
            "reset_strategy": "无进程级单例参与；哨兵在 finally 里还原",
            "scope_caveat": (
                "墙钟是「纯函数短路」的代价，不是线上路由延迟——后者含分类器往返，"
                "见 flag_off 子臂的读数与其超时占比"
            ),
        },
    )


def _ablation_router_enabled() -> bool:
    try:
        from agentix.domain.policies import ablation

        return bool(ablation.is_enabled("router_enabled"))
    except Exception:  # noqa: BLE001 - 消融开关读不到按默认（开）处理
        return True


def _fastpath_off_state(repeats: int, temperature: Optional[float]) -> Dict[str, Any]:
    """off 臂：真分类器（``with_structured_output``）。本机产不出就如实登记不可测。"""
    from langchain_core.messages import HumanMessage

    from agentix.application.agents import router as router_module
    from agentix.orchestration.langgraph.lg_states import AgentState

    counters = {"constructions": 0}
    original = router_module.get_llm_client

    def _counting(*args: Any, **kwargs: Any) -> Any:
        counters["constructions"] += 1
        return original(*args, **kwargs)

    rows: List[Dict[str, Any]] = []
    with _patched_settings(ROUTING_FASTPATH_ENABLED=False, ROUTING_CACHE_ENABLED=False):
        router_module.get_llm_client = _counting  # type: ignore[assignment]
        try:
            for repeat in range(repeats):
                for question in _FASTPATH_HIT_QUESTIONS:
                    started = time.perf_counter()
                    error = ""
                    router_type = ""
                    logic = ""
                    try:
                        result = asyncio.run(
                            router_module.analyze_and_route_query(
                                AgentState(messages=[HumanMessage(content=question)]), config={}
                            )
                        )
                        router_obj = result["router"]
                        router_type = str(getattr(router_obj, "type", ""))
                        logic = str(getattr(router_obj, "logic", ""))[:200]
                    except Exception as exc:  # noqa: BLE001 - 失败如实登记
                        error = f"{type(exc).__name__}: {exc}"
                    rows.append(
                        {
                            "repeat": repeat,
                            "question": question,
                            "router_type": router_type,
                            "logic": logic,
                            "fell_back": logic.startswith("fallback"),
                            "error": error,
                            "elapsed_ms": (time.perf_counter() - started) * 1000.0,
                        }
                    )
        finally:
            router_module.get_llm_client = original  # type: ignore[assignment]

    fell_back = [row for row in rows if row["fell_back"] or row["error"]]
    classified = [row for row in rows if not (row["fell_back"] or row["error"])]
    status = "completed"
    skip_reason = None
    if not classified:
        status = "skipped_no_environment"
        skip_reason = (
            "off 臂全部落到启发式兜底/异常（分类器没产出结果）⇒ 本机不可测："
            "读数会是「超时 + 兜底」，不能当分类器时延"
        )
    results: Dict[str, Any] = {
        "flag_values": {
            "ROUTING_FASTPATH_ENABLED": False,
            "ROUTING_CACHE_ENABLED": False,
        },
        "classifier_constructions": counters["constructions"],
        "classified_count": len(classified),
        "fallback_or_error_count": len(fell_back),
        "fallback_rows": fell_back,
        **_pooled(
            "pooled_quantiles_ms",
            [row["elapsed_ms"] for row in classified],
            "只有**成功分类**的样本进池；兜底/超时样本单列，不混入时延读数",
        ),
        "per_sample": rows,
        "quantile_source": QUANTILE_SOURCE,
    }
    return _emit(
        "fastpath",
        "flag_off",
        repeats=repeats,
        touches_model=True,
        temperature=temperature,
        status=status,
        skip_reason=skip_reason,
        results=results,
        notes={
            "what_is_measured": (
                "ROUTING_FASTPATH_ENABLED=false（默认）时 analyze_and_route_query 的"
                "**分类器往返**：建模次数、成功分类样本的墙钟、兜底/超时占比"
            ),
            "arms": {
                "flag_off": "ROUTING_FASTPATH_ENABLED=false（默认值）",
                "flag_on": "见同名臂的 flag_on 子臂（零模型短路）",
            },
            "fallback_is_not_a_reading": (
                "router 角色的 profile 超时是 10s；本机模型单次 16s 量级 ⇒ 长问句会"
                "超时并落到启发式兜底。兜底样本单列在 results.fallback_rows，"
                "**不参与**时延池化，也不被当成「分类器很快」"
            ),
            "temperature_source": (
                "调用点传 temperature=0.7 触发 llm_factory 的 profile sentinel ⇒ "
                "router 角色实际温度取 profile 的 0.0（指纹里记的就是这个 0.0）"
            ),
            "reset_strategy": "计数壳在 finally 里还原；无进程级单例参与",
            "endpoint_gate": "端点必须回环——本臂会真的发出提示词",
        },
    )


def arm_fastpath(
    repeats: int, samples: int, temperature: Optional[float]
) -> List[Dict[str, Any]]:
    _ = samples
    return [
        _fastpath_on_state(repeats),
        _fastpath_off_state(repeats, temperature),
    ]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

_ARMS: Dict[str, Callable[[int, int, Optional[float]], List[Dict[str, Any]]]] = {
    "probe": arm_probe,
    "admission": arm_admission,
    "retry_budget": arm_retry_budget,
    "load_shedding": arm_load_shedding,
    "bulkhead": arm_bulkhead,
    "singleflight": arm_singleflight,
    "tier_routing": arm_tier_routing,
    "token_window": arm_token_window,
    "fastpath": arm_fastpath,
}


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m bench.adaptive_ab",
        description="T17：Adaptive 七项 + token 窗口 A/B（复用 bench 骨架的分位数/指纹/落盘）",
    )
    parser.add_argument("--arm", required=True, choices=sorted(_ARMS),
                        help="一次调用只测一个臂（进程级单例 + 全局 lag 的隔离要求）")
    parser.add_argument("--repeats", type=int, default=MIN_REPEATS,
                        help=f"轮数（方法学下限 {MIN_REPEATS}，低于下限直接拒绝）")
    parser.add_argument("--samples", type=int, default=5,
                        help="每轮每场景的样本数（时延类臂用；缺省 5）")
    parser.add_argument("--temperature", type=float, default=None,
                        help="触碰模型采样的臂必须显式给（本仓指纹纪律）")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_RESULTS_DIR)
    args = parser.parse_args(argv)

    if args.repeats < MIN_REPEATS:
        parser.error(
            f"--repeats={args.repeats} 低于方法学下限 {MIN_REPEATS}"
            "（重复 <5 没有可发布的尾部，与 bench/README.md 铁律 3 同口径）"
        )
    if args.samples < 1:
        parser.error(f"--samples={args.samples} 必须 ≥1")

    endpoints = _gate_endpoints()
    print(f"[adaptive_ab] arm={args.arm} repeats={args.repeats} samples={args.samples}")
    print(
        "[adaptive_ab] 端点闸通过：llm={llm} embeddings={emb}（回环）".format(
            llm=endpoints["llm_base_url"], emb=endpoints["embedding_base_url"]
        )
    )

    payloads = _ARMS[args.arm](args.repeats, args.samples, args.temperature)
    unmeasurable: List[str] = []
    for payload in payloads:
        path = write_result(payload, out_dir=args.out_dir)
        status = payload["measurement_status"]
        if status != "completed":
            unmeasurable.append(payload["target"])
        print(f"[adaptive_ab] {payload['target']} status={status} -> {path}")
        results = payload["results"]
        for key in ("pooled_quantiles_ms", "window", "scenarios", "by_injection"):
            if key in results:
                print(f"[adaptive_ab]   {key}={json.dumps(results[key], ensure_ascii=False)}")
    if unmeasurable:
        print(
            "[adaptive_ab][WARNING] 以下子臂在本机环境不可测（原因已写进各自结果文件）："
            + ", ".join(unmeasurable)
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
