"""Image quota CRUD operations — 普通用户每日画图/改图配额计数。"""

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from ..utils import shanghai_now as _shanghai_now
from .models import CEImageQuota


class ImageQuotaRepository:
    """图片配额仓库 — 管理普通用户每日画图/改图用量计数。

    quota_key 格式由调用方构建:
    - 按用户维度: ``user:{sender_id}``
    - 按会话维度: ``session:{session_key}``

    date 为上海时区 ``YYYY-MM-DD``,跨日产生新行即完成"每日重置",
    无需定时任务清理历史行。
    """

    def __init__(self, session_factory: async_sessionmaker):
        self._factory = session_factory

    async def get_used(self, quota_key: str, date: str) -> int:
        """查询指定 key + date 的已用次数,无记录返回 0。"""
        async with self._factory() as session:
            result = await session.execute(
                select(CEImageQuota).where(
                    CEImageQuota.quota_key == quota_key,
                    CEImageQuota.date == date,
                )
            )
            row = result.scalar_one_or_none()
            return row.used_count if row else 0

    async def incr_used(self, quota_key: str, date: str) -> int:
        """原子 +1 并返回更新后的次数;无记录时插入。

        先用 ``UPDATE ... SET used_count = used_count + 1`` 保证并发安全,
        无行被更新时再插入新记录。并发插入冲突(唯一约束)时回滚并重试 update,
        最多重试 3 次,覆盖 MySQL 等真实并发场景。
        """
        for _ in range(3):
            async with self._factory() as session:
                result = await session.execute(
                    update(CEImageQuota)
                    .where(
                        CEImageQuota.quota_key == quota_key,
                        CEImageQuota.date == date,
                    )
                    .values(
                        used_count=CEImageQuota.used_count + 1,
                        updated_at=_shanghai_now(),
                    )
                )
                if result.rowcount > 0:
                    await session.commit()
                    break
                # 无记录 → 尝试插入;并发下可能冲突,回滚后重试 update
                session.add(
                    CEImageQuota(
                        quota_key=quota_key,
                        date=date,
                        used_count=1,
                        updated_at=_shanghai_now(),
                    )
                )
                try:
                    await session.commit()
                    break
                except IntegrityError:
                    await session.rollback()
                    continue
        return await self.get_used(quota_key, date)

    async def decr_used(self, quota_key: str, date: str) -> None:
        """回退 -1,不低于 0。用于图片 API 调用失败时退还配额。

        ``used_count > 0`` 条件确保不会出现负数。
        """
        async with self._factory() as session:
            await session.execute(
                update(CEImageQuota)
                .where(
                    CEImageQuota.quota_key == quota_key,
                    CEImageQuota.date == date,
                    CEImageQuota.used_count > 0,
                )
                .values(
                    used_count=CEImageQuota.used_count - 1,
                    updated_at=_shanghai_now(),
                )
            )
            await session.commit()

    async def list_today(self, date: str) -> list[CEImageQuota]:
        """查询指定日期的所有配额记录,按已用次数降序(供 WebUI 展示)。"""
        async with self._factory() as session:
            result = await session.execute(
                select(CEImageQuota)
                .where(CEImageQuota.date == date)
                .order_by(CEImageQuota.used_count.desc())
            )
            return list(result.scalars().all())

    async def reset(self, quota_key: str, date: str) -> bool:
        """重置指定 key + date 的配额(删除该行)。返回是否删除了记录。

        删除后 get_used 回到 0,效果等同于"手动清零"。
        """
        async with self._factory() as session:
            result = await session.execute(
                delete(CEImageQuota).where(
                    CEImageQuota.quota_key == quota_key,
                    CEImageQuota.date == date,
                )
            )
            await session.commit()
            return result.rowcount > 0
