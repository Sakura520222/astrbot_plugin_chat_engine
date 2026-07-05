"""Image generation module — OpenAI-compatible text-to-image client."""

from .client import ImageGenError, OpenAIImageClient

__all__ = ["OpenAIImageClient", "ImageGenError"]
