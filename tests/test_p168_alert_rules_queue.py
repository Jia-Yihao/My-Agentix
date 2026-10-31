"""T15 队列积压告警：规则形状守卫（``docker/monitoring/alert_rules.yml``）。

卡面：``docs/design/审批与中断恢复优化规划.md`` §4.3 T15 —— 验收①「积压告警规则
新增且阈值口径成文」。

**本文件钉的是形状与口径，不钉阈值对不对**（阈值合不合理属观测结论，测试管不到）：

1. 新规则存在，且既有 8 条规则一条都没被删/改名（T15 边界：只新增）；
2. 每条新规则的 ``expr`` / ``for`` / ``labels.severity`` / ``annotations`` 齐全，
   ``for`` 是合法 Prometheus 时长、``severity`` 在用词表内；
3. ``expr`` 里出现的每个指标名都**有出处**：要么被 ``agentix/observability/metrics.py``
   真实暴露（``Counter/Histogram/Gauge/Info`` 的第一个参数），要么是 Prometheus 内置
   ``up``，要么是 ``EXTERNAL_METRICS`` 里**署名登记过**的外部 exporter 指标。
   登记项必须同时满足：规则文件里写明出处的注释没被删（见用例 6）——
   判据与说明绑在一起，杜绝「编个指标名加规则、注释里什么也不说」；
4. 阈值口径成文：``expr`` 里的数字字面量必须能在 ``annotations.description`` 里找到，
   否则「阈值为什么是这个数」只活在提交信息里；
5. 无高基数 label：新规则的 ``labels:`` 键与 ``expr`` 里不许出现 run_id/session/user
   等可枚举爆炸的维度（硬性纪律 3 的精神），描述里也不许把它们插值进告警文本；
6. 外部指标的前置说明必须在**规则文件本身**里：删掉那段注释，本文件即失败。

**不做的事**：不整段比对 yml 原文（那样改个注释就红灯，守的是文本而不是口径），
也不 import 生产代码里的指标注册表去"求值"expressions——离线判据只做静态解析。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Set

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
RULES_PATH = REPO_ROOT / "docker" / "monitoring" / "alert_rules.yml"
METRICS_PY = REPO_ROOT / "agentix" / "observability" / "metrics.py"

#: 新增规则所在分组（与既有 agentix-availability 分开：队列面挂的是外部 exporter）
QUEUE_GROUP = "agentix-queue"

#: T15 新增的两条规则；阈值与判据口径见规则文件里的注释
NEW_ALERTS = {
    "My AgentixCeleryQueueBacklog",
    "My AgentixCeleryQueueNotDraining",
}

#: 加规则前就存在的 8 条（T15 只许新增，不许删/改名/改阈值）
LEGACY_ALERTS = {
    "My AgentixBackendDown",
    "My AgentixHighErrorRate",
    "My AgentixHighP95Latency",
    "My AgentixRateLimitStorm",
    "My AgentixComponentDegraded",
    "My AgentixCircuitOpen",
    "My AgentixGroundingLowScoreSpike",
    "My AgentixGatewayFallbackCascade",
}

#: Prometheus 自带、不来自本仓暴露面的指标（内置抓取探针）
PROMETHEUS_BUILTIN_METRICS = {"up"}

#: 外部 exporter 提供的指标：**署名登记**。键 = 指标名，值 = 出处（规则文件里必须
#: 逐字出现，见 ``test_external_metric_prerequisite_is_documented``）。
#: 加一条之前先问：本仓的抓取面真的会产出它吗？不产出就在这里写明出处与前置，
#: 不许只把指标名塞进 expr 就算完。
EXTERNAL_METRICS: Dict[str, str] = {
    "celery_queue_length": (
        "celery-exporter（danihodovic/celery-exporter）从 broker 侧导出；"
        "本仓 prometheus.yml 尚无该抓取目标，前置与三步落地清单见规则文件末尾"
        "「队列规则前置说明」"
    ),
}

#: PromQL 函数与关键字——出现在 expr 里但不是指标名
_PROMQL_WORDS = {
    # 函数
    "sum", "min", "max", "avg", "count", "count_values", "group", "stddev",
    "stdvar", "topk", "bottomk", "quantile", "abs", "ceil", "floor", "round",
    "clamp", "clamp_min", "clamp_max", "rate", "irate", "increase", "delta",
    "idelta", "deriv", "predict_linear", "histogram_quantile", "absent",
    "absent_over_time", "avg_over_time", "max_over_time", "min_over_time",
    "sum_over_time", "count_over_time", "last_over_time", "label_replace",
    "label_join", "vector", "scalar", "changes", "resets", "sort", "sort_desc",
    "timestamp", "time", "hour", "day_of_week",
    # 聚合子句与关键字
    "by", "without", "on", "ignoring", "group_left", "group_right", "bool",
    "and", "or", "unless", "offset", "le", "inf", "nan",
    # 既有规则 ``max by (name) (circuit_state)`` 的分组键：是 label 名，不是指标
    "name",
}

#: 高基数维度黑名单（硬性纪律 3 精神）：值域随请求/会话/用户增长的都不许进告警
_HIGH_CARDINALITY_TOKENS = (
    "run_id", "session", "session_id", "trace", "trace_id", "thread_id",
    "user_id", "request_id", "kb_id", "tenant_id", "query", "question",
    "answer", "thread",
)

#: Prometheus 时长字面量：一个或多个 <数字><单位>
_DURATION_RE = re.compile(r"^(?:\d+(?:ms|s|m|h|d|w|y))+$")

_IDENT_RE = re.compile(r"[a-zA-Z_:][a-zA-Z0-9_:]*")
_QUOTED_RE = re.compile(r"\"[^\"]*\"|'[^']*'")
_BRACES_RE = re.compile(r"\{[^{}]*\}")
_RANGE_RE = re.compile(r"\[[^\[\]]*\]")
_HYPHEN_RE = re.compile(r"\s-\s")


def _load_rules() -> Dict:
    return yaml.safe_load(RULES_PATH.read_text(encoding="utf-8"))


def _all_rules() -> List[Dict]:
    out: List[Dict] = []
    for group in _load_rules()["groups"]:
        for rule in group["rules"]:
            out.append({"__group__": group["name"], **rule})
    return out


def _by_alert() -> Dict[str, Dict]:
    return {rule["alert"]: rule for rule in _all_rules()}


def _new_rules() -> List[Dict]:
    return [rule for rule in _all_rules() if rule["alert"] in NEW_ALERTS]


def _promql_metric_names(expr: str) -> Set[str]:
    """从一条 PromQL 表达式里抽出指标名（静态、离线，不求值）。

    步骤刻意保守：先删掉引号里的字符串（label 值）与 ``{...}`` / ``[...]`` 内部
    （label 名、范围时长），再删聚合子句，最后剩下的标识符减去函数/关键字。
    抽多了会在「未登记指标」上误报，抽漏了会放过虚假指标名——所以用既有 8 条
    已知正确的规则做自检（见 ``test_metric_extractor_validated_on_legacy_rules``）。
    """
    text = " ".join(str(expr).split())
    text = _QUOTED_RE.sub('""', text)
    text = _BRACES_RE.sub("", text)
    text = _RANGE_RE.sub("", text)
    idents = set(_IDENT_RE.findall(text))
    return {name for name in idents if name.lower() not in _PROMQL_WORDS}


def _repo_exposed_metrics() -> Set[str]:
    """``observability/metrics.py`` 真实注册的指标名（``Counter("name"`` 等首参）。"""
    source = METRICS_PY.read_text(encoding="utf-8-sig")
    return set(
        re.findall(
            r"(?:Counter|Histogram|Gauge|Info)\(\s*\n?\s*\"([a-zA-Z_:][a-zA-Z0-9_:]*)\"",
            source,
        )
    )


def _repo_scrapeable_metric_names() -> Set[str]:
    """抓取面上实际出现的序列名 = 注册名 + prometheus_client 的派生后缀。

    Histogram ``foo`` 在 /metrics 上以 ``foo_bucket`` / ``foo_sum`` / ``foo_count``
    出现，规则里写的是派生名（既有 P95 规则就是 ``http_request_duration_seconds_bucket``）。
    只按注册名判会把合法引用误判成「未暴露」，所以派生后缀在这里补齐。
    """
    exposed = _repo_exposed_metrics()
    return exposed | {
        f"{name}{suffix}"
        for name in exposed
        for suffix in ("_bucket", "_sum", "_count")
    }


# ---------------------------------------------------------------------------
# 1. 新规则存在、旧规则一条不少
# ---------------------------------------------------------------------------


def test_rule_file_parses_and_queue_group_exists():
    doc = _load_rules()
    assert isinstance(doc.get("groups"), list) and doc["groups"], "规则文件没有 groups"
    names = [group["name"] for group in doc["groups"]]
    assert QUEUE_GROUP in names, f"缺队列分组 {QUEUE_GROUP}，现有：{names}"

    queue_rules = [r for r in doc["groups"][names.index(QUEUE_GROUP)]["rules"]]
    assert queue_rules, "队列分组里一条规则都没有"


def test_new_alerts_are_present():
    present = set(_by_alert())
    missing = NEW_ALERTS - present
    assert not missing, f"缺 T15 队列告警：{sorted(missing)}"


def test_legacy_alerts_untouched():
    """T15 边界：只新增。既有 8 条的名字一个都不许消失。"""
    present = set(_by_alert())
    missing = LEGACY_ALERTS - present
    assert not missing, f"既有告警被删/改名（T15 只许新增）：{sorted(missing)}"


# ---------------------------------------------------------------------------
# 2. 形状齐全：expr / for / severity / annotations
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("alert_name", sorted(NEW_ALERTS))
def test_new_rule_shape_is_complete(alert_name: str):
    rule = _by_alert()[alert_name]

    assert str(rule.get("expr", "")).strip(), f"{alert_name}: expr 为空"
    assert rule.get("labels", {}).get("severity") in {"warning", "critical"}, (
        f"{alert_name}: severity 必须是 warning/critical（与既有 8 条同一用词表）"
    )
    assert _DURATION_RE.match(str(rule.get("for", ""))), (
        f"{alert_name}: for 必须是 Prometheus 时长（如 10m），实际 {rule.get('for')!r}"
    )

    annotations = rule.get("annotations") or {}
    assert str(annotations.get("summary", "")).strip(), f"{alert_name}: 缺 summary"
    description = str(annotations.get("description", "")).strip()
    assert description, f"{alert_name}: 缺 description（阈值口径必须成文）"


# ---------------------------------------------------------------------------
# 3. 指标名必须有出处（本仓暴露面 / Prometheus 内置 / 署名登记的外部指标）
# ---------------------------------------------------------------------------


def test_metric_extractor_validated_on_legacy_rules():
    """自检：抽取器跑既有 8 条规则，结果必须全部落在「本仓暴露面 + up」里。

    没有这一条，后面的「未登记指标」断言可能只是抽取器太弱（漏抽即无声通过）。
    """
    allowed = _repo_scrapeable_metric_names() | PROMETHEUS_BUILTIN_METRICS
    assert "http_requests_total" in allowed, "metrics.py 解析失败（拿不到 HTTP 指标名）"

    for rule in _all_rules():
        if rule["alert"] not in LEGACY_ALERTS:
            continue
        found = _promql_metric_names(rule["expr"])
        assert found, f"{rule['alert']}: 抽取器没从 expr 里抽到任何指标名"

        undeclared = found - allowed
        assert not undeclared, (
            f"{rule['alert']}: 引用未暴露指标 {sorted(undeclared)}——"
            "要么抽取器有误，要么既有规则引用了不存在的东西"
        )

    # 补自检盲区（台账表 A G7）：上面循环只跑 legacy 8 条，新增规则的 expr 从未被
    # 「抽取器能抽到东西」覆盖过——若新 expr 形态整体漏抽，只有
    # test_new_rules_only_use_declared_metrics 的 found 断言会撞见，而那条不判抽取器
    # 健康。这里只补判「抽得到东西」（非空），不判抽到的是否合法（合法性由那条负责，
    # 不搬过来）；legacy 段的 continue 与 undeclared 断言一行未动，二者是两件事。
    for rule in _new_rules():
        found_new = _promql_metric_names(rule["expr"])
        assert found_new, (
            f"{rule['alert']}: 抽取器没从新规则 expr 里抽到任何指标名——自检盲区，"
            "抽取器对新规则形态漏抽"
        )


def test_new_rules_only_use_declared_metrics():
    allowed = (
        _repo_scrapeable_metric_names()
        | PROMETHEUS_BUILTIN_METRICS
        | set(EXTERNAL_METRICS)
    )
    for rule in _new_rules():
        found = _promql_metric_names(rule["expr"])
        assert found, f"{rule['alert']}: expr 里没有指标名"
        undeclared = found - allowed
        assert not undeclared, (
            f"{rule['alert']}: 引用未登记指标 {sorted(undeclared)}；"
            "外部指标必须先在 EXTERNAL_METRICS 里署名出处"
        )


# ---------------------------------------------------------------------------
# 4. 阈值口径成文：expr 的数字要能在 description 里读到
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("alert_name", sorted(NEW_ALERTS))
def test_threshold_number_is_documented(alert_name: str):
    rule = _by_alert()[alert_name]
    expr = " ".join(str(rule["expr"]).split())
    description = " ".join(str(rule["annotations"]["description"]).split())

    # 阈值 = expr 里比较运算符右侧的数字（> 20 / > 0）。取最后一个数字字面量。
    numbers = re.findall(r">\s*(\d+(?:\.\d+)?)", expr)
    assert numbers, f"{alert_name}: expr 里找不到显式阈值（> N）"
    threshold = numbers[-1]
    assert threshold in description, (
        f"{alert_name}: 阈值 {threshold} 没写进 annotations.description——"
        "「阈值为什么是这个数」不能只活在提交信息里"
    )

    # 光有数字不够：必须点名它的推导依据（容量/超时），否则读者只能猜
    bases = ("INDEX_JOB_TIMEOUT_SECONDS", "concurrency", "replicas",
             "RETRIEVAL_WORKER_REPLICAS")
    assert any(basis in description for basis in bases), (
        f"{alert_name}: description 没给出阈值的推导依据（{bases} 一个都没提）"
    )


# ---------------------------------------------------------------------------
# 5. 无高基数 label
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("alert_name", sorted(NEW_ALERTS))
def test_new_rule_has_no_high_cardinality_labels(alert_name: str):
    rule = _by_alert()[alert_name]

    label_keys = set((rule.get("labels") or {}).keys())
    assert label_keys <= {"severity", "component", "queue", "job"}, (
        f"{alert_name}: 告警附加 label 出现未登记维度 {sorted(label_keys)}"
    )

    expr_lower = str(rule["expr"]).lower()
    annotations_text = " ".join(str(v) for v in (rule.get("annotations") or {}).values()).lower()
    for token in _HIGH_CARDINALITY_TOKENS:
        assert token not in expr_lower, (
            f"{alert_name}: expr 引用了高基数维度 {token!r}（纪律 3 精神：值域随请求增长）"
        )
        assert f"$labels.{token}" not in annotations_text, (
            f"{alert_name}: 告警文本插值了高基数 label {{ $labels.{token} }}"
        )


# ---------------------------------------------------------------------------
# 6. 外部指标的前置说明必须在规则文件里（删注释即红灯）
# ---------------------------------------------------------------------------


def test_external_metric_prerequisite_is_documented():
    raw = RULES_PATH.read_text(encoding="utf-8")

    for metric, provenance in EXTERNAL_METRICS.items():
        assert metric in raw, f"规则文件没提到外部指标 {metric}"
        assert "celery-exporter" in raw, "规则文件没写明外部指标的出处（celery-exporter）"
        assert "队列规则前置说明" in raw, "规则文件缺前置说明段落——静默失效就没人知道了"
        assert provenance  # 登记项必须带出处说明（空值等于没登记）

        # 出处串是规则文件正文的压缩转述，整串不可能逐字出现在 raw 里；只判上一行
        # `assert provenance` 等于没登记——编个假出处照样非空、照样绿。取 provenance 里
        # 带斜杠的「上游出处名」（如 danihodovic/celery-exporter）做逐字子串判定：它才是
        # 这条外部指标真正的来源标识，也是上面三句通用断言（celery-exporter / 前置说明段 /
        # prometheus.yml）都没单独钉住的那个 token——删掉规则文件里的上游出处名，本断言即红。
        source_tokens = [tok for tok in re.split(r"[\s（）；，,]", provenance) if "/" in tok]
        assert source_tokens, (
            f"{metric}: provenance 未登记任何带斜杠的上游出处名，判据无从落地"
        )
        assert any(tok in raw for tok in source_tokens), (
            f"{metric}: 出处名 {source_tokens} 没有一个逐字出现在规则文件正文里——"
            "登记的出处与实际成文脱节"
        )

    # 前置三步必须可见：抓取目标、exporter 部署、worker 发事件
    assert "prometheus.yml" in raw, "前置说明没指出要改哪个抓取配置"
    assert "--events" in raw or "-E" in raw, "前置说明漏了 worker 需发送事件这一条"
