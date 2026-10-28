# ADR 0005：双检索通道边界与选择矩阵

> 状态：Accepted ｜ 日期：2026-09-21 ｜ 关联：R1 检索能力域迁移、企业级评估 §1.3

## 背景

仓库内并存两条检索通道，新读者易误判为"平行竞争引擎"。本文写透二者的边界与选择规则。

| | 通道 A：通用 RAG（检索域） | 通道 B：GraphRAG/LightRAG（编排域 kg_sub_graph） |
|---|---|---|
| 入口 | `KnowledgeGateway → RetrievalPort`（协议缝线） | `kg_sub_graph` 图节点（text2cypher / predefined cypher） |
| 存储 | FAISS / Milvus / Qdrant（注册制可插拔） | Neo4j 图谱 + LightRAG 工作目录 |
| 语义 | 向量相似度召回 + rerank 精排 | 关系遍历 / 多跳推理 |
| 适用 | "这条数据条目原文是什么"、"找相关示例记录" | "A 公司股东与 B 案件的关联"、"条文修订链条" |
| 伸缩 | 检索 worker 独立副本（compose `deploy.replicas`） | 单实例（Neo4j 瓶颈） |
| 失败语义 | `status="degraded"` 降级返回 | 分支停用 → 编排层 fallback |

## 选择矩阵（路由依据）

- 问题含**实体关系/多跳**诉求（"和…有关联"、"通过谁"、"修订自"）→ 通道 B
- 问题含**原文检索/语义相似**诉求（"规定"、"条文"、"示例记录"）→ 通道 A
- 混合问题 → 通道 A 打底（保证引用可溯源），通道 B 补充关系上下文
- 通道 B 不可用（Neo4j 宕机/未初始化）→ 一律落通道 A，标记 degraded

## 约束

1. 编排层**禁止**绕过 `RetrievalPort` 直接触碰 `retrieval.infra`（R5.2 已测试守护）；
2. 通道 B 的图谱数据必须能从 JSON 种子重建（`NEO4J_BOOTSTRAP_JSON`），不作为唯一事实源；
3. 新增检索后端先进检索域注册表（Factory），不新增第三条通道。
