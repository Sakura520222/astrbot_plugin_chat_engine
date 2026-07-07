"""Database model definitions using independent SQLAlchemy MetaData.

Uses a SEPARATE MetaData instance to avoid any interference with AstrBot's
global SQLModel metadata. This prevents the CancelledError crash.
"""

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from ..utils import shanghai_now as _shanghai_now

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
    messages_json: Mapped[str] = mapped_column(Text, default="[]")
    # 当前会话累计 Token 用量（估算值，/stats 与 WebUI 读取）
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
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
    # 归档时的 Token 用量快照（/switch 恢复时读回）
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
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


class CEImageQuota(ChatEngineBase):
    """图片生成配额计数 — 普通用户每日画图/改图用量。

    quota_key + date 唯一:同一计数对象(用户或会话)每天一行。
    quota_key 格式由调用方决定,如 ``user:{sender_id}`` / ``session:{session_key}``。
    date 为上海时区 ``YYYY-MM-DD``,跨日自然产生新行实现"每日重置"。
    """

    __tablename__ = "ce_image_quotas"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    quota_key: Mapped[str] = mapped_column(String(512), index=True)
    date: Mapped[str] = mapped_column(String(10))  # YYYY-MM-DD (上海时区)
    used_count: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_shanghai_now)

    __table_args__ = (
        UniqueConstraint("quota_key", "date", name="uq_image_quota_key_date"),
    )
