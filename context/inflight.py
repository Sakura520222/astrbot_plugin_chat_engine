"""In-flight session management and message guidance injection.

Manages session concurrency, buffers messages arriving while an AI request is in-flight,
and formats them as contextual guidance for the next interaction turn.
"""

import asyncio
from dataclasses import dataclass, field
from typing import Any

from .semantics import MessageSemantics, format_group_header


@dataclass
class InflightMessage:
    """Represents a message buffered during an in-flight AI turn.

    ``user_text`` / ``umo`` / ``event`` / ``is_group`` are carried so the
    buffered message can be re-dispatched into the debounce pipeline as a
    real user input for the automatic follow-up turn (not merely injected
    as guidance text). ``semantics`` 携带群聊消息语义（@对象/触发方式等，
    见 context/semantics.py），供引导提示与被动归档保留逐条消息意图。
    """

    id: str
    session_key: str
    sender_name: str
    sender_id: str
    time_str: str
    text: str
    images: list[str] = field(default_factory=list)
    message_id: str = ""
    extra: dict[str, Any] = field(default_factory=dict)
    # 完整格式化的用户消息（含发送者前缀与引用上下文），供自动追答转投防抖用
    user_text: str = ""
    # 消息来源会话与事件引用，供转投后发起后续 LLM 回合
    umo: str = ""
    event: Any = None
    is_group: bool = False
    # 群聊消息语义（私聊为 None）；trigger 为空表示被动/旁观消息
    semantics: MessageSemantics | None = None


class _SessionInflightState:
    """Internal state tracked per session."""

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.is_inflight = False
        self.generation = 1
        self.buffer: list[InflightMessage] = []
        self.staged: dict[int, list[InflightMessage]] = {}
        self.owner_task: asyncio.Task | None = None
        self.depth = 0


