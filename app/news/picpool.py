"""
图池（P2c-图池）：正文图入池 + 严格 FIFO 出图 + 上限淘汰
=======================================================

**解决什么问题**：以前每篇专栏只抓 1 张头图，"测试几次就把池子消耗完"。
现在把专栏**正文图**（一篇 6~12 张）收进本地池子，按入库顺序逐张发出。

四件事：

1. **补货 `harvest()`**：从图鉴型 UP（`NEWS_GALLERY_MIDS`）的最新专栏里挑**没抓过**的，
   抓一篇（默认一天一篇，天然低频 —— B站 -509 风控很凶，必须慢），图片入池。
   **抓过的 cv 号永久记账**；接口失败（-509/网络）**不记账**，下次还会试。
2. **出图 `next_picture()`**：**严格 FIFO** —— 按入库顺序发，发过的标记 `used_at`，
   全部发完才从头循环（符合"按顺序输出"的要求）。
3. **保底**：抓取失败**绝不动池子**；池子空了由调用方回落"资讯封面图"。
4. **上限 `evict()`**：超过 `PIC_POOL_LIMIT` 张时，优先淘汰**最旧的、已发过的**。

存储：

```
data/news/picpool.json     # 清单（items / next 指针 / 已抓 cv / 统计）
data/news/pics/<hash>.jpg  # 图片本体（文件名 = 缩放后 URL 的 hash，重复下载天然去重）
```

为什么清单与图片分开：清单小、可读、可手改；图片按内容寻址，重抓不会重复占空间。
"""

from __future__ import annotations

import json
import logging
import os
import random
import threading
import time
from pathlib import Path
from typing import Awaitable, Callable, Optional

from app.config import Config
from app.news.article import fetch_article_images, resized_url
from app.news.image import download_image
from app.paths import PICPOOL_DIR, PICPOOL_MANIFEST, ensure_dir

logger = logging.getLogger(__name__)

_MAX_HARVESTED = 300      # 已抓过的 cv 号最多记这么多（够用一年，防清单无限增长）


