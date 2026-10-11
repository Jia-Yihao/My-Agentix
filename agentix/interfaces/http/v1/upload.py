"""
文件上传处理API

归属校验（审计批次 A2，2026-09-28）：
- 读路径（GET files/images）补 ``require_v1_user``——原「写要凭证、读不要」
  是刻意例外（渲染面取不到 Authorization 头），已随审计改为一致口径；
- 上传时记录 owner sidecar（``UPLOAD_DIR/.owners/<filename>.owner``，独立
  目录防止 sidecar 被读走），读/删时比对：owner 存在且非空 → 必须同身份，
  否则 404（防存在性枚举）；owner 缺失 = 历史/匿名文件 → 放行（现状）。
  无身份声明（匿名轻量域）行为不变（strangler 纪律）。
"""
import uuid
import aiofiles
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, UploadFile, File, HTTPException, Depends, status
from fastapi.responses import JSONResponse
from loguru import logger

from agentix.config import settings
from agentix.domain.policies.upload_paths import resolve_within_root
from agentix.interfaces.http.v1.deps import resolve_v1_caller, require_v1_user

router = APIRouter()

# 创建上传目录。
# 2026-09（P43）：根目录改从 settings 读，不再硬编码 ``Path("uploads")``——
# application 层的 file_handler / image_handler 需要用**同一个根**来判定
# "这个路径是不是上传目录里的"（否则它们防不住任意文件读取），
# 而按分层它们不能 import 本模块。默认值仍是 "uploads"（相对 CWD），行为不变。
UPLOAD_DIR = Path(settings.UPLOAD_DIR)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# 归属 sidecar 目录（审计批次 A2）：运行时从 UPLOAD_DIR 派生，
# 测试 monkeypatch UPLOAD_DIR 时随之生效。
OWNERS_DIRNAME = ".owners"

# 支持的文件类型
ALLOWED_FILE_TYPES = {
    '.txt', '.md', '.json', '.csv', '.log', '.xlsx', '.xls', '.pdf', '.doc', '.docx'
}

ALLOWED_IMAGE_TYPES = {
    '.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp'
}

# 最大文件大小 (从 settings 读取，单位 MB)
MAX_FILE_SIZE = settings.FILE_UPLOAD_MAX_MB * 1024 * 1024

# 分块写入大小：边读边计数防止"先全量读入内存再检查大小"的 OOM 放大
_CHUNK_SIZE = 1024 * 1024


async def _save_upload_limited(upload_file: UploadFile, dest: Path) -> int:
    """分块读取上传内容并写入 dest，超限立即中断。

    返回写入字节数；超过 MAX_FILE_SIZE 抛 413。
    避免先 read() 全量入内存（攻击者可借大文件打爆内存）。
    """
    written = 0
    overflow = False
    async with aiofiles.open(dest, "wb") as f:
        while True:
            chunk = await upload_file.read(_CHUNK_SIZE)
            if not chunk:
                break
            written += len(chunk)
            if written > MAX_FILE_SIZE:
                overflow = True
                break
            await f.write(chunk)
    if overflow:
        # 文件已随 async with 关闭后再删除半成品（Windows 上开着句柄无法删除）
        dest.unlink(missing_ok=True)
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"文件太大，最大支持 {settings.FILE_UPLOAD_MAX_MB}MB",
        )
    return written


def _safe_resolve(base: Path, filename: str) -> Path:
    """Resolve filename within base directory, preventing path traversal.

    Checks for traversal patterns before extracting the basename.

    2026-09 加固（两处）：
    1. 末段包含性判定由 ``str(resolved).startswith(str(base))`` 改为
       **领域策略** ``resolve_within_root``（内部用 ``Path.is_relative_to``）。
       原写法是**字符串前缀比较**，会放过"前缀相同的兄弟目录"——
       ``base=/app/uploads`` 时 ``/app/uploads_evil/x`` 同样满足 ``startswith``。
       当前因为下面的分隔符黑名单已把 ``/`` ``\\`` 全部拒绝，这条路走不通，
       所以**不是活跃漏洞**；但它是颗地雷：谁放宽那份黑名单（比如为了支持子目录），
       前缀比较就会立刻变成真漏洞。
    2. 判定逻辑上提到 ``domain/policies/upload_paths.py``，
       使 application 层（file_handler / image_handler 的任意文件读取防护）
       能用**同一份**语义，而不是各自重写一遍。
    """
    if not filename:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid file path",
        )

    # Block path traversal patterns in the raw filename
    forbidden_patterns = ["..", "/", "\\", "\x00"]
    for pattern in forbidden_patterns:
        if pattern in filename:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid file path",
            )

    safe_name = Path(filename).name
    resolved = resolve_within_root(base, base / safe_name)
    if resolved is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid file path",
        )
    return resolved


