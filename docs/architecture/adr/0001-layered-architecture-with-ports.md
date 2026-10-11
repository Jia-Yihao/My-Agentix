# ADR-0001：采用分层架构 + 端口抽象（依赖倒置）

- **状态**：已采纳
- **日期**：2026-09-11
- **决策者**：My Agentix 维护者
- **相关**：[layers.md](../layers.md)、[interfaces.md](../interfaces.md)

---

## 背景 (Context)

项目初始为「可运行原型」：`interfaces`（HTTP 路由）直接调用 `infrastructure`
（数据库、向量库），业务逻辑散落在路由函数内。典型症状：

- `chat.py` 达 653 行，路由函数内建会话、写 CRUD、编排落库与响应组装
- `domain/` 是空目录——无接口抽象、无依赖倒置的落点
- 更换基础设施（如换向量库）需要改多处业务代码
- 核心逻辑无法脱离 FastAPI 单测

随着项目定位为「**可迁移垂直领域模板**」，需要一套能支撑长期演进、
可测试、可替换实现的架构。

---

## 决策 (Decision)

采用 **分层架构 + 端口抽象（依赖倒置）**：

```
interfaces → application → domain ← infrastructure
```

1. **四层划分**：`interfaces`（协议转换）/ `application`（编排）/ `domain`（领域规则）/ `infrastructure`（外部系统适配）
2. **端口抽象**：`domain/ports/` 用 `typing.Protocol` 定义契约（仓储、图供给），`application` 依赖抽象
3. **依赖规则**：依赖箭头指向 `domain`；`infrastructure` 实现 `domain.ports` 的 Protocol
4. **自动化守卫**：用 AST 静态扫描把规则固化为测试（`test_p17` / `test_p22`），CI 阻断违规

---

## 理由 (Rationale)

| 考量 | 说明 |
|------|------|
| **可测试性** | `application` 依赖 Protocol，可用内存假实现单测，无需启动 FastAPI / 数据库 |
| **可替换性** | 换向量库只需新增一个实现 `domain.ports` 的适配器，业务代码零改动 |
| **可迁移性** | 领域规则集中在 `domain/`，迁移新领域时业务层复用度高 |
| **防退化** | 守卫测试把「架构意图」变成「可执行约束」，避免架构随时间腐化 |
| **对齐业界** | 与 Clean Architecture / Hexagonal Architecture 一致，降低新人理解成本 |

### 备选方案与取舍

| 方案 | 否决理由 |
|------|---------|
| 保持扁平结构 | 迁移领域与替换基础设施成本高；无法单测核心逻辑 |
| 严格 Clean Architecture（每层独立模型 + 映射） | 对当前规模过度设计，映射代码量远超收益 |
| 仅用依赖注入框架 | 引入新依赖；且不解决「依赖方向」这一根本问题 |

---

## 后果 (Consequences)

### 正面

- `chat.py` 从 653 行降至 322 行，仅做协议转换
- `ChatService` 可脱离 FastAPI 单测（11 个用例，内存假 repo）
- `application → interfaces` 反向依赖清零（棘轮守卫以空集钉死）

### 负面 / 成本

- 新增代码需先判断归属层，学习成本略升
- 端口抽象带来少量类型标注开销（`Any` 表达外部类型）

> **历史成本项已消除**：本文初稿记录过「`domain/models/__init__.py` 成为唯一豁免」。
> 该 legacy 代理已在 **P2-6 T7**（2026-09-11）删除，连带 `domain/__init__.py` 的
> PEP 562 惰性导出一并移除。当前四条依赖规则**零例外**。

### 缓解

- 提供[归属决策树](../layers.md#6-新增文件的归属决策树)降低判断成本
- 守卫测试给出明确报错，违规即刻可见
- 零例外状态由 `test_p17::test_no_exemption_remains_in_guard`（AST 判定）持续守住 守卫测试给出明确报错，违规即刻可见

---

## 相关决策

- [ADR-0002](0002-registry-based-plugin-mechanism.md)：可插拔注册表
- [ADR-0003](0003-declarative-configuration-single-source.md)：配置单一事实来源
