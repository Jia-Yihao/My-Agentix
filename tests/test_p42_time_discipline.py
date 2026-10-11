"""P42：时间纪律守卫（防 naive 时钟调用回流）。

⚠️ 本文件是**绊线（guard test / tripwire）**，不是功能覆盖。

背景（2026-09 审计，已获用户授权修复）
----------------------------------------
``agentix/`` 下原有 5 处 naive 时钟用法，全部已修：

| 位置 | 原写法 | 危害 |
|---|---|---|
| ``crud_chat_session.update_last_activity`` | ``datetime.utcnow()`` | 3.12 已废弃；且往声明 ``DateTime(timezone=True)`` 的列写 **naive** 值 |
| ``redis_cache._now_timestamp/_iso_now`` | ``datetime.now()`` | 同一模块内 UTC epoch 与本地挂钟**两套"当前时间"口径** |
| ``chat_service`` 会话标题 | ``datetime.now()`` | 用户可见时间随容器 TZ 漂移 |
| ``search_service`` 的 ``cur_date`` | ``datetime.now()`` | 喂给 LLM 的"当前日期"可能早一天，直接影响时效性判断（文件已于 Phase 27 死代码清理删除，行留档为审计史实） |
| ``legal_effectiveness._as_date`` | ``date.today()`` | 法规"是否现行有效"边界整体错位一天（法律语义） |

**为什么值得设绊线**：naive 时钟在开发机（``+08:00``）与生产容器（``TZ`` 未设置
⇒ UTC）行为不同，是"本地全绿、上线错 8 小时"的典型；而且它**不抛异常**，
只让日期悄悄偏一天——日志里几乎看不出来，只有用户会发现"标题是昨天的时间"。

守卫四条：

1. ``agentix/`` 下不得出现任意 ``.utcnow()``、以及无参 ``datetime.now()`` / ``date.today()``；
2. 展示时区常量 ``config.DISPLAY_TZ`` 与领域层 ``legal_effectiveness._LEGAL_TZ``
   必须指向同一时区（防两处字面量漂移）；
3. ``domain`` 层不得 import ``agentix.config.settings``（该模块导入期即实例化
   ``Settings()`` 并读 ``.env``，会把领域纯函数绑死到运行环境）；
4. 边界语义：``expire_date`` 当天**仍有效**（严格大于才失效）——这条在修
   ``_as_date`` 时最容易被顺手改错，故钉住。

**显式排除**：``sqlalchemy.func.now()`` 是**数据库侧**服务器默认值，不是 Python
时钟；其接收者是 ``func`` 而非 ``datetime``，扫描器不会误判。
数据库时间戳的时区契约由列的 ``server_default`` / ``onupdate`` 承担。
"""

import ast
import pathlib
import re
from datetime import date, datetime, timedelta

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
AGENTIX = REPO_ROOT / "agentix"

#: ``agentix/`` 内的**显式豁免**：{相对路径: 理由}。
#:
#: 当前为空——修复后确实一处都不剩。留空本身是有意义的断言：
#: 一旦有人新增 naive 时钟，他必须来改这张表并写理由，而不是悄悄放过。
NAIVE_CLOCK_ALLOWLIST: dict = {}

#: 非 ``agentix/`` 目录（运维脚本 / 离线入库 / 评估）中的豁免：这些是**产物命名
#: 与人工阅读的报告文本**，本地挂钟更有辨识意义，且不进入业务数据。
#: **仍然登记**，避免"扫描不到就等于没有"；每条必须写明理由。
NON_AGENTIX_ALLOWLIST: dict = {
    "scripts/migration/backfill_vectors.py": "回填产物文件名的时间戳（本地挂钟便于人工辨识批次）",
    "evals/feedback_ingest.py": "回流候选集文件名的时间戳（同上）",
    "evals/run_eval.py": "评估结果目录/文件的时间戳（同上）",
    "kb_ingest/kb_service/services/processor.py":
        "产物目录名 + 处理报告 .txt 里的开始/完成时间（人工阅读用）；"
        "写入数据的 processed_time 与效力推导已改为 +08:00",
}

#: 时钟接收者白名单：``datetime`` / ``dt`` / ``date`` 这类别名。
_CLOCK_RECEIVER = re.compile(r"^\.(datetime|dt|date|_dt|_date)$")


