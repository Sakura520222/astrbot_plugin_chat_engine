# 项目概述：astrbot_plugin_chat_engine

## 1. 项目简介
AstrBot 聊天增强插件，完全替代内置聊天功能，提供独立的上下文管理、多用户识别、人格系统、Tool Calls、上下文压缩和 WebUI 管理面板。

## 2. 技术栈
- **核心**：Python（主逻辑）、JavaScript/HTML/CSS（WebUI 前端）
- **数据库**：SQLAlchemy ORM、aiosqlite（异步SQLite）、MySQL
- **Web服务**：aiohttp REST API
- **异步架构**：基于 asyncio 的异步 I/O 设计
- **集成目标**：AstrBot v4.25+，支持 QQ OneBot / Telegram

## 3. 项目结构
- `main.py`：插件入口，消息拦截 + LLM 调用编排 + 配置读取
- `db/`：独立数据层（models、engine、session_repo、persona_repo、tool_config_repo）
- `context/`：上下文管理核心（manager、compressor、token_counter）
- `persona/`：独立人格管理（CRUD + 活跃人格切换）
- `tools/`：工具扫描（scanner）与启用/禁用管理（manager）
- `web/`：aiohttp REST API 服务端 + 静态前端

## 4. 已识别的架构模式与规范
- **配置安全规范**：所有配置读取必须通过 `_cfg_int`/`_cfg_float`/`_cfg_bool` 辅助方法，禁止裸类型转换；需定义配置值的类型与语义契约
- **防御性编程**：try/except 中使用变量须在块外预初始化；异常路径本身须比主流程更健壮
- **管道连接点审查**：消息处理流程（记录→压缩→保存→截断→分段发送）的串联点是高风险区，需确保步骤间输出完整传递
- **异步资源管理**：优先使用 `async with` 管理锁；禁止在同步方法中 `release()` 后立即清理锁；`_session_locks` 字典须有生命周期清理策略
- **DRY（意图级）**：重复检测不止于代码文本，更应从"逻辑意图"层面识别（如持久化意图被多处表达）
- **前端安全**：动态生成的 HTML 属性值须统一使用 `escapeHtml`
- **配置与缓存分离**：持久化配置（`_conf_schema.json`）与运行时缓存应使用独立变量，禁止向 config 写入非声明键

## 5. 关键经验教训
- 增量审查须保持"全局嗅觉"，主动扫描同模式代码，从"点状修复"推向"模式级消除"
- 异步编程中 `release()` 仅唤醒等待者而非调度，基于 `locked()` 的状态判断不可靠
- 对用户配置须进行"恶意/意外输入"压力测试：空值、负值、极大值、特殊字符
- 评估新功能需同步考虑其维护成本与数据生命周期

## 6. 兼容性
- AstrBot v4.25+，平台 QQ OneBot / Telegram，数据库 SQLite / MySQL，Python 3.10+

---
累计反思 10 次