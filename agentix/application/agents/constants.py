"""
Shared constants for agent modules.
"""

SCOPE_DESCRIPTION = """
    测试数据集问答助手服务范围：基于内置测试数据集提供条目查询与内容说明，包括但不限于：

    📚 数据条目查询与释义
    - 示例数据集 A/B/C/D 中各类数据条目的具体内容
    - 数据条目的关键词、适用场景与核心说明
    - 数据条目之间的引用与关联关系

    🗂️ 数据分类与条目匹配
    - 各类问题（示例文档、异常行为、处理流程等）对应的数据条目
    - 数据分类与对应数据条目的匹配分析

    🏛️ 示例说明与数据背景
    - 示例说明、补充说明等配套内容的查询
    - 数据来源、条目沿革与数据规则说明

    🏢 数据来源与层级
    - 测试数据集的来源、编号与层级
    - 上级数据集与下级数据集的关系梳理

    暂不支持：政治敏感话题、娱乐八卦、新闻时事、天气预报、医疗诊断、具体业务代办等非测试数据问答内容。
    如遇此类问题，我会礼貌地引导您回到测试数据集问答话题～
"""

GUARDRAILS_REJECT_MESSAGE = "尊敬的用户您好～抱歉，这个问题不太属于我们的测试数据集问答范围呢，我主要帮您解答数据条目、数据分类与内容说明相关的问题～"

IMAGE_NOT_FOUND_MESSAGE = "抱歉，我无法查看这张图片，请重新上传。"

FILE_NOT_FOUND_MESSAGE = "抱歉，未找到该文件，请确认路径是否正确。"

FILE_TOO_LARGE_MESSAGE = "文件过大（>{max_mb}MB），请分割后重新上传。"

# ⚠️ 这条文案此前写的是「当前仅支持 .txt/.md/.json/.csv/.log/.xlsx/.xls」——
# **它撒了谎**：这 7 个后缀里只有 .xlsx/.xls 有可用实现（且需服务端配置
# INGEST_SERVICE_URL），其余全是走不通的路径。文案与实现不一致比没有文案更糟：
# 用户会照着它准备文件，然后卡在一个永远不会成功的入口上。
# 现在只列**真的有实现**的入口（对应 file_handler.create_file_query 的分支）。
FILE_TYPE_UNSUPPORTED_MESSAGE = (
    "暂不支持该文件类型：{suffix}。"
    "聊天上传当前仅支持 .xlsx/.xls（需服务端配置外部接入服务）；"
    "PDF 请通过「文档管理」上传。"
)

# 文本类文件（.txt/.md/.csv/.log/.json）**当前无法入库**：入库侧的
# KnowledgeGateway.add_document 是显式未实现的桩，而检索域上传接口
# （POST /api/v1/retrieval/documents）只收 PDF。
# 属**永久性**不支持，因此绝不能用 FILE_INGEST_ERROR_MESSAGE
# （「请稍后再试」会把用户引向一个永远试不通的重试循环）。
FILE_TEXT_INGEST_UNAVAILABLE_MESSAGE = (
    "暂不支持直接导入文本文件（{suffix}）。"
    "当前知识库入库仅接收 PDF：请将内容转为 PDF 后通过「文档管理」上传。"
)

# PDF 走聊天上传是**用错入口**（不是不支持），需要明确指到文档管理。
FILE_PDF_USE_DOCUMENT_API_MESSAGE = (
    "PDF 文件请通过「文档管理」上传：聊天上传通道仅把 Excel 转发给外部接入服务，"
    "PDF 的切分 / 向量化 / 索引由文档管理接口负责。"
)

FILE_INGEST_ERROR_MESSAGE = "文件导入出现异常，请稍后再试或联系管理员。"
