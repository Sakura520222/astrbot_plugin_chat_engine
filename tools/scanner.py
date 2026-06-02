"""Tool scanner — enumerate all registered tools from AstrBot's tool system.
Scans builtin tools, plugin-registered tools, and MCP tools.
"""

from astrbot.api import logger
from astrbot.core.agent.tool import ToolSet


class ToolScanner:
    """工具扫描器 — 扫描所有已注册的 Tools"""

    def __init__(self, llm_tool_manager):
        """
        Args:
            llm_tool_manager: FunctionToolManager instance from
                              self.context.get_llm_tool_manager()
        """
        self.manager = llm_tool_manager

    def scan_all_tools(self) -> list[dict]:
        """扫描所有注册的工具 (builtin + plugin + MCP)

        Returns:
            list[dict]: 工具信息列表，每项包含 name, description, parameters, source
        """
        tools = []
        seen_names: set[str] = set()

        # 1. 内置工具
        try:
            for tool in self.manager.iter_builtin_tools():
                if tool.name not in seen_names:
                    tools.append(self._tool_to_info(tool, "builtin"))
                    seen_names.add(tool.name)
        except Exception as e:
            logger.warning(f"[ChatEngine] 扫描内置工具失败: {e}")

        # 2. 插件 + MCP 工具 (func_list)
        try:
            for tool in self.manager.func_list:
                if tool.name not in seen_names:
                    source = (
                        "mcp" if getattr(tool, "mcp_server_name", None) else "plugin"
                    )
                    tools.append(self._tool_to_info(tool, source))
                    seen_names.add(tool.name)
        except Exception as e:
            logger.warning(f"[ChatEngine] 扫描插件工具失败: {e}")

        return tools

    def _tool_to_info(self, tool, source: str) -> dict:
        """将 FunctionTool 转为信息字典"""
        return {
            "name": tool.name,
            "description": tool.description or "",
            "parameters": tool.parameters if isinstance(tool.parameters, dict) else {},
            "source": source,
            "active": getattr(tool, "active", True),
        }

    def build_active_tool_set(self, enabled_names: set[str]) -> ToolSet:
        """构建只包含已启用工具的 ToolSet

        Args:
            enabled_names: 已启用的工具名称集合

        Returns:
            ToolSet: 可传递给 provider.text_chat(func_tool=...) 的工具集
        """
        tool_set = ToolSet()
        seen: set[str] = set()

        # 内置工具
        try:
            for tool in self.manager.iter_builtin_tools():
                if tool.name in enabled_names and tool.name not in seen:
                    if getattr(tool, "active", True):
                        tool_set.add_tool(tool)
                        seen.add(tool.name)
        except Exception:
            pass

        # 插件 + MCP 工具
        try:
            for tool in self.manager.func_list:
                if tool.name in enabled_names and tool.name not in seen:
                    if getattr(tool, "active", True):
                        tool_set.add_tool(tool)
                        seen.add(tool.name)
        except Exception:
            pass

        return tool_set

    def build_tool_description_text(
        self, tools_info: list[dict], enabled_names: set[str]
    ) -> str:
        """构建工具描述文本 (用于写入 system prompt)

        只包含已启用的工具。格式便于 LLM 理解。
        """
        lines = []
        for info in tools_info:
            if info["name"] not in enabled_names:
                continue
            desc = info["description"] or "无描述"
            params = info.get("parameters", {})
            param_str = ""
            if params and params.get("properties"):
                param_parts = []
                props = params["properties"]
                required = params.get("required", [])
                for pname, pinfo in props.items():
                    ptype = pinfo.get("type", "any")
                    pdesc = pinfo.get("description", "")
                    req = " (必填)" if pname in required else ""
                    param_parts.append(f"  - {pname} ({ptype}){req}: {pdesc}")
                if param_parts:
                    param_str = "\n参数:\n" + "\n".join(param_parts)

            lines.append(f"- **{info['name']}**: {desc}{param_str}")

        if not lines:
            return ""
        return "你可以使用以下工具:\n\n" + "\n\n".join(lines)
