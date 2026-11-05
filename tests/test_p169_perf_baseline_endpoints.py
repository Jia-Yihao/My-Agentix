"""T16：perf-baseline 扩关键业务端点的形状守卫（p169）。

覆盖（三条验收）：

1. 三个关键业务端点进了**夜间 job** ``business-p95`` 的压测清单；
2. 新端点不挂 PR 触发器——workflow 顶层没有 ``pull_request``，且该 job 用
   ``github.event_name == 'schedule'`` 钉死在夜间 cron（手动 dispatch 也不跑）；
3. 新端点首周走 bootstrap 口径：``PERF_ENFORCE_FROM`` 之前的劣化只 ``::warning``
   不阻断，到期后同一判定 ``sys.exit(1)``——切档靠日期，**不靠** ``continue-on-error``。

另加三条「不弱化既有守卫」的钉：
- 阈值口径未动：全文件 ``--max-regression`` 只有 ``0.20`` 这一个值，业务端点侧的
  ``PERF_MAX_REGRESSION`` 与它逐字相等；
- liveness 真阻断链路的形状仍在（``/liveness``、200 请求 / 10 workers / warmup 20、
  ``--direction lower_is_better``），且它的首跑建档声明 ``--allow-bootstrap`` 没被摘掉
  ——**那一行不是本卡新增**（属早前批次），这条钉守的是"别人改门禁默认值时不会连带删掉它"；
  业务端点基线走独立 cache 前缀，不与它互相恢复。
- 失败样本必须阻断：``errors > 0``（等价 ``success != requests``）判红，且该 slug **不得**
  推进基线。``scripts/load_test.py`` 的超时/异常记进 ``errors`` 且**不追加延迟样本**，
  只看 ``status_codes`` 会把"部分请求打不通"的**低报** p95 判成 PASS 并写成下一版参照。

判据一律是**结构**（YAML 解析后取 job / step 的 run 文本），不做全文件 `in`：
``/liveness`` 在 workflow 里出现多次（起服务探活 + 压测目标），全文件搜索分不清
"哪一处"——而"哪一处"正是本文件要钉住的东西。
"""

from __future__ import annotations

import datetime
import re
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = PROJECT_ROOT / ".github" / "workflows" / "perf-baseline.yml"

#: 端点清单（slug → URL 片段）必须落在夜间 job 的压测清单里。
#: sessions 的 URL 带 ``user_id`` 查询参数且保留结尾斜杠：缺参数会 422、
#: 缺斜杠会先吃一次 307 跳转——两种都不是被监控的那段读路径。
BUSINESS_ENDPOINTS = {
    "metrics": "http://127.0.0.1:8000/metrics",
    "sessions": "http://127.0.0.1:8000/api/v1/sessions/?user_id=perf-baseline&limit=50",
    "chat_routes": "http://127.0.0.1:8000/api/v1/chat/routes",
}

#: 首周只报告口径的到期日（含当天转阻断）。改这个日期 = 改门禁强度 ⇒ 必须同步本行。
BOOTSTRAP_UNTIL = datetime.date(2026, 11, 4)


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))


def _triggers() -> dict:
    """``on:`` 在 YAML 1.1 里是布尔键 True（PyYAML 按规范解析）。"""
    data = _workflow()
    return data.get(True, data.get("on", {}))


def _job(name: str) -> dict:
    data = _workflow()
    assert name in data["jobs"], f"workflow 里没有 {name} job"
    return data["jobs"][name]


def _step(job: dict, name: str) -> dict:
    for step in job["steps"]:
        if step.get("name") == name:
            return step
    raise AssertionError(
        f"job 里没有名为 {name!r} 的 step（现有 {[s.get('name') for s in job['steps']]}）"
    )


def _step_run(job: dict, name: str) -> str:
    run = _step(job, name).get("run")
    assert isinstance(run, str) and run.strip(), f"{name} 没有 run 脚本"
    return run


def test_business_endpoints_are_in_the_nightly_job() -> None:
    """三条验收之①：关键业务端点真的在夜间 job 的压测清单里。"""
    run = _step_run(_job("business-p95"), "Run load test per business endpoint")
    for slug, url in BUSINESS_ENDPOINTS.items():
        assert f"{slug}|{url}" in run, f"夜间 job 的清单里没有 {slug}（{url}）"

    # 防"清单一改就空转"：清单行数与端点数一致（slug|url 形式，只数数据行）。
    data_lines = [ln.strip() for ln in run.splitlines() if "|http" in ln]
    assert len(data_lines) == len(BUSINESS_ENDPOINTS), data_lines


