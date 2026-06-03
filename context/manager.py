"""Context manager — orchestrates session key building, context loading/saving,
user message formatting, and compression triggering.
"""

from astrbot.api import logger
from astrbot.api.platform import MessageType

from ..db.persona_repo import PersonaRepository
from ..db.session_repo import SessionRepository
from .compressor import BaseCompressor, ContextCompressorFactory
from .token_counter import TokenEstimator


class ChatContextManager:
    """上下文管理器 — 管理会话、用户标识、上下文存取与压缩"""

    def __init__(
        self,
        session_repo: SessionRepository,
        persona_repo: PersonaRepository,
        config: dict,
        provider_getter=None,
    ):
        self.repo = session_repo
        self.persona_repo = persona_repo
        self.config = config
        self.provider_getter = provider_getter
        self.compressor: BaseCompressor = ContextCompressorFactory.create(
            config, provider_getter
        )
        self.token_counter = TokenEstimator()

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
        """格式化用户消息。添加用户标识前缀。

        格式: "{{user}{昵称}({ID})}说：消息内容"
        群聊和私聊都添加用户标识前缀，确保 AI 能识别用户。
        """
        text = event.message_str or ""

        fmt = self.config.get("user_id_format", "{{user}{NAME}({ID})}说：")
        name = event.get_sender_name() or "unknown"
        uid = event.get_sender_id() or "unknown"
        prefix = fmt.replace("{NAME}", name).replace("{ID}", uid)
        return f"{prefix}{text}"

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
        """从数据库加载上下文"""
        try:
            return await self.repo.get_context(session_key)
        except Exception as e:
            logger.error(f"[ChatEngine] 加载上下文失败 [{session_key}]: {e}")
            return []

    async def get_max_context_tokens(self, provider) -> int:
        """获取模型最大上下文 Token 数。

        优先从 provider 配置获取，成功时自动回填到插件配置，
        确保 provider 不可用时也有合理的备选值。
        """
        max_tokens = 0
        try:
            max_tokens = provider.provider_config.get("max_context_tokens", 0)
        except Exception:
            pass
        if max_tokens > 0:
            # 自动回填到插件配置，确保 provider 不可用时也有合理的备选值
            self.config["fallback_max_context_tokens"] = max_tokens
            return max_tokens
        # provider 未报告，使用配置中的备选值（可能由之前自动回填）
        try:
            max_tokens = int(self.config.get("fallback_max_context_tokens", 128000))
        except (ValueError, TypeError):
            max_tokens = 128000
        return max_tokens

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
            _provider_name = type(_provider).__name__ if _provider is not None else "None"
            logger.error(
                f"[ChatEngine] 上下文压缩失败 [session={session_key}, "
                f"provider={_provider_name}, messages={len(messages)}]: "
                f"{e}，将保存未压缩的上下文",
                exc_info=True,
            )

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
        await self._load_compress_save(
            session_key, [user_msg], provider=provider
        )

    def reload_compressor(self):
        """重新加载压缩器 (配置变更后调用)"""
        self.compressor = ContextCompressorFactory.create(
            self.config, self.provider_getter
        )
