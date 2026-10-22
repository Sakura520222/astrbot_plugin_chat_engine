import asyncio
import pytest

from astrbot_plugin_chat_engine.context.inflight import InflightManager, InflightMessage


def test_inflight_message_creation():
    """测试 InflightMessage 数据结构创建与属性"""
    msg = InflightMessage(
        id="msg_1",
        session_key="test_session",
        sender_name="张三",
        sender_id="user_123",
        time_str="19:30",
        text="不对，我说的是北京",
        images=["http://example.com/img1.png"],
        message_id="mid_1",
    )
    assert msg.sender_name == "张三"
    assert msg.time_str == "19:30"
    assert msg.text == "不对，我说的是北京"
    assert msg.images == ["http://example.com/img1.png"]


@pytest.mark.asyncio
async def test_acquire_and_release_session():
    """测试会话执行权的原子获取与释放"""
    mgr = InflightManager()
    session_key = "session_a"

    assert not mgr.is_inflight(session_key)

    # 首次 acquire 应成功
    assert await mgr.acquire_session(session_key) is True
    assert mgr.is_inflight(session_key) is True

    # 其他并发协程再次 acquire 应失败
    other_task_acquired = False

    async def try_other():
        nonlocal other_task_acquired
        other_task_acquired = await mgr.acquire_session(session_key)

    await asyncio.create_task(try_other())
    assert other_task_acquired is False

    # 释放后应恢复
    await mgr.release_session(session_key)
    assert not mgr.is_inflight(session_key)

    # 释放后可以再次获取
    assert await mgr.acquire_session(session_key) is True
    await mgr.release_session(session_key)


@pytest.mark.asyncio
async def test_session_isolation():
    """测试跨会话隔离：不同会话之间互不影响"""
    mgr = InflightManager()
    session_a = "session_a"
    session_b = "session_b"

    assert await mgr.acquire_session(session_a) is True
    assert mgr.is_inflight(session_a) is True
    assert mgr.is_inflight(session_b) is False

    # session_b 依然可以正常 acquire
    assert await mgr.acquire_session(session_b) is True
    assert mgr.is_inflight(session_b) is True

    await mgr.release_session(session_a)
    assert mgr.is_inflight(session_a) is False
    assert mgr.is_inflight(session_b) is True

    await mgr.release_session(session_b)
    assert mgr.is_inflight(session_b) is False


@pytest.mark.asyncio
async def test_add_and_stage_messages_order_and_truncation():
    """测试消息暂存、顺序保留以及超过上限时的截断与溢出"""
    mgr = InflightManager()
    session_key = "session_a"

    # 添加 15 条消息
    for i in range(15):
        await mgr.add_inflight_message(
            session_key,
            InflightMessage(
                id=f"msg_{i}",
                session_key=session_key,
                sender_name=f"User{i}",
                sender_id=f"uid_{i}",
                time_str=f"19:{i:02d}",
                text=f"消息内容_{i}",
                images=[],
                message_id=f"mid_{i}",
            ),
        )

    assert mgr.get_buffered_count(session_key) == 15

    # 单轮上限设为 10
    gen, guide_msgs, overflow_msgs = await mgr.stage_messages(
        session_key, max_messages=10
    )

    # 溢出前 5 条较早消息
    assert len(overflow_msgs) == 5
    assert [m.id for m in overflow_msgs] == [f"msg_{i}" for i in range(5)]

    # 引导包含最近 10 条消息，且保持时间升序
    assert len(guide_msgs) == 10
    assert [m.id for m in guide_msgs] == [f"msg_{i}" for i in range(5, 15)]

    # buffer 应已被排空
    assert mgr.get_buffered_count(session_key) == 0


