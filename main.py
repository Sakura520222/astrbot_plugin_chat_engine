"""astrbot_plugin_chat_engine — 完全替代 AstrBot 自带聊天功能的插件。

深度劫持消息管道，独立实现:
- 上下文管理 (群聊共享 / 私聊隔离)
- 用户识别 ({{user}{昵称}({ID})}说：格式)
- 人格管理 (独立于 AstrBot)
- Tool Calls (扫描所有工具，原生 function calling)
- 上下文压缩 (轮数限制 / Token 阈值 LLM 总结)
- 记忆系统 (短期记忆 / 长期记忆 / LLM 工具主动记忆 / 自动总结)
- WebUI 管理面板 (AstrBot 插件 Pages)
"""

import asyncio
import base64
import contextvars
import copy
import hashlib
import inspect
import json
import re
import time as _time
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime

import emoji as _emoji_lib

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Plain, Reply
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core import sp
from astrbot.core.message.message_event_result import (
    MessageEventResult,
    ResultContentType,
)
from astrbot.core.pipeline.context_utils import call_event_hook
from astrbot.core.provider.modalities import sanitize_contexts_by_modalities
from astrbot.core.star.star_handler import EventType

from .context.active_turn import (
    ActiveTurnController,
    ActiveTurnRegistry,
    TurnPhase,
    build_user_row,
)
from .context.inflight import InflightMessage
from .context.manager import ChatContextManager
from .context.token_counter import TokenEstimator
from .db.engine import ChatEngineDB
from .debounce.manager import MessageDebouncer
from .image_gen import ImageGenError, OpenAIImageClient
from .memory.manager import MemoryManager
from .persona.manager import ChatPersonaManager
from .proactive.manager import ProactiveManager
from .tools.command_dispatcher import CommandDispatcher
from .tools.manager import ChatToolManager
from .tools.scanner import ToolScanner
from .utils import SHANGHAI_TZ, format_current_time, shanghai_now_iso
from .utils.config import cfg_bool, cfg_float, cfg_int
from .utils.image import image_data_url_from_base64, local_image_to_data_url
from .web.api import register_web_api_routes


class _ToolCallContext:
    """Per-call context for _llm_call_with_tools, stored in ContextVar to avoid race conditions.

    AstrBot 的 EventBus.dispatch 通过 asyncio.create_task 并发处理事件，
    将 _llm_call_with_tools 的中间状态存储在实例变量上会导致不同会话互相覆盖。
    使用 ContextVar 确保每个 asyncio task 拥有独立的上下文。
    """

    __slots__ = (
        "pending_sends",
        "intermediate_msgs",
        "current_contexts",
        "current_provider",
        "final_response",
        "turned_messages",
        "prompt_tokens",
        "completion_tokens",
    )

    def __init__(self):
        self.pending_sends: list[tuple[str, object]] = []
        self.intermediate_msgs: list[
            dict
        ] = []  # 完整的 tool call 周期消息（assistant + tool results）
        self.current_contexts: list[
            dict
        ] = []  # 当前轮内存上下文（含尚未落库的当前 user_msg）
        self.current_provider = None  # 本次实际使用的 Provider，供工具判断模态能力（fallback 切换后随 ctx 重建）
        self.final_response = None
        # 本回合经 Redirect/Steer 注入的原始消息（持久化成功前保留，
        # 失败时退回 inflight 缓冲，保证"仅消费一次、失败可恢复"）
        self.turned_messages: list = []
        # 本次 _llm_call_with_tools 累计的 Token 用量（估算），整轮结束后由
        # handle_all_messages 写入会话计数，供 /stats 与 WebUI 读取。
        self.prompt_tokens: int = 0
        self.completion_tokens: int = 0


_tool_call_ctx: contextvars.ContextVar[_ToolCallContext | None] = (
    contextvars.ContextVar("_tool_call_ctx", default=None)
)

# Redirect 抢占哨兵: _guarded_text_chat 返回它表示当前模型请求已被取消/作废，
# 调用方应以注入后的上下文重试同一逻辑回合（不进入异常路径，不触发 Provider Fallback）
_TURN_REDIRECTED = object()


