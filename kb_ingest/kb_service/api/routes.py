
import logging
import re
import tempfile
import shutil
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status, UploadFile, File, Form
from openai import APIConnectionError, AuthenticationError, OpenAIError, RateLimitError

from kb_service.api.deps import get_config, require_api_key
from kb_service.core.config import Config, clone_config
from kb_service.core.db_url_guard import ConnectionUrlError, enforce_connection_url
from kb_service.core.sql_filters import FilterError, enforce_where_policy
from kb_service.schemas.ingest import ExcelIngestRequest, MySQLIngestRequest
from kb_service.schemas.search import SearchRequest, HybridSearchRequest
from kb_service.services.processor import DataProcessor
from kb_service.services.search import VectorSearcher
from kb_service.services.mysql_ingest import MySQLIngestor

logger = logging.getLogger(__name__)

# 2026-09：鉴权挂钩。**挂在整个 router 上**而不是逐个端点加 —
# 逐个加的问题是新端点会默认漏掉（这正是本服务此前的状态：5 个端点全裸）。
# 挂在 router 上，新增端点自动继承，忘记加是"不可能"而不是"要靠记得"。
# ``/health`` 定义在 main.py 的 app 上、不经过本 router，因此保持免鉴权
# （compose 的 healthcheck 依赖它）。
router = APIRouter(dependencies=[Depends(require_api_key)])

#: 文件名里不许出现的字符（Windows 保留字符 + 控制字符）。
#: 分隔符 ``/`` ``\`` 在取 basename 时已被吃掉，这里再兜一层。
_UNSAFE_FILENAME_CHARS = re.compile(r'[\x00-\x1f<>:"/\\|?*]')

#: Windows 保留设备名。写 ``NUL.xlsx`` 之类会命中设备而不是文件。
_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def _safe_temp_path(temp_dir: str, filename: str) -> Path:
    """把客户端提供的文件名安全地落到 ``temp_dir`` 内，返回可直接写入的路径。

    **为什么不能直接写 ``Path(temp_dir) / filename``**（原实现的写法）：
    ``UploadFile.filename`` 完全由客户端控制，实测两处逃逸（本机 Windows 复现）::

        Path(r'D:\\tmp') / '../../evil.xlsx'      -> D:\\tmp\\..\\..\\evil.xlsx   # 穿出去
        Path(r'D:\\tmp') / 'C:/Windows/evil.xlsx' -> C:\\Windows\\evil.xlsx       # 整个替换 base

    而 ``/ingest/excel/upload`` 当时**没有任何鉴权**、端口还发布到宿主（compose 里
    ``8100:8000``）——于是这等于一个"任意路径写入"接口（后缀被限制为
    ``.xlsx/.xls``，仍可覆盖主机上任一可写的 Excel 文件）。
    这与本函数所在端点的自述意图（"文件会保存到临时目录"）直接矛盾，属缺陷而非设计。

    > 2026-09 后续：**鉴权已补上**（``api/deps.py::require_api_key``，挂在
    > router 级、全部端点继承）。因此现在要利用这条路径得先过 ``X-API-Key``。
    > 但本函数**仍然保留**——鉴权是"谁能进来"，路径净化是"进来之后能去哪"，
    > 两者不可互相替代（内部调用方也是"有凭证但可能传坏数据"的来源）。

    处理：取 basename → 洗掉保留字符 → 拒绝空名/点名/保留设备名 →
    ``resolve()`` 后**用 ``is_relative_to`` 断言仍在 temp_dir 内**。
    最后一步刻意不用字符串前缀比较：``str.startswith`` 会放过
    ``/app/uploads_evil`` 这类"前缀相同的兄弟目录"（见 ``agentix`` 侧同类加固）。
    """
    if not filename:
        raise HTTPException(status_code=400, detail="文件名不能为空")

    # basename：同时吃掉 POSIX 与 Windows 两种分隔符
    name = Path(filename.replace("\\", "/")).name
    name = _UNSAFE_FILENAME_CHARS.sub("_", name).strip()

    if not name or name in {".", ".."}:
        raise HTTPException(status_code=400, detail="文件名不合法")
    if Path(name).stem.upper() in _WINDOWS_RESERVED:
        raise HTTPException(status_code=400, detail="文件名不合法")

    root = Path(temp_dir).resolve()
    target = (root / name).resolve()
    if not target.is_relative_to(root):
        raise HTTPException(status_code=400, detail="文件名不合法")
    return target


