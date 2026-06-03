"""Image CRUD operations — 按 sha256 去重存储图片"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from .models import CEImage


class ImageRepository:
    """图片仓库 — 管理图片的去重存储与查询"""

    def __init__(self, session_factory: async_sessionmaker):
        self._factory = session_factory

    async def find_by_sha256(self, sha256: str) -> CEImage | None:
        """根据 sha256 查找已有图片"""
        async with self._factory() as session:
            result = await session.execute(
                select(CEImage).where(CEImage.sha256 == sha256)
            )
            return result.scalar_one_or_none()

    async def save(
        self, sha256: str, mime_type: str, file_path: str, file_size: int
    ) -> CEImage:
        """保存图片记录"""
        async with self._factory() as session:
            image = CEImage(
                sha256=sha256,
                mime_type=mime_type,
                file_path=file_path,
                file_size=file_size,
            )
            session.add(image)
            await session.commit()
            await session.refresh(image)
            return image

    async def get_by_id(self, image_id: int) -> CEImage | None:
        """根据 ID 获取图片记录"""
        async with self._factory() as session:
            result = await session.execute(
                select(CEImage).where(CEImage.id == image_id)
            )
            return result.scalar_one_or_none()