@register(
    "astrbot_plugin_chat_engine",
    "车厘子小樱",
    "完全替代 AstrBot 自带聊天功能，独立实现上下文管理、用户识别、多会话管理、人格系统、Tool Calls、上下文压缩、记忆系统和 Dashboard 管理面板。",
    "1.4.0",
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
        self.image_client: OpenAIImageClient | None = None
        self.cmd_dispatcher: CommandDispatcher = None
        self._pending_quotes: dict[str, str] = {}  # {session_key: message_id}
        self._last_tool_images: list[dict] | None = (
            None  # 当前工具调用产生的图片（单次执行生命周期）
        )
        self._last_generated_images: dict[
            str, dict
        ] = {}  # {session_key: image_ref} 最近生成图，供 edit_image(message_id="last") 使用
        self._group_info_cache: dict[str, tuple[float, str, str, bool]] = {}
        # 群聊信息缓存: session_key -> (timestamp, group_name, bot_card, is_fallback), TTL=300s
        # inflight 自动追答连续轮数: session_key -> 已连续自动发起的追答轮数，
        # 用户真实消息开启新一轮时清零；达到 max_inflight_followup_rounds 后停止自动追答
        self._inflight_followup_rounds: dict[str, int] = {}
        # 活跃回合控制器注册表（Redirect/Steer 消息路由目标，回合生命周期内挂载）
        self._active_turns = ActiveTurnRegistry()

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

    @staticmethod
    def _shorten_image_url(url: str) -> str:
        """压缩过长的 data URL（只保留 MIME 前缀），HTTP URL 原样保留。"""
        if url.startswith("data:"):
            # data:image/jpeg;base64,/9j/... → data:image/jpeg
            head = url.split(",", 1)[0]
            return head or "data:image"
        return url

    @staticmethod
    def _mark_images_for_text_only_provider(
        messages: list[dict], modalities: list[str] | None
    ) -> tuple[list[dict], int]:
        """对不支持 image 模态的 provider，把 image_url 块替换为带标识的文本块。

        在 ``sanitize_contexts_by_modalities`` **之前**调用：先把 image_url 编码为
        ``[Image: msg=<id>, url=<url>]`` 文本块，sanitize 看到的就不再是 image_url
        类型，不会再替换成裸 ``[Image]``，从而让 LLM 在纯文本 provider 下仍能看到
        图片的 message_id 与链接，便于准确调用 edit_image / view_image。

        支持图片模态时直接返回原消息（保留 image_url 块供 provider 读取）。
        data URL 只保留 MIME 前缀（``data:image/jpeg``），避免 base64 撑爆 token。

        Returns:
            ``(处理后的消息列表, 被标记的图片块数量)``
        """
        if not modalities or "image" in modalities:
            return messages, 0

        result: list[dict] = []
        marked = 0
        for msg in messages:
            content = msg.get("content")
            if not isinstance(content, list):
                result.append(msg)
                continue
            msg_id = msg.get("message_id", "")
            new_parts = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "image_url":
                    url = part.get("image_url", {}).get("url", "")
                    url_repr = ChatEnginePlugin._shorten_image_url(url)
                    if msg_id:
                        marker = f"[Image: msg={msg_id}, url={url_repr}]"
                    else:
                        marker = f"[Image: url={url_repr}]"
                    new_parts.append({"type": "text", "text": marker})
                    marked += 1
                else:
                    new_parts.append(part)
            result.append({**msg, "content": new_parts})
        return result, marked

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
            astrbot_config_getter=lambda: self.context.get_config(),
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

        # 画图客户端
        if self._cfg_bool("enable_image_generation", False):
            api_base = str(self.config.get("image_gen_api_base", "")).strip()
            api_key = str(self.config.get("image_gen_api_key", "")).strip()
            model = str(self.config.get("image_gen_model", "gpt-image-2")).strip()
            if not api_base or not api_key:
                logger.warning(
                    "[ChatEngine] 画图工具已启用但 API 地址或 Key 为空，画图功能不可用"
                )
                self.image_client = None
            else:
                self.image_client = OpenAIImageClient(
                    api_base=api_base,
                    api_key=api_key,
                    model=model or "gpt-image-2",
                    size=str(self.config.get("image_gen_size", "1024x1024")),
                    quality=str(self.config.get("image_gen_quality", "auto")),
                    timeout=self._cfg_int("image_gen_timeout", 120),
                )
                logger.info(
                    f"[ChatEngine] 画图客户端已初始化: model={self.image_client.model}, "
                    f"size={self.image_client.size}, quality={self.image_client.quality}"
                )
        else:
            self.image_client = None
            logger.info("[ChatEngine] 画图工具未启用")

        # 注册 Dashboard 插件 Pages Web API(认证继承 Dashboard)
        register_web_api_routes(self)

        logger.info("[ChatEngine] 初始化完成, WebAPI 已注册至 Dashboard 插件页")

    async def terminate(self):
        """插件停用/重载时调用"""
        logger.info("[ChatEngine] 正在关闭...")
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

    async def _try_submit_active_turn(
        self, session_key: str, msg: InflightMessage
    ) -> bool:
        """尝试把新消息实时提交给活跃回合（Redirect/Steer 引导）。

        会话存在活跃回合时由控制器按阶段原子路由: 模型请求中 → redirect
        （取消当前请求、回合内重建输入）；工具批次中 → steer（安全边界
        作为真实 user 消息注入）。回合无法消费时返回 False，调用方走
        inflight 缓冲兜底。

        Args:
            session_key: 会话标识
            msg: 完整承载字段的暂存消息

        Returns:
            True 表示活跃回合已接收并负责消费该消息。
        """
        ctrl = getattr(self, "_active_turns", None)
        if ctrl is not None:
            ctrl = ctrl.get(session_key)
        if ctrl is None:
            return False
        verdict = await ctrl.submit(msg)
        if verdict in ("redirect", "steer"):
            logger.info(
                f"[ActiveTurn] 会话 {session_key} 消息已进入活跃回合: {verdict}"
            )
            return True
        # buffer: 收尾阶段或引导预算耗尽，调用方走兜底缓冲
        return False

    @filter.event_message_type(filter.EventMessageType.ALL, priority=9999)
    async def handle_all_messages(self, event: AstrMessageEvent):
        """拦截所有消息，完全接管 AstrBot 的聊天流程。"""
        #  第一步: 无条件抑制 AstrBot 默认 LLM（防抖缓冲/inflight 暂存等延迟
        #  处理路径也不例外）——官方管道在 provider_settings.enable 开启时，
        #  只要 is_at_or_wake_command 且 call_llm=False 就会立即回复，造成
        #  官方 AI 与本插件双重回复。本插件接管全部聊天流程，任何路径都不应恢复。
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
                    # In-flight 拦截: 会话正在执行 AI 请求时，符合触发条件的纯图片消息
                    # 暂存为下次引导，不等待会话锁
                    if self._cfg_bool(
                        "enable_inflight_guidance", True
                    ) and self.context_mgr.should_respond(event):
                        img_key = self.context_mgr.build_session_key(event)
                        if self.context_mgr.inflight_mgr.is_inflight(img_key):
                            img_urls = await self._extract_image_urls(event)
                            img_inflight = InflightMessage(
                                id=uuid.uuid4().hex,
                                session_key=img_key,
                                sender_name=sender,
                                sender_id=event.get_sender_id() or "unknown",
                                time_str=datetime.now(SHANGHAI_TZ).strftime("%H:%M"),
                                text="",
                                images=img_urls,
                                message_id=getattr(event.message_obj, "message_id", ""),
                                umo=event.unified_msg_origin,
                                event=event,
                                is_group=is_group,
                                user_text=self.context_mgr.format_user_message(event),
                            )
                            # 活跃回合实时接收（Redirect/Steer），无法消费才落缓冲
                            if await self._try_submit_active_turn(
                                img_key, img_inflight
                            ):
                                return
                            if await self.context_mgr.inflight_mgr.try_handle_inflight(
                                img_key, img_inflight
                            ):
                                logger.info(
                                    f"[Inflight] 会话 {img_key} 正在执行 AI 请求，"
                                    f"纯图片消息已暂存为下次引导"
                                )
                                return

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
                return

            # 会话管理命令拦截: /new, /list, /switch N
            # 在框架命令检测之前处理，拦截框架原有的 /new 等命令
            if is_at or not is_group:
                session_cmd_result = await self._try_handle_session_cmd(
                    event, message_text
                )
                if session_cmd_result is not None:
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
                        "message_id": getattr(event.message_obj, "message_id", ""),
                        "umo": event.unified_msg_origin,
                        "event": event,
                        "is_active": False,
                    }
                    if self.debouncer.try_add_passive(_passive_key, _passive_data):
                        logger.info(f"[Debounce] 被动消息已并入: {_passive_key}")
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
                        await self.proactive_mgr.on_message(
                            passive_key, is_active=False
                        )
                    except Exception as e:
                        logger.debug(f"[ChatEngine] 主动回复注册失败: {e}")

                return

            # 构建会话 Key（供 in-flight 拦截与后续 LLM 回合共用）
            session_key = self.context_mgr.build_session_key(event)

            # === In-flight 拦截: 会话正在执行 AI 请求时暂存新触发消息 ===
            # 必须在进入防抖/会话锁路径之前识别，避免排队等待或防抖定时器
            # 触发独立回复
            _reply_ctx = self.context_mgr._extract_reply_context(event)
            _display_text = (
                f"[回复 {_reply_ctx}]{message_text}" if _reply_ctx else message_text
            )
            if self._cfg_bool(
                "enable_inflight_guidance", True
            ) and self.context_mgr.inflight_mgr.is_inflight(session_key):
                _inflight_msg = InflightMessage(
                    id=uuid.uuid4().hex,
                    session_key=session_key,
                    sender_name=sender,
                    sender_id=event.get_sender_id() or "unknown",
                    time_str=datetime.now(SHANGHAI_TZ).strftime("%H:%M"),
                    text=_display_text,
                    images=await self._extract_image_urls(event),
                    message_id=getattr(event.message_obj, "message_id", ""),
                    umo=event.unified_msg_origin,
                    event=event,
                    is_group=is_group,
                    user_text=self.context_mgr.format_user_message(event),
                )
                # 活跃回合实时接收（Redirect/Steer），无法消费才落缓冲兜底
                if await self._try_submit_active_turn(session_key, _inflight_msg):
                    return
                if await self.context_mgr.inflight_mgr.try_handle_inflight(
                    session_key, _inflight_msg
                ):
                    logger.info(
                        f"[Inflight] 会话 {session_key} 正在执行 AI 请求，"
                        f"消息已暂存（本轮结束后自动处理）"
                    )
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
                # 用户真实消息开启了新防抖窗口：自动追答连续轮数计数清零
                # （被暂存的消息不经过此处，不会清零，保证追答上限有效）
                self._inflight_followup_rounds.pop(_debounce_key, None)

                # 主动回复: 注册会话 + 轮数计数
                if self.proactive_mgr:
                    try:
                        await self.proactive_mgr.register_session(
                            _debounce_key, _debounce_data["umo"]
                        )
                        # 主动消息（@bot）会走 debounce 生成回复，标记 is_active=True
                        # 让 AI 判断跳过计数，避免与主动回复冲突
                        await self.proactive_mgr.on_message(
                            _debounce_key, is_active=True
                        )
                    except Exception as e:
                        logger.debug(f"[ChatEngine] 主动回复注册失败: {e}")

                if _force:
                    await self.debouncer.force_flush(_debounce_key)
                logger.info(
                    f"[Debounce] 消息已缓冲: {_debounce_key}, 强制刷新={_force}"
                )
                return

            #  构建会话 Key（已在 in-flight 拦截前构建）
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

            # === 会话执行权仲裁: 持有至本轮回复全部发送完毕，防止两轮回复交错 ===
            _inflight_enabled = self._cfg_bool("enable_inflight_guidance", True)
            _turn_acquired = False
            if _inflight_enabled:
                _turn_acquired = await self.context_mgr.inflight_mgr.acquire_session(
                    session_key
                )
                if not _turn_acquired:
                    # 极小概率竞态: is_inflight 检查后、acquire 前有其他回合抢得执行权
                    _race_msg = InflightMessage(
                        id=uuid.uuid4().hex,
                        session_key=session_key,
                        sender_name=sender,
                        sender_id=event.get_sender_id() or "unknown",
                        time_str=datetime.now(SHANGHAI_TZ).strftime("%H:%M"),
                        text=_display_text,
                        images=image_urls,
                        message_id=getattr(event.message_obj, "message_id", ""),
                        umo=event.unified_msg_origin,
                        event=event,
                        is_group=is_group,
                        user_text=user_text,
                    )
                    # 优先实时进入当前活跃回合（Redirect/Steer）
                    if await self._try_submit_active_turn(session_key, _race_msg):
                        return
                    await self.context_mgr.inflight_mgr.add_inflight_message(
                        session_key, _race_msg
                    )
                    logger.info(
                        f"[Inflight] 会话 {session_key} 并发竞态，消息已暂存（本轮结束后自动处理）"
                    )
                    return
                # 用户真实消息开启了新回合：自动追答连续轮数计数清零
                self._inflight_followup_rounds.pop(session_key, None)

            try:
                _turn_ok = False
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
                _turn_ok = isinstance(response_text, str) and response_text != ""
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
            finally:
                if _turn_acquired:
                    await self.context_mgr.inflight_mgr.release_session(session_key)
                    # 回合成功结束后，自动处理期间暂存的消息（无需用户再发消息）
                    if _turn_ok:
                        await self._maybe_followup_inflight(session_key)

        except Exception as e:
            logger.error(f"[ChatEngine] 顶层异常: {e}", exc_info=True)
            try:
                yield event.plain_result(f"❌ ChatEngine 异常: {type(e).__name__}")
            except Exception:
                pass

    # AstrBot 管道钩子广播 — 兼容依赖 LLM 管道钩子的插件

    @staticmethod
    def _extract_prompt_text(user_msg: dict) -> str:
        """从 user_msg dict 提取纯文本，供 ProviderRequest.prompt 使用。"""
        content = user_msg.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(
                part.get("text", "")
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            )
        return ""

    async def _broadcast_llm_request_hooks(
        self,
        event: AstrMessageEvent,
        system_prompt: str,
        contexts: list[dict],
        user_msg: dict,
        tool_set,
    ) -> tuple[str, object] | None:
        """广播 AstrBot 官方 LLM 请求钩子，兼容依赖钩子的插件。

        ChatEngine 抑制了 AstrBot 默认 LLM 管道（should_call_llm + 直连
        text_chat），依赖 on_llm_request 钩子的插件（如 meme_manager 的
        语义表情包）因此收不到通知。这里复刻官方管道行为，广播
        OnWaitingLLMRequestEvent 与 OnLLMRequestEvent，并采用钩子对
        ``req.system_prompt`` / ``req.func_tool`` 的改写——插件可借此注入
        提示词、移除不可用工具等。每条消息只广播一次（与官方管道一致）。

        Returns:
            (system_prompt, tool_set) 钩子改写后的值；None 表示钩子终止了
            事件传播，应放弃本轮回复（与官方管道行为一致）。
        """
        try:
            if await call_event_hook(event, EventType.OnWaitingLLMRequestEvent):
                logger.info("[ChatEngine] OnWaitingLLMRequest 钩子终止了事件")
                return None
            model = None
            try:
                provider = self.context.get_using_provider(event.unified_msg_origin)
                if provider is not None:
                    model = provider.get_model()
            except Exception:
                model = None
            req = ProviderRequest(
                prompt=self._extract_prompt_text(user_msg) or None,
                session_id=event.get_session_id(),
                contexts=list(contexts),
                system_prompt=system_prompt,
                func_tool=tool_set,
                model=model,
            )
            if await call_event_hook(event, EventType.OnLLMRequestEvent, req):
                logger.info("[ChatEngine] OnLLMRequest 钩子终止了事件")
                return None
            return req.system_prompt, req.func_tool
        except Exception as e:
            # 钩子广播不应让聊天挂掉：记录后按原参数继续
            logger.warning(f"[ChatEngine] LLM 请求钩子广播异常，跳过钩子: {e}")
            return system_prompt, tool_set

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
        # === 会话执行权仲裁（可重入）: 调用方已持有时仅加深计数 ===
        enable_inflight = self._cfg_bool("enable_inflight_guidance", True)
        stage_gen: int | None = None
        stage_committed = False
        guide_msgs: list[InflightMessage] = []
        overflow_msgs: list[InflightMessage] = []
        request_user_msg = user_msg
        turn_ctrl: ActiveTurnController | None = None
        turned_final: list[InflightMessage] = []
        if enable_inflight:
            if not await self.context_mgr.inflight_mgr.acquire_session(session_key):
                logger.warning(f"{log_tag} 会话执行权获取失败（并发竞态），跳过本轮")
                return None
            # 活跃回合控制器: 本回合期间新消息经 Redirect/Steer 实时进入
            # 当前逻辑回合，而非等待回合结束后独立回复
            turn_ctrl = self._active_turns.register(session_key)
            # === In-flight 引导注入: 原始 user_msg 保持不变用于持久化，
            # request_user_msg 仅作为本轮 LLM 输入（引导块不落库） ===
            max_guide = max(
                1, min(self._cfg_int("max_inflight_guidance_messages", 10), 100)
            )
            (
                stage_gen,
                guide_msgs,
                overflow_msgs,
            ) = await self.context_mgr.inflight_mgr.stage_messages(
                session_key, max_guide
            )
            if guide_msgs:
                guidance_prompt = self.context_mgr.inflight_mgr.build_guidance_prompt(
                    guide_msgs
                )
                guide_images = [u for gm in guide_msgs for u in gm.images]
                request_user_msg = copy.deepcopy(user_msg)
                u_content = request_user_msg.get("content")
                if isinstance(u_content, str):
                    if guide_images:
                        # 纯文本消息 + 引导含图片 → 升级为多模态列表
                        request_user_msg["content"] = [
                            {
                                "type": "text",
                                "text": f"{guidance_prompt}\n\n---\n\n{u_content}",
                            }
                        ] + [
                            {"type": "image_url", "image_url": {"url": u}}
                            for u in guide_images
                        ]
                    else:
                        request_user_msg["content"] = (
                            f"{guidance_prompt}\n\n---\n\n{u_content}"
                        )
                elif isinstance(u_content, list):
                    for part in request_user_msg["content"]:
                        if isinstance(part, dict) and part.get("type") == "text":
                            part["text"] = (
                                f"{guidance_prompt}\n\n---\n\n{part.get('text', '')}"
                            )
                            break
                    else:
                        request_user_msg["content"].insert(
                            0, {"type": "text", "text": guidance_prompt}
                        )
                    request_user_msg["content"].extend(
                        {"type": "image_url", "image_url": {"url": u}}
                        for u in guide_images
                    )
                logger.info(f"{log_tag} 注入 {len(guide_msgs)} 条 in-flight 引导")
        try:
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
                            logger.info(
                                f"{log_tag} 已启用工具名称数: {len(enabled_names)}"
                            )

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
                        if self.image_client:
                            system_prompt += self._build_image_tool_guidance()
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

                _sent_any = (
                    False  # 是否已将任何内容交给回调（True 后不再切换 provider）
                )
                final_response = None
                _llm_ctx = None
                used_provider = None
                last_err: object = None
                # 图片转述 turn 级缓存(url → caption):Provider 回退重试同批图片不重复转述
                _caption_cache: dict[str, str] = {}

                # 广播官方 LLM 管道钩子（每条消息一次，Provider 回退复用结果）
                # on_llm_request 类插件（如 meme_manager 语义表情包）依赖钩子注入
                # 提示词 / 调整工具集；钩子终止事件时返回 "" 而非 None，
                # 避免上层把钩子拦截当成 LLM 失败而发送错误提示。
                broadcast_hooks = bool(event) and self._cfg_bool(
                    "broadcast_llm_hooks", True
                )
                if broadcast_hooks:
                    hooked = await self._broadcast_llm_request_hooks(
                        event,
                        system_prompt,
                        context_messages,
                        request_user_msg,
                        tool_set,
                    )
                    if hooked is None:
                        return ""
                    system_prompt, tool_set = hooked
                    # 钩子可能移除了工具（如索引未就绪时 meme_manager 会移除
                    # search_memes），重新计算工具数保证日志准确
                    tool_count = (
                        len(tool_set.names())
                        if tool_set and not tool_set.empty()
                        else 0
                    )

                for _p_idx, provider in enumerate(providers):
                    _provider_id = (
                        provider.meta().id
                        if hasattr(provider, "meta")
                        else f"#{_p_idx}"
                    )
                    try:
                        # 模态过滤（不同 Provider 可能支持不同模态，每轮重算）
                        modalities = await self.context_mgr.get_modalities(provider)
                        all_messages = copy.deepcopy(
                            list(context_messages) + [request_user_msg]
                        )
                        # 图片转述:纯文本 Provider 且配置了 AstrBot 默认图片转述模型时,
                        # 把当前用户消息的图片块替换为转述文本;失败的块保留原样由
                        # _mark_images_for_text_only_provider 兜底标记
                        if modalities and "image" not in modalities:
                            captioned = await self._caption_user_msg_images(
                                all_messages, umo, _caption_cache
                            )
                            if captioned and log_tag == "[ChatEngine]":
                                logger.info(f"{log_tag} 图片转述: {captioned} 张")
                        all_messages, marked_images = (
                            self._mark_images_for_text_only_provider(
                                all_messages, modalities
                            )
                        )
                        if marked_images and log_tag == "[ChatEngine]":
                            logger.info(
                                f"{log_tag} 文本模态图片标记: {marked_images} 个图片块"
                            )
                        sanitized, stats = sanitize_contexts_by_modalities(
                            all_messages, modalities
                        )
                        llm_contexts = sanitized[:-1]
                        llm_user_msg = sanitized[-1]
                        if stats.changed:
                            if log_tag == "[ChatEngine]":
                                logger.info(
                                    f"{log_tag} 模态过滤: 替换 {stats.fixed_image_blocks} 个图片块, "
                                    f"{stats.fixed_audio_blocks} 个音频块"
                                )

                        # 剥离历史图片
                        llm_contexts = self._strip_history_images(llm_contexts)

                        # Token 安全截断
                        llm_contexts = await self._trim_context_to_fit(
                            llm_contexts, provider
                        )

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
                            turn_ctrl=turn_ctrl,
                            text_only_provider=bool(
                                modalities and "image" not in modalities
                            ),
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
                            # 已注入本回合的引导消息放回队首，随下一候选重注入
                            # （不随 Provider 失败丢失，也不重复消费）
                            if turn_ctrl is not None and _llm_ctx:
                                await turn_ctrl.requeue(_llm_ctx.turned_messages)
                            if _sent_any:
                                break
                            logger.warning(
                                f"{log_tag} Provider[{_p_idx}] {_provider_id} 返回 None，尝试下一个"
                            )
                            continue

                        if (
                            hasattr(final_response, "role")
                            and final_response.role == "err"
                        ):
                            last_err = (
                                f"LLM 错误: "
                                f"{getattr(final_response, 'completion_text', '未知')}"
                            )
                            if turn_ctrl is not None and _llm_ctx:
                                await turn_ctrl.requeue(_llm_ctx.turned_messages)
                            if _sent_any:
                                break
                            logger.warning(
                                f"{log_tag} Provider[{_p_idx}] {_provider_id} 返回错误，尝试下一个"
                            )
                            continue

                        # 成功
                        used_provider = provider
                        if _llm_ctx:
                            turned_final = list(_llm_ctx.turned_messages)
                        break

                    except Exception as e:
                        last_err = e
                        _fail_ctx = _tool_call_ctx.get()
                        if turn_ctrl is not None and _fail_ctx is not None:
                            await turn_ctrl.requeue(_fail_ctx.turned_messages)
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

                # 广播 LLM 响应钩子:让 on_llm_response 类插件处理/改写响应
                # (如 meme_manager 提取 &&meme:id&& 标记并从文本中剥离)
                if broadcast_hooks:
                    try:
                        if await call_event_hook(
                            event, EventType.OnLLMResponseEvent, final_response
                        ):
                            logger.info(
                                f"{log_tag} LLM 响应钩子终止了事件，放弃本轮发送"
                            )
                            return ""
                    except Exception as e:
                        logger.warning(f"{log_tag} LLM 响应钩子广播异常: {e}")

                # 处理响应文本
                response_text = final_response.completion_text or ""
                if log_tag == "[ChatEngine]":
                    logger.info(f"{log_tag} LLM 响应长度: {len(response_text)}")

                response_text = self._clean_response(response_text)

                # 广播消息装饰钩子:让 on_decorating_result 类插件清理待发文本、
                # 附加图片等组件 (如 meme_manager 剥离表情标记、附加表情图片)。
                # 结果以 LLM_RESULT 类型挂到 event 上供插件读取，钩子结束后
                # 取回改写内容并 clear_result，防止框架 respond 阶段重复发送。
                _decorating_components: list = []
                if broadcast_hooks and response_text:
                    try:
                        _deco = MessageEventResult(chain=[Plain(response_text)])
                        _deco.set_result_content_type(ResultContentType.LLM_RESULT)
                        event.set_result(_deco)
                        _stopped = await call_event_hook(
                            event, EventType.OnDecoratingResultEvent
                        )
                        _after = event.get_result()
                        if _after is not None:
                            _texts = []
                            for comp in _after.chain or []:
                                if isinstance(comp, Plain):
                                    _texts.append(comp.text)
                                else:
                                    _decorating_components.append(comp)
                            response_text = "".join(_texts).strip()
                        event.clear_result()
                        if _stopped or event.is_stopped():
                            logger.info(
                                f"{log_tag} 消息装饰钩子终止了事件，放弃本轮发送"
                            )
                            return ""
                    except Exception as e:
                        logger.warning(
                            f"{log_tag} 消息装饰钩子广播异常: {e}", exc_info=True
                        )
                        event.clear_result()

                # 检查引用回复
                pending_quote_id = self._pending_quotes.pop(session_key, None)

                # === SENDING 阶段: 回复发送期间停止过时输出 ===
                # 新消息到达（redirect 标记置位）时，未发送的旧回复片段不再
                # 发出（已发出片段按平台能力无法撤回），消息转入兜底缓冲
                if turn_ctrl is not None:
                    await turn_ctrl.set_phase(TurnPhase.SENDING)

                if response_text:
                    # 发送分段文本
                    _first_seg = True
                    _segs = list(self._iter_text_segments_no_delay(response_text))
                    for _si, _seg in enumerate(_segs):
                        if (
                            turn_ctrl is not None
                            and turn_ctrl.check_redirect_crossing()
                            and _si > 0
                        ):
                            logger.info(
                                f"{log_tag} 发送期间收到新消息，停止发送剩余 "
                                f"{len(_segs) - _si} 个旧回复片段"
                            )
                            break
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

                # 回合已无法再消费的引导消息（发送窗口竞态到达）转兜底缓冲，
                # 由回合结束后的自动追答或下次用户消息处理
                if turn_ctrl is not None:
                    _leftover = await turn_ctrl.take_pending()
                    if _leftover:
                        turn_ctrl.acknowledge_redirect()
                        for _lm in _leftover:
                            await self.context_mgr.inflight_mgr.add_inflight_message(
                                session_key, _lm
                            )
                        logger.info(
                            f"{log_tag} {len(_leftover)} 条消息到达于发送阶段，"
                            f"已转入兜底缓冲"
                        )

                # 发送装饰钩子附加的组件 (如 meme_manager 混合模式的表情图片)
                for _comp in _decorating_components:
                    if on_image_component:
                        await on_image_component(_comp)

                # 发送图片结果
                _has_result_chain = bool(getattr(final_response, "result_chain", None))
                if _has_result_chain:
                    for comp in final_response.result_chain.chain:
                        if isinstance(comp, Image):
                            if on_image_component:
                                await on_image_component(comp)

                # 广播消息发送后钩子 (如 meme_manager 发送待发表情图片、清理临时文件)。
                # 注: 正常路径的文本段此时仍在调用方队列中、由 handler yield 后才真正
                # 发出，因此图片可能先于文本到达平台——两路径共用此广播点，保持单点实现。
                if broadcast_hooks and (
                    response_text or _decorating_components or _has_result_chain
                ):
                    try:
                        await call_event_hook(event, EventType.OnAfterMessageSentEvent)
                    except Exception as e:
                        logger.warning(f"{log_tag} 消息发送后钩子广播异常: {e}")

                # === FINALIZING 阶段: 保存期间到达的消息走兜底缓冲 ===
                if turn_ctrl is not None:
                    await turn_ctrl.set_phase(TurnPhase.FINALIZING)

                # 保存上下文（暂存消息以 observed 角色按时间顺序先行归档，
                # user_msg 保持原始内容持久化，引导块不落库）
                all_new_msgs = []
                for gm in overflow_msgs + guide_msgs:
                    if gm.images:
                        all_new_msgs.append(
                            {
                                "role": "observed",
                                "message_id": gm.message_id,
                                "content": [{"type": "text", "text": gm.text}]
                                + [
                                    {"type": "image_url", "image_url": {"url": u}}
                                    for u in gm.images
                                ],
                            }
                        )
                    else:
                        all_new_msgs.append(
                            {
                                "role": "observed",
                                "message_id": gm.message_id,
                                "content": gm.text,
                            }
                        )
                all_new_msgs.append(user_msg)
                if _llm_ctx:
                    all_new_msgs.extend(_llm_ctx.intermediate_msgs)
                all_new_msgs.append({"role": "assistant", "content": response_text})

                pre_save_count = len(context_messages_raw)
                saved = await self.context_mgr._load_compress_save(
                    session_key, all_new_msgs, provider=used_provider
                )
                # 数据库保存成功后才 Commit 暂存消息
                # （保存异常会向上抛出 → finally 回滚，暂存消息不丢失）
                if enable_inflight and stage_gen is not None:
                    await self.context_mgr.inflight_mgr.commit_stage(
                        session_key, stage_gen
                    )
                    stage_committed = True
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
        finally:
            if enable_inflight:
                # 持久化未成功时，已注入本回合的引导消息退回 inflight 缓冲
                # （仅消费一次原则；成功路径它们已随 intermediate_msgs 落库）
                if not stage_committed and turned_final:
                    for m in turned_final:
                        await self.context_mgr.inflight_mgr.add_inflight_message(
                            session_key, m
                        )
                    logger.info(
                        f"{log_tag} 回合未持久化，{len(turned_final)} 条已注入"
                        f"引导消息退回缓冲"
                    )
                if turn_ctrl is not None:
                    # 残留 pending（Provider 全部失败后的 requeue、极端竞态）
                    # 同样退回缓冲，不静默丢弃
                    _residual = await turn_ctrl.take_pending()
                    for _rm in _residual:
                        await self.context_mgr.inflight_mgr.add_inflight_message(
                            session_key, _rm
                        )
                    self._active_turns.unregister(session_key, turn_ctrl)
                if stage_gen is not None and not stage_committed:
                    await self.context_mgr.inflight_mgr.rollback_stage(
                        session_key, stage_gen
                    )
                await self.context_mgr.inflight_mgr.release_session(session_key)

    # LLM 调用 + Tool Call 循环

    @staticmethod
    def _dump_tool_calls(response) -> list[dict]:
        """把 LLMResponse 的 tool_calls 转为 OpenAI dict 格式（存入上下文）。

        使用 4.27.1+ 的 to_openai_tool_calls_model；旧 to_openai_tool_calls 已被
        AstrBot 标记废弃（4.27.1 起触发运行时警告）。exclude_none 确保输出形状
        与旧 dict 版本一致（extra_content 为 None 时不出现）。
        """
        return [
            tc.model_dump(exclude_none=True)
            for tc in response.to_openai_tool_calls_model()
        ]

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
            self._dump_tool_calls(response)
            if getattr(response, "tools_call_name", None)
            else None
        )
        estimator = self.context_mgr.token_counter
        ctx.prompt_tokens += estimator.estimate_prompt(system_prompt, contexts)
        ctx.completion_tokens += estimator.estimate_completion(
            getattr(response, "completion_text", "") or "", tool_calls
        )

    async def _guarded_text_chat(
        self,
        turn_ctrl: ActiveTurnController | None,
        provider,
        kwargs: dict,
        current_contexts: list[dict],
        ctx: _ToolCallContext,
        text_only_provider: bool,
    ):
        """带 Redirect 抢占守卫的 provider.text_chat。

        请求注册到活跃回合控制器，可被新到达的用户消息实时取消；取消与
        "响应已返回但重定向同时到达"的交叉均转译为 ``_TURN_REDIRECTED``
        哨兵返回（过时响应直接丢弃，不重放未完成推理），已完成工作保留
        在 ``current_contexts`` 由调用方重建输入重试。非 Redirect 引起的
        取消（插件停用等）在终止内层请求后原样上抛；Redirect 走哨兵而非
        异常路径，因此绝不计入 Provider Fallback。

        Args:
            turn_ctrl: 活跃回合控制器；None 时退化为直连调用
            provider: Provider 实例
            kwargs: text_chat 关键字参数
            current_contexts: 本回合工作上下文（引导注入目标）
            ctx: 工具调用上下文（持久化与失败恢复追踪）
            text_only_provider: 当前 Provider 是否不支持图片输入

        Returns:
            Provider 响应，或 ``_TURN_REDIRECTED`` 表示需重建输入重试。
        """
        if turn_ctrl is None:
            return await provider.text_chat(**kwargs)

        await turn_ctrl.set_phase(TurnPhase.MODEL_RUNNING)
        task = asyncio.create_task(provider.text_chat(**kwargs))
        stale = await turn_ctrl.register_model_task(task)
        if stale is not None and not stale.done():
            stale.cancel()
        try:
            response = await task
        except asyncio.CancelledError:
            if task.cancelled() and turn_ctrl.consume_redirect_for(task):
                await self._inject_guidance_rows(
                    turn_ctrl,
                    current_contexts,
                    ctx,
                    text_only_provider,
                    trigger="redirect-cancel",
                )
                logger.info("[ActiveTurn] 模型请求已被新消息抢占，回合内重建输入")
                return _TURN_REDIRECTED
            # 外层取消（插件停用等）: 终止内层请求后原样上抛
            if not task.done():
                task.cancel()
            raise
        finally:
            await turn_ctrl.finish_model_task(task)
        if turn_ctrl.check_redirect_crossing():
            # 响应与重定向交叉: 丢弃过时响应，保留已完成工作后重建
            await self._inject_guidance_rows(
                turn_ctrl,
                current_contexts,
                ctx,
                text_only_provider,
                trigger="redirect-crossing",
            )
            logger.info("[ActiveTurn] 响应与重定向交叉，丢弃过时响应并重建输入")
            return _TURN_REDIRECTED
        return response

    async def _inject_guidance_rows(
        self,
        turn_ctrl: ActiveTurnController,
        current_contexts: list[dict],
        ctx: _ToolCallContext,
        text_only_provider: bool,
        trigger: str,
    ) -> int:
        """把活跃回合待注入消息转为真实 user 上下文行并追踪消费。

        行按到达顺序追加在 ``current_contexts`` 末尾（工具批次后为合法的
        ``tool → user`` 消息序列），同时进入 ``intermediate_msgs`` 参与
        持久化、原始消息进入 ``turned_messages`` 供持久化失败时退回缓冲，
        满足"消息仅消费一次、失败可恢复"。每批最后一条携带中途到达标注
        （STEER_NOTE），指引模型合并处理新旧任务而非丢弃未完成的旧任务。

        Args:
            turn_ctrl: 活跃回合控制器
            current_contexts: 本回合工作上下文
            ctx: 工具调用上下文
            text_only_provider: 当前 Provider 是否不支持图片输入
            trigger: 注入触发点（redirect-cancel / redirect-crossing /
                tool-boundary），仅用于日志

        Returns:
            注入的消息条数。
        """
        msgs = await turn_ctrl.take_pending()
        if not msgs:
            return 0
        for i, m in enumerate(msgs):
            row = build_user_row(m, text_only_provider, with_note=(i == len(msgs) - 1))
            current_contexts.append(row)
            ctx.intermediate_msgs.append(row)
            ctx.turned_messages.append(m)
        turn_ctrl.acknowledge_redirect()
        logger.info(f"[ActiveTurn] 注入 {len(msgs)} 条引导消息至当前回合（{trigger}）")
        return len(msgs)

    async def _llm_call_with_tools(
        self,
        provider,
        system_prompt: str,
        contexts: list[dict],
        user_msg: dict,
        tool_set=None,
        max_tool_rounds: int | None = None,
        event: AstrMessageEvent = None,
        turn_ctrl: ActiveTurnController | None = None,
        text_only_provider: bool = False,
    ):
        """调用 LLM，支持 Tool Call 循环与活跃回合实时引导。

        异步生成器：在工具调用期间立即 yield 中间结果给用户。
        最终 LLM 响应存储在 _tool_call_ctx (ContextVar) 中。

        活跃回合引导（参考 Hermes Agent 的 Redirect/Steer）：
        - 模型请求执行中收到新消息 → 取消当前请求，注入后在同一逻辑
          回合内重建输入重试（不消耗工具轮次、不触发 Provider Fallback）
        - 工具批次执行中收到新消息 → 批次完成的安全边界作为真实 user
          消息注入，下一 text_chat 可见；已执行工具结果保留不重放

        yield: ("text", str) | ("chain", list[component])
        """
        if max_tool_rounds is None:
            max_tool_rounds = self._cfg_int("max_tool_rounds", 10)

        ctx = _ToolCallContext()
        _tool_call_ctx.set(ctx)

        # 构建完整上下文
        current_contexts = list(contexts) + [user_msg]
        # 暴露给 tool handler（edit_image / view_image），让其能访问尚未落库的当前消息
        ctx.current_contexts = current_contexts
        # 记录本次实际使用的 Provider（含 fallback 切换），供 view_image 等工具判断模态能力
        ctx.current_provider = provider
        final_response = None

        if turn_ctrl is not None:
            await turn_ctrl.set_llm_loop(True)
        try:
            round_idx = 0
            while round_idx < max_tool_rounds:
                # 工具批次边界 / 回合起始: 注入上一边界后到达的引导消息
                if turn_ctrl is not None and await turn_ctrl.has_pending():
                    await self._inject_guidance_rows(
                        turn_ctrl,
                        current_contexts,
                        ctx,
                        text_only_provider,
                        trigger="tool-boundary",
                    )

                # 调用 LLM
                kwargs = {
                    "prompt": None,
                    "contexts": current_contexts,
                    "system_prompt": system_prompt,
                }

                # 传递工具集
                if tool_set and not tool_set.empty():
                    kwargs["func_tool"] = tool_set

                response = await self._guarded_text_chat(
                    turn_ctrl,
                    provider,
                    kwargs,
                    current_contexts,
                    ctx,
                    text_only_provider,
                )
                if response is _TURN_REDIRECTED:
                    # Redirect 重建不消耗工具轮次（迭代退款）；重建以"新消息
                    # 到达"为前提、且每轮重建前都完成一次模型调用，天然有界
                    continue

                if response is None:
                    ctx.final_response = final_response
                    return

                if hasattr(response, "role") and response.role == "err":
                    ctx.final_response = response
                    return

                # 累计本次调用的 Token 用量（估算）
                self._accumulate_token_usage(
                    ctx, system_prompt, current_contexts, response
                )

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
                    "tool_calls": self._dump_tool_calls(response),
                }
                current_contexts.append(assistant_msg)
                ctx.intermediate_msgs.append(assistant_msg)

                # 执行每个工具调用
                tool_calls_ids = response.tools_call_ids or []
                tool_calls_args = response.tools_call_args or []

                if turn_ctrl is not None:
                    await turn_ctrl.set_phase(TurnPhase.TOOL_RUNNING)

                for i, tool_name in enumerate(tool_calls_name):
                    tool_args = tool_calls_args[i] if i < len(tool_calls_args) else {}
                    tool_id = (
                        tool_calls_ids[i] if i < len(tool_calls_ids) else f"call_{i}"
                    )

                    # tool_args 已经是 dict，无需额外解析

                    # 每次工具调用前清空上一次工具的图片数据
                    self._last_tool_images = None

                    # 执行工具（Steer 消息不中断工具；批次边界统一注入）
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
                        logger.info(
                            f"[ChatEngine] 注入 {img_count} 张图片到 tool result"
                        )
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
                round_idx += 1
                logger.info(
                    f"[ChatEngine] Tool Call 轮次 {round_idx}: "
                    f"调用了 {len(tool_calls_name)} 个工具"
                )

            # 达到最大轮数，做一次无工具的最终调用
            logger.warning("[ChatEngine] 达到最大工具调用轮数，进行最终调用")
            while True:
                response = await self._guarded_text_chat(
                    turn_ctrl,
                    provider,
                    {
                        "prompt": None,
                        "contexts": current_contexts,
                        "system_prompt": system_prompt,
                    },
                    current_contexts,
                    ctx,
                    text_only_provider,
                )
                if response is not _TURN_REDIRECTED:
                    break
            self._accumulate_token_usage(ctx, system_prompt, current_contexts, response)
            ctx.final_response = response
        finally:
            if turn_ctrl is not None:
                await turn_ctrl.set_llm_loop(False)

    async def _check_tool_permission(
        self, tool_name: str, event: AstrMessageEvent | None
    ) -> str | None:
        """检查 AstrBot Dashboard 配置的工具权限，返回错误信息或 None (放行)。

        与官方 FunctionToolManager._check_tool_permission / _PermissionGuardedTool
        行为对齐：仅非 builtin 工具受 tool_permissions 配置约束（builtin 工具
        自带权限检查，本插件执行时走 tool.call() 分支同样会经过）；未配置的
        工具默认 member (放行)，仅显式配置为 admin 的工具要求管理员身份。
        """
        tool_manager = self.context.get_llm_tool_manager()
        try:
            if tool_manager.is_builtin_tool(tool_name):
                return None
        except Exception:
            # 判定失败时按官方默认 member 处理，不阻断
            return None

        try:
            perms_raw = await sp.global_get("tool_permissions", {})
        except Exception:
            return None
        defaults = perms_raw.get("_default", {}) if isinstance(perms_raw, dict) else {}
        if defaults.get(tool_name) != "admin":
            return None

        if event is None or not event.is_admin():
            sender_id = event.get_sender_id() if event is not None else "unknown"
            return (
                f"error: Permission denied. The tool '{tool_name}' requires admin "
                f"privileges. Your ID: {sender_id}. "
                "该工具被设置为仅管理员可用，请告知用户联系管理员执行，"
                "或在 AstrBot 管理面板调整该工具权限。"
            )
        return None

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

            # Dashboard 中配置为"仅管理员"的工具，非管理员调用时拒绝
            # (对齐 AstrBot 官方 _PermissionGuardedTool 的权限守卫)
            perm_error = await self._check_tool_permission(tool_name, event)
            if perm_error:
                logger.warning(f"[ChatEngine] 拒绝非管理员调用工具 '{tool_name}'")
                return json.dumps({"error": perm_error}, ensure_ascii=False)

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

        # === 会话执行权仲裁: 防抖定时器独立调用，不能绕过 in-flight 机制 ===
        _inflight_enabled = self._cfg_bool("enable_inflight_guidance", True)
        _turn_acquired = False
        if _inflight_enabled:
            _turn_acquired = await self.context_mgr.inflight_mgr.acquire_session(
                session_key
            )
            if not _turn_acquired:
                # 会话正在执行其他 AI 回合: 防抖消息优先实时进入活跃回合
                # （Redirect/Steer）；回合无法消费的才落缓冲走兜底
                _buffered = 0
                for m in messages:
                    ev = m.get("event")
                    if ev is None:
                        continue
                    _raw = (getattr(ev, "message_str", "") or "").strip()
                    _rc = self.context_mgr._extract_reply_context(ev)
                    _dt = f"[回复 {_rc}]{_raw}" if _rc else _raw
                    _stash_msg = InflightMessage(
                        id=uuid.uuid4().hex,
                        session_key=session_key,
                        sender_name=ev.get_sender_name() or "unknown",
                        sender_id=ev.get_sender_id() or "unknown",
                        time_str=datetime.now(SHANGHAI_TZ).strftime("%H:%M"),
                        text=_dt,
                        images=m.get("images") or [],
                        message_id=m.get("message_id", ""),
                        umo=m.get("umo", ""),
                        event=ev,
                        is_group=self.context_mgr.is_group_message(ev),
                        user_text=m.get("user_text", ""),
                    )
                    if await self._try_submit_active_turn(session_key, _stash_msg):
                        continue
                    await self.context_mgr.inflight_mgr.add_inflight_message(
                        session_key, _stash_msg
                    )
                    _buffered += 1
                if _buffered:
                    logger.info(
                        f"[Inflight] 会话 {session_key} 正在执行 AI 请求，"
                        f"{_buffered} 条防抖消息已暂存（本轮结束后自动处理）"
                    )
                return

        _turn_ok = False
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
            _turn_ok = isinstance(response_text, str) and response_text != ""

            if response_text is not None:
                logger.info(f"[Debounce] 处理完成: {session_key}")

        except Exception as e:
            logger.error(f"[Debounce] 顶层异常: {e}", exc_info=True)
        finally:
            if _turn_acquired:
                await self.context_mgr.inflight_mgr.release_session(session_key)
                # 回合成功结束后，自动处理期间暂存的消息（无需用户再发消息）
                if _turn_ok:
                    await self._maybe_followup_inflight(session_key)

    async def _maybe_followup_inflight(self, session_key: str) -> None:
        """边界兜底: 回合结束后自动处理无法在回合内消费的暂存消息。

        正常路径下，回合期间到达的消息经 Redirect/Steer 已被当前活跃回合
        实时消费；此处仅处理收尾阶段到达、引导预算外或竞态残留而落入
        inflight 缓冲的消息，保证不静默丢弃。

        将 inflight 暂存缓冲原子转投防抖缓冲：防抖窗口内继续合并新到达
        的消息，窗口到期后按正常防抖流程合并为一次后续 LLM 回合；该会话
        未启用防抖时直接发起后续回合。暂存消息作为后续回合的真实用户
        输入（保留发送者前缀、引用上下文与图片），而非背景引导。

        连续自动追答轮数达到 ``max_inflight_followup_rounds``（0 表示禁用
        自动追答）后停止转投，剩余暂存消息退回"随下次用户消息注入引导"
        的搭车语义；用户真实消息开启新一轮（进入防抖缓冲或直连
        回合）时轮数计数清零，被暂存的消息不清零。

        Args:
            session_key: 会话标识
        """
        if not self._cfg_bool("enable_inflight_guidance", True):
            return
        inflight_mgr = self.context_mgr.inflight_mgr
        if inflight_mgr.is_inflight(session_key):
            # 其他回合已接管本会话（其 stage_messages 会消费暂存消息）
            return
        if inflight_mgr.get_buffered_count(session_key) == 0:
            return
        max_rounds = max(0, self._cfg_int("max_inflight_followup_rounds", 5))
        rounds = self._inflight_followup_rounds.get(session_key, 0)
        if rounds >= max_rounds:
            logger.info(
                f"[Inflight] 会话 {session_key} 连续自动追答 {rounds} 轮已达上限，"
                f"暂存消息将随下次用户消息注入引导"
            )
            return
        msgs = await inflight_mgr.drain_buffer(session_key)
        if not msgs:
            return

        # 转投防抖数据（沿用正常防抖消息结构；缺 event/umo 的条目无法发起回合，跳过）
        pending: list[dict] = []
        for m in msgs:
            if m.event is None or not m.umo:
                logger.warning(
                    f"[Inflight] 暂存消息 {m.message_id} 缺少 event/umo，"
                    f"无法自动追答，放回缓冲待下次用户消息注入"
                )
                await inflight_mgr.add_inflight_message(session_key, m)
                continue
            pending.append(
                {
                    "user_text": m.user_text
                    or f"{m.sender_name}({m.sender_id})说：{m.text}",
                    "images": list(m.images),
                    "message_id": m.message_id,
                    "umo": m.umo,
                    "event": m.event,
                    "is_active": True,
                }
            )
        if not pending:
            return

        self._inflight_followup_rounds[session_key] = rounds + 1
        logger.info(
            f"[Inflight] 会话 {session_key} 有 {len(pending)} 条暂存消息，"
            f"自动发起后续回复（第 {rounds + 1}/{max_rounds} 轮）"
        )
        if self.debouncer and self.debouncer.should_debounce(msgs[-1].is_group):
            force = False
            for d in pending:
                force = await self.debouncer.add_message(session_key, d) or force
            if force:
                await self.debouncer.force_flush(session_key)
        else:
            # 该会话未启用防抖：无需合并窗口，直接发起后续回合
            asyncio.create_task(self._process_debounced_messages(session_key, pending))

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
        """获取 Provider，失败时按 AstrBot 配置的回退对话模型回退。

        回退顺序:
        1. 当前会话绑定的 Provider（通过 context.get_using_provider）
        2. AstrBot 配置的回退对话模型列表（见 :meth:`_get_fallback_provider_ids`）

        Args:
            umo: unified_message_origin
            log_tag: 日志前缀
        """
        from astrbot.core.provider import Provider as ProviderType

        # 首选: 当前会话的 Provider
        provider = self.context.get_using_provider(umo)
        if provider:
            return provider

        # 回退: 读取 AstrBot 配置中的回退对话模型
        try:
            fallback_ids = self._get_fallback_provider_ids(umo)
            if fallback_ids:
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

    def _get_fallback_provider_ids(self, umo: str = None) -> list:
        """读取 AstrBot 配置的回退对话模型 ID 列表（按会话路由配置档案）。

        兼容两代配置位置:
        - AstrBot >= 4.28: ``agent_runner.config.model.fallback_provider_ids``
          （旧键 ``provider_settings.fallback_chat_models`` 已被 migra_helper
          迁移删除，读取恒为空）；runner_type 非 local 时不适用回退模型
        - AstrBot 4.27:    ``provider_settings.fallback_chat_models``

        Args:
            umo: unified_message_origin

        Returns:
            回退 Provider ID 列表，可能为空。
        """
        try:
            config = self.context.get_config(umo)
            agent_runner = config.get("agent_runner", {}) or {}
            fallback_ids = []
            if agent_runner.get("runner_type", "local") == "local":
                fallback_ids = (
                    agent_runner.get("config", {})
                    .get("model", {})
                    .get("fallback_provider_ids", [])
                )
            # AstrBot 4.27 旧键兜底（>= 4.28 已迁移删除，读到空列表）
            if not fallback_ids:
                fallback_ids = config.get("provider_settings", {}).get(
                    "fallback_chat_models", []
                )
            return fallback_ids if isinstance(fallback_ids, list) else []
        except Exception as e:
            logger.warning(f"[ChatEngine] 读取回退模型配置失败: {e}")
            return []

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

        # 2. AstrBot 配置的回退对话模型列表（见 _get_fallback_provider_ids）
        try:
            fallback_ids = self._get_fallback_provider_ids(umo)
            if fallback_ids:
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
                await self.context_mgr.inflight_mgr.reset_session(session_key)

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

            await self.context_mgr.inflight_mgr.reset_session(session_key)

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
            await self.context_mgr.inflight_mgr.reset_session(session_key)

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
        """将 Image 组件转换为 base64 data URL。

        回退顺序：
        1. ``convert_to_base64`` → data URL
        2. 原始 URL —— 仅 http/https/data 形态；本地路径对画图 HTTP
           客户端与转述 Provider 均不可用
        3. 本地文件 —— 存在则读取转 data URL；不存在（如 AstrBot 临时
           文件已被清理）返回 None，交由上层回退链路兜底
        """
        try:
            b64 = await comp.convert_to_base64()
            if b64:
                return image_data_url_from_base64(b64)
        except Exception as e:
            logger.warning(f"[ChatEngine] 图片转 base64 失败: {e}")

        url = getattr(comp, "url", None)
        if isinstance(url, str) and url.startswith(("http://", "https://", "data:")):
            return url

        file_path = getattr(comp, "file", None)
        if isinstance(file_path, str) and file_path:
            data_url = await local_image_to_data_url(file_path)
            if data_url:
                return data_url
            logger.warning(f"[ChatEngine] 图片本地文件不可用: {file_path}")
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

    def _build_image_tool_guidance(self) -> str:
        """构建画图工具的使用指引，注入到 system prompt 中。"""
        return """

## Image Generation Guide

- **generate_image**: When the user asks you to draw, paint, create, design, or generate an image (e.g. "画一只猫", "帮我画张图", "生成一张夕阳的图片", "画个头像"), call `generate_image(prompt)`. The image is sent directly to the user — you only need to briefly acknowledge, do NOT try to render the image as text. Decide the prompt language and content yourself based on the user's request. Only call this tool when the user explicitly wants an image — do not invoke it for purely textual questions.
- **edit_image**: When the user provides or references an existing image and asks to modify, remix, redesign, or transform it (e.g. "把这张图背景换成海边", "参考这张图重画", "改一下这张", "去掉呆毛"), call `edit_image(message_id, prompt)`. Pick the reference image smartly: if the user is replying to / referencing the image you just generated or sent in your previous turn, use `message_id="last"`; otherwise use the [msg:ID] of the message carrying the reference image (current message's [msg:ID] if it has the image, else the historical [msg:ID]). The result is sent directly to the user and becomes the new "last" image. Multiple reference images in one message are supported (multi-image fusion).
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

    # 图片加载辅助

    @staticmethod
    async def _extract_urls_from_content(content: list, image_store) -> list[str]:
        """从消息 content 列表中提取图片 URL（解析 image_ref，直取 image_url）。"""
        urls: list[str] = []
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "image_ref":
                resolved = await image_store.resolve_image_ref(part)
                if resolved and resolved.get("type") == "image_url":
                    url = resolved.get("image_url", {}).get("url", "")
                    if url:
                        urls.append(url)
            elif part.get("type") == "image_url":
                url = part.get("image_url", {}).get("url", "")
                if url:
                    urls.append(url)
        return urls

    async def _remember_generated_image(
        self, session_key: str, image_bytes: bytes
    ) -> None:
        """记录会话最近生成图，供后续 edit_image(message_id="last") 使用。

        将生成图存入 ImageStore 得到 image_ref，按 session_key 记录。
        存储失败不影响主流程（图片仍会正常发送）。
        """
        try:
            image_store = self.context_mgr.image_store
            if not image_store:
                return
            data_url = f"data:image/png;base64,{base64.b64encode(image_bytes).decode()}"
            ref = await image_store.store_image(data_url)
            self._last_generated_images[session_key] = ref
        except Exception as e:
            logger.debug(f"[ChatEngine] 记录生成图失败: {e}")

    async def _resolve_message_image_urls(
        self, session_key: str, message_id: str
    ) -> tuple[str | None, list[str]]:
        """从当前 tool-call 内存上下文或数据库加载指定消息的图片 URL 列表。

        查找顺序：
        1. 当前轮内存上下文（``_tool_call_ctx.current_contexts``）——包含尚未落库的
           当前 user_msg，**用户刚发的图片只能从这里找到**
        2. 数据库历史上下文——已落库消息，图片以 ``image_ref`` 形式存储

        同一 message_id 可能同时存在于内存（图片可能被 ``_strip_history_images``
        剥成 ``[Image]`` 占位）和数据库（完整 image_ref），因此**逐一尝试所有匹配
        消息**，第一个提取到图片的就返回，避免被剥离版提前挡住。

        Returns:
            ``(error_msg, urls)``。error_msg 非 None 表示加载失败，urls 为空。
        """
        image_store = self.context_mgr.image_store
        if not image_store:
            return "Image storage is not available.", []

        # 收集候选消息：当前轮内存上下文 + 数据库历史
        candidates: list[dict] = []
        _ctx = _tool_call_ctx.get()
        if _ctx is not None:
            candidates.extend(_ctx.current_contexts or [])
        try:
            db_messages = await self.context_mgr.repo.get_context(session_key)
            candidates.extend(db_messages)
        except Exception as e:
            # 内存上下文仍可能命中，不直接失败
            logger.debug(f"[ChatEngine] 加载数据库上下文失败: {e}")

        # 找出所有同 message_id 的消息，逐一尝试提取图片
        # message_id 统一转 str 比较（event 侧可能是 int）
        message_id_str = str(message_id)
        matching = [
            msg
            for msg in candidates
            if str(msg.get("message_id", "")) == message_id_str
        ]
        if not matching:
            return (
                f"Message ID '{message_id}' not found in current session context. "
                "Available IDs can be found in [msg:ID] tags. "
                "To edit the last image you generated/sent, use message_id='last'."
            ), []

        for target_msg in matching:
            content = target_msg.get("content")
            if not isinstance(content, list):
                continue
            urls = await self._extract_urls_from_content(content, image_store)
            if urls:
                return None, urls

        return (
            "No extractable images in this message (they may have been stripped "
            "to [Image] placeholders or lost). To edit the last image you "
            "generated/sent, use message_id='last'."
        ), []

    async def _get_image_urls_for_message(
        self, event: AstrMessageEvent, message_id: str
    ) -> tuple[str | None, list[str]]:
        """获取指定 message_id 的图片 URL，供 edit_image / view_image 使用。

        优先级：
        1. 若 message_id 是当前 event 消息 → 直接从 event 提取原始图片组件
           （未经 ``sanitize_contexts_by_modalities`` 处理，图片完整）
        2. 当前消息为引用回复且组件图片不可用 → 沿 Reply 链按被引用
           message_id 查 :meth:`_resolve_reply_image_urls`（内存上下文 + 数据库）
        3. 回退到 :meth:`_resolve_message_image_urls`（内存上下文 + 数据库）

        第 1 步是关键：当 Provider 不支持 image 模态时，sanitize 会把当前
        user_msg 的图片剥离成 ``[Image]`` 文本，此时从 current_contexts 永远
        找不到图片，必须回到原始 event 提取。

        Returns:
            ``(error_msg, urls)``。error_msg 非 None 表示失败。
        """
        # message_id 可能为 str（LLM 工具参数）或 int（event.message_obj.message_id），
        # 统一转 str 比较避免 int/str 不等导致走不到 event 分支
        message_id_str = str(message_id)
        current_id = str(getattr(event.message_obj, "message_id", ""))
        if message_id_str == current_id:
            urls = await self._extract_image_urls(event)
            if urls:
                return None, urls
            logger.info(
                f"[ChatEngine] 当前消息图片提取为空: msg={message_id_str}，"
                "回退到上下文查找"
            )
            # 引用回复：当前消息图片组件不可用时，沿 Reply 链恢复被引用消息的图
            urls = await self._resolve_reply_image_urls(event)
            if urls:
                return None, urls
        session_key = self.context_mgr.build_session_key(event)
        err, urls = await self._resolve_message_image_urls(session_key, message_id_str)
        if err:
            logger.info(
                f"[ChatEngine] 图片查找失败: msg={message_id_str}, "
                f"current_id={current_id}, err={err}"
            )
        return err, urls

    async def _resolve_reply_image_urls(self, event: AstrMessageEvent) -> list[str]:
        """沿 Reply 链查找被引用消息的持久化图片。

        引用消息的 Image 组件常指向 AstrBot 临时文件，文件被清理后无法
        还原；被引用的原消息若已被本插件记录（上下文/数据库含 image_ref），
        可按其 message_id 恢复。
        """
        reply_ids = [
            str(comp.id)
            for comp in event.get_messages()
            if isinstance(comp, Reply) and comp.id is not None
        ]
        if not reply_ids:
            return []
        session_key = self.context_mgr.build_session_key(event)
        for rid in reply_ids:
            err, urls = await self._resolve_message_image_urls(session_key, rid)
            if not err and urls:
                logger.info(
                    f"[ChatEngine] 经引用消息恢复图片: reply_msg={rid}, "
                    f"images={len(urls)}"
                )
                return urls
        logger.info(f"[ChatEngine] 引用消息图片未找到: reply_ids={reply_ids}")
        return []

    # 图片转述 — 利用 AstrBot「默认图片转述模型」为纯文本 Provider 提供视觉能力

    def _get_caption_provider_config(self, umo: str) -> tuple[str, str]:
        """读取 AstrBot 全局配置中的图片转述设置。

        Returns:
            ``(provider_id, prompt)``:provider_id 为空表示未启用转述。
        """
        default_prompt = "Please describe the image using Chinese."
        try:
            config = self.context.get_config(umo)
            ps = getattr(config, "provider_settings", None) or {}
            prov_id = str(ps.get("default_image_caption_provider_id", "") or "")
            prompt = str(ps.get("image_caption_prompt", "") or default_prompt)
            return prov_id, prompt
        except Exception as e:
            logger.warning(f"[ChatEngine] 读取图片转述配置失败: {e}")
            return "", ""

    @staticmethod
    def _sha256_of_image_url(url: str) -> str | None:
        """计算图片内容的 sha256;仅支持 data URL,其他形态返回 None。"""
        if not url.startswith("data:"):
            return None
        try:
            _, b64 = url.split(",", 1)
            return hashlib.sha256(base64.b64decode(b64)).hexdigest()
        except Exception:
            return None

    async def _call_caption_provider(
        self, prov_id: str, prompt: str, url: str
    ) -> str | None:
        """调用 AstrBot 默认图片转述模型转述单张图片。

        Returns:
            转述文本;Provider 不可用或调用失败时返回 None(不缓存,可重试)。
        """
        from astrbot.core.provider import Provider as ProviderType

        try:
            prov = self.context.get_provider_by_id(prov_id)
        except Exception as e:
            logger.warning(f"[ChatEngine] 图片转述 Provider 获取失败 ({prov_id}): {e}")
            return None
        if prov is None or not isinstance(prov, ProviderType):
            logger.warning(
                f"[ChatEngine] 图片转述 Provider 不存在或类型不对: {prov_id}"
            )
            return None
        try:
            resp = await prov.text_chat(prompt=prompt, image_urls=[url])
            text = (resp.completion_text or "").strip() if resp else ""
            if text:
                return text
            logger.warning(f"[ChatEngine] 图片转述返回空结果: {prov_id}")
            return None
        except Exception as e:
            logger.warning(f"[ChatEngine] 图片转述调用失败: {e}")
            return None

    async def _get_image_caption(
        self,
        url: str,
        prov_id: str,
        prompt: str,
        turn_cache: dict[str, str],
    ) -> str | None:
        """获取单张图片的转述文本(带持久化缓存)。

        查询链: turn 缓存 → (sha256, provider, prompt_hash) 查 DB →
        调用转述 Provider → 回写 DB 与 turn 缓存。
        data URL 直接解码算 sha256;HTTP URL 无法离线计算哈希,直接转述不缓存。
        失败返回 None,调用方走现有兜底(图片标记 / 拒绝提示)。
        """
        cached = turn_cache.get(url)
        if cached:
            return cached

        prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]
        sha256 = self._sha256_of_image_url(url)
        repo = self.db.image_caption_repo if self.db else None

        if sha256 and repo:
            try:
                hit = await repo.find(sha256, prov_id, prompt_hash)
                if hit:
                    turn_cache[url] = hit
                    return hit
            except Exception as e:
                logger.warning(f"[ChatEngine] 图片转述缓存查询失败: {e}")

        caption = await self._call_caption_provider(prov_id, prompt, url)
        if not caption:
            return None

        if sha256 and repo:
            try:
                await repo.save(sha256, prov_id, prompt_hash, caption)
            except Exception as e:
                logger.warning(f"[ChatEngine] 图片转述缓存写入失败: {e}")
        turn_cache[url] = caption
        return caption

    async def _caption_user_msg_images(
        self, all_messages: list[dict], umo: str, turn_cache: dict[str, str]
    ) -> int:
        """为纯文本 Provider 转述当前用户消息中的图片块。

        把 user_msg(all_messages[-1])content 里的 image_url 块逐张独立转述,
        替换为 ``[Image #n: msg=<id>] <image_caption>...</image_caption>``
        文本块(保留 msg id 供 edit_image 按消息 ID 引用);转述失败的块保留
        原样,由 _mark_images_for_text_only_provider 兜底变 [Image: ...] 标记。
        未配置转述 Provider 时直接返回 0(功能关闭)。

        Returns:
            成功转述并替换的图片块数量。
        """
        prov_id, prompt = self._get_caption_provider_config(umo)
        if not prov_id:
            return 0

        user_msg = all_messages[-1] if all_messages else None
        if not isinstance(user_msg, dict):
            return 0
        content = user_msg.get("content")
        if not isinstance(content, list):
            return 0
        msg_id = str(user_msg.get("message_id", "") or "")

        replaced = 0
        img_idx = 0
        for i, part in enumerate(content):
            if not (isinstance(part, dict) and part.get("type") == "image_url"):
                continue
            img_idx += 1
            url = part.get("image_url", {}).get("url", "")
            caption = await self._get_image_caption(url, prov_id, prompt, turn_cache)
            if not caption:
                continue
            tag = (
                f"[Image #{img_idx}: msg={msg_id}]" if msg_id else f"[Image #{img_idx}]"
            )
            content[i] = {
                "type": "text",
                "text": f"{tag} <image_caption>{caption}</image_caption>",
            }
            replaced += 1
        return replaced

    async def _caption_view_image(
        self, event: AstrMessageEvent, message_id: str
    ) -> str:
        """纯文本 Provider 下的 view_image:用转述模型返回图片描述文本。

        未配置转述 Provider 时保持原有拒绝行为;已配置时逐张转述历史图片,
        描述文本作为工具结果返回给模型(不注入图片块,维持纯文本 Provider
        不接收 image_url 的原则)。转述结果与主路径共用持久化缓存。
        """
        prov_id, prompt = self._get_caption_provider_config(event.unified_msg_origin)
        if not prov_id:
            logger.info("[ChatEngine] 拒绝 view_image: 当前 Provider 不支持 image 模态")
            return (
                "你当前模型不支持视觉，无法查看图片。"
                "请直接基于对话中的文本内容回复，不要调用 view_image。"
            )

        err, urls = await self._get_image_urls_for_message(event, message_id)
        if err:
            return err

        parts: list[str] = []
        for i, url in enumerate(urls, 1):
            caption = await self._get_image_caption(url, prov_id, prompt, {})
            parts.append(f"[{i}] {caption}" if caption else f"[{i}] 转述失败")
        logger.info(
            f"[ChatEngine] view_image 转述: msg_id={message_id}, images={len(urls)}"
        )
        return (
            f"Image caption(s) of message [{message_id}] "
            "(your model has no vision; these descriptions were generated "
            "by the caption model):\n" + "\n".join(parts)
        )

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
        If your model supports vision, the image will be injected into your
        context for the next response; otherwise (text-only model), a caption
        model will describe the images in text for you.

        Args:
            message_id(string): The message ID containing the image. Shown as [msg:ID] tag in context messages.
        """
        # 纯文本 Provider 无法接收 image_url 注入（下一轮请求会 400），
        # 改用 AstrBot 默认图片转述模型把图片描述为文本返回
        _ctx = _tool_call_ctx.get()
        _provider = _ctx.current_provider if _ctx is not None else None
        if _provider is None:
            # 防御性回退：工具理论上只在 _llm_call_with_tools 内执行，ctx 必然存在
            try:
                _provider = self.context.get_using_provider()
            except Exception:
                _provider = None
        if _provider is not None:
            modalities = await self.context_mgr.get_modalities(_provider)
            if modalities and "image" not in modalities:
                return await self._caption_view_image(event, message_id)
        err, urls = await self._get_image_urls_for_message(event, message_id)
        if err:
            return err

        image_parts = [{"type": "image_url", "image_url": {"url": u}} for u in urls]
        # 暂存图片到实例变量，由 _llm_call_with_tools 直接嵌入当前 tool result
        self._last_tool_images = image_parts
        logger.info(
            f"[ChatEngine] 图片查看请求: msg_id={message_id}, images={len(image_parts)}"
        )
        return (
            f"Loaded {len(image_parts)} image(s) from message [{message_id}]. "
            "The images have been injected into your context for viewing."
        )

    # 画图/改图权限与配额

    def _build_image_quota_key(self, event: AstrMessageEvent) -> tuple[str, str]:
        """构建配额计数 key 与当日日期字符串(上海时区)。

        dimension=user: key = ``user:{sender_id}``(每个用户独立配额)
        dimension=session: key = ``session:{session_key}``(每会话共享配额)
        """
        dimension = str(self.config.get("image_gen_quota_dimension", "user"))
        if dimension == "session":
            quota_key = f"session:{self.context_mgr.build_session_key(event)}"
        else:
            quota_key = f"user:{event.get_sender_id()}"
        return quota_key, shanghai_now_iso()[:10]

    async def _check_image_tool_access(self, event: AstrMessageEvent) -> str | None:
        """检查画图/改图工具访问权限并预扣每日配额。

        返回 None 表示通过(配额已预扣);返回字符串为拒绝原因(回传给 LLM)。

        - 管理员(is_admin)直接放行,不扣配额
        - image_gen_admin_only=true 时普通用户拒绝
        - 否则原子 +1 预扣,超限则回退并拒绝

        采用「先 incr 再判断」而非「先 get 再 incr」,利用 incr 的原子性
        避免并发下多个请求同时通过检查导致超额。
        """
        # 管理员放行,不计配额
        if event.is_admin():
            return None

        # 普通用户:检查 admin_only 开关
        if self._cfg_bool("image_gen_admin_only", True):
            return (
                "Image generation tools are admin-only. "
                "Please ask an admin to enable access for normal users."
            )

        # 配额预扣:原子 +1 后判断是否超限
        quota_key, date_str = self._build_image_quota_key(event)
        limit = self._cfg_int("image_gen_daily_quota", 10)
        new_count = await self.db.image_quota_repo.incr_used(quota_key, date_str)
        if new_count > limit:
            # 超限,回退本次预扣
            await self.db.image_quota_repo.decr_used(quota_key, date_str)
            return (
                f"Daily image generation quota exceeded ({limit}/{limit}). "
                "Please try again tomorrow."
            )
        return None

    async def _refund_image_quota(self, event: AstrMessageEvent) -> None:
        """退还预扣的配额(图片 API 调用因网络/超时等异常失败时调用)。

        管理员无预扣,直接跳过。ImageGenError(业务错误,如内容违规)不退还,
        仅退还网络/超时等非业务异常,避免恶意试探不消耗配额。
        """
        if event.is_admin():
            return
        quota_key, date_str = self._build_image_quota_key(event)
        try:
            await self.db.image_quota_repo.decr_used(quota_key, date_str)
        except Exception as e:
            logger.warning(f"[ChatEngine] 退还画图配额失败: {e}")

    # 画图工具 — LLM Tool Call

    @filter.llm_tool(name="generate_image")
    async def tool_generate_image(
        self,
        event: AstrMessageEvent,
        prompt: str,
    ):
        """Generate an image from a text description and send it to the user.
        Use this when the user asks to draw, paint, create, design, or generate an image
        (e.g. "画一只猫", "帮我画张图", "生成一张夕阳的图片").
        The image is sent directly to the user — you only need to briefly acknowledge.

        Args:
            prompt(string): Text description of the image to generate.
        """
        if not self.image_client:
            return "Image generation is not enabled (api_base/api_key not configured)."

        # 权限与配额检查(管理员放行;普通用户预扣每日配额)
        access_err = await self._check_image_tool_access(event)
        if access_err:
            return access_err

        try:
            image_bytes = await self.image_client.generate(prompt)
        except ImageGenError as e:
            # 业务错误(如内容违规)— 不退还配额,防止恶意试探
            logger.warning(f"[ChatEngine] 画图失败: {e}")
            return f"Image generation failed: {e}"
        except Exception as e:
            # 网络/超时等异常 — 退还预扣配额
            await self._refund_image_quota(event)
            logger.error(f"[ChatEngine] 画图异常: {e}", exc_info=True)
            return f"Image generation error: {type(e).__name__}"

        # 通过 pending_sends 投递 Image 组件，由 _llm_call_with_tools 的回调发送
        # （与 execute_command 同机制，兼容 handle_all_messages 与 debounce 两条发送路径）
        _ctx = _tool_call_ctx.get()
        if _ctx is None:
            logger.error("[ChatEngine] 画图工具在无 tool-call 上下文中被调用")
            return "Image generated but no delivery channel available."

        _ctx.pending_sends.append(("chain", [Image.fromBytes(image_bytes)]))
        await self._remember_generated_image(
            self.context_mgr.build_session_key(event), image_bytes
        )
        logger.info(
            f"[ChatEngine] 画图已生成 ({len(image_bytes)} 字节)，已投递到发送队列"
        )
        return "Image generated and sent to the user."

    # 图片二次创作工具 — LLM Tool Call

    @filter.llm_tool(name="edit_image")
    async def tool_edit_image(
        self,
        event: AstrMessageEvent,
        message_id: str,
        prompt: str,
    ):
        """Edit or remix an existing image based on a text description.
        Use this when the user provides or references an image and asks to modify,
        redesign, remix, or transform it (e.g. "把这张图的背景换成海边", "参考这张图重画").
        The result is sent directly to the user — you only need to briefly acknowledge.

        Args:
            message_id(string): The message ID containing the reference image(s),
                shown as [msg:ID] tag in context messages. Use the special value "last"
                to edit the most recent image you generated/sent in this session
                (useful when the user replies to your generated image and asks to tweak it).
            prompt(string): Text description of how to edit or remix the image.
        """
        if not self.image_client:
            return "Image editing is not enabled (api_base/api_key not configured)."

        session_key = self.context_mgr.build_session_key(event)

        # message_id="last"：取本会话最近一张生成图（用户回复/引用你刚发的图时常用）
        if message_id == "last":
            image_store = self.context_mgr.image_store
            ref = self._last_generated_images.get(session_key)
            if not ref or not image_store:
                return (
                    "No previous generated image in this session. "
                    "Ask the user to provide or reference an image."
                )
            resolved = await image_store.resolve_image_ref(ref)
            if not resolved or resolved.get("type") != "image_url":
                return "The previous generated image has expired."
            image_urls = [resolved["image_url"]["url"]]
        else:
            err, image_urls = await self._get_image_urls_for_message(event, message_id)
            if err:
                return err

        # 权限与配额检查(管理员放行;普通用户预扣每日配额)
        # 放在图片 URL 解析之后:URL 解析失败时不扣配额,避免白扣
        access_err = await self._check_image_tool_access(event)
        if access_err:
            return access_err

        try:
            image_bytes = await self.image_client.edit(prompt, image_urls)
        except ImageGenError as e:
            # 业务错误(如内容违规)— 不退还配额
            logger.warning(f"[ChatEngine] 二次创作失败: {e}")
            return f"Image editing failed: {e}"
        except Exception as e:
            # 网络/超时等异常 — 退还预扣配额
            await self._refund_image_quota(event)
            logger.error(f"[ChatEngine] 二次创作异常: {e}", exc_info=True)
            return f"Image editing error: {type(e).__name__}"

        # 投递结果图片到发送队列（与 generate_image 同机制）
        _ctx = _tool_call_ctx.get()
        if _ctx is None:
            logger.error("[ChatEngine] 二次创作在无 tool-call 上下文中被调用")
            return "Image edited but no delivery channel available."

        _ctx.pending_sends.append(("chain", [Image.fromBytes(image_bytes)]))
        await self._remember_generated_image(session_key, image_bytes)
        logger.info(
            f"[ChatEngine] 二次创作已生成 ({len(image_bytes)} 字节)，"
            f"参考图 {len(image_urls)} 张"
        )
        return "Image edited and sent to the user."

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
