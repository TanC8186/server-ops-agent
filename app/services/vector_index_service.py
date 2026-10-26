"""向量索引服务模块

写入顺序（M0 数据保护）：

1. 读取并校验内容 → 切分 → **不触碰旧向量**
2. 先插入新分片（embedding 或插入失败时旧数据完好无损）
3. 再删除同来源的旧分片（排除刚插入的 id）
4. 删除失败时回滚本次插入，旧数据保持可检索
5. 批量插入中途失败时，按预生成的 chunk id 精确删除，避免孤儿行

同一来源的索引会按 ``source`` 串行执行（目录 indexing 与上传共用这把锁），
避免两个写入方交错插入/删除。

M2 边界（尚未实现，勿在本模块声称）：document_id / revision / job_id、
staging collection、撤销。``last_index_state`` 只记录单次操作的结果，用于
诊断与测试，不是持久化的作业状态。
"""

import os
import threading
import uuid
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from loguru import logger

from app.services.document_splitter_service import document_splitter_service
from app.services.vector_store_manager import vector_store_manager

MAX_INDEX_FILE_BYTES = 10 * 1024 * 1024
#: 同一来源锁表的容量上限，避免无限增长的字典。
MAX_SOURCE_LOCKS = 256

_SOURCE_LOCKS: "OrderedDict[str, threading.Lock]" = OrderedDict()
_SOURCE_LOCKS_GUARD = threading.Lock()


def source_lock(source: str) -> threading.Lock:
    """返回某个来源路径对应的进程内互斥锁（按解析后的路径归一化）。

    上传线程与目录索引线程可能同时索引同一个文件；只有同一把锁才能真正
    串行化“插入新分片 → 删除旧分片”，否则会互相删掉对方的行。
    """
    try:
        key = str(Path(source).resolve())
    except (OSError, ValueError):
        key = os.path.abspath(source)
    with _SOURCE_LOCKS_GUARD:
        lock = _SOURCE_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _SOURCE_LOCKS[key] = lock
        else:
            _SOURCE_LOCKS.move_to_end(key)
        while len(_SOURCE_LOCKS) > MAX_SOURCE_LOCKS:
            oldest_key, oldest_lock = next(iter(_SOURCE_LOCKS.items()))
            # 正在使用或等待中的锁不回收，避免同一来源出现两把锁。
            if oldest_lock.locked():
                break
            _SOURCE_LOCKS.pop(oldest_key)
        return lock


class IndexingResult:
    """索引结果类"""

    def __init__(self):
        self.success = False
        self.directory_path = ""
        self.total_files = 0
        self.success_count = 0
        self.fail_count = 0
        self.start_time: Optional[datetime] = None
        self.end_time: Optional[datetime] = None
        self.error_message = ""
        self.failed_files: Dict[str, str] = {}

    def increment_success_count(self):
        """增加成功计数"""
        self.success_count += 1

    def increment_fail_count(self):
        """增加失败计数"""
        self.fail_count += 1

    def add_failed_file(self, file_path: str, error: str):
        """添加失败文件"""
        self.failed_files[file_path] = error

    def get_duration_ms(self) -> int:
        """获取耗时（毫秒）"""
        if self.start_time and self.end_time:
            return int((self.end_time - self.start_time).total_seconds() * 1000)
        return 0

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            "success": self.success,
            "directory_path": self.directory_path,
            "total_files": self.total_files,
            "success_count": self.success_count,
            "fail_count": self.fail_count,
            "duration_ms": self.get_duration_ms(),
            "error_message": self.error_message,
            "failed_files": self.failed_files,
        }


