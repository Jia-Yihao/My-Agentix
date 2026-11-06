"""T15：kb_ingest 逐行吞吐基准（离线可复现）——走 ``flatten_row`` 原接口。

规划出处：§4.3 T15 卡（验收③
「kb_ingest 吞吐基准可复现」）。

**测的是什么（一句话钉死，防止读数被引用成别的东西）**：``kb_ingest`` 处理**一行**
数据的**确定性转换**耗时——``flatten_row(row, config)`` 把一行的列名归一化（去括号
后缀、剔 ``Unnamed`` 列、同名取非空）并拼成 ``key: value`` 文本。一个样本 = 顺序扁平化
``--rows`` 行（缺省 200），行/秒由池化 mean 换算，分位数由 ``bench.aggregate`` 池化。

**为什么是这条缝**：``DataProcessor._process_row`` 的政策分支（``USE_LLM=false``，
以及 ``LLM_FALLBACK_TO_FLATTEN=true`` 的兜底）对每一行都走这个函数，它是
kb_ingest 入库链上**唯一**不依赖外部服务、可离线重复的逐行工作量。计时在 bench
层，理由与 ``bench/cancel_propagation.py`` 同款：``flatten_row`` 自己**没有**任何
计时 API，也没有测量模式；bench 只包一次原接口调用的首尾，不重写归一化逻辑、
不做第二份扁平化实现。

**没测什么（读数的边界，引用时必须连着这句一起引）**：

- 不含 LLM 改写（``USE_LLM=true`` 时每行一次上游调用，量级由上游决定，本机无 key）；
- 不含 embedding 请求与 Milvus 写入（``_embed_and_store`` / ``VectorStoreWriter``）；
- 不含 MySQL 读取（``MySQLIngestor`` 的 ``read_sql_query``）与分批；
- ⇒ 这是**CPU 侧逐行转换吞吐**，不是「kb_ingest 一小时能入库多少条」的端到端吞吐，
  更不是线上结论。端到端吞吐需要 MySQL + Milvus + 模型 key，本文件不伪造那条读数。

**分位数、指纹、落盘全走骨架既有实现**：``bench.aggregate.pooled_summary`` →
``evals.perf_stats.nearest_rank_quantile``；``bench.fingerprint.collect_fingerprint``；
``bench.results.write_result``（无指纹不写出）。``rows_per_second`` 由池化 **mean**
换算（均值不是分位数，不经第二把尺子）；两者各自可复核，不互相推导。

用法::

    .venv/Scripts/python.exe -m bench.kb_ingest_throughput --repeats 5 --rows 200
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_REPO_ROOT = str(Path(__file__).resolve().parents[1])
if _REPO_ROOT not in sys.path:  # 兼容非 -m 方式直跑
    sys.path.insert(0, _REPO_ROOT)

from bench.aggregate import pooled_summary  # noqa: E402
from bench.fingerprint import MIN_REPEATS, QUANTILE_SOURCE, collect_fingerprint  # noqa: E402
from bench.results import DEFAULT_RESULTS_DIR, write_result  # noqa: E402
from bench.targets import SkipNoEnvironment  # noqa: E402

TARGET_NAME = "kb_ingest_flatten"

#: 逐字记录调的是哪个原接口（进指纹的 ``entry`` 字段）
ENTRY = (
    "kb_service.services.utils:flatten_row(row, load_config())"
    "（kb_ingest 逐行扁平化；USE_LLM=false 与 LLM 兜底路径的同一函数）"
)

#: 读数口径：被测对象的边界（下游引用读数时必须连着这句一起引）
MEASUREMENT_SCOPE = (
    "顺序扁平化 --rows 行（缺省 200）的墙钟毫秒数；行/秒由池化 mean 换算。"
    "只含 flatten_row 的列名归一化与字符串拼接；不含 LLM 改写、embedding、"
    "Milvus 写入、MySQL 读取。"
)

DEFAULT_ROWS = 200

#: kb_ingest 的包根：``kb_ingest/kb_service``。``kb_ingest`` 目录本身不是包，
#: 容器里也是把它放进 sys.path 后按 ``kb_service.*`` 导入的。
_KB_INGEST_ROOT = Path(_REPO_ROOT) / "kb_ingest"


def _fixture_row() -> Dict[str, Any]:
    """确定性的一行（12 列）：覆盖归一化的四条分支，且零随机。

    刻意包含：带括号后缀的重复列（触发行名 strip 循环）、``Unnamed: 0``（应被剔除）、
    空值与 ``-``（应被跳过）、一条长文本（让字符串拼接占真实比例）。
    行宽固定 12 列——行宽一改读数就换含义，所以它同时写进结果文件的 notes。
    """
    return {
        "Unnamed: 0": 17,
        "公司名称": "示例科技有限公司",
        "公司名称（曾用名）": "",
        "报告年份": 2024,
        "发文机关": "示例省市场监督管理局",
        "文号": "示例监管〔2024〕12 号",
        "效力层级": "地方规范性文件",
        "发布日期": "2024-06-01",
        "实施日期": "-",
        "正文摘要": (
            "为规范示例行业的经营活动，依据有关法律法规，结合本地区实际，"
            "制定本规定。本规定适用于在示例区域内从事相关经营活动的市场主体，"
            "自发布之日起施行。"
        ),
        "附件数量": 3,
        "备注": None,
    }


def _load_entry() -> Tuple[Any, Any]:
    """导入被测函数与生产配置（导入成本在采样前发生）。

    ``kb_service`` 只能从 ``kb_ingest/`` 解析——那是它自己的包根，不是 agentix 的。
    导入失败（缺 pandas 等）属环境缺失 ⇒ ``SkipNoEnvironment``，不伪造读数。
    """
    if str(_KB_INGEST_ROOT) not in sys.path:
        sys.path.insert(0, str(_KB_INGEST_ROOT))
    try:
        from kb_service.core.config import load_config
        from kb_service.services.utils import flatten_row
    except ImportError as exc:  # 缺依赖 = 没环境，不是失败伪装成成功
        raise SkipNoEnvironment(
            f"kb_service 无法导入（{exc}）⇒ 本机缺 kb_ingest 依赖，未实测"
        ) from exc
    return flatten_row, load_config()


def _config_facts(config: Any) -> Dict[str, Any]:
    """把影响读数的配置值抄进结果文件（行宽之外的另一半可比性前提）。"""
    return {
        "flat_sep": getattr(config, "flat_sep", None),
        "flat_kv_sep": getattr(config, "flat_kv_sep", None),
        "flat_max_len": getattr(config, "flat_max_len", None),
    }


def _one_sample_ms(flatten_row: Any, config: Any, rows: int, row: Dict[str, Any]) -> float:
    """一个样本：顺序扁平化 ``rows`` 行，返回总毫秒数。

    每行重新拷一份字典（``dict(row)``）——真实调用点拿到的也是逐行字典，
    共用同一对象会悄悄省掉一部分开销。
    """
    t0 = time.perf_counter()
    for _ in range(rows):
        flatten_row(dict(row), config)
    return (time.perf_counter() - t0) * 1000.0


def collect_samples(repeats: int, rows: int) -> Tuple[List[float], Dict[str, Any]]:
    """跑 ``repeats`` 轮、每轮一个「``rows`` 行」样本，原始样本全量交回池化。

    每轮开始前预热 1 行：首次调用的惰性 import / 正则缓存属启动成本，
    留在样本里会让第一轮系统性偏慢。同时返回本次实际生效的配置事实。
    """
    flatten_row, config = _load_entry()
    row = _fixture_row()
    flatten_row(dict(row), config)  # 预热

    samples: List[float] = []
    for _ in range(repeats):
        samples.append(_one_sample_ms(flatten_row, config, rows, row))
    return samples, _config_facts(config)


def _skipped_payload(reason: str, repeats: int) -> Dict[str, Any]:
    """跳过态：有指纹、无读数（与 ``bench.run`` 的 skipped 形态同形）。"""
    return {
        "fingerprint": collect_fingerprint(
            target=TARGET_NAME, entry=ENTRY, repeats=repeats, touches_model=False
        ),
        "target": TARGET_NAME,
        "data_class": "benchmark_measurement",
        "measurement_status": "skipped_no_environment",
        "skip_reason": reason,
        "measurement_scope": MEASUREMENT_SCOPE,
        "results": {
            "pooled_quantiles_ms": None,
            "pooled_status": "not_measured_skipped",
            "pooled_note": reason,
            "per_repeat": [],
            "notes": None,
        },
        "out_dir": str(DEFAULT_RESULTS_DIR),
    }


def build_payload(argv: Optional[List[str]] = None) -> Dict[str, Any]:
    parser = argparse.ArgumentParser(
        prog="python -m bench.kb_ingest_throughput",
        description="T15：kb_ingest 逐行扁平化吞吐（复用 bench 骨架的分位数/指纹/落盘）",
    )
    parser.add_argument(
        "--repeats", type=int, default=MIN_REPEATS,
        help=f"轮数（方法学下限 {MIN_REPEATS}，低于下限直接拒绝）",
    )
    parser.add_argument(
        "--rows", type=int, default=DEFAULT_ROWS,
        help=f"每轮样本扁平化的行数（缺省 {DEFAULT_ROWS}；行数进结果文件，不可默默改）",
    )
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_RESULTS_DIR)
    args = parser.parse_args(argv)

    if args.repeats < MIN_REPEATS:
        parser.error(
            f"--repeats={args.repeats} 低于方法学下限 {MIN_REPEATS}"
            "（重复 <5 没有可发布的尾部，与 bench/README.md 铁律 3 同口径）"
        )
    if args.rows < 1:
        parser.error(f"--rows={args.rows} 必须 ≥1")

    try:
        samples_ms, config_facts = collect_samples(args.repeats, args.rows)
    except SkipNoEnvironment as exc:
        payload = _skipped_payload(str(exc), args.repeats)
        payload["out_dir"] = str(args.out_dir)
        return payload

    fingerprint = collect_fingerprint(
        target=TARGET_NAME,
        entry=ENTRY,
        repeats=args.repeats,
        touches_model=False,  # 纯本地字符串处理，零网络
    )
    pooled = pooled_summary(samples_ms)

    # 吞吐由池化 mean 换算（均值不是分位数，不走第二把尺子）；mean 为 0 时不伪装
    mean_ms = pooled["mean"]
    rows_per_second = round(args.rows * 1000.0 / mean_ms, 1) if mean_ms else None

    return {
        "fingerprint": fingerprint,
        "target": TARGET_NAME,
        "data_class": "benchmark_measurement",
        "measurement_status": "completed",
        "skip_reason": None,
        "measurement_scope": MEASUREMENT_SCOPE,
        "results": {
            "pooled_quantiles_ms": pooled,
            "pooled_status": "pooled_from_raw_samples",
            "pooled_note": (
                f"{args.repeats} 轮 × 每轮 {args.rows} 行的原始样本（共 "
                f"{pooled['count']} 个）并池后取最近邻秩——非各轮分位数再平均"
            ),
            "rows_per_repeat": args.rows,
            "rows_per_second": rows_per_second,
            "rows_per_second_note": (
                f"由池化 mean（{mean_ms} ms / {args.rows} 行）换算："
                f"{args.rows}*1000/mean。均值不是分位数，不参与分位数口径"
            ),
            "per_repeat": [
                {"repeat": i, "rows": args.rows, "sample_ms": round(sample, 4)}
                for i, sample in enumerate(samples_ms)
            ],
            "quantile_source": QUANTILE_SOURCE,
            "notes": {
                "what_is_measured": (
                    "kb_service.services.utils.flatten_row 的逐行确定性转换"
                    "（列名归一化 + 跳过空值 + 拼接）；一行 = 一次调用"
                ),
                "row_shape": "12 列固定 fixture（括号后缀重复列 / Unnamed / 空值 / 长文本）",
                "config": config_facts,
                "warmup": "每轮开始前预热 1 行；样本不含首次 import 与正则缓存构建",
                "excluded": (
                    "不含 LLM 改写 / embedding 请求 / Milvus 写入 / MySQL 读取 ⇒ "
                    "这是逐行 CPU 转换吞吐，不是端到端入库吞吐"
                ),
                "comparability": (
                    "读数只在本指纹（Python/CPU/OS）+ 同一行宽（12 列）+ 同一 "
                    "flat_max_len 下可比；行宽与截断上限都会改数量级"
                ),
            },
        },
        "out_dir": str(args.out_dir),
    }


def _print(text: str) -> None:
    """控制台输出兜底：只替换**该编码装不下**的字符（如 ``⇒`` 在 GBK 下会抛
    ``UnicodeEncodeError``，让一次成功写出结果文件的运行以崩溃收场）。
    中文在 GBK 下可编码，因此不会被替换。"""
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    payload = text.encode(encoding, errors="replace").decode(encoding, errors="replace")
    sys.stdout.write(payload + chr(10))


def main(argv: Optional[List[str]] = None) -> int:
    payload = build_payload(argv)
    out_dir = Path(payload.pop("out_dir"))
    path = write_result(payload, out_dir=out_dir)
    status = payload["measurement_status"]
    _print(f"[bench] target={payload['target']} status={status}")
    if status == "completed":
        results = payload["results"]
        pooled = results["pooled_quantiles_ms"]
        _print(
            "[bench] rows/round={rows} p50={p50} p95={p95} mean={mean} (ms) "
            "rows_per_second={rps}".format(
                rows=results["rows_per_repeat"],
                p50=pooled["p50"], p95=pooled["p95"], mean=pooled["mean"],
                rps=results["rows_per_second"],
            )
        )
    else:
        _print(f"[bench] 未实测：{payload['skip_reason']}")
    _print(f"[bench] 结果（含指纹块）已写入 {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
