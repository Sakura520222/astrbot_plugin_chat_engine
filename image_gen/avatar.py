"""Avatar fetching — 平台头像获取、校验与缓存（Issue #13）。

从受信平台来源获取头像（QQ 官方头像服务 / Telegram Bot API），不做任何
模型提供的 URL 下载；下载结果经超时 / 大小 / 重定向 / MIME / 魔数校验后
由调用方存入 ImageStore 并登记为会话最近参考图，衔接 edit_image 二创。

本模块不依赖 AstrBot 框架类型（仅 message_components 的 At 做惰性导入），
目标解析、MIME 识别与缓存均可脱离框架独立单测。
"""

import base64
import binascii
import time
from dataclasses import dataclass

import aiohttp

# --- 下载限制 ---
DOWNLOAD_TIMEOUT_SEC = 15
DOWNLOAD_CHUNK_SIZE = 64 * 1024  # 64 KiB
MAX_AVATAR_BYTES = 10 * 1024 * 1024  # 10 MB
MAX_REDIRECTS = 5

# --- 会话最近参考图列表上限（供多图融合）---
MAX_LAST_IMAGES = 4

# --- 缓存：TTL 内复用已存图片引用，头像更新后自动过期重取 ---
AVATAR_CACHE_TTL_SEC = 6 * 3600
AVATAR_CACHE_MAX_ENTRIES = 500

# --- 图片魔数 → MIME（RIFF 容器需额外校验 WEBP 标识）---
_MAGIC_BYTES: tuple[tuple[bytes, str | None, str], ...] = (
    (b"\xff\xd8\xff", None, "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", None, "image/png"),
    (b"GIF87a", None, "image/gif"),
    (b"GIF89a", None, "image/gif"),
    (b"BM", None, "image/bmp"),
    (b"RIFF", 8, "image/webp"),  # bytes[8:12] == b"WEBP"
)

_VALID_TARGETS = ("bot", "sender", "mentioned", "user_id")


class AvatarError(Exception):
    """头像获取失败（平台不支持 / 无权限 / API 失败 / 校验不通过）。"""


def detect_image_mime(data: bytes) -> str | None:
    """通过文件魔数识别图片 MIME 类型；非图片或空数据返回 None。"""
    if not data:
        return None
    for magic, check_offset, mime in _MAGIC_BYTES:
        if not data.startswith(magic):
            continue
        if check_offset is not None:
            if (
                len(data) < check_offset + 4
                or data[check_offset : check_offset + 4] != b"WEBP"
            ):
                continue
        return mime
    return None


def build_qq_avatar_url(qq: str) -> str:
    """QQ 官方头像服务 URL（640 尺寸，NapCat/Lagrange 等实现通用）。"""
    return f"https://q1.qlogo.cn/g?b=qq&nk={qq}&s=640"


def decode_data_url(data_url: str) -> bytes | None:
    """从 data URL 解出原始字节；格式无效或解码失败返回 None。"""
    try:
        _, b64 = data_url.split(",", 1)
        return base64.b64decode(b64)
    except (ValueError, binascii.Error):
        return None


@dataclass
class AvatarTarget:
    """解析后的头像目标。kind="bot" 时 uid 为 Bot 自身平台 ID。"""

    kind: str  # "bot" | "user"
    uid: str


def extract_at_targets(components) -> list[tuple[str, str]]:
    """从消息组件提取 @ 目标 [(qq, name)]，排除 @全体 并按 qq 去重。

    组件异常逐个跳过，不抛出。At 类型惰性导入，保持模块可脱离框架加载。
    """
    try:
        from astrbot.api.message_components import At
    except Exception:  # pragma: no cover - 框架缺失时的降级路径
        return []
    targets: list[tuple[str, str]] = []
    seen: set[str] = set()
    try:
        items = list(components or [])
    except Exception:
        return []
    for comp in items:
        try:
            if not isinstance(comp, At):
                continue
            qq = str(getattr(comp, "qq", "") or "").strip()
            if not qq or qq == "all" or qq in seen:
                continue
            seen.add(qq)
            targets.append((qq, str(getattr(comp, "name", "") or "").strip()))
        except Exception:
            continue
    return targets


