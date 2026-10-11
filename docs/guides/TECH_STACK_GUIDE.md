# My Agentix 技术栈清单与项目应用详解

> 本文档列出项目用到的每一项技术，说明"它是什么"、"项目里怎么用的"、"在哪个文件"、"需要掌握到什么程度"。

---

## 一、Python 核心

### 1.1 Python 3.10
**是什么**: 编程语言，本项目的主语言
**项目里怎么用**: 所有后端代码都是 Python
**需要掌握**: 基础语法、类型提示（Type Hints）、dataclass、装饰器

### 1.2 async / await 异步编程
**是什么**: Python 的异步编程模型，允许在等待 I/O 时执行其他任务
**项目里怎么用**:
```python
# chat.py - 异步处理用户请求
async def process_agent_query(message, session_id, ...):
    result = await graph.ainvoke(input_state, config=config)  # await 等待 LLM 响应
    return result

# agentix/retrieval/infra/embedding/cache.py - 异步包装同步代码
embedding = await asyncio.to_thread(self.embedder.embed_query, query)
```
**在哪个文件**: `chat.py`、`retrieval/infra/embedding/cache.py`、`lg_builder.py`、所有 handler 模块
**需要掌握**: `async def`、`await`、`asyncio.gather()`（并行执行）、`asyncio.to_thread()`（包装同步代码）

### 1.3 dataclass
**是什么**: Python 的数据类装饰器，自动生成 `__init__`、`__repr__` 等方法
**项目里怎么用**:
```python
# lg_states.py - 定义 Agent 的状态
@dataclass
class AgentState:
    messages: Annotated[list, add_messages]  # 对话历史
    router: Optional[Router] = None          # 路由信息
    sources: list = field(default_factory=list)  # 来源引用
```
**在哪个文件**: `lg_states.py`
**需要掌握**: `@dataclass`、`field(default_factory=...)`、`Annotated` 类型提示

### 1.4 typing 类型提示
**是什么**: Python 的类型注解系统，不影响运行但帮助 IDE 和类型检查
**项目里怎么用**:
```python
from typing import Literal, Optional, Dict, List, Any

def route_query(state: AgentState) -> Literal["respond_to_general_query", "create_kb_query"]:
    ...

def get_llm_client(temperature: float = 0.7, tags: Optional[List[str]] = None) -> ChatOpenAI:
    ...
```
**在哪个文件**: 全项目所有 `.py` 文件
**需要掌握**: `Optional`、`List`、`Dict`、`Literal`、`Union`、`Annotated`

---

## 二、FastAPI（Web 框架）

### 2.1 FastAPI 基础
**是什么**: 现代 Python Web 框架，原生支持 async，自动生成 OpenAPI 文档
**项目里怎么用**:
```python
# main.py - 创建应用
application = FastAPI(title="My Agentix", version="0.2.0")

# chat.py - 定义路由
@router.post("/", response_model=ChatResponse)
async def chat(request: ChatRequest, db: Session = Depends(get_db)):
    result = await process_agent_query(request.message, session_id)
    return ChatResponse(message=result["message"], session_id=session_id, ...)
```
**在哪个文件**: `main.py`、`interfaces/http/v1/{sessions,chat,upload,events,runs}.py`
**需要掌握**: `@app.get/post`、`Depends`（依赖注入）、`ResponseModel`、`HTTPException`

### 2.2 Pydantic 数据模型
**是什么**: 数据验证库，FastAPI 用它做请求/响应校验
**项目里怎么用**:
```python
# chat.py - 定义请求和响应格式
class ChatRequest(BaseModel):
    message: str = Field(..., description="User message", min_length=1, max_length=5000)
    session_id: Optional[str] = Field(None, description="Session ID")
    stream: bool = Field(False, description="Enable streaming")

class ChatResponse(BaseModel):
    message: str
    session_id: str
    route: Optional[str] = None
```
**在哪个文件**: `interfaces/http/v1/chat.py`、`lg_states.py`（Router 模型）
**需要掌握**: `BaseModel`、`Field`、`model_validator`、`field_validator`