@router.post(
    "/ingest/excel",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Process an Excel file and push embeddings into Milvus",
)
def ingest_excel(payload: ExcelIngestRequest, background: BackgroundTasks, config: Config = Depends(get_config)):
    """按**主机本地路径**读取 Excel 并入库。

    ⚠️ **安全边界（2026-09 审计标注；鉴权已补，路径白名单仍未做）**

    本端点接受调用方给出的任意主机路径（``payload.excel_path``）并读取它。
    这是**刻意设计**——运维需要直接喂 ``/app/data/测试数据数据条目源头.xlsx``
    这类已挂载的数据文件，走上传会多一份拷贝。

    风险前提与当前状态（**已随本次改动更新，此前写的是"没有任何鉴权"**）：
    - ✅ 鉴权：**已补**。``api/deps.py::require_api_key`` 挂在 router 级，
      开启 ``KB_REQUIRE_API_KEY=true`` 后本端点要求 ``X-API-Key``。
      默认仍为 ``false``（保持既有部署行为），此时启动期会大声告警。
    - ⚠️ 路径白名单：**仍未做**。只要调用方能过鉴权（或鉴权未开启），
      仍可读主机上任意可读文件，内容经切分与 embedding 进入 Milvus，
      进而可被检索接口读出。

    因此：**该端口只允许在受信网络/内网暴露**，不得直接映射到公网。
    若将来要开放，最小改造是给 ``excel_path`` 加**允许根目录白名单**
    （默认 ``settings.DATA_DIR``）——但那会改变现有调用语义（运维确实会传
    任意已挂载路径），故本次只标注、不擅自改。
    """
    excel_path = Path(payload.excel_path)
    if not excel_path.exists():
        raise HTTPException(status_code=404, detail=f"Excel file not found: {excel_path}")

    def run_ingest() -> None:
        try:
            local_config = clone_config(config)
            local_config.excel_file_path = str(excel_path)
            processor = DataProcessor(local_config, incremental=payload.incremental)
            processor.process_excel()
        except Exception:  # pragma: no cover - background task logging
            logger.exception("Excel ingest failed for %s", excel_path)

    background.add_task(run_ingest)
    mode = "增量" if payload.incremental else "全量"
    logger.info("Excel ingest queued for %s (模式: %s)", excel_path, mode)
    return {
        "message": "ingest started",
        "path": str(excel_path),
        "mode": "incremental" if payload.incremental else "full"
    }


