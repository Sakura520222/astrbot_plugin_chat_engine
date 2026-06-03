"""Image storage service — 将图片保存到文件系统，按 sha256 去重。

消息中的图片以引用形式存储 (image_id)，加载时解析为 data URL。
"""

import base64
import hashlib
import os

from astrbot.api import logger

from ..db.image_repo import ImageRepository

# base64 前缀 → MIME 类型
_PREFIX_TO_MIME = {
    "/9j/": "image/jpeg",
    "iVBOR": "image/png",
    "R0lG": "image/gif",
    "UklG": "image/webp",
    "Qk0": "image/bmp",
}


def _detect_mime(b64: str) -> str:
    """通过 base64 前缀检测 MIME 类型"""
    for prefix, mime in _PREFIX_TO_MIME.items():
        if b64.startswith(prefix):
            return mime
    return "image/jpeg"


def _mime_to_ext(mime: str) -> str:
    """MIME 类型 → 文件扩展名"""
    return mime.split("/")[-1].replace("jpeg", "jpg")


class ImageStore:
    """图片存储服务 — 文件存储 + sha256 去重"""

    def __init__(self, image_dir: str, image_repo: ImageRepository):
        self.image_dir = image_dir
        self.repo = image_repo
        os.makedirs(image_dir, exist_ok=True)

    async def store_image(self, data_url: str) -> dict:
        """存储一张图片，返回引用信息 {"type": "image_ref", "image_id": int}。

        如果图片已存在（sha256 重复），直接复用已有记录。
        data_url 格式: "data:image/jpeg;base64,..."
        """
        try:
            # 解析 data URL
            if not data_url.startswith("data:"):
                # 非 data URL（原始 HTTP URL），暂时无法去重，存为原始引用
                return {"type": "image_url", "image_url": {"url": data_url}}

            header, b64 = data_url.split(",", 1)
            # 从 header 提取 mime 或从 base64 检测
            if "image/" in header:
                mime = header.split(":")[1].split(";")[0]
            else:
                mime = _detect_mime(b64)

            # 计算 sha256
            image_bytes = base64.b64decode(b64)
            sha256 = hashlib.sha256(image_bytes).hexdigest()

            # 查重
            existing = await self.repo.find_by_sha256(sha256)
            if existing:
                return {"type": "image_ref", "image_id": existing.id}

            # 保存到文件
            ext = _mime_to_ext(mime)
            file_name = f"{sha256[:16]}.{ext}"
            file_path = os.path.join(self.image_dir, file_name)

            with open(file_path, "wb") as f:
                f.write(image_bytes)

            # 数据库记录
            record = await self.repo.save(
                sha256=sha256,
                mime_type=mime,
                file_path=file_path,
                file_size=len(image_bytes),
            )
            return {"type": "image_ref", "image_id": record.id}

        except Exception as e:
            logger.warning(f"[ChatEngine] 图片存储失败: {e}")
            # 存储失败，保留原始 data URL
            return {"type": "image_url", "image_url": {"url": data_url}}

    async def resolve_image_ref(self, ref: dict) -> dict | None:
        """将 image_ref 引用解析为 image_url data URL。"""
        image_id = ref.get("image_id")
        if not image_id:
            return None

        record = await self.repo.get_by_id(image_id)
        if not record:
            return {"type": "text", "text": "[Image]"}

        try:
            with open(record.file_path, "rb") as f:
                image_bytes = f.read()
            b64 = base64.b64encode(image_bytes).decode("utf-8")
            data_url = f"data:{record.mime_type};base64,{b64}"
            return {"type": "image_url", "image_url": {"url": data_url}}
        except FileNotFoundError:
            logger.warning(f"[ChatEngine] 图片文件丢失: {record.file_path}")
            return {"type": "text", "text": "[Image]"}

    async def store_message_images(self, content: list[dict]) -> list[dict]:
        """处理消息内容列表中的所有图片，替换为引用。"""
        new_content = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "image_url":
                url = part.get("image_url", {}).get("url", "")
                if url.startswith("data:"):
                    ref = await self.store_image(url)
                    new_content.append(ref)
                else:
                    new_content.append(part)
            else:
                new_content.append(part)
        return new_content

    async def resolve_message_images(self, content: list[dict]) -> list[dict]:
        """解析消息内容列表中的所有图片引用，还原为 data URL。"""
        new_content = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "image_ref":
                resolved = await self.resolve_image_ref(part)
                if resolved:
                    new_content.append(resolved)
                else:
                    new_content.append({"type": "text", "text": "[Image]"})
            else:
                new_content.append(part)
        return new_content
