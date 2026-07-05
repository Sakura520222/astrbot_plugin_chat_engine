"""OpenAI-compatible image generation client.

封装 ``POST {api_base}/images/generations`` 调用，返回图片二进制字节。
不依赖 AstrBot API，便于单独测试与复用。
"""

import base64
import json

import aiohttp

from astrbot.api import logger


class ImageGenError(Exception):
    """画图客户端异常 — 携带可读的错误信息（HTTP 状态/网络错误/解析失败等）。"""


class OpenAIImageClient:
    """OpenAI 兼容的文生图客户端。

    每次 :meth:`generate` 调用创建独立的 ``aiohttp.ClientSession``，
    避免长期持有连接与超时配置冲突。
    """

    def __init__(
        self,
        api_base: str,
        api_key: str,
        model: str,
        size: str = "1024x1024",
        quality: str = "auto",
        timeout: int = 120,
    ):
        self.api_base = api_base.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.size = size
        self.quality = quality
        self.timeout = timeout

    def _url(self, path: str) -> str:
        """拼接 API 端点 URL。

        兼容 base URL 是否带 ``/v1`` 后缀：
        - ``https://api.openai.com`` + ``/images/generations`` → ``.../v1/images/generations``
        - ``https://api.openai.com/v1`` + ``/images/edits`` → ``.../v1/images/edits``
        """
        base = self.api_base
        if not base.endswith("/v1"):
            base = base + "/v1"
        return base + path

    async def generate(self, prompt: str) -> bytes:
        """生成一张图片，返回图片字节（PNG/JPEG）。

        Args:
            prompt: 图片描述文本。

        Raises:
            ImageGenError: API 调用失败、响应解析失败或无图片数据时。
        """
        payload: dict = {
            "model": self.model,
            "prompt": prompt,
            "n": 1,
        }
        if self.size:
            payload["size"] = self.size
        if self.quality:
            payload["quality"] = self.quality

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        try:
            timeout = aiohttp.ClientTimeout(total=max(10, self.timeout))
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    self._url("/images/generations"), json=payload, headers=headers
                ) as resp:
                    text = await resp.text()
                    if resp.status != 200:
                        raise ImageGenError(
                            f"HTTP {resp.status}: {self._extract_error(text)}"
                        )
                    logger.info(
                        f"[ImageGen] 生成成功: model={self.model}, "
                        f"size={self.size}, quality={self.quality}, "
                        f"prompt={prompt[:50]}"
                    )
                    return await self._parse_image(text)
        except aiohttp.ClientError as e:
            raise ImageGenError(f"网络错误: {type(e).__name__}: {e}") from e
        except ImageGenError:
            raise
        except Exception as e:
            raise ImageGenError(f"请求异常: {type(e).__name__}: {e}") from e

    async def edit(self, prompt: str, image_urls: list[str]) -> bytes:
        """基于参考图二次创作，返回新图片字节。

        调用 ``POST /v1/images/edits``（multipart/form-data），支持 gpt-image 系列
        的多图融合（多张参考图时用 ``image[]`` 字段）。

        Args:
            prompt: 修改/创作描述。
            image_urls: 参考图 URL 列表（data URL 或 HTTP URL），至少一张。

        Raises:
            ImageGenError: 参数缺失、API 调用失败或解析失败时。
        """
        if not image_urls:
            raise ImageGenError("至少需要一张参考图片")

        form = aiohttp.FormData()
        form.add_field("model", self.model)
        form.add_field("prompt", prompt)
        form.add_field("n", "1")
        if self.size:
            form.add_field("size", self.size)
        if self.quality:
            form.add_field("quality", self.quality)

        # 收集参考图：data URL 直接解析，HTTP URL 下载
        # 单图用 image 字段，多图用 image[]（gpt-image 系列多图融合）
        field_name = "image[]" if len(image_urls) > 1 else "image"
        for i, url in enumerate(image_urls):
            if url.startswith("data:"):
                img_bytes, mime = self._parse_data_url(url)
            else:
                img_bytes, mime = await self._download_url(url)
            ext = (mime.split("/")[-1].replace("jpeg", "jpg")) or "png"
            form.add_field(
                field_name,
                img_bytes,
                filename=f"image_{i}.{ext}",
                content_type=mime,
            )

        # 注意：multipart 请求不能手动设 Content-Type，aiohttp 会自动带 boundary
        headers = {"Authorization": f"Bearer {self.api_key}"}

        try:
            timeout = aiohttp.ClientTimeout(total=max(10, self.timeout))
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    self._url("/images/edits"), data=form, headers=headers
                ) as resp:
                    text = await resp.text()
                    if resp.status != 200:
                        raise ImageGenError(
                            f"HTTP {resp.status}: {self._extract_error(text)}"
                        )
                    logger.info(
                        f"[ImageGen] 二次创作成功: model={self.model}, "
                        f"refs={len(image_urls)}, size={self.size}, "
                        f"quality={self.quality}, prompt={prompt[:50]}"
                    )
                    return await self._parse_image(text)
        except aiohttp.ClientError as e:
            raise ImageGenError(f"网络错误: {type(e).__name__}: {e}") from e
        except ImageGenError:
            raise
        except Exception as e:
            raise ImageGenError(f"请求异常: {type(e).__name__}: {e}") from e

    @staticmethod
    def _extract_error(text: str) -> str:
        """从错误响应中提取 message 字段，失败则返回原文（截断 300 字符）。"""
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return text[:300]

        err = data.get("error")
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])[:300]
        if isinstance(data.get("message"), str):
            return data["message"][:300]
        return text[:300]

    @staticmethod
    async def _parse_image(text: str) -> bytes:
        """从成功响应解析第一张图片的字节。

        优先 ``b64_json``，其次 ``url``（下载为字节）。
        兼容 gpt-image 系列（仅返回 b64_json）与 DALL·E 系列（url 或 b64_json）。
        """
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise ImageGenError(f"响应非 JSON: {e}") from e

        items = data.get("data") or []
        if not isinstance(items, list) or not items:
            raise ImageGenError("响应中无 data 字段")

        first = items[0]
        if not isinstance(first, dict):
            raise ImageGenError("响应 data[0] 格式异常")

        b64 = first.get("b64_json")
        if b64:
            try:
                return base64.b64decode(b64)
            except Exception as e:
                raise ImageGenError(f"base64 解码失败: {e}") from e

        url = first.get("url")
        if url:
            img_bytes, _ = await OpenAIImageClient._download_url(url)
            return img_bytes

        raise ImageGenError("响应中既无 b64_json 也无 url")

    @staticmethod
    async def _download_url(url: str) -> tuple[bytes, str]:
        """下载图片 URL，返回 (字节, MIME)。"""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        raise ImageGenError(f"图片下载 HTTP {resp.status}")
                    mime = resp.content_type or "image/png"
                    if not mime.startswith("image/"):
                        mime = "image/png"
                    return await resp.read(), mime
        except aiohttp.ClientError as e:
            raise ImageGenError(f"图片下载失败: {type(e).__name__}: {e}") from e

    @staticmethod
    def _parse_data_url(data_url: str) -> tuple[bytes, str]:
        """解析 data URL，返回 (字节, MIME)。"""
        try:
            header, b64 = data_url.split(",", 1)
        except ValueError as e:
            raise ImageGenError(f"data URL 格式无效: {e}") from e
        mime = "image/png"
        if "image/" in header:
            mime = header.split(":")[1].split(";")[0]
        try:
            return base64.b64decode(b64), mime
        except Exception as e:
            raise ImageGenError(f"data URL 解码失败: {e}") from e
