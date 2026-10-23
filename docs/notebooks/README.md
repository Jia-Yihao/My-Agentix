# 源码导读（已迁移）

本目录原有的 9 本 Jupyter Notebook 已于 **2026-10-22** 移除，不再维护 ipynb 内容与 Jupyter 内核搭建流程。

原来的「边跑边看源码」内容已收敛到项目文档，按目的选择：

| 想了解 | 去哪里 |
|---|---|
| 平台全景、分层、一次请求的完整调用链 | [项目全景与整体结构](../architecture/overview.md) |
| 实现级事实（符号定位、默认值、失败语义、三条数据流） | [项目说明书](../PROJECT_SPEC.md)，端到端链路见 §22 |
| 分层规则、接口契约、扩展点、配置、运行时护栏 | [架构文档导航](../architecture/README.md) |
| 全量代码目录与模块职责 | [整体结构 · 代码目录](../architecture/overview.md#4-代码目录与模块职责) |

## 说明

- `scripts/setup_notebook_kernel.py` 是旧 notebook 的内核搭建脚本，现已不再需要；保留仅为避免影响已有本地环境，可后续随清理一并删除。
- 本目录下的 `data/`、`uploads/` 是旧 notebook 运行时产物目录，当前为空，可安全删除。
- 文档中的代码路径仍由 `tests/test_p41_doc_consistency.py` 校验；若重构导致路径变化，请同步更新上述文档。
