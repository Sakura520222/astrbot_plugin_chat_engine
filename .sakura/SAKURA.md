# 项目概述：astrbot_plugin_chat_engine

## 1. 项目简介
AstrBot 聊天增强插件，完全替代内置聊天功能，提供独立的上下文管理、多用户识别、人格系统、Tool Calls、上下文压缩、记忆系统、主动回复和 WebUI 管理面板。

## 2. 技术栈
- **核心**：Python（主逻辑）、JavaScript/HTML/CSS（WebUI 前端）
- **数据库**：SQLAlchemy ORM、aiosqlite（异步SQLite）、MySQL
- **Web服务**：aiohttp REST API
- **异步架构**：基于 asyncio 的异步 I/O 设计
- **集成目标**：AstrBot v4.25+，支持 QQ OneBot / Telegram

## 3. 项目结构
- `main.py`：插件入口，消息拦截 + LLM 调用编排 + 文本清洗 + 分段发送
- `db/`：独立数据层（models、engine、session_repo、persona_repo、tool_config_repo、image_repo、image_store）
- `context/`：上下文管理核心（manager、compressor、token_counter）
- `persona/`：独立人格管理（CRUD + 活跃人格切换）
- `tools/`：工具扫描（scanner）与启用/禁用管理（manager）
- `memory/`：记忆持久化管理（磁盘存储，sha256 去重）
- `proactive/`：主动回复调度系统（任务调度、注册表管理）
- `web/`：aiohttp REST API 服务端 + 静态前端

## 4. 已识别的架构模式与规范
- **异步资源生命周期规范**：强制 `async with` 管理锁；禁止 `lock.locked()` 预判状态；禁止 `asyncio.get_event_loop()`（用 `get_running_loop()`）；`_session_locks` 须有惰性清理策略
- **配置安全规范**：配置辅助方法禁止跨模块复制，须集中于 `utils/config.py`；枚举配置读取后须白名单校验；配置校验应在**持久化时**完成，而非仅读取时
- **热路径异步纯度**：标记 `async` 的高频方法禁止同步阻塞 I/O（`open()`、`config.save()` 等），须用 `aiofiles` 或 `run_in_executor`
- **前端安全**：动态 HTML 属性值须统一 `escapeHtml`；涉及认证的 Cookie 默认 `httponly=True, samesite="Lax"`
- **正则安全**：用户可配置正则须复杂度静态分析或长度限制；正则循环内禁止重复编译/多次扫描同一文本
- **任务调度规范**：调度任务 ID 须全局唯一（UUID），禁止依赖业务键；高频持久化须采用脏标记+延迟批量写入
- **管道连接点审查**：消息处理流程（记录→压缩→保存→截断→分段发送）的串联点是高风险区

## 5. 关键经验教训
- 增量审查须保持"全局嗅觉"，主动扫描同模式代码，从"点状修复"推向"模式级消除"
- 异步编程中 `release()` 仅唤醒等待者而非调度，基于 `locked()` 的状态判断不可靠
- 对用户配置须进行"恶意/意外输入"压力测试：空值、负值、极大值、特殊字符、ReDoS
- 新功能引入需同步评估：维护成本、数据生命周期、异步纯度、锁粒度影响
- 区分"本次引入的缺陷"与"可选技术债"，增量审查中避免问题通胀
- 技术债务须可见化（技术债务看板），避免多轮审查反复提及却无进展

## 6. 兼容性
AstrBot v4.25+，平台 QQ OneBot / Telegram，数据库 SQLite / MySQL，Python 3.10+

---
累计反思 20 次