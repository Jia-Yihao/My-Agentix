#!/usr/bin/env python
"""
LightRAG 初始化脚本

从 Neo4j 测试数据集图谱导出数据并导入到 LightRAG，用于图谱检索增强
支持两种模式:
1. 从 Neo4j 导入完整数据条目数据
2. 从 JSON 文件导入测试数据集数据
"""
import asyncio
import sys
from pathlib import Path
from typing import List, Dict, Any
import json

# 添加项目根目录到路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from agentix.config import settings
from agentix.infrastructure.core.logger import get_logger
from agentix.application.agents.kg_sub_graph.agentic_rag_agents.components.customer_tools.node import LightRAGAPI

logger = get_logger(service="init-lightrag")


def format_legal_document(record: Dict[str, Any]) -> str:
    """
    将 Neo4j / JSON 记录格式化为 LightRAG 文档格式

    Parameters
    ----------
    record : Dict[str, Any]
        数据条目记录，字段可为 title/content/数据集/条号/内容/关键词

    Returns
    -------
    str
        格式化后的测试数据集文档
    """
    title = record.get("title") or ""
    content = record.get("content") or record.get("内容") or ""
    law = record.get("法规") or ""
    article_no = record.get("条号") or ""
    keywords = record.get("关键词") or record.get("keywords") or []

    if not title and (law or article_no):
        if law and article_no:
            title = f"《{law}》第{article_no}条"
        elif law:
            title = f"《{law}》"

    # 格式化文档
    doc_parts = [f"# {title}\n"] if title else []

    if law:
        doc_parts.append(f"**法规**: {law}")

    if article_no:
        doc_parts.append(f"**条号**: {article_no}")

    if keywords:
        if isinstance(keywords, str):
            keywords = [item for item in keywords.split(";") if item]
        doc_parts.append(f"**关键词**: {', '.join(keywords)}")

    if content:
        doc_parts.append(f"\n{content}")

    return "\n".join(doc_parts)


async def import_from_neo4j(lightrag: LightRAGAPI, limit: int = None) -> int:
    """
    从 Neo4j 导入数据条目数据到 LightRAG

    Parameters
    ----------
    lightrag : LightRAGAPI
        LightRAG API 实例
    limit : int, optional
        限制导入的数据条目数量，用于测试

    Returns
    -------
    int
        导入的文档数量
    """
    try:
        from neo4j import GraphDatabase

        logger.info("开始从 Neo4j 导入数据条目数据")

        # 连接 Neo4j
        driver = GraphDatabase.driver(
            settings.NEO4J_URI,
            auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD)
        )

        documents = []

        with driver.session(database=settings.NEO4J_DATABASE) as session:
            # 构建查询：测试数据集图谱的「数据条目」节点
            query = """
            MATCH (f:法条)
            RETURN f.法规 as 法规,
                   f.条号 as 条号,
                   f.内容 as 内容,
                   f.关键词 as 关键词
            """

            if limit:
                query += f" LIMIT {limit}"

            logger.info(f"执行 Neo4j 查询{'（限制 ' + str(limit) + ' 条）' if limit else ''}")
            result = session.run(query)

            for record in result:
                record_dict = dict(record)
                doc = format_legal_document(record_dict)
                documents.append(doc)

                if len(documents) % 10 == 0:
                    logger.info(f"已准备 {len(documents)} 个文档")

        driver.close()

        logger.info(f"共准备了 {len(documents)} 个数据条目文档")

        # 批量插入到 LightRAG
        logger.info("开始插入文档到 LightRAG")
        result = await lightrag.insert_documents(documents)

        logger.info(f"导入完成: 总数={result['total']}, 成功={result['success']}, 失败={result['error']}")

        return result['success']

    except ImportError:
        logger.error("neo4j 包未安装，请运行 'pip install neo4j'")
        raise
    except Exception as e:
        logger.error(f"从 Neo4j 导入失败: {str(e)}", exc_info=True)
        raise


