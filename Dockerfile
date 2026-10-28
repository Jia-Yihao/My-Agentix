# ============================================================================
# My Agentix 多阶段构建（P1 部署收口）
#
# 构建目标：
#   docker build --target server .        # 后端运行时镜像（compose 默认 target）
#   docker build --target neo4j_seeded .  # 预导入法律 KG 的 Neo4j 镜像
#
# 多阶段要点：
#   - builder 阶段承担 apt/编译工具链 + pip 安装（build-essential 不进运行时镜像）；
#   - server 运行时阶段仅 COPY venv + 应用代码，无 apt 安装、无编译器；
#   - 依赖均为 manylinux wheel / psycopg[binary]（自带 libpq），运行时无系统库链接。
# ============================================================================

# ---------- Stage 1: builder（依赖安装，可缓存） ----------
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1

RUN set -eux; \
    printf '%s\n' \
        "deb https://mirrors.tuna.tsinghua.edu.cn/debian trixie main contrib non-free" \
        "deb https://mirrors.tuna.tsinghua.edu.cn/debian trixie-updates main contrib non-free" \
        "deb https://mirrors.tuna.tsinghua.edu.cn/debian-security trixie-security main contrib non-free" \
        > /etc/apt/sources.list; \
    rm -f /etc/apt/sources.list.d/*; \
    apt-get update; \
    apt-get install -y --no-install-recommends build-essential; \
    rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --upgrade pip -i https://mirrors.ustc.edu.cn/pypi/simple && \
    pip install -r requirements.txt -i https://mirrors.ustc.edu.cn/pypi/simple

# ---------- Stage 2: server（后端运行时） ----------
FROM python:3.12-slim AS server

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    PATH="/opt/venv/bin:$PATH"

# 仅拷贝 venv，不带编译器/pip 缓存
COPY --from=builder /opt/venv /opt/venv

COPY agentix ./agentix
COPY data ./data
COPY scripts ./scripts
COPY scripts/backend_entrypoint.sh ./backend_entrypoint.sh
RUN chmod +x /app/backend_entrypoint.sh && \
    sed -i 's/\r$//' /app/backend_entrypoint.sh

# Build-time arguments for LightRAG initialization (使用 .env 文件中的变量名)
ARG INIT_LIGHTRAG_ON_BUILD=false
ARG LLM_API_KEY
ARG LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
ARG LLM_MODEL=qwen3-max
ARG EMBEDDING_API_KEY
ARG EMBEDDING_MODEL=qwen/qwen3-embedding-8b
ARG EMBEDDING_BASE_URL=http://139.224.116.116:3000/v1
ARG EMBEDDING_DIMENSION=4096
ARG LIGHTRAG_INIT_LIMIT=1000

# Persist configuration for runtime
ENV LLM_BASE_URL=${LLM_BASE_URL}
ENV LLM_MODEL=${LLM_MODEL}
ENV EMBEDDING_MODEL=${EMBEDDING_MODEL}
ENV EMBEDDING_BASE_URL=${EMBEDDING_BASE_URL}
ENV EMBEDDING_DIMENSION=${EMBEDDING_DIMENSION}
ENV LIGHTRAG_BOOTSTRAP_DIR=/app/bootstrap/lightrag_template

# Initialize LightRAG data during build (if enabled)
# LightRAG 需要 OPENAI_API_KEY 环境变量，所以这里做映射
RUN if [ "$INIT_LIGHTRAG_ON_BUILD" = "true" ] && [ -n "$LLM_API_KEY" ]; then \
        echo "========================================"; \
        echo "Initializing LightRAG during build..."; \
        echo "========================================"; \
        mkdir -p ${LIGHTRAG_BOOTSTRAP_DIR} /app/data/lightrag; \
        export OPENAI_API_KEY=${LLM_API_KEY}; \
        export OPENAI_API_BASE=${LLM_BASE_URL}; \
        export OPENAI_MODEL=${LLM_MODEL}; \
        export EMBEDDING_API_KEY=${EMBEDDING_API_KEY}; \
        export EMBEDDING_MODEL=${EMBEDDING_MODEL}; \
        export EMBEDDING_BASE_URL=${EMBEDDING_BASE_URL}; \
        export EMBEDDING_DIMENSION=${EMBEDDING_DIMENSION}; \
        export LIGHTRAG_WORKING_DIR=${LIGHTRAG_BOOTSTRAP_DIR}; \
        python scripts/init_lightrag.py \
            --source json \
            --json-path /app/data/legal/legal_lightrag_corpus.json \
            ${LIGHTRAG_INIT_LIMIT:+--limit $LIGHTRAG_INIT_LIMIT} && \
        cp -a ${LIGHTRAG_BOOTSTRAP_DIR}/. /app/data/lightrag/ && \
        echo "========================================"; \
        echo "LightRAG initialization completed!"; \
        echo "Generated files:"; \
        ls -lh /app/data/lightrag/; \
        echo "========================================"; \
    else \
        echo "Skipping LightRAG initialization during build"; \
        echo "  INIT_LIGHTRAG_ON_BUILD=${INIT_LIGHTRAG_ON_BUILD}"; \
        echo "  LLM_API_KEY set: $([ -n \"${LLM_API_KEY}\" ] && echo 'yes' || echo 'no')"; \
        mkdir -p ${LIGHTRAG_BOOTSTRAP_DIR} /app/data/lightrag; \
    fi

# Clear build-time API keys (empty by default; inject at runtime via .env / compose).
# Placeholder strings like "your_api_key_here" would otherwise ship baked into the image
# and can be mistaken for a configured key.
ENV LLM_API_KEY=
ENV EMBEDDING_API_KEY=

EXPOSE 8000
ENTRYPOINT ["./backend_entrypoint.sh"]
CMD ["uvicorn", "agentix.main:application", "--host", "0.0.0.0", "--port", "8000"]


# ---------- Stage 3: neo4j_seeded（预导入法律 KG，保持原逻辑不变） ----------
FROM neo4j:5.18 AS neo4j_seeded

# neo4j-admin 的 JVM 用 **locale** 决定 argv 的解码字符集（sun.jnu.encoding）。
# 镜像默认 locale 是 POSIX/ASCII，于是 `--nodes=法条=…` 里的中文在进 JVM 之前就
# 变成 `?`：导入照样成功、库里的 label 却是 `??????`，运行时 `MATCH (f:法条)`
# 恒 0 行且毫无报错。这一行是灌库链路能用的前提（实测取证见 .tmp/kg_preflight.py）。
ENV LANG=C.UTF-8 LC_ALL=C.UTF-8

USER root
# Use faster mirror and install Python
RUN sed -i 's|http://deb.debian.org|https://mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list && \
    sed -i 's|http://security.debian.org|https://mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list && \
    apt-get update && \
    apt-get install -y --no-install-recommends python3 python3-pip curl && \
    rm -rf /var/lib/apt/lists/*

# Install APOC plugin for Neo4j
ARG APOC_VERSION=5.18.0
COPY docker/neo4j/plugins/apoc-${APOC_VERSION}.jar /var/lib/neo4j/plugins/
RUN ln -sf /var/lib/neo4j/plugins/apoc-${APOC_VERSION}.jar /var/lib/neo4j/plugins/apoc.jar

WORKDIR /var/lib/neo4j/build
COPY data ./data
COPY scripts ./scripts

ENV PYTHONPATH=/var/lib/neo4j/build
RUN mkdir -p ./import_generated && \
    chown -R neo4j:neo4j /var/lib/neo4j/build

USER neo4j
# 法律法规领域：由法律本体种子生成 Neo4j 导入用 CSV
RUN python3 scripts/legal_kg_to_csv.py --seed-json data/legal/legal_seed.json --output-dir import_generated

USER root
RUN rm -rf /data/databases /data/transactions && mkdir -p /data/databases /data/transactions && chown -R neo4j:neo4j /data

USER neo4j
# Label / 关系类型必须用 docs/design/测试数据域-本体Schema.md §1.1–1.2 的中文功能性
# 标识符（法条/章节/法规/案由/司法解释/机关）。历史上这里是英文（Article/…），
# 与运行时 Cypher 的 `MATCH (f:法条)` 各说一套：导入不报错、图也在，
# 但每条图谱查询恒返 0 行，graphrag 静默退化。守卫见
# tests/test_p122_kg_csv_referential_integrity.py。
RUN neo4j-admin database import full --overwrite-destination=true --multiline-fields=true neo4j \
    --nodes=法规=import_generated/regulation_nodes.csv \
    --nodes=法条=import_generated/article_nodes.csv \
    --nodes=章节=import_generated/chapter_nodes.csv \
    --nodes=案由=import_generated/cause_nodes.csv \
    --nodes=司法解释=import_generated/interpretation_nodes.csv \
    --nodes=机关=import_generated/authority_nodes.csv \
    --relationships=属于=import_generated/rel_article_belongs_chapter.csv \
    --relationships=属于=import_generated/rel_chapter_belongs_regulation.csv \
    --relationships=引用=import_generated/rel_article_cites_article.csv \
    --relationships=对应案由=import_generated/rel_article_cause.csv \
    --relationships=有司法解释=import_generated/rel_article_interpretation.csv \
    --relationships=制定机关=import_generated/rel_regulation_authority.csv \
    --relationships=制定机关=import_generated/rel_interpretation_authority.csv \
    --relationships=上位法=import_generated/rel_upper_law.csv

WORKDIR /var/lib/neo4j
