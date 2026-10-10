"""
外号管理器 —— 群成员给自己的 QQ 号绑定自定义外号
=================================================

存储结构（按群隔离）:
  data/aliases/
    100000005.json
    987654321.json

单个群文件结构:
  {
    "<QQ号>": {
      "nick": "小明",              # 昵称快照（仅展示用，不参与匹配）
      "aliases": ["明明", "小明明"], # 外号列表
      "updated_at": 1748123456     # 最后修改时间
    }
  }

为什么键是 QQ 而不是昵称？
  QQ 号是稳定唯一标识；昵称/群名片随时会变，不能当主键。

内存缓存 + 写时落盘，模仿 StickerManager 的做法。
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Optional

from app.paths import ALIAS_DIR, ensure_dir

logger = logging.getLogger(__name__)

# 外号允许的字符：中日文、英文字母、数字（不含空格/标点/方括号，防止破坏 CQ 码）
_ALIAS_RE = re.compile(r"^[\u4e00-\u9fff\u3040-\u30ffA-Za-z0-9]+$")

ALIAS_MIN_LEN = 2
ALIAS_MAX_LEN = 8


def _get_alias_dir() -> Path:
    # 绝对路径（项目根/data/aliases），不跟进程工作目录走
    return ensure_dir(ALIAS_DIR)


class AliasManager:
    """按群隔离的外号管理器"""

    def __init__(self):
        self._dir = _get_alias_dir()
        # group_id -> {qq_str: {"nick": str, "aliases": [str], "updated_at": int}}
        self._groups: dict[int, dict[str, dict]] = {}
        # 只保护内存字典，不跨文件 I/O 持锁：
        # 外号表的读操作可能发生在事件循环线程（_build_system_prompt），
        # 而写操作会被丢进 app.util.blocking 的 worker 线程，
        # 若不加锁，两个线程同时给同一个 dict 插入 key 会撞坏字典。
        # 文件读写由单 worker 串行化，不在这里管。
        self._lock = threading.RLock()

    # ---------- 持久化 ----------

    def _path(self, group_id: int) -> Path:
        return self._dir / f"{group_id}.json"

    def _load(self, group_id: int) -> dict[str, dict]:
        """加载某个群的外号表（带内存缓存）。阻塞；异步侧请用 run_blocking。"""
        with self._lock:
            cached = self._groups.get(group_id)
        if cached is not None:
            return cached

        data: dict[str, dict] = {}
        p = self._path(group_id)
        if p.exists():  # 磁盘 I/O 放在锁外
            try:
                raw = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    for qq, info in raw.items():
                        if not isinstance(info, dict):
                            continue
                        aliases = [a for a in info.get("aliases", []) if isinstance(a, str) and a]
                        if aliases:
                            data[str(qq)] = {
                                "nick": info.get("nick", ""),
                                "aliases": aliases,
                                "updated_at": info.get("updated_at", 0),
                            }
            except Exception as e:
                logger.warning(f"[Alias] group={group_id} 文件损坏，忽略: {e}")

        with self._lock:
            existing = self._groups.get(group_id)  # 另一个线程可能已加载好
            if existing is not None:
                return existing
            self._groups[group_id] = data
            return data

    def _save(self, group_id: int):
        p = self._path(group_id)
        data = self._groups.get(group_id, {})
        try:
            p.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.error(f"[Alias] group={group_id} 保存失败: {e}")

    # ---------- 只读查询 ----------

    def _items_snapshot(self, group_id: int) -> list[tuple[str, dict]]:
        """群外号表的快照 —— 迭代前必须先快照，否则可能和写入线程撞上"""
        data = self._load(group_id)
        with self._lock:
            return list(data.items())

    def list_aliases(self, group_id: int, qq) -> list[str]:
        """查某人的外号列表"""
        info = self._load(group_id).get(str(qq))
        return list(info["aliases"]) if info else []

    def all_of_group(self, group_id: int) -> dict[str, dict]:
        """返回整群外号表的副本"""
        data = self._load(group_id)
        with self._lock:
            return {k: dict(v) for k, v in data.items()}

    def find_qq_by_alias(self, group_id: int, alias: str) -> Optional[str]:
        """按外号反查 QQ（外号在群内唯一，先到先得）"""
        alias = alias.strip()
        if not alias:
            return None
        for qq, info in self._items_snapshot(group_id):
            if alias in info["aliases"]:
                return qq
        return None

    def get_prompt_view(self, group_id: int, limit: int = 15) -> list[dict]:
        """
        给 system prompt 用的精简视图：只返回有外号的人，最多 limit 个。
        形如 [{"nick": "小明", "aliases": ["明明"]}, ...]
        """
        out: list[dict] = []
        for qq, info in self._items_snapshot(group_id):
            if not info["aliases"]:
                continue
            out.append({"nick": info.get("nick") or qq, "aliases": info["aliases"]})
            if len(out) >= limit:
                break
        return out

    # ---------- 校验 ----------

    def _check_alias_format(self, alias: str) -> Optional[str]:
        """格式校验，返回错误提示；通过则返回 None"""
        if not alias:
            return "外号不能为空"
        if not (ALIAS_MIN_LEN <= len(alias) <= ALIAS_MAX_LEN):
            return f"外号长度要在 {ALIAS_MIN_LEN}~{ALIAS_MAX_LEN} 个字之间"
        if not _ALIAS_RE.match(alias):
            return "外号只能包含中英文和数字，不能有空格或标点"
        return None

    def validate(
        self,
        group_id: int,
        qq,
        alias: str,
        forbidden_names: Optional[set[str]] = None,
        bot_name: str = "",
        max_per_user: int = 3,
    ) -> Optional[str]:
        """
        校验一个外号能不能给这个 QQ 用。返回错误提示字符串；通过则返回 None。

        forbidden_names: 群内其他成员的昵称/群名片（排除自己），不允许重名
        bot_name:        机器人自己的名字，不允许重名
        """
        qq = str(qq)
        alias = alias.strip()

        err = self._check_alias_format(alias)
        if err:
            return err

        if bot_name and alias == bot_name:
            return f"「{alias}」是爱丽丝自己的名字，换一个吧～"

        if forbidden_names and alias in forbidden_names:
            return f"「{alias}」和群里某个成员的昵称重了，换一个吧～"

        # 是否被其他 QQ 占用（先到先得）
        owner = self.find_qq_by_alias(group_id, alias)
        if owner and owner != qq:
            return f"「{alias}」已经被别人用啦，换一个吧～"
        if owner == qq:
            return f"你已经在用「{alias}」这个外号啦"

        mine = self.list_aliases(group_id, qq)
        if len(mine) >= max_per_user:
            return f"每人最多设 {max_per_user} 个外号，先删一个再加吧～"

        return None

    # ---------- 写操作 ----------

    def add(
        self,
        group_id: int,
        qq,
        alias: str,
        nick: str = "",
        forbidden_names: Optional[set[str]] = None,
        bot_name: str = "",
        max_per_user: int = 3,
    ) -> tuple[bool, str]:
        """添加外号。返回 (是否成功, 提示文本)。阻塞；异步侧请用 run_blocking 调用。"""
        alias = (alias or "").strip()
        err = self.validate(
            group_id, qq, alias,
            forbidden_names=forbidden_names,
            bot_name=bot_name,
            max_per_user=max_per_user,
        )
        if err:
            return False, err

        data = self._load(group_id)
        qq = str(qq)
        with self._lock:
            info = data.setdefault(qq, {"nick": nick, "aliases": [], "updated_at": 0})
            info["aliases"].append(alias)
            if nick:
                info["nick"] = nick
            info["updated_at"] = int(time.time())
        self._save(group_id)  # 磁盘 I/O 放在锁外
        logger.info(f"[Alias] group={group_id} qq={qq} 新增外号 '{alias}'")
        return True, f"好嘞，以后我就叫你「{alias}」啦～"

    def remove(self, group_id: int, qq, alias: str = "") -> tuple[bool, str]:
        """
        删除外号。alias 为空则清空该人的所有外号。
        返回 (是否成功, 提示文本)。阻塞；异步侧请用 run_blocking 调用。
        """
        data = self._load(group_id)
        qq = str(qq)
        info = data.get(qq)
        if not info or not info["aliases"]:
            return False, "你还没设过外号呢～"

        alias = (alias or "").strip()
        if not alias:
            with self._lock:
                removed = list(info["aliases"])
                info["aliases"] = []
                info["updated_at"] = int(time.time())
            self._save(group_id)
            logger.info(f"[Alias] group={group_id} qq={qq} 清空外号 {removed}")
            return True, "好，以后不叫外号了，就叫你原来的昵称吧～"

        if alias not in info["aliases"]:
            return False, f"「{alias}」不在你的外号列表里哦"
        with self._lock:
            info["aliases"].remove(alias)
            info["updated_at"] = int(time.time())
        self._save(group_id)
        logger.info(f"[Alias] group={group_id} qq={qq} 删除外号 '{alias}'")
        return True, f"好，以后不叫你「{alias}」了～"

    # ---------- 供 _auto_mention 用：外号 → QQ 直连 ----------

    def alias_to_qq(self, group_id: int) -> dict[str, str]:
        """返回 {外号: QQ}，供自动 @ 使用（外号在群内唯一）。阻塞；异步侧请用 run_blocking。"""
        out: dict[str, str] = {}
        for qq, info in self._items_snapshot(group_id):
            for a in info["aliases"]:
                out[a] = qq
        return out