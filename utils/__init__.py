from datetime import datetime, timedelta, timezone

# 上海时区 (UTC+8)
SHANGHAI_TZ = timezone(timedelta(hours=8))


def shanghai_now() -> datetime:
    """返回当前上海时区的 naive datetime（去除 tzinfo，与旧数据兼容）。"""
    return datetime.now(SHANGHAI_TZ).replace(tzinfo=None)


def shanghai_now_iso() -> str:
    """返回当前上海时区时间的 ISO 格式字符串。"""
    return datetime.now(SHANGHAI_TZ).isoformat()


def format_current_time() -> str:
    """返回当前上海时区时间的格式化字符串，统一时间格式。"""
    return datetime.now(SHANGHAI_TZ).strftime("%Y-%m-%d %H:%M:%S")
