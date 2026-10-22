"""Inflight 自动追答端到端测试。

需要在 AstrBot 运行环境内执行（依赖 astrbot 包）；脱离 AstrBot 的
独立测试环境中自动跳过，保持 repo 层测试约束。

核心场景（对应用户需求）：M1 发起长时间 AI 回合（模拟工具调用），
期间 M2/M3 到达被暂存；M1 完成后无需任何新消息，自动将 M2/M3 合并
为一次后续回合处理 —— 全程无逐条排队、无重复回复、无消息丢失。
"""

import asyncio
import types
from types import SimpleNamespace

import pytest

pytest.importorskip("astrbot")

from astrbot_plugin_chat_engine.context.active_turn import ActiveTurnRegistry
from astrbot_plugin_chat_engine.context.inflight import InflightManager
from astrbot_plugin_chat_engine.debounce.manager import MessageDebouncer
from astrbot_plugin_chat_engine.main import ChatEnginePlugin


class FakeEvent:
    """handle_all_messages 路径所需的最小事件桩。"""

    def __init__(self, text: str, mid: str, sender_id: str = "user_1"):
        self.message_str = text
        self.message_obj = SimpleNamespace(message_id=mid)
        self.unified_msg_origin = f"telegram:FriendMessage:{sender_id}"
        self.is_at_or_wake_command = True
        self._sender_id = sender_id
        self.call_llm = None

    def get_sender_name(self):
        return "CherrySakura321"

    def get_sender_id(self):
        return self._sender_id

    def get_group_id(self):
        return None

    def get_platform_name(self):
        return "telegram"

    def get_messages(self):
        return []

    def get_extra(self, key, default=None):
        if key == "activated_handlers":
            return []
        return default

    def should_call_llm(self, call_llm: bool):
        self.call_llm = call_llm

    def stop_event(self):
        pass

    def plain_result(self, text: str):
        return SimpleNamespace(text=text)

    def chain_result(self, components):
        return SimpleNamespace(components=components)


class FakeContext:
    """记录 send_message 调用的 Context 桩。"""

    def __init__(self):
        self.sent: list[str] = []

    async def send_message(self, umo, chain) -> bool:
        self.sent.append("".join(getattr(c, "text", "") for c in chain.chain))
        return True


class FakeContextMgr:
    """真实 InflightManager + handle_all_messages 所需的最小会话管理桩。"""

    def __init__(self, config: dict):
        self.config = config
        self.inflight_mgr = InflightManager()
        self._locks: dict[str, asyncio.Lock] = {}

    def is_group_message(self, event) -> bool:
        return bool(event.get_group_id())

    def build_session_key(self, event) -> str:
        gid = event.get_group_id()
        if gid:
            return f"{event.get_platform_name()}:{gid}"
        return f"{event.get_platform_name()}:private:{event.get_sender_id()}"

    def should_respond(self, event) -> bool:
        return True

    def format_user_message(self, event) -> str:
        fmt = self.config.get("user_id_format", "{{user}{NAME}({ID})}说：")
        prefix = fmt.replace("{NAME}", event.get_sender_name()).replace(
            "{ID}", event.get_sender_id()
        )
        return f"{prefix}{event.message_str}"

    def _extract_reply_context(self, event) -> str:
        return ""

    def get_session_lock(self, session_key: str) -> asyncio.Lock:
        if session_key not in self._locks:
            self._locks[session_key] = asyncio.Lock()
        return self._locks[session_key]


class FakeTurnRunner:
    """替换 _execute_llm_turn：记录调用并按脚本逐轮执行。"""

    def __init__(self):
        self.calls: list[dict] = []
        self.behaviors: list = []  # 每项 async (idx, kwargs) -> str | None

    async def __call__(self, **kwargs):
        self.calls.append(kwargs)
        idx = len(self.calls) - 1
        behavior = self.behaviors[min(idx, len(self.behaviors) - 1)]
        return await behavior(idx, kwargs)


