"""群聊消息语义模型 — 从平台原生消息组件提取消息的四个语义维度（Issue #10）。

维度彼此独立，不得压缩为单一布尔值：
- Sender（发送者）: 昵称、稳定用户 ID、消息 ID
- Mentions（提及对象）: 消息中实际 @ 的全部对象（Bot 自身、其他成员、@全体）
- Reply-to（引用目标）: 被引用消息的 ID、作者与原文摘要（引用 ≠ 提及）
- Trigger（触发方式）: 本条消息为何触发 AI（直接@Bot / 引用回复Bot / @全体 /
  唤醒前缀 / 无需@配置 / 私聊），与"是否触发"（is_active）分开表达

提取只信任平台原生组件（At / AtAll / Reply）与事件字段，**不扫描纯文本**：
用户手打的 "@昵称" 文本不会被认定为真实提及。

可信边界：消息头使用全角括号〔〕作为插件生成元数据的定界符；用户原文
进入消息头前统一把〔〕替换为（）（见 sanitize_untrusted），用户无法在原文
中伪造可信元数据行。
"""

from dataclasses import dataclass, field

from astrbot.api.message_components import At, Reply

# --- 触发方式常量（触发为空字符串表示被动消息，未触发 AI） ---
TRIGGER_AT_BOT = "直接@Bot"
TRIGGER_REPLY_TO_BOT = "引用回复Bot"
TRIGGER_AT_ALL = "@全体"
TRIGGER_WAKE = "唤醒前缀"
TRIGGER_NO_AT_POLICY = "无需@配置"
TRIGGER_PRIVATE = "私聊"

# --- 消息头标签 ---
GROUP_ACTIVE_TAG = "群聊消息"
GROUP_PASSIVE_TAG = "群聊旁观消息"

# 可信元数据定界符（全角括号，用户原文中被 sanitize_untrusted 替换）
_RESERVED_L = "〔"
_RESERVED_R = "〕"

# 引用消息原文在序列化时的最大长度（防止长引用撑爆上下文）
_REPLY_TEXT_LIMIT = 200

# 昵称缓存容量上限（超出时淘汰最旧条目；按 platform:user_id 索引）
_NICKNAME_CACHE_MAX = 2000


def sanitize_untrusted(text: str) -> str:
    """把用户原文中的保留定界符替换为普通全角括号，防止伪造可信元数据头。"""
    if not text:
        return text
    return text.replace(_RESERVED_L, "（").replace(_RESERVED_R, "）")


class NicknameCache:
    """平台用户昵称缓存 — 从消息流中学习（发送者 / At 组件 / Reply 作者）。

    用于补全平台适配器缺失的 At 昵称（如 aiocqhttp 群成员信息查询失败时
    ``At.name`` 为空）：昵称按稳定用户 ID 记忆，最新值覆盖旧值（昵称变更
    自动跟进）。仅缓存"插件亲眼见过"的昵称，不做任何外部查询。
    """

    def __init__(self, max_entries: int = _NICKNAME_CACHE_MAX):
        self._max = max_entries
        self._names: dict[str, str] = {}

    def remember(self, platform: str, user_id: str, nickname: str) -> None:
        if not platform or not user_id or not nickname or user_id == "all":
            return
        key = f"{platform}:{user_id}"
        if key in self._names and self._names[key] == nickname:
            return
        if len(self._names) >= self._max and key not in self._names:
            self._names.pop(next(iter(self._names)))  # 淘汰最旧条目
        self._names[key] = nickname

    def resolve(self, platform: str, user_id: str) -> str:
        if not platform or not user_id or user_id == "all":
            return ""
        return self._names.get(f"{platform}:{user_id}", "")

    def __call__(self, platform: str, user_id: str) -> str:
        """等价 resolve：作为 extract_message_semantics 的 nickname_resolver 使用。"""
        return self.resolve(platform, user_id)


