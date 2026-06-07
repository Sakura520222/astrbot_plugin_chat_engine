"""Tool config CRUD operations"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from ..utils import shanghai_now as _shanghai_now
from .models import ToolConfig


class ToolConfigRepository:
    """工具配置仓库 — 管理工具的启用/禁用状态"""

    def __init__(self, session_factory: async_sessionmaker):
        self._factory = session_factory

    async def get_enabled_tools(self) -> set[str]:
        """获取所有已启用工具的名称集合。未记录的工具默认启用。"""
        async with self._factory() as session:
            result = await session.execute(
                select(ToolConfig).where(ToolConfig.enabled == True)  # noqa: E712
            )
            return {row.tool_name for row in result.scalars().all()}

    async def get_disabled_tools(self) -> set[str]:
        """获取所有已禁用工具的名称集合"""
        async with self._factory() as session:
            result = await session.execute(
                select(ToolConfig).where(ToolConfig.enabled == False)  # noqa: E712
            )
            return {row.tool_name for row in result.scalars().all()}

    async def is_tool_enabled(self, tool_name: str) -> bool:
        """检查工具是否启用。未记录的工具默认启用。"""
        async with self._factory() as session:
            result = await session.execute(
                select(ToolConfig).where(ToolConfig.tool_name == tool_name)
            )
            row = result.scalar_one_or_none()
            if row is None:
                return True  # 未记录 = 默认启用
            return row.enabled

    async def set_tool_enabled(
        self, tool_name: str, enabled: bool, source: str = "builtin"
    ) -> None:
        """设置工具启用/禁用状态"""
        async with self._factory() as session:
            result = await session.execute(
                select(ToolConfig).where(ToolConfig.tool_name == tool_name)
            )
            row = result.scalar_one_or_none()

            if row is None:
                row = ToolConfig(
                    tool_name=tool_name,
                    enabled=enabled,
                    source=source,
                    updated_at=_shanghai_now(),
                )
                session.add(row)
            else:
                row.enabled = enabled
                row.source = source
                row.updated_at = _shanghai_now()

            await session.commit()

    async def get_all_configs(self) -> list[ToolConfig]:
        """获取所有工具配置"""
        async with self._factory() as session:
            result = await session.execute(select(ToolConfig))
            return list(result.scalars().all())

    async def sync_tools(self, tool_infos: list[dict]) -> None:
        """同步扫描到的工具列表到数据库。新工具默认启用。"""
        async with self._factory() as session:
            result = await session.execute(select(ToolConfig))
            existing = {row.tool_name: row for row in result.scalars().all()}

            for info in tool_infos:
                name = info["name"]
                source = info.get("source", "builtin")
                if name not in existing:
                    session.add(
                        ToolConfig(
                            tool_name=name,
                            enabled=True,
                            source=source,
                            updated_at=_shanghai_now(),
                        )
                    )

            await session.commit()