class InflightManager:
    """Manages in-flight session arbitration, message buffering, and guidance injection."""

    def __init__(self) -> None:
        self._states: dict[str, _SessionInflightState] = {}
        self._global_lock = asyncio.Lock()

    def _get_state(self, session_key: str) -> _SessionInflightState:
        """Get or create session inflight state synchronously.

        Args:
            session_key: The session key identifier.

        Returns:
            The _SessionInflightState object for this session.
        """
        if session_key not in self._states:
            self._states[session_key] = _SessionInflightState()
        return self._states[session_key]

    def is_inflight(self, session_key: str) -> bool:
        """Check whether the given session currently has an in-flight AI request.

        Args:
            session_key: The session key identifier.

        Returns:
            True if the session has an active AI turn in progress, False otherwise.
        """
        state = self._states.get(session_key)
        return state.is_inflight if state is not None else False

    async def acquire_session(self, session_key: str) -> bool:
        """Atomically attempt to acquire the execution right for an AI turn.

        Supports reentrancy by the same asyncio task.

        Args:
            session_key: The session key identifier.

        Returns:
            True if the session execution right was acquired, False if already in-flight.
        """
        curr = asyncio.current_task()
        async with self._global_lock:
            state = self._get_state(session_key)
            if state.is_inflight:
                if state.owner_task is not None and state.owner_task is curr:
                    state.depth += 1
                    return True
                return False
            state.is_inflight = True
            state.owner_task = curr
            state.depth = 1
            return True

    async def release_session(self, session_key: str) -> None:
        """Release the in-flight execution right for a session.

        Args:
            session_key: The session key identifier.
        """
        curr = asyncio.current_task()
        async with self._global_lock:
            state = self._states.get(session_key)
            if state is not None and state.is_inflight:
                if state.owner_task is None or state.owner_task is curr:
                    state.depth -= 1
                    if state.depth <= 0:
                        state.is_inflight = False
                        state.owner_task = None
                        state.depth = 0

    async def try_handle_inflight(self, session_key: str, msg: InflightMessage) -> bool:
        """Atomically check if session is in-flight, and if so, buffer the message.

        Args:
            session_key: The session key identifier.
            msg: The InflightMessage to buffer if the session is currently in-flight.

        Returns:
            True if the session was in-flight and the message was buffered, False otherwise.
        """
        curr = asyncio.current_task()
        async with self._global_lock:
            state = self._get_state(session_key)
            if state.is_inflight and (
                state.owner_task is None or state.owner_task is not curr
            ):
                async with state.lock:
                    if len(state.buffer) >= 100:
                        state.buffer.pop(0)
                    state.buffer.append(msg)
                return True
            return False

    async def add_inflight_message(
        self, session_key: str, msg: InflightMessage
    ) -> None:
        """Buffer a message that arrived during an in-flight AI request.

        Args:
            session_key: The session key identifier.
            msg: The InflightMessage object to buffer.
        """
        state = self._get_state(session_key)
        async with state.lock:
            # Prevent unbounded memory usage (max 100 messages)
            if len(state.buffer) >= 100:
                state.buffer.pop(0)
            state.buffer.append(msg)

    def get_buffered_count(self, session_key: str) -> int:
        """Get the number of pending buffered messages for a session.

        Args:
            session_key: The session key identifier.

        Returns:
            The count of pending buffered messages.
        """
        state = self._states.get(session_key)
        return len(state.buffer) if state is not None else 0

    async def drain_buffer(self, session_key: str) -> list[InflightMessage]:
        """Atomically take out all buffered messages for a session.

        Used by the automatic follow-up flow after an AI turn completes:
        the drained messages are re-dispatched as real user input. The
        drain happens under the session lock so it cannot interleave with
        concurrent buffering or staging.

        Args:
            session_key: The session key identifier.

        Returns:
            The drained messages in arrival order (may be empty).
        """
        state = self._get_state(session_key)
        async with state.lock:
            drained = list(state.buffer)
            state.buffer.clear()
            return drained

    async def stage_messages(
        self, session_key: str, max_messages: int = 10
    ) -> tuple[int, list[InflightMessage], list[InflightMessage]]:
        """Stage buffered messages for the upcoming AI turn.

        Excess earlier messages beyond max_messages are separated as overflow
        so they can be archived to conversation history without being lost.

        Args:
            session_key: The session key identifier.
            max_messages: Maximum number of recent messages to inject as guidance.

        Returns:
            A tuple of (generation, guide_messages, overflow_messages).
        """
        state = self._get_state(session_key)
        async with state.lock:
            if not state.buffer:
                return state.generation, [], []

            all_msgs = list(state.buffer)
            state.buffer.clear()

            if len(all_msgs) > max_messages:
                split_idx = len(all_msgs) - max_messages
                overflow_messages = all_msgs[:split_idx]
                guide_messages = all_msgs[split_idx:]
            else:
                overflow_messages = []
                guide_messages = all_msgs

            state.staged[state.generation] = all_msgs
            return state.generation, guide_messages, overflow_messages

    async def commit_stage(self, session_key: str, generation: int) -> None:
        """Commit staged messages after successful LLM response and persistence.

        Args:
            session_key: The session key identifier.
            generation: The generation identifier returned by stage_messages.
        """
        state = self._states.get(session_key)
        if state is None:
            return
        async with state.lock:
            state.staged.pop(generation, None)

    async def rollback_stage(self, session_key: str, generation: int) -> None:
        """Rollback staged messages if the turn failed or wasn't persisted.

        Restores staged messages to the front of the buffer only if the session
        generation has not been incremented/reset in the meantime.

        Args:
            session_key: The session key identifier.
            generation: The generation identifier returned by stage_messages.
        """
        state = self._states.get(session_key)
        if state is None:
            return
        async with state.lock:
            staged_msgs = state.staged.pop(generation, None)
            if staged_msgs and state.generation == generation:
                state.buffer = staged_msgs + state.buffer

    async def reset_session(self, session_key: str) -> int:
        """Reset session buffer and increment generation on session clear/switch.

        Args:
            session_key: The session key identifier.

        Returns:
            The new generation number.
        """
        state = self._get_state(session_key)
        async with state.lock:
            state.generation += 1
            state.buffer.clear()
            state.staged.clear()
            return state.generation

    def build_guidance_prompt(self, messages: list[InflightMessage]) -> str:
        """Build the structured markdown prompt block for in-flight guidance.

        群聊消息逐条携带可信元数据头（@对象 / 直接@Bot / 触发方式），
        与上下文序列化保持同一契约，避免暂存消息丢失发言意图。

        Args:
            messages: List of InflightMessage objects.

        Returns:
            Formatted guidance markdown string.
        """
        if not messages:
            return ""

        lines = [
            "## 上一轮 AI 请求期间收到的消息",
            "",
            "以下消息在上一轮 AI 请求执行期间到达，尚未被单独回复。",
            "请结合这些消息与当前用户输入，自然理解对话上下文，",
            "注意消息的时间顺序和发送者，不要逐条补发回复。",
            "",
        ]

        for msg in messages:
            img_prefix = "[图片] " if msg.images and "[图片]" not in msg.text else ""
            content = f"{img_prefix}{msg.text}".strip()
            header = ""
            if msg.is_group and msg.semantics is not None:
                # trigger 非空 = 触发消息；空 = 被动/旁观消息
                header = format_group_header(msg.semantics, bool(msg.semantics.trigger))
            lines.append(f"- [{msg.time_str}][{msg.sender_name}]{header}：{content}")

        return "\n".join(lines)
