"""Context manager — orchestrates session key building, context loading/saving,
user message formatting, and compression triggering.
"""

import asyncio

from astrbot.api import logger
from astrbot.api.message_components import Reply
from astrbot.api.platform import MessageType

from ..db.persona_repo import PersonaRepository
from ..db.session_repo import SessionRepository
from .compressor import BaseCompressor, ContextCompressorFactory
from .token_counter import TokenEstimator


class ChatContextManager:
    """上下文管理器 — 管理会话、用户标识、上下文存取与压缩

    会话锁生命周期: _session_locks 按 session_key (platform:group_id / platform:private:sender_id)
    索引，随会话数增长但不会无限膨胀。同一插件实例中活跃会话数有限，无需定期清理。
    """

    _session_locks: dict[str, asyncio.Lock]

    def __init__(
        self,
        session_repo: SessionRepository,
        persona_repo: PersonaRepository,
        config: dict,
        provider_getter=None,
        image_store=None,
    ):
        self.repo = session_repo
        self.persona_repo = persona_repo
        self.config = config
        self.provider_getter = provider_getter
        self.image_store = image_store
        self.compressor: BaseCompressor = ContextCompressorFactory.create(
            config, provider_getter
        )
        self.token_counter = TokenEstimator()
        self._cached_modalities: list[str] | None = None
        self._cached_max_context_tokens: int | None = None
        self._session_locks: dict[str, asyncio.Lock] = {}

    def get_session_lock(self, session_key: str) -> asyncio.Lock:
        """获取会话级别的异步锁，确保同一会话的消息串行处理。

        锁条目随会话数增长（用户/群聊数量级），不会无限膨胀。
        """
        if session_key not in self._session_locks:
            self._session_locks[session_key] = asyncio.Lock()
        return self._session_locks[session_key]

    def build_session_key(self, event) -> str:
        """根据消息事件构建会话 key。

        群聊: "{platform}:{group_id}"
        私聊: "{platform}:private:{sender_id}"
        """
        platform = event.get_platform_name() or "unknown"
        group_id = event.get_group_id()
        if group_id:
            return f"{platform}:{group_id}"
        sender_id = event.get_sender_id() or "unknown"
        return f"{platform}:private:{sender_id}"

    def is_group_message(self, event) -> bool:
        """判断是否为群聊消息"""
        try:
            return event.get_message_type() == MessageType.GROUP_MESSAGE
        except Exception:
            return event.get_group_id() is not None

    def format_user_message(self, event) -> str:
        """格式化用户消息。添加用户标识前缀和引用消息上下文。

        格式: "{{user}{昵称}({ID})}说：[回复 {引用者}: {引用内容}]\n消息内容"
        """
        text = event.message_str or ""

        # 提取引用消息上下文
        reply_context = self._extract_reply_context(event)

        fmt = self.config.get("user_id_format", "{{user}{NAME}({ID})}说：")
        name = event.get_sender_name() or "unknown"
        uid = event.get_sender_id() or "unknown"
        prefix = fmt.replace("{NAME}", name).replace("{ID}", uid)

        if reply_context:
            return f"{prefix}[回复 {reply_context}]{text}"
        return f"{prefix}{text}"

    def _extract_reply_context(self, event) -> str:
        """从消息事件中提取引用消息的摘要信息。"""
        try:
            for comp in event.get_messages():
                if isinstance(comp, Reply):
                    sender = comp.sender_nickname or ""
                    quoted_text = comp.message_str or ""

                    if sender and quoted_text:
                        return f"{sender}: {quoted_text}"
                    elif quoted_text:
                        return quoted_text
                    elif sender:
                        return f"{sender}的消息"
        except Exception:
            pass
        return ""

    def should_respond(self, event) -> bool:
        """判断是否应该响应此消息。

        - 私聊: 始终响应
        - 群聊: 根据 require_at_in_group 配置决定
        """
        if not self.is_group_message(event):
            return True  # 私聊始终响应

        if not self.config.get("require_at_in_group", True):
            return True  # 配置为不需要@，响应所有群消息

        return event.is_at_or_wake_command  # 需要@Bot

    async def load_context(self, session_key: str) -> list[dict]:
        """从数据库加载上下文，并将 image_ref 引用解析为 data URL。"""
        try:
            messages = await self.repo.get_context(session_key)
            return await self._resolve_images_for_messages(messages)
        except Exception as e:
            logger.error(f"[ChatEngine] 加载上下文失败 [{session_key}]: {e}")
            return []

    async def get_max_context_tokens(self, provider) -> int:
        """获取模型最大上下文 Token 数。

        优先从 provider 配置获取，成功时缓存到实例变量并回填到插件配置，
        确保 provider 不可用时也有合理的备选值，同时 WebUI 可显示检测到的值。
        """
        try:
            max_tokens = provider.provider_config.get("max_context_tokens", 0)
        except Exception:
            max_tokens = 0
        if max_tokens > 0:
            self._cached_max_context_tokens = max_tokens
            # 仅在值变化时回填并持久化，避免每条消息都写磁盘
            if self.config.get("fallback_max_context_tokens") != max_tokens:
                self.config["fallback_max_context_tokens"] = max_tokens
                try:
                    loop = asyncio.get_event_loop()
                    await loop.run_in_executor(None, self.config.save_config)
                except Exception:
                    pass
            return max_tokens
        # provider 未报告，使用缓存的备选值
        if self._cached_max_context_tokens is not None:
            return self._cached_max_context_tokens
        return 128000

    async def get_modalities(self, provider) -> list[str]:
        """获取模型支持的模态能力列表。

        优先从 provider 配置获取，成功时缓存到实例变量，
        确保 provider 不可用时也有合理的备选值。

        返回值示例: ["text", "tool_use", "image"]
        默认值（保守策略）: ["text", "tool_use"] — 不含 image，避免发送不支持的内容。
        """
        default_modalities = ["text", "tool_use"]
        try:
            modalities = provider.provider_config.get("modalities", None)
        except Exception:
            modalities = None
        if modalities and isinstance(modalities, list) and len(modalities) > 0:
            self._cached_modalities = modalities
            return modalities
        # provider 未报告，使用缓存的备选值
        if self._cached_modalities:
            return self._cached_modalities
        return default_modalities

    async def _store_images_for_messages(self, messages: list[dict]) -> list[dict]:
        """将消息中的 inline 图片 (data URL) 替换为 image_ref 引用。

        遍历每条消息的 content 列表，将 data: 开头的 image_url 替换为 image_ref。
        image_store 为 None 时跳过（向后兼容）。
        """
        if not self.image_store:
            return messages
        stored = []
        for msg in messages:
            content = msg.get("content")
            if isinstance(content, list):
                msg = {
                    **msg,
                    "content": await self.image_store.store_message_images(content),
                }
            stored.append(msg)
        return stored

    async def _resolve_images_for_messages(self, messages: list[dict]) -> list[dict]:
        """将消息中的 image_ref 引用还原为 image_url data URL。

        用于发送给 LLM 和 WebUI 展示。
        image_store 为 None 时跳过（向后兼容旧数据）。
        """
        if not self.image_store:
            return messages
        resolved = []
        for msg in messages:
            content = msg.get("content")
            if isinstance(content, list):
                has_ref = any(
                    isinstance(p, dict) and p.get("type") == "image_ref"
                    for p in content
                )
                if has_ref:
                    msg = {
                        **msg,
                        "content": await self.image_store.resolve_message_images(
                            content
                        ),
                    }
            resolved.append(msg)
        return resolved

    async def _load_compress_save(
        self,
        session_key: str,
        new_messages: list[dict],
        provider=None,
    ) -> list[dict]:
        """加载上下文、追加消息、压缩检查、保存。

        被 append_and_save 和 record_passive_message 共用的内部方法。
        未传入 provider 时会尝试通过 provider_getter 自行获取，以确保压缩逻辑正常工作。
        """
        try:
            existing = await self.repo.get_context(session_key)
        except Exception:
            existing = []

        messages = existing + new_messages

        # 压缩检查：未传入 provider 时尝试自行获取
        _provider = None
        try:
            _provider = provider
            if _provider is None and self.provider_getter:
                try:
                    _provider = self.provider_getter()
                except Exception:
                    pass
            max_tokens = 0
            if _provider:
                max_tokens = await self.get_max_context_tokens(_provider)
            messages = await self.compressor.compress(messages, max_tokens)
        except Exception as e:
            _provider_name = (
                type(_provider).__name__ if _provider is not None else "None"
            )
            logger.error(
                f"[ChatEngine] 上下文压缩失败 [session={session_key}, "
                f"provider={_provider_name}, messages={len(messages)}]: "
                f"{e}，将保存未压缩的上下文",
                exc_info=True,
            )

        # 保存前: 将新消息中的 inline 图片替换为 image_ref 引用
        messages = await self._store_images_for_messages(messages)

        try:
            await self.repo.save_context(session_key, messages)
        except Exception as e:
            logger.error(f"[ChatEngine] 保存上下文失败 [{session_key}]: {e}")

        return messages

    async def append_and_save(
        self,
        session_key: str,
        user_msg: dict,
        assistant_msg: dict,
        provider=None,
    ) -> list[dict]:
        """追加用户+助手消息，运行压缩检查，保存到数据库。"""
        return await self._load_compress_save(
            session_key, [user_msg, assistant_msg], provider=provider
        )

    async def record_passive_message(
        self,
        session_key: str,
        user_msg: dict,
        provider=None,
    ) -> None:
        """记录被动 (未触发回复) 的用户消息到上下文。

        仅追加一条 user 消息，不产生 assistant 回复。
        同样会触发压缩检查以控制上下文长度。
        未传入 provider 时会自动通过 provider_getter 获取，确保压缩正常工作。
        """
        await self._load_compress_save(session_key, [user_msg], provider=provider)

    def reload_compressor(self):
        """重新加载压缩器 (配置变更后调用)"""
        self.compressor = ContextCompressorFactory.create(
            self.config, self.provider_getter
        )
