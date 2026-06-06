"""astrbot_plugin_chat_engine — 完全替代 AstrBot 自带聊天功能的插件。

深度劫持消息管道，独立实现:
- 上下文管理 (群聊共享 / 私聊隔离)
- 用户识别 ({{user}{昵称}({ID})}说：格式)
- 人格管理 (独立于 AstrBot)
- Tool Calls (扫描所有工具，原生 function calling)
- 上下文压缩 (轮数限制 / Token 阈值 LLM 总结)
- 记忆系统 (短期记忆 / 长期记忆 / LLM 工具主动记忆 / 自动总结)
- WebUI 管理面板 (独立 aiohttp 服务)
"""

import asyncio
import copy
import inspect
import json
import re

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Plain, Reply
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.provider.modalities import sanitize_contexts_by_modalities

from .context.manager import ChatContextManager
from .context.token_counter import TokenEstimator
from .db.engine import ChatEngineDB
from .memory.manager import MemoryManager
from .persona.manager import ChatPersonaManager
from .proactive.manager import ProactiveManager
from .tools.command_dispatcher import CommandDispatcher
from .tools.manager import ChatToolManager
from .tools.scanner import ToolScanner
from .utils.config import cfg_bool, cfg_float, cfg_int
from .web.server import ChatWebServer


