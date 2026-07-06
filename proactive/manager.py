"""Proactive reply manager — scheduled, timeout, and round-based proactive messages."""

import asyncio
import json
import random
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

from astrbot.api import logger

from ..utils import SHANGHAI_TZ as _SHANGHAI_TZ
from ..utils import format_current_time
from ..utils import shanghai_now_iso as _shanghai_now_iso
from ..utils.config import cfg_bool, cfg_int

PROACTIVE_SYSTEM_SUFFIX_PRIVATE = """

You are proactively sending a message to the user. This is NOT a response to their message.
Based on the conversation history, memories, and trigger reason, generate a natural, brief message.

Guidelines:
- Keep it to 3-5 short sentences
- Be casual and natural
- Reference something specific from context or memory if relevant
- Don't be pushy or annoying
- Match the tone of your persona
- Output ONLY the message text, nothing else
- Do NOT mention that this is a proactive/system-triggered message
"""

PROACTIVE_SYSTEM_SUFFIX_GROUP = """

You are proactively sending a message in a GROUP chat. This is NOT a response to anyone's message.
Based on the conversation history, memories, and trigger reason, generate a natural, brief message.

Guidelines:
- Keep it to 1-3 short sentences — be more concise than in private chat
- Be casual and natural
- Be considerate: you are speaking in front of many people, avoid flooding the chat
- Only speak up when you genuinely have something relevant or interesting to say
- If the trigger reason is weak or you have nothing meaningful to add, output a single empty line to abort
- Reference something specific from recent group conversation if relevant
- Don't be pushy or annoying — silence is better than noise in a group
- Match the tone of your persona
- Output ONLY the message text, nothing else
- Do NOT mention that this is a proactive/system-triggered message
"""

AI_JUDGE_SYSTEM_PROMPT = """You are a "should-I-chime-in" decider in a GROUP chat. Your task is to decide whether the bot (you) should proactively send a message to chime in, based on the recent group conversation only.

Answer YES when:
- Someone is directly asking you a question or @-mentioning you (and you haven't answered yet)
- The topic is exactly your expertise and you can add real value
- There is a natural opening to continue a joke, topic, or emotional resonance
- The conversation has stalled and someone seems to be waiting for a reply

Answer NO when:
- The topic is unrelated to you; chiming in would feel abrupt or intrusive
- People are actively chatting and don't need you
- You have nothing genuinely valuable or interesting to add
- Someone else is already answering the question

Output ONLY "YES" or "NO" — nothing else."""


