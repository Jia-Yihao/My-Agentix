# -*- coding: utf-8 -*-
"""P44：缩略图缓存语义（``Cache-Control``）与条件请求守卫。

背景（2026-09 深挖审计）——此前审计只到"有没有设 Cache-Control"这一层，
**没有审"设得对不对"**。实测发现 ``THUMBNAIL_CACHE_CONTROL`` 原值是::

    public, max-age=86400, immutable

它被用在 ``GET /documents/{document_id}/thumbnail`` 上，而该路由是**带鉴权**的：
``Depends(get_current_user)`` + ``require_document_readable(session, document_id,
current_user.id)``。于是这两个指令各自都是错的：

1. ``public`` —— 允许**共享缓存**（CDN / 反向代理 / 中间代理）留存并复用响应。
   鉴权粒度是"每文档 × 每用户"，共享缓存无法复制这份判定 ⇒ A 用户的缩略图
   会被回给 B 用户。

2. ``immutable`` —— 向浏览器承诺"在 max-age 内本 URL 的字节永不变化"。但缩略图
   **没有**按内容做版本化：``generate_document_thumbnail`` 每次都写回同一个 key
   ``thumbs/{document_id}.webp`` 并**原地覆盖**（``old_key == key``，不会走
   ``delete_object`` 分支），响应取用的 object key 又来自可变的
   ``documents.thumbnail_key`` 列。承诺不成立 ⇒ 浏览器在整个 max-age（24h）内
   不再校验，这就是**陈旧窗口**的来源，而且是 24 小时量级。

修法分两半，缺一不可：

- 常量收紧为 ``private, max-age=3600``：``private`` 挡掉跨用户共享，
  1h 把最坏陈旧时间从 86400s 压到 3600s。
- **路由补上 ETag/If-None-Match → 304 分支**：否则收紧 max-age 会把"24h 内零请求"
  变成"每小时整图重下"，是带宽/延迟回退。有了 304，过期后的校验是零 S3 GET 的廉价探测。
  该分支**必须放在 ``stream_get`` 之前**——放在之后等于先取了一次对象体再丢掉，
  不仅没省，还会泄漏一个未消费的流。

所以本文件守两件事：**(a) 常量本身的指令语义**，**(b) 304 分支与 ``stream_get``
的先后顺序**（这条是"少了就没意义"的承重不变量，最容易在重构中被打乱）。
"""

from __future__ import annotations

import ast
import fnmatch
import importlib
import os
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

AGENTIX_ROOT = REPO_ROOT / "agentix"
KB_INGEST_ROOT = REPO_ROOT / "kb_ingest"

#: ``Cache-Control`` 的合法指令词表（RFC 9111 §5.2）。用于**识别**一段字符串
#: 是不是 Cache-Control 值——只有"全部 token 都是已知指令"才认定，避免把普通
#: 文案（如 ``"public, private 都写一遍"``）误判成缓存指令。
KNOWN_DIRECTIVES = frozenset(
    {
        "public",
        "private",
        "no-cache",
        "no-store",
        "no-transform",
        "must-revalidate",
        "proxy-revalidate",
        "must-understand",
        "only-if-cached",
        "immutable",
        "max-age",
        "s-maxage",
        "stale-while-revalidate",
        "stale-if-error",
    }
)

#: 单 token 就能**唯一**指向 Cache-Control 的指令。``public`` / ``private`` 故意**不在**
#: 此列——它们单独出现时更可能是别的领域的枚举值（如可见性 ``"private"``），
#: 那种情况交由 §3.2 的"引用解析"那条线判定，避免误伤。
CACHE_EXCLUSIVE_DIRECTIVES = frozenset(
    {
        "immutable",
        "no-store",
        "no-cache",
        "must-revalidate",
        "proxy-revalidate",
        "no-transform",
        "only-if-cached",
        "must-understand",
        "max-age",
        "s-maxage",
        "stale-while-revalidate",
        "stale-if-error",
    }
)

