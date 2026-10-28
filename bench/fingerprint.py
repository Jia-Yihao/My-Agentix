"""环境指纹：每份 bench 结果的可复现性底座。

**为什么强制**：T1 卡的核心验收是"每次测量强制落环境指纹"——没有指纹的读数
无法判断能否与历史对比（换台机器、换个 SDK 版本，p95 就换了含义）。指纹块
必须**机器可读地写进每份结果文件**（JSON 顶层 ``fingerprint``），只打印到
stdout 等于没记。

字段取值域里的两个刻意决定：

- 温度：与被测模型无关的对象显式记 ``not_applicable`` 并附判据——省略字段会让
  下游无法区分"没记"与"不适用"；触碰模型的对象**必须**显式给温度，缺了就
  响亮报错（不许静默猜默认温度）。
- SDK 版本：未安装的包记 ``None``（缺失是事实，可观察），不跳过、不填假值。
"""

from __future__ import annotations

import os
import platform
import sys
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from typing import Any, Dict, Optional

#: 与唯一实现同源取尺子标识，不在这里重新描述秩算式（纪律 6 ④：单源）
from evals.perf_stats import QUANTILE_CONVENTION

#: 尺子的模块路径标识（reviewer 核对"分位数实现来源"时对照 evals/perf_stats.py）
QUANTILE_SOURCE = "evals.perf_stats.nearest_rank_quantile"

#: 方法学第 4 条：重复次数下限。<5 的样本没有可发布的尾部（T2 同一口径）。
MIN_REPEATS = 5

#: 验收②"SDK 版本"的落点：LLM/Agent 侧 SDK + 平台自身 + Web 框架。
#: 未安装的包记 None——"这个环境里压根没有它"本身就是指纹的一部分。
KEY_SDK_PACKAGES = (
    "agentix",
    "openai",
    "anthropic",
    "langchain",
    "langchain-openai",
    "langgraph",
    "fastapi",
)

#: 落盘器与守卫共用的必需字段清单（缺任一 = 指纹不完整 = 拒绝写出）
REQUIRED_FINGERPRINT_KEYS = (
    "measured_at",
    "target",
    "entry",
    "python_version",
    "sdk_versions",
    "temperature",
    "repeats",
    "os_platform",
    "cpu",
    "quantile_source",
    "quantile_convention",
)


def sdk_versions() -> Dict[str, Optional[str]]:
    """关键依赖版本快照；未安装记 ``None``，不省略键（省略=没查过）。"""
    out: Dict[str, Optional[str]] = {}
    for name in KEY_SDK_PACKAGES:
        try:
            out[name] = version(name)
        except PackageNotFoundError:
            out[name] = None
    return out


def collect_fingerprint(
    *,
    target: str,
    entry: str,
    repeats: int,
    touches_model: bool,
    temperature: Optional[float] = None,
) -> Dict[str, Any]:
    """构建一次测量的指纹块。

    :param touches_model: 被测对象是否驱动模型采样接口。判据由 target 自己声明，
        不由调用方临场猜——同一判定只许一个源。
    :raises ValueError: repeats 低于方法学下限，或触碰模型却没给温度。
    """
    if repeats < MIN_REPEATS:
        raise ValueError(
            f"repeats={repeats} 低于方法学下限 {MIN_REPEATS}（重复 <5 没有可发布的尾部）"
        )
    if temperature is None:
        if touches_model:
            raise ValueError(
                f"target {target!r} 触碰模型采样接口，必须显式提供 temperature——"
                "静默猜默认温度是指纹最常见的失真源"
            )
        temp_value: Any = "not_applicable"
        temp_basis = (
            "被测对象不驱动任何模型采样接口（离线假件/进程内计时/纯 HTTP 往返），"
            "target 元数据声明 touches_model=False"
        )
    else:
        temp_value = float(temperature)
        temp_basis = "调用方经 --temperature 显式提供"

    cpu_model = platform.processor() or None
    return {
        # 方法学第 5 条：任何读数必须自带测量日期（ISO8601, UTC）
        "measured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "target": target,
        # 被测入口：逐字写"调的是哪个原接口"，收纳≠重写
        "entry": entry,
        "python_version": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "sdk_versions": sdk_versions(),
        "temperature": temp_value,
        "temperature_basis": temp_basis,
        "repeats": repeats,
        "os_platform": platform.platform(),
        # platform.processor() 在 Windows 给 CPU 标识串、Linux 可能为空串；
        # 空归 None + 附 hostname，读数环境仍可定位。
        "cpu": {"count": os.cpu_count(), "model": cpu_model},
        "hostname": platform.node(),
        "quantile_source": QUANTILE_SOURCE,
        "quantile_convention": QUANTILE_CONVENTION,
    }
