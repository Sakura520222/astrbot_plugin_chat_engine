"""ActiveTurnController / ActiveTurnRegistry 单元测试（无 AstrBot 依赖）。"""

import asyncio

import pytest

from astrbot_plugin_chat_engine.context.active_turn import (
    ActiveTurnController,
    ActiveTurnRegistry,
    TurnPhase,
    build_user_row,
)
from astrbot_plugin_chat_engine.context.inflight import InflightMessage


def make_msg(mid: str, text: str, images: list[str] | None = None) -> InflightMessage:
    """构造测试用暂存消息"""
    return InflightMessage(
        id=mid,
        session_key="s",
        sender_name="张三",
        sender_id="u1",
        time_str="19:30",
        text=text,
        images=images or [],
        message_id=mid,
        umo="telegram:FriendMessage:u1",
        user_text=f"张三说：{text}",
    )


@pytest.mark.asyncio
async def test_submit_routes_by_phase():
    """submit 按阶段路由: MODEL→redirect(取消任务), TOOL→steer, 其他→buffer"""
    ctrl = ActiveTurnController("s", 1)

    await ctrl.set_phase(TurnPhase.MODEL_RUNNING)
    task = asyncio.create_task(asyncio.sleep(10))
    await ctrl.register_model_task(task)
    assert await ctrl.submit(make_msg("m1", "抢占")) == "redirect"
    # 模型任务被取消
    with pytest.raises(asyncio.CancelledError):
        await task
    assert ctrl.check_redirect_crossing() is True
    # pending 中有消息
    msgs = await ctrl.take_pending()
    assert [m.id for m in msgs] == ["m1"]
    assert ctrl.acknowledge_redirect() is True
    assert ctrl.check_redirect_crossing() is False

    await ctrl.set_phase(TurnPhase.TOOL_RUNNING)
    assert await ctrl.submit(make_msg("m2", "引导")) == "steer"
    assert [m.id for m in await ctrl.take_pending()] == ["m2"]

    await ctrl.set_phase(TurnPhase.FINALIZING)
    assert await ctrl.submit(make_msg("m3", "收尾")) == "buffer"
    # 收尾消息未入队（由调用方走兜底缓冲）
    assert await ctrl.take_pending() == []


@pytest.mark.asyncio
async def test_submit_model_running_without_task_sets_crossing():
    """MODEL_RUNNING 但任务已结束（响应刚落地）: 不取消，置标记由交叉检查丢弃"""
    ctrl = ActiveTurnController("s", 1)
    await ctrl.set_phase(TurnPhase.MODEL_RUNNING)
    assert await ctrl.submit(make_msg("m1", "迟到的重定向")) == "redirect"
    assert ctrl.check_redirect_crossing() is True
    assert await ctrl.has_pending() is True


@pytest.mark.asyncio
async def test_submit_sending_phase_routes_by_llm_loop():
    """SENDING 阶段: LLM 循环内可 steer（下一模型调用前注入），循环外走 buffer"""
    ctrl = ActiveTurnController("s", 1)
    await ctrl.set_phase(TurnPhase.SENDING)
    assert await ctrl.submit(make_msg("m1", "中间文本发送中")) == "buffer"

    await ctrl.set_llm_loop(True)
    assert await ctrl.submit(make_msg("m2", "循环内发送")) == "steer"
    await ctrl.set_llm_loop(False)
    assert await ctrl.submit(make_msg("m3", "循环外")) == "buffer"


@pytest.mark.asyncio
async def test_unlimited_submissions_all_accepted():
    """引导次数不设上限: 连续提交全部接收，永不因次数退回 buffer"""
    ctrl = ActiveTurnController("s", 1)
    await ctrl.set_phase(TurnPhase.TOOL_RUNNING)
    for i in range(30):
        assert await ctrl.submit(make_msg(f"m{i}", str(i))) == "steer"
    msgs = await ctrl.take_pending()
    assert len(msgs) == 30


@pytest.mark.asyncio
async def test_consume_redirect_for_guards_outer_cancel():
    """consume_redirect_for 仅匹配注册的任务且要求 redirect 标记；外层取消不误判"""
    ctrl = ActiveTurnController("s", 1)
    await ctrl.set_phase(TurnPhase.MODEL_RUNNING)
    task = asyncio.create_task(asyncio.sleep(10))
    await ctrl.register_model_task(task)

    # 未请求 redirect 时: 即使任务被取消也不是 redirect
    assert ctrl.consume_redirect_for(task) is False

    await ctrl.submit(make_msg("m1", "x"))  # 取消 task + 置标记
    assert ctrl.consume_redirect_for(task) is True
    # 二次消费返回 False（仅一次）
    assert ctrl.consume_redirect_for(task) is False
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_requeue_preserves_order_at_head():
    """requeue 把消息放回队首（Provider 失败重注入），新消息排其后"""
    ctrl = ActiveTurnController("s", 1)
    await ctrl.set_phase(TurnPhase.TOOL_RUNNING)
    assert await ctrl.submit(make_msg("m1", "先到")) == "steer"
    early = await ctrl.take_pending()
    await ctrl.requeue(early)
    assert await ctrl.submit(make_msg("m2", "后到")) == "steer"
    msgs = await ctrl.take_pending()
    assert [m.id for m in msgs] == ["m1", "m2"]


def test_build_user_row_variants():
    """user 行构建: 纯文本 / 图片列表 / 纯文本 Provider 图片降级"""
    plain = build_user_row(make_msg("m1", "你好"), text_only_provider=False)
    assert plain["role"] == "user"
    assert plain["content"] == "张三说：你好"

    img = build_user_row(
        make_msg("m2", "看图", images=["http://a.png"]), text_only_provider=False
    )
    assert isinstance(img["content"], list)
    assert img["content"][0] == {"type": "text", "text": "张三说：看图"}
    assert img["content"][1] == {
        "type": "image_url",
        "image_url": {"url": "http://a.png"},
    }

    degraded = build_user_row(
        make_msg("m3", "看图", images=["http://a.png", "http://b.png"]),
        text_only_provider=True,
    )
    assert isinstance(degraded["content"], str)
    assert "[图片 x2" in degraded["content"]

    # 运行期标注: with_note 仅在携带时追加（每批注入的最后一条）
    assert (
        build_user_row(make_msg("m4", "新任务"), False)["content"] == "张三说：新任务"
    )
    noted = build_user_row(make_msg("m5", "新任务"), True, with_note=True)
    assert noted["content"].startswith("张三说：新任务")
    assert "当前任务执行期间到达" in noted["content"]
    assert "无须强行合并成一条回复" in noted["content"], "不得强制单条汇总汇报"
    assert "已经向用户交付的事项" in noted["content"], "须区分执行与交付"
    assert "不代表用户再次发送了该消息" in noted["content"], "须声明历史回放语义"


def test_registry_generation_and_unregister():
    """注册表: 后注册覆盖前代，注销校验代际（迟到注销不误删新回合）"""
    reg = ActiveTurnRegistry()
    c1 = reg.register("s")
    assert reg.get("s") is c1
    c2 = reg.register("s")
    assert reg.get("s") is c2
    assert c2.generation > c1.generation
    # 迟到的 c1 注销不影响 c2
    reg.unregister("s", c1)
    assert reg.get("s") is c2
    reg.unregister("s", c2)
    assert reg.get("s") is None
