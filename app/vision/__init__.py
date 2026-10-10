"""视觉管道：图片下载 / 校验 / 内联 / 缓存 / 失败降级（P0-3）"""

from app.vision.cache import VisionCache
from app.vision.pipeline import VisionPipeline, VisionResult

__all__ = ["VisionPipeline", "VisionResult", "VisionCache"]