def _owner_sidecar(filename: str) -> Path:
    """归属 sidecar 路径（审计批次 A2）——独立于上传目录内容，防被读走。"""
    return UPLOAD_DIR / OWNERS_DIRNAME / f"{filename}.owner"


def _record_owner(filename: str, caller: Optional[str]) -> None:
    """上传时记录 owner（审计批次 A2）。

    匿名（无身份声明）不落 sidecar → 语义与历史文件一致（可读）。
    sidecar 写失败不阻断上传（归属是加固，不是上传主链路）。
    """
    if caller is None:
        return
    try:
        sidecar = _owner_sidecar(filename)
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        sidecar.write_text(caller, encoding="utf-8")
    except OSError:
        logger.warning(f"上传归属 sidecar 写入失败: {filename}")


def _owner_allows(filename: str, caller: Optional[str]) -> bool:
    """读/删时的归属比对（审计批次 A2）。

    - sidecar 缺失（历史/匿名文件）→ 放行（现状）；
    - sidecar 有 owner：持身份且相等 → 放行；否则拒绝（端点回 404）。
    """
    sidecar = _owner_sidecar(filename)
    try:
        if not sidecar.exists():
            return True
        owner = sidecar.read_text(encoding="utf-8").strip()
    except OSError:
        return True
    if not owner:
        return True
    return caller is not None and owner == caller


@router.post("/file", dependencies=[Depends(require_v1_user)])
async def upload_file(
    file: UploadFile = File(...),
    caller: Optional[str] = Depends(resolve_v1_caller),
) -> JSONResponse:
    """上传文件（txt, excel, pdf等）"""

    # 检查文件类型（filename 可能为 None：客户端未带 multipart filename 时
    # Path(None) 会抛 TypeError → 500，应返回 400）
    if not file.filename:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="缺少文件名",
        )
    file_ext = Path(file.filename).suffix.lower()
    if file_ext not in ALLOWED_FILE_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"不支持的文件类型。支持的类型: {', '.join(ALLOWED_FILE_TYPES)}"
        )

    try:
        # 生成唯一文件名
        file_id = str(uuid.uuid4())
        filename = f"{file_id}_{file.filename}"
        # P0 接口修复：写路径同样走 _safe_resolve——file.filename 来自客户端，
        # 含 ..\ 时 "UPLOAD_DIR / filename" 可写出上传目录（写入任意路径）
        file_path = _safe_resolve(UPLOAD_DIR, filename)

        # 分块限长写入（防大文件 OOM）
        file_size = await _save_upload_limited(file, file_path)

        # 归属 sidecar（审计批次 A2）：读/删时按此比对
        _record_owner(filename, caller)

        logger.info(f"文件上传成功: {filename} ({file_size} bytes)")

        return JSONResponse({
            "success": True,
            "file_id": file_id,
            "filename": filename,
            "original_name": file.filename,
            "size": file_size,
            "file_path": str(file_path),
            "file_url": f"/api/v1/upload/files/{filename}",
            "file_type": file_ext
        })

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"文件上传失败: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="文件上传失败"
        )


