"""Session/Context CRUD operations"""

import json

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from ..utils import shanghai_now as _shanghai_now
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
                    updated_at=_shanghai_now(),
                    created_at=_shanghai_now(),
                )
                session.add(row)
            else:
                row.messages_json = messages_json
                row.updated_at = _shanghai_now()

            await session.commit()

    async def delete_session(self, session_key: str) -> bool:
        """删除指定会话。返回是否存在并删除。"""
        async with self._factory() as session:
            result = await session.execute(
                delete(ChatSession).where(ChatSession.session_key == session_key)
            )
            await session.commit()
            return result.rowcount > 0

    async def get_token_usage(self, session_key: str) -> tuple[int, int]:
        """读取会话累计 token 用量。行不存在返回 (0, 0)。"""
        async with self._factory() as session:
            result = await session.execute(
                select(ChatSession).where(ChatSession.session_key == session_key)
            )
            row = result.scalar_one_or_none()
            if row is None:
                return (0, 0)
            return (row.prompt_tokens or 0, row.completion_tokens or 0)

    async def add_token_usage(
        self, session_key: str, prompt_delta: int, completion_delta: int
    ) -> None:
        """累加 token 用量增量（UPSERT）。"""
        async with self._factory() as session:
            result = await session.execute(
                select(ChatSession).where(ChatSession.session_key == session_key)
            )
            row = result.scalar_one_or_none()
            now = _shanghai_now()
            if row is None:
                row = ChatSession(
                    session_key=session_key,
                    messages_json="[]",
                    prompt_tokens=prompt_delta,
                    completion_tokens=completion_delta,
                    updated_at=now,
                    created_at=now,
                )
                session.add(row)
            else:
                row.prompt_tokens = (row.prompt_tokens or 0) + prompt_delta
                row.completion_tokens = (row.completion_tokens or 0) + completion_delta
                row.updated_at = now
            await session.commit()

    async def set_token_usage(
        self, session_key: str, prompt_tokens: int, completion_tokens: int
    ) -> None:
        """设置 token 用量绝对值（用于 /switch 恢复归档快照）。"""
        async with self._factory() as session:
            result = await session.execute(
                select(ChatSession).where(ChatSession.session_key == session_key)
            )
            row = result.scalar_one_or_none()
            now = _shanghai_now()
            if row is None:
                row = ChatSession(
                    session_key=session_key,
                    messages_json="[]",
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    updated_at=now,
                    created_at=now,
                )
                session.add(row)
            else:
                row.prompt_tokens = prompt_tokens
                row.completion_tokens = completion_tokens
                row.updated_at = now
            await session.commit()

    async def clear_session(self, session_key: str) -> int:
        """清空上下文并归零 token 计数。返回清空前的消息条数。"""
        async with self._factory() as session:
            result = await session.execute(
                select(ChatSession).where(ChatSession.session_key == session_key)
            )
            row = result.scalar_one_or_none()
            if row is None:
                return 0
            prev_count = len(json.loads(row.messages_json or "[]"))
            row.messages_json = "[]"
            row.prompt_tokens = 0
            row.completion_tokens = 0
            row.updated_at = _shanghai_now()
            await session.commit()
            return prev_count

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
                        "prompt_tokens": row.prompt_tokens or 0,
                        "completion_tokens": row.completion_tokens or 0,
                        "total_tokens": (row.prompt_tokens or 0)
                        + (row.completion_tokens or 0),
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
