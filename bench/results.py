"""结果落盘器：**无指纹不写出**的执行点。

T1 验收①"产出的每份结果文件含指纹块"靠约定撑不住——闸必须落在唯一出口上。
本模块是 bench 结果的唯一落盘路径：顶层缺 ``fingerprint``、或指纹缺必需字段
（``fingerprint.REQUIRED_FINGERPRINT_KEYS``）⇒ ``ValueError``，不静默降级、
不补空指纹。

产物默认落 ``bench/results/``（已在 .gitignore 登记）：评测原始数据不进 git 是
T1 卡风险行的直接要求。
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

from bench.fingerprint import REQUIRED_FINGERPRINT_KEYS

#: 默认产物目录：仓库根/bench/results（gitignored；不在这里放任何入库承诺）
DEFAULT_RESULTS_DIR = Path(__file__).resolve().parent / "results"

_UNSAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def _assert_fingerprint(payload: Dict[str, Any]) -> None:
    fp = payload.get("fingerprint")
    if not isinstance(fp, dict) or not fp:
        raise ValueError(
            "结果缺 fingerprint 块——bench 拒绝落盘无指纹的测量"
            "（T1 验收①：每次测量强制落环境指纹）"
        )
    missing = [k for k in REQUIRED_FINGERPRINT_KEYS if k not in fp]
    if missing:
        raise ValueError(
            f"指纹块缺必需字段：{missing}——拒绝落盘，不补空指纹假装完整"
        )


def write_result(payload: Dict[str, Any], out_dir: Path = DEFAULT_RESULTS_DIR) -> Path:
    """写出一份带指纹的结果 JSON，返回落盘路径。

    文件名 = ``<target>-<UTC时间戳>.json``：同一入口重复跑不互相覆盖，
    每份文件自含"哪一次测量的指纹"。
    """
    _assert_fingerprint(payload)
    target = _UNSAFE_NAME_RE.sub("_", str(payload.get("target", "result")))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{target}-{stamp}.json"
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return path
