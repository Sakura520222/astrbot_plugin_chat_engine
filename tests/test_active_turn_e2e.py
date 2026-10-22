"""活跃回合实时引导（Redirect/Steer）端到端测试。

需要在 AstrBot 运行环境内执行；独立环境下自动跳过。
直接驱动真实 `_llm_call_with_tools`（Redirect/Steer 引擎）与真实
ActiveTurnController，仅替换 Provider / 工具执行 / 分段发送，验证
**实际模型输入**与事件时序，而非仅最终回复次数。

覆盖验收场景:
- A 模型生成中抢占: M2 取消生成中的请求，同回合重建输入
- B 工具执行中引导: M3 在工具批次边界作为真实 user 消息注入
- C 连续消息与竞态: 多条消息合并注入、响应-重定向交叉丢弃过时响应
- D 异常与回退: Redirect 不进入异常路径（不触发 Provider Fallback）、
  Provider 失败后 requeue 保序重注入
"""

import asyncio
from types import SimpleNamespace

import pytest

pytest.importorskip("astrbot")

from astrbot_plugin_chat_engine.context.active_turn import (
    ActiveTurnRegistry,
    TurnPhase,
)
from astrbot_plugin_chat_engine.context.inflight import InflightMessage
from astrbot_plugin_chat_engine.main import (
    ChatEnginePlugin,
    _TURN_REDIRECTED,
    _tool_call_ctx,
)


class FakeResp:
    """Provider 响应对象（tools: list[(name, id, args)]），含工具调用序列化。"""

    def __init__(self, text="", tools=None):
        import json

        self.role = "assistant"
        self.completion_text = text
        self.tools_call_name = [t[0] for t in tools] if tools else None
        self.tools_call_ids = [t[1] for t in tools] if tools else []
        self.tools_call_args = [t[2] for t in tools] if tools else []
        self.result_chain = None
        self._tools = tools or []
        self._dumped = [
            {
                "id": cid,
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": json.dumps(args, ensure_ascii=False),
                },
            }
            for name, cid, args in self._tools
        ]

    def to_openai_tool_calls_model(self):
        return [
            SimpleNamespace(model_dump=lambda exclude_none=None, _d=d: dict(_d))
            for d in self._dumped
        ]


def llm_resp(text="", tools=None):
    """构造 Provider 响应对象"""
    return FakeResp(text, tools)


def make_msg(mid: str, text: str) -> InflightMessage:
    """构造引导消息"""
    return InflightMessage(
        id=mid,
        session_key="telegram:private:u1",
        sender_name="CherrySakura321",
        sender_id="u1",
        time_str="19:30",
        text=text,
        message_id=mid,
        umo="telegram:FriendMessage:u1",
        user_text=f"CherrySakura321(u1)说：{text}",
    )


class FakeProvider:
    """脚本化 Provider: 记录每次调用的实际模型输入，行为可控可取消。"""

    def __init__(self, behaviors):
        self.behaviors = list(behaviors)
        self.calls: list[dict] = []
        self.completed: list[int] = []  # 正常返回的调用序号（0-based）

    async def text_chat(self, **kwargs):
        idx = len(self.calls)
        self.calls.append(kwargs)
        behavior = self.behaviors[min(idx, len(self.behaviors) - 1)]
        if callable(behavior):
            result = behavior(idx, kwargs)
            if asyncio.iscoroutine(result):
                result = await result
            resp = result
        else:
            resp = behavior
        self.completed.append(idx)
        return resp


def make_plugin(tool_impl=None):
    """装配 _llm_call_with_tools 所需的最小插件骨架。"""
    p = ChatEnginePlugin.__new__(ChatEnginePlugin)
    p.config = {"max_tool_rounds": 5}
    # 隔离分段/清洗/统计子系统
    p._clean_response = lambda text: text
    p._accumulate_token_usage = lambda *a, **k: None
    p._last_tool_images = None

    async def _segments(text):
        yield text

    p._iter_text_segments = _segments

    executed: list[tuple[str, dict]] = []

    async def _execute_tool(tool_name, tool_args, tool_set=None, event=None):
        executed.append((tool_name, dict(tool_args)))
        if tool_impl is not None:
            return await tool_impl(tool_name, tool_args)
        return "ok"

    p._execute_tool = _execute_tool
    p._active_turns = ActiveTurnRegistry()
    return p, executed


async def run_turn(p, ctrl, provider, user_text="M1 在干嘛呢"):
    """驱动完整工具循环，返回 (yield 输出, 最终响应)。"""
    outs = []
    agen = p._llm_call_with_tools(
        provider=provider,
        system_prompt="SYS",
        contexts=[],
        user_msg={"role": "user", "message_id": "m0", "content": user_text},
        turn_ctrl=ctrl,
    )
    async for item in agen:
        outs.append(item)
    ctx = _tool_call_ctx.get()
    return outs, (ctx.final_response if ctx else None)


