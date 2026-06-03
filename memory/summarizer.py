"""Memory summarizer — auto-summarize short-term memories via LLM."""

from astrbot.api import logger


# 总结 prompt 模板
SUMMARY_SYSTEM_SUFFIX = """

You are managing short-term memories for a conversation. Review the current
memories and recent dialogue, then decide what to keep, delete, add, or update.

## Output Format (one operation per line)
KEEP: id1, id2, ...
DELETE: id1, id2, ...
ADD: new memory text
UPDATE: id | updated text

If no operations for a category, omit that line entirely.

Rules:
- Each memory must contain exactly one fact, under 200 chars
- Never explain your decisions
- Never output markdown or code fences
- Only output KEEP/DELETE/ADD/UPDATE lines
- One operation per line
"""


def parse_summary_output(text: str) -> dict:
    """解析 LLM 总结输出为结构化操作。

    Returns:
        {"keep": [id, ...], "delete": [id, ...],
         "add": [content, ...], "update": {id: content, ...}}
    """
    result: dict = {"keep": [], "delete": [], "add": [], "update": {}}
    for line in text.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        head, _, body = line.partition(":")
        head = head.strip().upper()
        body = body.strip()
        if not body:
            continue
        if head == "KEEP":
            result["keep"].extend(
                x.strip() for x in body.split(",") if x.strip()
            )
        elif head == "DELETE":
            result["delete"].extend(
                x.strip() for x in body.split(",") if x.strip()
            )
        elif head == "ADD":
            if body:
                result["add"].append(body)
        elif head == "UPDATE":
            if "|" in body:
                mid, content = body.split("|", 1)
                result["update"][mid.strip()] = content.strip()
    # DELETE/UPDATE 冲突检测: DELETE 优先
    for mid in result["delete"]:
        result["update"].pop(mid, None)
    return result


class MemorySummarizer:
    """短期记忆自动总结器 — 调用 LLM 总结并清理短期记忆。"""

    def __init__(self, config: dict):
        self.config = config

    async def summarize(
        self,
        short_term_data: dict,
        recent_context_text: str,
        persona_prompt: str,
        provider,
    ) -> dict | None:
        """执行总结，返回 parse_summary_output 格式的 dict，失败返回 None。

        Args:
            short_term_data: load() 返回的短期记忆数据
            recent_context_text: 最近几轮对话的纯文本
            persona_prompt: 当前活跃人格的 system prompt
            provider: LLM Provider 实例
        """
        if not provider:
            logger.warning("[Memory] 总结跳过: 无可用 Provider")
            return None

        memories_text = self._format_memories(short_term_data)
        if not memories_text and not recent_context_text:
            return None

        prompt = self._build_prompt(memories_text, recent_context_text)
        system_prompt = (persona_prompt or "") + SUMMARY_SYSTEM_SUFFIX

        try:
            response = await provider.text_chat(
                prompt=prompt,
                system_prompt=system_prompt,
            )
            if not response or not response.completion_text:
                logger.warning("[Memory] LLM 总结返回空")
                return None

            result = parse_summary_output(response.completion_text)
            has_ops = any(
                result[k] for k in ("delete", "add", "update")
            )
            if has_ops or result["keep"]:
                logger.info(
                    f"[Memory] 总结完成: keep={len(result['keep'])}, "
                    f"delete={len(result['delete'])}, "
                    f"add={len(result['add'])}, "
                    f"update={len(result['update'])}"
                )
            return result
        except Exception as e:
            logger.error(f"[Memory] 总结调用失败: {e}")
            return None

    @staticmethod
    def _format_memories(data: dict) -> str:
        """格式化短期记忆为 prompt 文本。"""
        memories = data.get("memories", [])
        if not memories:
            return ""
        lines = []
        for m in memories:
            lines.append(f"{{id: {m['id']}}} {m['content']}")
        return "\n".join(lines)

    @staticmethod
    def _build_prompt(memories_text: str, context_text: str) -> str:
        """构建总结 prompt。"""
        parts = []
        if memories_text:
            parts.append(f"## Current Short-Term Memories\n{memories_text}")
        if context_text:
            parts.append(f"## Recent Dialogue\n{context_text}")
        return "\n\n".join(parts)