@dataclass
class MentionTarget:
    """单个 @ 提及对象（来自原生 At 组件）。"""

    qq: str
    name: str = ""
    is_bot: bool = False
    is_all: bool = False

    def label(self) -> str:
        """渲染为消息头里的提及对象标签。"""
        if self.is_all:
            return "全体成员"
        if self.is_bot:
            return f"Bot({self.qq})" if self.qq else "Bot"
        name = (self.name or "").strip()
        return f"{name}({self.qq})" if name else self.qq

    def to_dict(self) -> dict:
        return {
            "qq": self.qq,
            "name": self.name,
            "is_bot": self.is_bot,
            "is_all": self.is_all,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "MentionTarget":
        return cls(
            qq=str(d.get("qq", "") or ""),
            name=str(d.get("name", "") or ""),
            is_bot=bool(d.get("is_bot", False)),
            is_all=bool(d.get("is_all", False)),
        )


@dataclass
class ReplyContextInfo:
    """引用目标（来自原生 Reply 组件）。"""

    message_id: str = ""
    sender_id: str = ""
    sender_name: str = ""
    text: str = ""
    is_bot: bool = False

    def author_label(self) -> str:
        if self.is_bot:
            return "Bot"
        name = (self.sender_name or "").strip()
        if name and self.sender_id:
            return f"{name}({self.sender_id})"
        return name or self.sender_id

    def to_dict(self) -> dict:
        return {
            "message_id": self.message_id,
            "sender_id": self.sender_id,
            "sender_name": self.sender_name,
            "text": self.text,
            "is_bot": self.is_bot,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ReplyContextInfo":
        return cls(
            message_id=str(d.get("message_id", "") or ""),
            sender_id=str(d.get("sender_id", "") or ""),
            sender_name=str(d.get("sender_name", "") or ""),
            text=str(d.get("text", "") or ""),
            is_bot=bool(d.get("is_bot", False)),
        )


@dataclass
class MessageSemantics:
    """一条消息的完整群聊语义。trigger 为空表示被动（未触发回复）。

    mentions_bot / reply_to_bot 是从原生组件按稳定 ID 判定的事实，
    与"是否触发回复"（is_active，由策略决定）相互独立：
    A@B 时 mentions 里只有 B、mentions_bot=False；A 同时 @Bot 和 B 时
    两个提及对象都在列表中，请求者仍是 A。
    """

    is_group: bool
    sender_id: str = ""
    sender_name: str = ""
    message_id: str = ""
    mentions: list[MentionTarget] = field(default_factory=list)
    reply_to: ReplyContextInfo | None = None
    trigger: str = ""

    @property
    def mentions_bot(self) -> bool:
        return any(m.is_bot for m in self.mentions)

    @property
    def mentions_all(self) -> bool:
        return any(m.is_all for m in self.mentions)

    @property
    def reply_to_bot(self) -> bool:
        return bool(self.reply_to and self.reply_to.is_bot)

    def to_dict(self) -> dict:
        return {
            "is_group": self.is_group,
            "sender_id": self.sender_id,
            "sender_name": self.sender_name,
            "message_id": self.message_id,
            "mentions": [m.to_dict() for m in self.mentions],
            "reply_to": self.reply_to.to_dict() if self.reply_to else None,
            "trigger": self.trigger,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "MessageSemantics":
        """从持久化 dict 还原（缺字段按安全默认值，不凭空补造提及）。"""
        if not isinstance(d, dict):
            return cls(is_group=False)
        mentions_raw = d.get("mentions") or []
        reply_raw = d.get("reply_to")
        return cls(
            is_group=bool(d.get("is_group", False)),
            sender_id=str(d.get("sender_id", "") or ""),
            sender_name=str(d.get("sender_name", "") or ""),
            message_id=str(d.get("message_id", "") or ""),
            mentions=[
                MentionTarget.from_dict(m) for m in mentions_raw if isinstance(m, dict)
            ],
            reply_to=ReplyContextInfo.from_dict(reply_raw)
            if isinstance(reply_raw, dict)
            else None,
            trigger=str(d.get("trigger", "") or ""),
        )


def _norm_id(value) -> str:
    """归一化平台 ID（int/str 统一为 str，0/None 视为缺失）。"""
    if value is None:
        return ""
    s = str(value).strip()
    return "" if s in ("0", "None") else s


def extract_message_semantics(
    event, is_group: bool, nickname_resolver=None
) -> MessageSemantics:
    """从事件原生消息链提取语义。任何组件解析失败按缺失降级，不抛异常。

    Args:
        event: 消息事件
        is_group: 是否群聊消息
        nickname_resolver: 昵称补全器（NicknameCache 实例，可调用且带
            remember 方法）；平台 At 组件缺失昵称时按稳定 ID 补全，同时
            从消息流学习发送者/At/Reply 作者昵称。None 时跳过补全。
    """
    try:
        sender_id = _norm_id(event.get_sender_id())
        sender_name = (event.get_sender_name() or "").strip()
        message_id = _norm_id(getattr(event.message_obj, "message_id", None))
    except Exception:
        # 事件缺少访问器（非标准桩/平台实现）时按缺失降级
        sender_id, sender_name, message_id = "", "", ""
    sem = MessageSemantics(
        is_group=is_group,
        sender_id=sender_id,
        sender_name=sender_name,
        message_id=message_id,
    )
    try:
        self_id = _norm_id(event.get_self_id())
    except Exception:
        self_id = ""
    try:
        platform = str(event.get_platform_name() or "")
    except Exception:
        platform = ""

    # 学习发送者昵称（最新值覆盖旧值，昵称变更自动跟进）
    if nickname_resolver is not None and sender_id and sender_name:
        nickname_resolver.remember(platform, sender_id, sender_name)

    try:
        components = event.get_messages() or []
    except Exception:
        components = []

    seen_qq: set[str] = set()
    for comp in components:
        try:
            if isinstance(comp, At):
                qq = _norm_id(getattr(comp, "qq", None))
                if not qq:
                    continue
                name = str(getattr(comp, "name", "") or "").strip()
                if nickname_resolver is not None and name:
                    nickname_resolver.remember(platform, qq, name)
                if qq in seen_qq:
                    # 重复 @：保留首个条目，但可用新昵称补全空名
                    for m in sem.mentions:
                        if m.qq == qq and not m.name and name:
                            m.name = name
                    continue
                seen_qq.add(qq)
                is_all = qq == "all"
                sem.mentions.append(
                    MentionTarget(
                        qq=qq,
                        name=name,
                        is_bot=not is_all and bool(self_id) and qq == self_id,
                        is_all=is_all,
                    )
                )
            elif isinstance(comp, Reply) and sem.reply_to is None:
                r_sender_id = _norm_id(getattr(comp, "sender_id", None))
                r_sender_name = str(getattr(comp, "sender_nickname", "") or "").strip()
                if nickname_resolver is not None and r_sender_id and r_sender_name:
                    nickname_resolver.remember(platform, r_sender_id, r_sender_name)
                sem.reply_to = ReplyContextInfo(
                    message_id=_norm_id(getattr(comp, "id", None)),
                    sender_id=r_sender_id,
                    sender_name=r_sender_name,
                    text=sanitize_untrusted(
                        str(getattr(comp, "message_str", "") or "")
                    )[:_REPLY_TEXT_LIMIT],
                    is_bot=bool(self_id) and r_sender_id == self_id,
                )
        except Exception:
            # 单个组件异常不阻断整条消息的语义提取
            continue

    # At 昵称补全：平台未提供昵称时按稳定 ID 从缓存取最近已知昵称
    if nickname_resolver is not None:
        for m in sem.mentions:
            if not m.name and not m.is_all and not m.is_bot:
                m.name = nickname_resolver(platform, m.qq)
    return sem


def reply_needs_resolution(reply: ReplyContextInfo | None) -> bool:
    """判断引用目标是否需要从已记录上下文还原（平台未提供完整详情）。"""
    if reply is None:
        return False
    return bool(reply.message_id) and (not reply.sender_id or not reply.text)


def render_reply_marker(reply: ReplyContextInfo | None) -> str:
    """把引用目标渲染为内联 ``[回复 …]`` 标记（与历史格式兼容）。

    平台提供了完整详情时直接使用；部分缺失时尽可能给出作者或消息 ID；
    全部缺失时返回空字符串（不显示标记）。
    """
    if reply is None:
        return ""
    if reply.is_bot:
        author = f"{reply.sender_name}(Bot)" if reply.sender_name else "Bot"
    else:
        author = reply.author_label()
    if author and reply.text:
        return f"{author}: {reply.text}"
    if reply.text:
        return reply.text
    if author:
        return f"{author}的消息"
    if reply.message_id:
        return f"未记录的消息 {reply.message_id}"
    return ""


def classify_trigger(
    sem: MessageSemantics,
    *,
    is_at_or_wake_command: bool,
    require_at_in_group: bool,
    is_active: bool,
) -> str:
    """判定本条消息为何触发 AI；被动消息返回空字符串。

    判定优先级：直接@Bot > 引用回复Bot > @全体 > 唤醒前缀 > 无需@配置。
    @全体 / 唤醒前缀仅在框架唤醒标记（is_at_or_wake_command）为真时成立，
    避免把"消息里恰好有 @全体 但策略不触发"误标为触发方式。
    """
    if not is_active:
        return ""
    if not sem.is_group:
        return TRIGGER_PRIVATE
    if sem.mentions_bot:
        return TRIGGER_AT_BOT
    if sem.reply_to_bot:
        return TRIGGER_REPLY_TO_BOT
    if sem.mentions_all and is_at_or_wake_command:
        return TRIGGER_AT_ALL
    if is_at_or_wake_command:
        return TRIGGER_WAKE
    return TRIGGER_NO_AT_POLICY


def format_group_header(sem: MessageSemantics, is_active: bool) -> str:
    """渲染插件可信元数据头（仅群聊消息使用；私聊保持原格式）。

    形如 ``〔群聊消息｜发送者 樱花(1001)｜消息ID 123｜@对象 Bot、小明(1002)｜
    直接@Bot 是｜触发 直接@Bot〕``。被动消息使用 ``群聊旁观消息`` 标签且
    不携带触发字段。所有字段来自原生组件提取，用户原文无法伪造。
    """
    parts = [GROUP_ACTIVE_TAG if is_active else GROUP_PASSIVE_TAG]

    sender = sem.sender_name or "unknown"
    sender = f"{sender}({sem.sender_id})" if sem.sender_id else sender
    parts.append(f"发送者 {sanitize_untrusted(sender)}")
    if sem.message_id:
        parts.append(f"消息ID {sem.message_id}")
    if sem.mentions:
        parts.append("@对象 " + "、".join(m.label() for m in sem.mentions))
    parts.append("直接@Bot " + ("是" if sem.mentions_bot else "否"))
    if sem.reply_to_bot:
        parts.append("引用回复Bot 是")
    if is_active and sem.trigger:
        parts.append(f"触发 {sem.trigger}")
    return _RESERVED_L + "｜".join(parts) + _RESERVED_R