def resolve_avatar_target(
    target: str,
    user_id: str,
    self_id: str,
    sender_id: str,
    at_targets: list[tuple[str, str]],
) -> tuple[AvatarTarget | None, str | None]:
    """解析 get_avatar 的目标参数为具体用户。

    Returns:
        (AvatarTarget, None) 成功；(None, error_msg) 失败（错误消息直接回传 LLM）。
    """
    target = str(target or "").strip().lower()
    user_id = str(user_id or "").strip()
    self_id = str(self_id or "").strip()
    sender_id = str(sender_id or "").strip()

    if target not in _VALID_TARGETS:
        return None, (
            f"Invalid target '{target}'. Use one of: {', '.join(_VALID_TARGETS)}."
        )
    if target == "user_id":
        if not user_id:
            return None, 'target="user_id" requires the user_id argument.'
        return AvatarTarget(kind="user", uid=user_id), None
    if target == "bot":
        if not self_id:
            return None, "Cannot determine the bot's own ID on this platform."
        return AvatarTarget(kind="bot", uid=self_id), None
    if target == "sender":
        if not sender_id:
            return None, "Cannot determine the sender's ID on this platform."
        return AvatarTarget(kind="user", uid=sender_id), None

    # target == "mentioned"
    non_bot = [qq for qq, _name in at_targets if qq != self_id]
    if len(at_targets) == 1 and at_targets[0][0] == self_id:
        # 仅 @ 了 Bot 自己
        return AvatarTarget(kind="bot", uid=self_id), None
    if len(non_bot) == 1:
        return AvatarTarget(kind="user", uid=non_bot[0]), None
    if len(non_bot) > 1:
        # 多个 @ 对象：要求模型显式指定，禁止随意取第一个
        listed = ", ".join(
            f"{name or qq}({qq})" for qq, name in at_targets if qq != self_id
        )
        return None, (
            f"Multiple users are mentioned ({listed}). "
            'Do not guess — call get_avatar again with target="user_id" '
            "and the exact platform user ID."
        )
    return None, (
        'No @-mentioned user found in the current message for target="mentioned".'
    )


class AvatarCache:
    """进程内头像缓存 {(platform_instance_id, platform_user_id): (ref, ts)}。

    平台实例 + 平台用户 ID 作键，避免不同 Bot 账号 / 平台实例串用；
    TTL 过期自动清除，容量上限淘汰最旧条目。
    """

    def __init__(
        self,
        ttl_sec: float = AVATAR_CACHE_TTL_SEC,
        max_entries: int = AVATAR_CACHE_MAX_ENTRIES,
    ):
        self._ttl = ttl_sec
        self._max = max_entries
        self._entries: dict[tuple[str, str], tuple[dict, float]] = {}

    def get(self, key: tuple[str, str]) -> dict | None:
        """取缓存引用；TTL 过期返回 None 并清除该条目。"""
        entry = self._entries.get(key)
        if entry is None:
            return None
        ref, stored_at = entry
        if time.monotonic() - stored_at > self._ttl:
            self._entries.pop(key, None)
            return None
        return ref

    def put(self, key: tuple[str, str], ref: dict) -> None:
        if len(self._entries) >= self._max and key not in self._entries:
            oldest = min(self._entries, key=lambda k: self._entries[k][1])
            self._entries.pop(oldest, None)
        self._entries[key] = (ref, time.monotonic())


def merge_ref(
    current: list[dict], ref: dict, append: bool, cap: int = MAX_LAST_IMAGES
) -> list[dict]:
    """合并会话最近参考图列表（纯函数）。

    - append=False：替换为单张（新生成 / 编辑图作废旧引用）
    - append=True：追加（如先后获取 Bot 与用户头像以便多图融合），
      连续相同引用去重，超出 cap 从头部淘汰
    """
    if not append:
        return [ref]
    merged = [r for r in (current or []) if isinstance(r, dict)]
    if merged and merged[-1] == ref:
        return merged
    merged.append(ref)
    if len(merged) > cap:
        del merged[:-cap]
    return merged


