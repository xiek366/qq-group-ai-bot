"""
OneBot 11 客户端
----------------
双路模式:
  - WebSocket  127.0.0.1:3001  收事件（群消息等）
  - HTTP API   127.0.0.1:3000  发消息（send_group_msg）

为什么分开？NapCat 正向 WS 对 action 响应支持不完整，
所以用 HTTP 发消息最稳，WS 只管收。
"""

import asyncio
import json
import logging
import time
from typing import Callable, Optional

import httpx

from app.config import Config

logger = logging.getLogger(__name__)


class OneBotClient:
    """OneBot 11 客户端 —— WS 收事件 + HTTP 发消息"""

    def __init__(self, on_group_message: Callable[[dict], None]):
        self.on_group_message = on_group_message  # async 回调
        self.ws = None
        self.ws_connected: bool = False   # 供面板读连接状态
        self.http = httpx.AsyncClient(timeout=Config.HTTP_TIMEOUT)
        self._running = False
        # 群成员缓存: group_id -> (timestamp, [{"user_id": int, "nickname": str, "card": str}])
        self._member_cache: dict[int, tuple[float, list[dict]]] = {}
        self._MEMBER_CACHE_TTL = 300  # 5 分钟

    # ---------- WebSocket 主循环（收事件）----------
    async def start(self):
        """启动 WebSocket 连接，断线自动重连"""
        self._running = True
        first_connect = True
        while self._running:
            try:
                await self._connect_and_loop()
                if first_connect:
                    logger.warning("[OneBot] 连接建立后立即断开")
                    first_connect = False
                self.ws_connected = False
                logger.info(f"[OneBot] 连接已关闭，{Config.WS_RECONNECT_INTERVAL}s 后重连...")
                await asyncio.sleep(Config.WS_RECONNECT_INTERVAL)
            except Exception as e:
                import traceback
                logger.error(f"[OneBot] 连接异常: {e}, {Config.WS_RECONNECT_INTERVAL}s 后重连...")
                logger.error(traceback.format_exc())  # 打完整 traceback
                await asyncio.sleep(Config.WS_RECONNECT_INTERVAL)

    async def stop(self):
        self._running = False
        if self.ws:
            await self.ws.close()
        await self.http.aclose()

    async def _connect_and_loop(self):
        import websockets  # 延迟导入

        headers = {}
        if Config.ONEBOT_ACCESS_TOKEN:
            headers["Authorization"] = f"Bearer {Config.ONEBOT_ACCESS_TOKEN}"

        logger.info(f"[OneBot] 正在连接 {Config.ONEBOT_WS_URL} ...")
        async with websockets.connect(
            Config.ONEBOT_WS_URL,
            additional_headers=headers,
            max_size=10 * 1024 * 1024,
        ) as ws:
            self.ws = ws
            self.ws_connected = True
            logger.info("[OneBot] ✅ WebSocket 已连接")

            async for raw in ws:
                try:
                    data = json.loads(raw)
                    await self._handle_event(data)
                except json.JSONDecodeError:
                    logger.warning(f"[OneBot] 收到非 JSON 消息: {raw[:200]}")
                except Exception as e:
                    import traceback
                    logger.error(f"[OneBot] 处理消息异常: {e}")
                    logger.error(traceback.format_exc())  # 打完整 traceback

    async def _handle_event(self, data: dict):
        """分发 OneBot 事件"""
        pt = data.get("post_type")
        if pt != "message":
            return

        msg_type = data.get("message_type")
        if msg_type == "group":
            await self.on_group_message(data)

    # ---------- HTTP 发送消息 ----------
    def _http_base_url(self) -> str:
        """从 WS URL 推导 HTTP base URL（端口 - 1）"""
        # ws://127.0.0.1:3001 → http://127.0.0.1:3000
        ws = Config.ONEBOT_WS_URL
        host_part = ws.replace("ws://", "http://").replace("wss://", "https://")
        # 拆端口 -1
        import re
        m = re.match(r"(https?://[^:]+):(\d+)(.*)", host_part)
        if m:
            host = m.group(1)
            port = int(m.group(2)) - 1  # HTTP 端口 = WS 端口 - 1
            rest = m.group(3)
            return f"{host}:{port}{rest}"
        return host_part

    async def send_group_message(self, group_id: int, text: str):
        """通过 OneBot HTTP API 发送群消息"""
        try:
            base = self._http_base_url()
            payload = {
                "group_id": group_id,
                "message": text,
                "auto_escape": False,
            }
            headers = {"Content-Type": "application/json"}
            if Config.ONEBOT_ACCESS_TOKEN:
                headers["Authorization"] = f"Bearer {Config.ONEBOT_ACCESS_TOKEN}"

            resp = await self.http.post(
                f"{base}/send_group_msg",
                json=payload,
                headers=headers,
            )
            if resp.status_code != 200:
                logger.error(f"[OneBot] 发送消息失败: HTTP {resp.status_code} {resp.text[:200]}")
            else:
                data = resp.json()
                if data.get("retcode") == 0:
                    logger.info(f"[OneBot] → 群 {group_id}: {text[:50]}")
                else:
                    logger.error(f"[OneBot] 发送消息失败: retcode={data.get('retcode')} msg={data.get('message')}")
        except httpx.TimeoutException:
            logger.error(f"[OneBot] 发送消息超时: 群 {group_id}")
        except Exception as e:
            logger.error(f"[OneBot] 发送消息异常: {e}")

    async def send_private_message(self, user_id, text: str) -> bool:
        """私聊某人（【P2a】干跑结果与任务状态走私聊，群里零打扰）

        返回是否成功 —— 调用方据此决定要不要退回群里说一句。
        """
        try:
            base = self._http_base_url()
            payload = {"user_id": int(user_id), "message": text, "auto_escape": False}
            headers = {"Content-Type": "application/json"}
            if Config.ONEBOT_ACCESS_TOKEN:
                headers["Authorization"] = f"Bearer {Config.ONEBOT_ACCESS_TOKEN}"
            resp = await self.http.post(f"{base}/send_private_msg", json=payload, headers=headers)
            if resp.status_code != 200:
                logger.error(f"[OneBot] 私聊失败: HTTP {resp.status_code} {resp.text[:200]}")
                return False
            data = resp.json()
            if data.get("retcode") == 0:
                logger.info(f"[OneBot] → 私聊 {user_id}: {text[:50]}")
                return True
            logger.error(f"[OneBot] 私聊失败: retcode={data.get('retcode')} msg={data.get('message')}")
            return False
        except httpx.TimeoutException:
            logger.error(f"[OneBot] 私聊超时: {user_id}")
            return False
        except Exception as e:
            logger.error(f"[OneBot] 私聊异常: {e}")
            return False

    # ---------- HTTP 查询群成员 ----------
    async def get_group_members(self, group_id: int) -> list[dict]:
        """
        获取群成员列表（带 5 分钟缓存）。
        返回: [{"user_id": int, "nickname": str, "card": str}, ...]
        """
        # 检查缓存
        now = time.time()
        cached = self._member_cache.get(group_id)
        if cached and (now - cached[0]) < self._MEMBER_CACHE_TTL:
            return cached[1]

        try:
            base = self._http_base_url()
            headers = {"Content-Type": "application/json"}
            if Config.ONEBOT_ACCESS_TOKEN:
                headers["Authorization"] = f"Bearer {Config.ONEBOT_ACCESS_TOKEN}"

            resp = await self.http.post(
                f"{base}/get_group_member_list",
                json={"group_id": group_id},
                headers=headers,
            )
            if resp.status_code != 200:
                logger.warning(f"[OneBot] get_group_member_list HTTP {resp.status_code}")
                return cached[1] if cached else []

            data = resp.json()
            if data.get("retcode") != 0:
                logger.warning(f"[OneBot] get_group_member_list retcode={data.get('retcode')}")
                return cached[1] if cached else []

            members = []
            for m in data.get("data", []):
                members.append({
                    "user_id": m.get("user_id"),
                    "nickname": m.get("nickname", ""),
                    "card": m.get("card") or "",  # 群名片（可能为空）
                })

            self._member_cache[group_id] = (now, members)
            logger.info(f"[OneBot] 群 {group_id} 成员缓存: {len(members)} 人")
            return members

        except Exception as e:
            logger.warning(f"[OneBot] 获取群成员失败: {e}")
            return cached[1] if cached else []
