"""astrbot_plugin_chat_engine — 完全替代 AstrBot 自带聊天功能的插件。

深度劫持消息管道，独立实现:
- 上下文管理 (群聊共享 / 私聊隔离)
- 用户识别 ({{user}{昵称}({ID})}说：格式)
- 人格管理 (独立于 AstrBot)
- Tool Calls (扫描所有工具，原生 function calling)
- 上下文压缩 (轮数限制 / Token 阈值 LLM 总结)
- WebUI 管理面板 (独立 aiohttp 服务)
"""

import asyncio
import inspect
import json
import re

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image
from astrbot.api.star import Context, Star, StarTools, register

from .context.manager import ChatContextManager
from .context.token_counter import TokenEstimator
from .db.engine import ChatEngineDB
from .persona.manager import ChatPersonaManager
from .tools.manager import ChatToolManager
from .tools.scanner import ToolScanner
from .web.server import ChatWebServer


@register(
    "astrbot_plugin_chat_engine",
    "车厘子小樱",
    "完全替代 AstrBot 自带聊天功能。独立实现上下文管理、用户识别、人格系统、Tool Calls、上下文压缩和 WebUI 管理面板。",
    "1.1.0",
)
class ChatEnginePlugin(Star):
    """Chat Engine 插件主类"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.db: ChatEngineDB = None
        self.context_mgr: ChatContextManager = None
        self.persona_mgr: ChatPersonaManager = None
        self.tool_mgr: ChatToolManager = None
        self.web_server: ChatWebServer = None

    async def initialize(self):
        """插件激活时调用 — 初始化数据库、管理器和 Web 服务"""
        logger.info("[ChatEngine] 正在初始化...")

        # 数据目录
        data_dir = StarTools.get_data_dir("astrbot_plugin_chat_engine")

        # 构建数据库 URL
        db_type = self.config.get("db_type", "sqlite")
        mysql_url = self.config.get("mysql_url", "")
        db_url = ChatEngineDB.build_db_url(db_type, data_dir, mysql_url)

        # 初始化数据库
        self.db = ChatEngineDB(db_url)
        await self.db.initialize()
        logger.info(f"[ChatEngine] 数据库已初始化 ({db_type})")

        # Provider getter (延迟获取)
        def _get_provider():
            return self.context.get_using_provider()

        # 初始化管理器
        self.persona_mgr = ChatPersonaManager(self.db.persona_repo)
        self.context_mgr = ChatContextManager(
            session_repo=self.db.session_repo,
            persona_repo=self.db.persona_repo,
            config=self.config,
            provider_getter=_get_provider,
        )

        # 工具管理器
        tool_scanner = ToolScanner(self.context.get_llm_tool_manager())
        self.tool_mgr = ChatToolManager(tool_scanner, self.db.tool_config_repo)
        # 同步工具列表
        tools = await self.tool_mgr.refresh_tools()
        logger.info(f"[ChatEngine] 扫描到 {len(tools)} 个工具")

        # 启动 WebUI
        web_port = int(self.config.get("web_port", 8765))
        self.web_server = ChatWebServer(self, port=web_port)
        await self.web_server.start()

        logger.info(f"[ChatEngine] 初始化完成, WebUI: http://localhost:{web_port}")

    async def terminate(self):
        """插件停用/重载时调用"""
        logger.info("[ChatEngine] 正在关闭...")
        if self.web_server:
            await self.web_server.stop()
        if self.db:
            await self.db.close()
        logger.info("[ChatEngine] 已关闭")

    # ========================================================================
    # 消息拦截 — 核心处理流程
    # ========================================================================

    @filter.event_message_type(filter.EventMessageType.ALL, priority=9999)
    async def handle_all_messages(self, event: AstrMessageEvent):
        """拦截所有消息，完全接管 AstrBot 的聊天流程。"""
        # ---------- 第一步: 无条件抑制 AstrBot 默认 LLM ----------
        event.should_call_llm(True)

        try:
            # ---------- 预检查 ----------
            message_text = (event.message_str or "").strip()
            sender = event.get_sender_name() or event.get_sender_id() or "unknown"
            is_group = self.context_mgr.is_group_message(event)
            is_at = event.is_at_or_wake_command

            logger.info(
                f"[ChatEngine] 收到消息: sender={sender}, group={is_group}, "
                f"at={is_at}, text={message_text[:50]}"
            )

            if not message_text:
                logger.info("[ChatEngine] 空消息，跳过")
                event.should_call_llm(False)
                return

            # ---------- 命令检测: 如果有其他插件的命令处理器匹配了此消息，交给它们处理 ----------
            # 只检查 CommandFilter 类型的处理器，忽略 event_message_type(ALL) 广播处理器
            activated_handlers = event.get_extra("activated_handlers") or []
            has_command_handler = False
            for h in activated_handlers:
                if "chat_engine" in str(getattr(h, "handler_module_path", "")):
                    continue  # 跳过 ChatEngine 自己的 handler
                for f in getattr(h, "event_filters", []):
                    # CommandFilter = 精确命令匹配, RegexFilter = 正则匹配
                    filter_type = type(f).__name__
                    if filter_type in (
                        "CommandFilter",
                        "RegexFilter",
                        "CommandGroupFilter",
                    ):
                        has_command_handler = True
                        break
                if has_command_handler:
                    break

            if has_command_handler:
                logger.info(
                    "[ChatEngine] 检测到命令处理器, 跳过，交给其他插件或框架处理"
                )
                event.should_call_llm(False)
                return

            # 判断是否应该响应 (群聊未@Bot时跳过)
            if not self.context_mgr.should_respond(event):
                # 被动记录: 群聊中未触发回复的消息也记录到上下文
                if (
                    self.config.get("enable_passive_record", False)
                    and is_group
                    and message_text
                ):
                    try:
                        passive_key = self.context_mgr.build_session_key(event)
                        passive_text = self.context_mgr.format_user_message(event)
                        # 使用 "observed" role 而非 "user"，防止压缩器
                        # 将每条被动消息都计为独立一轮
                        passive_msg = {"role": "observed", "content": passive_text}
                        await self.context_mgr.record_passive_message(
                            passive_key, passive_msg
                        )
                        logger.debug(
                            f"[ChatEngine] 被动记录消息到 {passive_key}"
                        )
                    except Exception as e:
                        logger.debug(f"[ChatEngine] 被动记录失败: {e}")

                event.should_call_llm(False)  # 恢复默认 LLM
                return

            # ---------- 获取 Provider ----------
            provider = self.context.get_using_provider(event.unified_msg_origin)
            if not provider:
                logger.warning("[ChatEngine] 未找到 LLM Provider")
                yield event.plain_result(
                    "❌ 未配置 LLM Provider，请在 AstrBot 设置中配置。"
                )
                return

            logger.info(f"[ChatEngine] 使用 Provider: {provider.meta().id}")

            # ---------- 构建会话 Key ----------
            session_key = self.context_mgr.build_session_key(event)
            logger.info(f"[ChatEngine] 会话 Key: {session_key}")

            # ---------- 加载上下文 ----------
            context_messages_raw = await self.context_mgr.load_context(session_key)
            # 被动记录消息使用 "observed" role 存储在数据库中，避免压缩器将每条
            # 被动消息都计为独立一轮（与 user/assistant 配对压缩逻辑冲突）。
            # 此处将其转换为 "user" role 供 LLM API 使用，同时拷贝一份避免修改原始数据。
            context_messages = [
                {**msg, "role": "user"} if msg.get("role") == "observed" else msg
                for msg in context_messages_raw
            ]
            logger.info(f"[ChatEngine] 已加载 {len(context_messages)} 条上下文消息")

            # ---------- 格式化用户消息 ----------
            user_text = self.context_mgr.format_user_message(event)

            image_urls = self._extract_image_urls(event)
            if image_urls:
                user_msg = {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": user_text},
                    ]
                    + [
                        {"type": "image_url", "image_url": {"url": url}}
                        for url in image_urls
                    ],
                }
            else:
                user_msg = {"role": "user", "content": user_text}

            # ---------- 获取人格 System Prompt ----------
            system_prompt = await self.persona_mgr.get_system_prompt()
            logger.info(f"[ChatEngine] System prompt 长度: {len(system_prompt)}")

            # ---------- 构建工具集和工具描述 ----------
            enable_tools = self.config.get("enable_tool_calls", True)
            tool_set = None
            tool_count = 0
            if enable_tools:
                try:
                    # 诊断: 检查启用的工具数量
                    enabled_names = await self.tool_mgr.get_enabled_names()
                    logger.info(f"[ChatEngine] 已启用工具名称数: {len(enabled_names)}")

                    tool_set = await self.tool_mgr.build_active_tool_set()
                    if tool_set:
                        tool_count = (
                            len(tool_set.names()) if not tool_set.empty() else 0
                        )

                    tool_desc = await self.tool_mgr.build_tool_description_text()
                    if tool_desc:
                        system_prompt += f"\n\n## 可用工具\n\n{tool_desc}"
                except Exception as e:
                    logger.warning(f"[ChatEngine] 构建工具集失败: {e}", exc_info=True)
            else:
                logger.info("[ChatEngine] Tool Calls 已禁用")

            # ---------- Token 安全截断 ----------
            context_messages = self._trim_context_to_fit(
                context_messages, provider
            )

            # ---------- 调用 LLM (含 Tool Call 循环) ----------
            logger.info(
                f"[ChatEngine] 开始调用 LLM, 上下文: {len(context_messages) + 1} 条, "
                f"工具: {tool_count} 个, 传递原生FC: {tool_set is not None and not tool_set.empty()}"
            )
            try:
                final_response = await self._llm_call_with_tools(
                    provider=provider,
                    system_prompt=system_prompt,
                    contexts=context_messages,
                    user_msg=user_msg,
                    tool_set=tool_set,
                    event=event,
                )
            except Exception as e:
                logger.error(f"[ChatEngine] LLM 调用异常: {e}", exc_info=True)
                yield event.plain_result(f"❌ LLM 调用失败: {type(e).__name__}: {e}")
                return

            if final_response is None:
                logger.warning("[ChatEngine] LLM 返回 None")
                yield event.plain_result("❌ LLM 未返回有效响应。")
                return

            if hasattr(final_response, "role") and final_response.role == "err":
                err_text = getattr(final_response, "completion_text", "未知错误")
                logger.error(f"[ChatEngine] LLM 返回错误: {err_text}")
                yield event.plain_result(f"❌ LLM 错误: {err_text}")
                return

            # ---------- 返回结果 ----------
            response_text = final_response.completion_text or ""
            logger.info(f"[ChatEngine] LLM 响应长度: {len(response_text)}")

            if response_text:
                segments = self._split_response(response_text)
                if len(segments) <= 1:
                    yield event.plain_result(response_text)
                else:
                    logger.info(
                        f"[ChatEngine] 分段发送: {len(segments)} 段"
                    )
                    for seg_idx, segment in enumerate(segments):
                        yield event.plain_result(segment)
                        if seg_idx < len(segments) - 1:
                            delay_ms = max(0, min(
                                int(self.config.get("split_delay_ms", 800)), 5000
                            ))
                            await asyncio.sleep(delay_ms / 1000)

            if hasattr(final_response, "result_chain") and final_response.result_chain:
                for comp in final_response.result_chain.chain:
                    if isinstance(comp, Image):
                        yield event.chain_result([comp])

            # ---------- 保存上下文 ----------
            assistant_msg = {"role": "assistant", "content": response_text}
            await self.context_mgr.append_and_save(
                session_key, user_msg, assistant_msg, provider=provider
            )
            logger.info("[ChatEngine] 上下文已保存")

        except Exception as e:
            logger.error(f"[ChatEngine] 顶层异常: {e}", exc_info=True)
            try:
                yield event.plain_result(f"❌ ChatEngine 异常: {type(e).__name__}")
            except Exception:
                pass

    # ========================================================================
    # LLM 调用 + Tool Call 循环
    # ========================================================================

    async def _llm_call_with_tools(
        self,
        provider,
        system_prompt: str,
        contexts: list[dict],
        user_msg: dict,
        tool_set=None,
        max_tool_rounds: int | None = None,
        event: AstrMessageEvent = None,
    ) -> object | None:
        """调用 LLM，支持 Tool Call 循环。

        如果 LLM 返回 tool_calls，执行工具并将结果追加上下文后再次调用。
        循环直到 LLM 返回纯文本响应或达到最大轮数。
        """
        if max_tool_rounds is None:
            max_tool_rounds = int(self.config.get("max_tool_rounds", 10))

        # 构建完整上下文
        current_contexts = list(contexts) + [user_msg]
        final_response = None

        for round_idx in range(max_tool_rounds):
            # 调用 LLM
            kwargs = {
                "prompt": None,
                "contexts": current_contexts,
                "system_prompt": system_prompt,
            }

            # 传递工具集
            if tool_set and not tool_set.empty():
                kwargs["func_tool"] = tool_set

            response = await provider.text_chat(**kwargs)

            if response is None:
                return final_response

            if hasattr(response, "role") and response.role == "err":
                return response

            # 检查是否有工具调用
            tool_calls_name = getattr(response, "tools_call_name", None)
            if not tool_calls_name:
                # 纯文本响应，返回
                return response

            # 有工具调用 — 追加 assistant 消息 (含 tool_calls)
            assistant_content = response.completion_text or ""
            assistant_msg = {
                "role": "assistant",
                "content": assistant_content,
                "tool_calls": response.to_openai_tool_calls(),
            }
            current_contexts.append(assistant_msg)

            # 执行每个工具调用
            tool_calls_ids = response.tools_call_ids or []
            tool_calls_args = response.tools_call_args or []

            for i, tool_name in enumerate(tool_calls_name):
                tool_args = tool_calls_args[i] if i < len(tool_calls_args) else {}
                tool_id = tool_calls_ids[i] if i < len(tool_calls_ids) else f"call_{i}"

                # tool_args 已经是 dict，无需额外解析

                # 执行工具
                tool_result_text = await self._execute_tool(
                    tool_name, tool_args, tool_set, event=event
                )

                # 追加 tool result 消息
                tool_msg = {
                    "role": "tool",
                    "tool_call_id": tool_id,
                    "content": tool_result_text,
                }
                current_contexts.append(tool_msg)

            final_response = response
            logger.info(
                f"[ChatEngine] Tool Call 轮次 {round_idx + 1}: "
                f"调用了 {len(tool_calls_name)} 个工具"
            )

        # 达到最大轮数，做一次无工具的最终调用
        logger.warning("[ChatEngine] 达到最大工具调用轮数，进行最终调用")
        response = await provider.text_chat(
            prompt=None,
            contexts=current_contexts,
            system_prompt=system_prompt,
        )
        return response

    async def _execute_tool(
        self,
        tool_name: str,
        tool_args: dict,
        tool_set=None,
        event: AstrMessageEvent = None,
    ) -> str:
        """执行单个工具调用，返回结果字符串。

        支持三种 handler 返回类型:
        1. coroutine — 标准 async 函数，直接 await 获取结果
        2. async generator — 部分插件 handler 使用 yield，通过 async for 收集后取最后一个值
        3. 同步返回值 — 非 awaitable 非 generator 的普通返回值
        """
        try:
            tool_manager = self.context.get_llm_tool_manager()

            # 优先从筛选后的 ToolSet 中查找
            tool = None
            if tool_set:
                tool = tool_set.get_tool(tool_name)
            if not tool:
                tool = tool_manager.get_func(tool_name)
            if not tool:
                return json.dumps(
                    {"error": f"工具 '{tool_name}' 未找到"},
                    ensure_ascii=False,
                )

            # 调用工具的 handler
            if hasattr(tool, "handler") and tool.handler:
                # 插件注册的工具 (通过 @filter.llm_tool)
                # 这些 handler 通常接收 event 作为第一个参数
                # 部分插件 handler 是 async generator (使用 yield)，不能直接 await
                result = None

                # 尝试传入 event + tool_args
                try:
                    ret = tool.handler(event, **tool_args)
                except TypeError:
                    # handler 不接受 event 参数
                    ret = tool.handler(**tool_args)

                if inspect.isasyncgen(ret):
                    # async generator — 用 async for 收集所有 yield 的值
                    parts = []
                    async for item in ret:
                        if item is not None:
                            parts.append(item)
                    result = parts[-1] if parts else None
                elif inspect.isawaitable(ret):
                    result = await ret
                else:
                    result = ret
            elif hasattr(tool, "call"):
                # 内置工具 (FunctionTool 子类) — 需要完整的 ContextWrapper
                from astrbot.core.agent.run_context import ContextWrapper

                # 构建包含 event 和 context 的上下文包装器
                # 内置工具通过 agent_ctx.event 访问事件
                # 通过 agent_ctx.context 访问 AstrBot 配置
                class _AgentContext:
                    """模拟 AstrAgentContext 以传递 event 和 context"""

                    def __init__(self, ev, ctx):
                        self.event = ev
                        self.context = ctx

                agent_ctx = _AgentContext(event, self.context)
                ctx_wrapper = ContextWrapper(
                    context=agent_ctx,
                    messages=[],
                    tool_call_timeout=120,
                )
                result = await tool.call(ctx_wrapper, **tool_args)
            else:
                return json.dumps(
                    {"error": f"工具 '{tool_name}' 没有可调用的 handler"},
                    ensure_ascii=False,
                )

            if result is None:
                return "工具执行完成（无输出）"
            if isinstance(result, dict):
                return json.dumps(result, ensure_ascii=False)
            return str(result)

        except Exception as e:
            logger.error(
                f"[ChatEngine] 工具 '{tool_name}' 执行失败: {e}", exc_info=True
            )
            return json.dumps(
                {"error": f"工具执行失败: {type(e).__name__}: {str(e)}"},
                ensure_ascii=False,
            )

    def _trim_context_to_fit(
        self, messages: list[dict], provider
    ) -> list[dict]:
        """Token 安全截断：确保上下文不超过模型阈值。

        从最旧的消息开始移除，直到总量低于阈值。
        被动记录的大量消息通常在最前面，会优先被裁剪。
        """
        # 获取模型最大 token 数
        max_tokens = 0
        try:
            max_tokens = provider.provider_config.get("max_context_tokens", 0)
        except Exception:
            pass
        if max_tokens <= 0:
            try:
                max_tokens = int(
                    self.config.get("fallback_max_context_tokens", 128000)
                )
            except (ValueError, TypeError):
                max_tokens = 128000

        # 保留比例 (与 token 压缩模式共用同一个配置)
        ratio = float(self.config.get("token_threshold_ratio", 0.8))
        threshold = int(max_tokens * ratio)

        counter = TokenEstimator()
        total = counter.count_messages_tokens(messages)

        if total <= threshold:
            return messages

        # 累加最旧消息的 token 数，找到截断点
        cut_idx = 0
        for i, msg in enumerate(messages):
            total -= counter.count_messages_tokens([msg])
            cut_idx = i + 1
            if total <= threshold:
                break

        # 确保至少保留最后1条消息（防止所有消息 token 都超过阈值时返回空列表）
        if cut_idx >= len(messages):
            cut_idx = len(messages) - 1

        if cut_idx > 0:
            logger.info(
                f"[ChatEngine] Token 安全截断: 移除前 {cut_idx} 条消息 "
                f"({len(messages)} -> {len(messages) - cut_idx})"
            )

        return messages[cut_idx:]

    def _split_response(self, text: str) -> list[str]:
        """将 LLM 回复按配置的分段符号拆分。

        使用 re.split + 捕获组保留分隔符，将文本拆为多段。
        超过 max_segments 时，从尾部合并多余段落。
        未启用分段或只有一段时直接返回原文。
        """
        if not text:
            return []

        if not self.config.get("enable_split_send", False):
            return [text]

        pattern = self.config.get("split_pattern", r"[。！？\n]")
        max_segments = int(self.config.get("max_segments", 5))

        try:
            # re.split 捕获组会保留分隔符: [text, delim, text, delim, ...]
            parts = re.split(f"({pattern})", text)
        except re.error:
            logger.warning(f"[ChatEngine] 分段正则无效: {pattern}，跳过分段")
            return [text]

        # 将 text+delim 配对合并
        segments = []
        i = 0
        while i < len(parts):
            segment = parts[i]
            if i + 1 < len(parts):
                segment += parts[i + 1]  # 追加分隔符
                i += 2
            else:
                i += 1
            if segment.strip():
                segments.append(segment.strip())

        if len(segments) <= 1:
            return [text]

        # 超过最大分段数时，合并尾部段落
        if len(segments) > max_segments:
            merged = segments[: max_segments - 1]
            merged.append("".join(segments[max_segments - 1 :]))
            segments = merged

        return segments

    def _extract_image_urls(self, event: AstrMessageEvent) -> list[str]:
        """从消息事件中提取图片 URL 列表"""
        urls = []
        try:
            for comp in event.get_messages():
                if isinstance(comp, Image):
                    if hasattr(comp, "url") and comp.url:
                        urls.append(comp.url)
                    elif hasattr(comp, "file") and comp.file:
                        urls.append(comp.file)
        except Exception:
            pass
        return urls