def make_plugin(config_overrides: dict | None = None) -> ChatEnginePlugin:
    """构造仅装配编排链路（handler → 防抖 → 回合 → 自动追答）的插件骨架。"""
    config = {
        "enable_message_debounce": True,
        "debounce_window_ms": 500,
        "debounce_max_messages": 10,
        "debounce_scope": "all",
        "debounce_merge_mode": "concat",
        "debounce_separator": "\n",
        "debounce_absorb_passive": True,
        "enable_inflight_guidance": True,
        "max_inflight_guidance_messages": 10,
        "max_inflight_followup_rounds": 5,
        "split_delay_ms": 0,
        "user_id_format": "{{user}{NAME}({ID})}说：",
        "require_at_in_group": True,
        **(config_overrides or {}),
    }
    p = ChatEnginePlugin.__new__(ChatEnginePlugin)
    p.config = config
    p.context = FakeContext()
    p.context_mgr = FakeContextMgr(config)
    p.proactive_mgr = None
    p.memory_mgr = None
    p.debouncer = MessageDebouncer(config, p._process_debounced_messages)
    p._inflight_followup_rounds = {}
    p._active_turns = ActiveTurnRegistry()
    # 会话命令与图片提取与追答链路无关，桩掉避免触及其他子系统
    p._try_handle_session_cmd = types.MethodType(lambda self, event, text: _none(), p)
    return p


async def _none():
    return None


async def deliver(p: ChatEnginePlugin, text: str, mid: str) -> None:
    """以事件驱动 handle_all_messages（消费其 async generator）。"""
    agen = p.handle_all_messages(FakeEvent(text, mid))
    async for _ in agen:
        pass


async def wait_until(predicate, timeout_s: float = 8.0, interval: float = 0.01):
    """轮询等待条件成立，超时返回 False。"""
    deadline = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return predicate()


def shutdown(p: ChatEnginePlugin) -> None:
    """静默关闭防抖定时器，避免测试结束时任务销毁告警。"""
    p.debouncer._closed = True
    for t in list(p.debouncer._timers.values()):
        t.cancel()


@pytest.mark.asyncio
async def test_auto_followup_merges_inflight_messages():
    """M1 长回合期间 M2/M3 暂存，M1 完成后无需新消息自动合并处理。"""
    p = make_plugin()
    runner = FakeTurnRunner()

    async def long_tool_turn(idx, kwargs):
        # 模拟长时间工具调用轮次，期间测试注入 M2/M3
        await asyncio.sleep(0.4)
        await kwargs["on_text_segment"]("reply-1", True, None)
        return "reply-1"

    async def followup_turn(idx, kwargs):
        await kwargs["on_text_segment"]("reply-2", True, None)
        return "reply-2"

    runner.behaviors = [long_tool_turn, followup_turn]
    p._execute_llm_turn = runner

    await deliver(p, "M1 帮我搜索地平线系列产品", "1941")
    key = "telegram:private:user_1"
    assert await wait_until(lambda: p.context_mgr.inflight_mgr.is_inflight(key)), (
        "第 1 轮应已开始执行"
    )

    # 第 1 轮执行期间 M2/M3 到达 → 应被暂存而非独立排队
    await deliver(p, "M2 再找一下千恋万花最新消息", "1942")
    await deliver(p, "M3 顺便看看天气预报", "1943")
    assert p.context_mgr.inflight_mgr.get_buffered_count(key) == 2

    # 第 1 轮完成 → 自动追答窗口 → 第 2 轮完成
    assert await wait_until(lambda: len(runner.calls) >= 2), "应自动发起第 2 轮"
    await asyncio.sleep(0.3)  # 等待第 2 轮发送与收尾彻底结束

    # 无逐条排队：恰好 2 轮；无消息丢失：M2/M3 均在第 2 轮合并输入中
    assert len(runner.calls) == 2
    content = runner.calls[1]["user_msg"]["content"]
    assert "M2 再找一下千恋万花最新消息" in content
    assert "M3 顺便看看天气预报" in content
    assert p.context_mgr.inflight_mgr.get_buffered_count(key) == 0
    assert not p.context_mgr.inflight_mgr.is_inflight(key)

    # 无重复回复：两轮各发送恰好一条
    assert p.context.sent.count("reply-1") == 1
    assert p.context.sent.count("reply-2") == 1
    shutdown(p)


