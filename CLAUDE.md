# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

AstrBot 插件（AstrBot >= 4.27.0，Python 3.10+），通过高优先级消息劫持完全替代 AstrBot 自带聊天功能，独立实现上下文管理、记忆系统、人格、Tool Calls、主动回复、消息抖动、画图与 WebUI 管理面板。本仓库位于 AstrBot 的 `data/plugins/astrbot_plugin_chat_engine/` 插件目录内——父目录即 AstrBot 本体源码，可直接查阅框架 API。

## 常用命令

```bash
# Lint / 格式化（Ruff，日志写入 logs/）
python run_ruff.py                    # 完整模式：检查 → 修复 → 格式化
python run_ruff.py --check            # 只检查不修改
python run_ruff.py --check context/   # 检查指定路径

# 测试（需 pytest + pytest-asyncio；pytest.ini 已设 asyncio_mode=auto）
python -m pytest                                        # 全部测试
python -m pytest tests/test_image_quota_repo.py          # 单文件
python -m pytest tests/test_image_quota_repo.py::test名  # 单测试
```

测试设计约束：repo/model 层只依赖 sqlalchemy 与 utils，经 [tests/conftest.py](tests/conftest.py) 把 plugins 目录加入 sys.path，可在脱离 AstrBot 运行环境时独立执行。因此导入 `astrbot.*` 的代码目前不可单测——新增测试请放在 repo 层或保持此约束。

无构建步骤；部署即把本目录放入 AstrBot `data/plugins/` 并在 Dashboard 启用。发版流程见下文「跨文件同步约定」。

## 架构

### 消息劫持管道

入口为 [main.py](main.py) `ChatEnginePlugin.handle_all_messages`（`@filter.event_message_type(ALL, priority=9999)` 拦截所有消息），管道顺序：

1. `event.should_call_llm(True)` 无条件抑制 AstrBot 默认 LLM
2. 空文本纯图片消息 → 被动记录（`observed` 角色）后返回
3. 会话管理命令（`/new` `/list` `/switch N` `/clear` `/stats`，群聊管理员限定）
4. 命令透传：检查 `activated_handlers` 中的 `CommandFilter`/`RegexFilter`/`CommandGroupFilter`（跳过 chat_engine 自身 handler），有则放手让其他插件/框架处理
5. `should_respond` 判定（群聊需 @）；不响应时并入抖动缓冲或被动记录
6. 活跃回合路由：会话存在活跃回合（`ActiveTurnController`，[context/active_turn.py](context/active_turn.py)，参考 Hermes Agent）时，新触发消息实时进入当前逻辑回合——模型请求中 **Redirect**（取消在飞请求、丢弃过时响应/取消-响应交叉、同回合重建输入重试，不触发 Provider Fallback、不消耗工具轮次）；工具批次中 **Steer**（批次安全边界作为真实 `role:user` 消息注入并携带运行期标注 STEER_NOTE——按新消息更新工作而非汇总汇报：保留未被取消的原有需求、区分已执行/已交付、按主题自然分段交付、不补报不报幕；工具结果保留不重放）；收尾阶段（FINALIZING/循环外 SENDING）退回 inflight 缓冲走兜底（自动追答/下次注入）。Redirect/Steer 消息经 `intermediate_msgs` 持久化，持久化失败退回缓冲（仅消费一次）
7. `_execute_llm_turn`：System Prompt 组装（时间注入 / 群信息缓存 / 人格 / 记忆 / 工具指引 / 命令指引）→ `_llm_call_with_tools` → 文本清洗 → 分段发送（发送期间收到新消息则停止未发旧片段）

### 双消息发送路径

正常消息流与抖动消息流（`_process_debounced_messages`）各自完整复刻 LLM 调用、Provider fallback、工具投递与分段发送。工具产生的图片/链式结果经 `_ToolCallContext.pending_sends` 投递，两条路径都要消费它——**改动发送或工具投递逻辑时须同时检查两条路径**。

### 并发隔离

- AstrBot `EventBus.dispatch` 用 `asyncio.create_task` 并发处理事件，因此 `_llm_call_with_tools` 的中间状态放在 `contextvars.ContextVar`（`_tool_call_ctx`）而非实例变量
- 同一会话串行化：`context_mgr.get_session_lock(session_key)`（会话级 `asyncio.Lock`）
- 记忆自动总结、`/new` 归档（「锁内快照 → 锁外 LLM 标题生成 → 锁内归档」）均有各自的会话级锁策略

### 子系统布局

