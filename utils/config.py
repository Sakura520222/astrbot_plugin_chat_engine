"""共享配置读取辅助函数。"""


def cfg_int(config: dict, key: str, default: int) -> int:
    """安全读取 int 配置项，类型异常时回退到默认值。"""
    try:
        return int(config.get(key, default))
    except (ValueError, TypeError):
        return default


def cfg_float(config: dict, key: str, default: float) -> float:
    """安全读取 float 配置项，类型异常时回退到默认值。"""
    try:
        return float(config.get(key, default))
    except (ValueError, TypeError):
        return default


def cfg_bool(config: dict, key: str, default: bool) -> bool:
    """安全读取 bool 配置项，支持字符串和数值类型转换。"""
    val = config.get(key, default)
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.lower() in ("true", "1", "yes")
    if isinstance(val, (int, float)):
        return bool(val)
    return default
