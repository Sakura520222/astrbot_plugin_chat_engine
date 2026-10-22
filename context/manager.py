"""Context manager — orchestrates session key building, context loading/saving,
user message formatting, and compression triggering.
"""

import asyncio
import re

from astrbot.api import logger
from astrbot.api.message_components import Reply
from astrbot.api.platform import MessageType
from astrbot.core.utils.llm_metadata import LLM_METADATAS

from ..db.persona_repo import PersonaRepository
from ..db.session_repo import SessionRepository
from ..utils.config import cfg_int
from .compressor import BaseCompressor, ContextCompressorFactory
from .inflight import InflightManager
from .semantics import (
    MessageSemantics,
    NicknameCache,
    ReplyContextInfo,
    extract_message_semantics,
    format_group_header,
    render_reply_marker,
    reply_needs_resolution,
    sanitize_untrusted,
)
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
        astrbot_config_getter=None,
    ):
        self.repo = session_repo
        self.persona_repo = persona_repo
        self.config = config
        self.provider_getter = provider_getter
        self.image_store = image_store
        self.astrbot_config_getter = astrbot_config_getter
        self.compressor: BaseCompressor = ContextCompressorFactory.create(
            config, provider_getter
        )
        self.token_counter = TokenEstimator()
        self.inflight_mgr = InflightManager()
        self.nicknames = NicknameCache()
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

    def format_user_message(
        self,
        event,
        semantics: MessageSemantics | None = None,
        is_active: bool = True,
    ) -> str:
        """格式化用户消息。添加用户标识前缀和引用消息上下文。

        格式: "{可信元数据头}{NAME}({ID})说：[回复 {引用者}: {引用内容}]\\n消息内容"

        群聊消息前置插件生成的可信元数据头（〔群聊消息〕/〔群聊旁观消息〕，
        含发送者、消息 ID、@对象、是否直接@Bot、触发方式，见
        context/semantics.py），当前消息与历史消息遵循同一契约；私聊保持
        原格式（无提及混淆问题）。用户原文中的保留定界符会被替换，无法
        伪造元数据头。

        Args:
            event: 消息事件
            semantics: 调用方已提取的消息语义；None 时自行提取（此时
                is_active 建议显式传入，被动消息须传 False）
            is_active: 该消息是否触发本轮回复（决定头标签与触发字段）
        """
        text = sanitize_untrusted(event.message_str or "")

        # 提取引用消息上下文：已提供语义时使用（可能经上下文还原补全），
        # 否则从原始 Reply 组件提取
        if semantics is not None and semantics.reply_to is not None:
            reply_context = render_reply_marker(semantics.reply_to)
        else:
            reply_context = self._extract_reply_context(event)

        fmt = self.config.get("user_id_format", "{{user}{NAME}({ID})}说：")
        name = event.get_sender_name() or "unknown"
        uid = event.get_sender_id() or "unknown"
        prefix = fmt.replace("{NAME}", name).replace("{ID}", uid)

        # 群聊前置可信元数据头；私聊保持原格式
        header = ""
        if self.is_group_message(event):
            sem = semantics or extract_message_semantics(event, is_group=True)
            header = format_group_header(sem, is_active)

        if reply_context:
            return f"{header}{prefix}[回复 {reply_context}]{text}"
        return f"{header}{prefix}{text}"

    def _extract_reply_context(self, event) -> str:
        """从消息事件中提取引用消息的摘要信息（含被引用者身份）。"""
        try:
            for comp in event.get_messages():
                if isinstance(comp, Reply):
                    sender = sanitize_untrusted(comp.sender_nickname or "")
                    sid = str(comp.sender_id or "").strip()
                    if sid in ("", "0", "None"):
                        sid = ""
                    quoted_text = sanitize_untrusted((comp.message_str or "").strip())

                    # 引用者标识带稳定 ID，昵称重复/变化时可区分
                    if sid:
                        sender = f"{sender}({sid})" if sender else sid

                    if sender and quoted_text:
                        return f"{sender}: {quoted_text}"
                    elif quoted_text:
                        return quoted_text
                    elif sender:
                        return f"{sender}的消息"
        except Exception:
            pass
        return ""

    # 引用目标还原 — 平台未提供 Reply 详情时从已记录上下文恢复

    # 行首的可信元数据头块：`〔…〕`（插件生成，用户原文不会出现）
    _ROW_HEADER_BLOCK_RE = re.compile(r"^〔([^〕]*)〕")
    # 头块内的发送者字段：`发送者 樱花(1001)`（昵称可含空格）
    _ROW_HEADER_SENDER_RE = re.compile(r"发送者\s*(.*?)\(([^()]*)\)")
    # 默认 user_id_format 前缀：`{{user}樱樱(1001)}说：`（捕获昵称与 ID）
    _ROW_LEGACY_PREFIX_RE = re.compile(r"^\{\{user\}(.+?)\(([^()]*)\)\}说：")
    # 无模板裸前缀：`樱樱(1001)说：`（自定义 user_id_format 去掉 {{user}} 时）
    _ROW_BARE_PREFIX_RE = re.compile(r"^[^（()]{1,30}\([^()（）]{1,64}\)说：")

    @staticmethod
    def _row_content_text(row: dict) -> str:
        """取消息行的纯文本部分（content 为 str 或 text 块列表）。"""
        content = row.get("content", "")
        if isinstance(content, list):
            return " ".join(
                p.get("text", "")
                for p in content
                if isinstance(p, dict) and p.get("type") == "text"
            )
        return content if isinstance(content, str) else ""

    def _row_identity(self, row: dict) -> tuple[str, str]:
        """从已记录的消息行解析发送者 (sender_id, sender_name)。

        优先读结构化 semantics 字段；缺失时从行首可信元数据头内的
        `发送者 名(ID)` 字段还原（仅匹配行首〔〕头块，旧记录用户正文中
        自行编写的"发送者"字样不会被采信）；再退回旧格式
        `{{user}名(ID)}说：` 前缀。解析失败返回空串，不凭空补造。
        """
        sem = row.get("semantics")
        if isinstance(sem, dict):
            sid = str(sem.get("sender_id", "") or "").strip()
            name = str(sem.get("sender_name", "") or "").strip()
            if sid or name:
                return sid, name
        text = self._row_content_text(row)
        header = self._ROW_HEADER_BLOCK_RE.match(text)
        if header:
            m = self._ROW_HEADER_SENDER_RE.search(header.group(1))
            if m:
                sid = m.group(2).strip()
                name = m.group(1).strip()
                if sid or name:
                    return sid, name
        m = self._ROW_LEGACY_PREFIX_RE.match(text)
        if m:
            return m.group(2).strip(), m.group(1).strip()
        return "", ""

    def _row_body_text(self, row: dict) -> str:
        """取消息行去除元数据头与发送者前缀后的正文（用于引用原文还原）。"""
        text = self._row_content_text(row)
        # 剥离开头的可信元数据头与发送者前缀，保留正文（含嵌套引用）
        text = re.sub(r"^〔[^〕]*〕", "", text)
        text = self._ROW_LEGACY_PREFIX_RE.sub("", text)
        text = self._ROW_BARE_PREFIX_RE.sub("", text)
        return text.strip()

    async def resolve_reply_target(
        self, session_key: str, reply: ReplyContextInfo, event
    ) -> bool:
        """按 message_id 从本会话已记录上下文还原引用目标。

        平台适配器未提供 Reply 详情时（如 aiocqhttp 的 get_reply 关闭或
        get_msg 调用失败，Reply 仅有 id），引用的消息若曾被本插件记录
        （user / observed 行），可按 message_id 恢复其作者与正文，并据此
        判定是否引用了 Bot 自己（sender_id 与 get_self_id 比较）。

        只填充缺失字段，不覆盖平台已提供的信息；未找到匹配行返回 False。
        找不到的引用大概率指向 Bot 自己的回复（bot 回复不携带平台
        message_id，无法回查），此时保守标记为未记录，不冒认作者。

        Args:
            session_key: 会话标识
            reply: 待补全的引用目标（原地修改）
            event: 原始消息事件（读取 self_id）

        Returns:
            是否从已记录上下文中找到了匹配的消息行。
        """
        if not reply_needs_resolution(reply):
            return False
        try:
            self_id = str(event.get_self_id() or "").strip()
        except Exception:
            self_id = ""
        try:
            rows = await self.repo.get_context(session_key)
        except Exception:
            return False
        target_id = reply.message_id
        for row in reversed(rows):
            if row.get("role") not in ("user", "observed"):
                continue
            if str(row.get("message_id", "") or "") != target_id:
                continue
            sid, name = self._row_identity(row)
            if sid and not reply.sender_id:
                reply.sender_id = sid
                reply.is_bot = bool(self_id) and sid == self_id
                # 学习引用作者昵称（平台未提供时的次优来源）
                if name:
                    try:
                        self.nicknames.remember(
                            str(event.get_platform_name() or ""), sid, name
                        )
                    except Exception:
                        pass
            if name and not reply.sender_name:
                reply.sender_name = name
            body = self._row_body_text(row)
            if body and not reply.text:
                reply.text = sanitize_untrusted(body)[:200]
            return True
        return False

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

        查找顺序（对齐 AstrBot 本体的三级逻辑 astr_main_agent）：
        1. provider 配置中用户手填的 max_context_tokens；
        2. AstrBot LLM_METADATAS 模型元数据 — 本插件抑制了 AstrBot 默认
           agent 流程，本体在流程内的回填不会执行，需自行查询。元数据由
           AstrBot 启动时异步拉取并就地更新，进程内共享，直接读取零开销；
        3. 缓存的备选值（provider 不可用时兜底）；
        4. AstrBot 全局 provider_settings.fallback_max_context_tokens。

        取到有效值时缓存到实例变量并回填到插件配置，确保 provider 不可用
        时也有合理的备选值，同时 WebUI 可显示检测到的值。
        """
        try:
            max_tokens = provider.provider_config.get("max_context_tokens", 0)
        except Exception:
            max_tokens = 0
        if max_tokens <= 0:
            # 未手填（0 = 自动）→ 从模型元数据查询
            try:
                model_info = LLM_METADATAS.get(provider.get_model())
                if model_info:
                    max_tokens = model_info["limit"]["context"]
            except Exception:
                max_tokens = 0
        if max_tokens > 0:
            self._cached_max_context_tokens = max_tokens
            # 仅在值变化时回填并持久化，避免每条消息都写磁盘
            if self.config.get("fallback_max_context_tokens") != max_tokens:
                self.config["fallback_max_context_tokens"] = max_tokens
                try:
                    loop = asyncio.get_running_loop()
                    await loop.run_in_executor(None, self.config.save_config)
                except Exception:
                    pass
            return max_tokens
        # provider 未报告，使用缓存的备选值
        if self._cached_max_context_tokens is not None:
            return self._cached_max_context_tokens
        # AstrBot 全局 fallback（与本体的最终兜底一致，默认 128000）
        if self.astrbot_config_getter:
            try:
                astrbot_config = self.astrbot_config_getter()
                settings = getattr(astrbot_config, "provider_settings", None)
                if settings:
                    return cfg_int(settings, "fallback_max_context_tokens", 128000)
            except Exception:
                pass
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

        await self.repo.save_context(session_key, messages)

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

    async def append_assistant_message(
        self,
        session_key: str,
        assistant_msg: dict,
        provider=None,
    ) -> None:
        """追加一条助手消息（用于主动回复等无对应用户输入的场景）。

        与 append_and_save 的区别：不追加伪造的 user 触发消息，避免把
        "[Proactive Reply Trigger] ..." 这类系统标记伪装成用户发言而污染
        上下文，进而误导后续 LLM 对话与 AI 判断。主动回复只需让 bot 记得
        自己说过什么即可，不需要虚构一条用户消息。
        """
        await self._load_compress_save(session_key, [assistant_msg], provider=provider)

    def reload_compressor(self):
        """重新加载压缩器 (配置变更后调用)"""
        self.compressor = ContextCompressorFactory.create(
            self.config, self.provider_getter
        )
