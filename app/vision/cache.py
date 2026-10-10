"""
图片描述缓存（P0-3）
===================

同一张图（尤其长截图和表情包）在群里经常被反复转发。老的实现每发一次就请求一次视觉接口：
既花钱又慢，而且一旦那次失败，用户看到的就是"没看清"。

缓存键 = 所有图片内容 MD5 + 提示词版本 拼起来再哈希：
  - 单图重发 → 命中缓存（最常见的场景）
  - 多图组合只要有变化 → 视为新请求（避免错误复用）
  - 提示词改了 → 也视为新请求（`_PROMPT_VERSION` 手动升版本即可）

一个键一个 JSON 文件，避免"读整份 → 改 → 写回"的大文件竞争（和对话流同一思路）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

from app.config import Config
from app.paths import VISION_CACHE_DIR, ensure_dir

logger = logging.getLogger(__name__)

# 提示词改了要升这个版本号，否则会复用旧提示词生成的描述
_PROMPT_VERSION = "v1"


class VisionCache:
    def __init__(self, cache_dir: Optional[Path] = None, ttl_days: Optional[int] = None):
        self._dir = ensure_dir(cache_dir or VISION_CACHE_DIR)
        self.ttl_seconds = int((ttl_days if ttl_days is not None else Config.VISION_CACHE_DAYS) * 86400)
        self._hits = 0
        self._misses = 0

    # ---------- 键 ----------

    @staticmethod
    def key_for(image_md5s: list[str], prompt: str = "") -> str:
        raw = "|".join(sorted(image_md5s)) + "|" + _PROMPT_VERSION + "|" + hashlib.md5(
            prompt.encode("utf-8")
        ).hexdigest()
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:32]

    def _path(self, key: str) -> Path:
        return self._dir / f"{key}.json"

    # ---------- 读写（阻塞；异步侧用 run_blocking） ----------

    def get(self, key: str) -> Optional[str]:
        path = self._path(key)
        if not path.exists():
            self._misses += 1
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"[VisionCache] 缓存损坏，忽略: {key[:8]} {e}")
            self._misses += 1
            return None
        ts = data.get("ts", 0)
        if self.ttl_seconds and (time.time() - ts) > self.ttl_seconds:
            self._misses += 1
            logger.debug(f"[VisionCache] 缓存过期: {key[:8]}")
            return None
        desc = data.get("description") or ""
        if not desc:
            self._misses += 1
            return None
        self._hits += 1
        return desc

    def put(self, key: str, description: str, image_md5s: list[str]) -> None:
        if not description:
            return
        path = self._path(key)
        payload = {
            "ts": time.time(),
            "description": description,
            "images": image_md5s,
            "prompt_version": _PROMPT_VERSION,
        }
        try:
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, path)
        except OSError as e:
            logger.warning(f"[VisionCache] 写入失败: {e}")

    # ---------- 维护 ----------

    def purge_expired(self) -> int:
        """清掉过期缓存，返回删除数量（由 P3 的 cleanup 任务调用）"""
        if not self._dir.is_dir():
            return 0
        now = time.time()
        removed = 0
        for p in self._dir.glob("*.json"):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                if self.ttl_seconds and (now - data.get("ts", 0)) > self.ttl_seconds:
                    p.unlink()
                    removed += 1
            except Exception:
                continue
        if removed:
            logger.info(f"[VisionCache] 清理过期缓存 {removed} 条")
        return removed

    def stats(self) -> dict:
        entries = len(list(self._dir.glob("*.json"))) if self._dir.is_dir() else 0
        return {"entries": entries, "hits": self._hits, "misses": self._misses}
