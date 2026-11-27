"""Image generation module — OpenAI-compatible text-to-image client and avatar fetching."""

from .avatar import AvatarCache, AvatarError
from .client import ImageGenError, OpenAIImageClient

__all__ = [
    "OpenAIImageClient",
    "ImageGenError",
    "AvatarError",
    "AvatarCache",
]