async def import_from_json(lightrag: LightRAGAPI, json_path: str, limit: int = None) -> int:
    """
    从 JSON 文件导入测试数据集数据到 LightRAG

    Parameters
    ----------
    lightrag : LightRAGAPI
        LightRAG API 实例
    json_path : str
        JSON 文件路径
    limit : int, optional
        限制导入的数据条目数量，用于测试

    Returns
    -------
    int
        导入的文档数量
    """
    try:
        logger.info(f"开始从 JSON 文件导入: {json_path}")

        with open(json_path, encoding="utf-8") as f:
            raw_records = json.load(f)

        records = _normalize_legal_payload(raw_records)

        if limit is not None and limit > 0:
            records = records[:limit]

        documents = []

        for record in records:
            # 假设 JSON 格式与 Neo4j 记录格式相似
            doc = format_legal_document(record)
            documents.append(doc)

            if len(documents) % 10 == 0:
                logger.info(f"已准备 {len(documents)} 个文档")

        logger.info(f"共准备了 {len(documents)} 个数据条目文档")

        # 批量插入到 LightRAG
        logger.info("开始插入文档到 LightRAG")
        result = await lightrag.insert_documents(documents)

        logger.info(f"导入完成: 总数={result['total']}, 成功={result['success']}, 失败={result['error']}")

        return result['success']

    except Exception as e:
        logger.error(f"从 JSON 导入失败: {str(e)}", exc_info=True)
        raise


def _normalize_legal_payload(raw: Any) -> List[Dict[str, Any]]:
    """
    将 JSON 原始数据统一转换为列表格式，确保包含 title 等关键字段。
    支持以下输入格式：
      1. 列表: [ {...}, {...} ]
      2. 字典: { "标题": {...}, ... }
    """
    records: List[Dict[str, Any]] = []

    if isinstance(raw, dict):
        for idx, (title, payload) in enumerate(raw.items(), start=1):
            record = _coerce_legal_entry(payload)
            if "title" not in record or not record["title"]:
                record["title"] = title or f"文档{idx}"
            records.append(record)
        return records

    if isinstance(raw, list):
        for idx, payload in enumerate(raw, start=1):
            record = _coerce_legal_entry(payload)
            if "title" not in record or not record["title"]:
                fallback_title = (
                    record.get("name")
                    or record.get("标题")
                )
                if fallback_title:
                    record["title"] = fallback_title
                else:
                    record["title"] = f"文档{idx}"
            records.append(record)
        return records

    raise ValueError("不支持的测试数据集数据格式，期望为 list 或 dict。")


def _coerce_legal_entry(entry: Any) -> Dict[str, Any]:
    """确保每个数据条目条目是 dict，并复制一份以避免修改原对象。"""
    if isinstance(entry, dict):
        return dict(entry)
    return {"title": str(entry), "content": ""}  # 最少保留标题


async def main():
    """主函数"""
    import argparse

    parser = argparse.ArgumentParser(description="LightRAG 初始化脚本")
    parser.add_argument(
        "--source",
        type=str,
        choices=["neo4j", "json"],
        default="neo4j",
        help="数据源类型 (neo4j 或 json)"
    )
    parser.add_argument(
        "--json-path",
        type=str,
        default=settings.LEGAL_LIGHTRAG_JSON_PATH,
        help="JSON 文件路径（当 source=json 时使用）"
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="限制导入数量（用于测试）"
    )
    parser.add_argument(
        "--working-dir",
        type=str,
        default=None,
        help="LightRAG 工作目录（覆盖配置文件）"
    )

    args = parser.parse_args()

    try:
        # 创建 LightRAG API 实例
        logger.info("初始化 LightRAG")
        lightrag = LightRAGAPI(working_dir=args.working_dir)

        # 根据数据源类型导入
        if args.source == "neo4j":
            count = await import_from_neo4j(lightrag, limit=args.limit)
        elif args.source == "json":
            count = await import_from_json(lightrag, args.json_path, limit=args.limit)

        logger.info(f"✅ LightRAG 初始化完成！成功导入 {count} 个测试数据集文档")
        logger.info(f"📂 工作目录: {lightrag.working_dir}")
        logger.info(f"🔍 检索模式: {lightrag.retrieval_mode}")

    except Exception as e:
        logger.error(f"❌ 初始化失败: {str(e)}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
