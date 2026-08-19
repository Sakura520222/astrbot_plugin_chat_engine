"""Tool state manager — tracks enabled/disabled tools using the database."""

from astrbot.api import logger

from ..db.tool_config_repo import ToolConfigRepository
from .scanner import ToolScanner


class ChatToolManager:
    """工具管理器 — 管理工具的启用/禁用状态和扫描同步"""

    def __init__(self, scanner: ToolScanner, config_repo: ToolConfigRepository):
        self.scanner = scanner
        self.repo = config_repo
        self._cached_disabled: set[str] | None = None

    async def refresh_tools(self) -> list[dict]:
        """扫描并同步工具列表到数据库。返回所有工具信息。"""
        tools_info = self.scanner.scan_all_tools()
        try:
            await self.repo.sync_tools(tools_info)
        except Exception as e:
            logger.error(f"[ChatEngine] 同步工具配置失败: {e}")

        # 刷新缓存
        self._cached_disabled = None
        return tools_info

    async def _get_disabled_tools_cached(self) -> set[str]:
        """获取数据库中的用户禁用偏好 (带缓存，仅 enable/disable 时变化)"""
        if self._cached_disabled is None:
            self._cached_disabled = await self.repo.get_disabled_tools()
        return self._cached_disabled

    async def get_enabled_names(self) -> set[str]:
        """获取已启用且当前可用的工具名称集合。

        用户禁用偏好来自数据库（带缓存）；工具可用性（所属插件已启用、
        工具未被 AstrBot 停用）每次实时扫描，插件启停后立即生效。
        """
        disabled = await self._get_disabled_tools_cached()
        all_tools = self.scanner.scan_all_tools()
        available_names = {t["name"] for t in all_tools if t.get("available", True)}
        return available_names - disabled

    async def get_all_tools_info(self) -> list[dict]:
        """获取所有工具信息 (含启用状态)"""
        tools_info = self.scanner.scan_all_tools()
        disabled = await self.repo.get_disabled_tools()
        for info in tools_info:
            info["enabled"] = info["name"] not in disabled
        return tools_info

    async def enable_tool(self, tool_name: str) -> None:
        """启用工具"""
        await self.repo.set_tool_enabled(tool_name, True)
        self._cached_disabled = None

    async def disable_tool(self, tool_name: str) -> None:
        """禁用工具"""
        await self.repo.set_tool_enabled(tool_name, False)
        self._cached_disabled = None

    async def build_active_tool_set(self):
        """构建已启用工具的 ToolSet"""
        enabled = await self.get_enabled_names()
        return self.scanner.build_active_tool_set(enabled)

    async def build_tool_description_text(self) -> str:
        """构建已启用工具的描述文本"""
        enabled = await self.get_enabled_names()
        tools_info = self.scanner.scan_all_tools()
        return self.scanner.build_tool_description_text(tools_info, enabled)
