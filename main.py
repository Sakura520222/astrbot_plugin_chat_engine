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
import contextvars
import copy
import inspect
import json
import re
import time as _time
from collections.abc import Awaitable, Callable

import emoji as _emoji_lib

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Plain, Reply
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.provider.modalities import sanitize_contexts_by_modalities

from .context.manager import ChatContextManager
from .context.token_counter import TokenEstimator
from .db.engine import ChatEngineDB
from .debounce.manager import MessageDebouncer
from .memory.manager import MemoryManager
from .persona.manager import ChatPersonaManager
from .proactive.manager import ProactiveManager
from .tools.command_dispatcher import CommandDispatcher
from .tools.manager import ChatToolManager
from .tools.scanner import ToolScanner
from .utils import format_current_time, shanghai_now_iso
from .utils.config import cfg_bool, cfg_float, cfg_int
from .web.server import ChatWebServer


class _ToolCallContext:
    """Per-call context for _llm_call_with_tools, stored in ContextVar to avoid race conditions.

    AstrBot 的 EventBus.dispatch 通过 asyncio.create_task 并发处理事件，
    将 _llm_call_with_tools 的中间状态存储在实例变量上会导致不同会话互相覆盖。
    使用 ContextVar 确保每个 asyncio task 拥有独立的上下文。
    """

    __slots__ = (
        "pending_sends",
        "intermediate_msgs",
        "final_response",
        "prompt_tokens",
        "completion_tokens",
    )

    def __init__(self):
        self.pending_sends: list[tuple[str, object]] = []
        self.intermediate_msgs: list[
            dict
        ] = []  # 完整的 tool call 周期消息（assistant + tool results）
        self.final_response = None
        # 本次 _llm_call_with_tools 累计的 Token 用量（估算），整轮结束后由
        # handle_all_messages 写入会话计数，供 /stats 与 WebUI 读取。
        self.prompt_tokens: int = 0
        self.completion_tokens: int = 0


_tool_call_ctx: contextvars.ContextVar[_ToolCallContext | None] = (
    contextvars.ContextVar("_tool_call_ctx", default=None)
)


