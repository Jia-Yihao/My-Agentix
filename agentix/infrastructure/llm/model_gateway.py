"""R3-7 Model Gateway（架构文档 §42-44）。

在既有 ``llm_factory``（role 分级 / Provider 注册表 / token 计量 / 预算
检查点）之上补齐 Gateway 三缺口——**Strangler Pattern：不推翻、只叠加**：

1. **Model Registry + Router**（§43）：任务分档（fast/standard/reasoning）
   → 模型解析。分档显式配置（``LLM_MODEL_FAST/STANDARD/REASONING``）优先，
   空 = 回退 ``LLM_MODEL``（单模型部署零行为变化）。
   ``resolve_model`` 是 registry 的唯一解析入口，被 ``fallback_chain``
   内部消费——Agent 不直接散落绑定具体模型（§43 纪律）。
2. **Fallback 执行器**（§44）：``ainvoke_with_fallback`` 按
   primary → fallback 链逐个尝试，硬上限 ``LLM_FALLBACK_MAX_ATTEMPTS``
   防「模型故障 → 无限重试 → 成本爆炸」；单次调用的超时/重试/预算/熔断
   由消费点注入的 ``build_client``/``invoke`` 决定（本模块只做链编排）。

分层纪律：本模块（infrastructure）**不构建 client、不发起调用**——
client 构建方式与端点参数因调用域而异（编排域 llm_factory vs 检索域
create_chat_llm，两者的 base_url/extra_body 不同），由消费点注入
``build_client(model)`` 保证 fallback 客户端与主链路**同构**。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal

from loguru import logger

from agentix.config import settings

#: 任务分档（§43 Model Routing）。vision/embedding 由既有独立配置覆盖
#: （RERANKER_MODEL / 向量库 embedding 配置），不在本 Gateway 重复建档。
TaskType = Literal["fast", "standard", "reasoning"]

#: 分档 → settings 键（Model Registry）
_TIER_SETTINGS_KEY: dict[str, str] = {
    "fast": "LLM_MODEL_FAST",
    "standard": "LLM_MODEL_STANDARD",
    "reasoning": "LLM_MODEL_REASONING",
}


@dataclass
class FallbackSpec:
    """跨端点 fallback 条目（P1-1）。

    ``base_url`` / ``api_key`` 两者**同时**非空才构成跨端点降级
    （消费点据此决定是否绕开主端点构造 client）；任一为空 = 仅换模型名、
    沿用主端点（与旧 ``fallback_chain`` 语义一致）。
    """

    model: str
    base_url: str | None = None
    api_key: str | None = None


@dataclass
class GatewayResult:
    """Fallback 调用结果协议。

    ``model_used`` / ``attempts`` / ``errors`` 供轨迹与成本归因；
    全部尝试失败时 ``ainvoke_with_fallback`` 直接抛最后异常，
    本 dataclass 只承载成功态。
    """

    response: Any
    model_used: str
    attempts: int
    errors: list[str] = field(default_factory=list)


class ModelGateway:
    """Model Gateway：分档路由（Registry/Router）+ Fallback 链编排。"""

    # -- Model Registry / Router（§43） --

    def resolve_model(self, task_type: TaskType = "standard") -> str:
        """任务分档 → 模型名。

        分档显式配置优先；空/未配置回退 ``LLM_MODEL``（单模型部署
        全档同模型，零行为变化）。未知分档 fail-fast（KeyError）——
        新增分档必须同步 ``_TIER_SETTINGS_KEY``。
        """
        key = _TIER_SETTINGS_KEY[task_type]
        model = (getattr(settings, key, "") or "").strip()
        return model or settings.LLM_MODEL

    # -- Fallback 链解析（§44） --

    def fallback_chain(self, *, primary_model: str | None = None) -> list[str]:
        """解析 ``LLM_FALLBACK_MODELS`` 为有序去重的 fallback 模型列表。

        - 空配置 → ``[]``（消费方据此走原路径，零行为变化）；
        - 去除空段与首尾空白、保持配置顺序、去重；
        - 剔除与 ``primary_model`` 相同的项（fallback 到自己无意义）。
        """
        raw = (getattr(settings, "LLM_FALLBACK_MODELS", "") or "").strip()
        if not raw:
            return []
        chain: list[str] = []
        for part in raw.split(","):
            model = part.strip()
            if not model or model in chain:
                continue
            if primary_model and model == primary_model:
                continue
            chain.append(model)
        return chain

    def fallback_specs(self, *, primary_model: str | None = None) -> list[FallbackSpec]:
        """解析 ``LLM_FALLBACK_MODEL_SPECS`` 为跨端点 fallback 条目列表（P1-1）。

        - 格式：逗号分隔的 ``model|base_url|api_key_env`` 三元组；
          ``base_url`` / ``api_key_env`` 段可空（``model||`` = 仅模型名，回退主端点）。
        - ``api_key_env`` 是环境变量名——此处只查名、读值，密钥不落配置文件；
          环境变量未设置时该条目 ``api_key=None``（消费点回退主端点路径，
          不抛错——降级链本身就是在处理故障，配置错不应雪上加霜）。
        - 解析语义与 :meth:`fallback_chain` 一致：去空段、保序、去重
          （按 model 名）、剔除与 primary 相同项。
        - **优先级**：本键非空时覆盖 ``LLM_FALLBACK_MODELS``（旧键旧语义零变化）。
        """
        raw = (getattr(settings, "LLM_FALLBACK_MODEL_SPECS", "") or "").strip()
        if not raw:
            return []
        specs: list[FallbackSpec] = []
        seen: set[str] = set()
        for part in raw.split(","):
            entry = part.strip()
            if not entry:
                continue
            pieces = entry.split("|")
            model = pieces[0].strip()
            if not model or model in seen:
                continue
            if primary_model and model == primary_model:
                continue
            seen.add(model)
            base_url = pieces[1].strip() if len(pieces) > 1 and pieces[1].strip() else None
            api_key_env = pieces[2].strip() if len(pieces) > 2 and pieces[2].strip() else None
            api_key = os.environ.get(api_key_env) if api_key_env else None
            specs.append(FallbackSpec(model=model, base_url=base_url, api_key=api_key))
        return specs

    # -- Fallback 执行器（§44） --

    async def ainvoke_with_fallback(
        self,
        messages: Any,
        *,
        primary_model: str,
        fallback_models: list[str],
        build_client: Callable[[str], Any],
        invoke: Callable[[Any], Awaitable[Any]],
        max_attempts: int | None = None,
    ) -> GatewayResult:
        """按 primary → fallback 链执行 LLM 调用（§44）。

        Args:
            messages: 发给模型的消息/prompt（透传给 ``invoke`` 的闭包）。
            primary_model: 主模型名。
            fallback_models: fallback 链（建议经 :meth:`fallback_chain` 解析）。
            build_client: ``model -> client`` 工厂（消费点注入，保证与
                主链路同构：同 base_url / extra_body / 计量包装）。
            invoke: ``client -> Awaitable[response]``——单次调用的执行方式
                （超时/重试/熔断/并发限制由它决定）。
            max_attempts: 总尝试上限（含 primary）；缺省读
                ``LLM_FALLBACK_MAX_ATTEMPTS``——硬上限防成本爆炸。

        Returns:
            GatewayResult（成功态；含 model_used/attempts/errors 归因信息）。

        Raises:
            最后一次尝试的原始异常（全部失败时；不吞错、不包装，
            调用方既有错误处理语义不变）。
        """
        cap = max_attempts if max_attempts is not None else settings.LLM_FALLBACK_MAX_ATTEMPTS
        chain = [primary_model, *fallback_models][: max(1, cap)]

        errors: list[str] = []
        last_exc: BaseException | None = None
        for i, model in enumerate(chain, start=1):
            client = build_client(model)
            try:
                response = await invoke(client)
                if errors:
                    logger.warning(
                        "model fallback succeeded on attempt {}/{}: model={}",
                        i,
                        len(chain),
                        model,
                    )
                    # P2 可观测（O1）：fallback 触发率进 Prometheus（连锁触发可告警）
                    try:
                        from agentix.observability.metrics import record_gateway_fallback

                        record_gateway_fallback(chain[0], model)
                    except Exception:  # noqa: BLE001 - 指标故障不影响调用结果
                        pass
                return GatewayResult(
                    response=response, model_used=model, attempts=i, errors=errors
                )
            except Exception as exc:  # noqa: BLE001 - 逐模型尝试，最后统一上抛
                errors.append(f"{model}: {type(exc).__name__}: {exc}")
                last_exc = exc
                logger.warning(
                    "model attempt {}/{} failed: model={} err={}",
                    i,
                    len(chain),
                    model,
                    exc,
                )
        # 不变量：chain 至少 1 个模型 ⇒ 循环至少一次，失败则 last_exc 必有值。
        # 用显式异常而非 assert：`python -O` 剥离 assert 后会变成 `raise None`，
        # 报出 "TypeError: exceptions must derive from BaseException"，
        # 把真正的失败原因（errors 列表）完全掩盖掉。
        if last_exc is None:  # pragma: no cover - 理论不可达
            raise RuntimeError(
                "model_gateway: chain 非空却没有任何尝试记录（不变量被破坏）"
            )
        raise last_exc


_gateway: ModelGateway | None = None


def get_model_gateway() -> ModelGateway:
    """获取全局 Model Gateway 单例（无状态，单例仅为调用便捷）。"""
    global _gateway
    if _gateway is None:
        _gateway = ModelGateway()
    return _gateway
