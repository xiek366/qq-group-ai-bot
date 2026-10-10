"""
OneBot 11 消息解析
--------------------
OneBot 的 message 是一个 segment 数组，例如：
  [
    {"type": "at",   "data": {"qq": "123456"}},
    {"type": "text", "data": {"text": "你好"}},
    {"type": "face", "data": {"id": "14"}}
  ]

本模块负责：
  1. 提取纯文本
  2. 判断是否 @机器人
  3. QQ 内置表情 → 中文含义（从 config/face_dict.json 读取）
  4. 统一成一个方便使用的 dict
"""

import json
from typing import Optional

from app.paths import FACE_DICT_PATH

# 懒加载 face 字典
_FACE_DICT: Optional[dict] = None


def _get_face_dict() -> dict:
    """加载 config/face_dict.json，返回 {id: ["含义1", "含义2"]}"""
    global _FACE_DICT
    if _FACE_DICT is not None:
        return _FACE_DICT
    try:
        dict_path = FACE_DICT_PATH
        if dict_path.exists():
            with open(dict_path, encoding="utf-8") as f:
                raw = json.load(f)
            _FACE_DICT = {k: v[0] for k, v in raw.items() if not k.startswith("_")}
        else:
            _FACE_DICT = {}
    except Exception:
        _FACE_DICT = {}
    return _FACE_DICT


def preload_face_dict() -> int:
    """预热表情字典，返回条数。

    字典是懒加载的，不预热的话**第一条群消息**会在事件循环里同步读盘。
    启动阶段（还没有任何任务在跑）调用它最划算。
    """
    return len(_get_face_dict())


