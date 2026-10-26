"""文件上传接口模块

上传是两阶段的：先写 staging 临时文件并校验，全部通过后才替换正式文件；
索引在替换之后执行，失败时恢复上一版字节，返回 202 并如实标明 indexed=false。

完整 revision 作业（job_id、queued/running/ready/failed、重试、撤销）属 M2，
本模块不声称已实现。
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from loguru import logger

from app.services.vector_index_service import MAX_INDEX_FILE_BYTES, vector_index_service

router = APIRouter()

# 文件上传后存储的路径
UPLOAD_DIR = Path("./uploads")
# 支持的文件类型
ALLOWED_EXTENSIONS = ["txt", "md"]
# 单个文件支持最大大小（与索引层共用同一个上限）
MAX_FILE_SIZE = MAX_INDEX_FILE_BYTES
# 流式读取分块大小
READ_CHUNK = 64 * 1024
#: staging 临时文件命名模式（与 ``_write_staging`` 的 prefix/suffix 一致）
STAGING_GLOB = ".staging-*.tmp"
#: 超过该时长（秒）的 staging 残留视为崩溃遗留，可以安全删除。
STALE_STAGING_SECONDS = 3600
#: 单次上传最多清理的残留文件数，避免目录很大时拖慢请求。
STALE_STAGING_LIMIT = 100
#: Windows 保留设备名：这些名字即便带扩展名也无法正常创建/读取。
WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
)


@router.post("/upload")
async def upload_file(file: UploadFile = File(...)):
    """上传文件并尝试建立向量索引。

    - 任何校验失败（大小/格式/编码/空内容）都不会触碰上一版文件。
    - 索引失败时回滚文件与向量，返回 202 + ``indexed=false``。
    """
    if not file.filename:
        raise HTTPException(status_code=400, detail="文件名不能为空")

    safe_filename = _sanitize_filename(file.filename)
    if _is_reserved_device_name(safe_filename):
        raise HTTPException(
            status_code=400,
            detail=f"文件名使用了 Windows 保留设备名（如 CON/NUL/COM1），无法保存: {safe_filename}",
        )
    file_extension = _get_file_extension(safe_filename)
    if file_extension not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的文件格式，仅支持: {', '.join(ALLOWED_EXTENSIONS)}",
        )

    try:
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        # 先清掉崩溃遗留的 staging 残留（有界、只清过期的），避免磁盘无限增长。
        _cleanup_stale_staging(limit=STALE_STAGING_LIMIT)
        content = await _read_bounded(file, MAX_FILE_SIZE)
        _validate_text(content)

        file_path = _resolve_upload_target(safe_filename)
        previous = file_path.read_bytes() if file_path.is_file() else None

        # 1. staging：同目录临时文件 + 原子替换，崩溃时最多留下 .tmp。
        staging = _write_staging(content)

        source = file_path.resolve().as_posix()
        indexed = False
        index_error = ""
        try:
            file_path = _activate(staging, file_path)
            logger.info(f"文件已写入: {file_path}")
            # source_path 用正式路径：切分与向量元数据必须与旧版本的来源一致，
            # 否则删除旧向量时会漏删，产生新旧重复。
            await asyncio.to_thread(
                vector_index_service.index_single_file,
                str(file_path),
                source_path=source,
            )
            indexed = True
            logger.info(f"向量索引创建成功: {file_path}")
        except Exception as exc:  # noqa: BLE001 - 需要回滚并如实回报
            index_error = str(exc)[:500]
            logger.error(f"索引失败，回滚上一版: {file_path}: {index_error}")
            # 向量层已保证“插入失败不删旧、清理失败回滚新插入”，这里只需恢复文件字节。
            _restore_file(file_path, previous)
            _discard(staging)

        return JSONResponse(
            status_code=200 if indexed else 202,
            content={
                "code": 200 if indexed else 202,
                "message": "indexed" if indexed else "uploaded_but_index_failed",
                "data": {
                    "filename": safe_filename,
                    "file_path": str(file_path),
                    "size": len(content),
                    "indexed": indexed,
                    "index_error": index_error,
                },
            },
        )

    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        logger.error(f"文件上传失败: {e}")
        raise HTTPException(status_code=500, detail=f"文件上传失败: {e}") from e


async def _read_bounded(upload: UploadFile, limit: int) -> bytes:
    """分块读取并在超限时立即中止，避免把超大文件读进内存。"""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await upload.read(READ_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise HTTPException(status_code=400, detail=f"文件大小超过限制（最大 {limit} 字节）")
        chunks.append(chunk)
    return b"".join(chunks)


def _validate_text(content: bytes) -> None:
    if not content.strip():
        raise HTTPException(status_code=400, detail="文件内容为空")
    try:
        content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"文件必须是 UTF-8 文本: {exc.reason}") from exc


def _write_staging(content: bytes) -> Path:
    handle = tempfile.NamedTemporaryFile(
        mode="wb", dir=UPLOAD_DIR, prefix=".staging-", suffix=".tmp", delete=False
    )
    try:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    finally:
        handle.close()
    return Path(handle.name)


def _activate(staging: Path, target: Path) -> Path:
    """原子替换到目标名（Windows 上 os.replace 覆盖已存在文件）。"""
    os.replace(staging, target)
    return target


def _restore_file(target: Path, previous: bytes | None) -> None:
    try:
        if previous is None:
            target.unlink(missing_ok=True)
        else:
            target.write_bytes(previous)
    except OSError as exc:
        logger.error(f"回滚文件失败，需人工检查 {target}: {exc}")


def _discard(staging: Path) -> None:
    try:
        staging.unlink(missing_ok=True)
    except OSError:
        pass


@router.post("/index_directory")
async def index_directory(directory_path: str | None = None):
    """索引指定目录下的所有文件（仅限知识库目录）。"""
    allowed_roots = [UPLOAD_DIR.resolve(), Path("./aiops-docs").resolve()]
    target = Path(directory_path).resolve() if directory_path else allowed_roots[0]
    if not any(target == root or root in target.parents for root in allowed_roots):
        raise HTTPException(
            status_code=400,
            detail=f"只允许索引以下目录: {', '.join(str(r) for r in allowed_roots)}",
        )

    logger.info(f"开始索引目录: {target}")
    result = await asyncio.to_thread(vector_index_service.index_directory, str(target))
    return JSONResponse(
        status_code=200,
        content={
            "code": 200,
            "message": "success" if result.success else "partial_success",
            "data": result.to_dict(),
        },
    )


def _get_file_extension(filename: str) -> str:
    """获取文件扩展名（小写，不含点）。"""
    parts = filename.rsplit(".", 1)
    if len(parts) == 2:
        return parts[1].lower()
    return ""


def _is_reserved_device_name(filename: str) -> bool:
    """判断文件名（含扩展名）是否使用了 Windows 保留设备名。

    Windows 上 ``CON``、``NUL``、``COM1`` 等即使带扩展名也无法正常创建，
    必须在落盘前拒绝；``console.md``、``auxiliary.md`` 这类包含前缀的普通
    文件名不受影响（按第一个点之前的主名整体比较）。
    """
    stem = filename.split(".", 1)[0].strip().upper()
    return stem in WINDOWS_RESERVED_NAMES


def _resolve_upload_target(name: str) -> Path:
    """把上传文件名解析为 UPLOAD_DIR 内的绝对路径。

    先拼接再 ``resolve()``，任何解析后跑到上传目录之外的名字（例如
    ``../evil.md``）都直接 400，防止上传写入目录外。
    """
    root = UPLOAD_DIR.resolve()
    target = (root / name).resolve()
    if target == root or root not in target.parents:
        raise HTTPException(status_code=400, detail=f"非法文件名，解析后不在上传目录内: {name}")
    return target


def _cleanup_stale_staging(limit: int = STALE_STAGING_LIMIT) -> int:
    """清理崩溃遗留的 staging 临时文件，返回删除数量。

    只删除超过 1 小时未修改的文件：仍在进行中的上传刚创建临时文件，
    mtime 很新，必须保留。``limit`` 保证单次请求的删除量有界。
    """
    try:
        root = UPLOAD_DIR
        if not root.is_dir():
            return 0
        cutoff = time.time() - STALE_STAGING_SECONDS
        removed = 0
        for path in root.glob(STAGING_GLOB):
            if removed >= limit:
                break
            try:
                if path.stat().st_mtime >= cutoff:
                    continue
                path.unlink(missing_ok=True)
                removed += 1
            except OSError as exc:
                logger.warning(f"清理 staging 残留失败（忽略）: {path}: {exc}")
        if removed:
            logger.info(f"已清理 {removed} 个过期的 staging 残留文件")
        return removed
    except OSError as exc:
        logger.warning(f"staging 残留清理跳过: {exc}")
        return 0


def _sanitize_filename(filename: str) -> str:
    """规范化文件名，去除路径分隔符与 Windows 保留字符。

    已知限制（有意保留）：空格统一替换为下划线，因此 ``a b.md`` 与
    ``a_b.md`` 指向同一份文档，后上传者覆盖前者。
    """
    # Windows 会静默丢弃结尾的点和空格，这里先显式去掉，避免扩展名被吞。
    trimmed = filename.strip().rstrip(". ")
    sanitized = trimmed.replace(" ", "_")
    for char in ["\\", "/", ":", "*", "?", '"', "<", ">", "|"]:
        sanitized = sanitized.replace(char, "_")
    sanitized = sanitized.strip(". ") or f"upload-{uuid.uuid4().hex[:8]}"
    return sanitized
