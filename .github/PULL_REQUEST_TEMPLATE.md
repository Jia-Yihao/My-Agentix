## 变更说明

<!-- 这个 PR 做了什么、为什么做。行为变化请写清"改前 / 改后"。 -->

## 关联

- 关联 Issue / 讨论：<!-- #123；无则填「无」 -->

## 自检清单

按 `CONTRIBUTING.md` 的流程逐项确认（不勾选会被 review 打回）：

- [ ] `make lint` 通过（= CI 的 `ruff check agentix scripts evals kb_ingest tests`）
- [ ] `pytest -m "not integration" -q` 通过
- [ ] 分层约束未被破坏（`domain` 不 import `infrastructure`；见 `tests/test_p17_domain_layer.py`）
- [ ] 新增/修改的核心链路（`application/`）有对应测试
- [ ] `CHANGELOG.md` 的 `[Unreleased]` 段已更新
- [ ] 受影响的文档已同步（README / `docs/PROJECT_SPEC.md` / 相关 runbook）
- [ ] 未提交任何密钥、`.env`、真实用户数据或截图

## 如何验证

<!-- 具体命令或步骤，让 reviewer 能照着复现。 -->

## 风险与回滚

- 影响面：<!-- 配置项 / 接口 / 数据结构 / 迁移 -->
- 回滚方式：<!-- revert 提交即可 / 需要额外动作，请写明 -->