@router.post(
    "/ingest/excel/upload",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Upload an Excel file and push embeddings into Milvus",
)
async def upload_and_ingest_excel(
    background: BackgroundTasks,
    file: UploadFile = File(..., description="Excel file to upload"),
    incremental: bool = Form(False, description="Whether to use incremental mode"),
    config: Config = Depends(get_config)
):
    """
    上传 Excel 文件并处理：
    - 支持主机任意路径的文件上传
    - 文件会保存到临时目录
    - 处理完成后自动清理临时文件
    """
    # 验证文件类型（filename 可能为 None → AttributeError 500，应 400）
    if not file.filename or not file.filename.endswith(('.xlsx', '.xls')):
        raise HTTPException(
            status_code=400,
            detail="只支持 Excel 文件 (.xlsx, .xls)"
        )

    # 创建临时文件
    temp_dir = tempfile.mkdtemp()
    # 2026-09 修复：原来写作 ``Path(temp_dir) / file.filename``——
    # file.filename 由客户端控制，``..\..\x.xlsx`` / ``C:/Windows/x.xlsx``
    # 都会写到 temp_dir 之外（见 _safe_temp_path 的实测记录）。
    temp_file_path = _safe_temp_path(temp_dir, file.filename)

    try:
        # 保存上传的文件
        with open(temp_file_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

        logger.info("文件已上传到临时目录: %s", temp_file_path)

        # 定义后台任务
        def run_ingest() -> None:
            try:
                local_config = clone_config(config)
                local_config.excel_file_path = str(temp_file_path)
                processor = DataProcessor(local_config, incremental=incremental)
                processor.process_excel()
                logger.info("Excel 处理完成，文件: %s", temp_file_path)
            except Exception:
                logger.exception("Excel ingest failed for %s", temp_file_path)
            finally:
                # 清理临时文件
                try:
                    shutil.rmtree(temp_dir)
                    logger.info("临时文件已清理: %s", temp_dir)
                except Exception:
                    logger.exception("清理临时文件失败: %s", temp_dir)

        background.add_task(run_ingest)
        mode = "增量" if incremental else "全量"
        logger.info("Excel 上传处理已加入队列: %s (模式: %s)", file.filename, mode)

        return {
            "message": "文件上传成功，处理已开始",
            "filename": file.filename,
            "mode": "incremental" if incremental else "full",
            "size_bytes": temp_file_path.stat().st_size
        }

    except Exception as e:
        # 如果出错，清理临时文件
        shutil.rmtree(temp_dir, ignore_errors=True)
        logger.exception("文件上传失败")
        raise HTTPException(status_code=500, detail=f"文件上传失败: {str(e)}")


@router.post(
    "/ingest/mysql",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Fetch rows from MySQL, rewrite/flatten, and store embeddings",
)
def ingest_mysql(
    payload: MySQLIngestRequest,
    background: BackgroundTasks,
    config: Config = Depends(get_config),
):
    # G12：闸门在**入队前**同步判定。放进后台任务的话，客户端拿到的是 202 +
    # 一条服务端异常日志，调用方根本不知道自己传的 URL 被拒了。
    # detail 只写拒绝原因，不回显 payload.connection_url（里面可能有密码）。
    try:
        enforce_connection_url(payload.connection_url, config)
    except ConnectionUrlError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"connection_url rejected by KB_MYSQL_URL_GUARD: {exc}",
        ) from exc

    # ⑫：WHERE 口径同样在入队前判掉（理由与上面一致：202 + 后台日志等于没告诉调用方
    # 他被拒了）。detail 只说规则名与原因，不回显 `where`/`filters` 的值。
    try:
        enforce_where_policy(
            config.mysql_where_parametrized, payload.where, payload.filters
        )
    except FilterError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"where/filters rejected by KB_MYSQL_WHERE_PARAMETRIZED: {exc}",
        ) from exc

    payload_data = payload.model_dump()

    def run_ingest() -> None:
        try:
            local_config = clone_config(config)
            ingestor = MySQLIngestor(local_config)
            ingestor.ingest(MySQLIngestRequest(**payload_data))
        except Exception:  # pragma: no cover - background task logging
            logger.exception("MySQL ingest failed for %s", payload_data.get('table'))

    background.add_task(run_ingest)
    logger.info("MySQL ingest queued for %s", payload.table)
    return {"message": "ingest started", "table": payload.table}


@router.post(
    "/search",
    status_code=status.HTTP_200_OK,
    summary="Query Milvus for similar records",
)
def vector_search(payload: SearchRequest, config: Config = Depends(get_config)):
    searcher = VectorSearcher(config)
    try:
        results = searcher.search_similar(
            query=payload.query,
            top_k=payload.top_k,
            threshold=payload.threshold,
            metric=payload.metric,
            company_filter=payload.company_filter,
            source_tables=payload.source_tables,
            effective_on=payload.effective_on,
            source_authority_level=payload.source_authority_level,
        )
        return {"query": payload.query, "results": results, "count": len(results)}
    except AuthenticationError:
        # Treat as service configuration issue (upstream embedding provider auth).
        # Do not leak any credential details into API responses.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Embedding provider authentication failed. Check KB_EMBEDDING_API_KEY/KB_EMBEDDING_BASE_URL.",
        )
    except (RateLimitError, APIConnectionError):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Embedding provider unavailable (rate limited or connection error).",
        )
    except OpenAIError:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Embedding provider returned an error.",
        )


@router.post(
    "/search/hybrid",
    status_code=status.HTTP_200_OK,
    summary="Hybrid search with vector retrieval + reranking",
)
def hybrid_search(payload: HybridSearchRequest, config: Config = Depends(get_config)):
    searcher = VectorSearcher(config)
    try:
        return searcher.hybrid_search(
            query=payload.query,
            vector_top_k=payload.vector_top_k,
            rerank_top_k=payload.rerank_top_k,
            threshold=payload.threshold,
            metric=payload.metric,
            source_tables=payload.source_tables,
            effective_on=payload.effective_on,
            source_authority_level=payload.source_authority_level,
        )
    except AuthenticationError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Embedding provider authentication failed. Check KB_EMBEDDING_API_KEY/KB_EMBEDDING_BASE_URL.",
        )
    except (RateLimitError, APIConnectionError):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Embedding provider unavailable (rate limited or connection error).",
        )
    except OpenAIError:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Embedding provider returned an error.",
        )
