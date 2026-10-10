"""
表情包管理器 —— 收藏群友发的自定义表情，自动存储，支持随机挑选发送。
==================================================================

存储结构:
  data/stickers/
    index.json    ← 所有表情包的元数据索引
    xxx.gif       ← 图片文件（从群里下载的）
    yyy.png

NapCat sticker segment 结构:
  {"type": "sticker", "data": {"file": "xxx.gif", "url": "https://...", "id": "xxx"}}
"""

import json
import logging
import os
import hashlib
import random
import time
from pathlib import Path
from typing import Optional

import httpx

from app.paths import STICKER_DIR, ensure_dir
from app.util.blocking import run_blocking

logger = logging.getLogger(__name__)


def _get_sticker_dir() -> Path:
    return ensure_dir(STICKER_DIR)


class StickerManager:
    """表情包管理器 —— 收藏 / 查询 / 随机"""

    MAX_SIZE = 512 * 1024  # 最大 512KB（sticker 一般是小 gif/png）
    ACCEPTED_EXTS = {".gif", ".png", ".jpg", ".jpeg", ".webp"}

    def __init__(self):
        self._dir = _get_sticker_dir()
        self._index_path = self._dir / "index.json"
        self._stickers: list[dict] = self._load_index()

    def _load_index(self) -> list[dict]:
        if self._index_path.exists():
            try:
                data = json.loads(self._index_path.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    return data
            except Exception as e:
                logger.warning(f"[Sticker] index.json 损坏，重建: {e}")
        return []

    def _save_index(self):
        try:
            self._index_path.write_text(
                json.dumps(self._stickers, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.error(f"[Sticker] 保存 index 失败: {e}")

    # ---------- 收藏（下载 + 入库）----------

    async def collect(
        self,
        sticker_info: dict,
        group_id: int,
        sender_id: int,
        sender_nick: str = "",
    ) -> Optional[str]:
        """
        下载并收藏一个自定义表情。
        返回: 本地文件路径（成功）/ None（失败或重复）
        """
        url = sticker_info.get("url", "")
        file_field = sticker_info.get("file", "")
        st_id = sticker_info.get("id", "")

        # 去重 ①：按 sticker_id + 群（快速）
        if st_id and any(s.get("id") == st_id and s.get("group_id") == group_id for s in self._stickers):
            logger.debug(f"[Sticker] 跳过重复 sticker_id={st_id}")
            return None  # 已收藏

        # 注：真正的 MD5 去重在下载拿到数据后做（见下方）

        # 确定后缀
        suffix = ""
        if file_field:
            _, ext = os.path.splitext(file_field)
            if ext.lower() in self.ACCEPTED_EXTS:
                suffix = ext.lower()
        if not suffix and url:
            # 从 URL 猜后缀
            url_path = url.split("?")[0]
            _, ext = os.path.splitext(url_path)
            if ext.lower() in self.ACCEPTED_EXTS:
                suffix = ext.lower()
        if not suffix:
            suffix = ".gif"  # 兜底

        # 决定下载方式
        data = None
        if url:
            data = await self._download(url)
        elif file_field:
            # NapCat 有时给本地路径（Windows 下），直接读
            local = file_field.replace("\\", "/")
            data = await run_blocking(self._read_local_file, local)

        if not data:
            logger.debug(f"[Sticker] 无法下载/读取: id={st_id} url={url[:60]} file={file_field}")
            return None

        if len(data) > self.MAX_SIZE:
            logger.info(f"[Sticker] 跳过过大文件 ({len(data)}B)")
            return None

        # 去重 + 写图片 + 更新索引 一起丢进线程：这三步必须原子，
        # 而且 write_bytes/index 写盘都是阻塞的，不能放在事件循环里。
        return await run_blocking(
            self._commit, data, suffix, st_id, group_id, sender_id, sender_nick
        )

    def _commit(
        self,
        data: bytes,
        suffix: str,
        st_id: str,
        group_id: int,
        sender_id: int,
        sender_nick: str,
    ) -> Optional[str]:
        """
        在阻塞线程里完成落盘：MD5 去重 → 写图片文件 → 入库 + 写索引。
        全部磁盘操作由 app.util.blocking 的单 worker 串行执行，
        所以这里不需要额外加锁（MD5 去重也就不会有竞态）。
        """
        # 去重：MD5 内容哈希 —— 即使 file_id 不一样，同一张图也只存一份！
        md5 = hashlib.md5(data).hexdigest()
        if any(s.get("md5") == md5 for s in self._stickers):
            logger.info(f"[Sticker] 跳过重复内容 md5={md5[:8]}...")
            return None

        filename = f"stk_{int(time.time())}_{random.randint(1000, 9999)}{suffix}"
        filepath = self._dir / filename
        try:
            filepath.write_bytes(data)
        except Exception as e:
            logger.error(f"[Sticker] 保存失败: {e}")
            return None

        self._stickers.append({
            "id": st_id,
            "md5": md5,  # 内容哈希，用于去重
            "filename": filename,
            "group_id": group_id,
            "sender_id": sender_id,
            "sender_nick": sender_nick,
            "collected_at": int(time.time()),
            "size": len(data),
        })
        self._save_index()
        logger.info(f"[Sticker] 收藏成功: {filename} ({len(data)}B) from {sender_nick}@{group_id}")
        return str(filepath.resolve())

    @staticmethod
    def _read_local_file(path: str) -> Optional[bytes]:
        """读本地表情文件（阻塞，供 run_blocking 调用）"""
        if not os.path.exists(path):
            return None
        try:
            with open(path, "rb") as f:
                return f.read()
        except Exception:
            return None

    async def _download(self, url: str) -> Optional[bytes]:
        try:
            async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
                resp = await client.get(url)
                if resp.status_code == 200:
                    return resp.content
        except Exception as e:
            logger.debug(f"[Sticker] 下载失败: {e}")
        return None

    # ---------- 查询 ----------

    def count(self) -> int:
        return len(self._stickers)

    def all(self) -> list[dict]:
        """所有已收藏的表情包元数据"""
        return list(self._stickers)

    def random(self, group_id: Optional[int] = None, limit: int = 1, prefer_same_group: bool = False) -> list[dict]:
        """
        随机挑几个表情包。默认从全池随机挑（跨群也能用，OneBot 协议里 file:/// 是本地路径无所谓群）。
        只有 prefer_same_group=True 时才优先挑本群收藏的。
        """
        # 默认全池优先；只有显式指定 prefer_same_group 才按群过滤
        pool = self._stickers
        if prefer_same_group and group_id is not None:
            same = [s for s in pool if s.get("group_id") == group_id]
            if same:
                pool = same  # 本群有收藏才用它，否则回退全池

        if not pool:
            return []

        # 放宽 sample 倍数，让更多候选参与，减少"文件丢失导致结果少"的概率
        picks = random.sample(pool, min(limit * 3, len(pool)))
        results = []
        seen_filenames = set()
        for p in picks:
            if len(results) >= limit:
                break
            fname = p.get("filename", "")
            if fname in seen_filenames:
                continue
            path = self._dir / fname
            if path.exists():
                seen_filenames.add(fname)
                results.append({
                    "filename": fname,
                    "path": str(path.resolve()),
                    "sender_nick": p.get("sender_nick", ""),
                    "sender_id": p.get("sender_id"),
                    "source_group_id": p.get("group_id"),  # 额外返回来源群，方便日志调试
                })

        # 挑不够 → 从全池兜底（之前 pool 里可能有坏文件）
        if len(results) < limit:
            fallback = [s for s in self._stickers if s.get("filename") not in seen_filenames]
            if fallback:
                more = random.sample(fallback, min(limit - len(results), len(fallback)))
                for p in more:
                    if len(results) >= limit:
                        break
                    fname = p.get("filename", "")
                    path = self._dir / fname
                    if path.exists():
                        results.append({
                            "filename": fname,
                            "path": str(path.resolve()),
                            "sender_nick": p.get("sender_nick", ""),
                            "sender_id": p.get("sender_id"),
                            "source_group_id": p.get("group_id"),
                        })
        return results

    # ---------- 构造 CQ 发送码 ----------

    @staticmethod
    def make_cq_image(local_path: str) -> str:
        """把本地文件路径转成 OneBot CQ:image 码"""
        p = local_path.replace("\\", "/")
        if not p.startswith("/"):
            p = "/" + p
        return f"[CQ:image,file=file://{p}]"
