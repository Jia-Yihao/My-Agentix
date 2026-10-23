"""搭建讲解 notebook 使用的 Jupyter 内核（`docs/notebooks/`）。

用法（仓库根目录）：
    .venv/Scripts/python.exe scripts/setup_notebook_kernel.py

ipykernel 及其依赖装进 `.tmp/notebook_tools`（`--no-deps`），内核再通过 PYTHONPATH 引用该目录——
项目 venv 保持干净，notebook 运行期导入的仍是 `.venv` 里的项目依赖。

内核建好后：
    交互式  JUPYTER_PATH=.tmp/jupyter .venv/Scripts/python.exe -m jupyter lab docs/notebooks
    批量执行 JUPYTER_PATH=.tmp/jupyter python -m jupyter nbconvert --to notebook --execute --inplace \
        --ExecutePreprocessor.kernel_name=agentix "docs/notebooks/00-平台全景与一次请求调用链.ipynb"
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / ".tmp" / "notebook_tools"
KERNEL_DIR = ROOT / ".tmp" / "jupyter" / "kernels" / "agentix"

#: 项目 venv 缺失、ipykernel 运行必需的包；逐个 --no-deps 安装，避免污染 venv 依赖树
SCRATCH_PACKAGES = (
    "ipykernel",
    "comm",
    "debugpy",
    "jupyter_client",
    "jupyter_core",
    "pyzmq",
    "tornado",
)

KERNEL_JSON = {
    "argv": [
        sys.executable.replace("\\", "/"),
        "-m",
        "ipykernel_launcher",
        "-f",
        "{connection_file}",
    ],
    "display_name": "Agentix (.venv 3.12)",
    "language": "python",
    "env": {
        "PYTHONPATH": ";".join([str(ROOT).replace("\\", "/"), str(TOOLS).replace("\\", "/")]),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
        "PYTHONWARNINGS": "ignore",
        "MPLBACKEND": "Agg",
    },
}


def main() -> int:
    if sys.prefix == sys.base_prefix:
        print("请用项目虚拟环境的 python 运行本脚本", file=sys.stderr)
        return 1

    cmd = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--quiet",
        "--target",
        str(TOOLS),
        "--no-deps",
        *SCRATCH_PACKAGES,
    ]
    print("$", " ".join(cmd))
    subprocess.run(cmd, check=True)

    KERNEL_DIR.mkdir(parents=True, exist_ok=True)
    (KERNEL_DIR / "kernel.json").write_text(
        json.dumps(KERNEL_JSON, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"kernelspec -> {KERNEL_DIR / 'kernel.json'}")
    print("下一步：设置 JUPYTER_PATH=.tmp/jupyter 再启动 jupyter / nbconvert")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
