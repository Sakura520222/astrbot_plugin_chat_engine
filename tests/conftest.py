"""Pytest 配置 — 把插件父目录加入 sys.path,使 astrbot_plugin_chat_engine 包可被 import。

repo/model 层仅依赖 sqlalchemy + utils,不触发 AstrBot 框架依赖,
可在脱离 AstrBot 运行环境的情况下独立测试。
"""

import sys
from pathlib import Path

# tests/ -> 插件目录 -> plugins/ (astrbot_plugin_chat_engine 的父目录)
_PLUGINS_DIR = Path(__file__).resolve().parent.parent.parent
if str(_PLUGINS_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGINS_DIR))
