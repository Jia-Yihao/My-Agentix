# My Agentix 测试数据集领域 · 本体 Schema 与共享约定

> **文档状态**：现行专题文档 · 与代码同步维护。
> 文档分层：唯一现行总览是根 [README](../../README.md)，实现细节见 [PROJECT_SPEC](../项目实现手册.md)，本文只讲本专题。

> 本文件描述 Neo4j 测试数据本体结构，以及跨路径的**溯源（sources）**与**免责声明**共享约定、数据文件格式。
> 与 `docs/design/测试数据域-架构设计.md`、`docs/design/测试数据域-产品需求.md` 配套使用。
> 说明：下表中的节点 Label、关系类型与参数名是图谱的**功能性标识符**（与真实 Neo4j schema 一一对应），保持不变；业务描述统一以「测试数据集」口径表述。

---

## 1. 测试数据本体（Neo4j 节点 Label 与关系）

### 1.1 节点（Label）与属性

| 节点 Label | 含义 | 关键属性 |
|---|---|---|
| `法规` (Regulation) | 一个测试数据集条目（如「示例数据集 A」「示例数据集 B」） | `name`、`生效日期`、`发文字号`、`效力层级` |
| `法条` (Article) | 具体数据条目 | `条号`、`法规`（所属数据集名）、`内容`、`关键词`（分号分隔字符串） |
| `章节` (Chapter) | 编/章/节 | `标题`、`层级`、`所属法` |
| `案由` (Cause) | 数据分类（问题类型） | `名称`、`类别`（类别一/类别二/类别三） |
| `司法解释` (JudicialInterpretation) | 示例说明/批复 | `name`、`发布机关`、`日期`、`文号` |
| `机关` (Authority) | 制定/发布机构 | `name`、`层级` |

### 1.2 关系（Relationship）类型与方向

| 关系 | 方向 | 语义 |
|---|---|---|
| `属于` | (法条) → (章节) | 数据条目归属章节 |
| `属于` | (章节) → (法规) | 章节归属数据集 |
| `引用` | (法条) → (法条) | 数据条目互引 |
| `对应案由` | (法条) → (案由) | 数据条目适用的问题类型 |
| `有司法解释` | (法条) → (司法解释) | 本条目对应的示例说明 |
| `制定机关` | (法规) → (机关) | 数据集的来源机构 |
| `制定机关` | (司法解释) → (机关) | 示例说明的发布机构 |
| `上位法` | (法规) → (法规) | 下级数据集指向上级数据集 |

---

## 2. 喂给 text2cypher 的静态 Schema 文本

权威动态 schema 来自 Neo4j `graph.schema`（灌库后自动生效）；静态补充文本存放于
`legal_retriever.py` 的 `LEGAL_GRAPH_SCHEMA` 常量，并被以下位置引用，确保单一来源、避免漂移：

- `kg_prompts.TEXT2CYPHER_GENERATION_PROMPT`
- `text2cypher/generation/prompts.py`（human 提示追加）

静态文本见 `legal_retriever.py`，核心规则：仅输出 Cypher、用 MATCH/WITH 开头、标签/关系/属性名与第 1 节一致、过滤用参数化（`$法规名`/`$条号`/`$案由名`）、返回时尽量带 `法规名` 与 `条号` 以便溯源。

---

## 3. 溯源（sources）数据结构约定

跨路径统一 `sources` 为 `list[dict]`，graphrag 与 kb 路径均向 `AIMessage.additional_kwargs["sources"]` 写入，并由 `chat.py` 透传到前端。

```python
{
  "type": "graphrag" | "kb",      # 来源路径
  "law": "示例数据集 A",           # 数据集名（graphrag 必填）
  "article": "第577条",            # 条号（graphrag 必填）
  "content_snippet": "参与方一方不履行…",  # 条目摘要（可选）
  "source": "《示例数据集 A》第577条",   # 展示用来源标记
  "document_id": "..."             # kb 路径沿用既有 document_id
}
```

- **graphrag 路径**：在 `lg_builder.create_research_plan` 中由 `response["cyphers"][*].records` 按「数据集名 + 条号」抽取并组装，附加到返回的 `AIMessage.additional_kwargs["sources"]`。
- **kb 路径**：沿用 `kb_ingest` 与检索域 usecases 既有 `sources`（`document_id` / `source`；旧 `knowledge_service` 已归档），由 `create_kb_query` 原样透传。
- **前端**：统一消费 `sources` 渲染「数据条目引用卡片」。

---

## 4. 免责声明（R-10）共享约定

统一文案（集中在 `lg_prompts.py` 的 `LEGAL_DISCLAIMER` 常量，并在 graphrag `summarize` 提示词、`multi_tool.final_prompt`、kb 知识提示词、`ChatWidget` 空态四处出现）：

> 本回答由 AI 基于测试数据集生成，不构成任何专业结论，具体以数据源最新版本为准。

---

## 5. 数据文件格式说明

| 文件 | 用途 | 格式要点 |
|---|---|---|
| `data/legal/legal_seed.json` | Neo4j 测试数据本体种子 | 顶层 `regulations` / `causes` / `interpretations` / `authorities`；数据集含嵌套 `chapters`→`articles`；由 `scripts/legal_kg_to_csv.py` 转为 CSV |
| `data/legal/legal_lightrag_corpus.json` | LightRAG 社区摘要语料 | 列表，元素为 `title`（条目标题）+ `content`（条目/示例说明全文），可被 `scripts/init_lightrag.py --source json` 读取 |
| `data/kb/legal_corpus.txt` | kb 向量库语料 | 按「一条数据条目一段」组织，`# 《数据集》第X条` 为标题行，正文为条目内容；由 `kb_ingest` 导入 Milvus legal 集合 |

> 以上三个数据文件均为**测试数据集语料**，字段与图谱 Label 保持不变，仅以「测试数据集」口径对外表述。
> 当前仓库保留 `legal*` 命名的测试数据集数据（文件名/标识符不变，避免 import 与测试断裂）。
