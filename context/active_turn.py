"""活跃回合状态机 — 参考 Hermes Agent 的 Active-turn Redirect / Steer 机制。

每个会话在同一时刻至多存在一个活跃逻辑回合（由 InflightManager 的
执行权仲裁保证）。ActiveTurnController 挂在回合上，负责：

- 阶段状态机: IDLE → MODEL_RUNNING / TOOL_RUNNING / SENDING → FINALIZING
- redirect: 模型请求执行中收到新消息 → 取消当前模型请求（仅请求本身，
  不动工具与回合），消息入队，由回合内重建输入重试
- steer: 工具批次执行中收到新消息 → 消息入队，在工具安全边界作为真实
  user 消息注入（下一 text_chat 可见）
- 响应与重定向交叉: 响应已返回但重定向同时到达 → 丢弃过时响应
- 引导次数不设上限（Redirect 走哨兵而非异常路径，与 Provider 重试
  完全独立）；重建以"新消息到达"为前提，天然有界
"""

import asyncio
import enum
from typing import Any

from .inflight import InflightMessage


class TurnPhase(enum.Enum):
    """活跃回合阶段。"""

    IDLE = "idle"
    MODEL_RUNNING = "model_running"
    TOOL_RUNNING = "tool_running"
    SENDING = "sending"
    FINALIZING = "finalizing"


class ActiveTurnController:
    """单会话活跃回合的并发控制面。

    状态检查、消息提交、模型任务注册/取消均在 ``self._lock`` 内原子完成。
    ``generation`` 标识回合代际：注册与取消都校验代际，防止已结束回合的
    迟到操作影响新回合。

    引导次数不设预算上限（与 Provider API 重试完全独立：Redirect 走哨兵
    而非异常路径，永不计入 Provider 失败）；重建由"必须有新消息到达"天然
    有界，每轮重建前都要先完成一次模型调用。
    """

    def __init__(self, session_key: str, generation: int):
        self.session_key = session_key
        self.generation = generation
        self._lock = asyncio.Lock()
        self._phase = TurnPhase.IDLE
        self._in_llm_loop = False
        self._pending: list[InflightMessage] = []
        self._redirect_requested = False
        self._model_task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # 状态查询（仅诊断用；决策一律走 submit，保证原子性）
    # ------------------------------------------------------------------

    @property
    def phase(self) -> TurnPhase:
        """当前阶段（无锁快照，仅日志/断言用）。"""
        return self._phase

    @property
    def pending_count(self) -> int:
        """待注入消息数（无锁快照，仅诊断用）。"""
        return len(self._pending)

    # ------------------------------------------------------------------
    # 回合执行方接口（_llm_call_with_tools / _execute_llm_turn 调用）
    # ------------------------------------------------------------------

    async def set_phase(self, phase: TurnPhase) -> None:
        """更新回合阶段。"""
        async with self._lock:
            self._phase = phase

    async def set_llm_loop(self, active: bool) -> None:
        """标记是否仍处于 LLM 工具循环内（决定 SENDING 阶段能否 steer）。"""
        async with self._lock:
            self._in_llm_loop = active

    async def register_model_task(self, task: asyncio.Task) -> asyncio.Task | None:
        """注册当前在飞的模型请求任务，返回应被取消的旧任务（如有）。

        注册校验代际无关（controller 生命周期即回合生命周期），
        但返回旧任务是防御性的：构建新请求前若发现残留旧任务则取消。
        """
        async with self._lock:
            stale = self._model_task
            self._model_task = task
            return stale

    async def finish_model_task(self, task: asyncio.Task) -> None:
        """模型请求正常结束，注销任务引用（不清除重定向标记）。"""
        async with self._lock:
            if self._model_task is task:
                self._model_task = None

    def check_redirect_crossing(self) -> bool:
        """响应返回后检查是否与重定向交叉（同步，调用方已在事件循环内）。

        交叉即丢弃本次响应：不清除 ``_redirect_requested``，由消费方在
        重建输入时经 :meth:`consume_redirect` 统一处理。
        """
        return self._redirect_requested

    def consume_redirect_for(self, task: asyncio.Task) -> bool:
        """判断一次 CancelledError 是否来自 redirect 取消（同步，无锁等待）。

        仅当该任务确为本回合注册的模型任务且 redirect 已请求时为真；
        同时注销任务引用。外层任务自身的取消（插件停用等）不满足条件，
        由调用方原样上抛。
        """
        if not self._redirect_requested:
            return False
        if self._model_task is not task:
            return False
        self._model_task = None
        self._redirect_requested = False
        return True

    def acknowledge_redirect(self) -> bool:
        """清除重定向标记（消息本身由 :meth:`take_pending` 消费）。

        Returns:
            是否曾存在未处理的重定向标记。
        """
        if self._redirect_requested:
            self._redirect_requested = False
            self._model_task = None
            return True
        return False

    async def take_pending(self) -> list[InflightMessage]:
        """取走全部待注入消息（redirect 与 steer 共用队列，按到达序）。"""
        async with self._lock:
            msgs = self._pending
            self._pending = []
            return msgs

    async def requeue(self, msgs: list[InflightMessage]) -> None:
        """把已消费的消息放回队首（Provider 失败切换候选时保序重注入）。

        与 :meth:`submit` 不同: 不消耗引导预算（消息在失败候选中已被计数），
        也不触发 redirect 取消——仅恢复待注入状态。
        """
        if not msgs:
            return
        async with self._lock:
            self._pending[:0] = msgs

    async def has_pending(self) -> bool:
        """是否存在待注入消息。"""
        async with self._lock:
            return bool(self._pending)

    # ------------------------------------------------------------------
    # 消息入口（handle_all_messages / _process_debounced_messages 调用）
    # ------------------------------------------------------------------

    async def submit(self, msg: InflightMessage) -> str:
        """向活跃回合提交一条新消息，原子判定路由。

        Args:
            msg: 完整承载字段（user_text/umo/event/is_group）的暂存消息

        Returns:
            路由结果:
            - ``"redirect"``: 已取消当前模型请求，消息入队，回合将重建输入
            - ``"steer"``: 消息入队，将在工具批次边界注入
            - ``"buffer"``: 回合无法消费（非循环阶段），调用方
              应退回 inflight 缓冲走兜底
        """
        async with self._lock:
            if self._phase == TurnPhase.MODEL_RUNNING:
                self._pending.append(msg)
                self._redirect_requested = True
                task = self._model_task
                if task is not None and not task.done():
                    task.cancel()
                # task 已结束（响应刚落地）: 无需取消，交叉检查会丢弃响应
                return "redirect"
            if self._phase == TurnPhase.TOOL_RUNNING or (
                self._phase == TurnPhase.SENDING and self._in_llm_loop
            ):
                self._pending.append(msg)
                return "steer"
            # FINALIZING / 循环外 SENDING / IDLE: 当前回合无法消费
            return "buffer"