#: 允许出现 ``public`` / ``immutable`` 的 ``Cache-Control`` **值**对应的**文件**。
#: 按文件粒度登记（与扫描粒度一致），每条必须写明"无鉴权 + 内容寻址"的理由。
#: 本仓库目前唯一合法场景：vite build 产物（文件名带内容 hash，URL 变 = 字节变，
#: immutable 承诺成立；且是纯静态资源，无任何鉴权语义）。
PUBLIC_IMMUTABLE_ALLOWLIST: dict[str, str] = {
    "web/nginx.conf": (
        "仅 location /assets/（vite build 带 hash 的静态产物）：内容寻址 + 无鉴权，"
        "immutable + 一年长缓存是标准做法；index.html 与 /api 不在此 location，不受影响"
    ),
}


# ---------------------------------------------------------------------------
# 1. 判定逻辑（先自测，再用它守全仓）
# ---------------------------------------------------------------------------

_DIRECTIVE_RE = re.compile(r"^([a-z-]+)\s*(?:=.*)?$")


def _parse_directives(value: str) -> list[str] | None:
    """把一段字符串解析成 Cache-Control 指令名列表；不像 Cache-Control 则返回 None。"""
    tokens = [tok.strip() for tok in value.split(",") if tok.strip()]
    if not tokens:
        return None
    names: list[str] = []
    for tok in tokens:
        m = _DIRECTIVE_RE.match(tok)
        if not m:
            return None
        name = m.group(1)
        if name not in KNOWN_DIRECTIVES:
            return None
        names.append(name)
    return names


def _looks_like_cache_control_literal(value: str) -> list[str] | None:
    """判断一个**字面量**是否够像 Cache-Control 值。

    门槛刻意收紧：要么是 ``>=2`` 个指令（如 ``"public, max-age=86400, immutable"``），
    要么是单个"只可能属于 Cache-Control"的指令（如 ``"no-store"``）。
    单个 ``"public"`` / ``"private"`` 不算——见 ``CACHE_EXCLUSIVE_DIRECTIVES`` 的说明。
    """
    names = _parse_directives(value)
    if names is None:
        return None
    if len(names) >= 2 or names[0] in CACHE_EXCLUSIVE_DIRECTIVES:
        return names
    return None


def _cache_control_violations(value: str) -> list[str]:
    """返回该 Cache-Control 值在**带鉴权资源**上的违规指令列表。"""
    names = _parse_directives(value)
    if names is None:
        return []
    bad: list[str] = []
    if "public" in names:
        bad.append("public")
    if "immutable" in names:
        bad.append("immutable")
    return bad


def _max_age_seconds(value: str) -> int | None:
    for tok in value.split(","):
        tok = tok.strip()
        if tok.startswith("max-age="):
            try:
                return int(tok.split("=", 1)[1].strip())
            except ValueError:
                return None
    return None


class TestDetectorSelfTest:
    """守卫自身必须先被证明有牙——否则 §2/§3 的扫描是**真空断言**。"""

    def test_detector_flags_the_old_value(self):
        """旧值必须被判为违规（这就是我们实际修掉的那个）。"""
        assert _cache_control_violations("public, max-age=86400, immutable") == [
            "public",
            "immutable",
        ]

    def test_detector_clears_the_new_value(self):
        assert _cache_control_violations("private, max-age=3600") == []

    def test_detector_handles_single_directives(self):
        assert _cache_control_violations("private") == []
        assert _cache_control_violations("immutable") == ["immutable"]
        assert _cache_control_violations("public") == ["public"]
        assert _cache_control_violations("no-store") == []

    def test_detector_ignores_prose_that_merely_mentions_directives(self):
        """散文里出现 public/immutable 不能误判——否则守卫会被自己的注释绊倒。"""
        prose = "缩略图响应不能是 public，也不能是 immutable；改用 private。"
        assert _parse_directives(prose) is None
        assert _looks_like_cache_control_literal(prose) is None
        assert _cache_control_violations(prose) == []

    def test_detector_is_case_sensitive_like_the_protocol(self):
        """HTTP 指令是小写；``Public`` 不是合法指令，因此不算"看起来像 Cache-Control"。"""
        assert _parse_directives("Public, max-age=60") is None

    def test_literal_gate_rejects_lone_public_but_accepts_the_old_combo(self):
        """点明收紧门槛的理由：单个 ``public`` 可能是枚举值，组合形态才是缓存值。"""
        assert _looks_like_cache_control_literal("public") is None
        assert _looks_like_cache_control_literal("private") is None
        assert _looks_like_cache_control_literal("public, max-age=86400, immutable") == [
            "public",
            "max-age",
            "immutable",
        ]
        assert _looks_like_cache_control_literal("no-store") == ["no-store"]


