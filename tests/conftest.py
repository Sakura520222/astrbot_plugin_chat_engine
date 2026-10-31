"""Pytest 配置 — 把插件父目录加入 sys.path,使 astrbot_plugin_chat_engine 包可被 import。

repo/model 层仅依赖 sqlalchemy + utils,不触发 AstrBot 框架依赖,
可在脱离 AstrBot 运行环境的情况下独立测试。
生产代码统一 `from astrbot.api import logger`(插件规范禁止内置 logging 回退),
故在检测不到 astrbot 包时,在此注入仅含 logger 的 astrbot.api 桩模块。
"""

import importlib.util
import sys
import types
from pathlib import Path

# tests/ -> 插件目录 -> plugins/ (astrbot_plugin_chat_engine 的父目录)
_PLUGINS_DIR = Path(__file__).resolve().parent.parent.parent
if str(_PLUGINS_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGINS_DIR))


class _StubLogger:
    """占位 logger:吞掉任意级别的日志调用,独立单测不依赖 AstrBot 框架。"""

    def __getattr__(self, _name: str):
        return lambda *args, **kwargs: None


def _install_astrbot_api_stub() -> None:
    """注入 astrbot / astrbot.api 桩模块,使 `from astrbot.api import logger` 可导入。"""
    pkg = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = _StubLogger()
    pkg.api = api
    sys.modules["astrbot"] = pkg
    sys.modules["astrbot.api"] = api


if importlib.util.find_spec("astrbot") is None:
    _install_astrbot_api_stub()