### 2.3 Depends 依赖注入
**是什么**: FastAPI 的依赖注入系统，自动解析和注入参数
**项目里怎么用**:
```python
# database.py - 定义依赖
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# chat.py - 使用依赖
@router.post("/")
async def chat(request: ChatRequest, db: Session = Depends(get_db)):
    chat_session.create(db, obj_in=session_data)
```
**在哪个文件**: `database.py`（定义）、`chat.py`（使用）
**需要掌握**: `Depends`、生成器依赖（`yield`）

### 2.4 StreamingResponse（SSE 流式响应）
**是什么**: FastAPI 的流式响应类，用于 Server-Sent Events
**项目里怎么用**:
```python
# chat.py - 流式聊天端点
@router.post("/stream")
async def chat_stream(request: ChatRequest, db: Session = Depends(get_db)):
    return StreamingResponse(
        stream_agent_response(request.message, session_id, ...),
        media_type="text/event-stream",
    )

# 流式生成器
async def stream_agent_response(message, session_id, ...):
    yield f"data: {metadata_chunk.model_dump_json()}\n\n"
    async for event in graph.astream(input_state, config=config, stream_mode="updates"):
        yield f"data: {message_chunk.model_dump_json()}\n\n"
    yield f"data: {done_chunk.model_dump_json()}\n\n"
```
**在哪个文件**: `chat.py`
**需要掌握**: `StreamingResponse`、`async generator`（`async def + yield`）、SSE 格式

### 2.5 CORS 中间件
**是什么**: 跨域资源共享，控制哪些域名可以访问 API
**项目里怎么用**:
```python
# main.py
application.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,  # 白名单
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
```
**在哪个文件**: `main.py`
**需要掌握**: CORS 原理、`allow_origins` 安全配置

---

## 三、LangGraph（Agent 编排框架）

### 3.1 StateGraph 状态图
**是什么**: LangGraph 的核心概念，用图结构编排多步骤 Agent 工作流
**项目里怎么用**:
```python
# lg_builder.py - 构建状态图
builder = StateGraph(AgentState, input=InputState)

# 添加节点（每个节点是一个函数）
builder.add_node(analyze_and_route_query)   # 路由节点
builder.add_node(respond_to_general_query)  # 通用回复
builder.add_node(get_additional_info)       # Guardrails
builder.add_node("create_research_plan", create_research_plan)  # GraphRAG
builder.add_node(create_image_query)        # 图片处理
builder.add_node(create_file_query)         # 文件处理
builder.add_node(create_kb_query)           # KB 查询

# 添加边
builder.add_edge(START, "analyze_and_route_query")
builder.add_conditional_edges("analyze_and_route_query", route_query)

# 编译
graph = builder.compile(checkpointer=MemorySaver())
```
**在哪个文件**: `lg_builder.py`
**需要掌握**: `StateGraph`、`add_node`、`add_edge`、`add_conditional_edges`、`compile`

### 3.2 条件边（Conditional Edges）
**是什么**: 根据当前状态动态决定下一步走哪个节点
**项目里怎么用**:
```python
# router.py - 路由函数
def route_query(state: AgentState) -> Literal[
    "respond_to_general_query", "get_additional_info", "create_research_plan",
    "create_image_query", "create_file_query", "create_kb_query",
]:
    router = _ensure_router(getattr(state, "router", None))
    _type = router.type or "kb-query"

    if _type == "general-query":
        return "respond_to_general_query"
    elif _type == "kb-query":
        return "create_kb_query"
    elif _type in ("graphrag-query", "text2sql-query"):
        return "create_research_plan"
    # ...

# 注册条件边
builder.add_conditional_edges("analyze_and_route_query", route_query)
```
**在哪个文件**: `router.py`（路由函数）、`lg_builder.py`（注册）
**需要掌握**: 条件边的返回值必须是已注册的节点名称

### 3.3 Checkpointer 状态持久化
**是什么**: 在每个图步骤后保存状态快照，支持会话恢复
**项目里怎么用**:
```python
# lg_builder.py
checkpointer = MemorySaver()  # 内存存储（开发用，生产用 Redis）
graph = builder.compile(checkpointer=checkpointer)

# chat.py - 通过 thread_id 恢复会话
config = {"configurable": {"thread_id": session_id}}
result = await graph.ainvoke(input_state, config=config)
```
**在哪个文件**: `lg_builder.py`、`chat.py`
**需要掌握**: `MemorySaver`、`thread_id`、`graph.get_state()`