# ---------------------------------------------------------------------------
# 2. 常量本身的语义
# ---------------------------------------------------------------------------


class TestThumbnailCacheControlValue:
    def _value(self) -> str:
        from agentix.retrieval.usecases.thumbnail_service import THUMBNAIL_CACHE_CONTROL

        return THUMBNAIL_CACHE_CONTROL

    def test_is_private_not_public(self):
        assert "private" in self._value(), (
            "缩略图路由带鉴权（get_current_user + require_document_readable），"
            "响应必须标记 private，禁止共享缓存留存"
        )
        assert "public" not in self._value()

    def test_is_not_immutable(self):
        assert "immutable" not in self._value(), (
            "缩略图内容按同一 object key 原地覆盖，URL 的字节会变，"
            "不能承诺 immutable（否则浏览器整个 max-age 内不再校验）"
        )

    def test_max_age_is_bounded(self):
        age = _max_age_seconds(self._value())
        assert age is not None, "缺少 max-age，浏览器将退化为每次都回源"
        assert 0 < age <= 3600, (
            f"max-age={age} 过大：陈旧窗口等于该值。原值 86400 的陈旧窗口达 24h，"
            "上限收紧到 3600s"
        )

    def test_no_placeholder_residue(self):
        """防止有人把它改成 TODO/占位符后守卫仍然全绿。"""
        value = self._value()
        assert "TODO" not in value and "{" not in value and "}" not in value


# ---------------------------------------------------------------------------
# 3. 全仓扫描
# ---------------------------------------------------------------------------


def _iter_python_sources() -> list[Path]:
    roots = [p for p in (AGENTIX_ROOT, KB_INGEST_ROOT) if p.exists()]
    out: list[Path] = []
    for root in roots:
        out.extend(sorted(root.rglob("*.py")))
    return out


def _parse_source(path: Path) -> ast.Module | None:
    try:
        return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError):  # pragma: no cover - 语法错误另有守卫
        return None


# --- 3.1 字面量扫描（形态已收紧，不会误判枚举值） -------------------------------


def _cache_control_literals(path: Path) -> list[tuple[int, str]]:
    tree = _parse_source(path)
    if tree is None:
        return []
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if _looks_like_cache_control_literal(node.value) is not None:
                hits.append((node.lineno, node.value))
    return hits


class TestNoWideOpenCacheControlLiteral:
    def test_at_least_one_cache_control_literal_exists(self):
        """非真空：扫描器必须真的找到东西（现在至少缩略图那个常量）。"""
        found = [hit for p in _iter_python_sources() for hit in _cache_control_literals(p)]
        assert found, "扫描器没找到任何 Cache-Control 字面量——探针已失效，需修扫描器"

    def test_finds_the_thumbnail_constant_specifically(self):
        """点名确认它能抓到我们知道存在的那一个。"""
        target = AGENTIX_ROOT / "retrieval" / "usecases" / "thumbnail_service.py"
        values = [v for _, v in _cache_control_literals(target)]
        assert "private, max-age=3600" in values

    def test_no_public_or_immutable_unless_allowlisted(self):
        offenders: list[str] = []
        for path in _iter_python_sources():
            rel = path.relative_to(REPO_ROOT).as_posix()
            if rel in PUBLIC_IMMUTABLE_ALLOWLIST:
                continue
            for lineno, value in _cache_control_literals(path):
                bad = _cache_control_violations(value)
                if bad:
                    offenders.append(f"{rel}:{lineno} -> {value!r} （违规指令: {', '.join(bad)}）")
        assert not offenders, (
            "发现 public/immutable 缓存指令。public 会让共享缓存跨用户复用；"
            "immutable 会让浏览器在 max-age 内永不校验。若确为无鉴权 + 内容寻址的静态资源，"
            "请登记进 PUBLIC_IMMUTABLE_ALLOWLIST 并写明理由：\n" + "\n".join(offenders)
        )

    def test_allowlist_entries_are_not_stale(self):
        """白名单空着也要能被检查：登记了却不存在对应文件 = 陈旧白名单。"""
        stale = [rel for rel in PUBLIC_IMMUTABLE_ALLOWLIST if not (REPO_ROOT / rel).exists()]
        assert not stale, f"PUBLIC_IMMUTABLE_ALLOWLIST 指向不存在的文件: {stale}"


