"""依赖声明一致性守卫：``requirements.txt`` ↔ ``pyproject.toml``。

## 为什么需要这个测试

CI 与 Docker 的安装方式是::

    pip install -r requirements.txt
    pip install -e . --no-deps      # ← 关键：--no-deps

那个 ``--no-deps`` 恰好让 **pyproject 里的版本 spec 完全不参与解析**。
于是 pyproject 写错了也不会让任何一条 CI 变红——只有"按 pyproject 安装"
（``pip install .``，比如下游用户、或构建 wheel 后安装）的人才会踩到。

实测发现的漂移（已修复）：

===========  =======================  ==================
包            requirements.txt         pyproject.toml
===========  =======================  ==================
langgraph-sdk  >=0.1.42,<0.2.0         >=0.2.0  ← 互斥！
pymilvus       >=2.4.0,<2.5            ==2.3.7  ← 缺 partition_key
pydantic       ==2.13.5                ==2.10.4
pyahocorasick  ==2.1.0                 ==2.0.0
===========  =======================  ==================

其中 ``langgraph-sdk`` 最严重：``langgraph==0.2.60`` 自身声明
``langgraph-sdk (>=0.1.42,<0.2.0)``，pyproject 却要求 ``>=0.2.0``——
**区间互斥，装不上**。requirements.txt 第 95 行的注释早就写了这件事，
但 pyproject 一直没同步。

## 断言口径

1. 两个文件共同声明的包，spec 必须逐字相同（空白归一化后）；
2. pyproject 里声明的运行期依赖不得在 requirements.txt 中缺失；
3. 关键 spec 必须能满足其"硬消费者"的真实约束（用已安装包的元数据核对）。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet

REPO_ROOT = Path(__file__).resolve().parents[1]
REQUIREMENTS_PATH = REPO_ROOT / "requirements.txt"
PYPROJECT_PATH = REPO_ROOT / "pyproject.toml"

#: 归一化后的包名（PEP 503）作为键
_SPEC_RE = re.compile(r"^([A-Za-z0-9_.\-]+)(\[[^\]]*\])?\s*(.*)$")


def _norm_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _norm_spec(spec: str) -> str:
    return re.sub(r"\s+", "", spec)


def _parse_requirements_txt() -> dict:
    """解析 requirements.txt → {PEP503 包名: spec 字符串}。"""
    out: dict = {}
    for raw in REQUIREMENTS_PATH.read_text(encoding="utf-8").splitlines():
        line = raw.split("#")[0].strip()
        if not line or line.startswith("-"):
            continue
        match = _SPEC_RE.match(line)
        if match:
            out[_norm_name(match.group(1))] = _norm_spec(match.group(3))
    return out


def _parse_pyproject_dependencies() -> dict:
    """解析 pyproject.toml 的 [project].dependencies → {PEP503 包名: spec}。

    这里刻意不依赖 ``tomllib``（Python 3.11+ 才有，本项目基线是 3.10），
    改为抽取 ``dependencies = [ ... ]`` 块内的字符串字面量。
    """
    text = PYPROJECT_PATH.read_text(encoding="utf-8")
    block = re.search(r"^dependencies\s*=\s*\[(.*?)^\]", text, re.S | re.M)
    assert block is not None, "pyproject.toml 中未找到 dependencies = [...] 块"
    out: dict = {}
    for item in re.findall(r'"([^"]+)"', block.group(1)):
        match = _SPEC_RE.match(item)
        if match:
            out[_norm_name(match.group(1))] = _norm_spec(match.group(3))
    return out


@pytest.fixture(scope="module")
def req_specs() -> dict:
    return _parse_requirements_txt()


@pytest.fixture(scope="module")
def pyp_specs() -> dict:
    return _parse_pyproject_dependencies()


# ---------------------------------------------------------------- 声明一致性


class TestDeclarationsAgree:
    def test_parsers_found_enough_packages(self, req_specs, pyp_specs):
        """防空真：两个解析器都必须真的抓到一批包，否则下面的断言会空跑通过。"""
        assert len(req_specs) > 30, f"requirements.txt 只解析出 {len(req_specs)} 个包，解析器可能失效"
        assert len(pyp_specs) > 30, f"pyproject.toml 只解析出 {len(pyp_specs)} 个包，解析器可能失效"

    def test_no_spec_drift_for_shared_packages(self, req_specs, pyp_specs):
        """两边都声明的包，版本 spec 必须一致。"""
        drift = {
            name: (req_specs[name], pyp_specs[name])
            for name in sorted(set(req_specs) & set(pyp_specs))
            if req_specs[name] != pyp_specs[name]
        }
        assert not drift, (
            "requirements.txt 与 pyproject.toml 版本 spec 漂移（CI 因 --no-deps 不会发现）："
            + "".join(f"\n  {n}: requirements={r!r} pyproject={p!r}" for n, (r, p) in drift.items())
        )

    def test_pyproject_runtime_deps_present_in_requirements(self, req_specs, pyp_specs):
        """pyproject 的运行期依赖必须同时出现在 requirements.txt（否则 Docker/CI 缺包）。"""
        missing = sorted(set(pyp_specs) - set(req_specs))
        assert not missing, f"pyproject 声明但 requirements.txt 缺失（CI/Docker 不会安装）：{missing}"


# ------------------------------------------------- 关键 spec 必须满足硬消费者


class TestCriticalSpecsSatisfyConsumers:
    """用**具体见证版本**核对 spec 是否真能满足其硬约束。

    为什么不用 ``SpecifierSet`` 求交集：``packaging`` 的 ``SpecifierSet``
    没有 ``intersection``，而"字符串非空"也**判不出语义矛盾**——
    ``>=2.0`` 与 ``<1.0`` 求 and 得到的字符串照样非空。所以这里改成
    "给出一个消费者都接受的见证版本，断言声明包含它"：可读、确定、离线可跑。
    """

    @staticmethod
    def _declared(name: str, specs: dict) -> SpecifierSet:
        raw = specs.get(_norm_name(name))
        assert raw is not None, f"未在 requirements.txt 中声明 {name}"
        return SpecifierSet(raw)

    @pytest.mark.parametrize(
        "pkg,witness,consumer_constraints,why",
        [
            (
                "langgraph-sdk",
                "0.4.10",
                ["langgraph==1.2.18 要求 (>=0.4.6,<0.5.0)"],
                "LG-0-2 升到 1.2 线：见证版本随约束同步（原 0.1.74 属 0.2.60 的 "
                ">=0.1.42,<0.2.0 区间，现该区间已不存在）",
            ),
            (
                "greenlet",
                "3.0.1",
                ["playwright==1.40.0 要求 ==3.0.1"],
                "playwright 精确钉 greenlet==3.0.1（SQLAlchemy 只要 !=0.4.17）",
            ),
            (
                "grpcio",
                "1.64.0",
                [
                    "pymilvus==2.4.15 要求 <=1.67.1,>=1.49.1",
                    "opentelemetry-exporter-otlp-proto-grpc==1.44.0 要求 >=1.63.2,<2.0.0",
                ],
                "两者交集为 [1.63.2,1.67.1]；原先未声明 grpcio，实际解析到 1.84.0（pip check 报错）",
            ),
        ],
    )
    def test_declared_spec_contains_a_version_all_consumers_accept(
        self, req_specs, pkg, witness, consumer_constraints, why
    ):
        declared = self._declared(pkg, req_specs)
        assert declared.contains(witness), (
            f"{pkg} 声明 {declared} 不含见证版本 {witness} —— {why}；消费者约束：{consumer_constraints}"
        )

    def test_pymilvus_allows_partition_key_api(self, req_specs):
        """代码用到 ``partition_key``（milvus_impl.py），必须允许 pymilvus>=2.4。

        反例：pyproject 原先写 ``==2.3.7``，那条路径会用 AttributeError 炸掉。
        """
        declared = self._declared("pymilvus", req_specs)
        assert declared.contains("2.4.0"), f"pymilvus 声明 {declared} 不允许 2.4.x"
        assert not declared.contains("2.3.7"), f"pymilvus 声明 {declared} 仍允许 2.3.7（无 partition_key）"

    def test_milvus_partition_key_usage_still_exists(self):
        """防空真：上面的理由基于"代码确实用了 partition_key"，这里核对代码事实。"""
        impl = REPO_ROOT / "agentix" / "retrieval" / "infra" / "vectorstore" / "milvus_impl.py"
        assert impl.exists(), "milvus_impl.py 路径变了，请同步本测试"
        assert "partition_key=True" in impl.read_text(encoding="utf-8"), (
            "代码里已不再使用 partition_key——若确实移除，请放宽 pymilvus 的约束说明"
        )

    def test_langgraph_sdk_constraint_matches_installed_langgraph(self):
        """防空真：见证版本的合法性来自"已装 langgraph 的确切约束"，这里核对元数据。

        若未来升级 langgraph 且放开了 langgraph-sdk 上界，本测试会红——
        那正是需要重新评估 requirements/pyproject 两处 pin 的信号，不是噪声。
        """
        from importlib.metadata import requires

        sdk_reqs = [r for r in (requires("langgraph") or []) if r.lower().startswith("langgraph-sdk")]
        assert sdk_reqs, "langgraph 未声明对 langgraph-sdk 的依赖，本测试前提失效"
        constraint = SpecifierSet(Requirement(sdk_reqs[0]).specifier)
        assert constraint.contains("0.4.10"), (
            f"langgraph 的 langgraph-sdk 约束已变化（现为 {sdk_reqs[0]}），"
            "请同步 requirements.txt / pyproject.toml 的 pin 与本测试的见证版本"
        )


# --------------------------------------------- 已装版本必须满足自身声明


# ---------------------------------------------------------------------------
# 环境快照：**按解释器版本（+ 非 Windows 平台）分档**
# ---------------------------------------------------------------------------
#
# 为什么必须分档（2026-09 实测教训）：
#
# 本地开发环境统一为 Python 3.12；CI 的 Linux Python 3.10 环境独立登记：
#
#   - Python 3.12（本地 .venv）—— RECORD 逐文件核对**零缺失**。
#   - Python 3.10-linux（CI）——全新安装，RECORD 逐文件核对**零缺失**。
#
# 将不同解释器/平台的事实合并成一份不分档的常量，容易让某一环境的豁免在另一环境过期。
#
# 因此下面按 ``major.minor`` 分档（非 Windows 平台再加平台后缀——2026-10-06 教训：
# CI 的 Linux py3.10 是**全新安装**，且没有 Windows 专属的 pywin32，
# 因而按平台分档，见 ``3.10-linux`` 档注释）。出现
# **未登记的解释器版本/平台**时直接失败——这是刻意设计：新环境里有没有损伤，
# 只有人核过才敢说"没有"，不能让"没登记 ⇒ 两张表都空 ⇒ 全绿"把真实损伤悄悄放过。
_ENV_KEY = f"{sys.version_info.major}.{sys.version_info.minor}"
if sys.platform != "win32":
    # 本地开发环境统一为 Windows Python 3.12；CI 等平台必须显式登记独立档。
    _ENV_KEY = f"{_ENV_KEY}-{sys.platform}"

_ENV_SNAPSHOTS: dict[str, dict] = {
    "3.12": {
        #: 本地唯一环境按 requirements.txt 安装；偏差或 RECORD 缺失均须先修复环境。
        #: 纪律不变：这里出现新条目 = 环境又漂移了，先修环境再考虑登记。
        "deviations": {},
        #: 该环境的 RECORD 逐文件核对为零缺失（实测），故豁免表必须为空——
        #: 留空才有意义：一旦将来真有包被半删，守卫能立刻报警。
        "record_exempt": {},
    },
    "3.10-linux": {
        #: CI（ubuntu-latest + setup-python 3.10）按 requirements.txt **全新安装**：
        #: pip 为满足每条声明而解析 ⇒ 偏差按构造为空（2026-10-06 实测 run 37421099435
        #: 本档 deviations 未报任何条目）。
        "deviations": {},
        #: 全新安装 ⇒ RECORD 逐文件核对零缺失；pywin32 是 Windows-only
        #: 包，Linux 上根本没装（登记了反而被 ``test_record_exempt_entries_are_still_needed``
        #: 判"已无缺失"而误红——这正是把 CI 档与本机档分开的直接原因）。
        #: 若将来 CI 上真出现半删包，守卫会列出具体包名，届时按同样口径补登记。
        "record_exempt": {},
    },
    "3.12-linux": {
        #: CI（ubuntu-latest + setup-python 3.12）按 requirements.txt **全新安装**：
        #: 与 3.10-linux 同理，pip 为满足每条声明而解析 ⇒ 偏差按构造为空。
        #: 换到 3.12 是因为 pydantic-settings 2.16.x 要求 Python >=3.11，
        #: 3.10 的 runner 在装依赖那一步就失败（run 41982113875）。
        "deviations": {},
        #: 全新安装 ⇒ RECORD 逐文件核对零缺失；Windows-only 的包在 Linux 上根本没装，
        #: 登记豁免反而会被 ``test_record_exempt_entries_are_still_needed`` 判成陈旧条目。
        "record_exempt": {},
    },
}

assert _ENV_KEY in _ENV_SNAPSHOTS, (
    f"未登记的解释器版本/平台 {_ENV_KEY}。"
    f"请先在该环境里跑 `pytest tests/test_dependency_consistency.py -q` 摸清它的"
    f"真实偏差与 RECORD 缺项，再往 _ENV_SNAPSHOTS 加一档（已登记："
    f"{sorted(_ENV_SNAPSHOTS)}）。**不要**为了让测试变绿而填伪造的空档。"
)

_SNAPSHOT = _ENV_SNAPSHOTS[_ENV_KEY]

#: 当前解释器下"已知「实装版本 ≠ 声明版本」"的快照（键为 PEP503 包名）。
#:
#: 为什么冻结而不是断言"完全相等"：CI/容器是从 requirements.txt **全新安装**的，
#: 那里不该有偏差；而开发机 .venv 是**增量演进**出来的，两者天然可能不同。
#: 因此断言方向只能是 ``实际偏差 ⊆ 本快照``——新偏差会红（真信号），
#: 已登记的偏差不阻塞（不噪声）。
#:
#: ⚠️ 这不是"豁免名单"，**更不是长期状态**。2026-09 审计时本机 `.venv` 与
#: 钉死版本差 9 个包（含 ``redis`` 5.0.1→8.1.0、``bcrypt`` 4.1.2→5.0.0 两个
#: 跨大版本），其中 ``pymupdf`` 的漂移直接导致 ``import fitz`` 在
#: PyMuPDF ≥1.26 上 ImportError —— **版本漂移不是"不干净"，是功能故障**。
#:
#: ``test_snapshot_has_no_stale_entries`` 会守住"不许再往这里塞东西当长期豁免"：
#: 只有当前**真的**存在偏差时，条目才允许存在。
_KNOWN_LOCAL_DEVIATIONS: dict = _SNAPSHOT["deviations"]

#: 当前解释器下 RECORD 豁免表（见 ``TestNoEnvironmentStructuralDamage.RECORD_EXEMPT``）。
_RECORD_EXEMPT: dict = _SNAPSHOT["record_exempt"]


def _installed_deviations(req_specs: dict) -> list:
    """返回 [(包名, 声明 spec, 实装版本)]，仅含"实装不满足声明"的包。"""
    from importlib.metadata import PackageNotFoundError, version

    out = []
    for name, spec in sorted(req_specs.items()):
        if not spec:
            continue
        try:
            installed = version(name)
        except PackageNotFoundError:
            continue  # 未安装（可选/未装包）不在此校验，由 ghost 检测单独负责
        if installed is None:
            # 元数据被掏空时 importlib.metadata 可能返回 None，
            # 直接抛给 packaging 会得到一个与病因无关的 TypeError。
            out.append((name, spec, "<metadata-unreadable>"))
            continue
        if not SpecifierSet(spec).contains(installed):
            out.append((name, spec, installed))
    return out


def _unreadable_metadata(req_specs: dict) -> list:
    """声明依赖里"元数据读不出/看不见"的包（幽灵包检测）。

    **为什么需要**：``importlib.metadata.version()`` 依赖 ``*.dist-info/METADATA``。
    文件系统里如果出现"代码目录在、dist-info 被删/被掏空"的状态，pip 与
    ``importlib.metadata`` 都会**看不见**这个包：``pip check`` 会说
    "requires X, which is not installed"，而依赖守卫此前用
    ``except PackageNotFoundError: continue`` 直接跳过——**守卫对这些包失明**。
    实测命中：本机 .venv 有一批 dist-info 被外部工具掏空（只剩 ``licenses/``），
    于是 ``pyyaml`` 实装 6.0.3 对守卫隐形、``python-dotenv`` 被卸载后无人察觉。

    判据：声明了该包，但 ``importlib.metadata`` 拿不到它的版本号。
    """
    from importlib.metadata import PackageNotFoundError, version

    missing = []
    for name in sorted(req_specs):
        try:
            if version(name) is None:
                missing.append(name)
        except PackageNotFoundError:
            missing.append(name)
        except Exception as exc:  # noqa: BLE001 - 元数据损坏的其它形态一并收口
            missing.append(f"{name}（读取失败：{type(exc).__name__}）")
    return missing


class TestInstalledVersionSatisfiesDeclaration:
    """已装版本不得出现**新的**、未登记的偏差。

    这是比"两文件互相比对"更强的一层：能抓到"pin 写错了但没人按它装过"的情况
    （langgraph-sdk / pydantic / pymilvus 三个漂移都会在这里暴露）。
    """

    def test_no_new_undeclared_deviation(self, req_specs):
        deviations = _installed_deviations(req_specs)
        new_ones = [d for d in deviations if d[0] not in _KNOWN_LOCAL_DEVIATIONS]
        assert not new_ones, (
            "出现**新的**「实装版本不满足自身声明」偏差（要么修声明，要么重装环境，"
            "要么在 _KNOWN_LOCAL_DEVIATIONS 写明原因）："
            + "".join(f"\n  {n}: 声明 {s!r}，实装 {v}" for n, s, v in new_ones)
        )

    def test_snapshot_has_no_stale_entries(self, req_specs):
        """★ 快照里登记过的偏差必须**当前仍然存在**。

        没有这条，快照就会退化成"永久豁免"：环境修好了，条目还留着，
        下次真的出现同名偏差时会被无声放行。方向性快照的正确形态是
        「偏差集合 == 快照集合 + 为空」，任何一边多出来都要报警。
        """
        actual = {d[0] for d in _installed_deviations(req_specs)}
        stale = sorted(set(_KNOWN_LOCAL_DEVIATIONS) - actual)
        assert not stale, (
            f"快照里这些包**已经没有偏差了**，请从 _KNOWN_LOCAL_DEVIATIONS 删除：{stale}"
        )

    def test_snapshot_entries_reference_real_declared_packages(self, req_specs):
        """快照里的包必须仍是本项目的声明依赖（防快照腐烂成无主条目）。"""
        orphaned = sorted(set(_KNOWN_LOCAL_DEVIATIONS) - set(req_specs))
        assert not orphaned, f"快照中的包已不在 requirements.txt 里，请清理：{orphaned}"

    def test_snapshot_is_documented(self):
        """快照每条都要有非空原因——不允许"裸豁免"。"""
        undocumented = sorted(
            k for k, v in _KNOWN_LOCAL_DEVIATIONS.items() if not str(v).strip()
        )
        assert not undocumented, f"以下快照条目没有写明原因：{undocumented}"


class TestNoGhostPackages:
    """★ 声明依赖必须能被 ``importlib.metadata`` 看见。

    守的是"环境看起来装好了、其实元数据被掏空"这一类故障：代码目录还在
    （``import attr`` 可能都能跑），但 dist-info 被删/被掏空 ⇒ pip 不认它、
    ``pip check`` 报 "not installed"、依赖守卫此前也失明。这种状态下
    "装了什么"这个问题**没有可信答案**，比"版本旧"危险得多。
    """

    #: 允许"已声明但本机确实未安装"的包（可选依赖 / 平台限定）。
    #: 每一条都必须写明理由，否则本测试形同虚设。
    OPTIONAL_NOT_INSTALLED: dict = {
        # qdrant-client 在 requirements.txt 里是注释掉的可选项，不会出现在 req_specs
    }

    def test_declared_packages_are_visible(self, req_specs):
        missing = _unreadable_metadata(req_specs)
        unexpected = [m for m in missing if m.split("（")[0] not in self.OPTIONAL_NOT_INSTALLED]
        assert not unexpected, (
            "以下**已声明**依赖的元数据读不出（多半是 dist-info 被删/被掏空，"
            "pip 与 importlib.metadata 都会看不见它）。修复方式："
            "`pip install -r requirements.txt` 重装，或 `pip install --no-deps "
            "--ignore-installed <包>` 恢复元数据：\n  " + "\n  ".join(unexpected)
        )

    def test_optional_allowlist_is_documented(self):
        """豁免名单不许有"裸豁免"，也不许过期。"""
        bad = sorted(k for k, v in self.OPTIONAL_NOT_INSTALLED.items() if not str(v).strip())
        assert not bad, f"OPTIONAL_NOT_INSTALLED 中以下条目没有写明理由：{bad}"


# ---------------------------------------------------------------------------
# 环境结构性损伤：半删包 + 残缺 dist-info
# ---------------------------------------------------------------------------
#
# 背景（2026-09 实测事故，两台机器上都复现过）：
#
#   本机 WorkBuddy 沙箱有**批量删除守卫**——单次工具调用内累计删除 ≥50 个文件
#   就 kill 掉进程。而 ``pip uninstall``（``--force-reinstall`` 的第一步）
#   本身就是批量删除：删到第 50 个文件时进程被杀，于是留下
#   **"dist-info 元数据完好、代码文件删了一半"**的包。实测命中：
#
#   - ``botocore``：目录里 ``__init__.py`` / ``exceptions.py`` 全不见，
#     只剩 ``data/`` ``docs/`` ``retries/`` ``vendored/`` 四个子目录 →
#     ``import boto3`` 直接 ``ModuleNotFoundError: botocore.exceptions``
#   - ``pycryptodome``：``Crypto/`` 下 248 个 .py 全灭
#
# 为什么既有守卫全都抓不到它：
#
#   - ``TestNoGhostPackages`` 查"元数据读不出"——而这里元数据**好得很**；
#   - ``pip check`` 只校验依赖区间，与"代码文件在不在"无关；
#   - ``pip install -r requirements.txt`` 见"已满足"直接跳过，**根本不会修**。
#
# 唯一有效的检测手段是拿 ``RECORD`` 逐文件核对，唯一有效的修复手段是
# **重新安装该包**（``pip install --ignore-installed --no-deps``，不能卸载）。


def _site_packages() -> Path:
    """当前解释器的 site-packages 真实路径（跨平台）。

    曾经写死 ``Path(sys.prefix) / "Lib" / "site-packages"``——Windows 专属布局。
    2026-10-06 实测：CI（Linux）上该路径不存在，``(sp / rel).exists()`` 对每个
    RECORD 条目恒为 False，把**全部已装包**误判为"代码被半删"（run 37421099435）。
    ``sysconfig.get_paths()["purelib"]`` 返回当前环境（含 venv/conda）的
    purelib 真值：Windows 为 ``<prefix>/Lib/site-packages``，Linux 为
    ``<prefix>/lib/pythonX.Y/site-packages``。
    """
    import sysconfig

    return Path(sysconfig.get_paths()["purelib"])


def _incomplete_dist_info() -> list:
    """返回 ``[(dist-info 目录名, 内含条目列表)]``——缺 METADATA 或 RECORD 者。

    比"元数据读不出"更靠前的一道探测：目录还在、但关键三件套
    （``METADATA`` / ``RECORD`` / ``WHEEL``）已被删除。
    """
    sp = _site_packages()
    bad = []
    if not sp.exists():
        return bad
    for entry in sorted(sp.iterdir()):
        if not (entry.is_dir() and entry.name.endswith(".dist-info")):
            continue
        names = {p.name for p in entry.iterdir()}
        if "METADATA" not in names or "RECORD" not in names:
            bad.append((entry.name, sorted(names)))
    return bad


def _record_missing_code_files() -> dict:
    """返回 ``{PEP503 包名: [缺失的相对路径, ...]}``——RECORD 列了但不存在的 .py/.pyd。

    这是本次事故的**核心探测器**：元数据完整、版本号正常，但代码文件被删。

    排除项（否则会淹没真信号）：

    - ``../`` 开头的条目：RECORD 里控制台脚本记作 ``../../Scripts/xxx.py``，
      即相对**环境根**而非 ``site-packages``。它们是 CLI 辅助脚本（``jp.py``、
      ``vba_extract.py``、``pywin32_postinstall.py``…），**不是可导入模块**，
      缺失不影响 ``import``。
    """
    import csv
    import io
    from importlib.metadata import distributions

    sp = _site_packages()
    out: dict = {}
    for dist in distributions():
        try:
            name = (dist.metadata["Name"] or "").strip()
        except Exception:  # noqa: BLE001 - 元数据损坏的包由 _incomplete_dist_info 负责
            continue
        record = dist.read_text("RECORD")
        if not record:
            continue
        missing = []
        for line in io.StringIO(record):
            row = next(csv.reader([line]), None)
            if not row:
                continue
            rel = row[0]
            if not rel.endswith((".py", ".pyd")):
                continue
            if rel.startswith("../"):
                continue  # 环境根下的控制台脚本，非可导入模块（见 docstring）
            if not (sp / rel).exists():
                missing.append(rel)
        if missing:
            out[_norm_name(name)] = missing
    return out


class TestNoEnvironmentStructuralDamage:
    """★ 环境不得处于"元数据完好、代码被半删"或"dist-info 被掏空"状态。

    这两类是同一根因（进程在批量删除中途被杀）的两个表现。它们最阴险之处在于
    **所有常规体检手段都显示正常**：``pip check`` 通过、``pip install -r``
    说"已满足"、版本号也对得上——直到某条代码路径真的 import 到那个包。
    """

    #: RECORD 里列了、但本机**确实不必存在**的条目。
    #: 键=PEP503 包名，值=理由（必须写清"为什么可以缺"，否则名单会退化成垃圾桶）。
    #:
    #: ⚠️ 这是**按解释器版本（+ 非 Windows 平台）分档**的表（见文件顶部的
    #: ``_ENV_SNAPSHOTS``），不能写成全局常量：不同环境的缺项不同，
    #: 混在一起必然"修好一个、弄红另一个"。
    #: Windows 3.10 列出 3 条良性缺项（matplotlib / pytest / pywin32，逐条实测无害）；
    #: 本地 3.12 与 CI Linux 3.10 均为空——这两个环境逐文件核对**零缺失**，
    #: 留空才能让本类在下次真出现半删包时立刻报警。
    RECORD_EXEMPT: dict = _RECORD_EXEMPT

    def test_scanner_is_not_vacuous(self):
        """防空真：必须真的扫到一批含 .py 的包，否则"无缺失"是假的。"""
        from importlib.metadata import distributions

        sp = _site_packages()
        counted = 0
        for dist in distributions():
            record = dist.read_text("RECORD") or ""
            if ".py" in record:
                counted += 1
        assert counted > 50, (
            f"只扫到 {counted} 个含 .py 的包（site-packages={sp}）——扫描器可能失效"
        )

    def test_no_dist_info_is_gutted(self):
        bad = _incomplete_dist_info()
        assert not bad, (
            "以下 dist-info 缺少 METADATA/RECORD（元数据被掏空，pip 与 "
            "importlib.metadata 都会看不见这些包）。修复："
            "`pip install --no-deps --ignore-installed <包>==<版本>`"
            "（**不要**用 --force-reinstall，其卸载阶段会触发沙箱批量删除守卫）：\n"
            + "\n".join(f"  {n}: 内含 {k}" for n, k in bad)
        )

    def test_no_record_listed_code_file_is_missing(self):
        missing = _record_missing_code_files()
        unexpected = {
            name: files
            for name, files in missing.items()
            if name not in self.RECORD_EXEMPT
        }
        assert not unexpected, (
            "以下包**元数据正常但代码文件缺失**（进程在卸载中途被杀留下的半删状态；"
            "`pip install -r requirements.txt` 会因'已满足'而跳过，修不了）：\n"
            + "\n".join(
                f"  {n}: 缺 {len(files)} 个，如 {files[:3]}" for n, files in sorted(unexpected.items())
            )
            + "\n\n修复：`pip install --no-deps --ignore-installed <包>==<版本>`"
        )

    def test_record_exempt_entries_are_documented(self):
        """豁免名单不许有裸条目。"""
        bad = sorted(k for k, v in self.RECORD_EXEMPT.items() if not str(v).strip())
        assert not bad, f"RECORD_EXEMPT 中以下条目没有写明理由：{bad}"

    def test_record_exempt_entries_are_still_needed(self):
        """豁免名单不许过期：登记了豁免的包，必须**当前真的**仍有缺失。"""
        actual = set(_record_missing_code_files())
        stale = sorted(set(self.RECORD_EXEMPT) - actual)
        assert not stale, f"以下包已无缺失，请从 RECORD_EXEMPT 删除：{stale}"


# ---------------------------------------------------------------------------
# 环境快照本身的结构守卫
# ---------------------------------------------------------------------------


class TestEnvSnapshotIsWellFormed:
    """守卫"按环境分档"这套机制本身，而不是某一档的内容。

    没有这一层，分档机制会以两种方式悄悄退化：

    1. 有人把两张表都清空 → 两边全绿，真实损伤被放过（典型"为了变绿而删断言"）；
    2. 有人改回全局单表 → 表面仍能跑，但换环境就红，重新变成打地鼠。
    """

    def test_current_interpreter_is_registered(self):
        """非真空：本机解释器必须在档位表里，否则模块导入时那条 assert 早已拦下。"""
        assert _ENV_KEY in _ENV_SNAPSHOTS, f"{_ENV_KEY} 未登记"

    def test_local_and_ci_envs_are_covered(self):
        """唯一本地环境和 CI Linux 环境必须分别登记。"""
        assert {"3.12", "3.10-linux"} <= set(_ENV_SNAPSHOTS), (
            f"本地与 CI 环境都应登记，实际只有 {sorted(_ENV_SNAPSHOTS)}"
        )

    def test_ci_linux_tier_is_covered(self):
        """CI 环境（Linux py3.10 全新安装）必须有独立档——防被静默删除后
        CI 回退到 Windows 本机档，又因"豁免过期"误红（2026-10-06 实测回归）。"""
        assert "3.10-linux" in _ENV_SNAPSHOTS, (
            f"CI Linux 档缺失，实际只有 {sorted(_ENV_SNAPSHOTS)}"
        )

    def test_every_snapshot_has_the_two_required_keys(self):
        bad = [
            key
            for key, snap in _ENV_SNAPSHOTS.items()
            if set(snap) != {"deviations", "record_exempt"}
        ]
        assert not bad, f"以下档位缺少/多出键（应为 deviations + record_exempt）：{bad}"

    def test_every_snapshot_is_a_dict(self):
        bad = [
            key
            for key, snap in _ENV_SNAPSHOTS.items()
            if not isinstance(snap.get("deviations"), dict)
            or not isinstance(snap.get("record_exempt"), dict)
        ]
        assert not bad, f"以下档位的值不是 dict：{bad}"

    def test_all_reasons_are_documented(self):
        """两档里每条登记都必须写明理由——不许裸豁免。"""
        bare: list[str] = []
        for key, snap in _ENV_SNAPSHOTS.items():
            for table_name in ("deviations", "record_exempt"):
                for pkg, reason in snap[table_name].items():
                    if not str(reason).strip():
                        bare.append(f"{key}/{table_name}/{pkg}")
        assert not bare, f"以下登记没有写明理由：{bare}"

    def test_resolved_tables_come_from_the_current_env(self):
        """★ 证明"接线"真的是按环境解析的，而不是被写死成某个字面量。

        环境快照按解释器与平台分别维护，所以"解析结果 == 当前档位的内容"这条断言是有信息量的：
        如果谁把 `_KNOWN_LOCAL_DEVIATIONS` 改回硬编码，两档必然对不上。
        """
        assert _KNOWN_LOCAL_DEVIATIONS is _SNAPSHOT["deviations"]
        assert _RECORD_EXEMPT is _SNAPSHOT["record_exempt"]
        assert TestNoEnvironmentStructuralDamage.RECORD_EXEMPT is _SNAPSHOT["record_exempt"]

    # "3.12" 与 "3.10-linux" 当前均无已知偏差，但分档机制仍保留：
    # 未登记环境不能因空表而静默通过。