def parse_message(event: dict) -> Optional[dict]:
    """
    把 OneBot 群消息事件解析成统一结构；返回 None 表示无法处理。

    返回示例:
    {
        "group_id": 123456,
        "user_id":  888888,
        "nickname": "小明",
        "message_id": 999,
        "raw_text": "@机器人 你好",
        "is_at_bot": True,
        "command": "/问",           # 如果以 / 开头
        "args": "什么是Transformer",
        "content": "你好",            # 去掉 @ 后的实际内容
    }
    """
    if event.get("post_type") != "message":
        return None
    if event.get("message_type") != "group":
        return None

    sender = event.get("sender", {})
    group_id = event.get("group_id")
    user_id = sender.get("user_id")
    nickname = sender.get("nickname", "")
    message_id = event.get("message_id")
    raw_msg = event.get("message", [])

    bot_qq = None
    try:
        from app.config import Config
        bot_qq = Config.BOT_QQ
    except Exception:
        pass

    # 把 message segment 数组转成纯文本
    is_at_bot = False
    text_parts = []
    at_mentions = []
    image_urls: list[str] = []  # 收集图片 URL 给视觉模型用
    sticker_infos: list[dict] = []  # 收集自定义表情信息给表情包库用

    for seg in raw_msg:
        if isinstance(seg, str):
            # 有些 OneBot 直接给纯字符串
            text_parts.append(seg)
            continue

        seg_type = seg.get("type")
        seg_data = seg.get("data", {})

        if seg_type == "text":
            text_parts.append(seg_data.get("text", ""))
        elif seg_type == "at":
            qq = seg_data.get("qq", "")
            display = seg_data.get("display", f"@{qq}")
            at_mentions.append(qq)
            # 检查是不是 @机器人
            if bot_qq and str(qq) == str(bot_qq):
                is_at_bot = True
            text_parts.append(f" {display} ")
        elif seg_type == "face":
            # QQ 内置表情 → 中文含义或 emoji
            face_id = str(seg_data.get("id", ""))
            text = seg_data.get("text", "")  # NapCat 有时会带 text 字段
            meaning = _get_face_dict().get(face_id, "")
            if meaning:
                text_parts.append(f"[{meaning}表情]")
            elif text:
                text_parts.append(text)  # 退而用 NapCat 提供的 text
            else:
                text_parts.append(f"[表情{face_id}]")
        elif seg_type == "reply":
            # 忽略 reply segment
            pass
        elif seg_type == "image":
            # NapCat 把动态表情包也包装成 type='image'，用 sub_type + summary 区分
            # sub_type=1 或 summary='[动画表情]' → 动态表情包（归 sticker_infos，不喂视觉模型）
            # sub_type=0 或 summary 为空 → 普通图片（归 image_urls）
            sub_type = seg_data.get("sub_type", 0)
            summary = seg_data.get("summary", "")
            is_animated = (sub_type == 1) or ("动画表情" in summary)

            img_url = seg_data.get("url", "")
            img_file = seg_data.get("file", "")
            img_id = seg_data.get("file", "")  # file 路径本身可当 id

            if is_animated:
                # 动态表情包 —— 归入 sticker 流程（下载收藏，不走视觉模型）
                sticker_infos.append({
                    "file": img_file,
                    "url": img_url,
                    "id": str(img_id)[:32] or f"img_{len(sticker_infos)}",
                })
                text_parts.append("[动画表情]")
            else:
                # 普通静态图片 —— 视觉模型看
                if img_url:
                    image_urls.append(img_url)
                text_parts.append("[图片]")
        elif seg_type == "sticker":
            # 自定义表情 —— 存 file/url 供收藏使用
            st_file = seg_data.get("file", "")
            st_url = seg_data.get("url", "")
            st_id = seg_data.get("id", "") or seg_data.get("sticker_id", "")
            sticker_infos.append({
                "file": st_file,
                "url": st_url,
                "id": str(st_id),
            })
            text_parts.append("[自定义表情]")
        elif seg_type == "record":
            text_parts.append("[语音]")
        elif seg_type == "json":
            # 卡片消息 / 小程序卡片 —— 占位（后续可以考虑解析 JSON 里的文本）
            text_parts.append("[卡片]")
        elif seg_type == "app":
            text_parts.append("[应用卡片]")
        else:
            # 其他未知 segment —— 跳过
            pass

    raw_text = " ".join(text_parts).strip()
    content = raw_text

    # 检测文本里是否 @ 了机器人（NapCat 有时不会把昵称 @ 解析成 CQ at 段）
    if not is_at_bot:
        bot_name = getattr(Config, "BOT_NAME", "")
        if bot_name and f"@{bot_name}" in raw_text:
            is_at_bot = True
        elif bot_qq and f"@{bot_qq}" in raw_text:
            is_at_bot = True

    # 去掉开头的 @机器人
    if is_at_bot:
        content = _strip_at_bot(content, bot_qq)

    # 识别命令
    command = None
    args = ""
    if content.startswith("/"):
        parts = content.split(maxsplit=1)
        command = parts[0].lower()
        args = parts[1] if len(parts) > 1 else ""

    return {
        "group_id": group_id,
        "user_id": user_id,
        "nickname": nickname,
        "message_id": message_id,
        "raw_text": raw_text,
        "is_at_bot": is_at_bot,
        "command": command,
        "args": args,
        "content": content,
        "image_urls": image_urls,  # 图片 URL 列表（给视觉模型用）
        "sticker_infos": sticker_infos,  # 自定义表情列表（给表情包库用）
        "_raw_msg": raw_msg,  # DEBUG: NapCat 给的原始 segment 数组
    }


def _strip_at_bot(text: str, bot_qq: Optional[str]) -> str:
    """从文本开头去掉 @机器人 的 mention"""
    from app.config import Config
    # 先移除 @BotName
    if Config.BOT_NAME:
        text = text.replace(f"@{Config.BOT_NAME}", "")
    if bot_qq:
        text = text.replace(f"@{bot_qq}", "")
    # 移除 NapCat 可能用的 [CQ:at,qq=xxx] 形式
    while True:
        idx = text.lower().find("[cq:at,")
        if idx < 0:
            break
        end = text.find("]", idx)
        if end < 0:
            break
        text = text[:idx] + text[end + 1:]
    return text.strip()
