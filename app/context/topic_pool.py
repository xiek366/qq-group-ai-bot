"""
话题库（P2c-7）
==============

Q4 的答复：**预置池子 + 按使用次数轮播**（不接 LLM 补库）。

为什么值得单独有个池子：破冰不能每天都靠 LLM 现编 —— 现编的内容质量波动大、
而且"这周问过什么"没有记忆。池子带来两件事：**类型可控**（怀旧/推荐/吐槽/冷知识/互动）
和**不会连着重复**（按使用次数选最少用过的，并避开本群最近用过的）。

状态落 `data/cache/topics.json`：全局使用次数 + 每群最近用过的 id。
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Optional

from app.paths import CACHE_DIR, ensure_dir

logger = logging.getLogger(__name__)

# 预置话题池：30 条，游戏/动漫向，都带"能接话"的钩子
PRESET_TOPICS: tuple[dict, ...] = (
    {"id": "t01", "type": "怀旧", "text": "你们最早玩的那款游戏是什么？还记得第一次打开它的感觉吗"},
    {"id": "t02", "type": "怀旧", "text": "有没有哪个游戏的通关存档你一直没舍得删？"},
    {"id": "t03", "type": "推荐", "text": "最近有没有什么游戏/番是你一口气刷完的？推一下呗"},
    {"id": "t04", "type": "推荐", "text": "如果只能推荐一部番给完全没看过动漫的人，你推哪部"},
    {"id": "t05", "type": "吐槽", "text": "哪个游戏的加载界面让你等到开始怀疑人生"},
    {"id": "t06", "type": "吐槽", "text": "最近一次被游戏气到摔手柄（或手机）是因为什么"},
    {"id": "t07", "type": "冷知识", "text": "你们知道游戏里的「存档点」最早是怎么来的吗"},
    {"id": "t08", "type": "冷知识", "text": "有没有哪个游戏的小细节是你玩了很久才发现、然后特别惊喜的"},
    {"id": "t09", "type": "互动", "text": "来玩个小的：说一个你游戏里的本命角色，我猜你喜欢的类型"},
    {"id": "t10", "type": "互动", "text": "如果你能进一个游戏世界生活一周，你选哪个"},
    {"id": "t11", "type": "日常", "text": "今天有没有摸鱼成功？我这边进度条刚过半"},
    {"id": "t12", "type": "日常", "text": "群里有没有最近在赶进度/赶作业的，报个到互相伤害一下"},
    {"id": "t13", "type": "怀旧", "text": "小时候有没有为了玩游戏偷偷熬过夜，被家长抓过没"},
    {"id": "t14", "type": "推荐", "text": "有没有那种「三分钟就能上手、但玩了三百小时」的游戏"},
    {"id": "t15", "type": "吐槽", "text": "哪个游戏的活动排期让你觉得策划完全没考虑过玩家的时间"},
    {"id": "t16", "type": "冷知识", "text": "游戏里那些「按了没用」的按钮，你们试过到底有没有彩蛋吗"},
    {"id": "t17", "type": "互动", "text": "要不我们来对个暗号：说一下你最近单曲循环的那首歌"},
    {"id": "t18", "type": "日常", "text": "今天天气怎么样？有没有人被天气搞到不想出门"},
    {"id": "t19", "type": "怀旧", "text": "你第一个网名是什么？现在想起来会不会脚趾抠地"},
    {"id": "t20", "type": "推荐", "text": "有没有什么冷门但很神的作品，一直想安利但没人听"},
    {"id": "t21", "type": "吐槽", "text": "哪个游戏的新手教程长到你想直接跳过"},
    {"id": "t22", "type": "冷知识", "text": "你们知道游戏里最常见的「空气墙」最早是为了什么存在的吗"},
    {"id": "t23", "type": "互动", "text": "来个选择题：单机还是联机？为什么"},
    {"id": "t24", "type": "日常", "text": "这个点还有人醒着吗？在忙什么"},
    {"id": "t25", "type": "怀旧", "text": "有没有哪个游戏的 BGM 现在一听就想起当年的自己"},
    {"id": "t26", "type": "推荐", "text": "最近有没有什么国创/国漫让你觉得「居然这么能打」"},
    {"id": "t27", "type": "吐槽", "text": "抽卡运气最差的一次是什么？说出来让大家开心一下"},
    {"id": "t28", "type": "冷知识", "text": "游戏里的「RNG」到底是不是真随机？你们信玄学吗"},
    {"id": "t29", "type": "互动", "text": "一句话形容你现在的状态，我用一个游戏术语回你"},
    {"id": "t30", "type": "日常", "text": "周末有人准备开黑吗？我这边可以先占个位"},
)

_RECENT_PER_GROUP = 3       # 本群最近用过的这几种排到后面，避免连着重复


# 每日一句的台词池（Q2：池子为主，LLM 偶尔原创）
# 都是"短句"级别：游戏/动漫台词与业界一句话，不搬运长段落（版权红线）
PRESET_QUOTES: tuple[dict, ...] = (
    {"id": "q01", "type": "游戏", "text": "「再来一次。」——失败从来不是终点，只是下一次的起手式。"},
    {"id": "q02", "type": "游戏", "text": "「存档已写入。」——今天做过的努力，都会留在进度里。"},
    {"id": "q03", "type": "游戏", "text": "「HP 不足，请及时休息。」——这句提示值得所有勇者当真。"},
    {"id": "q04", "type": "游戏", "text": "「这不是终点，只是下一张地图的入口。」"},
    {"id": "q05", "type": "游戏", "text": "「装备可以慢慢凑，队友要早点找。」"},
    {"id": "q06", "type": "游戏", "text": "「难度调低不丢人，卡关卡到弃坑才亏。」"},
    {"id": "q07", "type": "游戏", "text": "「支线任务做多了，主线反而不敢推。」——生活也是这样。"},
    {"id": "q08", "type": "游戏", "text": "「读档重来也需要勇气，因为你知道前面还是会难。」"},
    {"id": "q09", "type": "动漫", "text": "「把今天当成第一话来看，后面还有很长的剧情。」"},
    {"id": "q10", "type": "动漫", "text": "「主角光环的另一个名字，叫做一直没放弃。」"},
    {"id": "q11", "type": "动漫", "text": "「日常回也是正篇的一部分，别急着快进。」"},
    {"id": "q12", "type": "动漫", "text": "「第二季之所以好看，是因为第一季没有被弃番。」"},
    {"id": "q13", "type": "动漫", "text": "「声优换了没关系，故事还在往前走。」"},
    {"id": "q14", "type": "动漫", "text": "「没有人会在意你第几集入坑，喜欢就是喜欢。」"},
    {"id": "q15", "type": "业界", "text": "「好游戏不一定卖得好，但被玩过的人会记很久。」"},
    {"id": "q16", "type": "业界", "text": "「独立游戏的浪漫：预算有限，所以只能把想法做纯。」"},
    {"id": "q17", "type": "业界", "text": "「重制不是偷懒，是让新一代玩家也能走进同一扇门。」"},
    {"id": "q18", "type": "业界", "text": "「一个类型的复兴，往往从一款被人嘲笑的小作品开始。」"},
    {"id": "q19", "type": "业界", "text": "「愿每个开发者都能遇到愿意等他做完的玩家。」"},
    {"id": "q20", "type": "业界", "text": "「游戏史里最动人的从来不是技术，而是有人愿意把它做出来。」"},
)


class TopicPool:
    """话题池 + 轮播（全局使用次数 + 每群最近用过）"""

    def __init__(self, path: Optional[Path] = None, topics: Optional[list] = None):
        # ⚠️ 自定义路径时也要建父目录：本类被复用于"话题池"与"台词池"两个文件，
        # 只在默认分支建目录会让传了 path 的实例写盘失败（测试时抓到过）。
        self._path = path or (ensure_dir(CACHE_DIR) / "topics.json")
        ensure_dir(self._path.parent)
        self.topics = list(topics or PRESET_TOPICS)
        self._lock = threading.RLock()
        self._usage: dict[str, int] = {}
        self._recent: dict[str, list[str]] = {}
        self._loaded = False

    # ---------- 持久化 ----------

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            self._usage = dict(raw.get("usage") or {})
            self._recent = {str(k): list(v) for k, v in (raw.get("recent") or {}).items()}
        except Exception as e:
            logger.warning(f"[Topic] 话题库状态读取失败（从空开始）: {e}")

    def save(self) -> None:
        self._load()
        try:
            tmp = self._path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps({"usage": self._usage, "recent": self._recent},
                                      ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError as e:
            logger.warning(f"[Topic] 话题库状态写入失败: {e}")

    # ---------- 选与记 ----------

    def pick(self, group_id: int, kind: str = "") -> Optional[dict]:
        """选一条：优先"用得最少"，其次避开本群最近用过的；可按类型过滤"""
        self._load()
        pool = [t for t in self.topics if not kind or t.get("type") == kind] or list(self.topics)
        if not pool:
            return None
        with self._lock:
            recent = self._recent.get(str(group_id), [])
            # 排序键：最近用过的排最后 → 使用次数少的优先 → id 稳定
            def key(t: dict):
                return (t["id"] in recent, self._usage.get(t["id"], 0), t["id"])
            return sorted(pool, key=key)[0]

    def mark_used(self, group_id: int, topic_id: str) -> None:
        self._load()
        with self._lock:
            self._usage[topic_id] = self._usage.get(topic_id, 0) + 1
            lst = self._recent.setdefault(str(group_id), [])
            lst.append(topic_id)
            # 只留最近若干条（够避免重复即可）
            while len(lst) > _RECENT_PER_GROUP:
                lst.pop(0)
        self.save()

    def stats(self) -> dict:
        self._load()
        used = len([k for k, v in self._usage.items() if v])
        return {
            "total": len(self.topics),
            "used": used,
            "unused": len(self.topics) - used,
            "by_type": {t: len([x for x in self.topics if x.get("type") == t])
                        for t in sorted({x.get("type", "") for x in self.topics})},
        }
