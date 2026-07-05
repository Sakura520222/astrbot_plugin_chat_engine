"""消息抖动管理器 — 收集短时间内的多条消息，合并后一次性发给 LLM。

工作原理:
    1. 消息到达时放入缓冲区，启动 / 重置计时器
    2. 计时器到期（无新消息）后，合并所有缓冲消息并处理
    3. 缓冲区满时立即处理，不等计时器

配置项:
    - enable_message_debounce (bool, 默认 false): 是否启用消息抖动
    - debounce_window_ms      (int,  默认 2000) : 等待窗口（毫秒）
    - debounce_max_messages   (int,  默认 10)   : 最大缓冲消息数，超出立即处理
    - debounce_scope          (str,  默认 group): 适用范围 "group" | "private" | "all"
    - debounce_merge_mode     (str,  默认 concat): 合并模式 "concat" | "numbered"
    - debounce_separator      (str,  默认 \\n)   : 消息分隔符
"""

import asyncio
from collections.abc import Awaitable, Callable

from astrbot.api import logger

from ..utils.config import cfg_bool, cfg_int


class MessageDebouncer:
    """消息抖动管理器"""

    def __init__(
        self,
        config: dict,
        process_fn: Callable[[str, list[dict]], Awaitable[None]],
    ):
        """
        Args:
            config: 插件配置字典
            process_fn: 异步处理函数 ``(session_key, messages) -> None``
        """
        self.config = config
        self._process_fn = process_fn
        # session_key -> 消息列表
        self._buffers: dict[str, list[dict]] = {}
        # session_key -> 计时器 Task
        self._timers: dict[str, asyncio.Task] = {}
        # 防止 _on_timer 和 force_flush 并发 pop 同一 session 的缓冲
        self._flush_locks: dict[str, asyncio.Lock] = {}
        # 正在执行 _process_fn 的任务（含已从 _timers 弹出的）
        self._inflight: set[asyncio.Task] = set()
        self._closed = False

    def _get_flush_lock(self, session_key: str) -> asyncio.Lock:
        """获取指定会话的 flush 锁（懒创建）。"""
        lock = self._flush_locks.get(session_key)
        if lock is None:
            lock = asyncio.Lock()
            self._flush_locks[session_key] = lock
        return lock

    def _cleanup_lock(self, session_key: str) -> None:
        """清理指定会话的 flush 锁，避免无界内存增长。

        仅在对应 session 无活跃缓冲且无未完成计时器时才移除锁，
        避免新消息到达时的无锁窗口。
        """
        if session_key not in self._buffers and session_key not in self._timers:
            self._flush_locks.pop(session_key, None)

    # ------------------------------------------------------------------
    # 公共接口
    # ------------------------------------------------------------------

    def should_debounce(self, is_group: bool) -> bool:
        """判断当前消息是否应进入抖动缓冲。"""
        if not cfg_bool(self.config, "enable_message_debounce", False):
            return False
        scope = self.config.get("debounce_scope", "group")
        if scope not in ("group", "private", "all"):
            logger.warning(f"[Debounce] 无效 debounce_scope='{scope}'，回退到 'group'")
            scope = "group"
        if scope == "group" and not is_group:
            return False
        if scope == "private" and is_group:
            return False
        return True

    async def add_message(self, session_key: str, msg_data: dict) -> bool:
        """添加一条消息到缓冲区。

        Returns:
            ``True`` 表示缓冲区已满，调用方应立即 :meth:`force_flush`。
        """
        if self._closed:
            return False

        buf = self._buffers.setdefault(session_key, [])
        buf.append(msg_data)

        # 取消旧计时器，并等待其真正停止（Bug 修复：避免旧 timer 在 sleep
        # 结束后与新 force_flush / _on_timer 竞争缓冲区）
        old_task = self._timers.get(session_key)
        if old_task and not old_task.done():
            old_task.cancel()
            try:
                await old_task
            except (asyncio.CancelledError, Exception):
                pass

        # 缓冲区已满 → 不启动新计时器，由调用方 force_flush
        max_msgs = max(1, min(cfg_int(self.config, "debounce_max_messages", 10), 100))
        if len(buf) >= max_msgs:
            return True

        # 启动新计时器
        window_ms = max(
            500, min(cfg_int(self.config, "debounce_window_ms", 2000), 30000)
        )
        window_s = window_ms / 1000.0
        self._timers[session_key] = asyncio.create_task(
            self._on_timer(session_key, window_s)
        )
        logger.debug(f"[Debounce] 缓冲消息: {session_key}, 当前 {len(buf)} 条")
        return False

    def try_add_passive(self, session_key: str, msg_data: dict) -> bool:
        """尝试将被动消息并入指定会话的活跃缓冲。

        与 :meth:`add_message` 的区别：
        - 不重置计时器（窗口仍由最后一条激活消息起算），被动消息只追加；
        - 不计入 ``debounce_max_messages`` 满载判定，避免闲聊把缓冲撑到立即处理。

        方法体内无 ``await``，check 与 append 在同一事件循环步内原子完成，
        不会与 ``_on_timer`` 的 pop 交错。

        Returns:
            ``True`` 表示成功并入；``False`` 表示当前无活跃缓冲
            （缓冲为空或计时器已结束），调用方应回退到被动记录流程。
        """
        if self._closed:
            return False

        buf = self._buffers.get(session_key)
        task = self._timers.get(session_key)
        if not buf or task is None or task.done():
            return False

        buf.append(msg_data)
        logger.debug(
            f"[Debounce] 被动消息并入: {session_key}, 当前 {len(buf)} 条"
        )
        return True

    async def force_flush(self, session_key: str) -> None:
        """立即处理指定会话的缓冲消息（用于缓冲区满或插件关闭）。"""
        async with self._get_flush_lock(session_key):
            # 取消计时器并等待其真正停止，避免竞态
            task = self._timers.pop(session_key, None)
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
            messages = self._buffers.pop(session_key, [])
        if messages:
            logger.info(f"[Debounce] 强制刷新: {session_key}, {len(messages)} 条消息")
            await self._invoke_process_fn(session_key, messages)

    async def flush_all(self) -> None:
        """刷新所有缓冲消息。"""
        all_keys = list(self._buffers.keys())
        for key in all_keys:
            await self.force_flush(key)

    async def close(self) -> None:
        """关闭抖动管理器，先处理剩余缓冲消息再取消计时器，最后等待进行中的任务完成。"""
        self._closed = True
        # 先刷新所有未处理的缓冲消息，避免静默丢失
        await self.flush_all()
        # 取消残留计时器（flush_all 期间新启动的或已在 sleep 中的）
        for task in self._timers.values():
            if not task.done():
                task.cancel()
        self._timers.clear()
        self._buffers.clear()
        # 等待所有正在执行 _process_fn 的任务完成，确保不会在资源关闭后访问
        if self._inflight:
            await asyncio.gather(*self._inflight, return_exceptions=True)
        self._inflight.clear()
        self._flush_locks.clear()

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------

    async def _invoke_process_fn(self, session_key: str, messages: list[dict]) -> None:
        """包装 _process_fn 调用，追踪任务生命周期并清理锁。"""
        # 将 _process_fn 包装为独立 Task 以便追踪
        task = asyncio.create_task(self._do_process(session_key, messages))
        self._inflight.add(task)
        try:
            await task
        finally:
            self._inflight.discard(task)
            # 清理 flush 锁（不再有缓冲或计时器引用此 session）
            self._cleanup_lock(session_key)

    async def _do_process(self, session_key: str, messages: list[dict]) -> None:
        """实际执行 _process_fn，捕获并记录异常。"""
        try:
            await self._process_fn(session_key, messages)
        except Exception as e:
            logger.error(f"[Debounce] 处理失败: {e}", exc_info=True)

    async def _on_timer(self, session_key: str, delay: float) -> None:
        """计时器回调 — 等待窗口到期后处理缓冲消息。"""
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return

        if self._closed:
            return

        async with self._get_flush_lock(session_key):
            messages = self._buffers.pop(session_key, [])
            self._timers.pop(session_key, None)

        if not messages:
            self._cleanup_lock(session_key)
            return

        logger.info(f"[Debounce] 窗口到期，处理: {session_key}, {len(messages)} 条消息")
        await self._invoke_process_fn(session_key, messages)
