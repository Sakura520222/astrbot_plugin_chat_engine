"""图片 data URL 工具 — base64 魔数检测与本地文件读取。

仅依赖标准库与 aiofiles（与 utils 其他模块一致，不依赖 AstrBot 框架），
供 main.py 图片链路使用，并可在脱离框架的环境下独立测试。
"""

import base64
import os

import aiofiles

# base64 编码后的图片魔数前缀 → MIME 子类型
_B64_MAGIC_PREFIXES: tuple[tuple[str, str], ...] = (
    ("/9j/", "jpeg"),
    ("iVBOR", "png"),
    ("R0lG", "gif"),
    ("UklG", "webp"),
    ("Qk0", "bmp"),
)


def image_data_url_from_base64(b64: str) -> str:
    """通过 base64 魔数前缀检测图片格式并拼成 data URL。

    未知魔数按 jpeg 处理（主流平台的通用兜底）。
    """
    fmt = "jpeg"
    for prefix, mime in _B64_MAGIC_PREFIXES:
        if b64.startswith(prefix):
            fmt = mime
            break
    return f"data:image/{fmt};base64,{b64}"


async def local_image_to_data_url(path: str) -> str | None:
    """读取本地图片文件为 data URL；文件不存在或读取失败返回 None。

    不打日志 — 调用方负责记录失败路径与原因。
    """
    if not os.path.isfile(path):
        return None
    try:
        async with aiofiles.open(path, "rb") as f:
            raw = await f.read()
    except OSError:
        return None
    return image_data_url_from_base64(base64.b64encode(raw).decode())
