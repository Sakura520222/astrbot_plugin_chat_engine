"""get_avatar 工具单元测试（Issue #13）。

覆盖：目标解析（bot/sender/mentioned/user_id、多 @ 歧义）、MIME 魔数识别、
下载校验（超限 / 非图片 / 重定向）、Telegram 平台路由、缓存 TTL 与容量、
会话参考图合并（replace/append/去重/上限）、data URL 解码。
"""

import asyncio
import base64
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from astrbot_plugin_chat_engine.image_gen import avatar as av
from astrbot_plugin_chat_engine.image_gen.avatar import (
    AvatarCache,
    AvatarError,
    build_qq_avatar_url,
    decode_data_url,
    detect_image_mime,
    extract_at_targets,
    fetch_avatar,
    fetch_telegram_avatar_url,
    merge_ref,
    resolve_avatar_target,
)

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_JPEG_MAGIC = b"\xff\xd8\xff\xe0"
_WEBP_MAGIC = b"RIFF\x24\x60\x00\x00WEBPVP8 "
_GIF_MAGIC = b"GIF89a"
_BMP_MAGIC = b"BM\x36\x00"


def _tiny_png() -> bytes:
    """生成真实可解码的 2x2 PNG，作为下载测试夹具（须通过 PIL 解码校验）。"""
    from io import BytesIO

    from PIL import Image

    buf = BytesIO()
    Image.new("RGB", (2, 2), (120, 40, 200)).save(buf, format="PNG")
    return buf.getvalue()


# --- MIME 魔数识别 ---


def test_detect_image_mime_known_formats():
    assert detect_image_mime(_PNG_MAGIC + b"x") == "image/png"
    assert detect_image_mime(_JPEG_MAGIC + b"x") == "image/jpeg"
    assert detect_image_mime(_WEBP_MAGIC) == "image/webp"
    assert detect_image_mime(_GIF_MAGIC + b"x") == "image/gif"
    assert detect_image_mime(_BMP_MAGIC + b"x") == "image/bmp"


def test_detect_image_mime_rejects_non_image():
    assert detect_image_mime(b"<html>error page</html>") is None
    assert detect_image_mime(b"") is None
    # RIFF 容器但非 WEBP 标识
    assert detect_image_mime(b"RIFF\x00\x00\x00\x00WAVEfmt ") is None


# --- QQ 头像 URL ---


def test_build_qq_avatar_url():
    assert build_qq_avatar_url("12345") == "https://q1.qlogo.cn/g?b=qq&nk=12345&s=640"


# --- data URL 解码 ---


def test_decode_data_url_roundtrip():
    raw = _PNG_MAGIC + b"payload"
    url = "data:image/png;base64," + base64.b64encode(raw).decode()
    assert decode_data_url(url) == raw


def test_decode_data_url_invalid():
    assert decode_data_url("not-a-data-url") is None
    assert decode_data_url("data:image/png;base64,!!!not-base64!!!") is None


# --- @ 目标提取 ---


def test_extract_at_targets_dedupes_and_skips_all():
    from astrbot.api.message_components import At, Plain

    comps = [
        Plain(text="hi"),
        At(qq="111", name="甲"),
        At(qq="111", name="甲"),
        At(qq="all", name="全体成员"),
        At(qq="222", name="乙"),
    ]
    assert extract_at_targets(comps) == [("111", "甲"), ("222", "乙")]
    assert extract_at_targets(None) == []
    assert extract_at_targets([Plain(text="no at")]) == []


# --- 目标解析 ---


def test_resolve_target_bot_uses_self_id():
    t, err = resolve_avatar_target("bot", "", "999", "111", [])
    assert err is None and t.kind == "bot" and t.uid == "999"


def test_resolve_target_sender():
    t, err = resolve_avatar_target("sender", "", "999", "111", [])
    assert err is None and t.kind == "user" and t.uid == "111"


def test_resolve_target_user_id():
    t, err = resolve_avatar_target("user_id", "777", "999", "111", [])
    assert err is None and t.kind == "user" and t.uid == "777"
    # 缺少 user_id 参数
    t, err = resolve_avatar_target("user_id", "", "999", "111", [])
    assert t is None and "requires the user_id" in err