def test_new_endpoints_hang_on_nightly_cron_only() -> None:
    """三条验收之②：不挂 PR 触发器——顶层无 pull_request，job 再钉死 schedule。"""
    triggers = _triggers()
    assert "pull_request" not in triggers, "perf-baseline 出现了 PR 触发器：新端点不得挂 PR"
    assert "schedule" in triggers, "夜间 cron 被删了，新端点就没有挂载面了"

    job = _job("business-p95")
    assert (
        job.get("if") == "github.event_name == 'schedule'"
    ), f"业务端点 job 必须显式只跑夜间 cron；当前 if={job.get('if')!r}"

    # 两套口径不混：liveness job 里不许出现业务端点（否则等于给真阻断链路加端点）。
    p95_text = yaml.dump(_job("p95"), allow_unicode=True)
    for slug, url in BUSINESS_ENDPOINTS.items():
        assert url not in p95_text, f"业务端点 {slug} 混进了 liveness 真阻断 job"


def test_max_regression_threshold_is_unchanged() -> None:
    """边界：不改既有阈值——全文件只有 0.20 这一个账面值，业务侧与它逐字相等。"""
    text = WORKFLOW_PATH.read_text(encoding="utf-8")
    values = re.findall(r"--max-regression\s+([0-9.]+)", text)
    # 只钉"没有任何别的值"：注释里复述同一个 0.20 不算第二套口径，
    # 但出现 0.21/0.99 这类新数值就是悄悄换尺子。
    assert values and set(values) == {"0.20"}, f"--max-regression 的取值变了：{values}"

    liveness_compare = _step_run(_job("p95"), "Compare with previous baseline")
    assert (
        liveness_compare.count("--max-regression 0.20") == 1
    ), "liveness 比对步骤的阈值必须仍是一处 0.20"

    business_env = _job("business-p95")["env"]
    assert business_env["PERF_MAX_REGRESSION"] == "0.20", (
        "业务端点侧的预算必须与既有 --max-regression 同一个数，" "否则等于悄悄换了一把尺子"
    )


def test_bootstrap_week_is_explicit_and_expires() -> None:
    """三条验收之③：首周只报告、到期转真阻断，且判定只有一份实现。"""
    job = _job("business-p95")
    enforce_from = datetime.date.fromisoformat(job["env"]["PERF_ENFORCE_FROM"])
    assert enforce_from == BOOTSTRAP_UNTIL, (
        f"首周窗口改了：{enforce_from}" "（本测试与 workflow 注释同步改，等于登记一次门禁强度变更）"
    )

    run = _step_run(job, "Compare business endpoints with previous baseline")
    # 判定复用唯一实现：不在这里另写一份"劣化多少算劣化"。
    assert "from evals.quality_gate import evaluate_metric" in run
    assert 'direction="lower_is_better"' in run
    # 到期日必须真参与判定，而不是只写在注释里。
    assert re.search(r"today\(\)\s*>=\s*enforce_from", run), "到期日没有进入比较逻辑"
    # 两档语义都要在脚本里：到期后阻断、到期前只报告。
    assert "sys.exit(1)" in run
    assert "::warning" in run and "bootstrap-week" in run

    # 合取本身要钉住：把 `if regressions and enforcing:` 改成 `if regressions:` 之后，
    # 上面每一条子串断言**仍然是绿的**（`sys.exit(1)`、`::warning`、`bootstrap-week` 都还在），
    # 而首周就变成了真阻断——子串存在性钉不住"谁来当闸门"。所以钉这一整行。
    assert "if regressions and enforcing:" in run, (
        "到期前不阻断的合取被拆了：少了 `and enforcing` 等于首周就真阻断"
    )
    # 首周分支还得**可达**且顺序正确：合取之外要有一条独立的 `if regressions:` 兜住
    # "有回归但未到期"，而且它必须排在写基线之前（排到之后就成了"判红照样写基线"）。
    blocking_branch = run.index("if regressions and enforcing:")
    first_week_branch = run.index("if regressions:", blocking_branch)
    write_baseline = run.index("baseline_path.write_text")
    assert blocking_branch < first_week_branch < write_baseline, (
        "首周只告警分支不在阻断分支之后、或落到了写基线之后"
    )

    # 刻意不用 continue-on-error 表达"首周不阻断"：那会让到期后的红也一直是绿的。
    assert "continue-on-error" not in yaml.dump(job, allow_unicode=True)


