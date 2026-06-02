"""Session/Context CRUD operations"""

import json
from datetime import datetime

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from .models import ChatSession


class SessionRepository:
    """聊天会话仓库 — 管理上下文的存储与读取"""

    def __init__(self, session_factory: async_sessionmaker):
        self._factory = session_factory

    async def get_context(self, session_key: str) -> list[dict]:
        """加载指定会话的上下文消息列表"""
        async with self._factory() as session:
            result = await session.execute(
                select(ChatSession).where(ChatSession.session_key == session_key)
            )
            row = result.scalar_one_or_none()
            if row is None:
                return []
            return json.loads(row.messages_json)

    async def save_context(self, session_key: str, messages: list[dict]) -> None:
        """保存/替换整个上下文消息列表"""
        async with self._factory() as session:
            result = await session.execute(
                select(ChatSession).where(ChatSession.session_key == session_key)
            )
            row = result.scalar_one_or_none()
            messages_json = json.dumps(messages, ensure_ascii=False)

            if row is None:
                row = ChatSession(
                    session_key=session_key,
                    messages_json=messages_json,
                    updated_at=datetime.utcnow(),
                    created_at=datetime.utcnow(),
                )
                session.add(row)
            else:
                row.messages_json = messages_json
                row.updated_at = datetime.utcnow()

            await session.commit()

    async def delete_session(self, session_key: str) -> bool:
        """删除指定会话。返回是否存在并删除。"""
        async with self._factory() as session:
            result = await session.execute(
                delete(ChatSession).where(ChatSession.session_key == session_key)
            )
            await session.commit()
            return result.rowcount > 0

    async def list_sessions(
        self, page: int = 1, page_size: int = 50
    ) -> tuple[list[dict], int]:
        """分页列出所有会话。返回 (sessions, total_count)。"""
        async with self._factory() as session:
            # 总数
            count_result = await session.execute(
                select(func.count()).select_from(ChatSession)
            )
            total = count_result.scalar_one()

            # 分页查询
            offset = (page - 1) * page_size
            result = await session.execute(
                select(ChatSession)
                .order_by(ChatSession.updated_at.desc())
                .offset(offset)
                .limit(page_size)
            )
            rows = result.scalars().all()

            sessions = []
            for row in rows:
                messages = json.loads(row.messages_json)
                sessions.append(
                    {
                        "session_key": row.session_key,
                        "message_count": len(messages),
                        "updated_at": row.updated_at.isoformat()
                        if row.updated_at
                        else None,
                        "created_at": row.created_at.isoformat()
                        if row.created_at
                        else None,
                    }
                )

            return sessions, total

    async def get_session_count(self) -> int:
        """获取会话总数"""
        async with self._factory() as session:
            result = await session.execute(
                select(func.count()).select_from(ChatSession)
            )
            return result.scalar_one()
