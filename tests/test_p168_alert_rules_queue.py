"""T15 队列积压告警：规则形状守卫（``docker/monitoring/alert_rules.yml``）。

验收①「积压告警规则新增且阈值口径成文」。

**本文件钉的是形状、口径，外加「阈值与声明容量之间的乘积等式」**（见下第 7 条）。
第 7 条钉的是**关系**不是**取值**：阈值该不该是这个数，仍属观测结论，测试管不到：

1. 新规则存在，且既有 8 条规则一条都没被删/改名（T15 边界：只新增）；并逐条钉住
   既有 8 条的 ``expr``/``for``/``severity`` 快照基线（台账表 A G2，2026-10-31 登记）；
2. 每条新规则的 ``expr`` / ``for`` / ``labels.severity`` / ``annotations`` 齐全，
   ``for`` 是合法 Prometheus 时长、``severity`` 在用词表内；
3. ``expr`` 里出现的每个指标名都**有出处**：要么被 ``agentix/observability/metrics.py``
   真实暴露（``Counter/Histogram/Gauge/Info`` 的第一个参数），要么是 Prometheus 内置
   ``up``，要么是 ``EXTERNAL_METRICS`` 里**署名登记过**的外部 exporter 指标。
   登记项必须同时满足：规则文件里写明出处的注释没被删（见用例 6）——
   判据与说明绑在一起，杜绝「编个指标名加规则、注释里什么也不说」；
4. 阈值口径成文：``expr`` 里的阈值数字必须以**数字 token 词边界**形态出现在
   ``annotations.description``（台账表 A G3：子串判据对 "0" 恒真），推导依据按
   中英别名集判定、与措辞解耦（台账表 A G6），否则「阈值为什么是这个数」只活在提交信息里；
5. 无高基数 label：新规则的 ``labels:`` 键与 ``expr`` 里不许出现 run_id/session/user
   等可枚举爆炸的维度（硬性约定 3 的精神），描述里也不许把它们插值进告警文本；
6. 外部指标的前置说明必须在**规则文件本身**里：删掉那段注释，本文件即失败；
   前置说明与部署现状（``docker-compose.yml`` 的 worker command）的**差额**也进断言
   （台账表 A G5），文档与部署两头对不上即红灯。
7. 容量算术同源（台账表 A G9，断言折在快照基线用例 ``test_legacy_alerts_baseline_shape``
   体内）：钉两件等式——① ``阈值 == 串行波次 × 副本默认值 × concurrency``（副本与并发从
   ``docker-compose.yml`` **文本**解析，阈值从 Backlog 的 ``expr`` 经 ``_by_alert()`` 抽取）；
   ② ``My AgentixCeleryQueueNotDraining`` 的 ``for`` 换算秒数 ==
   ``INDEX_JOB_TIMEOUT_SECONDS`` 的默认值（从 ``agentix/retrieval/settings.py`` 文本解析）——
   台账明写这里的 ``30m`` 指「单作业预算 1800s」这一面，**不是** Backlog 规则自身的
   ``for: 10m`` 持续窗口。四条解析正则各自**必须恰命中一次**，命中 0 次或多次即判红，
   不静默跳过。
   **残余（如实登记）**：「串行波次＝10」是登记时点**人工选定的口径**，不从任何真源推出，
   本条只保证「改了 compose 副本/并发而没人重算阈值」时会红，**不证明 10 这个选择本身
   合理**；钉的是乘积关系而非字面 20（阈值与波次一起等比改动 ⇒ 乘积仍相等 ⇒ 不红）。

**不做的事**：不整段比对 yml 原文（那样改个注释就红灯，守的是文本而不是口径），
也不 import 生产代码里的指标注册表去"求值"expressions——离线判据只做静态解析。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Set, Tuple

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
RULES_PATH = REPO_ROOT / "docker" / "monitoring" / "alert_rules.yml"
METRICS_PY = REPO_ROOT / "agentix" / "observability" / "metrics.py"
#: 部署现状的只读核对源（台账表 A G5）：worker 的 command 到底开没开 -E/--events
COMPOSE_PATH = REPO_ROOT / "docker-compose.yml"
#: 容量算术的第四枚真源（台账表 A G9）：单作业预算 ``INDEX_JOB_TIMEOUT_SECONDS`` 的默认值
RETRIEVAL_SETTINGS_PATH = REPO_ROOT / "agentix" / "retrieval" / "settings.py"

#: 队列阈值推导的「串行波次」乘数（台账表 A G9 显式化的**唯一人工量**）。
#: 这是**登记时点（2026-11-01）人工选定的口径**，不是从任何真源推出来的——规则文件的注释与
#: 两条 description 写的都是「20 = 10 个串行波次 × 默认容量（副本 1 × 并发 2）」。
#: 把它从注释里提出来写成常量，正是为了让「改 compose 副本/并发而不动阈值」必须红：
#: 想继续绿就得改这里，改这里就等于把波次口径显式改过一遍、留下 diff。
#: 残余：本批只钉乘积关系，不证明 10 这个选择本身合理（见文件头第 7 条）。
QUEUE_THRESHOLD_WAVES = 10

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

#: 快照式基线（登记时点 2026-10-31，台账表 A G2）：既有 8 条告警名 → (expr, for, severity)
#: 三枚**现值**，取自 ``docker/monitoring/alert_rules.yml`` 实测；``expr``/``for`` 的比较口径
#: 沿用本文件既有归一 ``" ".join(str(x).split())``，``severity`` 取 ``labels.severity``。
#: 既有规则被有意改动时必须同批在这里显式登记，这是「改旧规则要过名」的抓手。
LEGACY_ALERTS_BASELINE: Dict[str, Tuple[str, str, str]] = {
    "My AgentixBackendDown": (
        'up{job="agentix-backend"} == 0', "1m", "critical",
    ),
    "My AgentixHighErrorRate": (
        'sum(rate(http_requests_total{status=~"5.."}[5m])) / '
        "clamp_min(sum(rate(http_requests_total[5m])), 0.001) > 0.01",
        "5m",
        "critical",
    ),
    "My AgentixHighP95Latency": (
        "histogram_quantile( 0.95, "
        "sum(rate(http_request_duration_seconds_bucket[5m])) by (le) ) > 10",
        "5m",
        "warning",
    ),
    "My AgentixRateLimitStorm": (
        "sum(rate(rate_limit_triggered_total[5m])) > 5", "5m", "warning",
    ),
    "My AgentixComponentDegraded": (
        "increase(degraded_total[10m]) > 0", "0m", "warning",
    ),
    "My AgentixCircuitOpen": (
        "max by (name) (circuit_state) == 2", "1m", "critical",
    ),
    "My AgentixGroundingLowScoreSpike": (
        "sum(increase(grounding_low_score_total[30m])) > 10", "0m", "warning",
    ),
    "My AgentixGatewayFallbackCascade": (
        "sum(increase(gateway_fallback_total[15m])) > 5", "0m", "warning",
    ),
}

#: 阈值推导依据的中英别名集（台账表 A G6：判据与措辞解耦）：键 = 推导依据键名，
#: 值 = 该键可接受的措辞别名集（命中其中任意一枚即算点名了该依据）。
#: 中文别名是 description 改纯中文时的出路；现树实测（2026-10-31）：中文别名
#: 「索引任务超时」「并发」在两条新规则 description 里 0 次出现，成文覆盖靠
#: 用例内的正向哨兵（合并两条 description 后逐键判定）钉住，不靠现树措辞。
_THRESHOLD_BASIS_ALIASES: Dict[str, Tuple[str, ...]] = {
    "INDEX_JOB_TIMEOUT_SECONDS": ("INDEX_JOB_TIMEOUT_SECONDS", "索引任务超时"),
    "concurrency": ("concurrency", "并发"),
    "replicas": ("replicas", "副本"),
    "RETRIEVAL_WORKER_REPLICAS": ("RETRIEVAL_WORKER_REPLICAS", "副本数"),
}

#: 逐规则推导必需键映射（WD-1 第 2 轮返工 S-1）：规则名 → **该规则自身**的阈值推导
#: 在自身 description 里必须成文的依据键（每键按 `_THRESHOLD_BASIS_ALIASES` 判别名命中，
#: 不许由另一条规则的文本代偿——合并哨兵挡不住「单条 description 退化到只剩一枚别名单词」）。
#: 必需键集取自 2026-10-31 对 ``docker/monitoring/alert_rules.yml:118-140`` 两条 description
#: 原文的逐键复测：Backlog 四键全需（`replicas=1`/`concurrency=2`/
#: `INDEX_JOB_TIMEOUT_SECONDS=1800s`/`副本数` 皆在文）；NotDraining 全文无 `concurrency`
#: 也无「并发」⇒ 该键**不得**列入其必需集（列入必红），只列现文确实成文的三键。
_THRESHOLD_BASIS_REQUIRED: Dict[str, Tuple[str, ...]] = {
    "My AgentixCeleryQueueBacklog": (
        "INDEX_JOB_TIMEOUT_SECONDS",
        "concurrency",
        "replicas",
        "RETRIEVAL_WORKER_REPLICAS",
    ),
    "My AgentixCeleryQueueNotDraining": (
        "INDEX_JOB_TIMEOUT_SECONDS",
        "replicas",
        "RETRIEVAL_WORKER_REPLICAS",
    ),
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

#: 高基数维度黑名单（硬性约定 3 精神）：值域随请求/会话/用户增长的都不许进告警
_HIGH_CARDINALITY_TOKENS = (
    "run_id", "session", "session_id", "trace", "trace_id", "thread_id",
    "user_id", "request_id", "kb_id", "tenant_id", "query", "question",
    "answer", "thread",
)

#: Prometheus 时长字面量：一个或多个 <数字><单位>
_DURATION_RE = re.compile(r"^(?:\d+(?:ms|s|m|h|d|w|y))+$")

# --- 容量算术的四处解析（台账表 A G9）-------------------------------------------
# 一律按**文本正则**，不 ``yaml.safe_load(compose)`` 重解析：compose 里 `${...}` 未展开的
# 形态与本断言无关，正则只钉现树这一种真实写法（本批解析器不讲「封族不封例」——
# 被钉的永远是那一枚实际生效的默认值，认不出就是认不出，必须红）。
# 每条都必须在自己的源上**恰命中一次**（0 次＝写法变了，多次＝冒出第二处容量声明）。
_COMPOSE_REPLICAS_RE = re.compile(r"replicas: \$\{RETRIEVAL_WORKER_REPLICAS:-" + r"(\d+)\}")
_COMPOSE_CONCURRENCY_RE = re.compile(r"--concurrency=(\d+)")
_SETTINGS_INDEX_TIMEOUT_RE = re.compile(
    r'INDEX_JOB_TIMEOUT_SECONDS = int\(os\.getenv\("INDEX_JOB_TIMEOUT_SECONDS", "(\d+)"\)\)'
)
_BACKLOG_THRESHOLD_RE = re.compile(r"sum\(celery_queue_length\) > (\d+)")

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


def test_legacy_alerts_baseline_shape():
    """快照式基线（登记时点 2026-10-31，台账表 A G2）：既有 8 条的 expr/for/severity 逐条钉现值。

    为什么钉三枚字段：T15 的边界是「只新增」，``test_legacy_alerts_untouched`` 只断言
    名字集合不消失——``expr`` 被顺手改窄/改松、``for`` 被拉长/清零、``severity`` 被降级，
    名字面一条都不受影响，既有守卫全部照常绿，而告警行为已经变了：同类破坏与删规则
    一样致命、一样无声。本用例是**快照式基线**：现值逐字登记在
    ``LEGACY_ALERTS_BASELINE``，任何对既有规则的有意改动必须同批在这里显式登记。

    本函数体内现在还钉**台账表 A G9 的容量算术同源**（阈值 == 波次 × 副本 × 并发，以及
    NotDraining 的 ``for`` 换算秒数 == ``INDEX_JOB_TIMEOUT_SECONDS`` 默认值）。为什么折在
    这里而不是新开用例：新开用例会动收集面，按台账口径要触发第二次全量 re-cite；只加断言
    则 ``--collect-only`` 前后同为 14 items，代价为零，而两类红都指向同一件事——
    「告警文本与部署声明各说一套」，放在一起读才看得出它们是同一个口径的两面。
    """
    rules = _by_alert()
    # G-2（WD-1 第 2 轮退回一般项）：防「向 LEGACY_ALERTS 增名而不增基线」的静默失覆盖——
    # 名字集合不一致时下方 for 循环不会红（基线字典少的那条压根不被迭代），必须显式判。
    assert set(LEGACY_ALERTS_BASELINE) == LEGACY_ALERTS, (
        "LEGACY_ALERTS_BASELINE 与 LEGACY_ALERTS 名字集合不一致："
        f"只在基线里 {sorted(set(LEGACY_ALERTS_BASELINE) - LEGACY_ALERTS)}，"
        f"只在 LEGACY_ALERTS 里 {sorted(LEGACY_ALERTS - set(LEGACY_ALERTS_BASELINE))}"
    )
    for alert_name, (base_expr, base_for, base_severity) in LEGACY_ALERTS_BASELINE.items():
        rule = rules.get(alert_name)
        assert rule is not None, f"既有告警 {alert_name} 消失，基线无从比对"

        expr = " ".join(str(rule.get("expr", "")).split())
        assert expr == base_expr, (
            f"{alert_name}: expr 与基线不符（基线 {base_expr!r}，实际 {expr!r}）"
        )

        duration = " ".join(str(rule.get("for", "")).split())
        assert duration == base_for, (
            f"{alert_name}: for 与基线不符（基线 {base_for!r}，实际 {duration!r}）"
        )

        severity = rule.get("labels", {}).get("severity")
        assert severity == base_severity, (
            f"{alert_name}: severity 与基线不符（基线 {base_severity!r}，实际 {severity!r}）"
        )

    # --- 台账表 A G9：容量算术同源（追加在本函数体内，不新增用例）------------------
    # 先判解析面（③）再判等式（①②）：等式两边的数字必须是**从真源现读**的，不许是
    # 「解析失败后回落的某个默认」。四枚正则命中次数不是 1 就红——刻意不 return、
    # 不 pytest.skip、不 try/except：静默跳过等于把闸拆掉还留着绿，而这类闸一旦漏红
    # 就再也看不出阈值和部署容量是什么时候分家的。
    compose_text = COMPOSE_PATH.read_text(encoding="utf-8")
    settings_text = RETRIEVAL_SETTINGS_PATH.read_text(encoding="utf-8")
    backlog_rule = rules.get("My AgentixCeleryQueueBacklog")
    not_draining_rule = rules.get("My AgentixCeleryQueueNotDraining")
    assert backlog_rule is not None, f"{RULES_PATH} 里没有 Backlog 规则，容量算术无从比对"
    assert not_draining_rule is not None, (
        f"{RULES_PATH} 里没有 NotDraining 规则，单作业预算窗口无从比对"
    )

    replicas_hits = _COMPOSE_REPLICAS_RE.findall(compose_text)
    concurrency_hits = _COMPOSE_CONCURRENCY_RE.findall(compose_text)
    timeout_hits = _SETTINGS_INDEX_TIMEOUT_RE.findall(settings_text)
    threshold_hits = _BACKLOG_THRESHOLD_RE.findall(
        " ".join(str(backlog_rule.get("expr", "")).split())
    )
    for factor, source, pattern, hits in (
        ("副本默认值", str(COMPOSE_PATH), _COMPOSE_REPLICAS_RE.pattern, replicas_hits),
        ("并发", str(COMPOSE_PATH), _COMPOSE_CONCURRENCY_RE.pattern, concurrency_hits),
        (
            "单作业预算默认值",
            str(RETRIEVAL_SETTINGS_PATH),
            _SETTINGS_INDEX_TIMEOUT_RE.pattern,
            timeout_hits,
        ),
        ("Backlog 阈值", str(RULES_PATH), _BACKLOG_THRESHOLD_RE.pattern, threshold_hits),
    ):
        assert len(hits) == 1, (
            f"{factor}：在 {source} 上按 /{pattern}/ 解析实得 {len(hits)} 次（{hits}），"
            "必须恰命中 1 次——0 次＝那一处容量声明的写法变了、多次＝冒出第二处容量声明，"
            "两种情况都无从判断乘积关系，判红而不是跳过（台账表 A G9）"
        )

    replicas = int(replicas_hits[0])
    concurrency = int(concurrency_hits[0])
    threshold = int(threshold_hits[0])
    index_timeout_s = int(timeout_hits[0])

    # ①：阈值 == 波次 × 副本 × 并发（现树 20 == 10 × (1 × 2)）。
    declared_capacity = replicas * concurrency
    assert threshold == QUEUE_THRESHOLD_WAVES * declared_capacity, (
        f"容量算术脱钩：{RULES_PATH} 的 Backlog 阈值 {threshold} != 波次 {QUEUE_THRESHOLD_WAVES}"
        f" × 声明容量（{COMPOSE_PATH} 的副本 {replicas} × 并发 {concurrency}"
        f" = {declared_capacity}）——改 compose 容量就得同批重算阈值，"
        "否则要显式改动 QUEUE_THRESHOLD_WAVES 这枚人工口径，diff 里藏不住"
    )

    # ②：NotDraining 的 for 换算秒数 == INDEX_JOB_TIMEOUT_SECONDS 默认值（现树 30m == 1800s）。
    # 换算前先过本文件既有 `_DURATION_RE` 判字面合法（单位表与它的取值域一致），再逐段
    # 吃满整串：拼不回原文的一截说明有认不出的单位，宁可红也不许「凑」出一个秒数。
    # 台账明写这里的 30m 指「单作业预算」，不是 Backlog 自身的 for: 10m 持续窗口。
    not_draining_for = " ".join(str(not_draining_rule.get("for", "")).split())
    assert _DURATION_RE.match(not_draining_for), (
        f"{RULES_PATH} 的 NotDraining for {not_draining_for!r}"
        " 不是合法 Prometheus 时长字面量，无从换算成单作业预算"
    )
    unit_seconds = {"ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800, "y": 31536000}
    segments = re.findall(r"(\d+)(ms|s|m|h|d|w|y)", not_draining_for)
    assert segments and "".join(f"{v}{u}" for v, u in segments) == not_draining_for, (
        f"{RULES_PATH} 的 NotDraining for {not_draining_for!r} 无法逐段换算成秒"
        f"（单位表 {sorted(unit_seconds)}）"
    )
    not_draining_seconds = sum(int(v) * unit_seconds[u] for v, u in segments)
    assert not_draining_seconds == index_timeout_s, (
        f"单作业预算与窗口脱钩：{RULES_PATH} 的 NotDraining for {not_draining_for!r}"
        f" 换算 {not_draining_seconds:g}s != {RETRIEVAL_SETTINGS_PATH} 的 "
        f"INDEX_JOB_TIMEOUT_SECONDS 默认 {index_timeout_s}s——窗口取的是作业硬上限本身，"
        "改超时就得同批改窗口"
    )


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
    # 台账表 A G3：旧判据 `threshold in description` 对 "0" 恒真——子串能命中 "10m"/"20"
    # 这类无关数字，阈值没成文也不红。改数字 token 词边界判据：命中前后都不许是数字或小数点。
    threshold_hit = re.search(rf"(?<![\d.]){re.escape(threshold)}(?![\d.])", description)
    if not threshold_hit and threshold == "0":
        # 简报②前提与现树不符（2026-10-31 实测）：NotDraining 的 expr 阈值是 "> 0"，其
        # description 用「连续 30 分钟非空」成文该口径，全文没有独立的 "0" 数字 token
        # （词边界正则 0 命中，简报期望的收紧落点会在现树直接红）。本批边界禁改
        # alert_rules.yml ⇒ 只登记这一枚显式等价：阈值为 0 时须 description 写明「非空」，
        # 不再接受 "0" 的任意子串——恒真退化在这条出口上同样已关闭。
        threshold_hit = "非空" in description
    assert threshold_hit, (
        f"{alert_name}: 阈值 {threshold} 没写进 annotations.description——"
        "「阈值为什么是这个数」不能只活在提交信息里"
    )

    # 光有数字不够：必须点名它的推导依据（容量/超时），否则读者只能猜。
    # 台账表 A G6：判据与措辞解耦——旧判据是四个英文键名的子串，description 改纯中文
    # 会**误判成缺失**（真缺失与措辞变化两种红分不开）。改为逐键配中英别名集
    # （_THRESHOLD_BASIS_ALIASES），命中任一键的任一别名即算点名推导依据——与旧判据
    # 等强度（仍是「至少一枚依据」），只是不再绑死英文措辞；逐键的强制力由下方哨兵承担。
    basis_hit = any(
        any(alias in description for alias in aliases)
        for aliases in _THRESHOLD_BASIS_ALIASES.values()
    )
    assert basis_hit, (
        f"{alert_name}: description 没给出阈值的推导依据"
        f"（{_THRESHOLD_BASIS_ALIASES} 的中英别名一个都没提）"
    )

    # 正向哨兵（防别名集写空/被删导致判据空转）：把当前两条新规则的 description 合并后，
    # 每个推导依据键都必须至少命中其对应别名集里的一枚——删掉某键的中英两枚别名即红，
    # 只删中文、保留英文则不红。逐键生效的是这里，不是上面那条「至少一枚依据」。
    combined = " ".join(
        " ".join(str(_by_alert()[name]["annotations"]["description"]).split())
        for name in sorted(NEW_ALERTS)
    )
    for basis_key, aliases in _THRESHOLD_BASIS_ALIASES.items():
        assert aliases, f"推导依据 {basis_key} 的别名集为空——判据无从落地"
        assert any(alias in combined for alias in aliases), (
            f"推导依据 {basis_key} 在当前两条 description（合并文本）里一枚别名都没命中"
            "——别名集与成文脱节，主判据正在空转"
        )

    # 逐规则逐键断言（WD-1 第 2 轮返工 S-1，台账表 A G6 的收紧项）：上面合并哨兵只判
    # 「合起来有没有」——单条 description 可退化到只剩一枚别名单词（其余推导依据全挤到
    # 另一条），主判据 basis_hit 与合并哨兵都不红。这里按 _THRESHOLD_BASIS_REQUIRED 里
    # **该规则自身的必需键集**、用**该规则自身的 description**（上方已归一的 description
    # 变量）逐键断言别名命中，任一键零命中即该条红。叠加而非替换：basis_hit 主判据
    # 与合并哨兵均原样保留。
    for basis_key in _THRESHOLD_BASIS_REQUIRED[alert_name]:
        aliases = _THRESHOLD_BASIS_ALIASES[basis_key]
        assert any(alias in description for alias in aliases), (
            f"{alert_name}: 推导依据 {basis_key} 在其自身 description 里一枚别名都没命中"
            f"（别名集 {aliases}）——不许由另一条规则的文本代偿"
        )


def test_required_basis_keys_cover_every_declared_basis():
    # 防弱化断言（D30b Y-1）：上面三条判据（basis_hit / 合并哨兵 / 逐规则逐键）读的都是
    # description 文本，**不读 _THRESHOLD_BASIS_REQUIRED 这张映射表本身**——把某枚依据键从
    # 必需集里悄悄删掉，表变小了而文本判据照旧，不会红。本条补的是对**表自身**的结构断言：
    # ① 每枚在 _THRESHOLD_BASIS_ALIASES 里声明过的依据键，必须至少被一条新规则列为必需；
    # ② 每枚必需键必须存在于别名集里——否则 test_threshold_number_is_documented 里的
    #    `_THRESHOLD_BASIS_ALIASES[basis_key]` 会直接 KeyError，红点落在错处、看不出是表写歪了。
    # 本条**堵不住的那一半**（如实登记，不声称封死了整个删键面）：单条规则内部把某键从
    # **它自己的**必需集里删掉，而另一条规则仍列着该键 ⇒ 并集照旧、本条不红。该残余面靠
    # `_THRESHOLD_BASIS_REQUIRED` 上方「取自实测 / 不得列入」的署名注释维持（改必需集要署名）。
    declared = set(_THRESHOLD_BASIS_ALIASES)
    required_union = set().union(*_THRESHOLD_BASIS_REQUIRED.values())
    assert required_union == declared, (
        "逐规则必需键并集与别名集脱节："
        f"别名集独有 {sorted(declared - required_union)}（这枚依据已被**所有**新规则从必需集"
        "删除，逐键强制力消失——要撤依据请连别名集与署名注释一起撤，不许只删必需集）；"
        f"必需集独有 {sorted(required_union - declared)}（必需键不在别名集里，无别名可命中）"
    )

    for rule_name, required_keys in _THRESHOLD_BASIS_REQUIRED.items():
        unknown = set(required_keys) - declared
        assert not unknown, (
            f"{rule_name}: 必需键 {sorted(unknown)} 不在 _THRESHOLD_BASIS_ALIASES 里"
            "——逐规则断言会先在那里 KeyError，红点落在错处；要么补别名，要么改键名"
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
            f"{alert_name}: expr 引用了高基数维度 {token!r}（约定 3 精神：值域随请求增长）"
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

    # 台账表 A G5：上面两行只对**前置说明正文**做子串判定，部署现状
    # （docker-compose.yml 的 worker command 到底带不带 -E/--events）从不进断言——
    # 文档写着写着就绿，部署没跟上没人知道。下面钉的是「文档前置与部署现状的差额」，
    # 不是「事件已接通」。
    # （简报③期望 raw 逐字含 "docker-compose.yml"：2026-10-31 实测 raw 不含该全称，
    # 只有 "compose" 3 处（command 注记/阈值口径/broker 注记）⇒ 按现树判 "compose"。）
    assert "compose" in raw, "前置说明没点名要改哪个部署文件（compose）"
    # 前置说明必须显式写着「要加」：worker 发事件的启动形态逐字成文（正向哨兵，
    # 防止只判 "compose" 命中了无关注记而前置本身没落纸）。
    assert "-E/--events" in raw, (
        "前置说明没显式写着 worker 需以 -E/--events 启动——补部署时这句是唯一成文指引"
    )

    compose_doc = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))
    worker_cmd = (compose_doc.get("services") or {}).get("retrieval-worker", {}).get("command")
    cmd_text = (
        " ".join(str(part) for part in worker_cmd)
        if isinstance(worker_cmd, list)
        else " ".join(str(worker_cmd or "").split())
    )
    # 正向哨兵：确实从 compose 读到了 celery worker 的 command，防「找不到对象、
    # 负判定空过」的扫描空转。
    assert "celery" in cmd_text and "worker" in cmd_text, (
        f"docker-compose.yml 的 retrieval-worker command 不是 celery worker 形态，实际 {cmd_text!r}"
    )
    # 部署现状实测（2026-10-31，docker-compose.yml:273）：command 既无 "--events" 也无 "-E"
    # ⇒ 前置说明「compose 的 retrieval-worker command 目前没有 -E」的差额陈述成立。
    # 若哪天真的加上了，本断言红——那时该改的是前置说明与本文件，把差额登记清零，
    # 而不是让「文档说没接通、部署其实已接通」两头对不上。
    assert "-E" not in cmd_text and "--events" not in cmd_text, (
        "docker-compose.yml 的 retrieval-worker command 已带 -E/--events——"
        "与前置说明的差额陈述脱节，需同批改写前置说明与本断言"
    )
