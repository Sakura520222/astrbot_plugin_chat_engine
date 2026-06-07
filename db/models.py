"""Database model definitions using independent SQLAlchemy MetaData.

Uses a SEPARATE MetaData instance to avoid any interference with AstrBot's
global SQLModel metadata. This prevents the CancelledError crash.
"""

from datetime import datetime, timezone, timedelta

from sqlalchemy import Boolean, DateTime, Integer, MetaData, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# 独立的 MetaData 实例 — 与 AstrBot 的 SQLModel.metadata 完全隔离
chat_engine_metadata = MetaData()

# 上海时区 (UTC+8)
_SHANGHAI_TZ = timezone(timedelta(hours=8))


def _shanghai_now() -> datetime:
    """返回当前上海时区的 naive datetime（去除 tzinfo，与旧数据兼容）。"""
    return datetime.now(_SHANGHAI_TZ).replace(tzinfo=None)


class ChatEngineBase(DeclarativeBase):
    """Chat Engine 插件的 ORM 基类，使用独立 MetaData"""

    metadata = chat_engine_metadata


class ChatSession(ChatEngineBase):
    """聊天会话/上下文存储"""

    __tablename__ = "ce_chat_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_key: Mapped[str] = mapped_column(String(512), unique=True, index=True)
    messages_json: Mapped[str] = mapped_column(Text, default="[]")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_shanghai_now)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_shanghai_now)


class CEImage(ChatEngineBase):
    """图片存储 — 按 sha256 去重，同一张图片只存一份"""

    __tablename__ = "ce_images"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sha256: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    mime_type: Mapped[str] = mapped_column(String(32))
    file_path: Mapped[str] = mapped_column(String(512))
    file_size: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_shanghai_now)


class CEPersona(ChatEngineBase):
    """人格/角色定义"""

    __tablename__ = "ce_personas"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(256), unique=True)
    system_prompt: Mapped[str] = mapped_column(Text, default="")
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_shanghai_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_shanghai_now)


class CEArchivedSession(ChatEngineBase):
    """归档会话 — 多会话支持，存储非活跃的历史会话"""

    __tablename__ = "ce_archived_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_key: Mapped[str] = mapped_column(String(512), index=True)
    title: Mapped[str] = mapped_column(String(256), default="")
    messages_json: Mapped[str] = mapped_column(Text, default="[]")
    message_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_shanghai_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_shanghai_now)


class ToolConfig(ChatEngineBase):
    """工具启用/禁用配置"""

    __tablename__ = "ce_tool_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tool_name: Mapped[str] = mapped_column(String(256), unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    source: Mapped[str] = mapped_column(String(32), default="builtin")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_shanghai_now)