async def download_image(url: str, attempts: int = 2) -> tuple[bytes, str]:
    """从受信 URL 下载图片并校验（超时 / 大小 / 重定向 / 响应类型 / 完整性）。

    通过流式分块读完整响应体（注意 aiohttp 的 ``StreamReader.read(n)``
    在 ``n > 0`` 时只返回单次缓冲块而不循环读至 EOF，必须使用流式迭代读完），
    同时在读取过程中强制限制最大体积；下载完成后再校验 Content-Length 一致性、
    魔数、JPEG EOI 与可解码性。校验或网络失败自动重试一次。

    Returns:
        (image_bytes, mime)。

    Raises:
        AvatarError: 重试后仍下载失败、响应非图片或校验不通过。
    """
    last_err: AvatarError | None = None
    for _ in range(max(1, attempts)):
        try:
            return await _download_image_once(url)
        except AvatarError as e:
            last_err = e
    assert last_err is not None
    raise last_err


def _check_transfer_complete(data: bytes, content_length: str | None) -> None:
    """校验收到的字节数与响应声明一致，拦截中途断开的截断传输。"""
    if content_length is None:
        return
    try:
        expected = int(content_length)
    except (TypeError, ValueError):
        return
    if expected >= 0 and len(data) != expected:
        raise AvatarError(
            f"avatar download incomplete: got {len(data)} of {expected} bytes "
            "(connection closed early)"
        )


def _verify_decodable(data: bytes, mime: str) -> None:
    """校验图片可完整解码（截断 / 损坏数据在此拒绝）。

    JPEG 必须含 EOI(FFD9) 结尾（允许常见尾随空字节/空白填充）；Pillow 可用时
    再做全量解码兜底（覆盖无 Content-Length 的分块传输）。Pillow 不可用时跳过。
    """
    if mime == "image/jpeg" and not data.rstrip(b"\x00\r\n\t ").endswith(b"\xff\xd9"):
        raise AvatarError("avatar JPEG is truncated (missing EOI marker)")
    try:
        from io import BytesIO

        from PIL import Image
    except Exception:
        return
    try:
        with Image.open(BytesIO(data)) as img:
            img.load()
    except Exception as e:
        raise AvatarError(f"avatar image failed to decode: {type(e).__name__}") from e


async def _download_image_once(url: str) -> tuple[bytes, str]:
    """单次下载 + 全部校验；异常一律归一为 AvatarError。"""
    timeout = aiohttp.ClientTimeout(total=DOWNLOAD_TIMEOUT_SEC)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, max_redirects=MAX_REDIRECTS) as resp:
                if resp.status != 200:
                    raise AvatarError(f"avatar download HTTP {resp.status}")
                content_type = (resp.content_type or "").split(";")[0].strip().lower()
                # Content-Type 不可信（部分服务返回 application/octet-stream），
                # 仅在明确为非图片类型时提前拒绝，最终以魔数校验为准
                if content_type and not content_type.startswith("image/"):
                    if content_type not in (
                        "application/octet-stream",
                        "binary/octet-stream",
                    ):
                        raise AvatarError(
                            f"avatar download got non-image content-type "
                            f"'{content_type}' (possible error page)"
                        )
                content_encoding = (
                    (resp.headers.get("Content-Encoding") or "").strip().lower()
                )
                is_uncompressed = content_encoding in ("", "identity")
                content_length = resp.headers.get("Content-Length")
                if is_uncompressed and content_length is not None:
                    try:
                        declared_len = int(content_length)
                    except (TypeError, ValueError):
                        declared_len = -1
                    if declared_len > MAX_AVATAR_BYTES:
                        raise AvatarError(
                            f"avatar exceeds max size "
                            f"({declared_len} > {MAX_AVATAR_BYTES} bytes)"
                        )
                # 必须使用 iter_chunked 循环读至 EOF：aiohttp 的
                # resp.content.read(n) 在 n > 0 时只返回单个缓冲区（HTTPS 下
                # 通常仅首个 16 KiB TLS 记录），会导致大图全部被截断。
                buf = bytearray()
                async for chunk in resp.content.iter_chunked(DOWNLOAD_CHUNK_SIZE):
                    buf.extend(chunk)
                    if len(buf) > MAX_AVATAR_BYTES:
                        raise AvatarError(
                            f"avatar exceeds max size "
                            f"({len(buf)} > {MAX_AVATAR_BYTES} bytes)"
                        )
                data = bytes(buf)
    except AvatarError:
        raise
    except aiohttp.TooManyRedirects:
        raise AvatarError(f"avatar download exceeded {MAX_REDIRECTS} redirects")
    except aiohttp.ClientError as e:
        raise AvatarError(f"avatar download failed: {type(e).__name__}") from e
    except Exception as e:
        raise AvatarError(f"avatar download error: {type(e).__name__}") from e

    if is_uncompressed:
        _check_transfer_complete(data, content_length)
    mime = detect_image_mime(data)
    if mime is None:
        raise AvatarError("avatar response is not a valid image (magic check failed)")
    _verify_decodable(data, mime)
    return data, mime