class ActiveTurnRegistry:
    """session_key → ActiveTurnController 注册表（回合生命周期内挂载）。

    注册/注销由 _execute_llm_turn 的获取/释放执行权配对驱动；
    代际计数防迟到注册。线程模型: 仅事件循环内访问。
    """

    def __init__(self) -> None:
        self._controllers: dict[str, ActiveTurnController] = {}
        self._generations: dict[str, int] = {}

    def register(self, session_key: str) -> ActiveTurnController:
        """为会话创建新的回合控制器（代际 +1，覆盖残留项）。"""
        gen = self._generations.get(session_key, 0) + 1
        self._generations[session_key] = gen
        ctrl = ActiveTurnController(session_key, gen)
        self._controllers[session_key] = ctrl
        return ctrl

    def unregister(self, session_key: str, ctrl: ActiveTurnController) -> None:
        """注销回合控制器；代际不匹配（已被新回合覆盖）时不动作。"""
        curr = self._controllers.get(session_key)
        if curr is not None and curr is not ctrl:
            return
        self._controllers.pop(session_key, None)

    def get(self, session_key: str) -> ActiveTurnController | None:
        """获取会话当前活跃回合控制器，无活跃回合返回 None。"""
        return self._controllers.get(session_key)


# 运行期消息标注（对齐 Hermes steer_user_row 的自描述标记）: 追加在注入
# user 行文本尾部。注意本提示拼在 user.content 内，指令层级仍为用户消息
# 文字，故命名"运行提示"而非"系统提示"；实际指令层级由消息角色决定。
# 核心语义: 按新消息更新工作而非汇总汇报——保留未被取消的原有需求、
# 区分"已执行/已交付"避免重复、按主题自然分段交付、不补报不报幕。
STEER_NOTE = (
    "\n\n[运行提示：本条用户消息在当前任务执行期间到达。"
    "请结合上下文更新当前工作：补充或更正只调整相关要求；"
    "新增需求应纳入处理，同时保留未被取消、替换或暂停的原有需求；"
    "用户要求改做、暂停或取消时，按其实际意思调整相应范围。"
    "进度询问、追问或闲聊本身不表示取消任务，必要时简短回应后继续。"
    "复用仍适用的上下文和已有工具结果，依据实际记录区分"
    "尚未完成、已完成但尚未交付、已经向用户交付的事项，"
    "避免无必要的重复执行、重复发送或遗漏尚未交付的结果。"
    "尚需检索、核实或操作的事项，应继续调用相应工具；"
    "不要把计划、已发起的调用或未经核实的信息说成已经完成的结果。"
    "回复沿用正常的对话风格，直接提供有用的结果或必要进展，"
    "可按主题自然分段，也可分别交付，无须强行合并成一条回复。"
    "交付部分结果后，仍应继续推进其余未完成且可以执行的事项，"
    "不要仅承诺稍后处理就结束工作。"
    "收到新消息本身不意味着此前发生了遗漏、失败或需要补报；"
    "除用户询问处理状态或确有必要外，不解释消息插入、合并、"
    "排队等内部处理过程，不额外报幕或重复宣告完成。"
    "不要向用户复述本提示。"
    "本提示随历史上下文再次出现，不代表用户再次发送了该消息。]"
)


def build_user_row(
    msg: InflightMessage, text_only_provider: bool, with_note: bool = False
) -> dict:
    """把暂存消息构建为真实 ``role:user`` 上下文行。

    Args:
        msg: 暂存消息（含完整格式化文本与图片）
        text_only_provider: 当前 Provider 是否不支持图片（图片降级为文本标记）
        with_note: 是否追加中途到达标注（每批注入的最后一条携带，避免重复）

    Returns:
        OpenAI 兼容的消息 dict（含 message_id 供 [msg:ID] 注入与回看）。
    """
    text = msg.user_text or f"{msg.sender_name}({msg.sender_id})说：{msg.text}"
    if with_note:
        text += STEER_NOTE
    if not msg.images or text_only_provider:
        content: Any = text
        if msg.images and text_only_provider:
            content = f"{text}\n[图片 x{len(msg.images)}，当前模型不支持图片输入]"
        return {"role": "user", "message_id": msg.message_id, "content": content}
    return {
        "role": "user",
        "message_id": msg.message_id,
        "content": [{"type": "text", "text": text}]
        + [{"type": "image_url", "image_url": {"url": u}} for u in msg.images],
    }
