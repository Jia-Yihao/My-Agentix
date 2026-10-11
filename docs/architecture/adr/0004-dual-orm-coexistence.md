# ADR 0004：双 ORM（SQLAlchemy + SQLModel）长期共存

> 状态：Accepted ｜ 日期：2026-09-21 ｜ 关联：R3.1 存储收敛、企业级评估 §1.2

## 背景

R3.1 存储收敛后，仓库内并存两套 ORM Base：

- **编排域**：SQLAlchemy（`agentix/infrastructure/persistence/`，会话/消息/租户/RBAC 等）
- **检索域**：SQLModel（`agentix/retrieval/infra/db/`，kb/document/chunk/usage 等，PG 走 retrieval schema）

## 决策

**不统一**。两套 Base 长期共存，理由：

1. 两域模型生命周期独立：编排域 schema 稳定（业务核心），检索域 schema 演进快（检索实验多）；
2. SQLModel 在检索域承担"模型即 Pydantic DTO"的职责（API schema 复用），编排域无此诉求；
3. 强行统一是跨域大迁移，风险收益比极差（五阶段融合已验证"协议缝线优于物理合并"）。

## 共存条件（守则）

- 跨域**不得**直接 import 对方 ORM 模型；数据交换走 `domain/ports` 协议或显式映射；
- 新表归属原则：会话/权限/审计 → 编排域；检索/索引/用量 → 检索域；
- p17 依赖规则测试继续守护（domain 禁 import 上层）。

## 统一触发条件（满足其一才重开此决策）

1. 出现必须跨域 JOIN 的强一致查询（当前为零——两域经 ID 弱关联）；
2. SQLModel 上游停更且安全审计要求冻结依赖；
3. 编排域新增的表超过一半是检索语义（说明边界划错了，应先修边界而非统一 ORM）。
