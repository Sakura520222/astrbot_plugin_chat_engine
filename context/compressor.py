"""Context compression algorithms.

Two modes:
1. Turn-limit: Discard oldest turns when limit exceeded
2. Token-based: Use LLM to summarize when context reaches token threshold
"""

from abc import ABC, abstractmethod
from collections.abc import Callable

try:
    from astrbot.api import logger
except Exception:  # 脱离 AstrBot 运行环境（独立单测）时降级为标准 logging
    import logging

    logger = logging.getLogger("astrbot_plugin_chat_engine")

from .token_counter import TokenEstimator

# 单次摘要输入的 Token 上限：旧内容可能远超摘要模型上下文，超限时按行
# 裁剪保留最新部分投喂，避免摘要调用本身失败导致降级 (Issue #11)
_SUMMARY_INPUT_TOKEN_CAP = 24000
# 近期消息占预算比例超过该值时不走摘要路径：摘要没有裁剪空间，压缩结果
# 会贴着阈值，每来一条新消息都再次触发摘要调用 (Issue #11)
_SUMMARY_HEADROOM_RATIO = 0.8


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

        - user 消息开启新轮次（真实对话轮）；
        - observed (被动记录) 消息跟在被动段内时并入当前被动段，跟在
          真实消息后则独立开轮——被动消息堆积不再被无限并入仅有的真实
          轮次，否则压缩会因"轮次不足"被无限跳过 (Issue #11)；
        - 例外：observed 夹在 user 与其 assistant 回复之间（回合执行期间
          被记录的消息）时不拆分，保持问答对完整；
        - tool / assistant 消息始终并入当前轮次。
        """
        turns: list[list[dict]] = []
        current_turn: list[dict] = []
        for idx, msg in enumerate(messages):
            role = msg.get("role")
            prev_role = current_turn[-1].get("role") if current_turn else None
            next_role = (
                messages[idx + 1].get("role") if idx + 1 < len(messages) else None
            )
            start_new = bool(current_turn) and (
                role == "user"
                or (
                    role == "observed"
                    and prev_role != "observed"
                    and next_role != "assistant"
                )
            )
            if start_new:
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

        # 拆分轮次。保留窗口动态收缩：轮次充足时保留最近 keep_recent 轮；
        # 不足（大量被动消息堆积在少数轮次）时也必须压缩 (Issue #11)，
        # 仅强制保留最后一轮不参与摘要。
        turns = self.split_into_turns(messages)
        keep_n = min(self.keep_recent, max(1, len(turns) - 1))
        old_turns = turns[:-keep_n]
        recent_turns = turns[-keep_n:]
        recent_msgs = self.flatten_turns(recent_turns)
        recent_tokens = self.token_counter.count_messages_tokens(recent_msgs)

        # 摘要路径需要：(a) 有旧内容可摘要；(b) 近期消息给摘要留足预算
        # 空间，否则压缩结果贴着阈值，每来一条新消息都会再触发一次摘要
        if old_turns and recent_tokens <= int(
            threshold_tokens * _SUMMARY_HEADROOM_RATIO
        ):
            old_text = self._clip_text_to_budget(
                self.turns_to_text(old_turns), _SUMMARY_INPUT_TOKEN_CAP
            )
            if old_text.strip():
                summary = await self._summarize(old_text)

                if summary:
                    result = self._build_summary_block(summary, recent_turns)
                    result_tokens = self.token_counter.count_messages_tokens(result)
                    if result_tokens < threshold_tokens:
                        logger.info(
                            f"[ChatEngine] Token 压缩(摘要): {current_tokens} -> "
                            f"{result_tokens} tokens, 摘要吸收 "
                            f"{len(old_turns)} 轮 / "
                            f"{sum(len(t) for t in old_turns)} 条, "
                            f"保留最近 {keep_n} 轮 {len(recent_msgs)} 条"
                        )
                        return result
                    # 摘要本身过大：按剩余预算截短摘要文本后复检 (Issue #11)
                    clipped = self._clip_text_to_budget(
                        summary, threshold_tokens - recent_tokens - 64
                    )
                    if clipped:
                        result = self._build_summary_block(clipped, recent_turns)
                        result_tokens = self.token_counter.count_messages_tokens(result)
                        if result_tokens < threshold_tokens:
                            logger.info(
                                f"[ChatEngine] Token 压缩(摘要截短): "
                                f"{current_tokens} -> {result_tokens} tokens, "
                                f"摘要超限已按预算截短, 保留最近 {keep_n} 轮"
                            )
                            return result
                    logger.warning(
                        "[ChatEngine] Token 压缩: 摘要截短后仍超出阈值，"
                        "降级为确定性截断"
                    )
                else:
                    logger.warning(
                        "[ChatEngine] Token 压缩: LLM 总结失败，"
                        "降级为确定性截断（按轮边界保留预算内最近内容）"
                    )

        # 确定性降级：按轮边界从最旧丢弃至预算内，至少保留最后 1 条消息。
        # 无 LLM 依赖、结果可预测，日志记录裁剪量供诊断 (Issue #11)
        result = self._truncate_turns_to_budget(turns, threshold_tokens)
        result_tokens = self.token_counter.count_messages_tokens(result)
        logger.warning(
            f"[ChatEngine] Token 压缩(确定性截断): {current_tokens} -> "
            f"{result_tokens} tokens, 保留 {len(result)}/{len(messages)} 条消息"
        )
        return result

    @staticmethod
    def _build_summary_block(
        summary: str, recent_turns: list[list[dict]]
    ) -> list[dict]:
        """构造「摘要 + 最近轮次」的压缩结果"""
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
        result.extend(BaseCompressor.flatten_turns(recent_turns))
        return result

    def _truncate_turns_to_budget(
        self, turns: list[list[dict]], threshold_tokens: int
    ) -> list[dict]:
        """从最新往旧保留预算内能容纳的最多整轮次。

        连最后一轮都放不下时，在最后一轮内部按消息截断，至少保留最后 1 条。
        """
        kept_tokens = 0
        start = len(turns)
        for i in range(len(turns) - 1, -1, -1):
            turn_tokens = self.token_counter.count_messages_tokens(turns[i])
            if kept_tokens + turn_tokens > threshold_tokens:
                break
            kept_tokens += turn_tokens
            start = i
        if start >= len(turns):
            return self._truncate_messages_to_budget(turns[-1], threshold_tokens)
        return self.flatten_turns(turns[start:])

    def _truncate_messages_to_budget(
        self, messages: list[dict], threshold_tokens: int
    ) -> list[dict]:
        """单轮内部截断：保留预算内最新的消息，至少保留最后 1 条。"""
        kept_tokens = self.token_counter.count_messages_tokens([messages[-1]])
        start = len(messages) - 1
        for i in range(len(messages) - 2, -1, -1):
            msg_tokens = self.token_counter.count_messages_tokens([messages[i]])
            if kept_tokens + msg_tokens > threshold_tokens:
                break
            kept_tokens += msg_tokens
            start = i
        return messages[start:]

    def _clip_text_to_budget(self, text: str, budget_tokens: int) -> str:
        """按行边界裁剪文本至 Token 预算内，保留最新的内容。

        用于约束摘要输入/输出体积；一行都放不下时返回空串。
        Token 核算按中文字符数/其他字符数聚合进行，与整段拼装后的估算
        完全一致，避免逐行 int 截断导致预算低估。
        """
        if budget_tokens <= 0:
            return ""
        if self.token_counter.count_text(text) <= budget_tokens:
            return text
        lines = text.splitlines(keepends=True)
        n = len(lines)
        # 后缀字符计数和：suffix[i] = 第 i 行至末行的中文字符/其他字符数
        suffix_cn = [0] * (n + 1)
        suffix_other = [0] * (n + 1)
        for i in range(n - 1, -1, -1):
            cn = sum(1 for c in lines[i] if "一" <= c <= "鿿")
            suffix_cn[i] = suffix_cn[i + 1] + cn
            suffix_other[i] = suffix_other[i + 1] + (len(lines[i]) - cn)
        # 二分找最小 start，使其后缀估算不超过预算（suffix[n] 恒为 0）
        lo, hi = 0, n
        while lo < hi:
            mid = (lo + hi) // 2
            estimate = self.token_counter.estimate_counts(
                suffix_cn[mid], suffix_other[mid]
            )
            if estimate <= budget_tokens:
                hi = mid
            else:
                lo = mid + 1
        if lo > 0:
            kept_tokens = self.token_counter.estimate_counts(
                suffix_cn[lo], suffix_other[lo]
            )
            logger.info(
                f"[ChatEngine] 摘要文本超预算，裁剪最早 {lo} 行"
                f"（保留最新 {n - lo} 行，约 {kept_tokens} tokens）"
            )
        return "".join(lines[lo:])

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
