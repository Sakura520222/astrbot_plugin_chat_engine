"""Archived session CRUD operations — multi-session support."""

import json

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from ..utils import shanghai_now as _shanghai_now
from .models import CEArchivedSession


class ArchivedSessionRepository:
    """归档会话仓库 — 管理非活跃历史会话的存取"""

    def __init__(self, session_factory: async_sessionmaker):
        self._factory = session_factory

    async def archive(
        self,
        session_key: str,
        title: str,
        messages: list[dict],
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
    ) -> CEArchivedSession:
        """将一组消息归档为一条记录，返回创建的 ORM 对象。

        prompt_tokens/completion_tokens 为归档时的累计用量快照，
        /switch 恢复时读回以还原该会话的 token 计数。
        """
        messages_json = json.dumps(messages, ensure_ascii=False)
        async with self._factory() as session:
            row = CEArchivedSession(
                session_key=session_key,
                title=title,
                messages_json=messages_json,
                message_count=len(messages),
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                created_at=_shanghai_now(),
                updated_at=_shanghai_now(),
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return row

    async def list_by_session_key(self, session_key: str) -> list[CEArchivedSession]:
        """按 session_key 列出所有归档，按 updated_at 倒序（最新在前）。"""
        async with self._factory() as session:
            result = await session.execute(
                select(CEArchivedSession)
                .where(CEArchivedSession.session_key == session_key)
                .order_by(CEArchivedSession.updated_at.desc())
            )
            return list(result.scalars().all())

    async def count_by_session_keys(self, session_keys: list[str]) -> dict[str, int]:
        """批量查询多个 session_key 的归档数量，返回 {session_key: count}。"""
        if not session_keys:
            return {}
        async with self._factory() as session:
            result = await session.execute(
                select(
                    CEArchivedSession.session_key,
                    func.count(CEArchivedSession.id),
                )
                .where(CEArchivedSession.session_key.in_(session_keys))
                .group_by(CEArchivedSession.session_key)
            )
            return dict(result.all())

    async def get_by_id(self, archive_id: int) -> CEArchivedSession | None:
        """按主键获取归档记录。"""
        async with self._factory() as session:
            result = await session.execute(
                select(CEArchivedSession).where(CEArchivedSession.id == archive_id)
            )
            return result.scalar_one_or_none()

    async def delete(self, archive_id: int) -> bool:
        """删除指定归档。返回是否存在并删除。"""
        async with self._factory() as session:
            result = await session.execute(
                delete(CEArchivedSession).where(CEArchivedSession.id == archive_id)
            )
            await session.commit()
            return result.rowcount > 0

    async def update_title(self, archive_id: int, title: str) -> bool:
        """更新归档话题名。返回是否成功。"""
        async with self._factory() as session:
            result = await session.execute(
                select(CEArchivedSession).where(CEArchivedSession.id == archive_id)
            )
            row = result.scalar_one_or_none()
            if row is None:
                return False
            row.title = title
            row.updated_at = _shanghai_now()
            await session.commit()
            return True