def test_resolve_target_invalid():
    t, err = resolve_avatar_target("everyone", "", "999", "111", [])
    assert t is None and "Invalid target" in err


def test_resolve_target_mentioned_single():
    t, err = resolve_avatar_target("mentioned", "", "999", "111", [("222", "乙")])
    assert err is None and t.kind == "user" and t.uid == "222"


def test_resolve_target_mentioned_bot_only():
    t, err = resolve_avatar_target("mentioned", "", "999", "111", [("999", "Bot")])
    assert err is None and t.kind == "bot" and t.uid == "999"


def test_resolve_target_mentioned_bot_plus_user():
    # @Bot + @用户：取非 Bot 的那一个
    t, err = resolve_avatar_target(
        "mentioned", "", "999", "111", [("999", "Bot"), ("222", "乙")]
    )
    assert err is None and t.kind == "user" and t.uid == "222"


def test_resolve_target_mentioned_multiple_users_requires_explicit_choice():
    t, err = resolve_avatar_target(
        "mentioned", "", "999", "111", [("222", "乙"), ("333", "丙")]
    )
    assert t is None
    assert "Multiple users are mentioned" in err
    assert 'target="user_id"' in err


def test_resolve_target_mentioned_none():
    t, err = resolve_avatar_target("mentioned", "", "999", "111", [])
    assert t is None and "No @-mentioned user" in err


# --- 会话参考图合并 ---


def test_merge_ref_replace():
    assert merge_ref([{"image_id": 1}], {"image_id": 2}, append=False) == [
        {"image_id": 2}
    ]


def test_merge_ref_append_and_dedupe_tail():
    merged = merge_ref([], {"image_id": 1}, append=True)
    merged = merge_ref(merged, {"image_id": 2}, append=True)
    assert merged == [{"image_id": 1}, {"image_id": 2}]
    # 连续相同引用去重
    assert merge_ref(merged, {"image_id": 2}, append=True) == merged


def test_merge_ref_append_caps():
    merged = []
    for i in range(6):
        merged = merge_ref(merged, {"image_id": i}, append=True)
    assert [r["image_id"] for r in merged] == [2, 3, 4, 5]


# --- 缓存 ---


def test_cache_put_get_roundtrip():
    cache = AvatarCache()
    cache.put(("p1", "111"), {"image_id": 1})
    assert cache.get(("p1", "111")) == {"image_id": 1}
    assert cache.get(("p1", "222")) is None
    assert cache.get(("p2", "111")) is None


def test_cache_ttl_expiry():
    cache = AvatarCache(ttl_sec=0.01)
    cache.put(("p1", "111"), {"image_id": 1})
    import time as _t

    _t.sleep(0.03)
    assert cache.get(("p1", "111")) is None


def test_cache_max_entries_evicts_oldest():
    cache = AvatarCache(max_entries=2)
    cache.put(("p1", "1"), {"image_id": 1})
    import time as _t

    _t.sleep(0.005)
    cache.put(("p1", "2"), {"image_id": 2})
    _t.sleep(0.005)
    cache.put(("p1", "3"), {"image_id": 3})
    assert cache.get(("p1", "1")) is None  # 最旧被淘汰
    assert cache.get(("p1", "2")) == {"image_id": 2}
    assert cache.get(("p1", "3")) == {"image_id": 3}


# --- 下载校验（本地 aiohttp 测试服务）---


async def _start_server(routes: list[tuple[str, object]]) -> TestServer:
    app = web.Application()
    for path, handler in routes:
        app.router.add_get(path, handler)
    server = TestServer(app)
    await server.start_server()
    return server


async def test_download_image_ok():
    png = _tiny_png()

    async def handler(request):
        return web.Response(body=png, content_type="image/png")

    server = await _start_server([("/avatar.png", handler)])
    try:
        data, mime = await av.download_image(
            f"http://127.0.0.1:{server.port}/avatar.png"
        )
        assert (data, mime) == (png, "image/png")
    finally:
        await server.close()


async def test_download_image_octet_stream_magic_wins():
    # Content-Type 不可信时以魔数为准
    png = _tiny_png()

    async def handler(request):
        return web.Response(body=png, content_type="application/octet-stream")

    server = await _start_server([("/a", handler)])
    try:
        data, mime = await av.download_image(f"http://127.0.0.1:{server.port}/a")
        assert mime == "image/png" and data == png
    finally:
        await server.close()


