# AGENTS.md — My-Agentix 协作约定

> 本文件同时是 `agentix/agent/context/instructions.py` 指令级联的项目层来源
> （`LG_INSTRUCTIONS_ENABLED=true` 时注入 L2 general-query system 前缀）。
> 修改本文件 = 修改生产前缀，请保持本文件**稳定**：约定性的、低频变化的
> 内容放这里；随任务变化的内容放会话/任务描述里。

## 常用命令

```bash
pytest tests -q                                  # 全量回归（约 7 分钟）
pytest -m "not integration" -q --tb=short        # CI 口径（与全量等价）
ruff check agentix tests                         # lint（CI 同一把尺子）
ruff format agentix tests                        # 格式化
mypy agentix/domain                              # 类型检查（domain 簇 0 错门禁）
cd web && npm run lint && npm run test           # 前端 vue-tsc + vitest
```

## 硬性纪律

1. **新开关默认关**：新增 Settings flag 一律 `False`/`0` 起步；flag-off 路径必须与
   基线**逐位等价**（Strangler 惯例）。行为变更走「新 flag + 默认关 + 显式启用」。
2. **fail-open 边界**：指标/记忆/指令/预算等辅助子系统出错**绝不影响主链路**，
   捕获后降级（空串 / 跳过 / 回基线），并 `logger.warning` 留痕。
3. **指标标签有界**：新指标的 label 名必须先进
   `tests/test_p100_genai_observability.py` 的 `ALLOWED_METRIC_LABELS` 白名单；
   禁止 user/session/trace/问答内容等高基数标签进指标（只进 span）。
4. **配置键五处同步**：新增 Settings 字段必须同步——
   ① `agentix/config/settings.py` ② `agentix/config/selfcheck.py`
   （`DEFAULT_IGNORED_MISSING`）③ `.env.example` ④
   `docs/architecture/configuration.md` §5 表 ⑤
   `tests/test_p41_doc_consistency.py::test_new_settings_are_documented` 手写清单。
5. **改动前先问**：任何对数据、脚本、测试基线的改动需先征得维护者同意。

## 代码风格

- 注释/文档字符串用简体中文，说明「为什么」而非「是什么」。
- 宽 except 必须带 `# noqa: BLE001 - <理由>` 注释，且理由说明降级去向。
- 异步函数用 `async/await`；CPU/IO 阻塞调用经 `asyncio.to_thread` 下放。
- 测试文件命名 `tests/test_p<NN>_<主题>.py`，模块 docstring 写清覆盖面与验收口径。

## 文档同步面（代码行为变更时的强制清单）

`CHANGELOG.md`（[Unreleased]）· `README.md`（§3 架构 / §11 测试真源数）·
`docs/PROJECT_SPEC.md` · `docs/architecture/configuration.md` ·
`docs/architecture/runtime-guardrails.md` · `docs/source-code-study.md` ·
`.env.example` · `agentix/config/selfcheck.py` · 本文件。
