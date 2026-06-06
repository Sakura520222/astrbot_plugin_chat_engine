from datetime import datetime


def format_current_time() -> str:
    """返回当前本地时间的格式化字符串，统一时间格式。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