async def test_download_image_rejects_html_error_page():
    async def handler(request):
        return web.Response(body=b"<html>404</html>", content_type="text/html")

    server = await _start_server([("/a", handler)])
    try:
        with pytest.raises(AvatarError, match="non-image content-type"):
            await av.download_image(f"http://127.0.0.1:{server.port}/a")
    finally:
        await server.close()


async def test_download_image_rejects_bad_magic(monkeypatch):
    async def handler(request):
        return web.Response(
            body=b"<html>fake</html>", content_type="application/octet-stream"
        )

    server = await _start_server([("/a", handler)])
    try:
        with pytest.raises(AvatarError, match="magic check failed"):
            await av.download_image(f"http://127.0.0.1:{server.port}/a")
    finally:
        await server.close()


async def test_download_image_rejects_oversize(monkeypatch):
    # 上限设为「完整图大小 - 1」：读满 MAX+1 字节后 Content-Length 一致、
    # 仅触发超限判断（多读 1 字节判超限）
    tiny = _tiny_png()
    monkeypatch.setattr(av, "MAX_AVATAR_BYTES", len(tiny) - 1)

    async def handler(request):
        return web.Response(body=tiny, content_type="image/png")

    server = await _start_server([("/a", handler)])
    try:
        with pytest.raises(AvatarError, match="max size"):
            await av.download_image(f"http://127.0.0.1:{server.port}/a")
    finally:
        await server.close()


async def test_download_image_rejects_error_status():
    async def handler(request):
        return web.Response(status=403, text="forbidden")

    server = await _start_server([("/a", handler)])
    try:
        with pytest.raises(AvatarError, match="HTTP 403"):
            await av.download_image(f"http://127.0.0.1:{server.port}/a")
    finally:
        await server.close()


async def test_download_image_rejects_redirect_loop(monkeypatch):
    async def redirect(request):
        raise web.HTTPFound(location="/loop")

    server = await _start_server([("/loop", redirect)])
    try:
        with pytest.raises(AvatarError, match="redirect"):
            await av.download_image(f"http://127.0.0.1:{server.port}/loop")
    finally:
        await server.close()


# --- Telegram 平台路由 ---


def _tg_client(total_count=1, file_url="https://t.me/file/abc", fail_get_file=False):
    photo = SimpleNamespace(file_id="FID", width=640, height=640)

    async def get_user_profile_photos(user_id=None, limit=None):
        if total_count:
            return SimpleNamespace(total_count=total_count, photos=[[photo]])
        return SimpleNamespace(total_count=0, photos=[])

    async def get_file(file_id=None):
        if fail_get_file:
            raise RuntimeError("boom")
        return SimpleNamespace(file_path=file_url)

    return SimpleNamespace(
        get_user_profile_photos=get_user_profile_photos, get_file=get_file
    )


async def test_telegram_avatar_url_success():
    url = await fetch_telegram_avatar_url(_tg_client(), "111")
    assert url == "https://t.me/file/abc"


async def test_telegram_avatar_no_photos():
    with pytest.raises(AvatarError, match="no accessible profile photo"):
        await fetch_telegram_avatar_url(_tg_client(total_count=0), "111")


async def test_telegram_avatar_get_file_failure():
    with pytest.raises(AvatarError, match="getFile failed"):
        await fetch_telegram_avatar_url(_tg_client(fail_get_file=True), "111")


async def test_telegram_avatar_bad_file_url():
    with pytest.raises(AvatarError, match="unavailable"):
        await fetch_telegram_avatar_url(_tg_client(file_url="/relative/path"), "111")


async def test_telegram_avatar_invalid_user_id():
    with pytest.raises(AvatarError, match="Invalid Telegram user ID"):
        await fetch_telegram_avatar_url(_tg_client(), "not-a-number")


async def test_fetch_avatar_telegram_bot_explicitly_unsupported():
    event = SimpleNamespace(get_platform_name=lambda: "telegram", client=_tg_client())
    # Bot API 无法获取 Bot 自身头像 — 必须明确报错而非伪造 URL
    with pytest.raises(AvatarError, match="cannot fetch the bot's own avatar"):
        await fetch_avatar(event, "bot", "999")