def contexts_text(kwargs) -> str:
    """提取一次模型调用的全部上下文文本（验证实际模型输入）。"""
    parts = []
    for m in kwargs["contexts"]:
        c = m.get("content")
        if isinstance(c, str):
            parts.append(c)
        elif isinstance(c, list):
            parts.extend(x.get("text", "") for x in c if isinstance(x, dict))
    return "\n".join(parts)


async def wait_until(predicate, timeout_s=5.0, interval=0.005):
    """轮询等待条件成立。"""
    for _ in range(int(timeout_s / interval)):
        if predicate():
            return True
        await asyncio.sleep(interval)
    return predicate()


# ---------------------------------------------------------------------------
# 场景 A: 模型生成中抢占
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scenario_a_redirect_during_model_generation():
    """M1 生成期间 M2 到达: 立即取消过时请求，同回合以含 M2 的输入重试。"""
    p, _ = make_plugin()

    async def generating(idx, kwargs):
        await asyncio.sleep(5)  # 模拟长时间生成
        return llm_resp(f"对 M1 的过时回答 #{idx}")

    provider = FakeProvider(
        [generating, lambda idx, kw: llm_resp("好的，马上去搜《初雪樱》！")]
    )
    ctrl = p._active_turns.register("telegram:private:u1")

    turn_task = asyncio.create_task(run_turn(p, ctrl, provider))
    # 等待第 1 次请求在飞（任务已注册到控制器）
    assert await wait_until(lambda: provider.calls and ctrl._model_task is not None), (
        "第 1 次模型请求应已发出"
    )

    verdict = await ctrl.submit(make_msg("m2", "帮我搜索一下初雪樱"))
    assert verdict == "redirect"

    outs, final = await turn_task

    # 第 1 次请求被抢占、从未完成；第 2 次以包含 M2 的输入重试（同 Provider）
    assert len(provider.calls) == 2
    assert 0 not in provider.completed, "被抢占的请求不得返回结果"
    assert "帮我搜索一下初雪樱" in contexts_text(provider.calls[1])
    assert "对 M1 的过时回答" not in contexts_text(provider.calls[1])
    # 最终回复转向 M2 的任务（最终文本经 ctx.final_response 交付）
    assert final is not None and final.completion_text == "好的，马上去搜《初雪樱》！"


# ---------------------------------------------------------------------------
# 场景 B: 工具执行中引导
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scenario_b_steer_at_tool_boundary():
    """工具执行期间 M3 到达: 工具不被中断，结果保留，M3 于批次边界注入。"""
    tool_started = asyncio.Event()

    async def slow_tool(name, args):
        tool_started.set()
        await asyncio.sleep(0.3)  # 模拟 web_search 执行
        return "《初雪樱》搜索结果"

    p, executed = make_plugin(tool_impl=slow_tool)
    provider = FakeProvider(
        [
            lambda idx, kw: llm_resp(
                "正在搜索", tools=[("web_search", "call_1", {"query": "初雪樱"})]
            ),
            lambda idx, kw: llm_resp("综合初雪樱与千恋万花的回答"),
        ]
    )
    ctrl = p._active_turns.register("telegram:private:u1")

    turn_task = asyncio.create_task(run_turn(p, ctrl, provider))
    assert await wait_until(tool_started.is_set), "工具应已开始执行"
    assert ctrl.phase is TurnPhase.TOOL_RUNNING

    verdict = await ctrl.submit(make_msg("m3", "再找一下千恋万花最新消息"))
    assert verdict == "steer"

    outs, final = await turn_task

    # 工具恰好执行一次（结果保留，不因引导重放）
    assert executed == [("web_search", {"query": "初雪樱"})]
    # 第 2 次调用可见 M3: user 行位于 tool 结果之后（合法 tool → user 序列）
    ctxs = provider.calls[1]["contexts"]
    roles = [m.get("role") for m in ctxs]
    tool_idx = roles.index("tool")
    steer_rows = [
        i
        for i, m in enumerate(ctxs)
        if m.get("role") == "user" and "再找一下千恋万花" in str(m.get("content"))
    ]
    assert steer_rows and steer_rows[0] > tool_idx, (
        f"Steer user 行应在 tool 结果之后: roles={roles}"
    )
    # 注入行携带运行期标注，指引模型按新消息更新工作而非汇总汇报
    assert "当前任务执行期间到达" in str(ctxs[steer_rows[0]]["content"]), (
        "Steer 注入行应携带运行提示"
    )
    assert "《初雪樱》搜索结果" in contexts_text(provider.calls[1]), "工具结果须保留"
    assert final.completion_text == "综合初雪樱与千恋万花的回答"
    # 中间文本照常即时输出
    assert ("text", "正在搜索") in outs