### 3.4 astream 流式输出
**是什么**: LangGraph 的流式执行 API，逐节点返回更新
**项目里怎么用**:
```python
# chat.py - 真流式
async for event in graph.astream(input_state, config=config, stream_mode="updates"):
    if "analyze_and_route_query" in event:
        router_data = event["analyze_and_route_query"].get("router", {})
    if "respond_to_general_query" in event:
        node_data = event["respond_to_general_query"]
        msgs = node_data.get("messages", [])
```
**在哪个文件**: `chat.py`
**需要掌握**: `graph.astream()`、`stream_mode="updates"`、事件结构

---

## 四、LangChain（LLM 框架）

### 4.1 ChatOpenAI
**是什么**: LangChain 的 OpenAI 聊天模型封装
**项目里怎么用**:
```python
# llm_factory.py - 统一工厂
from langchain_openai import ChatOpenAI

def get_llm_client(temperature=0.7, tags=None):
    return ChatOpenAI(
        openai_api_key=settings.OPENAI_API_KEY,
        model_name=settings.OPENAI_MODEL,
        openai_api_base=settings.OPENAI_API_BASE,
        temperature=temperature,
        tags=tags or [],
    )
```
**在哪个文件**: `llm_factory.py`（工厂）、所有 handler（使用）
**需要掌握**: `ChatOpenAI`、OpenAI 兼容 API（`base_url` 自定义）

### 4.2 with_structured_output 结构化输出
**是什么**: 让 LLM 输出符合 Pydantic model 格式的 JSON
**项目里怎么用**:
```python
# router.py - 路由分类
model = get_llm_client(temperature=0.7, tags=["router"])
raw_response = await model.with_structured_output(Router).ainvoke(messages)
# raw_response 一定是 Router 实例，有 .type, .logic, .question 字段

# guardrails.py - Guardrails 决策
guardrails_chain = full_system_prompt | model.with_structured_output(AdditionalGuardrailsOutput)
output = await guardrails_chain.ainvoke({"question": question})
# output.decision 一定是 "end" 或 "proceed"
```
**在哪个文件**: `router.py`、`guardrails.py`、`lg_builder.py`
**需要掌握**: `with_structured_output()`、Pydantic model 定义、`Literal` 类型限制

### 4.3 ChatPromptTemplate
**是什么**: LangChain 的提示词模板
**项目里怎么用**:
```python
# guardrails.py
from langchain_core.prompts import ChatPromptTemplate

full_system_prompt = ChatPromptTemplate.from_messages([
    ("system", GUARDRAILS_SYSTEM_PROMPT),
    ("human", message),
])

guardrails_chain = full_system_prompt | model.with_structured_output(AdditionalGuardrailsOutput)
```
**在哪个文件**: `guardrails.py`、`lg_builder.py`
**需要掌握**: `ChatPromptTemplate.from_messages()`、`|` 管道操作符

### 4.4 AIMessage / HumanMessage
**是什么**: LangChain 的消息类型
**项目里怎么用**:
```python
from langchain_core.messages import AIMessage, HumanMessage

return {"messages": [AIMessage(content="抱歉，这个问题超出了我的服务范围。")]}

# 附加额外信息
ai_message = AIMessage(content=answer_text)
ai_message.additional_kwargs["sources"] = sources
```
**在哪个文件**: 所有 handler 模块
**需要掌握**: `AIMessage`、`HumanMessage`、`additional_kwargs`

### 4.5 LCEL 管道操作符
**是什么**: LangChain Expression Language，用 `|` 连接组件
**项目里怎么用**:
```python
guardrails_chain = full_system_prompt | model.with_structured_output(AdditionalGuardrailsOutput)
# 等价于: step1 prompt格式化 → step2 LLM调用 → step3 结构化解析
```
**在哪个文件**: `guardrails.py`
**需要掌握**: `|` 管道、`prompt | llm | parser` 模式

---

## 五、Neo4j（图数据库）

