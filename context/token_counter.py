"""Token estimation for OpenAI-format message lists.

Uses character-based heuristics:
- Chinese characters: ~0.6 tokens per character
- Other characters (ASCII, etc.): ~0.3 tokens per character
"""

import json


class TokenEstimator:
    """估算 OpenAI 格式消息列表的 Token 数量"""

    def count_messages_tokens(self, messages: list[dict]) -> int:
        """计算消息列表的总 Token 估算值"""
        total = 0
        for msg in messages:
            # 消息内容
            content = msg.get("content", "")
            if isinstance(content, str):
                total += self._estimate_text(content)
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        total += self._estimate_text(part.get("text", ""))
            # tool_calls JSON
            tool_calls = msg.get("tool_calls")
            if tool_calls:
                total += self._estimate_text(json.dumps(tool_calls, ensure_ascii=False))
            # 每条消息的固定开销 (role, etc.)
            total += 4
        return total

    def estimate_prompt(self, system_prompt: str, contexts: list[dict]) -> int:
        """估算一次 LLM 调用的输入 token 数（system prompt + 上下文消息）。

        注意：不含工具 schema（func_tool 结构定义），仅计文本部分。
        """
        total = self._estimate_text(system_prompt or "")
        total += self.count_messages_tokens(contexts)
        return total

    def estimate_completion(
        self, completion_text: str, tool_calls: list | None = None
    ) -> int:
        """估算一次 LLM 调用的输出 token 数（回复文本 + tool_calls JSON）。"""
        total = self._estimate_text(completion_text or "")
        if tool_calls:
            total += self._estimate_text(json.dumps(tool_calls, ensure_ascii=False))
        return total

    def _estimate_text(self, text: str) -> int:
        """估算单段文本的 Token 数"""
        if not text:
            return 0
        chinese_count = sum(1 for c in text if "一" <= c <= "鿿")
        other_count = len(text) - chinese_count
        return int(chinese_count * 0.6 + other_count * 0.3)
