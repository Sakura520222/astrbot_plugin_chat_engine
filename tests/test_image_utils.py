"""utils.image 单元测试 — base64 魔数检测与本地文件 data URL 转换。

覆盖:
- 格式检测: jpeg/png/gif/webp/bmp 魔数前缀各自命中, 未知魔数兜底 jpeg
- 本地文件: 存在的图片文件读取为 data URL 且字节一致
- 本地文件: 不存在的路径 / HTTP URL 字符串均返回 None
"""

import base64

import pytest

from astrbot_plugin_chat_engine.utils.image import (
    image_data_url_from_base64,
    local_image_to_data_url,
)


@pytest.mark.parametrize(
    ("raw", "mime"),
    [
        (b"\xff\xd8\xff\xe0JFIF", "jpeg"),
        (b"\x89PNG\r\n\x1a\n", "png"),
        (b"GIF89a", "gif"),
        (b"RIFF\x00\x00\x00\x00WEBP", "webp"),
        (b"BM\x36\x00", "bmp"),
        (b"\x00\x01\x02\x03", "jpeg"),  # 未知魔数兜底
    ],
)
def test_image_data_url_from_base64(raw, mime):
    data_url = image_data_url_from_base64(base64.b64encode(raw).decode())

    assert data_url.startswith(f"data:image/{mime};base64,")
    assert base64.b64decode(data_url.split(",", 1)[1]) == raw


async def test_local_image_to_data_url_reads_file(tmp_path):
    raw = b"\x89PNG\r\n\x1a\n" + b"fake-png-body"
    img = tmp_path / "img.png"
    img.write_bytes(raw)

    data_url = await local_image_to_data_url(str(img))

    assert data_url is not None
    assert data_url.startswith("data:image/png;base64,")
    assert base64.b64decode(data_url.split(",", 1)[1]) == raw


async def test_local_image_to_data_url_missing_file(tmp_path):
    assert await local_image_to_data_url(str(tmp_path / "nope.gif")) is None


async def test_local_image_to_data_url_rejects_url():
    # URL 字符串不是本地文件路径, 不应被当作可读文件
    assert await local_image_to_data_url("https://example.com/a.png") is None