@router.post("/image", dependencies=[Depends(require_v1_user)])
async def upload_image(
    image: UploadFile = File(...),
    caller: Optional[str] = Depends(resolve_v1_caller),
) -> JSONResponse:
    """上传图片文件"""

    # 检查文件类型（filename 为 None 时 Path(None) 会 500）
    if not image.filename:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="缺少文件名",
        )
    file_ext = Path(image.filename).suffix.lower()
    if file_ext not in ALLOWED_IMAGE_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"不支持的图片类型。支持的类型: {', '.join(ALLOWED_IMAGE_TYPES)}"
        )

    try:
        # 生成唯一文件名
        image_id = str(uuid.uuid4())
        filename = f"{image_id}_{image.filename}"

        # 创建图片目录
        image_dir = UPLOAD_DIR / "images"
        image_dir.mkdir(parents=True, exist_ok=True)

        # P0 接口修复：写路径同样走 _safe_resolve（防 file.filename 穿越写出目录）
        file_path = _safe_resolve(image_dir, filename)

        # 分块限长写入（防大文件 OOM）
        file_size = await _save_upload_limited(image, file_path)

        # 归属 sidecar（审计批次 A2）
        _record_owner(filename, caller)

        logger.info(f"图片上传成功: {filename} ({file_size} bytes)")

        return JSONResponse({
            "success": True,
            "image_id": image_id,
            "filename": filename,
            "original_name": image.filename,
            "size": file_size,
            "file_path": str(file_path),
            "image_url": f"/api/v1/upload/images/{filename}",
            "file_type": file_ext
        })

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"图片上传失败: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="图片上传失败"
        )


@router.get(
    "/files/{filename}",
    dependencies=[Depends(require_v1_user)],
)
async def get_uploaded_file(
    filename: str,
    caller: Optional[str] = Depends(resolve_v1_caller),
):
    """获取上传的文件（审计批次 A2：读也要凭证 + 归属比对，跨身份 404）"""
    file_path = _safe_resolve(UPLOAD_DIR, filename)
    if not file_path.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="文件不存在"
        )
    if not _owner_allows(file_path.name, caller):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="文件不存在"
        )

    from fastapi.responses import FileResponse
    return FileResponse(file_path)


@router.get(
    "/images/{filename}",
    dependencies=[Depends(require_v1_user)],
)
async def get_uploaded_image(
    filename: str,
    caller: Optional[str] = Depends(resolve_v1_caller),
):
    """获取上传的图片（审计批次 A2：同 files 口径）"""
    image_dir = UPLOAD_DIR / "images"
    file_path = _safe_resolve(image_dir, filename)
    if not file_path.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="图片不存在"
        )
    if not _owner_allows(file_path.name, caller):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="图片不存在"
        )

    from fastapi.responses import FileResponse
    return FileResponse(file_path)


@router.delete("/{file_id}", dependencies=[Depends(require_v1_user)])
async def delete_uploaded_file(
    file_id: str,
    caller: Optional[str] = Depends(resolve_v1_caller),
):
    """删除上传的文件（审计批次 A2：跨身份文件跳过 → 404，不泄露存在性）"""
    # P0 接口修复：file_id 直接进 rglob 的 glob 模式，未校验时
    # 特殊字符（* ? [ ]）可被用来匹配并删除任意前缀匹配的文件。
    # 上传时 file_id 一律为 uuid4()，这里强制回验格式。
    try:
        uuid.UUID(file_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="非法的文件 ID"
        )

    # 查找文件（跳过 .owners 归属 sidecar 目录——sidecar 自身的
    # "sidecar" 不存在会被 _owner_allows 误放行，跨身份者可借此删掉归属记录）
    for file_path in UPLOAD_DIR.rglob(f"{file_id}_*"):
        if OWNERS_DIRNAME in file_path.parts:
            continue
        if not _owner_allows(file_path.name, caller):
            logger.warning(
                f"跨身份删除被拒: {file_path.name} (caller={caller})"
            )
            continue
        try:
            file_path.unlink()
            _owner_sidecar(file_path.name).unlink(missing_ok=True)
            logger.info(f"文件删除成功: {file_path}")
            return JSONResponse({
                "success": True,
                "message": "文件删除成功"
            })
        except Exception as e:
            logger.error(f"文件删除失败: {e}")
            break

    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="文件不存在"
    )