@register(
    "astrbot_plugin_chat_engine",
    "车厘子小樱",
    "完全替代 AstrBot 自带聊天功能，独立实现上下文管理、用户识别、多会话管理、人格系统、Tool Calls、上下文压缩、记忆系统和 WebUI 管理面板。",
    "1.3.4",
)
class ChatEnginePlugin(Star):
    """Chat Engine 插件主类"""

    # 会话管理命令正则（在唤醒前缀已去除后的消息文本上匹配）
    # 框架会剥离唤醒前缀(如"/")，所以需同时匹配有/无前缀的情况
    _SESSION_CMD_NEW = re.compile(r"^/?new$", re.IGNORECASE)
    _SESSION_CMD_LIST = re.compile(r"^/?list$", re.IGNORECASE)
    _SESSION_CMD_SWITCH = re.compile(r"^/?switch\s+(\d+)$", re.IGNORECASE)
    _SESSION_CMD_CLEAR = re.compile(r"^/?clear$", re.IGNORECASE)
    _SESSION_CMD_STATS = re.compile(r"^/?stats$", re.IGNORECASE)

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.db: ChatEngineDB = None
        self.context_mgr: ChatContextManager = None
        self.persona_mgr: ChatPersonaManager = None
        self.tool_mgr: ChatToolManager = None
        self.memory_mgr: MemoryManager = None
        self.proactive_mgr: ProactiveManager = None
        self.debouncer: MessageDebouncer = None
        self.cmd_dispatcher: CommandDispatcher = None
        self.web_server: ChatWebServer = None
        self._pending_quotes: dict[str, str] = {}  # {session_key: message_id}
        self._last_tool_images: list[dict] | None = (
            None  # 当前工具调用产生的图片（单次执行生命周期）
        )
        self._group_info_cache: dict[str, tuple[float, str, str, bool]] = {}
        # 群聊信息缓存: session_key -> (timestamp, group_name, bot_card, is_fallback), TTL=300s

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

    async def _build_system_prompt_prefix(
        self, event: AstrMessageEvent, session_key: str = ""
    ) -> str:
        """构建 System Prompt 环境信息前缀（时间、群聊信息等）。

        群聊信息（群名、Bot 群昵称）通过平台 API 获取，按会话缓存 5 分钟。
        API 调用失败时缓存 fallback 值（短 TTL），避免持续重试。
        """
        parts = []

        # 注入当前时间
        parts.append(f"当前时间: {format_current_time()}")

        # 群聊额外信息
        is_group = self.context_mgr.is_group_message(event)
        if is_group:
            try:
                if not session_key:
                    session_key = self.context_mgr.build_session_key(event)
                self_id = event.get_self_id()
                cache_ttl = 300  # 5 分钟

                # 清理过期缓存条目，防止无限增长
                if len(self._group_info_cache) > 500:
                    now_ts = _time.time()
                    expired_keys = [
                        k
                        for k, (ts, _, _, is_fb) in self._group_info_cache.items()
                        if now_ts - ts >= (60 if is_fb else cache_ttl)
                    ]
                    for k in expired_keys:
                        del self._group_info_cache[k]

                # 检查缓存
                cache_hit = False
                cached_group_name = ""
                cached_bot_card = ""
                if session_key in self._group_info_cache:
                    ts, cached_group_name, cached_bot_card, is_fallback = (
                        self._group_info_cache[session_key]
                    )
                    effective_ttl = 60 if is_fallback else cache_ttl
                    if _time.time() - ts < effective_ttl:
                        cache_hit = True

                if not cache_hit:
                    # 优先从事件直接获取群名（无需 API 调用）
                    group = getattr(event.message_obj, "group", None)
                    fallback_group_name = (
                        getattr(group, "group_name", "") if group else ""
                    )
                    cached_group_name = fallback_group_name
                    cached_bot_card = ""

                    # 调用平台 API 获取完整群信息（含 Bot 群昵称）
                    try:
                        group_info = await event.get_group()
                        if group_info:
                            if group_info.group_name:
                                cached_group_name = group_info.group_name
                            if group_info.members and self_id:
                                self_id_str = str(self_id)
                                for member in group_info.members:
                                    # str() 统一类型：OneBot API 的 user_id 可能为 int
                                    if str(member.user_id) == self_id_str:
                                        cached_bot_card = (
                                            member.card
                                            if getattr(member, "card", None)
                                            else (getattr(member, "nickname", "") or "")
                                        )
                                        break
                    except Exception:
                        # API 调用失败：用 fallback 值短 TTL 缓存，避免持续重试
                        self._group_info_cache[session_key] = (
                            _time.time(),
                            fallback_group_name,
                            "",
                            True,  # 标记为 fallback 条目（短 TTL）
                        )
                        if fallback_group_name:
                            parts.append(f"群名: {fallback_group_name}")
                        raise

                    # 仅在 API 调用成功后缓存（完整 TTL）
                    self._group_info_cache[session_key] = (
                        _time.time(),
                        cached_group_name,
                        cached_bot_card,
                        False,  # 正常条目
                    )

                if cached_group_name:
                    parts.append(f"群名: {cached_group_name}")
                if cached_bot_card:
                    parts.append(f"你在群里的昵称: {cached_bot_card}")
            except Exception as e:
                logger.warning(f"[ChatEngine] 构建群聊环境信息失败: {e}")

        return "\n".join(parts)

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

        # 消息抖动管理器
        if self._cfg_bool("enable_message_debounce", False):
            try:
                self.debouncer = MessageDebouncer(
                    config=self.config,
                    process_fn=self._process_debounced_messages,
                )
                logger.info("[ChatEngine] 消息抖动已启用")
            except Exception as e:
                logger.warning(f"[ChatEngine] 消息抖动初始化失败: {e}")
                self.debouncer = None
        else:
            logger.info("[ChatEngine] 消息抖动未启用")

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
        if self.debouncer:
            await self.debouncer.close()
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

            # 会话管理命令拦截: /new, /list, /switch N
            # 在框架命令检测之前处理，拦截框架原有的 /new 等命令
            if is_at or not is_group:
                session_cmd_result = await self._try_handle_session_cmd(
                    event, message_text
                )
                if session_cmd_result is not None:
                    event.should_call_llm(False)
                    yield event.plain_result(session_cmd_result)
                    event.stop_event()
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
                # 被动消息优先并入活跃防抖缓冲（不重置计时器、不计满载）。
                # 仅当本会话已有激活消息开启的活跃缓冲时才并入；否则回退到被动记录。
                if (
                    self.debouncer
                    and self.debouncer.should_debounce(is_group)
                    and self._cfg_bool("debounce_absorb_passive", True)
                    and is_group
                    and message_text
                ):
                    _passive_key = self.context_mgr.build_session_key(event)
                    _passive_data = {
                        "user_text": self.context_mgr.format_user_message(event),
                        "images": await self._extract_image_urls(event),
                        "message_id": getattr(
                            event.message_obj, "message_id", ""
                        ),
                        "umo": event.unified_msg_origin,
                        "event": event,
                        "is_active": False,
                    }
                    if self.debouncer.try_add_passive(_passive_key, _passive_data):
                        logger.info(
                            f"[Debounce] 被动消息已并入: {_passive_key}"
                        )
                        event.should_call_llm(False)
                        return

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

            # 消息抖动: 收集短时间内的多条消息，合并后一次性处理
            if self.debouncer and self.debouncer.should_debounce(is_group):
                _debounce_key = self.context_mgr.build_session_key(event)
                # 提前提取易失数据（图片 URL 等可能过期），
                # 仅保留 event 引用供工具系统使用（handler 需要 event 参数）
                _debounce_text = self.context_mgr.format_user_message(event)
                _debounce_images = await self._extract_image_urls(event)
                _debounce_data = {
                    "user_text": _debounce_text,
                    "images": _debounce_images,
                    "message_id": getattr(event.message_obj, "message_id", ""),
                    "umo": event.unified_msg_origin,
                    "event": event,
                    "is_active": True,
                }
                _force = await self.debouncer.add_message(_debounce_key, _debounce_data)

                # 主动回复: 注册会话 + 轮数计数
                if self.proactive_mgr:
                    try:
                        await self.proactive_mgr.register_session(
                            _debounce_key, _debounce_data["umo"]
                        )
                        await self.proactive_mgr.on_message(_debounce_key)
                    except Exception as e:
                        logger.debug(f"[ChatEngine] 主动回复注册失败: {e}")

                if _force:
                    await self.debouncer.force_flush(_debounce_key)
                logger.info(
                    f"[Debounce] 消息已缓冲: {_debounce_key}, 强制刷新={_force}"
                )
                event.should_call_llm(False)
                return

            #  构建会话 Key
            session_key = self.context_mgr.build_session_key(event)
            logger.info(f"[ChatEngine] 会话 Key: {session_key}")

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

            # 收集所有需要 yield 的结果
            _yield_queue = []

            async def _collect_text(seg_text, is_first, quote_id, **_kw):
                if is_first and quote_id:
                    _yield_queue.append(
                        event.chain_result([Reply(id=quote_id), Plain(seg_text)])
                    )
                    logger.info(f"[ChatEngine] 引用回复已发送: quote_msg_id={quote_id}")
                else:
                    _yield_queue.append(event.plain_result(seg_text))

            async def _collect_chain(components):
                _yield_queue.append(event.chain_result(components))

            async def _collect_image(comp):
                _yield_queue.append(event.chain_result([comp]))

            response_text = await self._execute_llm_turn(
                umo=event.unified_msg_origin,
                session_key=session_key,
                user_msg=user_msg,
                event=event,
                user_text=user_text,
                log_tag="[ChatEngine]",
                on_text_segment=_collect_text,
                on_chain_segment=_collect_chain,
                on_image_component=_collect_image,
            )

            # 处理错误情况
            if response_text is None:
                # 区分 LLM 异常和 LLM 返回 None/err
                # _execute_llm_turn 返回 None 可能是异常或空响应
                # 检查是否有回调已发送了错误信息（通过 _yield_queue）
                if not _yield_queue:
                    yield event.plain_result("❌ LLM 调用失败或未返回有效响应。")
            else:
                # 分段发送延迟
                for _idx, _result in enumerate(_yield_queue):
                    yield _result
                    if _idx < len(_yield_queue) - 1:
                        delay_ms = max(
                            0,
                            min(
                                self._cfg_int("split_delay_ms", 800),
                                5000,
                            ),
                        )
                        if delay_ms > 0:
                            await asyncio.sleep(delay_ms / 1000)

        except Exception as e:
            logger.error(f"[ChatEngine] 顶层异常: {e}", exc_info=True)
            try:
                yield event.plain_result(f"❌ ChatEngine 异常: {type(e).__name__}")
            except Exception:
                pass

    # 共享 LLM 处理流程

    async def _execute_llm_turn(
        self,
        *,
        umo: str,
        session_key: str,
        user_msg: dict,
        event: AstrMessageEvent,
        user_text: str,
        log_tag: str = "[ChatEngine]",
        on_text_segment: Callable[..., Awaitable[None]] | None = None,
        on_chain_segment: Callable[[list], Awaitable[None]] | None = None,
        on_image_component: Callable[[object], Awaitable[None]] | None = None,
    ) -> str | None:
        """执行一轮完整的 LLM 调用流程（上下文加载 → Prompt 构建 → LLM 调用 → 结果处理 → 上下文保存）。

        被 handle_all_messages 和 _process_debounced_messages 共享调用，
        避免两处维护 ~200 行几乎相同的代码。

        内部通过 :meth:`_get_chat_providers_with_fallback` 解析 Provider 候选列表
        （主 Provider + ``fallback_chat_models``）。主 Provider 调用失败
        （异常 / err / None）且尚未向回调发送任何内容时，自动依次尝试下一个候选；
        一旦已向回调交出内容（``_sent_any``），不再切换，避免重复发送。

        Args:
            umo: unified_message_origin，用于解析 Provider 候选列表
            session_key: 会话标识
            user_msg: 构建好的用户消息 dict
            event: 消息事件（用于环境信息构建和工具调用）
            user_text: 用户文本（用于记忆查询）
            log_tag: 日志前缀
            on_text_segment: ``async (text, is_first, pending_quote_id, *, has_more=False) -> None``
            on_chain_segment: ``async (components) -> None``
            on_image_component: ``async (component) -> None``

        Returns:
            LLM 响应文本，或 None 表示失败/无需进一步处理。
        """
        async with self.context_mgr.get_session_lock(session_key):
            # 加载上下文
            context_messages_raw = await self.context_mgr.load_context(session_key)
            context_messages = [
                {**msg, "role": "user"} if msg.get("role") == "observed" else msg
                for msg in context_messages_raw
            ]
            logger.info(f"{log_tag} 已加载 {len(context_messages)} 条上下文消息")

            # 构建系统 Prompt
            system_prompt = await self.persona_mgr.get_system_prompt()
            if log_tag == "[ChatEngine]":
                logger.info(f"{log_tag} System prompt 长度: {len(system_prompt)}")

            context_prefix = await self._build_system_prompt_prefix(
                event, session_key=session_key
            )
            if context_prefix:
                system_prompt = context_prefix + "\n\n" + system_prompt

            # 注入记忆
            if self.memory_mgr:
                try:
                    memory_text = await self.memory_mgr.get_memory_prompt(
                        session_key, query=user_text
                    )
                    if memory_text:
                        system_prompt += f"\n\n{memory_text}"
                        logger.info(f"{log_tag} 注入记忆到 System Prompt")
                except Exception as e:
                    logger.warning(f"{log_tag} 注入记忆失败: {e}")

            # 构建工具集
            enable_tools = self._cfg_bool("enable_tool_calls", True)
            tool_set = None
            tool_count = 0
            if enable_tools:
                try:
                    if log_tag == "[ChatEngine]":
                        enabled_names = await self.tool_mgr.get_enabled_names()
                        logger.info(f"{log_tag} 已启用工具名称数: {len(enabled_names)}")

                    tool_set = await self.tool_mgr.build_active_tool_set()
                    if tool_set:
                        tool_count = (
                            len(tool_set.names()) if not tool_set.empty() else 0
                        )

                    tool_desc = await self.tool_mgr.build_tool_description_text()
                    if tool_desc:
                        system_prompt += f"\n\n## 可用工具\n\n{tool_desc}"

                    if self.memory_mgr:
                        system_prompt += self._build_memory_tool_guidance()
                except Exception as e:
                    logger.warning(f"{log_tag} 构建工具集失败: {e}", exc_info=True)
            else:
                if log_tag == "[ChatEngine]":
                    logger.info(f"{log_tag} Tool Calls 已禁用")

            # === Provider 候选列表 + 失败重试 ===
            providers = self._get_chat_providers_with_fallback(umo, log_tag=log_tag)
            if not providers:
                logger.warning(f"{log_tag} 未找到 LLM Provider")
                return None

            _sent_any = False  # 是否已将任何内容交给回调（True 后不再切换 provider）
            final_response = None
            _llm_ctx = None
            used_provider = None
            last_err: object = None

            for _p_idx, provider in enumerate(providers):
                _provider_id = (
                    provider.meta().id if hasattr(provider, "meta") else f"#{_p_idx}"
                )
                try:
                    # 模态过滤（不同 Provider 可能支持不同模态，每轮重算）
                    modalities = await self.context_mgr.get_modalities(provider)
                    llm_contexts = list(context_messages)
                    llm_user_msg = user_msg
                    all_messages = copy.deepcopy(list(context_messages) + [user_msg])
                    sanitized, stats = sanitize_contexts_by_modalities(
                        all_messages, modalities
                    )
                    if stats.changed:
                        if log_tag == "[ChatEngine]":
                            logger.info(
                                f"{log_tag} 模态过滤: 替换 {stats.fixed_image_blocks} 个图片块, "
                                f"{stats.fixed_audio_blocks} 个音频块"
                            )
                        llm_contexts = sanitized[:-1]
                        llm_user_msg = sanitized[-1]

                    # 剥离历史图片
                    llm_contexts = self._strip_history_images(llm_contexts)

                    # Token 安全截断
                    llm_contexts = await self._trim_context_to_fit(llm_contexts, provider)

                    # 注入 [msg:ID] 标记
                    llm_contexts = self._enrich_context_with_ids(llm_contexts)
                    llm_user_msg = self._inject_msg_id_tag(llm_user_msg)

                    logger.info(
                        f"{log_tag} 开始调用 LLM [{_p_idx + 1}/{len(providers)}] "
                        f"{_provider_id}, 上下文: {len(llm_contexts) + 1} 条, "
                        f"工具: {tool_count} 个"
                    )

                    # 每轮重置 tool_call 上下文，避免上轮失败的中间状态污染
                    _tool_call_ctx.set(_ToolCallContext())

                    async for _st, _sd in self._llm_call_with_tools(
                        provider=provider,
                        system_prompt=system_prompt,
                        contexts=llm_contexts,
                        user_msg=llm_user_msg,
                        tool_set=tool_set,
                        event=event,
                    ):
                        # 通过回调发送中间结果；一旦交出内容就置位 _sent_any
                        if _st == "text" and on_text_segment:
                            _sent_any = True
                            await on_text_segment(_sd, False, None)
                        elif _st == "chain" and on_chain_segment:
                            _sent_any = True
                            await on_chain_segment(_sd)

                    _llm_ctx = _tool_call_ctx.get()
                    final_response = _llm_ctx.final_response if _llm_ctx else None

                    # 判定本轮结果：None / err 视为失败
                    if final_response is None:
                        last_err = "LLM 返回 None"
                        if _sent_any:
                            break
                        logger.warning(
                            f"{log_tag} Provider[{_p_idx}] {_provider_id} 返回 None，尝试下一个"
                        )
                        continue

                    if hasattr(final_response, "role") and final_response.role == "err":
                        last_err = (
                            f"LLM 错误: "
                            f"{getattr(final_response, 'completion_text', '未知')}"
                        )
                        if _sent_any:
                            break
                        logger.warning(
                            f"{log_tag} Provider[{_p_idx}] {_provider_id} 返回错误，尝试下一个"
                        )
                        continue

                    # 成功
                    used_provider = provider
                    break

                except Exception as e:
                    last_err = e
                    if _sent_any:
                        # 已向回调交出内容，不能安全重试——上抛让外层感知
                        logger.error(
                            f"{log_tag} 已发送内容后调用异常，上抛: {e}",
                            exc_info=True,
                        )
                        raise
                    logger.warning(
                        f"{log_tag} Provider[{_p_idx}] {_provider_id} 调用异常: "
                        f"{e}，尝试下一个",
                        exc_info=True,
                    )
                    continue
            else:
                # for-else: 所有候选均失败
                logger.error(f"{log_tag} 所有 Provider 均失败: {last_err}")
                return None

            # 到这里：要么成功，要么 _sent_any 后中途失败（final_response 可能无效）
            if final_response is None:
                logger.warning(f"{log_tag} 已发送部分内容但未获得最终响应")
                return None

            if hasattr(final_response, "role") and final_response.role == "err":
                err_text = getattr(final_response, "completion_text", "未知错误")
                logger.error(f"{log_tag} LLM 错误: {err_text}")
                return None

            # 处理响应文本
            response_text = final_response.completion_text or ""
            if log_tag == "[ChatEngine]":
                logger.info(f"{log_tag} LLM 响应长度: {len(response_text)}")

            response_text = self._clean_response(response_text)

            # 检查引用回复
            pending_quote_id = self._pending_quotes.pop(session_key, None)

            if response_text:
                # 发送分段文本
                _first_seg = True
                _segs = list(self._iter_text_segments_no_delay(response_text))
                for _si, _seg in enumerate(_segs):
                    if on_text_segment:
                        await on_text_segment(
                            _seg,
                            _first_seg,
                            pending_quote_id if _first_seg else None,
                            has_more=(_si < len(_segs) - 1),
                        )
                    _first_seg = False
            elif pending_quote_id:
                # LLM 返回空但设置了引用——清除 pending
                self._pending_quotes.pop(session_key, None)

            # 发送图片结果
            if hasattr(final_response, "result_chain") and final_response.result_chain:
                for comp in final_response.result_chain.chain:
                    if isinstance(comp, Image):
                        if on_image_component:
                            await on_image_component(comp)

            # 保存上下文
            all_new_msgs = [user_msg]
            if _llm_ctx:
                all_new_msgs.extend(_llm_ctx.intermediate_msgs)
            all_new_msgs.append({"role": "assistant", "content": response_text})

            pre_save_count = len(context_messages_raw)
            saved = await self.context_mgr._load_compress_save(
                session_key, all_new_msgs, provider=used_provider
            )
            logger.info(f"{log_tag} 上下文已保存")

            # Token 用量
            if _llm_ctx and (_llm_ctx.prompt_tokens or _llm_ctx.completion_tokens):
                try:
                    await self.context_mgr.repo.add_token_usage(
                        session_key,
                        _llm_ctx.prompt_tokens,
                        _llm_ctx.completion_tokens,
                    )
                except Exception as e:
                    logger.warning(f"{log_tag} Token 用量累计失败: {e}")

            # 记忆追踪
            if self.memory_mgr:
                try:
                    post_save_count = len(saved) if saved else pre_save_count
                    compressed = post_save_count < pre_save_count + 2

                    if compressed:
                        logger.info(f"{log_tag} 检测到上下文压缩，触发记忆总结")
                        await self.memory_mgr.on_context_compressed(
                            session_key,
                            used_provider,
                            self.persona_mgr,
                            self.context_mgr,
                        )

                    await self.memory_mgr.on_turn_complete(
                        session_key,
                        used_provider,
                        self.persona_mgr,
                        self.context_mgr,
                    )
                except Exception as e:
                    logger.warning(f"{log_tag} 记忆追踪失败: {e}")

            # 主动回复管理
            if self.proactive_mgr:
                try:
                    await self.proactive_mgr.register_session(
                        session_key, event.unified_msg_origin
                    )
                    await self.proactive_mgr.reset_round_count(session_key)
                except Exception as e:
                    logger.debug(f"{log_tag} 主动回复管理失败: {e}")

        return response_text

    # LLM 调用 + Tool Call 循环

    def _accumulate_token_usage(
        self, ctx: _ToolCallContext, system_prompt: str, contexts: list[dict], response
    ) -> None:
        """估算单次 LLM 调用的 Token 用量并累加到 ctx。

        prompt 端 = system_prompt + 实际发送的 contexts；
        completion 端 = 回复文本 + tool_calls JSON。
        None/err 响应不计入。工具 schema 不计入（非文本）。
        """
        if response is None:
            return
        if hasattr(response, "role") and response.role == "err":
            return
        tool_calls = (
            response.to_openai_tool_calls()
            if getattr(response, "tools_call_name", None)
            else None
        )
        estimator = self.context_mgr.token_counter
        ctx.prompt_tokens += estimator.estimate_prompt(system_prompt, contexts)
        ctx.completion_tokens += estimator.estimate_completion(
            getattr(response, "completion_text", "") or "", tool_calls
        )

    async def _llm_call_with_tools(
        self,
        provider,
        system_prompt: str,
        contexts: list[dict],
        user_msg: dict,
        tool_set=None,
        max_tool_rounds: int | None = None,
        event: AstrMessageEvent = None,
    ):
        """调用 LLM，支持 Tool Call 循环。

        异步生成器：在工具调用期间立即 yield 中间结果给用户。
        最终 LLM 响应存储在 _tool_call_ctx (ContextVar) 中。

        yield: ("text", str) | ("chain", list[component])
        """
        if max_tool_rounds is None:
            max_tool_rounds = self._cfg_int("max_tool_rounds", 10)

        ctx = _ToolCallContext()
        _tool_call_ctx.set(ctx)

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
                ctx.final_response = final_response
                return

            if hasattr(response, "role") and response.role == "err":
                ctx.final_response = response
                return

            # 累计本次调用的 Token 用量（估算）
            self._accumulate_token_usage(ctx, system_prompt, current_contexts, response)

            # 检查是否有工具调用
            tool_calls_name = getattr(response, "tools_call_name", None)
            if not tool_calls_name:
                # 纯文本响应，存储并返回
                ctx.final_response = response
                return

            # 有工具调用 — 追加 assistant 消息 (含 tool_calls)
            assistant_content = response.completion_text or ""

            # 立即发送中间文本（经清洗和分段处理）
            if assistant_content.strip():
                _t = self._clean_response(assistant_content)
                if _t:
                    async for _seg in self._iter_text_segments(_t):
                        yield ("text", _seg)

            assistant_msg = {
                "role": "assistant",
                "content": assistant_content,
                "tool_calls": response.to_openai_tool_calls(),
            }
            current_contexts.append(assistant_msg)
            ctx.intermediate_msgs.append(assistant_msg)

            # 执行每个工具调用
            tool_calls_ids = response.tools_call_ids or []
            tool_calls_args = response.tools_call_args or []

            for i, tool_name in enumerate(tool_calls_name):
                tool_args = tool_calls_args[i] if i < len(tool_calls_args) else {}
                tool_id = tool_calls_ids[i] if i < len(tool_calls_ids) else f"call_{i}"

                # tool_args 已经是 dict，无需额外解析

                # 每次工具调用前清空上一次工具的图片数据
                self._last_tool_images = None

                # 执行工具
                tool_result_text = await self._execute_tool(
                    tool_name, tool_args, tool_set, event=event
                )

                # 构造 tool result：若工具产生了图片，直接嵌入 content 中
                if self._last_tool_images:
                    img_count = len(self._last_tool_images)
                    tool_content: str | list[dict] = [
                        {"type": "text", "text": tool_result_text},
                    ] + self._last_tool_images
                    self._last_tool_images = None
                    logger.info(f"[ChatEngine] 注入 {img_count} 张图片到 tool result")
                else:
                    tool_content = tool_result_text

                tool_msg = {
                    "role": "tool",
                    "tool_call_id": tool_id,
                    "content": tool_content,
                }
                current_contexts.append(tool_msg)
                ctx.intermediate_msgs.append(tool_msg)

            # 立即发送工具产生的直接发送结果（如 execute_command 的命令输出）
            if ctx.pending_sends:
                for _ps in ctx.pending_sends:
                    yield _ps
                ctx.pending_sends = []

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
        self._accumulate_token_usage(ctx, system_prompt, current_contexts, response)
        ctx.final_response = response

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

    # 消息抖动 — 合并处理

    def _merge_debounced_messages(self, messages: list[dict]) -> tuple[str, list[str]]:
        """合并缓冲消息。

        Returns:
            (合并后文本, 合并后图片列表)
        """
        separator = str(self.config.get("debounce_separator", "\n"))
        # 处理转义序列: AstrBot 配置面板输入的 \n 是字面两个字符
        separator = separator.replace("\\n", "\n").replace("\\t", "\t")
        merge_mode = self.config.get("debounce_merge_mode", "concat")

        texts = [m["user_text"] for m in messages]
        images: list[str] = []
        for m in messages:
            images.extend(m.get("images", []))

        if merge_mode == "numbered" and len(texts) > 1:
            numbered = [f"[{i + 1}] {t}" for i, t in enumerate(texts)]
            combined_text = separator.join(numbered)
        else:
            combined_text = separator.join(texts)

        return combined_text, images

    async def _process_debounced_messages(
        self, session_key: str, messages: list[dict]
    ) -> None:
        """处理抖动收集的消息 — 合并后调用 LLM 并通过 send_message 发送回复。

        被 MessageDebouncer 的计时器回调和 force_flush 调用，
        运行在独立的 asyncio.Task 中，不走 handle_all_messages 的 yield 流程。
        """
        from astrbot.core.message.message_event_result import MessageChain

        if not messages:
            return

        # 回复目标取最后一条激活消息：被动消息（含 bot 自身回复、他人发言）
        # 并入后可能排在末尾，用它做回复目标会引用错对象、回复目标错位
        target = next(
            (m for m in reversed(messages) if m.get("is_active", True)),
            messages[-1],
        )
        umo = target["umo"]
        last_event = target["event"]

        try:
            # 1. 合并消息
            combined_text, combined_images = self._merge_debounced_messages(messages)

            # 2. 构建合并后的 user_msg
            if combined_images:
                user_msg = {
                    "role": "user",
                    "message_id": target["message_id"],
                    "content": [
                        {"type": "text", "text": combined_text},
                    ]
                    + [
                        {"type": "image_url", "image_url": {"url": url}}
                        for url in combined_images
                    ],
                }
            else:
                user_msg = {
                    "role": "user",
                    "message_id": target["message_id"],
                    "content": combined_text,
                }

            # 3. 回调: 通过 send_message 发送 LLM 输出
            async def _send_text(seg_text, is_first, quote_id, has_more=False):
                if is_first and quote_id:
                    chain = MessageChain([Reply(id=quote_id), Plain(seg_text)])
                    logger.info(f"[Debounce] 引用回复: quote_msg_id={quote_id}")
                else:
                    chain = MessageChain([Plain(seg_text)])
                sent = await self.context.send_message(umo, chain)
                if not sent:
                    logger.warning(f"[Debounce] 发送失败: {session_key}")
                # 分段发送延迟（仅在有后续段时执行）
                if has_more:
                    delay_ms = max(0, min(self._cfg_int("split_delay_ms", 800), 5000))
                    if delay_ms > 0:
                        await asyncio.sleep(delay_ms / 1000)

            async def _send_chain(components):
                await self.context.send_message(umo, MessageChain(components))

            async def _send_image(comp):
                await self.context.send_message(umo, MessageChain([comp]))

            # 4. 调用共享 LLM 处理流程（Provider 选择与失败重试由其内部处理）
            response_text = await self._execute_llm_turn(
                umo=umo,
                session_key=session_key,
                user_msg=user_msg,
                event=last_event,
                user_text=combined_text,
                log_tag="[Debounce]",
                on_text_segment=_send_text,
                on_chain_segment=_send_chain,
                on_image_component=_send_image,
            )

            if response_text is not None:
                logger.info(f"[Debounce] 处理完成: {session_key}")

        except Exception as e:
            logger.error(f"[Debounce] 顶层异常: {e}", exc_info=True)

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

    def _get_provider_with_fallback(
        self, umo: str = None, log_tag: str = "[ChatEngine]"
    ) -> object | None:
        """获取 Provider，失败时按 AstrBot 配置的 fallback_chat_models 回退。

        回退顺序:
        1. 当前会话绑定的 Provider（通过 context.get_using_provider）
        2. AstrBot provider_settings.fallback_chat_models 中配置的回退模型列表

        Args:
            umo: unified_message_origin
            log_tag: 日志前缀
        """
        from astrbot.core.provider import Provider as ProviderType

        # 首选: 当前会话的 Provider
        provider = self.context.get_using_provider(umo)
        if provider:
            return provider

        # 回退: 读取 AstrBot 配置中的 fallback_chat_models
        try:
            config = self.context.get_config(umo)
            provider_settings = getattr(config, "provider_settings", None) or {}
            fallback_ids = provider_settings.get("fallback_chat_models", [])
            if isinstance(fallback_ids, list) and fallback_ids:
                for fallback_id in fallback_ids:
                    if not isinstance(fallback_id, str) or not fallback_id:
                        continue
                    fallback_provider = self.context.get_provider_by_id(fallback_id)
                    if fallback_provider is not None and isinstance(
                        fallback_provider, ProviderType
                    ):
                        logger.warning(
                            f"{log_tag} 主 Provider 不可用，回退到: {fallback_id}"
                        )
                        return fallback_provider
                    else:
                        logger.warning(
                            f"{log_tag} 回退 Provider `{fallback_id}` 未找到或类型不对，跳过"
                        )
        except Exception as e:
            logger.warning(f"{log_tag} Provider 回退查找失败: {e}")

        return None

    def _get_chat_providers_with_fallback(
        self, umo: str = None, log_tag: str = "[ChatEngine]"
    ) -> list:
        """获取 chat Provider 候选列表（主 Provider 在前，回退 Provider 随后）。

        用于 :meth:`_execute_llm_turn` 的失败重试：主 Provider 调用失败
        （异常 / err / None）且尚未向用户发送任何内容时，依次尝试列表中的后续 Provider。

        与 :meth:`_get_provider_with_fallback` 的区别：
        - 后者返回单个 Provider（主 Provider 不存在时挑第一个可用 fallback），
          适合"只选不重试"的场景（如会话标题生成）。
        - 本方法返回完整候选列表，由调用方按序尝试。

        Args:
            umo: unified_message_origin
            log_tag: 日志前缀

        Returns:
            候选 Provider 列表（按 id 去重、过滤 None）。可能为空。
        """
        from astrbot.core.provider import Provider as ProviderType

        candidates: list = []
        seen_ids: set[int] = set()

        def _add(p) -> None:
            if p is None or not isinstance(p, ProviderType):
                return
            if id(p) in seen_ids:
                return
            seen_ids.add(id(p))
            candidates.append(p)

        # 1. 主 Provider
        _add(self.context.get_using_provider(umo))

        # 2. fallback_chat_models 列表
        try:
            config = self.context.get_config(umo)
            provider_settings = getattr(config, "provider_settings", None) or {}
            fallback_ids = provider_settings.get("fallback_chat_models", [])
            if isinstance(fallback_ids, list) and fallback_ids:
                for fallback_id in fallback_ids:
                    if not isinstance(fallback_id, str) or not fallback_id:
                        continue
                    _add(self.context.get_provider_by_id(fallback_id))
        except Exception as e:
            logger.warning(f"{log_tag} Provider 回退列表查找失败: {e}")

        return candidates

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
            merged.append("\n".join(segments[max_segments - 1 :]))
            segments = merged

        return segments

    def _iter_text_segments_no_delay(self, text: str):
        """将文本分段并迭代返回，不添加段间延迟。

        延迟由调用方根据回调参数 ``has_more`` 自行控制，
        避免在无法预知总段数时最后一段后多等待。
        """
        segments = self._split_response(text)
        if not segments:
            return
        if len(segments) > 1:
            logger.info(f"[ChatEngine] 分段发送: {len(segments)} 段")
        yield from segments

    async def _iter_text_segments(self, text: str):
        """将文本分段并异步迭代返回，段间自动添加延迟。

        用于 _llm_call_with_tools 的中间文本输出。
        """
        segments = self._split_response(text)
        if not segments:
            return
        if len(segments) > 1:
            logger.info(f"[ChatEngine] 分段发送: {len(segments)} 段")
        for i, seg in enumerate(segments):
            yield seg
            if i < len(segments) - 1:
                await asyncio.sleep(
                    max(0, min(self._cfg_int("split_delay_ms", 800), 5000)) / 1000
                )

    # Emoji 清洗: 使用 emoji 库的 demojize，将 emoji 替换为空而非文本描述
    # 这比手写正则更准确、覆盖更全，且不会误删 ZWJ/VS16 等组合字符

    # 括号及其内容: 中英文括号
    _BRACKET_RE = re.compile(r"[\(（\[【][^\)）\]】]*?[\)）\]】]")

    def _clean_response(self, text: str) -> str:
        """对 LLM 回复进行文本清洗。

        根据配置可选清洗以下内容:
        - Emoji 表情符号 (使用 emoji 库)
        - 括号块及内容: ()（）[]【】
        - 句尾多余字符 (波浪号、多余标点等)
        """
        if not text:
            return text

        if not self._cfg_bool("enable_text_clean", False):
            return text

        cleaned = text

        # 1. 去除 Emoji — 使用 emoji 库精确匹配并替换为空
        if self._cfg_bool("clean_emoji", True):
            try:
                cleaned = _emoji_lib.replace_emoji(cleaned, replace="")
            except Exception:
                # emoji 库异常时回退到静默跳过，不破坏输出
                pass

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

    # 多会话管理 — 命令拦截与处理

    async def _try_handle_session_cmd(
        self, event: AstrMessageEvent, message_text: str
    ) -> str | None:
        """尝试拦截会话管理命令。返回响应文本，不匹配返回 None。

        支持的命令:
        - /new: 归档当前会话，开启新会话
        - /list: 查看所有归档会话
        - /switch <N>: 切换到指定归档会话
        - /clear: 清空当前会话上下文（不归档），归零 Token 计数
        - /stats: 查看当前会话累计 Token 用量
        """
        text = message_text.strip()

        # /new
        if self._SESSION_CMD_NEW.match(text):
            return await self._cmd_new(event)

        # /list
        if self._SESSION_CMD_LIST.match(text):
            return await self._cmd_list(event)

        # /switch <N>
        switch_match = self._SESSION_CMD_SWITCH.match(text)
        if switch_match:
            index = int(switch_match.group(1))
            return await self._cmd_switch(event, index)

        # /clear
        if self._SESSION_CMD_CLEAR.match(text):
            return await self._cmd_clear(event)

        # /stats
        if self._SESSION_CMD_STATS.match(text):
            return await self._cmd_stats(event)

        return None

    def _check_session_cmd_permission(self, event: AstrMessageEvent) -> str | None:
        """检查会话命令权限。返回 None 表示通过，返回字符串为拒绝原因。"""
        if self.context_mgr.is_group_message(event):
            if not event.is_admin():
                return "此操作仅限群管理员。"
        return None

    async def _generate_session_title(
        self, event: AstrMessageEvent, messages: list[dict]
    ) -> str:
        """调用 LLM 为会话生成话题标题。

        使用当前激活人格的 system prompt 作为基础，追加命名指令。
        失败时回退到 "未命名会话 (时间戳)" 格式。
        """
        try:
            provider = self._get_provider_with_fallback(
                event.unified_msg_origin, log_tag="[ChatEngine]"
            )
            if not provider:
                raise ValueError("未找到 Provider")

            # 提取最近对话文本用于命名（最多取最后 10 条 user+assistant 消息）
            recent_texts = []
            for msg in reversed(messages):
                role = msg.get("role", "")
                if role in ("user", "assistant"):
                    content = msg.get("content", "")
                    if isinstance(content, list):
                        # 提取 text 部分
                        parts = [
                            p.get("text", "")
                            for p in content
                            if isinstance(p, dict) and p.get("type") == "text"
                        ]
                        text_content = " ".join(parts)
                    else:
                        text_content = content
                    if isinstance(text_content, str) and text_content.strip():
                        prefix = "用户" if role == "user" else "助手"
                        recent_texts.insert(
                            0, f"{prefix}: {text_content.strip()[:200]}"
                        )
                if len(recent_texts) >= 10:
                    break

            if not recent_texts:
                return f"未命名会话 ({shanghai_now_iso()[:16].replace('T', ' ')})"

            # 构建命名 prompt — 使用激活人格的 system prompt
            persona_prompt = await self.persona_mgr.get_system_prompt()
            naming_prompt = (
                f"{persona_prompt}\n\n"
                "请根据以下对话内容，生成一个简短的话题标题（不超过20个字）。"
                "只输出标题本身，不要加引号、标点或其他内容。"
            )
            conversation_summary = "\n".join(recent_texts)

            response = await provider.text_chat(
                system_prompt=naming_prompt,
                prompt=conversation_summary,
            )

            if response and response.completion_text:
                title = response.completion_text.strip().strip("\"'''")
                if title and len(title) <= 50:
                    return title

            return f"未命名会话 ({shanghai_now_iso()[:16].replace('T', ' ')})"

        except Exception as e:
            logger.warning(f"[ChatEngine] 生成会话标题失败: {e}")
            return f"未命名会话 ({shanghai_now_iso()[:16].replace('T', ' ')})"

    async def _cmd_new(self, event: AstrMessageEvent) -> str:
        """处理 /new 命令 — 归档当前会话并开启新会话。



        锁策略: 先锁内快照，锁外生成标题，再锁内重新读取并归档。

        第二次加锁时重新读取最新消息，避免锁外 LLM 调用期间新增消息被丢失。

        """

        # 权限检查

        perm_err = self._check_session_cmd_permission(event)

        if perm_err:
            return f"❌ {perm_err}"

        session_key = self.context_mgr.build_session_key(event)

        # 锁内: 快速加载原始上下文（保留 image_ref，不膨胀）

        async with self.context_mgr.get_session_lock(session_key):
            raw_messages = await self.context_mgr.repo.get_context(session_key)

        # 锁外: LLM 标题生成（网络调用，不阻塞并发消息）

        archive_title = None

        if raw_messages:
            archive_title = await self._generate_session_title(event, raw_messages)

        # 锁内: 重新读取最新消息 + 归档 + 清空

        # 重新读取确保 LLM 标题生成期间新增的消息不会丢失

        async with self.context_mgr.get_session_lock(session_key):
            latest_messages = await self.context_mgr.repo.get_context(session_key)

            if latest_messages:
                (
                    prompt_tokens,
                    completion_tokens,
                ) = await self.context_mgr.repo.get_token_usage(session_key)
                await self.db.archived_session_repo.archive(
                    session_key,
                    archive_title,
                    latest_messages,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                )

                # 清空当前上下文 + 归零 Token 计数

                await self.context_mgr.repo.clear_session(session_key)

                logger.info(
                    f"[ChatEngine] 会话已归档: {session_key}, "
                    f"标题: {archive_title}, 消息数: {len(latest_messages)}, "
                    f"Token: {prompt_tokens + completion_tokens}"
                )

        if archive_title and raw_messages:
            return f"✅ 已开启新会话\n上一话题已归档: {archive_title}"

        return "✅ 已开启新会话"

    async def _cmd_list(self, event: AstrMessageEvent) -> str:
        """处理 /list 命令 — 查看所有归档会话。"""
        # 权限检查
        perm_err = self._check_session_cmd_permission(event)
        if perm_err:
            return f"❌ {perm_err}"

        session_key = self.context_mgr.build_session_key(event)
        archives = await self.db.archived_session_repo.list_by_session_key(session_key)

        if not archives:
            return "📋 暂无归档会话。"

        lines = ["📋 归档会话列表："]
        for idx, archive in enumerate(archives, 1):
            title = archive.title or "未命名"
            count = archive.message_count
            updated = (
                archive.updated_at.strftime("%m-%d %H:%M") if archive.updated_at else ""
            )
            lines.append(f"{idx}. {title} ({count}条, {updated})")

        lines.append("\n使用 /switch <序号> 切换到指定会话")
        return "\n".join(lines)

    async def _cmd_switch(self, event: AstrMessageEvent, index: int) -> str:
        """处理 /switch <N> 命令 — 切换到指定归档会话。

        锁策略: 先锁内快照+验证，锁外生成标题，再锁内重新读取并执行归档+恢复。
        与 _cmd_new 保持一致的两阶段锁模式，避免锁内网络调用阻塞并发消息。
        """
        # 权限检查
        perm_err = self._check_session_cmd_permission(event)
        if perm_err:
            return f"❌ {perm_err}"

        session_key = self.context_mgr.build_session_key(event)

        # 第一阶段: 锁内快照 + 获取归档列表（验证有效性）
        async with self.context_mgr.get_session_lock(session_key):
            raw_current = await self.context_mgr.repo.get_context(session_key)
            archives = await self.db.archived_session_repo.list_by_session_key(
                session_key
            )
            if not archives:
                return "❌ 没有可切换的归档会话。使用 /list 查看。"
            if index < 1 or index > len(archives):
                return f"❌ 无效序号 {index}，有效范围: 1-{len(archives)}"
            target = archives[index - 1]
            # 快照目标 ID 和标题（锁外不依赖归档列表对象）
            target_id = target.id
            target_title = target.title or "未命名"

        # 锁外: LLM 标题生成（网络调用，不阻塞并发消息）
        current_archive_title = None
        if raw_current:
            current_archive_title = await self._generate_session_title(
                event, raw_current
            )

        # 第二阶段: 锁内重新读取并执行归档+恢复
        async with self.context_mgr.get_session_lock(session_key):
            # 重新获取目标归档（防止锁外期间被并发删除）
            target = await self.db.archived_session_repo.get_by_id(target_id)
            if not target:
                return "❌ 目标归档已不存在（可能被其他操作删除）。"
            if target.session_key != session_key:
                return "❌ 归档不属于此会话。"

            # 重新读取当前上下文（避免锁外期间新增的消息被丢失）
            latest_current = await self.context_mgr.repo.get_context(session_key)

            # 归档当前上下文（连带当前 Token 计数快照）
            if latest_current:
                (
                    cur_prompt,
                    cur_completion,
                ) = await self.context_mgr.repo.get_token_usage(session_key)
                await self.db.archived_session_repo.archive(
                    session_key,
                    current_archive_title,
                    latest_current,
                    prompt_tokens=cur_prompt,
                    completion_tokens=cur_completion,
                )

            # 恢复目标归档的上下文到当前会话
            target_messages = json.loads(target.messages_json)
            target_messages = await self.context_mgr._store_images_for_messages(
                target_messages
            )
            await self.context_mgr.repo.save_context(session_key, target_messages)

            # 恢复目标归档的 Token 计数快照
            await self.context_mgr.repo.set_token_usage(
                session_key,
                target.prompt_tokens or 0,
                target.completion_tokens or 0,
            )

            # 删除已恢复的归档记录
            deleted = await self.db.archived_session_repo.delete(target_id)
            if not deleted:
                logger.warning(
                    f"[ChatEngine] 归档记录 {target_id} 删除失败（可能已被并发操作）"
                )

        parts = [f"✅ 已切换到会话: {target_title}"]
        if current_archive_title:
            parts.append(f"当前会话已归档: {current_archive_title}")
        return "\n".join(parts)

    async def _cmd_clear(self, event: AstrMessageEvent) -> str:
        """处理 /clear 命令 — 清空当前会话上下文（不归档），归零 Token 计数。"""
        perm_err = self._check_session_cmd_permission(event)
        if perm_err:
            return f"❌ {perm_err}"

        session_key = self.context_mgr.build_session_key(event)

        async with self.context_mgr.get_session_lock(session_key):
            cleared_count = await self.context_mgr.repo.clear_session(session_key)

        logger.info(
            f"[ChatEngine] 会话上下文已清空: {session_key}, 清除 {cleared_count} 条消息"
        )
        if cleared_count:
            return f"✅ 已清空当前会话（{cleared_count} 条消息），Token 计数已归零。"
        return "✅ 当前会话已是空的。"

    async def _cmd_stats(self, event: AstrMessageEvent) -> str:
        """处理 /stats 命令 — 查看当前会话累计 Token 用量（估算）。"""
        session_key = self.context_mgr.build_session_key(event)

        async with self.context_mgr.get_session_lock(session_key):
            (
                prompt_tokens,
                completion_tokens,
            ) = await self.context_mgr.repo.get_token_usage(session_key)
        total = prompt_tokens + completion_tokens
        return (
            "📊 当前会话 Token 用量（估算）\n"
            f"输入：{prompt_tokens:,}\n"
            f"输出：{completion_tokens:,}\n"
            f"总计：{total:,}"
        )

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

