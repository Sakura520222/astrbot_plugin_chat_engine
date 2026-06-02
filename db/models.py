"""Database model definitions using independent SQLAlchemy MetaData.

Uses a SEPARATE MetaData instance to avoid any interference with AstrBot's
global SQLModel metadata. This prevents the CancelledError crash.
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, MetaData, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# 独立的 MetaData 实例 — 与 AstrBot 的 SQLModel.metadata 完全隔离
chat_engine_metadata = MetaData()


class ChatEngineBase(DeclarativeBase):
    """Chat Engine 插件的 ORM 基类，使用独立 MetaData"""

    metadata = chat_engine_metadata


class ChatSession(ChatEngineBase):
    """聊天会话/上下文存储"""

    __tablename__ = "ce_chat_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_key: Mapped[str] = mapped_column(String(512), unique=True, index=True)
    messages_json: Mapped[str] = mapped_column(String, default="[]")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class CEPersona(ChatEngineBase):
    """人格/角色定义"""

    __tablename__ = "ce_personas"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(256), unique=True)
    system_prompt: Mapped[str] = mapped_column(String, default="")
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ToolConfig(ChatEngineBase):
    """工具启用/禁用配置"""

    __tablename__ = "ce_tool_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tool_name: Mapped[str] = mapped_column(String(256), unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    source: Mapped[str] = mapped_column(String(32), default="builtin")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