class PicPool:
    """正文图池（FIFO 出图 + 补货 + 淘汰）"""

    def __init__(self, manifest: Optional[Path] = None, pic_dir: Optional[Path] = None,
                 downloader: Optional[Callable[[str], Awaitable[Optional[Path]]]] = None,
                 fetcher: Optional[Callable[[str], Awaitable[tuple]]] = None):
        """`downloader` / `fetcher` 可注入（测试不联网）"""
        self._manifest = Path(manifest or PICPOOL_MANIFEST)
        self._dir = ensure_dir(pic_dir or PICPOOL_DIR)
        ensure_dir(self._manifest.parent)
        # ⚠️ 默认下载器**必须存进池子目录**（`self._dir`）。否则图落到全局
        # `data/news_images/`，而 `next_picture()` 去池子目录找 → 全部找不到 → 永远出不了图。
        # （2026-10-19 真机验证时踩到：单元测试注入了自定义 downloader，所以测不出来。）
        self._download = downloader or (
            lambda url: download_image(url, dest_dir=self._dir,
                                       referer="https://www.bilibili.com/"))
        self._fetch = fetcher or fetch_article_images
        self._lock = threading.RLock()
        self._data = self._blank()
        self._loaded = False

    @staticmethod
    def _blank() -> dict:
        return {
            "version": 1,
            "items": [],          # [{id,file,url,w,h,size_kb,cid,title,artist,added_at,used_at}]
            "next": 0,            # FIFO 指针：下一张要发的下标
            "harvested": {},      # cv 号 → 抓取时间（抓过就不再抓）
            "harvest_by_source": {},   # 源 → 已抓篇数（来源轮换用，避免高产源饿死别人）
            "last_source_at": {},      # 源 → 最后抓取时间
            "last_harvest": 0.0,
            "last_error": "",
        }

    # ---------- 持久化 ----------

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if not self._manifest.exists():
            return
        try:
            raw = json.loads(self._manifest.read_text(encoding="utf-8"))
            data = self._blank()
            data.update({k: v for k, v in (raw or {}).items() if k in data})
            self._data = data
        except Exception as e:
            logger.warning(f"[PicPool] 清单读取失败（从空开始，图本体不动）: {e}")

    def save(self) -> None:
        self._load()
        payload = dict(self._data)
        # 已抓 cv 号只留最近 N 个，避免清单无限长
        if len(payload.get("harvested") or {}) > _MAX_HARVESTED:
            items = sorted((payload["harvested"]).items(), key=lambda kv: kv[1])[-_MAX_HARVESTED:]
            payload["harvested"] = dict(items)
        try:
            tmp = self._manifest.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self._manifest)
        except OSError as e:
            logger.warning(f"[PicPool] 清单写入失败: {e}")

    # ---------- 补货 ----------

    def candidates(self, articles: list) -> list:
        """从资讯条目里挑出图鉴 UP 的未抓专栏，**按源公平轮换**排序。

        【为什么必须轮换】2026-10-19 用户实测：只抓到萌村百科，另一个 UP 一张都没有。
        原因是资讯条目**按源分组**（`PRESET_BILI` 里萌村百科在前），而 `harvest(limit=1)`
        永远取第一个 → 萌村百科日更、每天都有新 cv → **另一个 UP 被永久饿死**。
        现在：按"该源已抓过几篇"升序排（并列时看最后抓取时间、再看配置顺序），
        于是「萌村百科 → 难改狐言 → 萌村百科 → …」交替。
        """
        self._load()
        gallery = [
            f"bili_{m.strip()}"
            for m in (Config.NEWS_GALLERY_MIDS or "").replace("，", ",").split(",")
            if m.strip().isdigit()
        ]
        gallery_set = set(gallery)
        # 每个源挑最新的未抓条目（保持源内时间倒序）
        by_source: dict[str, list] = {}
        seen: set[str] = set()
        for it in articles or []:
            sid = getattr(it, "source_id", "")
            if sid not in gallery_set:
                continue
            cid = str(getattr(it, "link", "")).rsplit("cv", 1)[-1]
            if not cid.isdigit() or cid in seen or cid in (self._data["harvested"] or {}):
                continue
            seen.add(cid)
            by_source.setdefault(sid, []).append((cid, getattr(it, "title", ""), sid))

        counts = self._data.get("harvest_by_source") or {}
        last = self._data.get("last_source_at") or {}
        order = {sid: i for i, sid in enumerate(gallery)}
        out: list = []
        # 轮转：每一轮每个源出一个候选，源之间的顺序由"抓得少的优先"决定
        while any(by_source.values()):
            ready = [sid for sid, lst in by_source.items() if lst]
            ready.sort(key=lambda sid: (
                int(counts.get(sid, 0)),                  # 抓得最少的优先
                float(last.get(sid, 0)),                  # 其次是最久没抓的
                order.get(sid, 99),                       # 再次按配置顺序
            ))
            for sid in ready:
                out.append(by_source[sid].pop(0))
        return out

    async def harvest(self, articles: list, *, limit: int = 1) -> dict:
        """抓 `limit` 篇**没抓过**的专栏并入库；返回 {ok, added, articles, error}"""
        self._load()
        limit = max(1, int(limit or 1))
        picks = self.candidates(articles)[:limit]
        if not picks:
            return {"ok": True, "added": 0, "articles": 0, "error": "", "reason": "没有新专栏可抓"}

        added, done_articles = 0, 0
        failed_error = ""
        for cid, title, sid in picks:
            imgs, meta = await self._fetch(cid)
            if not meta.get("ok"):
                # ⚠️ 接口失败（-509/网络）→ **不记账**，下次还会试；池子一张都不动
                failed_error = str(meta.get("error") or "抓取失败")
                self._data["last_error"] = failed_error
                logger.warning(f"[PicPool] cv{cid} 抓取失败（池子保持不变）: {failed_error}")
                break
            title = meta.get("title") or title
            artist = meta.get("artist") or ""
            n = 0
            for img in imgs:
                try:
                    path = await self._download(img.url)
                except Exception as e:
                    logger.warning(f"[PicPool] 下载失败 {img.url[:60]}: {e}")
                    continue
                if not path:
                    continue
                self._append(path, img, cid=cid, title=title,
                             artist=getattr(img, "artist", "") or artist)
                n += 1
            self._data["harvested"][cid] = time.time()
            # 记"哪个源抓了几篇"：来源轮换据此让抓得少的源优先（否则高产源会饿死别人）
            self._data.setdefault("harvest_by_source", {})
            self._data["harvest_by_source"][sid] = int(
                self._data["harvest_by_source"].get(sid, 0)) + 1
            self._data.setdefault("last_source_at", {})
            self._data["last_source_at"][sid] = time.time()
            done_articles += 1
            added += n
            logger.info(f"[PicPool] cv{cid} 入库 {n} 张（{title[:24]!r} / {sid}）")
            if n == 0:
                # 一篇没有可用大图也算"抓过"，避免天天重复试
                logger.info(f"[PicPool] cv{cid} 没有可用大图（已记账，不再重试）")

        self._data["last_harvest"] = time.time()
        # 全部失败 → ok=False（让任务如实报"补货失败"而不是"没有新增"）；
        # 部分成功（先抓到一篇、后一篇失败）→ 仍算成功，但把错误留在 last_error 里
        ok = not (failed_error and added == 0)
        if ok:
            self._data["last_error"] = ""
        self.evict()
        self.save()
        return {"ok": ok, "added": added, "articles": done_articles, "error": failed_error}

    def _append(self, path: Path, img, *, cid: str, title: str, artist: str) -> None:
        url = img.url
        pid = os.path.basename(str(path)).split(".")[0]
        with self._lock:
            if any(i["id"] == pid for i in self._data["items"]):
                return                       # 同一张图（同 URL）只入池一次
            self._data["items"].append({
                "id": pid, "file": os.path.basename(str(path)), "url": url,
                "w": img.width, "h": img.height, "size_kb": round(img.size_kb, 1),
                "cid": cid, "title": title[:80], "artist": artist[:40],
                "added_at": time.time(), "used_at": 0.0,
            })

    # ---------- 出图（随机 / FIFO 两种模式） ----------

    def _existing_items(self) -> list:
        return [i for i in self._data["items"] if (self._dir / i["file"]).exists()]

    def _reset_cycle_if_exhausted(self, items: list) -> None:
        """本轮全部发过 → 清零 used 标记，开始新一轮"""
        if items and all(i.get("used_at") for i in items):
            for it in items:
                it["used_at"] = 0.0
            self._data["cycle"] = int(self._data.get("cycle") or 0) + 1
            logger.info(f"[PicPool] 第 {self._data['cycle']} 轮开始（{len(items)} 张可发）")
            self.save()

    def _mark_sent(self, it: dict, *, advance: bool = False, n: int = 0) -> dict:
        it["used_at"] = time.time()
        if advance and n:
            idx = self._data["items"].index(it)
            self._data["next"] = (idx + 1) % n
        # 记"刚发过哪篇专栏"，随机模式据此避开连着同主题
        recent = self._data.setdefault("recent_cids", [])
        cid = str(it.get("cid") or "")
        if cid:
            recent.append(cid)
            del recent[:-6]
        self.save()
        return {**it, "path": str(self._dir / it["file"])}

    def next_picture(self) -> Optional[dict]:
        """取下一张（默认**随机**，可选 FIFO）

        **为什么默认随机**（2026-10-19 用户要求）：一篇专栏的 6~12 张是**同一角色/同一主题**
        （如全是《星穹铁道》托帕），FIFO 会连着好几天发同一个人。随机除此之外还有两条约束：

        - **一轮内不重复**：只从 `used_at == 0` 的图里抽，全部发过才清零重来；
        - **避开上一篇专栏**：不选与最近 `PIC_POOL_AVOID_CIDS` 次相同 `cid` 的图
          （池子里只剩同一篇时才退让，避免卡死）。

        `PIC_POOL_ORDER=fifo` 可切回严格顺序（发完一轮才循环）。
        """
        self._load()
        with self._lock:
            items = self._existing_items()
            if not items:
                return None
            order = str(Config.PIC_POOL_ORDER or "random").strip().lower()
            if order == "fifo":
                return self._next_fifo(items)

            # ---- 随机模式 ----
            unused = [i for i in items if not i.get("used_at")]
            if not unused:
                self._reset_cycle_if_exhausted(items)
                unused = [i for i in items if not i.get("used_at")] or items
            avoid_n = max(0, int(Config.PIC_POOL_AVOID_CIDS or 0))
            avoid = set((self._data.get("recent_cids") or [])[-avoid_n:]) if avoid_n else set()
            pool = [i for i in unused if str(i.get("cid") or "") not in avoid] or unused
            pick = random.choice(pool)
            if avoid and str(pick.get("cid") or "") in avoid:
                logger.debug("[PicPool] 池内只剩同一篇专栏的图 → 本次退让，不避开主题")
            out = self._mark_sent(pick)
            st = self.stats()
            logger.info(f"[PicPool] 随机出图 {pick['file']}"
                        f"（cv{pick.get('cid')} 本轮 {st['used']}/{len(items)}，剩 {st['remaining']}）")
            return out

    def _next_fifo(self, items: list) -> Optional[dict]:
        """严格 FIFO：按入库顺序发，一轮发完自动从头循环"""
        n = len(items)
        self._reset_cycle_if_exhausted(items)
        start = int(self._data.get("next") or 0) % n
        for step in range(n):
            it = items[(start + step) % n]
            if it.get("used_at"):
                continue                       # 本轮发过 → 跳过
            out = self._mark_sent(it, advance=True, n=n)
            st = self.stats()
            logger.info(f"[PicPool] FIFO 出图 {it['file']}"
                        f"（本轮 {st['used']}/{n}，剩 {st['remaining']}）")
            return out
        return None

    def peek_next(self) -> Optional[dict]:
        """只看下一张（干跑用）：不标记、不移动指针；随机模式给一个同样规则的候选"""
        self._load()
        items = self._existing_items()
        if not items:
            return None
        order = str(Config.PIC_POOL_ORDER or "random").strip().lower()
        if order == "fifo":
            n = len(items)
            start = int(self._data.get("next") or 0) % n
            for step in range(n):
                it = items[(start + step) % n]
                if not it.get("used_at"):
                    return {**it, "path": str(self._dir / it["file"])}
            it = items[0]
            return {**it, "path": str(self._dir / it["file"])}
        # 随机：用同样的"未发过 + 避开最近专栏"规则给一个候选（不落盘）
        unused = [i for i in items if not i.get("used_at")] or items
        avoid_n = max(0, int(Config.PIC_POOL_AVOID_CIDS or 0))
        avoid = set((self._data.get("recent_cids") or [])[-avoid_n:]) if avoid_n else set()
        pool = [i for i in unused if str(i.get("cid") or "") not in avoid] or unused
        it = random.choice(pool)
        return {**it, "path": str(self._dir / it["file"])}

    # ---------- 上限淘汰 ----------

    def evict(self) -> int:
        """超过 `PIC_POOL_LIMIT` 时淘汰最旧的**已用**图（没用过的尽量留着）"""
        self._load()
        limit = int(Config.PIC_POOL_LIMIT or 0)
        if limit <= 0:
            return 0
        items = self._data["items"]
        if len(items) <= limit:
            return 0
        with self._lock:
            excess = len(items) - limit
            used = sorted([i for i in items if i.get("used_at")], key=lambda i: i["added_at"])
            removed = 0
            for it in used[:excess]:
                self._remove_item(it)
                removed += 1
            # 已用的删完还不够 → 再删最旧的未使用图
            if removed < excess:
                rest = sorted([i for i in self._data["items"]], key=lambda i: i["added_at"])
                for it in rest[:excess - removed]:
                    self._remove_item(it)
                    removed += 1
            if removed:
                self._data["next"] = 0
                logger.info(f"[PicPool] 超过上限 {limit}，淘汰 {removed} 张最旧图")
        return removed

    def _remove_item(self, it: dict) -> None:
        try:
            (self._dir / it["file"]).unlink(missing_ok=True)
        except OSError:
            pass
        try:
            self._data["items"].remove(it)
        except ValueError:
            pass

    # ---------- 查询 ----------

    def stats(self) -> dict:
        self._load()
        items = self._data["items"]
        used = sum(1 for i in items if i.get("used_at"))
        disk = 0
        for i in items:
            try:
                disk += (self._dir / i["file"]).stat().st_size
            except OSError:
                pass
        return {
            "total": len(items),
            "used": used,
            "remaining": len(items) - used,
            "limit": int(Config.PIC_POOL_LIMIT or 0),
            "disk_mb": round(disk / 1048576, 1),
            "harvested": len(self._data.get("harvested") or {}),
            "last_harvest": self._data.get("last_harvest") or 0,
            "last_error": self._data.get("last_error") or "",
            "next": int(self._data.get("next") or 0),
            "order": str(Config.PIC_POOL_ORDER or "random").lower(),
            "cycle": int(self._data.get("cycle") or 0),
        }
