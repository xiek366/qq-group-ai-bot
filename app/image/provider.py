"""
AI 图片生成 Provider
---------------------
支持 Pollinations.ai（免费、无需 key）和可扩展的付费 provider。

Pollinations API:
  GET https://image.pollinations.ai/prompt/{prompt}?width=&height=&model=&nologo=true&enhance=true

可选模型 (Pollinations):
  flux          默认，综合效果
  flux-anime    二次元/动漫风格  ← 推荐给爱丽丝
  flux-realism  写实/摄影风格
  flux-pro      加强效果（更慢但更好）
  turbo         快速生成（质量稍低）
"""

import logging
import os
import random
import time
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import httpx

from app.config import Config
from app.paths import IMAGE_DIR, ensure_dir
from app.util.blocking import run_blocking

logger = logging.getLogger(__name__)


# 图片临时保存目录
def _get_image_dir() -> Path:
    return ensure_dir(IMAGE_DIR)


class ImageProvider:
    """图片生成基类 —— 不同 provider 实现 generate()"""

    async def generate(self, prompt: str, width: int = 1024, height: int = 1024) -> Optional[bytes]:
        raise NotImplementedError


class PollinationsProvider(ImageProvider):
    """Pollinations.ai —— 免费，无需 API Key"""

    # Pollinations 支持的模型列表
    MODELS = {"flux", "flux-anime", "flux-realism", "flux-pro", "turbo", "flux-3d"}

    BASE_URL = "https://image.pollinations.ai/prompt"

    def __init__(self, model: str = "flux-anime"):
        self.model = model if model in self.MODELS else "flux-anime"

    async def generate(self, prompt: str, width: int = 1024, height: int = 1024) -> Optional[bytes]:
        """
        调用 Pollinations 生成图片。
        返回: 图片二进制 bytes；失败返回 None
        """
        # URL 编码 prompt
        encoded = quote(prompt)
        seed = random.randint(1, 999999)
        url = (
            f"{self.BASE_URL}/{encoded}"
            f"?width={width}&height={height}"
            f"&model={self.model}"
            f"&nologo=true&enhance=true"
            f"&seed={seed}"
        )

        logger.info(f"[Image] Pollinations 请求: model={self.model} size={width}x{height} prompt={prompt[:60]}...")
        try:
            async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
                resp = await client.get(url)
                if resp.status_code != 200:
                    logger.error(f"[Image] Pollinations HTTP {resp.status_code}: {resp.text[:200]}")
                    return None
                # 验证确实拿到了图片（不是 HTML 错误页）
                content_type = resp.headers.get("content-type", "")
                if "image" not in content_type and len(resp.content) < 1000:
                    logger.error(f"[Image] Pollinations 返回非图片: type={content_type}")
                    return None
                logger.info(f"[Image] Pollinations 成功: {len(resp.content)} bytes")
                return resp.content
        except httpx.TimeoutException:
            logger.error("[Image] Pollinations 超时（60 秒）")
            return None
        except Exception as e:
            logger.error(f"[Image] Pollinations 异常: {e}")
            return None


def create_provider() -> Optional[ImageProvider]:
    """根据 Config 创建对应 provider"""
    name = (Config.IMAGE_PROVIDER or "pollinations").lower()
    if name == "pollinations":
        model = Config.IMAGE_MODEL or "flux-anime"
        logger.info(f"[Image] 初始化 Pollinations provider, model={model}")
        return PollinationsProvider(model=model)
    else:
        logger.warning(f"[Image] 未知 IMAGE_PROVIDER={name}，跳过图片生成功能")
        return None


async def save_image(data: bytes, filename: Optional[str] = None) -> Optional[str]:
    """
    把图片 bytes 保存到本地 data/images/ 目录。
    返回: 本地绝对路径字符串（OneBot CQ:image 用 file:/// 协议）
    """
    img_dir = _get_image_dir()
    if not filename:
        filename = f"img_{int(time.time())}_{random.randint(1000, 9999)}.jpg"
    filepath = img_dir / filename
    # 图片可能有几百 KB，写盘放线程里，别钉住事件循环
    await run_blocking(filepath.write_bytes, data)
    logger.info(f"[Image] 保存: {filepath}")
    return str(filepath.resolve())