async def test_fetch_avatar_unsupported_platform():
    event = SimpleNamespace(get_platform_name=lambda: "wechat_official")
    with pytest.raises(AvatarError, match="not supported on platform"):
        await fetch_avatar(event, "user", "111")


async def test_fetch_avatar_aiocqhttp_routes_to_qq_service(monkeypatch):
    png = _tiny_png()

    async def handler(request):
        assert request.query["nk"] == "111"  # 平台用户 ID 正确透传
        return web.Response(body=png, content_type="image/jpeg")

    server = await _start_server([("/g", handler)])
    try:
        monkeypatch.setattr(
            av,
            "build_qq_avatar_url",
            lambda qq: f"http://127.0.0.1:{server.port}/g?b=qq&nk={qq}&s=640",
        )
        event = SimpleNamespace(get_platform_name=lambda: "aiocqhttp")
        data, mime = await fetch_avatar(event, "user", "111")
        assert data == png and mime == "image/png"  # 魔数为准
    finally:
        await server.close()


# --- 传输完整性校验（截断传输拦截，Issue #13 实测回归）---


def test_check_transfer_complete_passes():
    av._check_transfer_complete(b"abcdef", "6")
    av._check_transfer_complete(b"abcdef", None)
    av._check_transfer_complete(b"abcdef", "not-a-number")


def test_check_transfer_complete_rejects_truncation():
    # 实测场景：Content-Length 135598，实际仅收到 16093 字节
    with pytest.raises(AvatarError, match="incomplete.*16093 of 135598"):
        av._check_transfer_complete(b"x" * 16093, "135598")


def test_verify_decodable_truncated_jpeg():
    from io import BytesIO

    from PIL import Image

    buf = BytesIO()
    Image.new("RGB", (8, 8), (200, 100, 100)).save(buf, format="JPEG")
    full = buf.getvalue()
    assert full.endswith(b"\xff\xd9")
    av._verify_decodable(full, "image/jpeg")  # 完整图通过

    truncated = full[:-6]
    with pytest.raises(AvatarError, match="truncated"):
        av._verify_decodable(truncated, "image/jpeg")


def test_verify_decodable_png_passthrough():
    av._verify_decodable(_tiny_png(), "image/png")  # 完整 PNG 通过全量解码


async def test_download_image_rejects_truncated_jpeg():
    from io import BytesIO

    from PIL import Image

    buf = BytesIO()
    Image.new("RGB", (8, 8), (10, 200, 10)).save(buf, format="JPEG")
    full = buf.getvalue()
    partial = full[:-6]  # 模拟链路中途截断

    async def handler(request):
        # aiohttp 服务器会按 body 重设 Content-Length，故此处仅覆盖
        # EOI / 解码校验路径；Content-Length 不一致路径由纯函数测试覆盖
        return web.Response(body=partial, content_type="application/octet-stream")

    server = await _start_server([("/a", handler)])
    try:
        with pytest.raises(AvatarError):
            await av.download_image(f"http://127.0.0.1:{server.port}/a")
    finally:
        await server.close()


# --- 失败自动重试 ---


async def test_download_image_retries_and_succeeds():
    png = _tiny_png()
    state = {"n": 0}

    async def handler(request):
        state["n"] += 1
        if state["n"] == 1:
            return web.Response(status=503, text="flaky")
        return web.Response(body=png, content_type="image/png")

    server = await _start_server([("/a", handler)])
    try:
        data, mime = await av.download_image(f"http://127.0.0.1:{server.port}/a")
        assert data == png and mime == "image/png"
        assert state["n"] == 2
    finally:
        await server.close()


async def test_download_image_raises_after_retries_exhausted():
    state = {"n": 0}

    async def handler(request):
        state["n"] += 1
        return web.Response(status=500, text="down")

    server = await _start_server([("/a", handler)])
    try:
        with pytest.raises(AvatarError, match="HTTP 500"):
            await av.download_image(f"http://127.0.0.1:{server.port}/a")
        assert state["n"] == 2
    finally:
        await server.close()