# --- 3.2 引用解析：抓"值写成常量名"那种间接形态（本项目缩略图就是这种） -----------

_CACHE_CONTROL_KEY = "cache-control"


def _module_level_string_constants() -> dict[str, tuple[str, int, str]]:
    """``常量名 -> (文件, 行号, 字符串值)``，仅收模块级、值为字符串字面量的赋值。"""
    registry: dict[str, tuple[str, int, str]] = {}
    for path in _iter_python_sources():
        tree = _parse_source(path)
        if tree is None:
            continue
        rel = path.relative_to(REPO_ROOT).as_posix()
        for node in tree.body:  # 只看模块级
            targets: list[ast.expr] = []
            if isinstance(node, ast.Assign):
                targets = list(node.targets)
                value = node.value
            elif isinstance(node, ast.AnnAssign):
                targets = [node.target]
                value = node.value
            else:
                continue
            if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
                continue
            for tgt in targets:
                if isinstance(tgt, ast.Name):
                    registry[tgt.id] = (rel, node.lineno, value.value)
    return registry


def _cache_control_sites() -> list[tuple[str, int, str | None, str | None]]:
    """找出所有"把值塞进 Cache-Control"的位置。

    返回 ``(文件, 行号, 已解析的值 或 None, 未解析的常量名 或 None)``。
    覆盖两种形态：``{"Cache-Control": X}`` 字典字面量、``headers["Cache-Control"] = X`` 下标赋值。
    """
    registry = _module_level_string_constants()
    sites: list[tuple[str, int, str | None, str | None]] = []

    def _resolve(expr: ast.expr) -> tuple[str | None, str | None]:
        if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
            return expr.value, None
        if isinstance(expr, ast.Name):
            hit = registry.get(expr.id)
            if hit is not None:
                return hit[2], None
            return None, expr.id
        return None, f"<{type(expr).__name__}>"

    def _is_key(expr: ast.expr) -> bool:
        return (
            isinstance(expr, ast.Constant)
            and isinstance(expr.value, str)
            and expr.value.lower() == _CACHE_CONTROL_KEY
        )

    for path in _iter_python_sources():
        tree = _parse_source(path)
        if tree is None:
            continue
        rel = path.relative_to(REPO_ROOT).as_posix()
        for node in ast.walk(tree):
            if isinstance(node, ast.Dict):
                for key, val in zip(node.keys, node.values):
                    if key is not None and _is_key(key):
                        resolved, unresolved = _resolve(val)
                        sites.append((rel, node.lineno, resolved, unresolved))
            elif isinstance(node, ast.Assign):
                for tgt in node.targets:
                    if isinstance(tgt, ast.Subscript) and _is_key(tgt.slice):
                        resolved, unresolved = _resolve(node.value)
                        sites.append((rel, node.lineno, resolved, unresolved))
    return sites