@pytest.mark.asyncio
async def test_followup_rounds_cap_and_reset_on_real_message():
    """连续自动追答达到上限后停止；用户真实消息到达时计数清零并恢复处理。"""
    p = make_plugin({"max_inflight_followup_rounds": 1})
    runner = FakeTurnRunner()

    async def turn_1(idx, kwargs):
        await asyncio.sleep(0.3)
        await kwargs["on_text_segment"]("reply-1", True, None)
        return "reply-1"

    async def turn_2(idx, kwargs):
        # 追答轮也模拟耗时，期间注入 M4
        await asyncio.sleep(0.3)
        await kwargs["on_text_segment"]("reply-2", True, None)
        return "reply-2"

    async def later_turn(idx, kwargs):
        await kwargs["on_text_segment"](f"reply-{idx + 1}", True, None)
        return f"reply-{idx + 1}"

    runner.behaviors = [turn_1, turn_2, later_turn, later_turn]
    p._execute_llm_turn = runner

    await deliver(p, "M1", "1001")
    key = "telegram:private:user_1"
    assert await wait_until(lambda: p.context_mgr.inflight_mgr.is_inflight(key))
    await deliver(p, "M2", "1002")  # 第 1 轮期间暂存

    # 第 2 轮（自动追答 M2）开始后，再注入 M4
    assert await wait_until(lambda: len(runner.calls) >= 2)
    assert await wait_until(lambda: p.context_mgr.inflight_mgr.is_inflight(key))
    await deliver(p, "M4", "1004")  # 第 2 轮期间暂存

    # 第 2 轮结束：连续追答已达上限(1)，M4 不再自动追答
    await wait_until(lambda: not p.context_mgr.inflight_mgr.is_inflight(key))
    await asyncio.sleep(1.0)
    assert len(runner.calls) == 2, "达到上限后不应继续自动追答"
    assert p.context_mgr.inflight_mgr.get_buffered_count(key) == 1, "M4 应保留待注入"

    # 用户真实消息 M5 到达：计数清零，正常处理 M5
    await deliver(p, "M5", "1005")
    assert await wait_until(lambda: len(runner.calls) >= 3)
    # M5 回合结束后，M4 作为暂存消息被自动追答（计数已清零）
    assert await wait_until(lambda: len(runner.calls) >= 4)
    await asyncio.sleep(0.3)
    assert p.context_mgr.inflight_mgr.get_buffered_count(key) == 0
    assert len(runner.calls) == 4
    shutdown(p)


@pytest.mark.asyncio
async def test_failed_turn_does_not_chase():
    """回合失败（无有效回复）时不自动追答，暂存消息保留待搭车注入。"""
    p = make_plugin()
    runner = FakeTurnRunner()

    async def failing_turn(idx, kwargs):
        await asyncio.sleep(0.3)  # 保持回合可观察，期间 M2 到达被暂存
        return None  # 模拟 Provider 全部失败

    runner.behaviors = [failing_turn]
    p._execute_llm_turn = runner

    await deliver(p, "M1", "2001")
    key = "telegram:private:user_1"
    assert await wait_until(lambda: p.context_mgr.inflight_mgr.is_inflight(key))
    await deliver(p, "M2", "2002")  # 暂存

    await wait_until(lambda: not p.context_mgr.inflight_mgr.is_inflight(key))
    await asyncio.sleep(1.2)  # 超过防抖窗口 + 追答窗口
    assert len(runner.calls) == 1, "失败回合不应触发自动追答"
    assert p.context_mgr.inflight_mgr.get_buffered_count(key) == 1, "M2 保留待下次注入"
    shutdown(p)
