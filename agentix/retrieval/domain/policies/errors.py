"""错误脱敏与分类：对外只暴露用户可行动的信息，细节留在服务端日志。"""
from enum import Enum


class ErrorCode(str, Enum):
    PARSE_FAILED = "parse_failed"
    EMBEDDING_UNAVAILABLE = "embedding_unavailable"
    VECTOR_BUILD_FAILED = "vector_build_failed"
    STORAGE_FAILED = "storage_failed"
    TIMEOUT = "timeout"
    UNKNOWN = "unknown"


USER_MESSAGES = {
    ErrorCode.PARSE_FAILED: "文档解析失败，请确认 PDF 未损坏或未加密",
    ErrorCode.EMBEDDING_UNAVAILABLE: "向量服务暂时不可用，请稍后重试",
    ErrorCode.VECTOR_BUILD_FAILED: "索引构建失败，请稍后重试",
    ErrorCode.STORAGE_FAILED: "文件存储失败，请检查磁盘空间",
    ErrorCode.TIMEOUT: "索引任务超时，请减少单次上传的文档数量后重试",
    ErrorCode.UNKNOWN: "索引任务失败，请稍后重试或联系管理员",
}


def classify_message(name: str, message: str) -> ErrorCode:
    """按「异常类型名 + 消息文本」判定错误类别。

    单独抽出来是因为生产代码里有**两种**错误载体：异常对象
    （``sanitize_error(e)``）与**已序列化成字符串**的失败原因
    （``index_worker`` 把 ``result.error`` 同时喂给 ``classify`` 与
    ``sanitize_error``）。两条路径必须用同一套判据，否则同一个错误会
    得到两个不一致的结论——数据库里 ``error_code=parse_failed``，
    而给用户看的是"索引任务失败，请稍后重试"，排查时根本对不上。

    :param name: 异常类型名（字符串载体时传空串）
    :param message: 错误消息（大小写不敏感）
    """
    name = (name or "").lower()
    msg = (message or "").lower()
    if "timeout" in name or "timeout" in msg:
        return ErrorCode.TIMEOUT
    # ``pymupdf`` 一并匹配：新版 PyMuPDF 的模块名不再含 ``fitz``，
    # 只靠 "fitz"/"pdf" 匹配会漏掉 "No module named 'pymupdf'" 这类报错
    # （实际能命中纯属 "pymupdf" 里恰好含 "pdf"，不该依赖这种巧合）。
    if "fitz" in msg or "pymupdf" in msg or "pdf" in msg or "parse" in name:
        return ErrorCode.PARSE_FAILED
    if "embed" in msg or "embed" in name:
        return ErrorCode.EMBEDDING_UNAVAILABLE
    if "faiss" in msg or "vector" in msg:
        return ErrorCode.VECTOR_BUILD_FAILED
    if "s3" in msg or "minio" in msg or "storage" in msg:
        return ErrorCode.STORAGE_FAILED
    return ErrorCode.UNKNOWN


def classify(exc: BaseException) -> ErrorCode:
    """按异常对象判定错误类别（字符串载体请用 :func:`classify_message`）。"""
    return classify_message(type(exc).__name__, str(exc))


def sanitize_error(exc: BaseException | str | None) -> str:
    """把错误转成对用户可见的固定话术（细节留在服务端日志）。

    ``str`` 入参同样参与分类：此前走的是一条「直接返回 UNKNOWN」的短路，
    于是同一个失败原因，``classify()`` 判 ``parse_failed``、
    ``sanitize_error()`` 却回"索引任务失败，请稍后重试"——同一个错误
    在落库字段与用户提示之间自相矛盾。
    """
    if exc is None:
        return USER_MESSAGES[ErrorCode.UNKNOWN]
    code = classify(exc) if isinstance(exc, BaseException) else classify_message("", str(exc))
    return USER_MESSAGES[code]