@register(
    "astrbot_plugin_chat_engine",
    "车厘子小樱",
    "完全替代 AstrBot 自带聊天功能。独立实现上下文管理、用户识别、人格系统、Tool Calls、上下文压缩、记忆系统和 WebUI 管理面板。",
    "1.3.0",
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
        self.memory_mgr: MemoryManager = None
        self.proactive_mgr: ProactiveManager = None
        self.cmd_dispatcher: CommandDispatcher = None
        self.web_server: ChatWebServer = None
        self._pending_quotes: dict[str, str] = {}  # {session_key: message_id}

    # 上下文消息 ID 注入

    @staticmethod
    def _inject_msg_id_tag(msg: dict) -> dict:
        """为单条消息的 content 前注入 [msg:ID] 标记。

        仅对有 message_id 且 role 为 user/observed 的消息注入。
        返回修改后的副本，不修改原始消息。
        """
        msg_id = msg.get("message_id", "")
        role = msg.get("role", "")
        if not msg_id or role not in ("user", "observed"):
            return msg

        tag = f"[msg:{msg_id}] "
        content = msg.get("content")
        new_msg = {**msg}

        if isinstance(content, str):
            new_msg["content"] = tag + content
        elif isinstance(content, list):
            # 在第一个 text 块前插入标记
            new_parts = []
            tag_inserted = False
            for part in content:
                if (
                    not tag_inserted
                    and isinstance(part, dict)
                    and part.get("type") == "text"
                ):
                    new_parts.append(
                        {"type": "text", "text": tag + part.get("text", "")}
                    )
                    tag_inserted = True
                else:
                    new_parts.append(part)
            if not tag_inserted:
                # 没有 text 块，在最前面插入
                new_parts.insert(0, {"type": "text", "text": tag.strip()})
            new_msg["content"] = new_parts

        return new_msg

    @staticmethod
    def _strip_history_images(messages: list[dict]) -> list[dict]:
        """移除历史上下文消息中的图片，替换为 [Image] 文本标记。

        仅当前用户消息保留图片，历史消息中的图片替换为纯文本占位符，
        减少 Token 消耗同时保留"曾经发过图片"的语义信息。
        """
        result = []
        for msg in messages:
            content = msg.get("content")
            if isinstance(content, list):
                new_parts = []
                replaced = False
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "image_url":
                        replaced = True
                    else:
                        new_parts.append(part)
                if replaced:
                    has_text = any(
                        isinstance(p, dict)
                        and p.get("type") == "text"
                        and p.get("text", "").strip()
                        for p in new_parts
                    )
                    if not has_text:
                        new_parts.insert(0, {"type": "text", "text": "[Image]"})
                msg = {**msg, "content": new_parts}
            result.append(msg)
        return result

    def _enrich_context_with_ids(self, context_messages: list[dict]) -> list[dict]:
        """为上下文消息列表中的用户/被动消息注入 [msg:ID] 标记。

        返回新列表，不修改原始数据。
        """
        return [self._inject_msg_id_tag(msg) for msg in context_messages]

    # 配置读取辅助

    def _cfg_int(self, key: str, default: int) -> int:
        """安全读取 int 配置项，类型异常时回退到默认值"""
        return cfg_int(self.config, key, default)

    def _cfg_float(self, key: str, default: float) -> float:
        """安全读取 float 配置项，类型异常时回退到默认值"""
        return cfg_float(self.config, key, default)

    def _cfg_bool(self, key: str, default: bool) -> bool:
        """安全读取 bool 配置项，支持字符串和数值类型转换"""
        return cfg_bool(self.config, key, default)

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
        self.db.init_image_store(data_dir)
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
            image_store=self.db.image_store,
        )

        # 工具管理器
        tool_scanner = ToolScanner(self.context.get_llm_tool_manager())
        self.tool_mgr = ChatToolManager(tool_scanner, self.db.tool_config_repo)
        # 同步工具列表
        tools = await self.tool_mgr.refresh_tools()
        logger.info(f"[ChatEngine] 扫描到 {len(tools)} 个工具")

        # 命令分发器（管理员通过自然语言执行其他插件命令）
        if self._cfg_bool("enable_command_execution", False):
            self.cmd_dispatcher = CommandDispatcher()
            cmds = self.cmd_dispatcher.scan_commands()
            logger.info(
                f"[ChatEngine] 命令分发器已初始化, 扫描到 {len(cmds)} 个可执行命令"
            )
        else:
            self.cmd_dispatcher = None
            logger.info("[ChatEngine] 命令分发功能未启用")

        # 记忆管理器
        if self._cfg_bool("enable_memory", True):
            try:
                self.memory_mgr = MemoryManager(
                    config=self.config,
                    data_dir=data_dir,
                    # 传入 getter 函数，运行时动态获取 provider（插件加载先于 provider 初始化）
                    embedding_getter=lambda: self.context.get_all_embedding_providers(),
                    rerank_getter=lambda: getattr(
                        self.context.provider_manager, "rerank_provider_insts", []
                    ),
                    provider_getter=_get_provider,
                )
                await self.memory_mgr.initialize()
                logger.info("[ChatEngine] 记忆系统已初始化")
            except Exception as e:
                logger.warning(
                    f"[ChatEngine] 记忆系统初始化失败，记忆功能将被禁用: {e}"
                )
                self.memory_mgr = None
        else:
            logger.info("[ChatEngine] 记忆功能已禁用")

        # 主动回复管理器
        if self._cfg_bool("enable_proactive", False):
            try:
                self.proactive_mgr = ProactiveManager(
                    config=self.config,
                    data_dir=data_dir,
                    context=self.context,
                    provider_getter=_get_provider,
                    persona_mgr=self.persona_mgr,
                    context_mgr=self.context_mgr,
                    memory_mgr=self.memory_mgr,
                    clean_fn=self._clean_response,
                    split_fn=self._split_response,
                )
                await self.proactive_mgr.initialize()
                logger.info("[ChatEngine] 主动回复系统已初始化")
            except Exception as e:
                logger.warning(f"[ChatEngine] 主动回复系统初始化失败: {e}")
                self.proactive_mgr = None
        else:
            logger.info("[ChatEngine] 主动回复功能未启用")

        # 启动 WebUI
        web_port = self._cfg_int("web_port", 8765)
        self.web_server = ChatWebServer(self, port=web_port)
        await self.web_server.start()

        logger.info(f"[ChatEngine] 初始化完成, WebUI: http://localhost:{web_port}")

    async def terminate(self):
        """插件停用/重载时调用"""
        logger.info("[ChatEngine] 正在关闭...")
        if self.web_server:
            await self.web_server.stop()
        if self.memory_mgr:
            await self.memory_mgr.close()
        if self.proactive_mgr:
            await self.proactive_mgr.close()
        if self.db:
            await self.db.close()
        logger.info("[ChatEngine] 已关闭")

    # 消息拦截 — 核心处理流程

    @filter.event_message_type(filter.EventMessageType.ALL, priority=9999)
    async def handle_all_messages(self, event: AstrMessageEvent):
        """拦截所有消息，完全接管 AstrBot 的聊天流程。"""
        #  第一步: 无条件抑制 AstrBot 默认 LLM
        event.should_call_llm(True)

        try:
            #  预检查
            message_text = (event.message_str or "").strip()
            sender = event.get_sender_name() or event.get_sender_id() or "unknown"
            is_group = self.context_mgr.is_group_message(event)
            is_at = event.is_at_or_wake_command

            logger.info(
                f"[ChatEngine] 收到消息: sender={sender}, group={is_group}, "
                f"at={is_at}, text={message_text[:50]}"
            )

            # 预检查: 检测消息中是否有图片（用于纯图片消息的处理）
            has_image = any(isinstance(comp, Image) for comp in event.get_messages())

            if not message_text:
                if has_image:
                    # 纯图片消息: 作为被动消息存入上下文，不触发 LLM
                    passive_key = self.context_mgr.build_session_key(event)
                    async with self.context_mgr.get_session_lock(passive_key):
                        try:
                            image_urls = await self._extract_image_urls(event)
                            if image_urls:
                                passive_prefix = self.context_mgr.format_user_message(
                                    event
                                )
                                passive_msg = {
                                    "role": "observed",
                                    "message_id": getattr(
                                        event.message_obj, "message_id", ""
                                    ),
                                    "content": [
                                        {"type": "text", "text": passive_prefix},
                                    ]
                                    + [
                                        {"type": "image_url", "image_url": {"url": url}}
                                        for url in image_urls
                                    ],
                                }
                                await self.context_mgr.record_passive_message(
                                    passive_key, passive_msg
                                )
                                logger.info(
                                    f"[ChatEngine] 纯图片被动记录到 {passive_key}"
                                )
                        except Exception as e:
                            logger.warning(f"[ChatEngine] 纯图片被动记录失败: {e}")
                else:
                    logger.info("[ChatEngine] 空消息，跳过")
                event.should_call_llm(False)
                return

            #  命令检测: 如果有其他插件的命令处理器匹配了此消息，交给它们处理
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
                    self._cfg_bool("enable_passive_record", False)
                    and is_group
                    and message_text
                ):
                    passive_key = self.context_mgr.build_session_key(event)
                    async with self.context_mgr.get_session_lock(passive_key):
                        try:
                            passive_text = self.context_mgr.format_user_message(event)
                            passive_images = await self._extract_image_urls(event)
                            _msg_id = getattr(event.message_obj, "message_id", "")
                            if passive_images:
                                passive_msg = {
                                    "role": "observed",
                                    "message_id": _msg_id,
                                    "content": [
                                        {"type": "text", "text": passive_text},
                                    ]
                                    + [
                                        {"type": "image_url", "image_url": {"url": url}}
                                        for url in passive_images
                                    ],
                                }
                            else:
                                passive_msg = {
                                    "role": "observed",
                                    "message_id": _msg_id,
                                    "content": passive_text,
                                }
                            await self.context_mgr.record_passive_message(
                                passive_key, passive_msg
                            )
                            logger.debug(f"[ChatEngine] 被动记录消息到 {passive_key}")
                        except Exception as e:
                            logger.debug(f"[ChatEngine] 被动记录失败: {e}")

                # 主动回复: 注册会话 + 轮数计数
                if self.proactive_mgr:
                    try:
                        passive_umo = event.unified_msg_origin
                        await self.proactive_mgr.register_session(
                            passive_key, passive_umo
                        )
                        await self.proactive_mgr.on_message(passive_key)
                    except Exception as e:
                        logger.debug(f"[ChatEngine] 主动回复注册失败: {e}")

                event.should_call_llm(False)  # 恢复默认 LLM
                return

            #  获取 Provider
            provider = self.context.get_using_provider(event.unified_msg_origin)
            if not provider:
                logger.warning("[ChatEngine] 未找到 LLM Provider")
                yield event.plain_result(
                    "❌ 未配置 LLM Provider，请在 AstrBot 设置中配置。"
                )
                return

            logger.info(f"[ChatEngine] 使用 Provider: {provider.meta().id}")

            #  构建会话 Key
            session_key = self.context_mgr.build_session_key(event)
            logger.info(f"[ChatEngine] 会话 Key: {session_key}")

            # 获取会话锁，确保同一会话的消息串行处理（防止竞态条件）
            async with self.context_mgr.get_session_lock(session_key):
                #  加载上下文
                context_messages_raw = await self.context_mgr.load_context(session_key)
                # 被动记录消息使用 "observed" role 存储在数据库中，避免压缩器将每条
                # 被动消息都计为独立一轮（与 user/assistant 配对压缩逻辑冲突）。
                # 此处将其转换为 "user" role 供 LLM API 使用，同时拷贝一份避免修改原始数据。
                context_messages = [
                    {**msg, "role": "user"} if msg.get("role") == "observed" else msg
                    for msg in context_messages_raw
                ]
                logger.info(f"[ChatEngine] 已加载 {len(context_messages)} 条上下文消息")

                #  格式化用户消息
                user_text = self.context_mgr.format_user_message(event)

                image_urls = await self._extract_image_urls(event)
                if image_urls:
                    logger.info(f"[ChatEngine] 提取到 {len(image_urls)} 张图片")
                    user_msg = {
                        "role": "user",
                        "message_id": getattr(event.message_obj, "message_id", ""),
                        "content": [
                            {"type": "text", "text": user_text},
                        ]
                        + [
                            {"type": "image_url", "image_url": {"url": url}}
                            for url in image_urls
                        ],
                    }
                else:
                    user_msg = {
                        "role": "user",
                        "message_id": getattr(event.message_obj, "message_id", ""),
                        "content": user_text,
                    }

                #  获取人格 System Prompt
                system_prompt = await self.persona_mgr.get_system_prompt()
                logger.info(f"[ChatEngine] System prompt 长度: {len(system_prompt)}")

                #  注入记忆到 System Prompt
                if self.memory_mgr:
                    try:
                        memory_text = await self.memory_mgr.get_memory_prompt(
                            session_key, query=user_text
                        )
                        if memory_text:
                            system_prompt += f"\n\n{memory_text}"
                            logger.info("[ChatEngine] 注入记忆到 System Prompt")
                    except Exception as e:
                        logger.warning(f"[ChatEngine] 注入记忆失败: {e}")

                #  构建工具集和工具描述
                enable_tools = self._cfg_bool("enable_tool_calls", True)
                tool_set = None
                tool_count = 0
                if enable_tools:
                    try:
                        # 诊断: 检查启用的工具数量
                        enabled_names = await self.tool_mgr.get_enabled_names()
                        logger.info(
                            f"[ChatEngine] 已启用工具名称数: {len(enabled_names)}"
                        )

                        tool_set = await self.tool_mgr.build_active_tool_set()
                        if tool_set:
                            tool_count = (
                                len(tool_set.names()) if not tool_set.empty() else 0
                            )

                        tool_desc = await self.tool_mgr.build_tool_description_text()
                        if tool_desc:
                            system_prompt += f"\n\n## 可用工具\n\n{tool_desc}"

                        # 记忆工具使用指引
                        if self.memory_mgr:
                            system_prompt += self._build_memory_tool_guidance()
                    except Exception as e:
                        logger.warning(
                            f"[ChatEngine] 构建工具集失败: {e}", exc_info=True
                        )
                else:
                    logger.info("[ChatEngine] Tool Calls 已禁用")

                #  模态过滤
                # 根据模型能力过滤上下文和当前消息中不支持的模态内容（如图片）
                # 深拷贝确保原始消息不被修改，只影响传给 LLM 的副本，不影响保存到数据库
                modalities = await self.context_mgr.get_modalities(provider)
                llm_contexts = list(context_messages)
                llm_user_msg = user_msg
                all_messages = copy.deepcopy(list(context_messages) + [user_msg])
                sanitized, stats = sanitize_contexts_by_modalities(
                    all_messages, modalities
                )
                if stats.changed:
                    logger.info(
                        f"[ChatEngine] 模态过滤: 替换 {stats.fixed_image_blocks} 个图片块, "
                        f"{stats.fixed_audio_blocks} 个音频块"
                    )
                    llm_contexts = sanitized[:-1]
                    llm_user_msg = sanitized[-1]

                #  剥离历史上下文中的图片，仅保留当前用户消息的图片
                llm_contexts = self._strip_history_images(llm_contexts)

                #  Token 安全截断
                llm_contexts = await self._trim_context_to_fit(llm_contexts, provider)

                #  注入 [msg:ID] 标记，让 LLM 可以引用特定消息
                llm_contexts = self._enrich_context_with_ids(llm_contexts)
                llm_user_msg = self._inject_msg_id_tag(llm_user_msg)

                #  调用 LLM (含 Tool Call 循环)
                logger.info(
                    f"[ChatEngine] 开始调用 LLM, 上下文: {len(llm_contexts) + 1} 条, "
                    f"工具: {tool_count} 个, 传递原生FC: {tool_set is not None and not tool_set.empty()}"
                )
                try:
                    final_response = await self._llm_call_with_tools(
                        provider=provider,
                        system_prompt=system_prompt,
                        contexts=llm_contexts,
                        user_msg=llm_user_msg,
                        tool_set=tool_set,
                        event=event,
                    )
                except Exception as e:
                    logger.error(f"[ChatEngine] LLM 调用异常: {e}", exc_info=True)
                    yield event.plain_result(
                        f"❌ LLM 调用失败: {type(e).__name__}: {e}"
                    )
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

                #  返回结果
                response_text = final_response.completion_text or ""
                logger.info(f"[ChatEngine] LLM 响应长度: {len(response_text)}")

                # 文本清洗 (在分段发送之前)
                response_text = self._clean_response(response_text)

                # 检查是否有待发送的引用回复
                pending_quote_id = self._pending_quotes.pop(session_key, None)

                if response_text:
                    segments = self._split_response(response_text)
                    if len(segments) <= 1:
                        if pending_quote_id:
                            yield event.chain_result(
                                [Reply(id=pending_quote_id), Plain(response_text)]
                            )
                            logger.info(
                                f"[ChatEngine] 引用回复已发送: quote_msg_id={pending_quote_id}"
                            )
                        else:
                            yield event.plain_result(response_text)
                    else:
                        logger.info(f"[ChatEngine] 分段发送: {len(segments)} 段")
                        for seg_idx, segment in enumerate(segments):
                            if seg_idx == 0 and pending_quote_id:
                                yield event.chain_result(
                                    [Reply(id=pending_quote_id), Plain(segment)]
                                )
                                logger.info(
                                    f"[ChatEngine] 引用回复已发送: quote_msg_id={pending_quote_id}"
                                )
                            else:
                                yield event.plain_result(segment)
                            if seg_idx < len(segments) - 1:
                                delay_ms = max(
                                    0, min(self._cfg_int("split_delay_ms", 800), 5000)
                                )
                                await asyncio.sleep(delay_ms / 1000)
                elif pending_quote_id:
                    # LLM 返回空但设置了引用——清除 pending
                    self._pending_quotes.pop(session_key, None)

                if (
                    hasattr(final_response, "result_chain")
                    and final_response.result_chain
                ):
                    for comp in final_response.result_chain.chain:
                        if isinstance(comp, Image):
                            yield event.chain_result([comp])

                #  保存上下文
                assistant_msg = {"role": "assistant", "content": response_text}
                # 记录保存前的消息数，用于检测压缩是否发生
                pre_save_count = len(context_messages_raw)
                saved = await self.context_mgr.append_and_save(
                    session_key, user_msg, assistant_msg, provider=provider
                )
                logger.info("[ChatEngine] 上下文已保存")

                #  记忆系统: 轮数追踪 + 自动总结
                if self.memory_mgr:
                    try:
                        # 检测压缩是否发生
                        post_save_count = len(saved) if saved else pre_save_count
                        compressed = post_save_count < pre_save_count + 2

                        if compressed:
                            logger.info("[ChatEngine] 检测到上下文压缩，触发记忆总结")
                            await self.memory_mgr.on_context_compressed(
                                session_key,
                                provider,
                                self.persona_mgr,
                                self.context_mgr,
                            )

                        await self.memory_mgr.on_turn_complete(
                            session_key, provider, self.persona_mgr, self.context_mgr
                        )
                    except Exception as e:
                        logger.warning(f"[ChatEngine] 记忆轮数追踪失败: {e}")

                # 主动回复: 注册会话 + 重置轮数（机器人已回复，从零开始计数）
                if self.proactive_mgr:
                    try:
                        await self.proactive_mgr.register_session(
                            session_key,
                            event.unified_msg_origin,
                        )
                        await self.proactive_mgr.reset_round_count(session_key)
                    except Exception as e:
                        logger.debug(f"[ChatEngine] 主动回复注册失败: {e}")

        except Exception as e:
            logger.error(f"[ChatEngine] 顶层异常: {e}", exc_info=True)
            try:
                yield event.plain_result(f"❌ ChatEngine 异常: {type(e).__name__}")
            except Exception:
                pass

    # LLM 调用 + Tool Call 循环

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
            max_tool_rounds = self._cfg_int("max_tool_rounds", 10)

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
                # 记录 handler 调用前的发送状态，用于检测 handler 是否直接发送了消息
                had_sent_before = getattr(event, "_has_send_oper", False)

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
                # 检查 handler 是否通过 event.send() 直接向用户发送了消息/媒体
                has_sent_now = getattr(event, "_has_send_oper", False)
                if has_sent_now and not had_sent_before:
                    return "工具已直接向用户发送了消息或媒体内容。无需再次回复。"
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

    async def _trim_context_to_fit(self, messages: list[dict], provider) -> list[dict]:
        """Token 安全截断：确保上下文不超过模型阈值。

        从最旧的消息开始移除，直到总量低于阈值。
        被动记录的大量消息通常在最前面，会优先被裁剪。
        """
        # 获取模型最大 token 数（自动从 provider 获取并回填到配置）
        max_tokens = await self.context_mgr.get_max_context_tokens(provider)

        # 保留比例 (与 token 压缩模式共用同一个配置)
        ratio = self._cfg_float("token_threshold_ratio", 0.8)
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

        支持三种模式:
        - sentence: 按标点符号分段 (经典模式，re.finditer 匹配「文本+分隔符」)
        - newline:  仅按换行符分段，保持每行完整
        - smart:    先按换行拆行，含对话引号的行保留完整，纯叙述行按标点细分
        """
        if not text:
            return []

        if not self._cfg_bool("enable_split_send", False):
            return [text]

        pattern = self.config.get("split_pattern", r"[。！？\n]")
        max_segments = self._cfg_int("max_segments", 5)
        split_mode = self.config.get("split_mode", "sentence")

        if split_mode not in ("sentence", "newline", "smart"):
            logger.warning(
                f"[ChatEngine] 未知 split_mode: {split_mode}，回退到 sentence 模式"
            )
            split_mode = "sentence"

        # 统一提取字符类内容，避免各分支重复剥括号
        char_class = pattern
        if char_class.startswith("[") and char_class.endswith("]"):
            char_class = char_class[1:-1]

        try:
            if split_mode == "newline":
                # 仅按换行符分段，保持每行完整
                raw_segments = text.split("\n")
                segments = [s.strip() for s in raw_segments if s.strip()]
            elif split_mode == "smart":
                # 智能分段: 先按换行拆行，保护对话文本不被劈断
                # 对含引号的行保留整行，对纯叙述行再按标点细分
                # 仅包含对话引号，不含（）【】等括号
                # 括号常用于动作描写(微笑)或标注【重点】，不属于对话边界
                quote_chars = """“”‘’「」『』"""
                # 标点后跟非引号字符 (即行内标点不作为分割点)
                punct_then_nonquote = (
                    f"[^{char_class}{quote_chars}]*[{char_class}](?=[^{quote_chars}]|$)"
                )
                segments = []
                for line in text.split("\n"):
                    line = line.strip()
                    if not line:
                        continue
                    # 含引号的行视为对话，保持完整
                    if any(q in line for q in quote_chars):
                        segments.append(line)
                        continue
                    # 纯叙述行: 单次 finditer 收集片段 + 追踪尾部
                    parts = []
                    last_end = 0
                    for m in re.finditer(punct_then_nonquote, line):
                        parts.append(m.group())
                        last_end = m.end()
                    if not parts:
                        segments.append(line)
                    else:
                        tail = line[last_end:]
                        if tail.strip():
                            parts.append(tail)
                        segments.extend([p.strip() for p in parts if p.strip()])
            else:
                # sentence 模式: 按标点符号分段 (经典模式)
                # 单次 finditer 收集片段 + 追踪尾部位置
                segments = []
                last_end = 0
                for m in re.finditer(f"[^{char_class}]*[{char_class}]", text):
                    segments.append(m.group())
                    last_end = m.end()
                # 循环结束后处理尾部文本
                tail = text[last_end:]
                if tail.strip():
                    segments.append(tail)
                segments = [s.strip() for s in segments if s.strip()]
        except re.error:
            logger.warning(f"[ChatEngine] 分段正则无效: {pattern}，跳过分段")
            return [text]

        if len(segments) <= 1:
            return [text]

        # 超过最大分段数时，合并尾部段落
        if len(segments) > max_segments:
            merged = segments[: max_segments - 1]
            merged.append("".join(segments[max_segments - 1 :]))
            segments = merged

        return segments

    # Emoji 正则: 包含 Unicode Emoji 属性的字符
    _EMOJI_RE = re.compile(
        "["
        "\U0001f600-\U0001f64f"  # Emoticons
        "\U0001f300-\U0001f5ff"  # Symbols & Pictographs
        "\U0001f680-\U0001f6ff"  # Transport & Map
        "\U0001f1e0-\U0001f1ff"  # Flags
        "\U00002702-\U000027b0"  # Dingbats
        "\U0000fe00-\U0000fe0f"  # Variation Selectors
        "\U0001f900-\U0001f9ff"  # Supplemental Symbols
        "\U0001fa00-\U0001fa6f"  # Chess Symbols
        "\U0001fa70-\U0001faff"  # Symbols Extended-A
        "\U00002600-\U000026ff"  # Misc Symbols
        "\U0000200d"  # Zero Width Joiner
        "\U0000fe0f"  # Variation Selector-16
        # "\U00002b50"  # Star
        "\U00002b55"  # Circle
        "\U0000231a-\U0000231b"  # Watch/Hourglass
        "\U000023e9-\U000023f3"  # Various symbols
        "\U000023f8-\U000023fa"  # Various symbols
        "\U000025aa-\U000025ab"  # Squares
        "\U000025b6"  # Play button
        "\U000025c0"  # Reverse button
        "\U000025fb-\U000025fe"  # Squares
        "\U00002934-\U00002935"  # Arrows
        "\U00002b05-\U00002b07"  # Arrows
        "\U00002b1b-\U00002b1c"  # Squares
        "\U00003030"  # Wavy Dash
        "\U0000303d"  # Part Alternation Mark
        "\U00003297"  # Circled Ideograph Congratulation
        "\U00003299"  # Circled Ideograph Secret
        "]+",
        flags=re.UNICODE,
    )

    # 括号及其内容: 中英文括号
    _BRACKET_RE = re.compile(r"[\(（\[【][^\)）\]】]*?[\)）\]】]")

    def _clean_response(self, text: str) -> str:
        """对 LLM 回复进行文本清洗。

        根据配置可选清洗以下内容:
        - Emoji 表情符号
        - 括号块及内容: ()（）[]【】
        - 句尾多余字符 (波浪号、多余标点等)
        """
        if not text:
            return text

        if not self._cfg_bool("enable_text_clean", False):
            return text

        cleaned = text

        # 1. 去除 Emoji
        if self._cfg_bool("clean_emoji", True):
            cleaned = self._EMOJI_RE.sub("", cleaned)

        # 2. 去除括号及内容
        if self._cfg_bool("clean_brackets", True):
            cleaned = self._BRACKET_RE.sub("", cleaned)

        # 3. 清理句尾字符
        if self._cfg_bool("clean_trailing_chars", True):
            pattern = self.config.get(
                "trailing_chars_pattern", r"[~～\.\。!！?？…·•\-—_\s]+$"
            )
            try:
                cleaned = re.sub(pattern, "", cleaned, flags=re.MULTILINE)
            except re.error:
                pass  # 正则无效时跳过

        # 4. 清理多余空白: 多空格合并、行首行尾空格
        cleaned = re.sub(r"[ \t]+", " ", cleaned)  # 多空格→单空格
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)  # 多空行→双空行
        cleaned = cleaned.strip()

        return cleaned

    async def _extract_image_urls(self, event: AstrMessageEvent) -> list[str]:
        """从消息事件中提取图片并转换为 base64 data URL 列表。

        同时提取当前消息中的图片和引用消息（Reply）中的图片。
        转换为 data URL 确保所有 Provider（OpenAI / Anthropic 等）都能正确处理。
        转换失败时回退到原始 URL。
        """
        urls = []
        try:
            for comp in event.get_messages():
                if isinstance(comp, Image):
                    data_url = await self._image_to_data_url(comp)
                    if data_url:
                        urls.append(data_url)
                elif isinstance(comp, Reply) and comp.chain:
                    # 提取引用消息中的图片
                    for chain_comp in comp.chain:
                        if isinstance(chain_comp, Image):
                            data_url = await self._image_to_data_url(chain_comp)
                            if data_url:
                                urls.append(data_url)
        except Exception:
            pass
        return urls

    async def _image_to_data_url(self, comp: Image) -> str | None:
        """将 Image 组件转换为 base64 data URL，失败时回退到原始 URL。"""
        try:
            b64 = await comp.convert_to_base64()
            if b64:
                # 通过 base64 前缀检测图片格式
                if b64.startswith("/9j/"):
                    fmt = "jpeg"
                elif b64.startswith("iVBOR"):
                    fmt = "png"
                elif b64.startswith("R0lG"):
                    fmt = "gif"
                elif b64.startswith("UklG"):
                    fmt = "webp"
                elif b64.startswith("Qk0"):
                    fmt = "bmp"
                else:
                    fmt = "jpeg"
                return f"data:image/{fmt};base64,{b64}"
        except Exception as e:
            logger.warning(f"[ChatEngine] 图片转 base64 失败: {e}")
        # 回退到原始 URL
        if hasattr(comp, "url") and comp.url:
            return comp.url
        if hasattr(comp, "file") and comp.file:
            return comp.file
        return None

    # 记忆工具 — LLM Tool Call

    @staticmethod
    def _build_memory_tool_guidance() -> str:
        """构建记忆工具和主动回复工具的使用指引，注入到 system prompt 中。"""
        return """

