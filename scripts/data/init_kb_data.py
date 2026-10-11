#!/usr/bin/env python3
"""
初始化知识库数据
1. 加载法律法规文本语料
2. 加载法律法规结构化语料
"""

import asyncio
import json
import aiohttp

# 配置
INGEST_URL = "http://localhost:8000"
TEXT_FILE = "data/kb/legal_corpus.txt"
LEGAL_JSON_FILE = "data/legal/legal_lightrag_corpus.json"


async def load_text_to_milvus():
    """加载法律法规文本语料到 Milvus 向量库"""
    print("\n" + "="*60)
    print("1. 加载法律法规文本语料到 Milvus 向量库")
    print("="*60)

    try:
        # 读取文本文件
        with open(TEXT_FILE, encoding='utf-8') as f:
            content = f.read()

        print(f"✅ 成功读取文本文件，大小: {len(content)} 字符")

        # 分段处理
        chunk_size = 500  # 每段500字符
        overlap = 50     # 重叠50字符

        documents = []
        for i in range(0, len(content), chunk_size - overlap):
            chunk = content[i:i + chunk_size]
            if len(chunk.strip()) > 50:  # 过滤太短的片段
                doc = {
                    "id": f"text_{i // (chunk_size - overlap)}",
                    "content": chunk,
                    "metadata": {
                        "source": "legal_corpus.txt",
                        "chunk": i // (chunk_size - overlap),
                        "type": "milvus_data"
                    }
                }
                documents.append(doc)

        print(f"✅ 分成 {len(documents)} 个文档片段")

        # 批量插入到ingest服务
        async with aiohttp.ClientSession() as session:
            url = f"{INGEST_URL}/api/v1/knowledge/documents/batch"
            payload = {"documents": documents}

            async with session.post(url, json=payload) as response:
                if response.status == 200:
                    result = await response.json()
                    print(f"✅ 成功插入 {len(documents)} 条文档到Milvus")
                    print(f"   响应: {result}")
                else:
                    error_text = await response.text()
                    print(f"❌ 插入失败: HTTP {response.status}")
                    print(f"   错误: {error_text}")

    except Exception as e:
        print(f"❌ 处理文本文件失败: {e}")
        import traceback
        traceback.print_exc()


async def load_legal_documents():
    """加载法律法规结构化语料到向量库"""
    print("\n" + "="*60)
    print("2. 加载法律法规语料 legal_lightrag_corpus.json 到向量库")
    print("="*60)

    try:
        # 读取JSON文件
        with open(LEGAL_JSON_FILE, encoding='utf-8') as f:
            records = json.load(f)

        print(f"✅ 成功读取 legal_lightrag_corpus.json，共 {len(records)} 条法条/司法解释")

        # 准备文档
        documents = []
        for i, record in enumerate(records):
            title = record.get("title", "")
            content = record.get("content", "")
            law = record.get("法规", "")
            article_no = record.get("条号", "")

            doc = {
                "id": f"legal_{i}",
                "content": f"标题: {title}\n法规: {law}\n条号: {article_no}\n正文: {content}",
                "metadata": {
                    "source": "legal_lightrag_corpus.json",
                    "title": title,
                    "law": law,
                    "article_no": article_no,
                    "type": "legal_data"
                }
            }
            documents.append(doc)

        print(f"✅ 准备了 {len(documents)} 条法律法规文档")

        # 批量插入到ingest服务
        async with aiohttp.ClientSession() as session:
            url = f"{INGEST_URL}/api/v1/knowledge/documents/batch"
            payload = {"documents": documents}

            async with session.post(url, json=payload) as response:
                if response.status == 200:
                    result = await response.json()
                    print(f"✅ 成功插入 {len(documents)} 条法律法规文档")
                    print(f"   响应: {result}")
                else:
                    error_text = await response.text()
                    print(f"❌ 插入失败: HTTP {response.status}")
                    print(f"   错误: {error_text}")

    except Exception as e:
        print(f"❌ 处理 legal_lightrag_corpus.json 失败: {e}")
        import traceback
        traceback.print_exc()


async def check_kb_stats():
    """检查知识库统计信息"""
    print("\n" + "="*60)
    print("3. 检查知识库统计信息")
    print("="*60)

    async with aiohttp.ClientSession() as session:
        url = f"{INGEST_URL}/api/v1/knowledge/stats"

        async with session.get(url) as response:
            if response.status == 200:
                stats = await response.json()
                print("✅ 知识库统计信息:")
                for key, value in stats.get("data", {}).items():
                    print(f"   {key}: {value}")
            else:
                print(f"❌ 获取统计信息失败: HTTP {response.status}")


async def test_search():
    """测试搜索功能"""
    print("\n" + "="*60)
    print("4. 测试搜索功能")
    print("="*60)

    test_queries = [
        "民法典 民事主体",
        "刑法 正当防卫",
        "劳动合同法 试用期",
        "宪法 基本权利"
    ]

    async with aiohttp.ClientSession() as session:
        for query in test_queries:
            print(f"\n测试查询: {query}")
            print("-" * 40)

            url = f"{INGEST_URL}/api/v1/knowledge/search"
            payload = {
                "query": query,
                "top_k": 3,
                "threshold": 0.3
            }

            async with session.post(url, json=payload) as response:
                if response.status == 200:
                    result = await response.json()
                    results = result.get("results", [])

                    if results:
                        print(f"✅ 找到 {len(results)} 条结果")
                        for i, res in enumerate(results[:2], 1):
                            content = res.get("content", "")[:100]
                            source = res.get("metadata", {}).get("source", "未知")
                            print(f"  {i}. {content}... (来源: {source})")
                    else:
                        print("⚠️ 无结果")
                else:
                    print(f"❌ 搜索失败: HTTP {response.status}")


async def main():
    """主函数"""
    print("开始初始化知识库数据...")

    # 1. 加载文本数据到Milvus
    await load_text_to_milvus()

    # 2. 加载法律法规结构化语料
    await load_legal_documents()

    # 等待索引建立
    print("\n等待索引建立...")
    await asyncio.sleep(3)

    # 3. 检查统计信息
    await check_kb_stats()

    # 4. 测试搜索
    await test_search()

    print("\n" + "="*60)
    print("初始化完成！")
    print("="*60)


if __name__ == "__main__":
    asyncio.run(main())
