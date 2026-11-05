"""T3「默认题集缺失 ⇒ 响亮退出」契约的最小自动化覆盖。

**为什么需要这条覆盖**：``evals/golden_source.py`` 把六个评测入口的 ``--golden``
缺省串统一成「默认文件不存在 ⇒ 打印可用清单 + 改道示例 + 非零退出」。此前这条契约
只靠人工核对，而任一入口丢掉 ``ensure_default_golden`` 调用都**不会自己报错**：
``ragas_report.py`` / ``feedback_ingest.py`` 的读集函数对缺失文件返回 ``[]``，
于是退化成「空样本 + 退出 0」——看的人以为评测跑过了，正是本机制要消灭的收法。
六处接线形状相同（约定 6 ④：同一个判定不许两个源），所以用一个文件同时钉住两侧：

1. 缺省缺失且扫描到可用集：返回 ``(None, MISSING_GOLDEN_EXIT)``；stderr 含一份
   **真扫出来的**可用集文件名，以及 ``missing_golden_message`` 逐字写出的改道示例
   （只断言关键片段，不抄整段长文）；
2. 显式传值（哪怕文件不存在）原样返回、退出码 0——「响亮退出」不得误伤显式调用；
3. 六个评测入口的 ``--golden`` argparse 缺省值全为 ``None``：实造 parser 再
   ``parse_args``，而不是在源码文本上做字符串匹配。

**不依赖宿主机题集**：那份"可用的题集"由 ``tmp_path`` 现场造，``default`` 指向
``tmp_path`` 下不存在的路径；``golden_source`` 的扫描根就是 ``default.parent``，
所以用例结果与仓库里现存的题集无关。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import List, Tuple

import pytest

from evals.golden_source import MISSING_GOLDEN_EXIT, ensure_default_golden

REPO_ROOT = Path(__file__).resolve().parents[1]

#: (入口标识, 相对仓库根的路径, 让 ``build_parser().parse_args(...)`` 通过所需的 argv)
#: ``local_kb_demo`` 的 ``--golden`` 只挂在 ``eval`` 子命令上，根级没有该 dest，
#: 因此给它一个子命令；``feedback_ingest`` 的 ``--input`` 是必填，同理补上。
ENTRIES: List[Tuple[str, str, List[str]]] = [
    ("evals/run_eval.py", "evals/run_eval.py", []),
    ("evals/hybrid_eval.py", "evals/hybrid_eval.py", []),
    ("evals/ragas_report.py", "evals/ragas_report.py", []),
    ("evals/golden_to_promptfoo.py", "evals/golden_to_promptfoo.py", []),
    ("evals/feedback_ingest.py", "evals/feedback_ingest.py", ["--input", "probe.jsonl"]),
    ("scripts/demo/local_kb_demo.py", "scripts/demo/local_kb_demo.py", ["eval"]),
]


def _load_entry_module(alias: str, rel_path: str) -> ModuleType:
    """按文件路径加载入口脚本（``scripts/`` 不是包，不能按模块名 import）。"""
    spec = importlib.util.spec_from_file_location(alias, REPO_ROOT / rel_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclass 装饰器在模块执行期要反查 sys.modules[cls.__module__]，必须先登记。
    sys.modules[alias] = module
    spec.loader.exec_module(module)
    return module


def test_missing_default_golden_exits_loudly(tmp_path, capsys):
    """缺省缺失 ⇒ ``(None, 2)``，且 stderr 既有可用集文件名也有逐字改道示例。"""
    available = tmp_path / "probe_golden.jsonl"
    available.write_text('{"id": "C001"}\n{"id": "C002"}\n', encoding="utf-8")
    default = tmp_path / "legal_golden.jsonl"  # 刻意不创建：模拟题集已下线
    assert not default.exists()

    golden, code = ensure_default_golden(None, entry="evals/run_eval.py", default=default)

    assert golden is None
    assert code == MISSING_GOLDEN_EXIT
    assert code == 2
    err = capsys.readouterr().err
    # 清单项来自 default.parent 的真实扫描（不是硬编码名单），至少含现场造的这份。
    assert available.name in err
    # 改道示例逐字对齐 missing_golden_message 的拼接方式：入口名 + --golden + 可用集路径。
    assert "改道示例" in err
    assert f".venv/Scripts/python.exe evals/run_eval.py --golden {available}" in err


def test_explicit_value_passes_through_even_if_missing(tmp_path, capsys):
    """显式传值原样返回、退出码 0：响亮退出只针对缺省路径。"""
    absent = tmp_path / "absent_golden.jsonl"
    default = tmp_path / "legal_golden.jsonl"
    assert not absent.exists()

    for explicit in (str(absent), [str(absent)]):
        golden, code = ensure_default_golden(
            explicit, entry="evals/run_eval.py", default=default
        )
        assert golden is explicit
        assert code == 0
    # 显式路径下不打任何提示：行为与改动前逐位一致。
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize(
    ("entry", "rel_path", "argv"), ENTRIES, ids=[spec[0] for spec in ENTRIES]
)
def test_golden_default_is_none_in_every_entry(entry, rel_path, argv):
    """六个入口的 ``--golden`` 缺省值必须都是 ``None``（缺省解析交给 golden_source）。"""
    module = _load_entry_module(f"_t3_entry_{Path(rel_path).stem}", rel_path)
    args = module.build_parser().parse_args(argv)
    assert args.golden is None, f"{entry} 的 --golden 缺省值不是 None"