async def fetch_telegram_avatar_url(client, user_id: str) -> str:
    """经 Telegram Bot API 获取用户头像的下载 URL。

    client 为 python-telegram-bot 的 ExtBot（duck-typed，便于独立测试）。
    """
    if client is None:
        raise AvatarError("Telegram client is unavailable on this event.")
    try:
        photos = await client.get_user_profile_photos(user_id=int(user_id), limit=1)
    except (TypeError, ValueError):
        raise AvatarError(f"Invalid Telegram user ID '{user_id}'.") from None
    except Exception as e:
        raise AvatarError(
            f"Telegram getUserProfilePhotos failed: {type(e).__name__}"
        ) from e

    if not getattr(photos, "total_count", 0) or not getattr(photos, "photos", None):
        raise AvatarError(
            f"User {user_id} has no accessible profile photo on Telegram."
        )
    # 最新一张头像中取分辨率最大的 PhotoSize
    sizes = photos.photos[0]
    best = max(sizes, key=lambda s: getattr(s, "width", 0) or 0)
    try:
        file = await client.get_file(best.file_id)
    except Exception as e:
        raise AvatarError(f"Telegram getFile failed: {type(e).__name__}") from e
    url = getattr(file, "file_path", "") or ""
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        raise AvatarError("Telegram file URL is unavailable.")
    return url


async def fetch_avatar(event, kind: str, uid: str) -> tuple[bytes, str]:
    """按事件所属平台适配器获取头像字节。

    头像地址一律由受信平台来源生成，不接受模型提供的 URL。

    Raises:
        AvatarError: 平台不支持或获取失败。
    """
    try:
        platform = str(event.get_platform_name() or "")
    except Exception:
        platform = ""

    if platform == "aiocqhttp":
        if kind != "bot" and not uid:
            raise AvatarError("Missing QQ user ID for avatar.")
        return await download_image(build_qq_avatar_url(uid))

    if platform == "telegram":
        if kind == "bot":
            # Bot API 无法获取 Bot 自身头像（getMe 不含头像字段）
            raise AvatarError(
                "Telegram Bot API cannot fetch the bot's own avatar (API limitation)."
            )
        client = getattr(event, "client", None)
        url = await fetch_telegram_avatar_url(client, uid)
        return await download_image(url)

    raise AvatarError(
        f"Avatar fetching is not supported on platform '{platform or 'unknown'}'."
    )


def describe_avatar_target(target: AvatarTarget) -> str:
    """工具返回消息中的目标描述。"""
    if target.kind == "bot":
        return "the bot itself"
    return f"user {target.uid}"
