#!/usr/bin/env python
"""staging 配置矩阵生成器（项目实现手册 §24.2 G13 的终态形态）。

用法：
    python scripts/gen_staging_matrix.py             # 打印**生成**的矩阵（含手写列占位）
    python scripts/gen_staging_matrix.py --check      # 与 runbook 已提交的表逐行比对，漂移即退出 1

为什么需要它：`docs/runbooks/预发环境与配置矩阵.md` 的「配置矩阵」是运维照着做的那张表，
但它原先三列里只有第 1 列（键名）是硬事实，第 2、3 列靠人抄。本轮审计里它已经错过两次
（G13：矩阵写 `TOOL_SYNC_OFFLOAD_ENABLED` 的 staging=true 而 override 压根没设；
G13 第二例：矩阵写 `UVICORN_RELOAD`/`DB_ECHO` 的 staging=false 而 override 没钉，
实际取值由**宿主机** `.env` 决定）。人眼 diff 不是机制，所以把这张表变成生成物。

三列的事实来源各不相同，这也是这张表可信度的边界：
    dev      ← `docker-compose.yml` 的 backend 转发：`${K:-X}` 取 X；`${K:-}` 记「空」
               （转发但默认空 ⇒ 容器实际拿到空串，语义由 Settings/DEBUG 决定；这里刻意
               **不拿字段默认值去填空**——填了就把"没声明"写成"声明成默认值"）；
               只有 compose 压根没转发的键，才回落 `agentix.config.settings.Settings`
               的字段默认值（读 model_fields，不实例化）
    staging  ← `docker/compose.override.staging.yml` 的 backend environment（没设就是没设）
    生产列   ← **没有第三份文件**。它只有约定 + `STRICT_VALIDATION` 的拒绝启动兜底，
               所以本脚本不校验它，runbook 那段「可信度来源不一样」的说法继续成立

为什么事实源是这两份 YAML 的声明，而不是 `docker compose config` 的渲染输出（刻意的落点偏差）：
    1) 渲染会把**宿主机 `.env` 展开进 stdout**，而这张表要写进文档、`--check` 要进 CI——
       把真实密钥搬进终端与 CI 日志，是比表漂移更贵的事故；
    2) 若拿渲染输出当基准，被校验的对象就同时定义了基准：`.env` 里一个错误取值会让表
       和"用来校验表"的渲染结果一起错，`--check` 照样绿。校验要看见的是"部署物声明了
       什么"，不是"这台机器此刻算出什么"。

口径两条（都是机械的）：
    - override 里设了该键 ⇒ staging 单元格必须**等于**那个值，并且按本表约定加粗；
    - override 里没设 ⇒ staging 单元格**不许加粗**（加粗 = 声明"预发真的设了"）。
      后者是 G13 两例的共同形态：表里写了具体值而部署物里没有，等于给运维一张假图。

`MANUAL_KEYS` 是显式登记的例外：那些行的取值不是 compose 或字段默认值能算出来的，
必须留在人写的一侧，但它们**被点名**而不是被静默跳过——这是本脚本唯一允许"手填"的口子。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
COMPOSE = ROOT / "docker-compose.yml"
OVERRIDE = ROOT / "docker/compose.override.staging.yml"
RUNBOOK = ROOT / "docs/runbooks/预发环境与配置矩阵.md"
SERVICE = "backend"

# Windows 默认 GBK 控制台打不出中文行，会 UnicodeEncodeError 非零退出
# （与 scripts/bump_version.py 同一处理：强制 UTF-8，失败则降级为 replace，绝不因打印崩溃）。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - 打印能力不该决定校验结果
        pass

#: 不参与机器校验的行（键名 → 为什么只能手填）。写在这里而不是散落进 if 里，
#: 是为了让人一眼看到"这张表还有哪几格不是生成物"。
MANUAL_KEYS: dict[str, str] = {
    "SECRET_KEY": "取值由 resolved_secret_key 在运行期决定（缺失时临时生成并告警），不是字段默认值",
}

_VAR_RE = re.compile(r"^\$\{([A-Za-z0-9_]+):-(.*)\}$", re.DOTALL)


def _load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _env_map(doc: dict, service: str) -> dict[str, Any]:
    """compose 的 environment 允许 mapping 与 `- K=V` 列表两种写法，两边都得吃。"""
    env = (doc.get("services", {}).get(service, {}) or {}).get("environment") or {}
    if isinstance(env, dict):
        return dict(env)
    out: dict[str, Any] = {}
    for item in env:
        key, _, value = str(item).partition("=")
        out[key.strip()] = value.strip()
    return out


def _settings_default(key: str) -> Any:
    """读字段默认值，**不实例化 Settings**（实例化会去读宿主机环境，正是要避免的事）。"""
    sys.path.insert(0, str(ROOT))
    from agentix.config.settings import Settings

    field = Settings.model_fields.get(key)
    return None if field is None else field.default


def _norm_token(value: Any) -> str:
    """把"期望值"压成矩阵单元格的裸词：`False`→false、`None`/`""`→空、其余 str 原样。"""
    if value is None or value == "":
        return "空"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value).strip().lower()


def dev_claim(key: str, compose_raw: Any, settings_default: Any) -> tuple[str, str]:
    """dev 列 = 一次不带任何宿主机 `.env` 的 `docker compose up` 里该键的实际取值口径。"""
    if compose_raw is None:
        return _norm_token(settings_default), "compose 未转发 ⇒ Settings 字段默认值"
    text = str(compose_raw).strip()
    match = _VAR_RE.match(text)
    if match is None:
        return _norm_token(text), f"compose 写死为 {text!r}"
    inner = match.group(2).strip()
    if inner.startswith("${"):
        nested = _VAR_RE.match(inner)
        other = nested.group(1) if nested else "?"
        return "空", f"compose 是 ${{{match.group(1)}:-${{{other}:-…}}}} ⇒ 空值时由 {other} 推断"
    if inner == "":
        # `${K:-}` 渲染出来就是空串：这一格没钉死值，"空" 就是它的事实，
        # 不拿 Settings 默认值去填空（填了会把"没声明"写成"声明成默认值"）。
        return "空", "compose 转发但默认空 ⇒ 容器拿到空值，实际语义由 Settings/DEBUG 决定"
    return _norm_token(inner), f"compose 转发默认 {inner!r}"


def normalize_cell(cell: str) -> tuple[str, bool]:
    """矩阵单元格 → (裸词, 是否加粗)。括号里的说明、反引号、`**` 都不参与等式。"""
    bold = "**" in cell
    body = cell.replace("**", "").strip()
    body = re.split(r"[（(]", body, maxsplit=1)[0]
    return body.strip().strip("`").strip().lower(), bold


def generated_rows() -> list[dict[str, str]]:
    """表的覆盖面 = 「环境间差异项」，不是 compose 的全量 92 项。

    差异项由部署物自己声明：**override 里设了的键**就是差异项（dev 与 staging 取值不同，
    或显式钉住了一个不依赖宿主机环境的值）。想让一个 compose 键进这张表，就得先让
    override 也管它，或把它登记进 `MANUAL_KEYS` 并写清理由——两条路都是"机器看得见"。
    """
    compose_env = _env_map(_load_yaml(COMPOSE), SERVICE)
    override_env = _env_map(_load_yaml(OVERRIDE), SERVICE)
    rows = []
    for key in sorted(set(override_env) | set(MANUAL_KEYS)):
        if key in MANUAL_KEYS:
            rows.append(
                {"key": key, "dev": "（手填）", "staging": "（手填）", "pinned": False,
                 "dev_source": MANUAL_KEYS[key], "staging_source": MANUAL_KEYS[key],
                 "manual": "yes"}
            )
            continue
        claim, why = dev_claim(key, compose_env.get(key), _settings_default(key))
        if key in override_env:
            staging = _norm_token(override_env[key])
            note = f"override = {override_env[key]!r}"
            pinned = True
        else:
            staging = "空"
            note = "override 未设 ⇒ 该环境的实际取值由基线 compose / Settings 决定"
            pinned = False
        rows.append(
            {"key": key, "dev": claim, "staging": staging, "dev_source": why, "staging_source": note,
             "pinned": pinned, "manual": "no"}
        )
    return rows


def tracked_keys() -> set[str]:
    return {row["key"] for row in generated_rows()}


def runbook_matrix() -> dict[str, dict[str, str]]:
    """读 runbook 里那张表：`## 配置矩阵` 小节内、以 `` | `KEY` `` 开头的行。"""
    text = RUNBOOK.read_text(encoding="utf-8")
    section = text.split("## 配置矩阵", 1)
    if len(section) < 2:
        raise SystemExit("runbook 里没有「配置矩阵」小节，生成器无法定位")
    body = section[1].split("\n## ", 1)[0]
    out: dict[str, dict[str, str]] = {}
    for line in body.splitlines():
        stripped = line.strip()
        # 一个判据就够：键名带反引号才是矩阵的行。原先这里还叠了一条
        # `stripped.startswith("| `")`，两条判据同义，等于给解析器留了第二个"只关一条"的
        # 地方（变异测试里放宽正则被那条 startswith 挡回来，是个等价变异 ⇒ 看不出现实改动）。
        match = re.match(r"^\|\s*`([^`]+)`\s*\|", stripped)
        if not match:
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if len(cells) < 4:
            continue
        out[match.group(1)] = {"dev": cells[1], "staging": cells[2], "production": cells[3]}
    return out


def check(verbose: bool) -> list[str]:
    """把 runbook 那张表逐格对回部署物。返回漂移清单（空 = 一致）。"""
    problems: list[str] = []
    committed = runbook_matrix()
    override_env = _env_map(_load_yaml(OVERRIDE), SERVICE)
    rows = {row["key"]: row for row in generated_rows()}

    for key in sorted(set(rows) - set(committed)):
        problems.append(f"`{key}`：是环境差异项（override 里设了 / 已登记为手填），矩阵里却没有这一行")
    for key in sorted(set(committed) - set(rows)):
        # 这一行的两格都不是部署物的派生物，所以只能按"加粗 = override 真设了"这一条约定判：
        # 不加粗 → 只是没登记；加粗 → 表在给运维一张假图，那正是 G13 案发时的形态。
        _, bold = normalize_cell(committed[key]["staging"])
        if bold:
            problems.append(
                f"`{key}`：staging 列加粗声明了 {committed[key]['staging'].replace('**', '').strip()!r}，"
                f"但 override 里没有这一项（G13 的形态：表里写了值、部署物里没有）"
            )
        else:
            problems.append(
                f"`{key}`：矩阵里有这一行，但 override 没设它、也没登记进 MANUAL_KEYS "
                f"⇒ 它的 dev/staging 两格机器校验不了。要么让 override 也管这个键，"
                f"要么在脚本里登记理由"
            )

    for key, row in sorted(rows.items()):
        cell = committed.get(key)
        if cell is None or row["manual"] == "yes":
            continue
        dev, _ = normalize_cell(cell["dev"])
        staging, bold = normalize_cell(cell["staging"])
        if dev != row["dev"]:
            problems.append(
                f"`{key}` 的 dev 列 = {dev!r}，生成值 = {row['dev']!r}（依据：{row['dev_source']}）"
            )
        if key in override_env:
            if staging != row["staging"]:
                problems.append(
                    f"`{key}` 的 staging 列 = {staging!r}，override 实际 = {row['staging']!r}"
                )
            if not bold:
                problems.append(
                    f"`{key}`：override 真设了值，staging 列却没加粗——加粗是本表「预发确实设了」的记号"
                )
    if verbose and not problems:
        manual = sum(1 for row in rows.values() if row["manual"] == "yes")
        print(
            f"配置矩阵与部署物一致："
            f"{len(rows) - manual} 行由 override/Settings 生成，{manual} 行登记为手填"
        )
    return problems


def render() -> str:
    lines = [
        "| 配置 | dev | staging | production（**手写，无部署物兜底**） |",
        "|---|---|---|---|",
    ]
    for row in generated_rows():
        if row["manual"] == "yes":
            lines.append(
                f"| `{row['key']}` | （手填：{row['dev_source']}） | （手填） | （待人工补） |"
            )
            continue
        staging = f"**{row['staging']}**" if row["pinned"] else row["staging"]
        lines.append(f"| `{row['key']}` | {row['dev']} | {staging} | （待人工补：生产建议与理由） |")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="只比对，不打印生成表")
    args = parser.parse_args()
    if args.check:
        problems = check(verbose=True)
        if problems:
            print("配置矩阵漂移：")
            for problem in problems:
                print("  -", problem)
            print("\n要么改 runbook 的表，要么改部署物；不要只改一边。")
            return 1
        return 0
    print(render())
    return 0


if __name__ == "__main__":
    sys.exit(main())