@pytest.mark.asyncio
async def test_stage_commit_and_rollback():
    """测试暂存消息的事务性 Commit 与 Rollback"""
    mgr = InflightManager()
    session_key = "session_a"

    msg1 = InflightMessage(
        id="msg_1",
        session_key=session_key,
        sender_name="张三",
        sender_id="u1",
        time_str="19:30",
        text="第一条",
        images=[],
        message_id="m1",
    )
    msg2 = InflightMessage(
        id="msg_2",
        session_key=session_key,
        sender_name="李四",
        sender_id="u2",
        time_str="19:31",
        text="第二条",
        images=[],
        message_id="m2",
    )
    await mgr.add_inflight_message(session_key, msg1)
    await mgr.add_inflight_message(session_key, msg2)

    # 1. 测试 Rollback（模拟 LLM 或数据库保存失败）
    gen1, guide1, overflow1 = await mgr.stage_messages(session_key, max_messages=10)
    assert len(guide1) == 2
    assert mgr.get_buffered_count(session_key) == 0

    # 执行 rollback
    await mgr.rollback_stage(session_key, gen1)
    # 消息应该回滚到 buffer 中，且顺序一致
    assert mgr.get_buffered_count(session_key) == 2

    # 2. 再次 stage 并 commit
    gen2, guide2, overflow2 = await mgr.stage_messages(session_key, max_messages=10)
    assert len(guide2) == 2
    assert [m.id for m in guide2] == ["msg_1", "msg_2"]

    # 执行 commit
    await mgr.commit_stage(session_key, gen2)
    assert mgr.get_buffered_count(session_key) == 0

    # 尝试再次 rollback（应该无效，因为已 commit）
    await mgr.rollback_stage(session_key, gen2)
    assert mgr.get_buffered_count(session_key) == 0


@pytest.mark.asyncio
async def test_reset_session_generation_prevents_stale_rollback():
    """测试会话重置（/new, /clear, /switch 等）时的代际隔离：丢弃 staged 消息，防止回流"""
    mgr = InflightManager()
    session_key = "session_a"

    msg = InflightMessage(
        id="msg_1",
        session_key=session_key,
        sender_name="张三",
        sender_id="u1",
        time_str="19:30",
        text="旧消息",
        images=[],
        message_id="m1",
    )
    await mgr.add_inflight_message(session_key, msg)

    # Stage
    gen, guide, overflow = await mgr.stage_messages(session_key, max_messages=10)
    assert len(guide) == 1

    # 会话被外部重置（例如用户输入 /clear 或 WebUI 清空）
    new_gen = await mgr.reset_session(session_key)
    assert new_gen != gen
    assert mgr.get_buffered_count(session_key) == 0

    # 之前尚未完成的请求尝试 rollback
    await mgr.rollback_stage(session_key, gen)

    # 不应该回流到已清空的会话中！
    assert mgr.get_buffered_count(session_key) == 0


def test_build_guidance_prompt_format():
    """测试引导提示词渲染格式是否严格符合规范要求"""
    mgr = InflightManager()
    messages = [
        InflightMessage(
            id="1",
            session_key="k",
            sender_name="张三",
            sender_id="u1",
            time_str="19:30",
            text="不对，我说的是北京",
            images=[],
            message_id="m1",
        ),
        InflightMessage(
            id="2",
            session_key="k",
            sender_name="李四",
            sender_id="u2",
            time_str="19:31",
            text="我觉得上海也可以",
            images=["http://example.com/test.png"],
            message_id="m2",
        ),
    ]

    prompt = mgr.build_guidance_prompt(messages)

    assert "## 上一轮 AI 请求期间收到的消息" in prompt
    assert "以下消息在上一轮 AI 请求执行期间到达，尚未被单独回复。" in prompt
    assert "请结合这些消息与当前用户输入，自然理解对话上下文，" in prompt
    assert "注意消息的时间顺序和发送者，不要逐条补发回复。" in prompt
    assert "- [19:30][张三]：不对，我说的是北京" in prompt
    assert "- [19:31][李四]：[图片] 我觉得上海也可以" in prompt


@pytest.mark.asyncio
async def test_concurrent_acquire_stress():
    """并发压力测试：验证只有一个协程能够成功获取执行权"""
    mgr = InflightManager()
    session_key = "stress_session"

    results = []

    async def worker():
        acquired = await mgr.acquire_session(session_key)
        results.append(acquired)

    # 50 个协程并发抢占
    await asyncio.gather(*(worker() for _ in range(50)))

    # 必须且仅有 1 个 True
    assert results.count(True) == 1
    assert results.count(False) == 49

    await mgr.release_session(session_key)