class ProactiveManager:
    """主动回复管理器 — 协调定时任务、超时监控和轮数触发。"""

    def __init__(
        self,
        config: dict,
        data_dir: str,
        context,  # StarContext
        provider_getter=None,
        persona_mgr=None,
        context_mgr=None,
        memory_mgr=None,
        clean_fn: Callable[[str], str] | None = None,
        split_fn: Callable[[str], list[str]] | None = None,
    ):
        self.config = config
        self.data_dir = data_dir
        self._context = context
        self._provider_getter = provider_getter
        self._persona_mgr = persona_mgr
        self._context_mgr = context_mgr
        self._memory_mgr = memory_mgr
        self._clean_fn = clean_fn
        self._split_fn = split_fn

        self._registry_dir = Path(data_dir) / "proactive"
        self._registry_dir.mkdir(parents=True, exist_ok=True)
        self._registry_path = self._registry_dir / "registry.json"
        # {session_key: {umo, timeout_enabled, round_enabled, round_count,
        #   last_message_at, consecutive_proactive_count}}
        self._sessions: dict[str, dict] = {}

        self._scheduled_tasks: dict[str, asyncio.Task] = {}
        self._cooldown_wakeup_tasks: dict[str, asyncio.Task] = {}
        self._monitor_task: asyncio.Task | None = None
        self._running = False
        self._registry_dirty = False  # 脏标记：数据已变更但尚未写入磁盘

    # 配置读取辅助

    def _cfg_int(self, key: str, default: int) -> int:
        return cfg_int(self.config, key, default)

    def _cfg_bool(self, key: str, default: bool) -> bool:
        return cfg_bool(self.config, key, default)

    # 初始化与持久化

    async def initialize(self):
        """加载会话注册表，启动后台监控。"""
        self._load_registry()
        if self._cfg_bool("enable_proactive", False):
            self._running = True
            self._monitor_task = asyncio.create_task(self._timeout_monitor())
            logger.info("[Proactive] 主动回复已启用")
        else:
            logger.info("[Proactive] 主动回复未启用")

    def _load_registry(self):
        if not self._registry_path.exists():
            return
        try:
            text = self._registry_path.read_text("utf-8")
            self._sessions = json.loads(text)
        except Exception as e:
            logger.warning(f"[Proactive] 加载注册表失败: {e}")
            self._sessions = {}

    async def _save_registry(self):
        try:
            text = json.dumps(self._sessions, ensure_ascii=False, indent=2)
            await asyncio.to_thread(self._registry_path.write_text, text, "utf-8")
            self._registry_dirty = False
        except Exception as e:
            logger.error(f"[Proactive] 保存注册表失败: {e}")

    def _mark_dirty(self):
        """标记注册表为脏，将在下次定期保存时写入磁盘。"""
        self._registry_dirty = True

    async def _flush_registry(self):
        """将脏数据立即写入磁盘；无变更时跳过。"""
        if self._registry_dirty:
            await self._save_registry()

    def _get_or_create_session(self, session_key: str, umo: str | None = None) -> dict:
        if session_key not in self._sessions:
            self._sessions[session_key] = {
                "umo": umo or "",
                "timeout_enabled": False,
                "round_enabled": False,
                "round_count": 0,
                "last_message_at": _shanghai_now_iso(),
                "consecutive_proactive_count": 0,
                "ai_judge_enabled": False,
                "ai_judge_count": 0,
                "ai_judge_cooldown_until": "",  # ISO 时间戳，空表示未冷却
            }
        else:
            if umo:
                self._sessions[session_key]["umo"] = umo
        return self._sessions[session_key]

    # 会话管理

    async def register_session(self, session_key: str, umo: str):
        """注册/更新会话（每次收到消息时调用）。"""
        self._get_or_create_session(session_key, umo)
        self._sessions[session_key]["last_message_at"] = _shanghai_now_iso()
        self._mark_dirty()

    async def on_message(self, session_key: str):
        """收到消息时调用：更新时间戳 + 重置连续主动计数 + 检查 AI 判断/轮数触发。"""
        session = self._sessions.get(session_key)
        if not session:
            return

        session["last_message_at"] = _shanghai_now_iso()

        # 用户主动发言 → 重置连续主动回复计数
        if session.get("consecutive_proactive_count", 0) != 0:
            session["consecutive_proactive_count"] = 0
            self._mark_dirty()

        is_group = ":private:" not in session_key

        # AI 判断触发 — 仅群聊（攒够 N 条让 AI 判断是否插话，YES 才回复并进入冷却）
        if is_group and session.get("ai_judge_enabled"):
            interval = self._cfg_int("proactive_ai_judge_interval", 5)
            if interval > 0:
                session["ai_judge_count"] = session.get("ai_judge_count", 0) + 1
                if session["ai_judge_count"] >= interval:
                    cooldown_sec = self._cfg_int("proactive_ai_judge_cooldown", 300)
                    if self._is_ai_judge_in_cooldown(session, cooldown_sec):
                        # 冷却中：不触发判断，保留累计计数（冷却结束后基于累计量继续触发）
                        self._mark_dirty()
                    else:
                        # 即时写入：计数重置必须在 spawn 任务前持久化，防止崩溃后重复触发
                        session["ai_judge_count"] = 0
                        await self._save_registry()
                        asyncio.create_task(self._ai_judge_and_reply(session_key))

        # 轮数触发 — 仅群聊（私聊每条都触发回复，无需轮数触发）
        if is_group and session.get("round_enabled"):
            interval = self._cfg_int("proactive_round_interval", 0)
            if interval > 0:
                session["round_count"] = session.get("round_count", 0) + 1
                if session["round_count"] >= interval:
                    session["round_count"] = 0
                    # 即时写入：确保 round_count 重置在 spawn 任务前持久化，
                    # 防止进程崩溃后重复触发
                    await self._save_registry()
                    reason = f"已收到 {interval} 条消息，触发轮数主动回复"
                    asyncio.create_task(self._send_proactive(session_key, reason))
                    return

        self._mark_dirty()

    async def reset_round_count(self, session_key: str):
        """机器人回复后重置轮数计数器，使计数仅从上一次回复开始。"""
        session = self._sessions.get(session_key)
        if not session:
            return
        if session.get("round_count", 0) != 0:
            session["round_count"] = 0
            self._mark_dirty()

    # 定时任务工具 (LLM Tool)

    async def schedule_reply(
        self,
        session_key: str,
        delay_minutes: int,
        reason: str,
    ) -> str:
        """LLM 工具调用：安排一个延迟的主动回复。"""
        if not self._running:
            return "Proactive replies are not enabled."

        session = self._sessions.get(session_key)
        if not session or not session.get("umo"):
            return "Session not registered. Cannot schedule reply."

        delay_minutes = max(1, min(delay_minutes, 1440))  # 1 min ~ 24h
        task_id = f"schedule_{uuid.uuid4().hex[:8]}"

        async def _fire():
            await asyncio.sleep(delay_minutes * 60)
            self._scheduled_tasks.pop(task_id, None)
            await self._send_proactive(session_key, reason)

        task = asyncio.create_task(_fire())
        self._scheduled_tasks[task_id] = task
        logger.info(
            f"[Proactive] 已安排定时回复: {session_key}, "
            f"延迟 {delay_minutes} 分钟, 原因: {reason}"
        )
        return f"Proactive reply scheduled in {delay_minutes} minutes."

    # 会话设置

    async def set_timeout_enabled(self, session_key: str, enabled: bool):
        session = self._get_or_create_session(session_key)
        session["timeout_enabled"] = enabled
        await self._save_registry()

    async def set_round_enabled(self, session_key: str, enabled: bool):
        session = self._get_or_create_session(session_key)
        session["round_enabled"] = enabled
        session["round_count"] = 0
        await self._save_registry()

    async def set_ai_judge_enabled(self, session_key: str, enabled: bool):
        """开启/关闭 AI 判断主动回复。关闭时一并清空计数与冷却状态。"""
        session = self._get_or_create_session(session_key)
        session["ai_judge_enabled"] = enabled
        session["ai_judge_count"] = 0
        session["ai_judge_cooldown_until"] = ""
        await self._save_registry()

    def get_session_settings(self, session_key: str) -> dict:
        return self._sessions.get(session_key, {})

    async def list_sessions(self) -> list[dict]:
        result = []
        for key, s in self._sessions.items():
            result.append(
                {
                    "session_key": key,
                    **s,
                }
            )
        return result

    # 核心：生成并发送主动回复

    async def _send_proactive(self, session_key: str, reason: str) -> bool:
        """生成主动回复并发送。成功发送返回 True；任何原因未发送（LLM 返回空、
        清洗后为空、发送失败、异常）返回 False，供调用方决定后续动作（如是否冷却）。
        """
        session = self._sessions.get(session_key)
        if not session:
            logger.warning(f"[Proactive] 会话 {session_key} 未注册，跳过")
            return False

        umo = session.get("umo", "")
        # 需要先收到过至少一条消息才能获取真实的 UMO（平台实例 ID）
        if not umo:
            logger.debug(
                f"[Proactive] 会话 {session_key} 尚未收到过消息（UMO 为空），跳过主动回复"
            )
            return False

        try:
            # 1. 获取 Provider
            provider = None
            if self._provider_getter:
                try:
                    provider = self._provider_getter()
                except Exception:
                    pass
            if not provider:
                logger.warning("[Proactive] 无可用 Provider，跳过主动回复")
                return False

            # 2. 构建系统 Prompt
            system_prompt = ""
            if self._persona_mgr:
                try:
                    system_prompt = await self._persona_mgr.get_system_prompt()
                except Exception:
                    pass

            # 注入当前时间
            system_prompt = system_prompt or ""
            system_prompt = f"当前时间: {format_current_time()}\n\n" + system_prompt

            is_group = ":private:" not in session_key
            system_prompt += (
                PROACTIVE_SYSTEM_SUFFIX_GROUP
                if is_group
                else PROACTIVE_SYSTEM_SUFFIX_PRIVATE
            )

            # 3. 注入记忆
            if self._memory_mgr:
                try:
                    memory_text = await self._memory_mgr.get_memory_prompt(
                        session_key,
                        query=reason,
                    )
                    if memory_text:
                        system_prompt += f"\n\n{memory_text}"
                except Exception:
                    pass

            # 4. 获取最近上下文
            recent_text = await self._get_recent_context(session_key)

            # 5. 构建 prompt
            prompt_parts = [f"Trigger reason: {reason}"]
            if recent_text:
                prompt_parts.append(f"Recent conversation:\n{recent_text}")
            prompt = "\n\n".join(prompt_parts)

            # 6. 调用 LLM
            response = await provider.text_chat(
                prompt=prompt,
                system_prompt=system_prompt,
            )
            if not response or not response.completion_text:
                logger.warning("[Proactive] LLM 返回空，跳过主动回复")
                return False

            text = response.completion_text.strip()
            if not text:
                return False

            # 7. 文本清洗
            if self._clean_fn:
                text = self._clean_fn(text)

            if not text:
                return False

            # 8. 发送消息（支持分段）
            from astrbot.core.message.components import Plain
            from astrbot.core.message.message_event_result import MessageChain

            if self._split_fn:
                segments = self._split_fn(text)
            else:
                segments = [text]

            if len(segments) <= 1:
                chain = MessageChain([Plain(text)])
                sent = await self._context.send_message(umo, chain)
                if not sent:
                    logger.warning(f"[Proactive] 发送失败: {session_key}")
                    return False
            else:
                logger.info(f"[Proactive] 分段发送: {len(segments)} 段")
                delay_ms = max(0, min(self._cfg_int("split_delay_ms", 800), 5000))
                for seg_idx, segment in enumerate(segments):
                    chain = MessageChain([Plain(segment)])
                    sent = await self._context.send_message(umo, chain)
                    if not sent:
                        logger.warning(
                            f"[Proactive] 分段发送失败 ({seg_idx + 1}/{len(segments)}): {session_key}"
                        )
                        break
                    if seg_idx < len(segments) - 1:
                        await asyncio.sleep(delay_ms / 1000)

            logger.info(f"[Proactive] 已发送主动回复到 {session_key}: {text[:50]}...")

            # 9. 重置轮数计数器（回复已发送，从零开始重新计数）
            session["round_count"] = 0
            await self._save_registry()

            # 10. 保存到上下文
            if self._context_mgr:
                try:
                    user_msg = {
                        "role": "user",
                        "message_id": "",
                        "content": f"[Proactive Reply Trigger] {reason}",
                    }
                    assistant_msg = {"role": "assistant", "content": text}
                    await self._context_mgr.append_and_save(
                        session_key,
                        user_msg,
                        assistant_msg,
                        provider=provider,
                    )
                except Exception as e:
                    logger.warning(f"[Proactive] 保存主动回复到上下文失败: {e}")

            return True

        except Exception as e:
            logger.error(f"[Proactive] 主动回复失败 [{session_key}]: {e}")
            return False

    async def _get_recent_context(self, session_key: str) -> str:
        """获取最近几轮对话的纯文本，包含 [msg:ID] 标记。"""
        if not self._context_mgr:
            return ""
        try:
            messages = await self._context_mgr.load_context(session_key)
            recent = []
            for msg in reversed(messages):
                role = msg.get("role", "")
                if role in ("user", "assistant"):
                    content = msg.get("content", "")
                    if isinstance(content, list):
                        content = " ".join(
                            p.get("text", "")
                            for p in content
                            if isinstance(p, dict) and p.get("type") == "text"
                        )
                    # 为用户消息注入 [msg:ID] 标记
                    if role == "user" and content:
                        msg_id = msg.get("message_id", "")
                        if msg_id:
                            content = f"[msg:{msg_id}] {content}"
                    if content:
                        recent.append(f"[{role}]: {content}")
                if len(recent) >= 10:
                    break
            return "\n".join(reversed(recent))
        except Exception:
            return ""

    # AI 判断主动回复

    def _is_ai_judge_in_cooldown(self, session: dict, cooldown_sec: int) -> bool:
        """检查会话是否处于 AI 判断冷却中。cooldown_sec <= 0 视为无冷却。"""
        if cooldown_sec <= 0:
            return False
        until_str = session.get("ai_judge_cooldown_until", "")
        if not until_str:
            return False
        try:
            until = datetime.fromisoformat(until_str)
            if until.tzinfo is None:
                until = until.replace(tzinfo=_SHANGHAI_TZ)
            return datetime.now(_SHANGHAI_TZ) < until
        except Exception:
            return False

    async def _ai_judge_and_reply(self, session_key: str):
        """AI 判断入口：先轻量判断，YES 才生成回复并进入冷却；NO 仅记录日志。"""
        session = self._sessions.get(session_key)
        if not session:
            return
        # 二次确认非冷却（防止外部重置计数后又攒满的极端竞态）
        cooldown_sec = self._cfg_int("proactive_ai_judge_cooldown", 300)
        if self._is_ai_judge_in_cooldown(session, cooldown_sec):
            return

        try:
            should_reply = await self._judge_should_reply(session_key)
        except Exception as e:
            logger.error(f"[Proactive] AI 判断异常 [{session_key}]: {e}")
            should_reply = False

        if not should_reply:
            logger.info(f"[Proactive] AI 判断为 NO，不插话 [{session_key}]")
            return

        reason = "AI 判断当前群聊上下文适合主动插话"
        sent = await self._send_proactive(session_key, reason)

        # 仅在真正发送成功后才进入冷却；生成失败/模型放弃/发送失败都不冷却，
        # 让下次攒够消息时重新判断，避免"没发消息还白白冷却"。
        session = self._sessions.get(session_key)
        if not session:
            return
        if not sent:
            logger.info(
                f"[Proactive] 回复未发送（生成失败或模型放弃），不进入冷却 [{session_key}]"
            )
            return
        if cooldown_sec > 0:
            until = datetime.now(_SHANGHAI_TZ) + timedelta(seconds=cooldown_sec)
            session["ai_judge_cooldown_until"] = until.isoformat()
            await self._save_registry()
            logger.info(
                f"[Proactive] AI 判断触发回复，进入冷却 {cooldown_sec}s [{session_key}]"
            )
            # 冷却到期主动唤醒：立即判断冷却期间累计的消息，不等下一条消息
            self._schedule_cooldown_wakeup(session_key, cooldown_sec)

    def _schedule_cooldown_wakeup(self, session_key: str, cooldown_sec: int):
        """安排冷却到期唤醒任务。取消同会话旧的唤醒任务，避免重复堆积。"""
        old = self._cooldown_wakeup_tasks.pop(session_key, None)
        if old and not old.done():
            old.cancel()
        task = asyncio.create_task(
            self._ai_judge_cooldown_wakeup(session_key, cooldown_sec)
        )
        self._cooldown_wakeup_tasks[session_key] = task

    async def _ai_judge_cooldown_wakeup(self, session_key: str, cooldown_sec: int):
        """冷却到期唤醒：清空冷却标记，若累计消息够则立即触发一次判断。

        这样冷却一结束就一次性判断冷却期间累计的所有消息，无需等下一条消息驱动。
        """
        try:
            await asyncio.sleep(cooldown_sec)
        except asyncio.CancelledError:
            return
        finally:
            self._cooldown_wakeup_tasks.pop(session_key, None)

        session = self._sessions.get(session_key)
        if not session or not session.get("ai_judge_enabled"):
            return

        # 冷却已到期：清空标记
        session["ai_judge_cooldown_until"] = ""

        interval = self._cfg_int("proactive_ai_judge_interval", 5)
        count = session.get("ai_judge_count", 0)
        if interval <= 0 or count < interval:
            # 冷却期间累计不足：等 on_message 继续攒
            self._mark_dirty()
            return

        # 累计够：立即触发一次判断（复用 _ai_judge_and_reply，YES 会重新进入冷却）
        session["ai_judge_count"] = 0
        await self._save_registry()
        logger.info(
            f"[Proactive] 冷却到期，累计 {count} 条消息，立即触发判断 [{session_key}]"
        )
        asyncio.create_task(self._ai_judge_and_reply(session_key))

    async def _judge_should_reply(self, session_key: str) -> bool:
        """轻量 LLM 判断：当前群聊上下文是否适合主动插话。任何异常均保守返回 False。"""
        provider = None
        if self._provider_getter:
            try:
                provider = self._provider_getter()
            except Exception:
                pass
        if not provider:
            logger.warning("[Proactive] AI 判断无可用 Provider，跳过")
            return False

        recent_text = await self._get_judge_context(session_key)
        if not recent_text:
            logger.debug(f"[Proactive] AI 判断无可用上下文 [{session_key}]")
            return False

        prompt = (
            "以下是群聊最近的对话记录（user 包含群成员发言，assistant 为你之前的回复）。"
            "请判断现在是否适合你（机器人）主动插话：\n\n"
            f"{recent_text}\n\n"
            "只回答 YES 或 NO。"
        )
        try:
            response = await provider.text_chat(
                prompt=prompt,
                system_prompt=AI_JUDGE_SYSTEM_PROMPT,
            )
        except Exception as e:
            logger.warning(f"[Proactive] AI 判断 LLM 调用失败 [{session_key}]: {e}")
            return False
        if not response or not response.completion_text:
            logger.warning(f"[Proactive] AI 判断 LLM 返回空 [{session_key}]")
            return False

        text = response.completion_text.strip().upper()
        # 解析 YES/NO：优先看开头，再看是否包含
        if text.startswith("YES"):
            return True
        if text.startswith("NO"):
            return False
        has_yes = "YES" in text
        has_no = "NO" in text
        if has_yes and not has_no:
            return True
        if has_no and not has_yes:
            return False
        # 模糊或冲突 → 保守不回
        logger.debug(f"[Proactive] AI 判断结果解析失败，保守视为 NO: {text[:30]}")
        return False

    async def _get_judge_context(self, session_key: str) -> str:
        """获取最近群聊上下文（含 observed 被动消息），用于 AI 判断。

        与 _get_recent_context 的区别：后者只取 user/assistant，看不到群成员的
        被动消息；判断"是否插话"必须看到群里在聊什么，因此这里也纳入 observed。
        observed 与 user 统一标记为 user（都是"别人说的话"）。
        """
        if not self._context_mgr:
            return ""
        try:
            messages = await self._context_mgr.load_context(session_key)
            recent = []
            for msg in reversed(messages):
                role = msg.get("role", "")
                if role not in ("user", "assistant", "observed"):
                    continue
                content = msg.get("content", "")
                if isinstance(content, list):
                    content = " ".join(
                        p.get("text", "")
                        for p in content
                        if isinstance(p, dict) and p.get("type") == "text"
                    )
                if not content:
                    continue
                # assistant 保留原角色；user/observed 统一为 user（群成员发言）
                label = "assistant" if role == "assistant" else "user"
                recent.append(f"[{label}]: {content}")
                if len(recent) >= self._cfg_int(
                    "proactive_ai_judge_context_messages", 10
                ):
                    break
            return "\n".join(reversed(recent))
        except Exception:
            return ""

    # 后台超时监控

    async def _timeout_monitor(self):
        """每 60 秒检查一次超时触发，并顺便 flush 脏数据。

        改进：不再 100% 触发，而是结合概率和最大连续次数：
        - proactive_timeout_probability: 每次超时的触发概率 (0.0~1.0, 默认 0.3)
        - proactive_timeout_max_consecutive: 连续主动回复最大次数 (默认 2，0=不限)
        """
        while self._running:
            try:
                await asyncio.sleep(60)

                # 定期将脏数据写入磁盘（防抖核心：每 60 秒最多写一次）
                await self._flush_registry()

                timeout_min = self._cfg_int("proactive_timeout_minutes", 30)
                if timeout_min <= 0:
                    continue

                # 概率触发 (0~100, 默认 30)
                probability = self._cfg_int("proactive_timeout_probability", 30)
                probability = max(0, min(probability, 100)) / 100.0

                # 最大连续次数 (默认 2, 0=不限)
                max_consecutive = self._cfg_int("proactive_timeout_max_consecutive", 2)

                now = datetime.now(_SHANGHAI_TZ)
                for key, session in list(self._sessions.items()):
                    if not session.get("timeout_enabled"):
                        continue

                    # 连续次数已达上限 → 跳过，等用户发消息后重置
                    if (
                        max_consecutive > 0
                        and session.get("consecutive_proactive_count", 0)
                        >= max_consecutive
                    ):
                        continue

                    last_str = session.get("last_message_at", "")
                    if not last_str:
                        continue

                    try:
                        last = datetime.fromisoformat(last_str)
                        if last.tzinfo is None:
                            last = last.replace(tzinfo=_SHANGHAI_TZ)
                        elapsed_min = (now - last).total_seconds() / 60
                        if elapsed_min >= timeout_min:
                            # 概率判定：未命中则仅更新时间戳，不触发
                            if random.random() > probability:
                                session["last_message_at"] = _shanghai_now_iso()
                                self._mark_dirty()
                                continue

                            reason = (
                                f"用户已 {int(elapsed_min)} 分钟未发消息"
                                f"（超时阈值 {timeout_min} 分钟）"
                            )
                            # 更新时间戳防止重复触发
                            session["last_message_at"] = _shanghai_now_iso()
                            # 累加连续主动回复计数
                            session["consecutive_proactive_count"] = (
                                session.get("consecutive_proactive_count", 0) + 1
                            )
                            await self._save_registry()
                            asyncio.create_task(self._send_proactive(key, reason))
                    except Exception:
                        continue

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[Proactive] 超时监控异常: {e}")

    # 生命周期

    async def close(self):
        """关闭所有后台任务，最终 flush 脏数据。"""
        self._running = False
        for task in self._scheduled_tasks.values():
            task.cancel()
        self._scheduled_tasks.clear()
        for task in self._cooldown_wakeup_tasks.values():
            task.cancel()
        self._cooldown_wakeup_tasks.clear()
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
        await self._flush_registry()
        logger.info("[Proactive] 已关闭")
