"""Token 压缩模式对大量被动 (observed) 消息的处理测试（Issue #11）。

覆盖验收场景:
1. 100+ 条 observed 消息 + 少量真实 user 轮次，超阈值后压缩实际减少上下文
2. 几乎全是被动消息的会话不因轮次不足而无限跳过压缩
3. 摘要成功时旧被动消息由摘要保留，最近消息完整
4. 摘要失败时按确定性截断降级（保留预算内最近内容，不静默全丢）
5. 摘要本身过大时按预算截短后仍能满足阈值
6. 带图片的被动消息不因非文本内容异常或错误估算 Token
7. 压缩结果 Token 收敛到阈值内，可重复压缩（链式摘要）

本文件不依赖 AstrBot 运行环境（compressor 的 logger 导入已做降级处理）。
"""

from types import SimpleNamespace

from astrbot_plugin_chat_engine.context.compressor import (
    BaseCompressor,
    ContextCompressorFactory,
    TokenBasedCompressor,
    TurnLimitCompressor,
)
from astrbot_plugin_chat_engine.context.token_counter import TokenEstimator

MAX_TOKENS = 10000
THRESHOLD = int(MAX_TOKENS * 0.8)  # 8000


def _obs(text: str, with_image: bool = False) -> dict:
    content = [{"type": "text", "text": text}]
    if with_image:
        content.append(
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
        )
    return {"role": "observed", "content": content}


def _obs_fill(i: int) -> dict:
    """一条约 187 token 的被动消息（正文 600 ASCII 字符 ≈ 180 token + 开销 4）"""
    return {"role": "observed", "content": f"OLD_FILL_{i:03d} " + "x" * 600}


def _user(text: str) -> dict:
    return {"role": "user", "content": text}


def _asst(text: str) -> dict:
    return {"role": "assistant", "content": text}


def _group_scenario() -> list[dict]:
    """大量被动堆积 + 3 个真实问答轮的典型群聊上下文（共 76 条，超阈值）"""
    msgs = [_obs_fill(i) for i in range(60)]
    msgs += [_user("u1"), _asst("a1")]
    msgs += [_obs("hi1"), _obs("hi2")]
    msgs += [_user("u2"), _asst("a2")]
    msgs += [_obs("hi3"), _obs("hi4")]
    msgs += [_user("u3"), _asst("a3")]
    msgs += [_obs(f"tail{i}") for i in range(5)]
    return msgs


class StubProvider:
    """可编程的 Provider 桩：记录调用并返回预设摘要"""

    def __init__(self, reply: str | None = "这是旧对话的摘要"):
        self.reply = reply
        self.calls = 0
        self.prompts: list[str] = []

    async def text_chat(self, prompt=None, system_prompt=None, **_kw):
        self.calls += 1
        self.prompts.append(prompt or "")
        return SimpleNamespace(completion_text=self.reply)


def _compressor(stub: StubProvider) -> TokenBasedCompressor:
    return TokenBasedCompressor(
        token_threshold_ratio=0.8,
        keep_recent_turns=5,
        provider_getter=lambda: stub,
    )


# --- split_into_turns ---


def test_split_turns_observed_runs_form_independent_turns():
    msgs = [
        _obs("o1"),
        _obs("o2"),
        _user("u1"),
        _asst("a1"),
        _obs("o3"),
        _obs("o4"),
    ]
    turns = BaseCompressor.split_into_turns(msgs)
    assert [len(t) for t in turns] == [2, 2, 2]
    assert turns[1] == [_user("u1"), _asst("a1")]


def test_split_turns_interleaved_observed_keeps_qa_pair():
    # 回合执行期间被记录的 observed 不拆散问答对
    msgs = [_user("u1"), _obs("mid"), _asst("a1")]
    turns = BaseCompressor.split_into_turns(msgs)
    assert len(turns) == 1 and len(turns[0]) == 3


def test_split_turns_proactive_reply_then_passive_run():
    msgs = [_asst("主动发言"), _obs("o1"), _obs("o2")]
    turns = BaseCompressor.split_into_turns(msgs)
    assert [len(t) for t in turns] == [1, 2]


def test_split_turns_tool_messages_stay_in_turn():
    msgs = [_user("u1"), _asst("a1"), {"role": "tool", "content": "r"}]
    turns = BaseCompressor.split_into_turns(msgs)
    assert len(turns) == 1