# ---------------------------------------------------------------------------
# 场景 C: 连续消息与竞态
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scenario_c1_redirect_response_crossing_discards_stale():
    """响应返回与重定向交叉: 过时响应被丢弃，输入重建后重试。"""
    p, _ = make_plugin()

    async def instant(idx, kwargs):
        return llm_resp("针对 M1 的完整回答（已过时）")

    provider = FakeProvider([instant])
    ctrl = p._active_turns.register("telegram:private:u1")

    # 直接驱动 _guarded_text_chat 验证交叉分支: redirect 标记置位于任务
    # 注册间隙（无法取消），响应正常返回后由交叉检查丢弃并注入重建
    await ctrl.set_phase(TurnPhase.MODEL_RUNNING)
    assert await ctrl.submit(make_msg("m2", "改一下需求")) == "redirect"

    from astrbot_plugin_chat_engine.main import _ToolCallContext

    ctx = _ToolCallContext()
    working_ctxs: list[dict] = [{"role": "user", "content": "M1"}]
    result = await p._guarded_text_chat(
        ctrl,
        provider,
        {"prompt": None, "contexts": working_ctxs, "system_prompt": "SYS"},
        working_ctxs,
        ctx,
        False,
    )
    assert result is _TURN_REDIRECTED, "交叉应转译为重建哨兵而非返回过时响应"
    assert provider.completed == [0], "响应本身正常返回（非取消路径）"
    assert "改一下需求" in contexts_text({"contexts": working_ctxs}), (
        "重定向消息须注入工作上下文供重建"
    )
    assert ctx.turned_messages and ctx.turned_messages[0].id == "m2"


@pytest.mark.asyncio
async def test_scenario_c2_multiple_messages_merge_into_one_rebuild():
    """M2/M3 在抢占后、重建前相继到达: 合并为一次重建输入，逐条可见。"""
    p, _ = make_plugin()

    async def generating(idx, kwargs):
        await asyncio.sleep(5)
        return llm_resp("过时回答")

    released = asyncio.Event()

    async def second(idx, kwargs):
        released.set()  # 重建后的请求已发出，此后到达的消息走下一轮
        return llm_resp("综合 M2 与 M3 的回答")

    provider = FakeProvider([generating, second])
    ctrl = p._active_turns.register("telegram:private:u1")

    turn_task = asyncio.create_task(run_turn(p, ctrl, provider))
    assert await wait_until(lambda: provider.calls and ctrl._model_task is not None)

    # M2 触发抢占；取消传播期间 M3 立即到达，应合并进同一次重建
    assert await ctrl.submit(make_msg("m2", "搜初雪樱")) == "redirect"
    assert await ctrl.submit(make_msg("m3", "再看千恋万花")) == "redirect"

    outs, final = await turn_task
    assert released.is_set()
    assert len(provider.calls) == 2, "M2/M3 应合并为一次重建而非两次抢占重试"
    text = contexts_text(provider.calls[1])
    assert "搜初雪樱" in text and "再看千恋万花" in text
    assert final.completion_text == "综合 M2 与 M3 的回答"


# ---------------------------------------------------------------------------
# 场景 D: 异常与回退语义
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scenario_d_redirect_is_not_provider_failure():
    """Redirect 取消走哨兵而非异常: 不计入 Provider 失败、不触发 Fallback。"""
    p, _ = make_plugin()

    async def generating(idx, kwargs):
        await asyncio.sleep(5)
        return llm_resp("过时")

    provider = FakeProvider([generating, lambda idx, kw: llm_resp("转向后回答")])
    ctrl = p._active_turns.register("telegram:private:u1")

    turn_task = asyncio.create_task(run_turn(p, ctrl, provider))
    assert await wait_until(lambda: provider.calls and ctrl._model_task is not None)
    await ctrl.submit(make_msg("m2", "转向"))

    # 正常完成、无异常抛出（异常会被 _execute_llm_turn 计为 Provider 失败）
    outs, final = await asyncio.wait_for(turn_task, timeout=5)
    assert final.completion_text == "转向后回答"
    # 哨兵未泄漏到输出
    assert all(item is not _TURN_REDIRECTED for item in outs)


@pytest.mark.asyncio
async def test_scenario_d_provider_failure_requeues_turned_messages():
    """Provider 真实失败切换候选: 已注入消息 requeue 后随下一候选重注入。

    Provider[0] 回合内注入了 M2 后失败 → _execute 语义层 requeue →
    Provider[1] 的 _llm_call_with_tools 循环顶部以真实 user 行重注入，
    消息不随 Provider 失败丢失、不重复消费。
    """
    p, _ = make_plugin()
    ctrl = p._active_turns.register("telegram:private:u1")

    # 模拟 Provider[0] 的回合: 消息已消费（注入其 current_contexts）
    m2 = make_msg("m2", "搜初雪樱")
    await ctrl.set_phase(TurnPhase.TOOL_RUNNING)
    assert await ctrl.submit(m2) == "steer"
    assert [m.id for m in await ctrl.take_pending()] == ["m2"]
    # _execute 失败分支语义: requeue 回队首
    await ctrl.requeue([m2])

    # Provider[1] 的回合: 循环顶部（首次模型调用前）注入 requeue 消息
    provider = FakeProvider([lambda i, kw: llm_resp("Provider2 的回答")])
    outs, final = await run_turn(p, ctrl, provider)
    assert "搜初雪樱" in contexts_text(provider.calls[0])
    assert final.completion_text == "Provider2 的回答"