### 5.1 Cypher 查询语言
**是什么**: Neo4j 的图查询语言，类似 SQL 但用于图
**项目里怎么用**:
```cypher
-- 查询法条内容
MATCH (a:Article {条号: '577', 法规: '民法典'})
RETURN a.内容, a.关键词

-- 查询法条引用的其他法条
MATCH (a:Article {条号: '577'})-[:CITES_ARTICLE]->(b:Article)
RETURN b.法规, b.条号, b.内容

-- 查询案由对应的法条
MATCH (a:Article)-[:ARTICLE_CAUSE]->(c:Cause {名称: '合同违约'})
RETURN a.法规, a.条号, a.内容
```
**在哪个文件**: `legal_cypher_dict.py`（预定义查询）、`legal_retriever.py`（动态生成）
**需要掌握**: `MATCH`、`WHERE`、`RETURN`、关系模式 `(a)-[r]->(b)`、属性过滤

### 5.2 图 Schema 设计
**是什么**: 节点类型和关系类型的定义
**项目里的 Schema**:
```
节点类型:
  - Regulation (法规): name, 生效日期, 效力层级
  - Chapter (章节): name
  - Article (法条): 条号, 内容, 关键词
  - Cause (案由): 名称, 类别
  - JudicialInterpretation (司法解释): name, 发布机关
  - Authority (制定机关): name, 层级

关系类型:
  - BELONGS_TO_CHAPTER: Article → Chapter
  - CITES_ARTICLE: Article → Article (法条互引)
  - ARTICLE_CAUSE: Article → Cause (法条-案由映射)
  - ARTICLE_INTERPRETATION: Article → JudicialInterpretation
  - REGULATION_AUTHORITY: Regulation → Authority
  - UPPER_LAW: Regulation → Regulation (上位法关系)
```
**在哪个文件**: `legal_kg_to_csv.py`（导入脚本）、`legal_seed.json`（种子数据）
**需要掌握**: 节点/关系设计原则、属性 vs 关系的选择

### 5.3 Neo4j Python 驱动
**是什么**: Neo4j 的官方 Python 客户端
**项目里怎么用**:
```python
# kg_neo4j_conn.py - 连接管理
from neo4j import GraphDatabase

def get_neo4j_graph():
    driver = GraphDatabase.driver(uri, auth=(user, password))
    return driver

# 使用
with driver.session() as session:
    result = session.run("MATCH (n) RETURN n LIMIT 10")
    for record in result:
        print(record["n"])
```
**在哪个文件**: `application/agents/kg_sub_graph/kg_neo4j_conn.py`
**需要掌握**: `GraphDatabase.driver()`、`session.run()`、事务管理

### 5.4 Schema 提取
**是什么**: 从 Neo4j 动态获取图谱结构，注入 LLM prompt
**项目里怎么用**:
```python
# utils.py
def retrieve_and_parse_schema_from_graph_for_prompts(graph):
    # 获取所有节点标签和属性
    # 获取所有关系类型
    # 格式化为文本注入 prompt
    return schema_text
```
**在哪个文件**: `utils.py`（`retrieve_and_parse_schema_from_graph_for_prompts`）
**需要掌握**: Neo4j 的 `db.labels()`、`db.propertyKeys()` 等元数据查询

---

## 六、向量数据库（Milvus）

### 6.1 Embedding 向量
**是什么**: 把文本转换为高维向量（如 1024 维），语义相似的文本向量距离近
**项目里怎么用**:
```python
# agentix/retrieval/infra/embedding/client.py - Embedding 客户端
class OpenAICompatibleEmbeddings:
    def embed_query(self, text: str) -> List[float]:
        # 调用 embedding API，返回 1024 维向量
        response = self.client.embeddings.create(input=text, model=self.model)
        return response.data[0].embedding

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        # 批量 embedding
        response = self.client.embeddings.create(input=texts, model=self.model)
        return [item.embedding for item in response.data]
```
**在哪个文件**: `agentix/retrieval/infra/embedding/client.py`
**需要掌握**: embedding 概念、余弦相似度、批量 vs 单条