class TestNoWideOpenCacheControlViaConstant:
    def test_finds_the_thumbnail_header_site(self):
        """非真空 + 点名：必须能解析出缩略图那处（它走的是常量名）。"""
        sites = _cache_control_sites()
        assert sites, "没找到任何 Cache-Control 头设置点——探针已失效"
        resolved = {v for _, _, v, _ in sites if v}
        assert "private, max-age=3600" in resolved, (
            f"未能解析出缩略图的缓存值，实际解析到：{sorted(resolved)}"
        )

    def test_constant_registry_knows_the_thumbnail_constant(self):
        registry = _module_level_string_constants()
        assert "THUMBNAIL_CACHE_CONTROL" in registry
        assert registry["THUMBNAIL_CACHE_CONTROL"][2] == "private, max-age=3600"

    def test_all_header_values_are_resolvable(self):
        """有值解析不出来 ⇒ 守卫存在**静默盲区**，必须登记成字面量常量。"""
        unresolved = [f"{rel}:{lineno} -> {name}" for rel, lineno, _, name in _cache_control_sites() if name]
        assert not unresolved, (
            "以下 Cache-Control 头的值无法静态解析，守卫会漏过它。"
            "请把该值定义成模块级字符串常量（本文件按名解析）：\n" + "\n".join(unresolved)
        )

    def test_no_site_gets_a_wide_open_value(self):
        offenders = [
            f"{rel}:{lineno} -> {value!r}"
            for rel, lineno, value, _ in _cache_control_sites()
            if rel not in PUBLIC_IMMUTABLE_ALLOWLIST and value and _cache_control_violations(value)
        ]
        assert not offenders, "Cache-Control 头出现 public/immutable：\n" + "\n".join(offenders)


# ---------------------------------------------------------------------------
# 4. 承重不变量：304 分支必须在 stream_get 之前
# ---------------------------------------------------------------------------