## Tool Usage Guide

You have access to memory tools (save_memory, search_memory, update_memory, delete_memory), proactive reply tools (schedule_reply), and quote reply tool (reply_with_quote). Use them proactively:

- **save_memory**: When the user shares personal preferences, habits, important facts, or explicitly says things like "记住了", "记住", "别忘了", "记住这个". Choose type="long_term" for persistent facts (preferences, identity) or type="short_term" for temporary context (current topic, recent plans).
  - **pinned="true"**: Use for standing rules or instructions that must ALWAYS be active regardless of topic (e.g. "user wants responses under 30 chars", "always reply in a cute tone"). Pinned memories bypass semantic search and are injected every turn.
- **search_memory**: Before answering questions about the user's preferences or past discussions, search your long-term memory for relevant context.
- **update_memory**: When the user corrects or updates previously remembered information.
- **delete_memory**: When the user explicitly asks to forget something.
- **schedule_reply**: When the user asks you to remind them later, when you want to follow up on a topic, or when saying things like "一会提醒我", "过XX分钟告诉我". Also use when the conversation naturally suggests a follow-up would be welcome.
- **reply_with_quote**: When you want to reply to a specific earlier message in the conversation. Each user message is tagged with `[msg:ID]` — call `reply_with_quote(message_id)` first, then generate your reply text. It will be sent as a quoted reply on the platform. Use this when directly addressing a specific past message (e.g. answering an earlier question, confirming something the user said). Do NOT overuse — only when there is a clear reference target.

