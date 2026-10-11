"""
Predefined task handlers for common async operations.

Register these with the TaskQueue at application startup.
"""

import json
import re
import time
from pathlib import Path
from typing import Any, Dict

from loguru import logger

from agentix.application.services.prompts import CONVERSATION_SUMMARY_PROMPT
from agentix.application.services.task_queue import get_task_queue


queue = get_task_queue()


@queue.register("file_import")
async def handle_file_import(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Async file import handler.

    Supports: Excel (.xlsx) via the external Ingest Service.
    R5.2: TXT/MD/JSON branches removed together with the deprecated
    KnowledgeService shim; use the retrieval domain upload API instead.
    """
    file_path = payload.get("file_path")
    file_type = payload.get("file_type", "")
    incremental = payload.get("incremental", True)

    if not file_path:
        raise ValueError("file_path is required")

    p = Path(file_path)
    if not p.exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    suffix = p.suffix.lower() or file_type

    logger.info("Processing file import: path={} type={}", file_path, suffix)

    if suffix in {".xlsx", ".xls"}:
        from agentix.config import settings
        from agentix.domain.policies.ingest_auth import ingest_auth_headers_for
        import aiohttp

        ingest_url = settings.INGEST_SERVICE_URL
        if not ingest_url:
            raise RuntimeError("INGEST_SERVICE_URL not configured")

        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{ingest_url.rstrip('/')}/api/ingest/excel",
                json={
                    "excel_path": str(p),
                    "incremental": incremental,
                },
                # 与 file_handler 同一处理：kb_ingest 开启鉴权后不带 Key 即 401，
                # 而这里会把 401 报成 "Excel import failed (401)"，排查时容易被
                # 误判为服务故障而不是配置缺失。
                headers=ingest_auth_headers_for(
                    ingest_url,
                    api_key=settings.KB_API_KEY,
                    ingest_service_url=settings.INGEST_SERVICE_URL,
                ),
                timeout=300,
            ) as resp:
                if resp.status not in (200, 202):
                    text = await resp.text()
                    raise RuntimeError(f"Excel import failed ({resp.status}): {text}")
                result = await resp.json()

        return {"status": "imported", "file": str(p), "result": result}

    elif suffix in {".txt", ".md", ".csv", ".log", ".json"}:
        # R5.2：旧 KnowledgeService 兼容 shim 已下线，TXT/MD/JSON 入库
        # 统一经 retrieval 域上传接口（/api/v1/retrieval/documents）完成。
        raise ValueError(
            f"File type {suffix} is not supported by the file_import task; "
            "use the retrieval domain upload API (/api/v1/retrieval/documents) instead"
        )

    else:
        raise ValueError(f"Unsupported file type: {suffix}")


@queue.register("conversation_summary")
async def handle_conversation_summary(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Generate and store a summary of a conversation for long-term memory."""
    session_id = payload.get("session_id")
    messages = payload.get("messages", [])

    if not session_id or not messages:
        raise ValueError("session_id and messages are required")

    from agentix.application.agents.llm_factory import get_llm_client
    from agentix.application.services.long_term_memory import get_long_term_memory

    conversation_text = "\n".join(
        f"{m.get('role', 'user')}: {m.get('content', '')}"
        for m in messages[-20:]
    )
    # 批次 10（§10.10.2）：可重建判据溯源——记录被摘要覆盖窗口（末 20 条）的
    # 消息 id。与 conversation_text 用同一窗口，保证溯源与摘要内容一致；
    # 无 id 的消息（历史 payload）不溯源，is_rebuildable 会保守判 False。
    summarized_message_ids = [
        str(m["id"]) for m in messages[-20:] if isinstance(m, dict) and m.get("id") is not None
    ]

    # P0-2(b)：显式绑定 summarizer 角色（temperature=0.3, timeout=45s, max_retries=2）。
    model = get_llm_client(temperature=0.3, tags=["summary"], role="summarizer")
    # 静态指令在前，动态对话文本随后拼接。
    summary_prompt = CONVERSATION_SUMMARY_PROMPT.format(conversation_text=conversation_text)
    response = await model.ainvoke([{"role": "user", "content": summary_prompt}])
    summary = response.content

    memory = get_long_term_memory()
    await memory.store_summary(
        session_id, summary, messages, source_message_ids=summarized_message_ids
    )

    # P6-6.2（§3.5 后台评审）：flag-on 时把本次摘要抽出的稳定卡片异步晋升为
    # 长期画像条目——fire-and-forget，不阻塞 handler 返回；调度失败仅记
    # warning（摘要已落库，评审是旁路）。flag-off 报告形状与基线完全一致。
    from agentix.config import settings as app_settings

    if getattr(app_settings, "MEMORY_REVIEW_ENABLED", False):
        try:
            from agentix.application.services.memory_review import (
                schedule_review_for_summary,
            )

            schedule_review_for_summary(summary)
        except Exception as exc:  # noqa: BLE001 - 评审调度失败不影响摘要落库
            logger.warning("background memory review schedule failed (non-fatal): {}", exc)

    return {"status": "summarized", "session_id": session_id, "summary": summary}


@queue.register("memory_consolidation")
async def handle_memory_consolidation(payload: Dict[str, Any]) -> Dict[str, Any]:
    """触发长期记忆整理（Phase 24，Ch3 遗忘三动作生产化）。

    - **可关**：``MEMORY_CONSOLIDATION_ENABLED`` 默认关 ⇒ 直接 skipped，
      不触碰任何存储（flag-off 零行为）。
    - **幂等**：``LongTermMemory.try_claim_consolidation`` 的 ``SET NX EX``
      时间窗——窗口内重复触发返回 ``already_consolidated`` 跳过；
      ``force=true`` 可绕过；``window_seconds <= 0`` 关闭窗口。
    - ``dry_run=true`` 只返回计划不写数据（且不占窗口，便于反复观察）。
    - **profile_card 重派生**（Phase 29）：``MEMORY_PROFILE_CARD_ENABLED`` 开
      且非 dry_run 时，在整理后从当前事件日志确定性重派生 ``profile_card``
      （幂等、只增不改）并写回报告；flag-off 报告形状与基线完全一致。
    """
    from agentix.config import settings

    user_id = str(payload.get("user_id") or "default_user")
    if not settings.MEMORY_CONSOLIDATION_ENABLED:
        return {"status": "skipped", "reason": "disabled", "user_id": user_id}

    dry_run = bool(payload.get("dry_run", False))
    force = bool(payload.get("force", False))
    window_seconds = int(payload.get("window_seconds", 86400))

    from agentix.application.services.long_term_memory import get_long_term_memory

    memory = get_long_term_memory()
    if not dry_run:
        claimed = await memory.try_claim_consolidation(
            user_id, window_seconds=window_seconds, force=force
        )
        if not claimed:
            return {
                "status": "skipped",
                "reason": "already_consolidated",
                "user_id": user_id,
            }

    report = await memory.consolidate(user_id, dry_run=dry_run)

    # Phase 29（Ch3 §3.6 User as Code）：flag 开时在**整理触发点**重派生
    # profile_card（幂等、纯函数）；读路径不隐式重放。dry_run 零写入约定
    # 不变 ⇒ dry_run 不重派生；flag-off 报告形状与基线完全一致。
    if settings.MEMORY_PROFILE_CARD_ENABLED and not dry_run:
        card = await memory.refresh_profile_card(user_id)
        if card is not None:
            report["profile_card"] = card

    return {"status": "consolidated", **report}


# ---------------------------------------------------------------------------
# 睡眠学习（Phase 30，Ch9 §9.9）
# ---------------------------------------------------------------------------

#: 进程内幂等窗（单进程 task_queue 语义；多实例部署换 Redis SET NX——另期）
_EVOLUTION_SLEEP_CLAIM: Dict[str, float] = {}


def _try_claim_evolution_sleep(scope: str, *, window_seconds: int, force: bool) -> bool:
    """睡眠学习幂等闸：沿 ``try_claim_consolidation`` 语义的进程内时间窗版。

    ``force=True`` 无条件声明并刷新窗口；``window_seconds <= 0`` 关闭窗口
    （总是可触发）；窗口内重复触发返回 ``False``。
    """
    if window_seconds <= 0 or force:
        _EVOLUTION_SLEEP_CLAIM[scope] = time.monotonic()
        return True
    now = time.monotonic()
    last = _EVOLUTION_SLEEP_CLAIM.get(scope)
    if last is not None and (now - last) < window_seconds:
        return False
    _EVOLUTION_SLEEP_CLAIM[scope] = now
    return True


def _parse_proposal_json(text: str) -> Dict[str, Any]:
    """提取 LLM 输出中的提案 JSON（容忍代码围栏；不可解析即抛，不猜不填）。"""
    raw = (text or "").strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", raw, flags=re.S)
    if fence:
        raw = fence.group(1).strip()
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("提案输出必须是 JSON 对象")
    return data


@queue.register("evolution_sleep")
async def handle_evolution_sleep(payload: Dict[str, Any]) -> Dict[str, Any]:
    """离线睡眠学习（Phase 30，Ch9 §9.9）：无人值守聚合信号 → **只产提案，不晋升**。

    - **可关**：``EVOLUTION_SLEEP_ENABLED`` 默认关 ⇒ 直接 skipped，零副作用。
    - **门槛**：近窗信号数 ≥ ``min_signals``（默认 20）且错误率 ≥
      ``min_error_rate``（默认 0.1）才继续；未达 ⇒ ``no_op``（不占窗、不触网）。
    - **端点预检 fail-closed**：``OPENAI_API_KEY``/``OPENAI_API_BASE`` 任一为空
      ⇒ skipped 非伪造（先例 Phase 26 ``_judge_preflight``）。
    - **幂等**：端点就绪后先 claim（window/force 语义同
      ``try_claim_consolidation``；不浪费窗口在 no-op 路径上）。
    - **只 propose**：LLM 合成 → ``validate_proposal`` 白名单（carrier 限定
      knowledge/program）→ 落 staging；**绝不** ``run_regression``/``promote``
      （Ch9 双循环：提案可自动、晋升不可自动，晋升仍走 evals 门 + 人工灰度）。
    - ``dry_run=true``：只返回门槛计划（不占窗、不触网、不产提案）。
    """
    from agentix.config import settings

    if not settings.EVOLUTION_SLEEP_ENABLED:
        return {"status": "skipped", "reason": "disabled"}

    from agentix.agent.evolution import (
        EvolutionCarrier,
        EvolutionProposal,
        OfflineEvolutionLoop,
        get_online_loop_recorder,
        jsonl_proposal_sink,
    )
    from agentix.application.agents.llm_factory import get_llm_client
    from agentix.application.services.prompts import EVOLUTION_SLEEP_PROPOSAL_PROMPT

    # 档案路径先构造（非法路径 fail-closed，不浪费 LLM 调用）
    sink_path = str(getattr(settings, "EVOLUTION_PROPOSAL_LOG", "") or "").strip()
    sink = jsonl_proposal_sink(sink_path) if sink_path else None

    min_signals = max(int(payload.get("min_signals", 20)), 0)
    min_error_rate = float(payload.get("min_error_rate", 0.1))
    window_seconds = int(payload.get("window_seconds", 86400))
    force = bool(payload.get("force", False))
    dry_run = bool(payload.get("dry_run", False))

    agg = get_online_loop_recorder().aggregate()
    n = int(agg.get("n", 0))
    error_rate = round(1.0 - float(agg.get("success_rate", 0.0)), 4) if n else 0.0
    threshold: Dict[str, Any] = {
        "n": n,
        "error_rate": error_rate,
        "min_signals": min_signals,
        "min_error_rate": min_error_rate,
    }
    if n < min_signals or error_rate < min_error_rate:
        return {"status": "no_op", "reason": "threshold_not_met", **threshold}
    if dry_run:
        return {"status": "planned", "threshold_met": True, **threshold}

    api_key = (getattr(settings, "OPENAI_API_KEY", "") or "").strip()
    api_base = (getattr(settings, "OPENAI_API_BASE", "") or "").strip()
    if not api_key or not api_base:
        return {"status": "skipped", "reason": "no_endpoint"}

    if not _try_claim_evolution_sleep(
        "evolution_sleep", window_seconds=window_seconds, force=force
    ):
        return {"status": "skipped", "reason": "already_triggered"}

    model = get_llm_client(temperature=0.0, tags=["evolution_sleep"], role="general")
    prompt = EVOLUTION_SLEEP_PROPOSAL_PROMPT.format(
        n=n,
        error_rate=error_rate,
        error_stages=json.dumps(agg.get("error_stages", {}), ensure_ascii=False),
    )
    response = await model.ainvoke([{"role": "user", "content": prompt}])
    try:
        data = _parse_proposal_json(str(getattr(response, "content", "") or ""))
        carrier = EvolutionCarrier(str(data.get("carrier", "")).strip())
        if carrier not in (EvolutionCarrier.KNOWLEDGE, EvolutionCarrier.PROGRAM):
            return {"status": "rejected", "rejected_reason": "carrier_not_allowed_for_sleep"}
        proposal = EvolutionProposal(
            carrier=carrier,
            summary=str(data.get("summary", "")),
            payload=dict(data.get("payload") or {}),
        )
    except (ValueError, TypeError) as exc:
        return {"status": "rejected", "rejected_reason": f"invalid_proposal_json:{str(exc)[:160]}"}

    loop = OfflineEvolutionLoop(proposal_sink=sink)
    staged = loop.propose(proposal)
    result: Dict[str, Any] = {
        "status": "proposed" if staged.staged else "rejected",
        "staged": staged.staged,
        "proposal_id": staged.proposal_id,
        "carrier": carrier.value,
        **threshold,
    }
    if not staged.staged:
        result["rejected_reason"] = staged.rejected_reason
    if sink_path:
        result["proposal_log"] = sink_path
    # 只产提案：不调用 run_regression / promote（晋升不可自动）
    return result