def _load_thumbnail_route_ast() -> ast.FunctionDef:
    src = (AGENTIX_ROOT / "retrieval" / "api" / "routers" / "documents.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "api_get_document_thumbnail":
            return node
    raise AssertionError("documents.py 里找不到 api_get_document_thumbnail —— 路由被改名/删除了？")


def _called_names(stmt: ast.stmt) -> set[str]:
    return {
        n.func.id if isinstance(n.func, ast.Name) else (
            n.func.attr if isinstance(n.func, ast.Attribute) else ""
        )
        for n in ast.walk(stmt)
        if isinstance(n, ast.Call)
    }


class TestConditionalRequestPrecedesStreamFetch:
    def test_route_calls_etag_matcher_and_stream_get(self):
        """非真空：两个调用都得在，否则下面的顺序断言毫无意义。"""
        func = _load_thumbnail_route_ast()
        all_names = {n for stmt in func.body for n in _called_names(stmt)}
        assert "_etag_matches" in all_names, "路由未做 If-None-Match 校验，304 分支丢失"
        assert "stream_get" in all_names, "路由未调用 stream_get —— 测试锚点失效"

    def test_etag_check_runs_before_stream_get(self):
        """★ 承重不变量：先判 304 再取流。

        若把校验挪到 ``stream_get`` 之后，就等于"先拉整个对象体、再决定丢弃"——
        既不省 S3 GET，又多泄漏一个未消费的流。
        """
        func = _load_thumbnail_route_ast()
        etag_idx = next(
            (i for i, stmt in enumerate(func.body) if "_etag_matches" in _called_names(stmt)),
            None,
        )
        stream_idx = next(
            (i for i, stmt in enumerate(func.body) if "stream_get" in _called_names(stmt)),
            None,
        )
        assert etag_idx is not None and stream_idx is not None
        assert etag_idx < stream_idx, (
            f"ETag 校验（语句 #{etag_idx}）必须在 stream_get（语句 #{stream_idx}）之前"
        )

    def test_etag_check_returns_304(self):
        """校验命中后必须返回 304，而不是继续往下走。"""
        func = _load_thumbnail_route_ast()
        for stmt in func.body:
            if "_etag_matches" not in _called_names(stmt):
                continue
            assert isinstance(stmt, ast.If), "ETag 校验应写成 if 分支"
            returned = [
                rt
                for rt in ast.walk(stmt)
                if isinstance(rt, ast.Return) and isinstance(rt.value, ast.Call)
            ]
            assert returned, "ETag 命中分支里没有 return"
            src = ast.unparse(returned[0])
            assert "Response" in src and "304" in src, f"该分支未返回 304：{src}"
            return
        raise AssertionError("未找到 ETag 校验分支")

    def test_route_uses_the_shared_constant_not_a_literal(self):
        """防止有人绕过常量、在路由里另写一个字面量（双真源）。"""
        src = (AGENTIX_ROOT / "retrieval" / "api" / "routers" / "documents.py").read_text(encoding="utf-8")
        assert "THUMBNAIL_CACHE_CONTROL" in src
        literals = [v for _, v in _cache_control_literals(AGENTIX_ROOT / "retrieval" / "api" / "routers" / "documents.py")]
        assert not literals, f"路由文件里出现了裸的 Cache-Control 字面量，应用常量：{literals}"


# ---------------------------------------------------------------------------
# 5. _etag_matches 的行为（RFC 9110 §13.1.2 弱比较）
# ---------------------------------------------------------------------------


class TestEtagMatches:
    @staticmethod
    def _fn():
        mod = importlib.import_module("agentix.retrieval.api.routers.documents")
        return mod._etag_matches

    @pytest.mark.parametrize(
        "header,etag,expected",
        [
            # 缺失任一侧 ⇒ 保守返回 200
            (None, '"abc"', False),
            ("", '"abc"', False),
            ('"abc"', None, False),
            ("", "", False),
            # 精确命中（S3 的 ETag 带引号）
            ('"abc"', '"abc"', True),
            ("abc", '"abc"', True),
            # 弱标记前缀应被忽略（浏览器可能回传 W/"abc"）
            ('W/"abc"', '"abc"', True),
            ('"abc"', 'W/"abc"', True),
            # 通配符
            ("*", '"abc"', True),
            ("*", None, False),
            # 多 tag 逗号列表里命中任意一个
            ('"x", "abc", "y"', '"abc"', True),
            ('"x", "y"', '"abc"', False),
            ("* , \"abc\"", '"abc"', True),
            # 不匹配
            ('"zzz"', '"abc"', False),
            # 不做子串比较：前缀相同但不同值不得命中
            ('"abc"', '"abcdef"', False),
        ],
    )
    def test_matrix(self, header, etag, expected):
        assert self._fn()(header, etag) is expected

    def test_does_not_substring_match(self):
        """点出易犯的错：``in``/``startswith`` 会把 "abc" 判成命中 "abcdef"。"""
        fn = self._fn()
        assert fn('"abc"', '"abcdef"') is False


# ---------------------------------------------------------------------------
# 6. 补掉守卫自身的盲区：非 Python 的缓存配置
# ---------------------------------------------------------------------------
#
# 上面 §3 两条扫描**只覆盖 `agentix/` 与 `kb_ingest/` 下的 `.py`**——
# 而缓存策略完全可以在 Python 之外声明：nginx 的 `expires` / `proxy_cache_valid`、
# 反代配置、`Dockerfile` 里给静态资源加的头、CI 里的 CDN 规则。
# 实测当前仓库**没有任何**这类配置（前端由后端托管，无 nginx 缓存层），
# 所以这是一个**尚未被利用的盲区**，而不是已存在的问题。
#
# 既然它现在为空，正好可以把扫描扩到配置文件并让"空"变成一条有意义的断言：
# 将来谁加了一条 `immutable` 的 nginx 规则，这里会立刻红。
#
# **本扫描不覆盖什么（诚实说明）**：只看**行文本**，不解析 nginx/YAML 语法；
# 也不检查运行时动态下发的缓存头（那属于应用代码，由 §3 的两条扫描负责）。

#: 可能声明缓存策略的非 Python 文件（glob 集合）
_CACHE_CONFIG_GLOBS = (
    "Dockerfile",
    "Dockerfile.*",
    "*.conf",
    "*.nginx",
    "docker-compose*.yml",
    "docker-compose*.yaml",
    "vite.config.*",
    "package.json",
)

#: 扫描时要跳过的目录名（虚拟环境 / 依赖 / 产物 / 缓存 / 本地临时目录）
_SKIP_DIR_PARTS = frozenset(
    {
        ".venv",
        "node_modules",
        ".git",
        "dist",
        "build",
        "__pycache__",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        "site-packages",
        "archive",
        # `.tmp/` 被 `.gitignore` 的 `*.tmp` 规则忽略＝不随仓库交付，本守卫只约束
        # 交付源码。且 pytest `--basetemp` 指到仓库内时，上面那个合成违规样例就落在
        # 这里——不排除会让「无违规」这条守卫被测试自己的临时产物搞成随机红。
        ".tmp",
    }
)


def _iter_cache_config_files() -> list[Path]:
    """仓库内可能声明缓存策略的非 Python 文件（已排除 venv / 依赖 / 产物）。

    ⚠️ 用 ``os.walk`` + **剪枝** 而不是 ``Path.rglob``：``rglob`` 无法跳过子树，
    会先走完 ``node_modules``（数万文件）才排除，实测让本组测试从 <1s 涨到 **67s**。
    剪枝后遍历量与仓库源码规模同阶。
    """
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(REPO_ROOT):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIR_PARTS]
        for name in filenames:
            if any(fnmatch.fnmatch(name, pattern) for pattern in _CACHE_CONFIG_GLOBS):
                found.append(Path(dirpath) / name)
    return sorted(found)


