# My Agentix 法律法规领域 · 本体 Schema 与共享约定

> 本文件描述 Neo4j 法律本体结构，以及跨路径的**溯源（sources）**与**免责声明**共享约定、数据文件格式。
> 与 `docs/design/design-legal-domain.md`、`docs/design/prd-legal-domain.md` 配套使用。

---

## 1. 法律法规本体（Neo4j 节点 Label 与关系）

### 1.1 节点（Label）与属性

| 节点 Label | 含义 | 关键属性 |
|---|---|---|
| `法规` (Regulation) | 一部法律/法规（如《民法典》《刑法》） | `name`、`生效日期`、`发文字号`、`效力层级` |
| `法条` (Article) | 具体条文 | `条号`、`法规`（所属法规名）、`内容`、`关键词`（分号分隔字符串） |
| `章节` (Chapter) | 编/章/节 | `标题`、`层级`、`所属法` |
| `案由` (Cause) | 案件事由/纠纷类型 | `名称`、`类别`（民事/刑事/行政） |
| `司法解释` (JudicialInterpretation) | 两高司法解释/批复 | `name`、`发布机关`、`日期`、`文号` |
| `机关` (Authority) | 制定/发布机关 | `name`、`层级` |

### 1.2 关系（Relationship）类型与方向

| 关系 | 方向 | 语义 |
|---|---|---|
| `属于` | (法条) → (章节) | 法条归属章节 |
| `属于` | (章节) → (法规) | 章节归属法规 |
| `引用` | (法条) → (法条) | 法条互引 |
| `对应案由` | (法条) → (案由) | 法条适用的纠纷类型 |
| `有司法解释` | (法条) → (司法解释) | 本条文对应的司法解释 |
| `制定机关` | (法规) → (机关) | 法规的制定机关 |
| `制定机关` | (司法解释) → (机关) | 司法解释的发布机关 |
| `上位法` | (法规) → (法规) | 下位法指向上位法 |

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
  "law": "民法典",                 # 法规名（graphrag 必填）
  "article": "第577条",            # 条号（graphrag 必填）
  "content_snippet": "当事人一方不履行…",  # 条文摘要（可选）
  "source": "《民法典》第577条",   # 展示用来源标记
  "document_id": "..."             # kb 路径沿用既有 document_id
}
```

- **graphrag 路径**：在 `lg_builder.create_research_plan` 中由 `response["cyphers"][*].records` 按「法规名 + 条号」抽取并组装，附加到返回的 `AIMessage.additional_kwargs["sources"]`。
- **kb 路径**：沿用 `kb_ingest` 与检索域 usecases 既有 `sources`（`document_id` / `source`；旧 `knowledge_service` 已归档），由 `create_kb_query` 原样透传。
- **前端**：统一消费 `sources` 渲染「法条引用卡片」。

---

## 4. 免责声明（R-10）共享约定

统一文案（集中在 `lg_prompts.py` 的 `LEGAL_DISCLAIMER` 常量，并在 graphrag `summarize` 提示词、`multi_tool.final_prompt`、kb 知识提示词、`ChatWidget` 空态四处出现）：

> 本回答由 AI 基于公开法律文本生成，不构成正式法律意见，具体以官方发布文本及专业律师意见为准。

---

## 5. 数据文件格式说明

| 文件 | 用途 | 格式要点 |
|---|---|---|
| `data/legal/legal_seed.json` | Neo4j 法律本体种子 | 顶层 `regulations` / `causes` / `interpretations` / `authorities`；法规含嵌套 `chapters`→`articles`；由 `scripts/legal_kg_to_csv.py` 转为 CSV |
| `data/legal/legal_lightrag_corpus.json` | LightRAG 社区摘要语料 | 列表，元素为 `title`（条款标题）+ `content`（条文/司法解释全文），可被 `scripts/init_lightrag.py --source json` 读取 |
| `data/kb/legal_corpus.txt` | kb 向量库语料 | 按「一条法条一段」组织，`# 《法规》第X条` 为标题行，正文为条文；由 `kb_ingest` 导入 Milvus legal 集合 |

> 以上三个数据文件均为**模板/骨架**，**不含真实法律条文**，正式上线前需由用户填入官方发布文本。
> 当前仓库仅保留法律法规领域数据（`legal*`）。
