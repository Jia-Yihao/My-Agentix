"""
Shared constants for agent modules.
"""

SCOPE_DESCRIPTION = """
    法律法规智能助手服务范围：为您提供专业的法律条文查询与法律咨询服务，包括但不限于：

    ⚖️ 法律条文查询与释义
    - 民法典、刑法、行政法等各类法律法规的具体条文内容
    - 法条的关键词、适用情形与核心规定解读
    - 法条之间的引用与对应关系梳理

    📘 案由与法条适用
    - 各类纠纷（合同违约、侵权、劳动争议、刑事犯罪等）适用的法律规定
    - 案由与对应法条的匹配分析

    🏛️ 司法解释与立法背景
    - 两高司法解释、批复等配套规定的查询
    - 立法背景、法条沿革与法律原则释义

    🏢 机关与效力层级
    - 法律法规的制定机关、发文字号与效力层级
    - 上位法与下位法关系梳理

    暂不支持：政治敏感话题、娱乐八卦、新闻时事、天气预报、医疗诊断、具体个案代理等非法律咨询内容。
    如遇此类问题，我会礼貌地引导您回到法律法规咨询话题～
"""

GUARDRAILS_REJECT_MESSAGE = "尊敬的用户您好～抱歉，这个问题不太属于我们的法律法规咨询范围呢，我主要帮您解答法律条文、法条适用与法律概念相关的问题～⚖️"

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