def _config_cache_violations() -> list[str]:
    """在配置文件里找"宽开放缓存"指令，返回 ``["rel:line -> 原文", ...]``。"""
    offenders: list[str] = []
    for path in _iter_cache_config_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        if rel in PUBLIC_IMMUTABLE_ALLOWLIST:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:  # pragma: no cover - 读不了就不判，避免噪声
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            low = line.lower().strip()
            if not low or low.startswith("#"):
                continue
            # `immutable` 在配置语境里几乎只可能是缓存指令
            if "immutable" in low:
                offenders.append(f"{rel}:{lineno} -> {line.strip()!r}（immutable）")
                continue
            # `Cache-Control` 行里出现 public
            if "cache-control" in low and "public" in low:
                offenders.append(f"{rel}:{lineno} -> {line.strip()!r}（public）")
    return offenders


class TestNoWideOpenCacheInConfigFiles:
    """补掉"只扫 .py"的盲区：配置层也不许出现宽开放缓存。"""

    def test_scanner_finds_config_files(self):
        """非真空：必须真的扫到配置文件，否则"无违规"是恒真的空话。"""
        files = _iter_cache_config_files()
        assert files, (
            "扫描器一个配置文件都没找到——glob 或跳过规则有问题，"
            "此时「无违规」是真空断言，必须修扫描器"
        )

    def test_scanner_excludes_virtualenvs_and_deps(self):
        """反向：扫描结果里不得混入 venv / node_modules（否则既慢又全是假信号）。"""
        leaked = [
            p.relative_to(REPO_ROOT).as_posix()
            for p in _iter_cache_config_files()
            if set(p.relative_to(REPO_ROOT).parts) & _SKIP_DIR_PARTS
        ]
        assert not leaked, f"扫描结果混入了被排除目录：{leaked}"

    def test_no_wide_open_cache_directive_in_config(self):
        offenders = _config_cache_violations()
        assert not offenders, (
            "配置文件里出现宽开放缓存指令。若确为**无鉴权 + 内容寻址**的静态资源，"
            "请登记进 PUBLIC_IMMUTABLE_ALLOWLIST 并写明理由；"
            "若是带鉴权的资源，改为 private 且不要 immutable：\n" + "\n".join(offenders)
        )

    def test_detector_has_teeth_on_synthetic_samples(self, tmp_path):
        """证明侦测逻辑真的会报——否则上面的"无违规"是恒真。"""
        sample = tmp_path / "nginx.conf"
        sample.write_text(
            "location /static/ {\n"
            "    add_header Cache-Control \"public, immutable\";\n"
            "}\n",
            encoding="utf-8",
        )
        text = sample.read_text(encoding="utf-8")
        hits = [
            line
            for line in text.splitlines()
            if "immutable" in line.lower()
            or ("cache-control" in line.lower() and "public" in line.lower())
        ]
        assert hits, "合成的违规样例竟然没被规则识别——本类的判定逻辑已失效"