def test_split_turns_all_passive_single_run():
    msgs = [_obs(f"o{i}") for i in range(10)]
    turns = BaseCompressor.split_into_turns(msgs)
    assert len(turns) == 1


# --- TokenBasedCompressor 基础守卫 ---


async def test_compress_skips_below_threshold():
    stub = StubProvider()
    msgs = _group_scenario()[:10]
    result = await _compressor(stub).compress(msgs, MAX_TOKENS)
    assert result == msgs
    assert stub.calls == 0


async def test_compress_skips_unknown_limit():
    stub = StubProvider()
    msgs = _group_scenario()
    result = await _compressor(stub).compress(msgs, 0)
    assert result == msgs
    assert stub.calls == 0


# --- 验收 1/3: 被动堆积 + 少量真实轮次，摘要生效 ---


async def test_compress_summarizes_old_passive_turns():
    stub = StubProvider()
    msgs = _group_scenario()
    counter = TokenEstimator()
    assert counter.count_messages_tokens(msgs) > THRESHOLD  # 前置: 确实超阈值

    result = await _compressor(stub).compress(msgs, MAX_TOKENS)

    assert stub.calls == 1
    assert counter.count_messages_tokens(result) <= THRESHOLD
    # 摘要块在最前
    assert result[0]["role"] == "user"
    assert result[0]["content"].startswith("[之前的对话摘要]")
    assert result[1]["role"] == "assistant"
    # 最近消息完整保留: 最后一轮被动消息与最近问答都在
    assert result[-1] == _obs("tail4")
    contents = [str(m["content"]) for m in result[2:]]
    assert any("u3" in c for c in contents)
    assert any("a3" in c for c in contents)
    # 旧被动消息正文退出上下文（其事实由摘要承载）
    assert all("OLD_FILL" not in c for c in contents)


# --- 验收 2: 几乎全是被动消息的会话不被跳过 ---


async def test_compress_all_passive_single_run_truncates():
    stub = StubProvider()
    msgs = [_obs_fill(i) for i in range(100)]  # 单个被动段，无真实轮次
    counter = TokenEstimator()

    result = await _compressor(stub).compress(msgs, MAX_TOKENS)

    # 无旧轮次可摘要（摘要路径跳过），走确定性截断
    assert stub.calls == 0
    assert counter.count_messages_tokens(result) <= THRESHOLD
    assert 0 < len(result) < len(msgs)
    assert result[-1] == msgs[-1]  # 至少保留最新消息


# --- 验收 4: 摘要失败确定性降级 ---


async def test_compress_summary_failure_falls_back_to_deterministic_truncation():
    stub = StubProvider(reply=None)  # 摘要返回空 → 失败
    msgs = _group_scenario()
    counter = TokenEstimator()

    result = await _compressor(stub).compress(msgs, MAX_TOKENS)

    assert stub.calls == 1
    assert counter.count_messages_tokens(result) <= THRESHOLD
    assert 0 < len(result) < len(msgs)
    # 保留预算内尽量多的最近轮次（u1 轮放得下也应保留），最旧被动段被裁
    contents = [str(m["content"]) for m in result]
    assert any("u1" in c for c in contents)
    assert all("OLD_FILL" not in c for c in contents)
    assert result[-1] == _obs("tail4")


async def test_compress_summary_error_falls_back_to_truncation():
    class BoomProvider(StubProvider):
        async def text_chat(self, prompt=None, system_prompt=None, **_kw):
            self.calls += 1
            raise RuntimeError("provider down")

    stub = BoomProvider()
    msgs = _group_scenario()
    counter = TokenEstimator()

    result = await _compressor(stub).compress(msgs, MAX_TOKENS)

    assert stub.calls == 1
    assert counter.count_messages_tokens(result) <= THRESHOLD
    assert 0 < len(result) < len(msgs)


# --- 验收 5: 摘要本身过大时截短复检 ---


async def test_compress_oversized_summary_clipped_to_budget():
    stub = StubProvider(
        reply="\n".join(f"summary line {i} " + "y" * 100 for i in range(300))
    )
    msgs = _group_scenario()
    counter = TokenEstimator()

    result = await _compressor(stub).compress(msgs, MAX_TOKENS)

    assert stub.calls == 1
    assert result[0]["content"].startswith("[之前的对话摘要]")
    assert counter.count_messages_tokens(result) <= THRESHOLD
    # 截短后仍是摘要路径而非全量截断
    assert len(result) < len(msgs)