You have access to memory tools (save_memory, search_memory, update_memory, delete_memory), proactive reply tools (schedule_reply), quote reply tool (reply_with_quote), and image viewing tool (view_image). Use them proactively:

- **save_memory**: When the user shares personal preferences, habits, important facts, or explicitly says things like "记住了", "记住", "别忘了", "记住这个". Choose type="long_term" for persistent facts (preferences, identity) or type="short_term" for temporary context (current topic, recent plans).
  - **pinned="true"**: Use for standing rules or instructions that must ALWAYS be active regardless of topic (e.g. "user wants responses under 30 chars", "always reply in a cute tone"). Pinned memories bypass semantic search and are injected every turn.
- **search_memory**: Before answering questions about the user's preferences or past discussions, search your long-term memory for relevant context.
- **update_memory**: When the user corrects or updates previously remembered information.
- **delete_memory**: When the user explicitly asks to forget something.
- **schedule_reply**: When the user asks you to remind them later, when you want to follow up on a topic, or when saying things like "一会提醒我", "过XX分钟告诉我". Also use when the conversation naturally suggests a follow-up would be welcome.
- **reply_with_quote**: When you want to reply to a specific earlier message in the conversation. Each user message is tagged with `[msg:ID]` — call `reply_with_quote(message_id)` first, then generate your reply text. It will be sent as a quoted reply on the platform. Use this when directly addressing a specific past message (e.g. answering an earlier question, confirming something the user said). Do NOT overuse — only when there is a clear reference target.
- **view_image**: When you see [Image] placeholders in historical context and need to see the actual image content, call `view_image(message_id)` to load it. The image will be injected into your context. Use this when the image content is relevant to your response (e.g. user asks about an earlier image, or you need visual context to answer a question).

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

    # 图片查看工具 — LLM Tool Call

    @filter.llm_tool(name="view_image")
    async def tool_view_image(
        self,
        event: AstrMessageEvent,
        message_id: str,
    ):
        """View the actual image(s) from a historical message.
        Historical images appear as [Image] placeholders in context.
        Call this to load and see the real image content.
        After calling, the image will be injected into your context for the next response.

        Args:
            message_id(string): The message ID containing the image. Shown as [msg:ID] tag in context messages.
        """
        session_key = self.context_mgr.build_session_key(event)

        # 从数据库加载原始上下文（保留 image_ref 引用）
        try:
            raw_messages = await self.context_mgr.repo.get_context(session_key)
        except Exception as e:
            return f"Failed to load context: {type(e).__name__}"

        # 查找目标消息
        target_msg = None
        for msg in raw_messages:
            if msg.get("message_id") == message_id:
                target_msg = msg
                break

        if not target_msg:
            return (
                f"Message ID '{message_id}' not found in current session context. "
                "Available IDs can be found in [msg:ID] tags."
            )

        content = target_msg.get("content")
        if not isinstance(content, list):
            return "This message does not contain any images."

        # 解析 image_ref 为 data URL
        image_parts = []
        image_store = self.context_mgr.image_store
        if not image_store:
            return "Image storage is not available."

        for part in content:
            if isinstance(part, dict) and part.get("type") == "image_ref":
                resolved = await image_store.resolve_image_ref(part)
                if resolved and resolved.get("type") == "image_url":
                    image_parts.append(resolved)
            elif isinstance(part, dict) and part.get("type") == "image_url":
                # 已经是 data URL（尚未存储的）
                image_parts.append(part)

        if not image_parts:
            return "No images found in this message (images may have been lost or expired)."

        # 暂存图片到实例变量，由 _llm_call_with_tools 直接嵌入当前 tool result
        self._last_tool_images = image_parts
        logger.info(
            f"[ChatEngine] 图片查看请求: msg_id={message_id}, images={len(image_parts)}"
        )
        return (
            f"Loaded {len(image_parts)} image(s) from message [{message_id}]. "
            "The images have been injected into your context for viewing."
        )

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
        return_to_llm: str = "false",
    ):
        """Execute a registered bot command by name.
        By default, the command output is sent directly to the user. Set return_to_llm="true" to receive the output yourself for further processing.
        IMPORTANT: Only call this when the user's intent directly maps to a specific command.
        If the command is not found, STOP and inform the user. Do NOT try alternative commands.

        Args:
            command(string): The full command string to execute (without the wake prefix). e.g. "help", "provider 1", "sid".
            return_to_llm(string): Set to "true" to return the command output to the LLM for further processing. Default "false" — results are sent directly to the user.
        """
        if not self.cmd_dispatcher:
            return json.dumps({"error": "命令执行功能未启用。"}, ensure_ascii=False)

        send_directly = return_to_llm.lower() not in ("true", "1", "yes")
        result = await self.cmd_dispatcher.dispatch(
            event, command, capture_result=send_directly
        )

        if send_directly:
            if not result.get("success", False):
                # 命令执行失败，返回错误信息给 LLM
                return json.dumps(result, ensure_ascii=False)

            # 命令执行成功，结果直接发送给用户
            result_chains = result.get("result_chains", [])
            result_text = result.get("result", "")

            _ctx = _tool_call_ctx.get()
            if _ctx:
                if result_chains:
                    for _chain in result_chains:
                        _ctx.pending_sends.append(("chain", _chain))
                elif result_text and result_text != "命令执行完成（无输出）":
                    _ctx.pending_sends.append(("text", result_text))

            return json.dumps(
                {"success": True, "result": "命令已执行，结果已直接发送给用户。"},
                ensure_ascii=False,
            )

        return json.dumps(result, ensure_ascii=False)
