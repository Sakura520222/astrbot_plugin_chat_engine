"""Image caption cache CRUD — 图片转述结果持久化缓存。

按 (sha256, provider_id, prompt_hash) 全局唯一:同一张图在同一转述
Provider + 提示词下只转述一次,跨会话、跨重启复用。
"""

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from .models import CEImageCaption


class ImageCaptionRepository:
    """图片转述缓存仓库 — 查询与幂等写入"""

    def __init__(self, session_factory: async_sessionmaker):
        self._factory = session_factory

    async def find(self, sha256: str, provider_id: str, prompt_hash: str) -> str | None:
        """查找缓存的转述文本,未命中返回 None。"""
        async with self._factory() as session:
            result = await session.execute(
                select(CEImageCaption.caption).where(
                    CEImageCaption.sha256 == sha256,
                    CEImageCaption.provider_id == provider_id,
                    CEImageCaption.prompt_hash == prompt_hash,
                )
            )
            return result.scalar_one_or_none()

    async def save(
        self, sha256: str, provider_id: str, prompt_hash: str, caption: str
    ) -> None:
        """写入转述缓存;唯一键冲突(并发写入同图)时静默忽略。"""
        async with self._factory() as session:
            session.add(
                CEImageCaption(
                    sha256=sha256,
                    provider_id=provider_id,
                    prompt_hash=prompt_hash,
                    caption=caption,
                )
            )
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
