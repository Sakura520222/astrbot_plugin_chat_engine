"""Context compression algorithms.

Two modes:
1. Turn-limit: Discard oldest turns when limit exceeded
2. Token-based: Use LLM to summarize when context reaches token threshold
"""

from abc import ABC, abstractmethod
from collections.abc import Callable

from astrbot.api import logger

from .token_counter import TokenEstimator


class BaseCompressor(ABC):
    """压缩器基类"""

    @abstractmethod
    async def compress(
        self, messages: list[dict], max_context_tokens: int = 0
    ) -> list[dict]:
        """压缩消息列表。返回压缩后的列表。"""
        ...

    @staticmethod
    def split_into_turns(messages: list[dict]) -> list[list[dict]]:
        """将消息列表拆分为逻辑轮次。
        每个轮次以 user 消息开始，包含后续所有 tool 调用/结果，直到下一条 user 消息。
        """
        turns: list[list[dict]] = []
        current_turn: list[dict] = []
        for msg in messages:
            if msg.get("role") == "user" and current_turn:
                turns.append(current_turn)
                current_turn = []
            current_turn.append(msg)
        if current_turn:
            turns.append(current_turn)
        return turns

    @staticmethod
    def flatten_turns(turns: list[list[dict]]) -> list[dict]:
        """将轮次列表展平为消息列表"""
        result = []
        for turn in turns:
            result.extend(turn)
        return result

    @staticmethod
    def turns_to_text(turns: list[list[dict]]) -> str:
        """将轮次列表转为可读文本 (用于 LLM 总结)"""
        lines = []
        for turn in turns:
            for msg in turn:
                role = msg.get("role", "unknown")
                content = msg.get("content", "")
                if isinstance(content, list):
                    content = " ".join(
                        p.get("text", "") for p in content if isinstance(p, dict)
                    )
                if content:
                    lines.append(f"[{role}]: {content}")
        return "\n".join(lines)


class TurnLimitCompressor(BaseCompressor):
    """轮数限制压缩器 — 超过轮数时丢弃最早的消息"""

    def __init__(self, max_turns: int = 50):
        self.max_turns = max_turns

    async def compress(
        self, messages: list[dict], max_context_tokens: int = 0
    ) -> list[dict]:
        turns = self.split_into_turns(messages)
        if len(turns) <= self.max_turns:
            return messages
        kept_turns = turns[-self.max_turns :]
        logger.info(f"[ChatEngine] 轮数压缩: {len(turns)} -> {len(kept_turns)} 轮")
        return self.flatten_turns(kept_turns)


class TokenBasedCompressor(BaseCompressor):
    """Token 阈值压缩器 — 达到 Token 上限时用 LLM 总结旧消息"""

    def __init__(
        self,
        token_threshold_ratio: float = 0.8,
        keep_recent_turns: int = 5,
        provider_getter: Callable = None,
    ):
        self.threshold = token_threshold_ratio
        self.keep_recent = keep_recent_turns
        self.provider_getter = provider_getter
        self.token_counter = TokenEstimator()

    async def compress(
        self, messages: list[dict], max_context_tokens: int = 0
    ) -> list[dict]:
        if max_context_tokens <= 0:
            return messages  # 未知限制，跳过压缩

        current_tokens = self.token_counter.count_messages_tokens(messages)
        threshold_tokens = int(max_context_tokens * self.threshold)

        if current_tokens < threshold_tokens:
            return messages  # 低于阈值

        # 拆分轮次
        turns = self.split_into_turns(messages)
        if len(turns) <= self.keep_recent:
            return messages  # 轮次不够，无法压缩

        old_turns = turns[: -self.keep_recent]
        recent_turns = turns[-self.keep_recent :]

        # 用 LLM 总结旧轮次
        old_text = self.turns_to_text(old_turns)
        summary = await self._summarize(old_text)

        if summary:
            # 摘要 + 最近轮次
            result = [
                {
                    "role": "user",
                    "content": f"[之前的对话摘要]\n{summary}",
                },
                {
                    "role": "assistant",
                    "content": "好的，我已了解之前的对话内容，我们继续。",
                },
            ]
            result.extend(self.flatten_turns(recent_turns))
            new_tokens = self.token_counter.count_messages_tokens(result)
            logger.info(
                f"[ChatEngine] Token 压缩: {current_tokens} -> {new_tokens} tokens, "
                f"保留最近 {self.keep_recent} 轮"
            )
            return result
        else:
            # LLM 总结失败，简单截断
            logger.warning("[ChatEngine] LLM 总结失败，回退到简单截断")
            return self.flatten_turns(recent_turns)

    async def _summarize(self, text: str) -> str | None:
        """调用 LLM 总结对话内容"""
        try:
            provider = self.provider_getter()
            if not provider:
                return None

            response = await provider.text_chat(
                prompt=text,
                system_prompt=(
                    "你是一个对话摘要助手。请简洁地总结以下对话历史，"
                    "保留关键信息、用户偏好、重要决定和正在进行的话题。"
                    "只输出摘要内容，不要添加额外说明。"
                ),
            )
            return (
                response.completion_text.strip() if response.completion_text else None
            )
        except Exception as e:
            logger.error(f"[ChatEngine] LLM 总结调用失败: {e}")
            return None


class ContextCompressorFactory:
    """压缩器工厂"""

    @staticmethod
    def create(config: dict, provider_getter: Callable = None) -> BaseCompressor:
        """根据配置创建对应的压缩器"""
        mode = config.get("compression_mode", "turn_limit")
        if mode == "token":
            return TokenBasedCompressor(
                token_threshold_ratio=config.get("token_threshold_ratio", 0.8),
                keep_recent_turns=config.get("keep_recent_turns", 5),
                provider_getter=provider_getter,
            )
        # 默认: 轮数限制
        return TurnLimitCompressor(max_turns=config.get("max_turns", 50))
