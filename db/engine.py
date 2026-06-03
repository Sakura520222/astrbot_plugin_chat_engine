"""Database engine setup for the Chat Engine plugin.

Uses a completely independent SQLAlchemy engine with its own MetaData.
This ensures NO interference with AstrBot's global SQLModel metadata.
"""

import os

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from .models import (  # noqa: F401
    CEImage,
    CEPersona,
    ChatSession,
    ToolConfig,
    chat_engine_metadata,
)


class ChatEngineDB:
    """独立数据库引擎管理器 — 使用独立 MetaData"""

    def __init__(self, db_url: str):
        self.db_url = db_url
        self.engine = create_async_engine(self.db_url, echo=False)
        self.session_factory = async_sessionmaker(
            self.engine, class_=AsyncSession, expire_on_commit=False
        )
        self.session_repo = None
        self.persona_repo = None
        self.tool_config_repo = None
        self.image_repo = None
        self.image_store = None

    async def initialize(self):
        """创建所有表并初始化 Repository"""
        # 使用插件自己的 MetaData，而非 SQLModel.metadata
        async with self.engine.begin() as conn:
            await conn.run_sync(chat_engine_metadata.create_all)

        from .image_repo import ImageRepository
        from .persona_repo import PersonaRepository
        from .session_repo import SessionRepository
        from .tool_config_repo import ToolConfigRepository

        self.session_repo = SessionRepository(self.session_factory)
        self.persona_repo = PersonaRepository(self.session_factory)
        self.tool_config_repo = ToolConfigRepository(self.session_factory)
        self.image_repo = ImageRepository(self.session_factory)

    def init_image_store(self, data_dir: str):
        """初始化图片存储服务（需要在 initialize 之后调用）"""
        from .image_store import ImageStore

        image_dir = os.path.join(data_dir, "images")
        self.image_store = ImageStore(image_dir, self.image_repo)

    async def close(self):
        """关闭数据库连接"""
        await self.engine.dispose()

    @staticmethod
    def build_db_url(db_type: str, data_dir: str, mysql_url: str = "") -> str:
        """根据配置构建数据库 URL"""
        if db_type == "mysql" and mysql_url:
            return mysql_url
        # 默认 SQLite
        db_path = os.path.join(data_dir, "chat_engine.db")
        return f"sqlite+aiosqlite:///{db_path}"