async def test_compress_summary_still_oversized_after_clip_falls_back():
    # 无换行的超长摘要无法按行截短 → 降级为确定性截断
    stub = StubProvider(reply="z" * 60000)
    msgs = _group_scenario()
    counter = TokenEstimator()

    result = await _compressor(stub).compress(msgs, MAX_TOKENS)

    assert counter.count_messages_tokens(result) <= THRESHOLD
    assert 0 < len(result) < len(msgs)


# --- 验收 6: 图片内容不异常、不错误估算 ---


def test_token_estimator_ignores_image_parts():
    counter = TokenEstimator()
    plain = counter.count_messages_tokens([_obs("hi")])
    with_img = counter.count_messages_tokens([_obs("hi", with_image=True)])
    assert plain == with_img


async def test_compress_with_image_passive_messages():
    stub = StubProvider()
    msgs = [_obs_fill(i) for i in range(60)]
    msgs += [_user("u1"), _asst("a1")]
    msgs += [_obs("看图", with_image=True), _obs("再看", with_image=True)]
    counter = TokenEstimator()

    result = await _compressor(stub).compress(msgs, MAX_TOKENS)

    assert stub.calls == 1
    assert counter.count_messages_tokens(result) <= THRESHOLD
    # 近期带图被动消息完整保留
    assert _obs("看图", with_image=True) in result
    assert _obs("再看", with_image=True) in result


# --- 验收 7: 可重复压缩（链式摘要收敛） ---


async def test_repeated_compress_chains_summary():
    stub1 = StubProvider()
    compressor = _compressor(stub1)
    first = await compressor.compress(_group_scenario(), MAX_TOKENS)

    # 继续追加被动消息再次超阈值，重压缩应吸收旧摘要（链式）
    stub2 = StubProvider()
    compressor.provider_getter = lambda: stub2
    follow_up: list[dict] = []
    for i in range(45):
        follow_up += [_obs_fill(100 + i), _user(f"n{i}"), _asst(f"na{i}")]

    second = await compressor.compress(first + follow_up, MAX_TOKENS)

    assert stub2.calls == 1
    # 旧摘要被喂给新摘要（链式吸收）
    assert "[之前的对话摘要]" in stub2.prompts[0]
    assert TokenEstimator().count_messages_tokens(second) <= THRESHOLD
    assert second[0]["content"].startswith("[之前的对话摘要]")


async def test_compress_headroom_guard_skips_summary_when_recent_too_big():
    # 近期轮次自身占满预算时跳过摘要（避免每条新消息都触发摘要调用）
    stub = StubProvider()
    msgs = [_user("u1"), _asst("a1")] + [_obs_fill(i) for i in range(60)]
    counter = TokenEstimator()

    result = await _compressor(stub).compress(msgs, MAX_TOKENS)

    # 唯一被动段是最新轮（受保护），近期占比超限 → 确定性截断而非摘要
    assert stub.calls == 0
    assert counter.count_messages_tokens(result) <= THRESHOLD
    assert 0 < len(result) <= len(msgs)


# --- 轮数限制模式同样受被动段独立成轮保护 ---


async def test_turn_limit_compressor_counts_passive_runs():
    compressor = TurnLimitCompressor(max_turns=3)
    msgs = [_user("u1"), _asst("a1")]
    # 真实轮次之间穿插被动段（被动消息跟在真实消息后独立成轮）
    for i in range(5):
        msgs += [_obs(f"o{i}"), _user(f"u{i + 2}")]
    result = await compressor.compress(msgs, 0)
    # 11 轮 > 3 轮: 只保留最近 3 轮（被动段计入轮数，不再无限堆积）
    assert result == [_user("u5"), _obs("o4"), _user("u6")]


# --- 工厂 ---


def test_factory_creates_token_compressor():
    compressor = ContextCompressorFactory.create(
        {"compression_mode": "token", "token_threshold_ratio": 0.8}, None
    )
    assert isinstance(compressor, TokenBasedCompressor)


def test_factory_creates_turn_limit_compressor_default():
    compressor = ContextCompressorFactory.create({}, None)
    assert isinstance(compressor, TurnLimitCompressor)
