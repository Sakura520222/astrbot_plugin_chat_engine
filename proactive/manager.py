"""Proactive reply manager — scheduled, timeout, and round-based proactive messages."""

import asyncio
import json
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from astrbot.api import logger

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
        # {session_key: {umo, timeout_enabled, round_enabled, round_count, last_message_at}}
        self._sessions: dict[str, dict] = {}

        self._scheduled_tasks: dict[str, asyncio.Task] = {}
        self._monitor_task: asyncio.Task | None = None
        self._running = False

    # 配置读取辅助

    def _cfg_int(self, key: str, default: int) -> int:
        try:
            return int(self.config.get(key, default))
        except (ValueError, TypeError):
            return default

    def _cfg_bool(self, key: str, default: bool) -> bool:
        val = self.config.get(key, default)
        if isinstance(val, bool):
            return val
        if isinstance(val, str):
            return val.lower() in ("true", "1", "yes")
        if isinstance(val, (int, float)):
            return bool(val)
        return default

    @staticmethod
    def _utcnow() -> str:
        return datetime.now(timezone.utc).isoformat()

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
        except Exception as e:
            logger.error(f"[Proactive] 保存注册表失败: {e}")

    def _get_or_create_session(self, session_key: str, umo: str | None = None) -> dict:
        if session_key not in self._sessions:
            self._sessions[session_key] = {
                "umo": umo or "",
                "timeout_enabled": False,
                "round_enabled": False,
                "round_count": 0,
                "last_message_at": self._utcnow(),
            }
        else:
            if umo:
                self._sessions[session_key]["umo"] = umo
        return self._sessions[session_key]

    # 会话管理

    async def register_session(self, session_key: str, umo: str):
        """注册/更新会话（每次收到消息时调用）。"""
        self._get_or_create_session(session_key, umo)
        self._sessions[session_key]["last_message_at"] = self._utcnow()
        await self._save_registry()

    async def on_message(self, session_key: str):
        """收到消息时调用：更新时间戳 + 检查轮数触发。"""
        session = self._sessions.get(session_key)
        if not session:
            return

        session["last_message_at"] = self._utcnow()

        # 轮数触发 — 仅群聊（私聊每条都触发回复，无需轮数触发）
        is_group = ":private:" not in session_key
        if is_group and session.get("round_enabled"):
            interval = self._cfg_int("proactive_round_interval", 0)
            if interval > 0:
                session["round_count"] = session.get("round_count", 0) + 1
                if session["round_count"] >= interval:
                    session["round_count"] = 0
                    await self._save_registry()
                    reason = f"已收到 {interval} 条消息，触发轮数主动回复"
                    asyncio.create_task(self._send_proactive(session_key, reason))
                    return

        await self._save_registry()

    async def reset_round_count(self, session_key: str):
        """机器人回复后重置轮数计数器，使计数仅从上一次回复开始。"""
        session = self._sessions.get(session_key)
        if not session:
            return
        if session.get("round_count", 0) != 0:
            session["round_count"] = 0
            await self._save_registry()

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
        task_id = f"schedule_{session_key}_{delay_minutes}m"

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

    async def _send_proactive(self, session_key: str, reason: str):
        """生成主动回复并发送。"""
        session = self._sessions.get(session_key)
        if not session:
            logger.warning(f"[Proactive] 会话 {session_key} 未注册，跳过")
            return

        umo = session.get("umo", "")
        # 需要先收到过至少一条消息才能获取真实的 UMO（平台实例 ID）
        if not umo:
            logger.debug(
                f"[Proactive] 会话 {session_key} 尚未收到过消息（UMO 为空），跳过主动回复"
            )
            return

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
                return

            # 2. 构建系统 Prompt
            system_prompt = ""
            if self._persona_mgr:
                try:
                    system_prompt = await self._persona_mgr.get_system_prompt()
                except Exception:
                    pass
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
                return

            text = response.completion_text.strip()
            if not text:
                return

            # 7. 文本清洗
            if self._clean_fn:
                text = self._clean_fn(text)

            if not text:
                return

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
                    return
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

        except Exception as e:
            logger.error(f"[Proactive] 主动回复失败 [{session_key}]: {e}")

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

    # 后台超时监控

    async def _timeout_monitor(self):
        """每 60 秒检查一次超时触发。"""
        while self._running:
            try:
                await asyncio.sleep(60)
                timeout_min = self._cfg_int("proactive_timeout_minutes", 30)
                if timeout_min <= 0:
                    continue

                now = datetime.now(timezone.utc)
                for key, session in list(self._sessions.items()):
                    if not session.get("timeout_enabled"):
                        continue

                    last_str = session.get("last_message_at", "")
                    if not last_str:
                        continue

                    try:
                        last = datetime.fromisoformat(last_str)
                        if last.tzinfo is None:
                            last = last.replace(tzinfo=timezone.utc)
                        elapsed_min = (now - last).total_seconds() / 60
                        if elapsed_min >= timeout_min:
                            reason = (
                                f"用户已 {int(elapsed_min)} 分钟未发消息"
                                f"（超时阈值 {timeout_min} 分钟）"
                            )
                            # 更新时间戳防止重复触发
                            session["last_message_at"] = self._utcnow()
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
        """关闭所有后台任务。"""
        self._running = False
        for task in self._scheduled_tasks.values():
            task.cancel()
        self._scheduled_tasks.clear()
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
        logger.info("[Proactive] 已关闭")
