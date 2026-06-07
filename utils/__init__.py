from datetime import datetime, timezone, timedelta

# 上海时区 (UTC+8)
_SHANGHAI_TZ = timezone(timedelta(hours=8))


def format_current_time() -> str:
    """返回当前上海时区时间的格式化字符串，统一时间格式。"""
    return datetime.now(_SHANGHAI_TZ).strftime("%Y-%m-%d %H:%M:%S")