### 6.2 Milvus 向量数据库
**是什么**: 专用向量数据库，支持大规模向量检索
**项目里怎么用**:
```python
# kb_ingest/kb_service/services/vector_store.py - Milvus 封装
from pymilvus import Collection, connections

class VectorStore:
    def __init__(self, collection_name, host, port, dimension):
        connections.connect(alias="default", host=host, port=port)
        self.collection = Collection(collection_name)

    def add_documents(self, ids, embeddings, documents, metadatas):
        # 插入向量和元数据
        self.collection.insert([ids, embeddings, documents, ...])

    def search(self, query_embedding, top_k, filter_expr):
        # 向量检索
        results = self.collection.search(
            data=[query_embedding],
            anns_field="embedding",
            param={"metric_type": "IP", "params": {"nprobe": 10}},
            limit=top_k,
            output_fields=["id", "content", "doc_id", "title"],
        )
        return formatted_results
```
**在哪个文件**: `kb_ingest/kb_service/services/vector_store.py`
**需要掌握**: `Collection`、`insert`、`search`、索引类型（IVF_FLAT）、相似度度量（IP/L2）

### 6.3 结构化法律库（Milvus legal 集合）

**是什么**: 原基于 pgvector 的结构化法律知识库，R3.7 起统一迁移至 Milvus
`legal` 集合（确定性主键 `sha1(source_table|source_id)[:32]` + L2 归一化 + IP 度量 ≡ cosine）
**项目里怎么用**:
```python
# kb_ingest/kb_service/services/milvus_store.py
store.upsert([{
    "source_table": "laws",
    "source_id": "1",
    "rewritten_content": "善意取得的构成要件……",
    "original_data": {"title": "民法典", "legal_effective_from": "2021-01-01"},
}])  # 先删后插实现幂等 upsert，content_hash 支持增量跳过

results = store.search_similar(
    query_embedding=vec, top_k=5,
    source_tables=["laws"],           # 下推为 Milvus expr
    effective_on=date(2021, 6, 1),    # metadata['legal_effective_from'] JSON 字段过滤
    company_filter="某某公司",          # Milvus 无 ILIKE → 超量召回 k*3 + Python 子串过滤
)
```
**在哪个文件**: `kb_ingest/kb_service/services/milvus_store.py`、`kb_service/services/vector_store.py`（兼容 shim）
**需要掌握**: JSON 字段表达式、partition_key 租户隔离、IP+L2 归一化 ≡ cosine 的数学等价、确定性主键幂等 upsert

### 6.4 文本分块（Text Splitting）
**是什么**: 把长文本切分成小块，每块独立 embedding
**项目里怎么用**:
```python
# agentix/retrieval/infra/vectorstore/faiss_manager.py
from langchain_text_splitters import RecursiveCharacterTextSplitter

self.splitter = RecursiveCharacterTextSplitter(
    chunk_size=512,       # 每块最大 512 字符
    chunk_overlap=80,     # 相邻块重叠 80 字符
    separators=["\n\n", "\n", "。", "！", "？", " "],  # 中文分隔符
)

chunks = self.splitter.split_documents([document])
```
**在哪个文件**: `agentix/retrieval/infra/vectorstore/faiss_manager.py`
**需要掌握**: chunk_size/chunk_overlap 的选择、分隔符对中文的影响

---

## 七、Redis（缓存）

### 7.1 Redis 基础
**是什么**: 内存键值数据库，支持多种数据结构
**项目里怎么用**:
```python
# redis_cache.py - 语义缓存
import redis.asyncio as aioredis

class SemanticCache:
    def __init__(self):
        self._redis = aioredis.from_url(settings.REDIS_URL)

    async def get_cached(self, query_embedding):
        # 遍历所有缓存的向量，找相似的
        async for raw_key in self._redis.scan_iter("cache:*"):
            cached_vector = await self._redis.get(raw_key)
            similarity = cosine_similarity(query_embedding, cached_vector)
            if similarity > threshold:
                return await self._redis.get(f"answer:{raw_key}")
        return None

    async def set_cached(self, query, answer, embedding):
        await self._redis.setex(f"cache:{hash}", ttl, answer)
```
**在哪个文件**: `redis_cache.py`
**需要掌握**: `redis.asyncio`、`scan_iter`、`setex`（带 TTL）、管道（pipeline）

