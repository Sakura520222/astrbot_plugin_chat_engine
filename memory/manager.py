"""Memory manager — coordinates short-term, long-term, and auto-summarization."""

import asyncio

from astrbot.api import logger

from .short_term import ShortTermMemoryStore
from .summarizer import MemorySummarizer
from .vector_store import LongTermMemoryStore


class MemoryManager:
    """记忆系统统一管理器 — 协调短期记忆、长期记忆和自动总结。"""

    def __init__(
        self,
        config: dict,
        data_dir: str,
        embedding_getter=None,
        rerank_getter=None,
        provider_getter=None,
    ):
        self.config = config
        self.data_dir = data_dir

        # 使用 getter 函数动态获取 provider
        # AstrBot 的插件加载先于 Provider 初始化，所以初始化时 provider 列表可能为空
        # 每次实际使用时通过 getter 获取最新的 provider 实例
        self._embedding_getter = embedding_getter
        self._rerank_getter = rerank_getter
        self._provider_getter = provider_getter

        self.short_term: ShortTermMemoryStore = None
        self.long_term: LongTermMemoryStore = None
        self.summarizer: MemorySummarizer = None

    async def initialize(self):
        """初始化存储和总结器。"""
        self.short_term = ShortTermMemoryStore(self.data_dir)
        self.long_term = LongTermMemoryStore(
            self.data_dir,
            embedding_getter=self._embedding_getter,
            rerank_getter=self._rerank_getter,
        )
        self.summarizer = MemorySummarizer(self.config)

        # 注意：此时 AstrBot 的 Provider 可能尚未初始化
        # 长期记忆的实际可用性在第一次使用时动态检测
        logger.info("[Memory] 短期记忆: 已启用")
        logger.info("[Memory] 长期记忆: 延迟检测（首次使用时确认 EmbeddingProvider）")

    # 配置读取辅助 (与 main.py 相同模式)

    def _cfg_int(self, key: str, default: int) -> int:
        try:
            return int(self.config.get(key, default))
        except (ValueError, TypeError):
            return default

    def _cfg_float(self, key: str, default: float) -> float:
        try:
            return float(self.config.get(key, default))
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

    # System Prompt 注入

    async def get_memory_prompt(self, session_key: str, query: str) -> str:
        """获取格式化的记忆文本用于注入 system prompt。

        Returns:
            格式化的记忆文本，无记忆时返回空字符串。
        """
        sections = []

        # 短期记忆 — 全量注入
        short_memories = await self.short_term.list_memories(session_key)
        if short_memories:
            lines = []
            for m in short_memories:
                lines.append(f"- [id:{m['id']}] {m['content']}")
            sections.append("### Short-Term Memory\n" + "\n".join(lines))
            logger.debug(f"[Memory] 注入 {len(short_memories)} 条短期记忆")

        # 长期记忆 — 语义检索
        if self.long_term.available and query:
            try:
                results = await self.long_term.search(
                    session_key,
                    query=query,
                    top_k=self._cfg_int("long_term_retrieval_top_k", 5),
                    fetch_k=self._cfg_int("long_term_fetch_k", 20),
                    enable_rerank=self._cfg_bool("long_term_enable_rerank", True),
                    similarity_threshold=self._cfg_float("long_term_similarity_threshold", 0.3),
                )
                if results:
                    lines = []
                    for r in results:
                        lines.append(f"- [id:{r['id']}] {r['content']}")
                    sections.append("### Relevant Long-Term Memory\n" + "\n".join(lines))
                    logger.info(f"[Memory] 检索到 {len(results)} 条相关长期记忆")
                else:
                    logger.debug("[Memory] 未检索到相关长期记忆")
            except Exception as e:
                logger.warning(f"[Memory] 检索长期记忆失败: {e}")

        if not sections:
            return ""

        return "## Memories\n\n" + "\n\n".join(sections)

    # CRUD 操作

    async def save_memory(
        self, session_key: str, content: str, mem_type: str, source: str = "tool"
    ) -> str | None:
        """保存记忆，返回 ID。"""
        if mem_type == "short_term":
            return await self.short_term.add(session_key, content, source=source)
        elif mem_type == "long_term":
            return await self.long_term.save(session_key, content, source=source)
        return None

    async def search_long_term(self, session_key: str, query: str, top_k: int = 5) -> list[dict]:
        """搜索长期记忆。"""
        return await self.long_term.search(
            session_key,
            query=query,
            top_k=top_k,
            fetch_k=self._cfg_int("long_term_fetch_k", 20),
            enable_rerank=self._cfg_bool("long_term_enable_rerank", True),
            similarity_threshold=self._cfg_float("long_term_similarity_threshold", 0.3),
        )

    async def update_memory(self, session_key: str, mem_id: str, content: str) -> bool:
        """更新记忆（自动查找短期/长期）。"""
        ok = await self.short_term.update(session_key, mem_id, content)
        if ok:
            return True
        return await self.long_term.update(session_key, mem_id, content)

    async def delete_memory(self, session_key: str, mem_id: str, mem_type: str) -> bool:
        """删除指定记忆。"""
        if mem_type == "short_term":
            return await self.short_term.delete(session_key, mem_id)
        elif mem_type == "long_term":
            return await self.long_term.delete(session_key, mem_id)
        return False

    # WebUI 列表

    async def list_short_term(self, session_key: str) -> list[dict]:
        return await self.short_term.list_memories(session_key)

    async def list_long_term(self, session_key: str) -> list[dict]:
        return await self.long_term.list_all(session_key)

    # 自动总结触发

    async def on_turn_complete(self, session_key: str, provider, persona_mgr, context_mgr) -> None:
        """轮数 +1，检查是否需要触发自动总结。"""
        if not self._cfg_bool("enable_auto_summary", True):
            return

        turn = await self.short_term.increment_turn(session_key)
        interval = self._cfg_int("memory_summary_interval", 5)
        if interval <= 0:
            return

        data = await self.short_term.load(session_key)
        last = data.get("last_summary_turn", 0)
        gap = turn - last

        logger.info(
            f"[Memory] 轮数追踪: turn={turn}, last_summary={last}, "
            f"interval={interval}, gap={gap}"
        )

        if gap >= interval:
            logger.info(f"[Memory] 达到总结间隔 ({gap}>={interval})，触发自动总结")
            await self._run_summary(session_key, provider, persona_mgr, context_mgr)

    async def on_context_compressed(
        self, session_key: str, provider, persona_mgr, context_mgr
    ) -> None:
        """上下文压缩触发时执行总结。"""
        if not self._cfg_bool("enable_auto_summary", True):
            return
        logger.info("[Memory] 上下文压缩触发，执行记忆总结")
        await self._run_summary(session_key, provider, persona_mgr, context_mgr)

    async def _run_summary(
        self, session_key: str, provider, persona_mgr, context_mgr
    ) -> None:
        """执行一次自动总结。"""
        try:
            data = await self.short_term.load(session_key)
            memories = data.get("memories", [])
            recent_text = await self._get_recent_context(session_key, context_mgr)
            if not memories and not recent_text:
                logger.debug("[Memory] 无记忆且无上下文，跳过总结")
                return

            persona_prompt = ""
            if persona_mgr:
                try:
                    persona_prompt = await persona_mgr.get_system_prompt()
                except Exception:
                    pass

            _provider = provider
            if not _provider and self._provider_getter:
                try:
                    _provider = self._provider_getter()
                except Exception:
                    pass

            result = await self.summarizer.summarize(
                short_term_data=data,
                recent_context_text=recent_text or "",
                persona_prompt=persona_prompt,
                provider=_provider,
            )

            if result:
                await self.short_term.replace_memories(
                    session_key,
                    keeps=result.get("keep", []),
                    deletes=result.get("delete", []),
                    adds=result.get("add", []),
                    updates=result.get("update", {}),
                )
                # 更新 last_summary_turn
                current_data = await self.short_term.load(session_key)
                await self.short_term.set_last_summary_turn(
                    session_key, current_data.get("turn_count", 0)
                )

        except Exception as e:
            logger.error(f"[Memory] 自动总结失败 [{session_key}]: {e}")

    async def _get_recent_context(self, session_key: str, context_mgr) -> str:
        """获取最近几轮对话的纯文本。"""
        if not context_mgr:
            return ""
        try:
            recent_turns = self._cfg_int("memory_summary_recent_turns", 5)
            messages = await context_mgr.load_context(session_key)

            # 只取最近的 user/assistant 消息
            recent = []
            for msg in reversed(messages):
                role = msg.get("role", "")
                if role in ("user", "assistant"):
                    content = msg.get("content", "")
                    if isinstance(content, list):
                        content = " ".join(
                            p.get("text", "") for p in content
                            if isinstance(p, dict) and p.get("type") == "text"
                        )
                    if content:
                        recent.append(f"[{role}]: {content}")
                if len(recent) >= recent_turns * 2:
                    break

            return "\n".join(reversed(recent))
        except Exception:
            return ""

    # 生命周期

    async def on_session_delete(self, session_key: str) -> None:
        """会话删除时，关闭该 session 的 FaissVecDB 实例（释放内存），但保留记忆数据。"""
        # 短期记忆：保留文件（不调用 delete_session）
        # 长期记忆：关闭实例但保留向量数据（不删除目录）
        await self.long_term.close(session_key)
        logger.info(f"[Memory] 会话 {session_key} 已关闭，记忆数据已保留")

    async def close(self) -> None:
        """关闭所有资源。"""
        if self.long_term:
            await self.long_term.close_all()
