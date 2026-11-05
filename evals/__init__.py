"""评估工具链（P2-1）。

本包只包含**评估**相关资产，不参与生产运行链路：

* ``run_eval.py`` —— 批量跑分 + 确定性打分 + CSV/summary 输出
* ``quality_gate.py`` —— 质量门禁（PASS / FAIL / NOT-ARMED / NOT-ENFORCED 四态）
* ``trajectory_regression.py`` —— 轨迹前缀回归
* ``README.md`` —— 构造、扩充与回流流程说明

2026-10-26 起仓内**评测集已下线**（法律金标集与录制基线删除，CI 质量门禁机位撤除）；
工具链本身原样保留，接一份新评测集即可重新武装。
"""