### 7.2 Redis 数据结构
**项目里用了哪些**:
| 结构 | 用途 |
|------|------|
| String | 语义缓存（answer）、对话历史 |
| scan_iter | 遍历缓存 key |

**需要掌握**: String、Hash、List、Set、Sorted Set 的基本操作

---

## 八、Reranker（重排序）

### 8.1 Reranker 原理
**是什么**: 二阶段检索的精排阶段，用 cross-encoder 判断 query-document 相关性
**项目里怎么用**:
```python
# kb_ingest/kb_service/services/reranker.py（示例为归档实现形态）
class Reranker:
    async def rerank(self, query, documents, top_k):
        if self.provider == "custom":
            ranked = await self._custom_rerank(query, documents, top_k)
        elif self.provider == "cohere":
            ranked = await asyncio.to_thread(self._cohere_rerank, query, documents, top_k)
        elif self.provider == "jina":
            ranked = await self._jina_rerank(query, documents, top_k)
        return ranked

    def _process_rerank_results(self, results, documents, top_k):
        # results: [{"index": 0, "relevance_score": 0.9}, ...]
        # 按 score 排序，返回重排后的文档列表
        reranked = []
        for item in results:
            idx = item["index"]
            doc = dict(documents[idx])
            doc["rerank_score"] = item["relevance_score"]
            reranked.append(doc)
        return reranked[:top_k]
```
**在哪个文件**: `kb_ingest/kb_service/services/reranker.py`（多 Provider）；检索域 httpx 单端点实现在 `agentix/retrieval/infra/rag/rerank.py`
**需要掌握**: cross-encoder vs bi-encoder、双阈值过滤（similarity + rerank_score）

### 8.2 多 Provider 支持
**项目支持的 Reranker**:
| Provider | API 格式 | 项目实现 |
|----------|---------|---------|
| Cohere | `client.rerank()` | `_cohere_rerank`（同步） |
| Jina | REST API | `_jina_rerank`（异步 httpx） |
| Voyage | REST API | `_voyage_rerank`（异步 httpx） |
| BGE (自部署) | DashScope 格式 | `_custom_rerank`（异步 httpx） |

**在哪个文件**: `kb_ingest/kb_service/services/reranker.py`
**需要掌握**: HTTP API 调用、异步 httpx 客户端

---

## 九、Vue 3（前端）

### 9.1 Composition API + `<script setup>`
**是什么**: Vue 3 的组件写法，比 Options API 更简洁
**项目里怎么用**:
```vue
<script setup lang="ts">
import { ref, reactive } from "vue";

const userInput = ref("");
const state = reactive({
  isOpen: false,
  isTyping: false,
  messages: [] as ChatMessage[],
});

async function sendMessage() {
  const response = await axios.post("/api/v1/chat/stream", { message: userInput.value });
  // 处理响应
}
</script>
```
**在哪个文件**: `ChatWidget.vue`
**需要掌握**: `ref`、`reactive`、`computed`、`watch`、`<script setup>` 语法

### 9.2 SSE 流式接收
**是什么**: 前端通过 EventSource 或 fetch 接收服务端推送
**项目里怎么用**:
```typescript
// ChatWidget.vue - 接收 SSE 流
const response = await fetch("/api/v1/chat/stream", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ message: userInput.value }),
});

const reader = response.body.getReader();
const decoder = new TextDecoder();

while (true) {
  const { done, value } = await reader.read();
  if (done) break;

  const text = decoder.decode(value);
  const lines = text.split("\n").filter(line => line.startsWith("data: "));

  for (const line of lines) {
    const data = JSON.parse(line.slice(6));  // 去掉 "data: " 前缀
    if (data.type === "message") {
      currentMessage.value += data.content;  // 追加内容
    }
  }
}
```
**在哪个文件**: `ChatWidget.vue`
**需要掌握**: `fetch` API、`ReadableStream`、SSE 格式解析