def _iter_trees(root: pathlib.Path):
    """产出 ``(绝对路径, AST)``；解析失败的文件静默跳过（守卫不应因无关语法问题炸掉）。"""
    for path in sorted(root.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        except (SyntaxError, OSError, UnicodeDecodeError):
            continue
        yield path, tree


def _naive_clock_calls(root: pathlib.Path = AGENTIX):
    """AST 扫描（非正则，故注释/字符串里的写法不算）返回 naive 时钟调用。

    判定规则：

    - 任意接收者的 ``.utcnow()`` —— 天然 naive，且 Python 3.12 起废弃；
    - 接收者形如 ``datetime`` / ``dt`` / ``date`` 的**无参** ``.now()`` / ``.today()``。
      带参数（如 ``datetime.now(timezone.utc)``）即 aware，不算违规。
    """
    out = []
    for path, tree in _iter_trees(root):
        rel = path.relative_to(REPO_ROOT).as_posix()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            func = node.func
            if func.attr == "utcnow":
                out.append((rel, node.lineno, ast.unparse(node)))
                continue
            if func.attr in ("now", "today") and not node.args and not node.keywords:
                receiver = "." + ast.unparse(func.value).split(".")[-1]
                if _CLOCK_RECEIVER.match(receiver):
                    out.append((rel, node.lineno, ast.unparse(node)))
    return out


def _aware_clock_calls(root: pathlib.Path = AGENTIX) -> int:
    """统计**带参数**（即 aware）的时钟调用数，用于防空真断言。

    若扫描器坏掉（例如 AST 走不通、正则写错），naive 集合会是空集，
    于是"没有违规"这条断言会**假通过**。用一个必然 > 0 的计数把它钉住。
    """
    n = 0
    for _, tree in _iter_trees(root):
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in ("now", "today") and (node.args or node.keywords):
                    n += 1
    return n


# ---------------------------------------------------------------------------
# 1) 源码里不得有 naive 时钟
# ---------------------------------------------------------------------------

class TestNoNaiveClockInSource:
    def test_scanner_is_not_vacuous(self):
        """防空真：扫描器必须真的扫到了 aware 调用。

        ``agentix/`` 里 ``datetime.now(timezone.utc)`` 有若干处
        （runtime/steps、observability/trajectory 等）。扫不到说明扫描器失效，
        下面的"无违规"断言就没有意义。
        """
        assert _aware_clock_calls() > 0, (
            "扫描不到任何 aware 时钟调用 → 扫描器失效，"
            "``test_no_naive_clock_calls`` 的通过是假的"
        )

    def test_no_naive_clock_calls(self):
        found = [
            (p, ln, src)
            for p, ln, src in _naive_clock_calls()
            if p not in NAIVE_CLOCK_ALLOWLIST
        ]
        assert not found, (
            "``agentix/`` 出现 naive 时钟调用（会随部署时区漂移 8 小时）：\n"
            + "\n".join(f"  {p}:{ln}  {src}" for p, ln, src in found)
            + "\n\n修法：改用 ``datetime.now(timezone.utc)``（数据/日志）或 "
            "``datetime.now(DISPLAY_TZ)``（用户可见时间，见 agentix/config/settings.py）。"
        )

    def test_allowlist_entries_still_exist(self):
        """防永久豁免：白名单里的路径必须真的还有 naive 调用。

        否则白名单会变成"历史遗留的假条目"，掩盖将来同路径的新违规。
        """
        naive_paths = {p for p, _, _ in _naive_clock_calls()}
        stale = sorted(set(NAIVE_CLOCK_ALLOWLIST) - naive_paths)
        assert not stale, f"白名单已失效（路径下已无 naive 调用），请删除：{stale}"

    def test_utcnow_is_gone_repo_wide(self):
        """``utcnow()`` 在**本仓所有源码树**内一处都不许留。

        ⚠️ 2026-09 修复：本用例名一直叫 "repo_wide"，但**实现只扫了
        ``agentix/``**（``_naive_clock_calls()` 默认 root）——名实不符，
        且真的漏掉了东西：``tests/test_p16_chat_service.py`` 的假 repository
        还在用 ``datetime.utcnow()``（3.12+ 每次执行抛 DeprecationWarning，
        且与已修复的真实实现漂移）。现把范围落到源码树，与用例名一致。

        为什么只对 ``utcnow`` 做全树零容忍，而不把 ``now()`` / ``today()``
        也扩到 ``tests/``：后者在**测试夹具**里常是合理的（构造"今天"这类
        相对日期数据，且不进入业务产物）。``utcnow()`` 不同——它在所有
        Python 版本上都是**废弃 API**，没有"合理使用"的场景，故零容忍。
        """
        #: 纳入零容忍范围的源码树（与 tests/test_p46_toolchain_parity.py 同口径）
        trees = ("agentix", "kb_ingest", "evals", "scripts", "tests")

        # 防空真：目录若被改名/删除，上面的循环会静默缩小范围，
        # 这个用例就退化成"扫了个寂寞还全绿"。
        missing = [t for t in trees if not (REPO_ROOT / t).exists()]
        assert not missing, (
            f"零容忍扫描范围里的目录不存在，用例已静默缩小：{missing}"
        )

        offenders = []
        for tree in trees:
            root = REPO_ROOT / tree
            if not root.exists():
                continue
            offenders += [
                (p, ln, src)
                for p, ln, src in _naive_clock_calls(root)
                if ".utcnow()" in src
            ]
        assert not offenders, (
            "仍存在 datetime.utcnow()（3.12+ 已废弃，且产生 naive 值）：\n"
            + "\n".join(f"  {p}:{ln}  {src}" for p, ln, src in offenders)
            + "\n\n修法：改用 ``datetime.now(timezone.utc)``。"
            "若出现在测试假体里，通常说明**假体与真实实现漂移**——"
            "请一并对齐真实实现的写法。"
        )

    def test_non_agentix_trees_are_scanned_and_allowlisted(self):
        """``scripts`` / ``kb_ingest`` 也扫，且豁免必须登记在案。"""
        unregistered = []
        for tree_name in ("scripts", "kb_ingest", "evals"):
            root = REPO_ROOT / tree_name
            if not root.exists():
                continue
            for p, ln, src in _naive_clock_calls(root):
                if p not in NON_AGENTIX_ALLOWLIST:
                    unregistered.append((p, ln, src))
        assert not unregistered, (
            "以下位置使用了 naive 时钟但未登记豁免（请确认是产物命名还是业务数据）：\n"
            + "\n".join(f"  {p}:{ln}  {src}" for p, ln, src in unregistered)
        )


# ---------------------------------------------------------------------------
# 2) 时区常量一致性（防两处字面量漂移）
# ---------------------------------------------------------------------------

class TestTimezoneConsistency:
    def test_display_tz_matches_domain_tz(self):
        """``config.DISPLAY_TZ`` 与 ``legal_effectiveness._LEGAL_TZ`` 必须同一时区。

        领域层因分层约束（不得依赖 ``agentix.config``）而**重复了字面量**，
        重复值最大的风险就是漂移——本用例让漂移在 CI 立刻失败。
        """
        from agentix.config.settings import DISPLAY_TZ
        from agentix.domain.policies import legal_effectiveness as le

        domain_tz = getattr(le, "_LEGAL_TZ", None)
        assert domain_tz is not None, "领域层 _LEGAL_TZ 消失（_as_date 可能又回到 date.today()）"
        assert DISPLAY_TZ.key == domain_tz.key, (
            f"时区常量漂移：config.DISPLAY_TZ={DISPLAY_TZ.key!r} "
            f"vs legal_effectiveness._LEGAL_TZ={domain_tz.key!r}"
        )

    def test_display_tz_is_utc_plus_8(self):
        """产品面向中文法律场景，展示时区应为 +08:00。

        ⚠️ ``ZoneInfo.utcoffset()`` 必须传 ``datetime``：它要知道具体时刻
        才能解析夏令时规则，传 ``None`` 一律返回 ``None``（不是 0，也不是 8h）。
        """
        from agentix.config.settings import DISPLAY_TZ

        assert DISPLAY_TZ.utcoffset(datetime(2024, 6, 1)) == timedelta(hours=8)
        assert DISPLAY_TZ.utcoffset(datetime(2024, 12, 1)) == timedelta(hours=8)

    def test_all_three_legal_tz_constants_agree(self):
        """★ 三处"法规效力判定时区"必须完全一致。

        这是本类里**最有价值**的一条：法规效力在**两个不同环节**各自推导——

        - 入库侧：``kb_service.services.legal_metadata._derive_status``（离线写 metadata）
        - 查询侧：``agentix.domain.policies.legal_effectiveness``（M6 前旧路径 ``agentix.retrieval.domain.policies.legal_effectiveness`` 已删，见 ``tests/test_p74`` 旁路冻结）（检索后过滤）

        而 ``kb_ingest`` 是独立包（0 处 import agentix），领域层又不得依赖
        ``agentix.config``，因此"同一时区"这件事被**重复声明了三次字面量**。
        任何一处漂移，都会让同一部法规"入库时有效、查询时失效"（或反之），
        且只在跨零点的窗口期偶发——几乎不可能被人工发现。
        """
        from agentix.config.settings import DISPLAY_TZ
        from agentix.domain.policies import legal_effectiveness as le
        from kb_service.services import legal_metadata as lm

        keys = {
            "config.DISPLAY_TZ": DISPLAY_TZ.key,
            "retrieval.legal_effectiveness._LEGAL_TZ": le._LEGAL_TZ.key,
            "kb_service.legal_metadata._LEGAL_TZ": lm._LEGAL_TZ.key,
        }
        assert len(set(keys.values())) == 1, f"法规效力时区三处不一致（会导致两侧判定分歧）：{keys}"

    def test_as_date_defaults_to_display_tz_today(self):
        """``_as_date(None)`` 取的是**展示时区**的今天，而非进程本地时区。"""
        from agentix.config.settings import DISPLAY_TZ
        from agentix.domain.policies.legal_effectiveness import _as_date

        assert _as_date(None) == datetime.now(DISPLAY_TZ).date()

    def test_session_title_format_uses_display_tz_receiver(self):
        """``chat_service`` 建标题处必须传时区（AST 级，防被改回无参 ``now()``）。"""
        path = REPO_ROOT / "agentix" / "application" / "services" / "chat_service.py"
        src = path.read_text(encoding="utf-8")
        assert "datetime.now(DISPLAY_TZ)" in src, (
            "会话标题未使用 DISPLAY_TZ——用户可见时间会随容器 TZ 漂移"
        )


# ---------------------------------------------------------------------------
# 3) 领域层不得依赖配置实现
# ---------------------------------------------------------------------------

class TestDomainLayerStaysPure:
    """审计中发现并修掉的一处**差点引入的分层违规**。

    修 ``_as_date`` 时最自然的写法是 ``from agentix.config.settings import DISPLAY_TZ``，
    但 ``agentix.config.settings`` 在**导入期**就执行 ``Settings()``（pydantic-settings
    会读 ``.env``）。领域层本该是"无外部依赖的纯业务规则"，一旦绑定它：

    - 单独 import 领域模块即触发配置加载 → 单测必须准备环境；
    - ``domain`` 的行为随部署环境变化 → 纯函数不再纯。

    故 ``domain`` / ``retrieval/domain`` 内禁止出现指向 ``agentix.config`` 的导入。
    """

    DOMAIN_LAYERS = ("agentix/domain", "agentix/retrieval/domain")

    def test_no_config_import_in_domain_layers(self):
        offenders = []
        for layer in self.DOMAIN_LAYERS:
            root = REPO_ROOT / layer
            if not root.exists():
                continue
            for path, tree in _iter_trees(root):
                rel = path.relative_to(REPO_ROOT).as_posix()
                for node in ast.walk(tree):
                    if isinstance(node, ast.ImportFrom):
                        if (node.module or "").startswith("agentix.config"):
                            offenders.append(f"{rel}:{node.lineno}  from {node.module} import ...")
                    elif isinstance(node, ast.Import):
                        for alias in node.names:
                            if alias.name.startswith("agentix.config"):
                                offenders.append(f"{rel}:{node.lineno}  import {alias.name}")
        assert not offenders, (
            "领域层出现对 agentix.config 的依赖（会把纯业务规则绑到运行环境）：\n"
            + "\n".join(f"  {o}" for o in offenders)
        )

    def test_domain_layers_are_actually_scanned(self):
        """防空真：两个领域层都真的存在且含文件。"""
        for layer in self.DOMAIN_LAYERS:
            root = REPO_ROOT / layer
            assert root.exists(), f"{layer} 不存在——分层守卫扫了个空目录"
            assert any(root.rglob("*.py")), f"{layer} 下没有 .py 文件"


# ---------------------------------------------------------------------------
# 4) 效力判定的边界语义（最容易在改时间处理时被顺手改坏）
# ---------------------------------------------------------------------------

class TestEffectivenessBoundary:
    def test_expire_day_is_still_in_force(self):
        """``expire_date`` 当天仍有效（判定是严格大于 ``today > expire``）。"""
        from agentix.domain.policies.legal_effectiveness import effective_status_of

        meta = {"expire_date": "2024-06-30"}
        # 失效当天：仍有效
        assert effective_status_of(meta, now=date(2024, 6, 30)) == "in_force"
        # 次日：失效
        assert effective_status_of(meta, now=date(2024, 7, 1)) == "expired"

    def test_future_effective_date_is_pending(self):
        from agentix.domain.policies.legal_effectiveness import effective_status_of

        meta = {"effective_date": "2030-01-01"}
        assert effective_status_of(meta, now=date(2029, 12, 31)) == "pending"
        assert effective_status_of(meta, now=date(2030, 1, 1)) == "in_force"

    def test_missing_fields_fail_open(self):
        """存量数据/脏数据一律视为有效（**宁可漏报不误报**，法律场景硬原则）。"""
        from agentix.domain.policies.legal_effectiveness import effective_status_of

        assert effective_status_of(None) == "in_force"
        assert effective_status_of({}) == "in_force"
        assert effective_status_of({"expire_date": "不是日期"}) == "in_force"

    def test_asof_annotation_uses_injected_now(self):
        """``检索截至`` 标注必须走可注入的 ``now``（否则测试无法稳定断言）。"""
        from agentix.domain.policies.legal_effectiveness import format_retrieved_asof

        assert format_retrieved_asof(date(2024, 6, 30)) == "检索截至 2024-06-30"