[main.py](main.py)（约 2900 行）是编排中心，各子系统为独立包，由 main.py 构造并注入依赖：

| 包 | 职责 |
|---|---|
| [db/](db/) | 独立 SQLAlchemy 层（**独立 MetaData**，避免与 AstrBot 全局元数据冲突）；sqlite/mysql 双支持；图片按 sha256 去重存储，上下文以 `image_ref` 引用 |
| [context/](context/) | 会话 key、用户标识格式、双模式压缩（turn_limit / token）、Token 估算、会话锁、被动记录 |
| [memory/](memory/) | 短期/长期记忆、Embedding 向量检索 + 可选 Rerank、自动总结、LLM 记忆工具 |
| [debounce/](debounce/) | 消息抖动缓冲与合并 |
| [proactive/](proactive/) | 超时 / N 轮 / AI 判断主动回复、定时回复 |
| [image_gen/](image_gen/) | OpenAI 兼容文生图/改图 HTTP 客户端 |
| [persona/](persona/)、[tools/](tools/) | 人格 CRUD；工具扫描与启用管理、命令分发（LLM 自然语言执行其他插件命令） |
| [web/](web/) + [pages/](pages/) | WebUI 采用 AstrBot 插件 Pages 架构：[web/api.py](web/api.py) 用 `register_web_api` 注册路由（经 Dashboard 同源转发，认证继承 Dashboard JWT），前端静态文件在 [pages/chat-engine/](pages/chat-engine/)。无独立端口/服务器 |

### LLM 调用关键约定

- Provider fallback：主 Provider 失败且尚未发出内容时，逐个尝试 AstrBot 全局 `fallback_chat_models`；`_sent_any` 后不再切换。模态过滤、Token 截断、`[msg:ID]` 注入对每个候选 Provider 独立重算
- 历史上下文中图片剥离为 `[Image]` 占位符，仅当前消息保留图片；`view_image` 工具按 message_id 回看；纯文本 Provider 下 `_mark_images_for_text_only_provider` 把图片编码为文本标记
- 全局时间统一上海时区 UTC+8（[utils](utils/__init__.py) 的 `shanghai_now_iso` / `format_current_time`）

## 跨文件同步约定（改动时容易漏）

- **新增配置项**：须同步四处——[_conf_schema.json](_conf_schema.json)（schema）、[web/api.py](web/api.py) 的 `_CONFIG_KEYS`（WebUI GET/PUT 白名单）、[pages/chat-engine/app.js](pages/chat-engine/app.js) 的 `CONFIG_FIELDS`（WebUI 设置表单，含分组/类型/hint，配置需重启生效时加 `restart: true`），README 配置表也应更新
- **新增模块目录**：须同步 [.github/workflows/release.yml](.github/workflows/release.yml) 的 zip 打包清单，否则发布包缺目录导致 import 失败
- **发版**：更新 [metadata.yaml](metadata.yaml) `version` + [CHANGELOG.md](CHANGELOG.md) 顶部条目 → push main 自动构建 zip 并创建 GitHub Release（changelog 最新条目作为 release body）
- **会话命令**：`_SESSION_CMD_*` 正则需同时匹配有/无唤醒前缀的情况（框架会剥离唤醒前缀）

## Git 提交纪律

- **永远都不要自主提交**（git commit / git push 等），**也不允许子代理提交**。所有提交必须在用户明确指示后才执行。

## 代码规范（项目既定，源自 .sakura/）

- 配置读取统一走 [utils/config.py](utils/config.py) 的 `cfg_int` / `cfg_float` / `cfg_bool`，禁止在其他模块复制类型转换逻辑；枚举型配置读取后须白名单校验，校验应在持久化时完成而非仅读取时
- 异步：强制 `async with` 管理锁；禁止 `lock.locked()` 预判状态（`release()` 仅唤醒等待者，不调度）；禁止 `asyncio.get_event_loop()`（用 `get_running_loop()`）
- 热路径 async 方法禁止同步阻塞 I/O（`open()`、`config.save()` 等），须用 `aiofiles` 或 `run_in_executor`
- 前端：动态 HTML 属性值统一 `escapeHtml`；禁止内联 `onclick` 拼接 JS 字符串（用 data 属性 + `addEventListener`，见 CHANGELOG 1.3.8 的 XSS 修复）
- 用户可配置的正则（分段、句尾清理等）须防 ReDoS，并按空值 / 负值 / 极大值 / 特殊字符做压力测试
- 消息管道串联点（记录 → 压缩 → 保存 → 截断 → 分段发送）是高风险区，改动一个阶段须审查相邻阶段
