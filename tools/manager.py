"""Tool state manager — tracks enabled/disabled tools using the database."""

from astrbot.api import logger

from ..db.tool_config_repo import ToolConfigRepository
from .scanner import ToolScanner


class ChatToolManager:
    """工具管理器 — 管理工具的启用/禁用状态和扫描同步"""

    def __init__(self, scanner: ToolScanner, config_repo: ToolConfigRepository):
        self.scanner = scanner
        self.repo = config_repo
        self._cached_enabled: set[str] | None = None

    async def refresh_tools(self) -> list[dict]:
        """扫描并同步工具列表到数据库。返回所有工具信息。"""
        tools_info = self.scanner.scan_all_tools()
        try:
            await self.repo.sync_tools(tools_info)
        except Exception as e:
            logger.error(f"[ChatEngine] 同步工具配置失败: {e}")

        # 刷新缓存
        self._cached_enabled = None
        return tools_info

    async def get_enabled_names(self) -> set[str]:
        """获取已启用工具的名称集合 (带缓存)"""
        if self._cached_enabled is None:
            disabled = await self.repo.get_disabled_tools()
            all_tools = self.scanner.scan_all_tools()
            all_names = {t["name"] for t in all_tools}
            self._cached_enabled = all_names - disabled
        return self._cached_enabled

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
        self._cached_enabled = None

    async def disable_tool(self, tool_name: str) -> None:
        """禁用工具"""
        await self.repo.set_tool_enabled(tool_name, False)
        self._cached_enabled = None

    async def build_active_tool_set(self):
        """构建已启用工具的 ToolSet"""
        enabled = await self.get_enabled_names()
        return self.scanner.build_active_tool_set(enabled)

    async def build_tool_description_text(self) -> str:
        """构建已启用工具的描述文本"""
        enabled = await self.get_enabled_names()
        tools_info = self.scanner.scan_all_tools()
        return self.scanner.build_tool_description_text(tools_info, enabled)
