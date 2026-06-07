"""命令分发器 — 扫描并执行其他插件注册的命令。

允许用户通过 LLM 自然语言调用其他插件的命令。
尊重每个命令自身的权限定义（admin / member / everyone）。
"""

import inspect
import re
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.core.star.filter.command import CommandFilter
from astrbot.core.star.filter.command_group import CommandGroupFilter
from astrbot.core.star.filter.permission import PermissionType, PermissionTypeFilter
from astrbot.core.star.star import star_map
from astrbot.core.star.star_handler import star_handlers_registry


class CommandDispatcher:
    """命令扫描与分发器"""

    def __init__(self):
        self._commands_cache: list[dict] | None = None

    def scan_commands(self) -> list[dict]:
        """扫描所有已注册的命令处理器（不含 chat_engine 自身）。

        Returns:
            list[dict]: 命令信息列表，每项包含 name, description, parameters, plugin_name, permission
        """
        if self._commands_cache is not None:
            return self._commands_cache

        commands: list[dict] = []
        seen: set[str] = set()

        for handler in star_handlers_registry:
            # 跳过 chat_engine 自身的 handler
            if "chat_engine" in str(getattr(handler, "handler_module_path", "")):
                continue

            # 跳过未激活的插件
            plugin_meta = star_map.get(handler.handler_module_path)
            if not plugin_meta or not getattr(plugin_meta, "activated", False):
                continue

            # 跳过已禁用的 handler
            if not getattr(handler, "enabled", True):
                continue

            # 查找 CommandFilter
            cmd_filter: CommandFilter | None = None
            for f in getattr(handler, "event_filters", []):
                if isinstance(f, CommandFilter):
                    cmd_filter = f
                    break

            if not cmd_filter:
                continue

            # 获取完整命令名列表
            try:
                names = cmd_filter.get_complete_command_names()
            except Exception:
                continue

            if not names:
                continue

            # 跳过命令组 handler（它本身不执行具体操作，子命令才是）
            is_group = False
            for f in getattr(handler, "event_filters", []):
                if isinstance(f, CommandGroupFilter):
                    is_group = True
                    break

            if is_group:
                continue

            primary_name = names[0]

            # 去重
            if primary_name in seen:
                continue
            seen.add(primary_name)

            # 获取插件名
            plugin_meta = star_map.get(handler.handler_module_path)
            plugin_name = (
                plugin_meta.display_name or plugin_meta.name
                if plugin_meta
                else handler.handler_module_path
            )

            # 构建参数描述
            param_desc = self._build_param_description(cmd_filter)

            # 确定命令权限
            permission = self._determine_permission(handler)

            commands.append(
                {
                    "name": primary_name,
                    "aliases": [n for n in names[1:] if n],
                    "description": handler.desc or "",
                    "parameters": param_desc,
                    "plugin_name": plugin_name,
                    "permission": permission,
                    "handler": handler,
                    "filter": cmd_filter,
                }
            )

        self._commands_cache = commands
        return commands

    def refresh(self):
        """清除缓存，强制下次重新扫描"""
        self._commands_cache = None

    def list_plugins(self) -> list[dict]:
        """列出所有提供命令的插件（含框架内置）。

        Returns:
            list[dict]: 插件信息列表，每项包含 name, command_count
        """
        commands = self.scan_commands()
        plugin_map: dict[str, int] = {}
        for cmd in commands:
            name = cmd["plugin_name"]
            plugin_map[name] = plugin_map.get(name, 0) + 1

        return [
            {"name": name, "command_count": count}
            for name, count in sorted(plugin_map.items())
        ]

    def list_commands(self, plugin: str = "", query: str = "") -> list[dict]:
        """查询命令列表，支持按插件名或关键词过滤。

        Args:
            plugin: 按插件名过滤（精确匹配）。
            query: 按关键词过滤（模糊匹配命令名、描述、别名）。

        Returns:
            list[dict]: 命令信息列表
        """
        commands = self.scan_commands()

        if plugin:
            commands = [c for c in commands if c["plugin_name"] == plugin]

        if query:
            q = query.lower()
            commands = [
                c
                for c in commands
                if q in c["name"].lower()
                or q in c["description"].lower()
                or any(q in a.lower() for a in c["aliases"])
            ]

        result = []
        for cmd in commands:
            item = {"name": cmd["name"]}
            if cmd["aliases"]:
                item["aliases"] = cmd["aliases"]
            if cmd["description"]:
                item["description"] = cmd["description"]
            if cmd["parameters"]:
                item["parameters"] = cmd["parameters"]
            if cmd["permission"] == "admin":
                item["admin_only"] = True
            result.append(item)

        return result

    def find_handler(self, command_str: str):
        """根据命令字符串查找匹配的 handler。

        Args:
            command_str: 命令字符串（不含唤醒前缀），如 "help" 或 "plugin list"

        Returns:
            匹配的 (handler, cmd_filter, matched_name, permission) 元组，未找到返回 None
        """
        command_str = re.sub(r"\s+", " ", command_str.strip())
        if not command_str:
            return None

        best_match = None
        best_match_len = 0

        for handler in star_handlers_registry:
            if "chat_engine" in str(getattr(handler, "handler_module_path", "")):
                continue
            # 跳过未激活的插件
            plugin_meta = star_map.get(handler.handler_module_path)
            if not plugin_meta or not getattr(plugin_meta, "activated", False):
                continue
            if not getattr(handler, "enabled", True):
                continue

            for f in getattr(handler, "event_filters", []):
                if not isinstance(f, CommandFilter):
                    continue
                try:
                    names = f.get_complete_command_names()
                except Exception:
                    continue
                for name in names:
                    if not name:
                        continue
                    if command_str == name or command_str.startswith(name + " "):
                        if len(name) > best_match_len:
                            perm = self._determine_permission(handler)
                            best_match = (handler, f, name, perm)
                            best_match_len = len(name)

        return best_match

    async def dispatch(
        self, event: AstrMessageEvent, command_str: str, capture_result: bool = False
    ) -> dict[str, Any]:
        """分发命令并返回执行结果。

        Args:
            event: 消息事件
            command_str: 命令字符串
            capture_result: 是否捕获结果链（用于直接发送给用户）

        Returns:
            dict: 包含 success (bool) 和 result (str) 的结果字典。
                  当 capture_result=True 时，额外包含 result_chain (list)。
        """
        command_str = re.sub(r"\s+", " ", command_str.strip())

        # 去除前导 /
        if command_str.startswith("/"):
            command_str = command_str[1:].strip()

        if not command_str:
            return {"success": False, "result": "命令不能为空。"}

        match = self.find_handler(command_str)
        if not match:
            return {
                "success": False,
                "result": (
                    f"未找到匹配的命令: '{command_str}'。"
                    "请直接告知用户该命令不存在，不要尝试其他命令。"
                ),
            }

        handler, cmd_filter, matched_name, permission = match

        # 权限检查：尊重命令自身的权限定义
        if permission == "admin" and not event.is_admin():
            return {
                "success": False,
                "result": f"权限不足：命令 '{matched_name}' 仅限管理员使用。",
            }

        # 提取剩余文本作为参数
        remaining = command_str[len(matched_name) :].strip()
        params_list = [p for p in remaining.split(" ") if p] if remaining else []

        # 解析参数
        try:
            handler_params = getattr(cmd_filter, "handler_params", {})
            if handler_params and params_list:
                params = cmd_filter.validate_and_convert_params(
                    params_list, handler_params
                )
            else:
                params = {}
        except ValueError as e:
            return {
                "success": False,
                "result": f"参数解析失败: {e}",
            }

        # 执行命令 handler
        try:
            plugin_meta = star_map.get(handler.handler_module_path)
            plugin_display = (
                plugin_meta.display_name or plugin_meta.name
                if plugin_meta
                else "未知插件"
            )
            logger.info(
                f"[ChatEngine] 命令分发: {command_str} -> "
                f"{plugin_display}.{handler.handler_name}"
            )

            # 保存当前 event 状态
            saved_result = event._result
            saved_stopped = event._force_stopped

            captured_components: list = []

            try:
                ret = handler.handler(event, **params)
            except TypeError:
                # handler 可能不接受 **params
                ret = handler.handler(event)

            result_text = None

            if inspect.isasyncgen(ret):
                # 异步生成器 — 收集所有 yield 的值
                parts = []
                async for item in ret:
                    if item is not None:
                        parts.append(str(item))
                        # 捕获每次 yield 的结果链
                        if capture_result and hasattr(item, "_result") and item._result:
                            chain = getattr(item._result, "chain", [])
                            captured_components.extend(chain)
                result_text = parts[-1] if parts else None
            elif inspect.isawaitable(ret):
                result = await ret
                if result is not None:
                    result_text = str(result)
            elif ret is not None:
                result_text = str(ret)

            # 捕获 event._result（适用于非 async gen 或 async gen 未 yield 的情况）
            if capture_result and not captured_components and event._result is not None:
                chain = getattr(event._result, "chain", [])
                captured_components.extend(chain)

            # 检查 handler 是否通过 event.set_result 设置了结果
            if result_text is None and event._result is not None:
                result_chain = getattr(event._result, "chain", [])
                if result_chain:
                    text_parts = []
                    for comp in result_chain:
                        if hasattr(comp, "text"):
                            text_parts.append(comp.text)
                    result_text = "".join(text_parts) if text_parts else None

            # 恢复 event 状态（防止影响后续流程）
            event._result = saved_result
            event._force_stopped = saved_stopped

            result = {
                "success": True,
                "result": result_text or "命令执行完成（无输出）",
            }

            if capture_result and captured_components:
                result["result_chain"] = captured_components

            return result

        except Exception as e:
            logger.error(f"[ChatEngine] 命令分发执行失败: {e}", exc_info=True)
            return {
                "success": False,
                "result": f"命令执行失败: {type(e).__name__}: {e}",
            }

    #  内部工具方法

    @staticmethod
    def _determine_permission(handler) -> str:
        """从 handler 的 event_filters 中确定权限等级。

        Returns:
            "admin" | "member" | "everyone"
        """
        for f in getattr(handler, "event_filters", []):
            if isinstance(f, PermissionTypeFilter):
                if f.permission_type == PermissionType.ADMIN:
                    return "admin"
                return "member"
        return "everyone"

    @staticmethod
    def _build_param_description(cmd_filter: CommandFilter) -> str:
        """从 CommandFilter 构建参数描述文本。"""
        handler_params = getattr(cmd_filter, "handler_params", {})
        if not handler_params:
            return ""

        parts = []
        for name, val in handler_params.items():
            if isinstance(val, type):
                parts.append(f"{name}({val.__name__})")
            elif isinstance(val, str) and val:
                parts.append(f'{name}="{val}"')
            elif isinstance(val, bool):
                parts.append(f"{name}={val}")
            elif isinstance(val, (int, float)):
                parts.append(f"{name}={val}")
            else:
                parts.append(f"{name}")

        return ", ".join(parts)