class VectorIndexService:
    """向量索引服务 - 负责读取文件、生成向量、存储到 Milvus"""

    def __init__(self):
        """初始化向量索引服务"""
        self.upload_path = "./uploads"
        #: 最近一次 index_single_file 的结果，供 HTTP 层如实回报。
        self.last_index_state: Optional[Dict[str, Any]] = None
        logger.info("向量索引服务初始化完成")

    def index_directory(self, directory_path: Optional[str] = None) -> IndexingResult:
        """
        索引指定目录下的所有文件

        Args:
            directory_path: 目录路径（可选，默认使用配置的上传目录）

        Returns:
            IndexingResult: 索引结果
        """
        result = IndexingResult()
        result.start_time = datetime.now()

        try:
            # 使用指定目录或默认上传目录
            target_path = directory_path if directory_path else self.upload_path
            dir_path = Path(target_path).resolve()

            if not dir_path.exists() or not dir_path.is_dir():
                raise ValueError(f"目录不存在或不是有效目录: {target_path}")

            result.directory_path = str(dir_path)

            # 获取所有支持的文件（跳过 staging 临时文件）
            files = [
                path
                for pattern in ("*.txt", "*.md")
                for path in dir_path.glob(pattern)
                if not path.name.startswith(".")
            ]

            if not files:
                logger.warning(f"目录中没有找到支持的文件: {target_path}")
                result.total_files = 0
                result.success = True
                result.end_time = datetime.now()
                return result

            result.total_files = len(files)
            logger.info(f"开始索引目录: {target_path}, 找到 {len(files)} 个文件")

            # 遍历并索引每个文件
            for file_path in files:
                try:
                    self.index_single_file(str(file_path))
                    result.increment_success_count()
                    logger.info(f"✓ 文件索引成功: {file_path.name}")
                except Exception as e:
                    result.increment_fail_count()
                    result.add_failed_file(str(file_path), str(e))
                    logger.error(f"✗ 文件索引失败: {file_path.name}, 错误: {e}")

            result.success = result.fail_count == 0
            result.end_time = datetime.now()

            logger.info(
                f"目录索引完成: 总数={result.total_files}, "
                f"成功={result.success_count}, 失败={result.fail_count}"
            )

            return result

        except Exception as e:
            logger.error(f"索引目录失败: {e}")
            result.success = False
            result.error_message = str(e)
            result.end_time = datetime.now()
            return result

    def read_text(self, file_path: str | Path) -> str:
        """有界读取并按 UTF-8 严格解码；失败时抛出可解释的 ValueError。"""
        path = Path(file_path)
        size = path.stat().st_size
        if size > MAX_INDEX_FILE_BYTES:
            raise ValueError(f"文件大小超过限制（最大 {MAX_INDEX_FILE_BYTES} 字节）")
        raw = path.read_bytes()
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"文件不是有效的 UTF-8 文本: {exc.reason}") from exc
        if not content.strip():
            raise ValueError("文件内容为空")
        return content

    def index_single_file(self, file_path: str, *, source_path: str | None = None):
        """
        索引单个文件

        先插入新分片再删除旧分片：任何一步失败都不会让旧知识先消失。

        Args:
            file_path: 待读取的文件路径
            source_path: 记录到向量元数据中的来源路径（默认等于 file_path）；
                上传流程用它把 staging 文件登记成最终文件名。

        Raises:
            ValueError: 文件不存在、超限、编码非法或内容为空
            RuntimeError: 切分、embedding、插入或清理失败
        """
        path = Path(file_path).resolve()

        if not path.exists() or not path.is_file():
            raise ValueError(f"文件不存在: {file_path}")

        source = source_path or str(path)
        logger.info(f"开始索引文件: {path} (来源: {source})")

        # 同一来源的插入/删除必须串行：两个写入方交错时会互相删行。
        with source_lock(source):
            self._index_locked(path, source, file_path)

    def _index_locked(self, path: Path, source: str, file_path: str) -> None:
        state: Dict[str, Any] = {
            "file": str(path),
            "source": source,
            "chunks": 0,
            "inserted_ids": [],
            "deleted_old": 0,
            "rolled_back": False,
            "duplicates_possible": False,
            "ok": False,
        }
        self.last_index_state = state

        # 预先生成 chunk id：批量插入中途失败时也能精确清理已落库的行。
        chunk_ids: list[str] = []

        try:
            content = self.read_text(path)
            state["chars"] = len(content)

            # 1. 切分在删除之前完成：解析失败不得影响旧向量。
            documents = document_splitter_service.split_document(content, source)
            if not documents:
                # 纯标题文档不会产出 child，但内容本身合法（非空）；退化为
                # 单段切分，避免把可索引的小文件误判为“无法切分”。
                documents = document_splitter_service.split_text(content, source)
            if not documents:
                raise ValueError("文件无法切分为任何分片")
            state["chunks"] = len(documents)
            # 候选 id 只放局部变量：插入成功前 ``inserted_ids`` 必须保持空，
            # 否则调用方会把“从未落库”的 id 误判为已写入。
            chunk_ids = [uuid.uuid4().hex for _ in documents]

            # 2. 先插入新分片；embedding/插入失败时旧数据仍在。
            inserted = vector_store_manager.add_documents(documents, ids=list(chunk_ids)) or list(
                chunk_ids
            )
            state["inserted_ids"] = list(inserted) or list(chunk_ids)

            # 3. 再清理旧分片，保留本次插入的 id。
            try:
                state["deleted_old"] = vector_store_manager.delete_by_source(
                    source, keep_ids=list(inserted)
                )
            except Exception as cleanup_error:
                # 4. 清理失败：撤销本次插入，避免新旧混杂或重复。
                state["duplicates_possible"] = True
                self._rollback(list(inserted))
                raise RuntimeError(
                    f"删除旧向量失败，已回滚本次写入: {cleanup_error}"
                ) from cleanup_error

            state["ok"] = True
            logger.info(f"文件索引完成: {path} -> {len(documents)} 个分片")
        except Exception as e:
            logger.error(f"索引文件失败: {file_path}, 错误: {e}")
            # 5. 插入阶段异常（批量中途失败）：不删旧数据，只按 id 清理本次可能
            #    已落库的部分行。清理成功即不存在重复风险。
            if chunk_ids and not state["ok"] and not state["duplicates_possible"]:
                self._rollback_back_insert(chunk_ids)
            if isinstance(e, ValueError) and not state["inserted_ids"]:
                raise
            raise RuntimeError(f"索引文件失败: {e}") from e

    def _rollback_back_insert(self, candidate_ids: list[str]) -> None:
        """插入抛错后按预生成 id 清理，避免留下孤儿行。"""
        try:
            vector_store_manager.delete_by_ids(candidate_ids)
            if self.last_index_state is not None:
                self.last_index_state["rolled_back"] = True
            logger.info(f"已按预生成 id 清理 {len(candidate_ids)} 个可能落库的分片")
        except Exception as rollback_error:
            if self.last_index_state is not None:
                self.last_index_state["duplicates_possible"] = True
            logger.error(f"清理半批新分片失败，可能残留孤儿行: {rollback_error}")

    def _rollback(self, inserted_ids: list[str]) -> None:
        if not inserted_ids:
            return
        try:
            vector_store_manager.delete_by_ids(inserted_ids)
            if self.last_index_state is not None:
                self.last_index_state["rolled_back"] = True
            logger.info(f"已回滚 {len(inserted_ids)} 个新分片")
        except Exception as rollback_error:
            logger.error(f"回滚新分片失败，可能残留重复数据: {rollback_error}")


# 全局单例
vector_index_service = VectorIndexService()