def test_failed_samples_block_the_slug_from_advancing_baseline() -> None:
    """失败样本必须判红，且判红的 slug 不得推进基线。

    事实链（只读 ``scripts/load_test.py`` 确认，不改它）：超时/异常进 ``errors`` 且
    **不追加延迟样本**（``_Worker.run`` 只在拿到响应之后才 append），``success`` 只是
    成功样本数。于是"200 个请求里 60 个超时"时 p95 只由那 140 个存活样本算出——读数比
    真实情况漂亮得多。比对脚本若只看 ``status_codes`` / ``p95_seconds``，这个**低报值**
    会被判 PASS，然后被写成下一版参照：一次静默低报 + 一次基线污染。
    """
    run = _step_run(_job("business-p95"), "Compare business endpoints with previous baseline")

    # 判据在：读的是压测报告里失败样本数，不是只看状态码。
    assert 'report.get("errors")' in run, "比对脚本没有读失败样本数 errors"
    assert "errors > 0" in run, "没有按失败样本数判红（只剩状态码一道闸）"

    # 位置在：失败判据必须落在脚本里**两处** `baseline[slug] = p95` 之前，并用 `continue`
    # 收口，否则"判红"只是打一行注解，读数照样被写成下一版参照。
    # 两处各是什么：① 建档分支（`previous is None` 时写入首个读数）；② 比对通过分支
    # （`verdict["ok"]` 时推进）。失败 slug 走不到 ②——它在上面的失败分支就 `continue` 了，
    # ② 对失败 slug **不可达**；而这个不可达由「失败分支的 continue 排在 ① 之前」+
    # 「① 排在 ② 之前」两条一起保证，所以下面两处都要锚住。
    # 收口判据要**贴着本分支**判：往前找到下一个语句锚点（读 p95 那一行）为止，
    # 断言这段里出现 `continue`。用"到写基线为止"会误吃下一个失败分支的 continue。
    errors_check = run.index("errors > 0")
    p95_read = run.index('p95 = report.get("p95_seconds")', errors_check)
    # 两处分别锚（`index` 只认第一处 ①，`rindex` 认最后一处 ②）：合并成一次 `index`
    # 等于只钉住 ①，注释却宣称管两处——那种"注释比断言宽"正是本条要修的东西。
    advance_bootstrap = run.index("baseline[slug] = p95")
    advance_pass = run.rindex("baseline[slug] = p95")
    assert advance_pass > advance_bootstrap, (
        "脚本里只剩一处 `baseline[slug] = p95`：建档分支或比对通过分支少了一个，"
        "本条守卫的前提变了，请同步核对上面的注释与锚点"
    )
    assert "continue" in run[errors_check:p95_read], "失败分支没有 continue，会继续走到写基线"
    assert errors_check < p95_read < advance_bootstrap, (
        "失败判据落在推进基线之后：低报读数照样会被写成下一版参照"
    )
    # ② 也必须排在失败判据之后（顺序上已被 ① < ② 蕴含，这里显式钉一次：
    # 防止日后有人把比对通过分支整体挪到失败分支之前）。
    assert errors_check < advance_pass, "比对通过分支的写基线挪到了失败判据之前"


def test_business_baseline_is_persisted_and_separate() -> None:
    """业务端点基线必须跨运行持久化，且不与 liveness 基线互相恢复。"""
    job = _job("business-p95")
    cache_steps = [
        step for step in job["steps"] if str(step.get("uses", "")).startswith("actions/cache")
    ]
    assert cache_steps, "business-p95 没有 actions/cache：每晚都会退化成首跑建档"
    cache = cache_steps[0]["with"]
    assert cache["path"] == "perf-cache"
    assert cache["key"].startswith("perf-business-baseline-")
    assert cache["restore-keys"].strip().startswith("perf-business-baseline-")
    # 发布动作必须真存在（否则 cache 里永远是空目录）。
    assert _step(job, "Publish new business baseline")["run"].strip()


def test_liveness_chain_shape_survives_and_keeps_bootstrap_declaration() -> None:
    """既有守卫不弱化：liveness 仍是 200 请求 / 10 workers / warmup 20 + 显式建档声明。

    名字不再写 ``is_untouched``：本卡**没有**逐位拷一张 liveness 链路的快照，
    ``--allow-bootstrap`` 那一行本身就是早前批次加进来的。这条钉守的是三件事各自还在
    原位（压测参数、方向、建档声明），不是"整段一字未动"。
    """
    liveness_run = _step_run(_job("p95"), "Run load test")
    assert "--url http://127.0.0.1:8000/liveness" in liveness_run
    assert "--requests 200 --workers 10 --warmup 20" in liveness_run

    compare = _step_run(_job("p95"), "Compare with previous baseline")
    assert "--direction lower_is_better" in compare
    assert "--allow-bootstrap" in compare, "liveness 的首跑建档声明被摘了"