@pytest.mark.asyncio
async def test_try_handle_inflight():
    """测试 try_handle_inflight 原子性检测与缓冲"""
    mgr = InflightManager()
    session_key = "session_x"

    msg = InflightMessage(
        id="1",
        session_key=session_key,
        sender_name="张三",
        sender_id="u1",
        time_str="19:30",
        text="并发消息",
        images=[],
    )

    # 未处于 in-flight 状态时，应当返回 False，且不缓冲
    assert await mgr.try_handle_inflight(session_key, msg) is False
    assert mgr.get_buffered_count(session_key) == 0

    # 获取执行权，处于 in-flight
    assert await mgr.acquire_session(session_key) is True

    # 其他协程处于 in-flight 时，应当返回 True，并成功缓冲消息
    handled_by_other = False

    async def try_handle_other():
        nonlocal handled_by_other
        handled_by_other = await mgr.try_handle_inflight(session_key, msg)

    await asyncio.create_task(try_handle_other())
    assert handled_by_other is True
    assert mgr.get_buffered_count(session_key) == 1

    await mgr.release_session(session_key)
    assert not mgr.is_inflight(session_key)


@pytest.mark.asyncio
async def test_reentrant_acquire_and_release():
    """测试同一个 task 内的重入 acquire 和 release"""
    mgr = InflightManager()
    session_key = "session_reentrant"

    assert await mgr.acquire_session(session_key) is True
    # 同一任务重入
    assert await mgr.acquire_session(session_key) is True
    assert mgr.is_inflight(session_key) is True

    # 释放一次，仍处于 in-flight
    await mgr.release_session(session_key)
    assert mgr.is_inflight(session_key) is True

    # 释放第二次，彻底退出 in-flight
    await mgr.release_session(session_key)
    assert mgr.is_inflight(session_key) is False


@pytest.mark.asyncio
async def test_stage_empty():
    """测试缓冲为空时 stage 返回空列表"""
    mgr = InflightManager()
    session_key = "session_empty"

    gen, guide, overflow = await mgr.stage_messages(session_key)
    assert guide == []
    assert overflow == []


def _make_msg(mid: str, text: str) -> InflightMessage:
    """构造测试用 InflightMessage"""
    return InflightMessage(
        id=mid,
        session_key="session_drain",
        sender_name="张三",
        sender_id="user_123",
        time_str="19:30",
        text=text,
        message_id=mid,
    )


@pytest.mark.asyncio
async def test_drain_buffer_returns_all_in_order_and_clears():
    """测试 drain_buffer 按到达顺序取回全部消息并清空缓冲"""
    mgr = InflightManager()
    key = "session_drain"

    await mgr.add_inflight_message(key, _make_msg("m1", "第一条"))
    await mgr.add_inflight_message(key, _make_msg("m2", "第二条"))
    await mgr.add_inflight_message(key, _make_msg("m3", "第三条"))

    drained = await mgr.drain_buffer(key)
    assert [m.id for m in drained] == ["m1", "m2", "m3"]
    assert [m.text for m in drained] == ["第一条", "第二条", "第三条"]
    # 缓冲已被清空，二次 drain 为空
    assert mgr.get_buffered_count(key) == 0
    assert await mgr.drain_buffer(key) == []


@pytest.mark.asyncio
async def test_drain_buffer_isolated_between_sessions():
    """测试 drain_buffer 会话隔离：drain 一个会话不影响另一个"""
    mgr = InflightManager()
    await mgr.add_inflight_message("session_drain", _make_msg("m1", "A"))
    await mgr.add_inflight_message("other_session", _make_msg("m2", "B"))

    drained = await mgr.drain_buffer("session_drain")
    assert [m.id for m in drained] == ["m1"]
    assert mgr.get_buffered_count("other_session") == 1


@pytest.mark.asyncio
async def test_drain_buffer_after_stage_commit_leftovers():
    """测试 stage 消费引导后，新到达消息仍可被 drain 取回（自动追答路径）"""
    mgr = InflightManager()
    key = "session_drain"

    # 模拟回合开始时 stage 走了部分消息
    await mgr.add_inflight_message(key, _make_msg("m1", "引导消息"))
    gen, guide, _ = await mgr.stage_messages(key)
    assert len(guide) == 1

    # 回合执行期间又到达两条新消息
    await mgr.add_inflight_message(key, _make_msg("m2", "追答消息1"))
    await mgr.add_inflight_message(key, _make_msg("m3", "追答消息2"))

    # 回合提交（stage 中的消息已作为引导消费），随后 drain 新消息
    await mgr.commit_stage(key, gen)
    drained = await mgr.drain_buffer(key)
    assert [m.id for m in drained] == ["m2", "m3"]
    assert mgr.get_buffered_count(key) == 0
