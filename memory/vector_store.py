"""Long-term memory store — per-session FaissVecDB instances with lazy loading."""

import asyncio
import hashlib
import json
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from astrbot.api import logger

if TYPE_CHECKING:
    from astrbot.core.db.vec_db.faiss_impl.vec_db import FaissVecDB


class LongTermMemoryStore:
    """长期记忆存储 — 每个 session_key 独立的 FaissVecDB 实例。"""

    def __init__(
        self,
        base_dir: str,
        embedding_getter: Callable | None = None,
        rerank_getter: Callable | None = None,
    ):
        self._base_dir = Path(base_dir) / "memory" / "long_term"
        self._base_dir.mkdir(parents=True, exist_ok=True)
        # 使用 getter 函数动态获取 provider，避免缓存已关闭的客户端
        self._embedding_getter = embedding_getter
        self._rerank_getter = rerank_getter
        self._instances: dict[str, FaissVecDB] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        logger.info("[Memory] 长期记忆存储已创建（EmbeddingProvider 延迟检测）")

    def _get_embedding_provider(self):
        """动态获取 EmbeddingProvider，每次调用取最新实例。"""
        if not self._embedding_getter:
            return None
        try:
            providers = self._embedding_getter()
            if providers:
                return providers[0] if isinstance(providers, list) else providers
        except Exception:
            pass
        return None

    def _get_rerank_provider(self):
        """动态获取 RerankProvider。"""
        if not self._rerank_getter:
            return None
        try:
            providers = self._rerank_getter()
            if providers:
                return providers[0] if isinstance(providers, list) else providers
        except Exception:
            pass
        return None

    @property
    def available(self) -> bool:
        return self._get_embedding_provider() is not None

    def _get_lock(self, session_key: str) -> asyncio.Lock:
        if session_key not in self._locks:
            self._locks[session_key] = asyncio.Lock()
        return self._locks[session_key]

    def _session_dir(self, session_key: str) -> Path:
        h = hashlib.sha256(session_key.encode()).hexdigest()
        d = self._base_dir / h
        d.mkdir(parents=True, exist_ok=True)
        return d

    @staticmethod
    def _utcnow() -> str:
        return datetime.now(timezone.utc).isoformat()

    async def get_or_create(self, session_key: str):
        """获取或创建该 session 的 FaissVecDB 实例（懒加载）。"""
        ep = self._get_embedding_provider()
        if not ep:
            return None
        if session_key in self._instances:
            return self._instances[session_key]

        from astrbot.core.db.vec_db.faiss_impl.vec_db import FaissVecDB

        d = self._session_dir(session_key)
        doc_store_path = str(d / "docs.db")
        index_store_path = str(d / "index.faiss")

        try:
            vec_db = FaissVecDB(
                doc_store_path=doc_store_path,
                index_store_path=index_store_path,
                embedding_provider=ep,
                rerank_provider=self._get_rerank_provider(),
            )
            await vec_db.initialize()
            self._instances[session_key] = vec_db
            return vec_db
        except Exception as e:
            logger.error(f"[Memory] 创建 FaissVecDB 失败 [{session_key}]: {e}")
            return None

    async def save(
        self,
        session_key: str,
        content: str,
        source: str = "tool",
        pinned: bool = False,
    ) -> str | None:
        """保存一条长期记忆，返回记忆 ID。失败返回 None。"""
        if not self.available:
            return None
        async with self._get_lock(session_key):
            vec_db = await self.get_or_create(session_key)
            if not vec_db:
                return None
            try:
                mid = uuid.uuid4().hex
                metadata = {
                    "id": mid,
                    "source": source,
                    "session_key": session_key,
                    "pinned": pinned,
                    "created_at": self._utcnow(),
                    "updated_at": self._utcnow(),
                }
                await vec_db.insert(content=content, metadata=metadata, id=mid)
                return mid
            except Exception as e:
                logger.error(f"[Memory] 保存长期记忆失败: {e}")
                return None

    async def search(
        self,
        session_key: str,
        query: str,
        top_k: int = 5,
        fetch_k: int = 20,
        enable_rerank: bool = True,
        similarity_threshold: float = 0.3,
    ) -> list[dict]:
        """语义检索长期记忆。返回 [{id, content, similarity}]。"""
        if not self.available:
            return []
        try:
            vec_db = await self.get_or_create(session_key)
        except Exception as e:
            logger.error(f"[Memory] 创建长期记忆实例失败 [{session_key}]: {e!r}")
            return []
        if not vec_db:
            return []
        try:
            rp = self._get_rerank_provider()
            use_rerank = enable_rerank and rp is not None
            results = await vec_db.retrieve(
                query=query,
                k=top_k,
                fetch_k=fetch_k,
                rerank=use_rerank,
            )
            filtered = []
            for r in results:
                if r.similarity < similarity_threshold:
                    continue
                data = r.data
                # metadata 在 DocumentStorage 中可能是 JSON 字符串
                raw_meta = data.get("metadata", "{}")
                meta = (
                    json.loads(raw_meta)
                    if isinstance(raw_meta, str)
                    else (raw_meta or {})
                )
                filtered.append(
                    {
                        "id": meta.get("id", data.get("id", "")),
                        "content": data.get("text", ""),
                        "similarity": round(r.similarity, 4),
                    }
                )
            return filtered
        except Exception as e:
            logger.error(f"[Memory] 检索长期记忆失败: {e!r}", exc_info=True)
            return []

    async def update(
        self,
        session_key: str,
        doc_id: str,
        content: str,
        pinned: bool | None = None,
    ) -> bool:
        """更新长期记忆（删除旧向量 + 插入新向量）。"""
        if not self.available:
            return False
        async with self._get_lock(session_key):
            vec_db = await self.get_or_create(session_key)
            if not vec_db:
                return False
            try:
                old_doc = await self._get_doc_by_id(vec_db, doc_id)
                # metadata 在 DocumentStorage 中是 JSON 字符串，需要解析
                old_meta = {}
                if old_doc:
                    raw = old_doc.get("metadata", "{}")
                    old_meta = json.loads(raw) if isinstance(raw, str) else (raw or {})
                source = old_meta.get("source", "tool")
                old_pinned = old_meta.get("pinned", False)
                await vec_db.delete(doc_id)
                metadata = {
                    "id": doc_id,
                    "source": source,
                    "session_key": session_key,
                    "pinned": pinned if pinned is not None else old_pinned,
                    "created_at": old_meta.get("created_at", self._utcnow()),
                    "updated_at": self._utcnow(),
                }
                await vec_db.insert(content=content, metadata=metadata, id=doc_id)
                return True
            except Exception as e:
                logger.error(f"[Memory] 更新长期记忆失败: {e}")
                return False

    async def delete(self, session_key: str, doc_id: str) -> bool:
        """删除指定长期记忆。"""
        if not self.available:
            return False
        vec_db = await self.get_or_create(session_key)
        if not vec_db:
            return False
        try:
            await vec_db.delete(doc_id)
            return True
        except Exception as e:
            logger.error(f"[Memory] 删除长期记忆失败: {e}")
            return False

    async def list_all(self, session_key: str) -> list[dict]:
        """列出所有长期记忆（用于 WebUI）。"""
        if not self.available:
            return []
        vec_db = await self.get_or_create(session_key)
        if not vec_db:
            return []
        try:
            doc_store = vec_db.document_storage
            docs = await doc_store.get_documents(metadata_filters={})
            result = []
            for doc in docs:
                # metadata 在 DocumentStorage 中是 JSON 字符串，需要解析
                raw_meta = doc.get("metadata", "{}")
                meta = (
                    json.loads(raw_meta)
                    if isinstance(raw_meta, str)
                    else (raw_meta or {})
                )
                result.append(
                    {
                        "id": meta.get("id", str(doc.get("doc_id", ""))),
                        "content": doc.get("text", ""),
                        "source": meta.get("source", ""),
                        "pinned": meta.get("pinned", False),
                        "created_at": meta.get("created_at", ""),
                        "updated_at": meta.get("updated_at", ""),
                    }
                )
            return result
        except Exception as e:
            logger.error(f"[Memory] 列出长期记忆失败: {e}")
            return []

    async def list_pinned(self, session_key: str) -> list[dict]:
        """列出所有置顶的长期记忆（每次都注入 system prompt）。"""
        all_memories = await self.list_all(session_key)
        return [m for m in all_memories if m.get("pinned")]

    async def _get_doc_by_id(self, vec_db, doc_id: str) -> dict | None:
        """通过 ID 获取文档。"""
        try:
            doc_store = vec_db.document_storage
            docs = await doc_store.get_documents(metadata_filters={"id": doc_id})
            return docs[0] if docs else None
        except Exception:
            return None

    async def close(self, session_key: str) -> None:
        """关闭并释放指定 session 的 FaissVecDB 实例。"""
        vec_db = self._instances.pop(session_key, None)
        if vec_db:
            try:
                await vec_db.close()
            except Exception:
                pass
        self._locks.pop(session_key, None)

    async def close_all(self) -> None:
        """关闭所有 FaissVecDB 实例。"""
        for key in list(self._instances.keys()):
            await self.close(key)