### 9.3 XSS 安全渲染
**是什么**: 防止恶意 HTML/JS 注入
**项目里怎么用**:
```typescript
// ChatWidget.vue - linkify 函数
function linkify(text: string): string {
  if (!text) return "";
  // 先转义 HTML 实体（防止 XSS）
  const escaped = text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
  // 再做 URL 链接化
  return escaped.replace(
    /(https?:\/\/[^\s]+)/g,
    '<a href="$1" target="_blank" rel="noopener noreferrer">$1</a>'
  );
}
```
**在哪个文件**: `ChatWidget.vue`
**需要掌握**: `v-html` 风险、HTML 实体转义、DOMPurify

---

## 十、Docker（容器化）

### 10.1 Dockerfile
**是什么**: 定义如何构建 Docker 镜像
**项目里怎么用**:
```dockerfile
# 多阶段构建
FROM python:3.10-slim AS builder
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

FROM builder AS server
COPY . .
CMD ["uvicorn", "agentix.main:application"]
```
**在哪个文件**: `Dockerfile`
**需要掌握**: `FROM`、`COPY`、`RUN`、`CMD`、多阶段构建

### 10.2 Docker Compose
**是什么**: 定义和运行多容器应用
**项目里怎么用**:
```yaml
# docker-compose.yml
services:
  backend:
    build: .
    ports: ["8000:8000"]
    depends_on:
      redis: { condition: service_healthy }
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/health"]

  neo4j:
    image: neo4j:5.18
    ports: ["17474:7474", "17687:7687"]

  redis:
    image: redis:7-alpine
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]

  milvus:
    image: milvusdb/milvus:v2.3.0
```
**在哪个文件**: `docker-compose.yml`
**需要掌握**: `services`、`depends_on`、`healthcheck`、`volumes`、`networks`

### 10.3 环境变量
**是什么**: 通过 `.env` 文件配置应用参数
**项目里怎么用**:
```bash
# .env.example
LLM_API_KEY=sk-xxx
LLM_MODEL=qwen3-max
LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
NEO4J_URI=bolt://neo4j:7687
NEO4J_PASSWORD=legalpass
MILVUS_HOST=milvus
REDIS_URL=redis://redis:6379/0
```
**在哪个文件**: `.env`、`.env.example`、`settings.py`（读取）
**需要掌握**: `pydantic-settings` 的 `BaseSettings`、`env_file` 配置

---

## 十一、其他依赖库

### 11.1 aiohttp
**是什么**: 异步 HTTP 客户端
**项目里怎么用**: 调用视觉模型 API、CogView-4 图片生成 API
**在哪个文件**: `image_handler.py`

### 11.2 Pillow (PIL)
**是什么**: Python 图片处理库
**项目里怎么用**: 压缩用户上传的图片（resize + 转 JPEG + base64 编码）
**在哪个文件**: `image_handler.py`

### 11.3 httpx
**是什么**: 异步 HTTP 客户端（比 aiohttp 更现代）
**项目里怎么用**: 调用 Reranker API（Jina/Voyage/BGE）
**在哪个文件**: `kb_ingest/kb_service/services/reranker.py`

### 11.4 loguru
**是什么**: Python 日志库（比标准 logging 更好用）
**项目里怎么用**:
```python
from loguru import logger
logger.info("Starting {} v{}", settings.APP_NAME, settings.APP_VERSION)
logger.error("Failed: %s", exc, exc_info=True)
```
**在哪个文件**: 全项目

### 11.5 SQLAlchemy
**是什么**: Python ORM 框架
**项目里怎么用**: 定义数据库模型、CRUD 操作
**在哪个文件**: `database.py`、`crud/` 目录

### 11.6 PyJWT / python-jose
**是什么**: JWT Token 处理库
**项目里怎么用**: API Key 认证（`security.py`）
**在哪个文件**: `security.py`

### 11.7 pymilvus
**是什么**: Milvus 的 Python 客户端
**项目里怎么用**: 向量数据库的增删查操作
**在哪个文件**: `kb_ingest/kb_service/services/vector_store.py`

### 11.8 neo4j (Python driver)
**是什么**: Neo4j 的官方 Python 驱动
**项目里怎么用**: 执行 Cypher 查询、管理连接
**在哪个文件**: `agentix/application/agents/kg_sub_graph/kg_neo4j_conn.py`