Important: Memory tools are per-session. Each memory should contain exactly one fact, concise and under 200 characters. Do NOT mention the existence of these tools to the user — just use them naturally when appropriate.
"""

    @filter.llm_tool(name="save_memory")
    async def tool_save_memory(
        self,
        event: AstrMessageEvent,
        content: str,
        type: str,
        pinned="true",
    ):
        """Save a memory. Choose type based on persistence value:
        - short_term: temporary context (current topic, recent plans, dialogue state)
        - long_term: persistent facts (user preferences, identity, key decisions, recurring patterns)

        Args:
            content(string): Memory content. One fact per memory, concise, under 200 chars.
            type(string): Memory type. Must be "short_term" or "long_term".
            pinned(string): Only for long_term. "true" = always active every turn (rules, preferences, standing instructions). "false" = retrieved by semantic relevance only. Default "true".
        """
        if not self.memory_mgr:
            return "Memory system is not available."
        session_key = self.context_mgr.build_session_key(event)
        # 解析 pinned 字符串
        is_pinned = str(pinned).lower() in ("true", "1", "yes")
        try:
            from .memory.tools import save_memory_tool

            return await save_memory_tool(
                self.memory_mgr,
                session_key,
                content,
                type,
                source="tool",
                pinned=is_pinned,
            )
        except Exception as e:
            logger.error(f"[ChatEngine] save_memory 工具失败: {e}")
            return f"Failed to save memory: {type(e).__name__}"

    @filter.llm_tool(name="search_memory")
    async def tool_search_memory(self, event: AstrMessageEvent, query: str, top_k="5"):
        """Semantic search in long-term memory.
        Short-term memory is always visible in the system prompt and does not need searching.

        Args:
            query(string): Search query text.
            top_k(string): Number of results to return. Default 5.
        """
        if not self.memory_mgr:
            return "Memory system is not available."
        session_key = self.context_mgr.build_session_key(event)
        try:
            from .memory.tools import search_memory_tool

            k = int(top_k) if isinstance(top_k, str) else (top_k or 5)
            return await search_memory_tool(self.memory_mgr, session_key, query, k)
        except Exception as e:
            logger.error(f"[ChatEngine] search_memory 工具失败: {e}")
            return f"Search failed: {type(e).__name__}"

    @filter.llm_tool(name="update_memory")
    async def tool_update_memory(self, event: AstrMessageEvent, id: str, content: str):
        """Update an existing memory. Automatically searches both short-term and long-term
        storage by ID (short-term first, then long-term).

        Args:
            id(string): Memory ID (shown in brackets in the system prompt memories section).
            content(string): New memory content. One fact, under 200 chars.
        """
        if not self.memory_mgr:
            return "Memory system is not available."
        session_key = self.context_mgr.build_session_key(event)
        try:
            from .memory.tools import update_memory_tool

            return await update_memory_tool(self.memory_mgr, session_key, id, content)
        except Exception as e:
            logger.error(f"[ChatEngine] update_memory 工具失败: {e}")
            return f"Update failed: {type(e).__name__}"

    @filter.llm_tool(name="delete_memory")
    async def tool_delete_memory(self, event: AstrMessageEvent, id: str, type: str):
        """Delete a specific memory by ID.

        Args:
            id(string): Memory ID to delete.
            type(string): Memory type. Must be "short_term" or "long_term".
        """
        if not self.memory_mgr:
            return "Memory system is not available."
        session_key = self.context_mgr.build_session_key(event)
        try:
            from .memory.tools import delete_memory_tool

            return await delete_memory_tool(self.memory_mgr, session_key, id, type)
        except Exception as e:
            logger.error(f"[ChatEngine] delete_memory 工具失败: {e}")
            return f"Delete failed: {type(e).__name__}"

    # 主动回复工具 — LLM Tool Call

    @filter.llm_tool(name="schedule_reply")
    async def tool_schedule_reply(
        self,
        event: AstrMessageEvent,
        delay_minutes: str,
        reason: str,
    ):
        """Schedule a proactive message to be sent to the user after a delay.
        Use this when you want to follow up, remind, or check in with the user later.

        Args:
            delay_minutes(string): Minutes to wait before sending. Min 1, max 1440 (24h).
            reason(string): Why you want to follow up. Helps generate the right message.
        """
        if not self.proactive_mgr:
            return "Proactive replies are not enabled."
        session_key = self.context_mgr.build_session_key(event)
        try:
            mins = int(delay_minutes)
            return await self.proactive_mgr.schedule_reply(
                session_key,
                mins,
                reason,
            )
        except Exception as e:
            logger.error(f"[ChatEngine] schedule_reply 工具失败: {e}")
            return f"Schedule failed: {type(e).__name__}"

    # 消息引用回复工具 — LLM Tool Call

    @filter.llm_tool(name="reply_with_quote")
    async def tool_reply_with_quote(
        self,
        event: AstrMessageEvent,
        message_id: str,
    ):
        """Quote (reply to) a specific historical message from the conversation.
        Use when your response directly addresses or answers a specific earlier message.
        After calling this tool, generate your reply text as normal — it will be sent as a quoted reply.

        Args:
            message_id(string): The message ID to quote. Shown as [msg:ID] tag in context messages.
        """
        session_key = self.context_mgr.build_session_key(event)

        # 验证 message_id 存在于当前会话上下文中
        try:
            messages = await self.context_mgr.load_context(session_key)
            found = any(msg.get("message_id") == message_id for msg in messages)
        except Exception:
            found = False

        if not found:
            return f"Message ID '{message_id}' not found in current session context. Available IDs can be found in [msg:ID] tags."

        self._pending_quotes[session_key] = message_id
        logger.info(
            f"[ChatEngine] 引用回复已准备: session={session_key}, "
            f"quote_msg_id={message_id}"
        )
        return "Quote reply prepared. Now generate your response text — it will be sent as a quoted reply to that message."

    # 命令执行工具 — LLM Tool Call

    @filter.llm_tool(name="list_plugins")
    async def tool_list_plugins(
        self,
        event: AstrMessageEvent,
    ):
        """List all plugins that provide bot commands, along with their command counts.
        Call this FIRST when the user wants to find or execute a bot command.

        Returns:
            A JSON list of plugins with their command counts.
        """
        if not self.cmd_dispatcher:
            return json.dumps({"error": "命令执行功能未启用。"}, ensure_ascii=False)
        plugins = self.cmd_dispatcher.list_plugins()
        return json.dumps(
            {"count": len(plugins), "plugins": plugins}, ensure_ascii=False
        )

    @filter.llm_tool(name="list_commands")
    async def tool_list_commands(
        self,
        event: AstrMessageEvent,
        plugin: str = "",
        query: str = "",
    ):
        """List available bot commands. Use after list_plugins to see commands from a specific plugin.
        Returns command names, descriptions, parameters, and permission levels.

        Args:
            plugin(string): Filter by exact plugin name (from list_plugins result).
            query(string): Optional keyword to further filter by command name or description.
        """
        if not self.cmd_dispatcher:
            return json.dumps({"error": "命令执行功能未启用。"}, ensure_ascii=False)
        result = self.cmd_dispatcher.list_commands(plugin=plugin, query=query)
        return json.dumps(
            {"count": len(result), "commands": result},
            ensure_ascii=False,
        )

    @filter.llm_tool(name="execute_command")
    async def tool_execute_command(
        self,
        event: AstrMessageEvent,
        command: str,
    ):
        """Execute a registered bot command by name.
        IMPORTANT: Only call this when the user's intent directly maps to a specific command.
        If the command is not found, STOP and inform the user. Do NOT try alternative commands.

        Args:
            command(string): The full command string to execute (without the wake prefix). e.g. "help", "provider 1", "sid".
        """
        if not self.cmd_dispatcher:
            return json.dumps({"error": "命令执行功能未启用。"}, ensure_ascii=False)

        result = await self.cmd_dispatcher.dispatch(event, command)
        return json.dumps(result, ensure_ascii=False)
