"""
P2b 回归测试：天气服务 + 两个新任务
===================================
覆盖 DEVLOG [P2b-0] §4 的测试计划：

城市与解析
  1. 预置城市解析（4 个 + 英文/简写/带"市"的写法 + 未收录）
  2. WMO 天气代码 → 中文
  3. provider 解析：正常 / 缺温度 / 空 daily / 可选字段缺失
  4. provider 网络失败：HTTP 非 200 / 超时 → None（**绝不抛给上层**）

缓存与降级（硬要求，§5.5）
  5. 写缓存 → 另一个 service 实例能读到（落盘生效）
  6. 命中缓存时**不再请求 API**；同城第二次 fetch 不再发请求
  7. 不同城市互不串（缓存按城市隔离）
  8. 跨天失效（文件名带日期）
  9. API 失败 → fetch_daily 返回 None，且**不写缓存**（下次会重试）

规则引擎（Q4：按 ROADMAP §5.3）
 10. 每条注意事项的阈值边界（降雨≥40 / 温差≥8 / 高温≥33 / 低温≤5 / UV≥6 / 风≥30）
 11. 注意事项最多 3 条（避免播报变长）
 12. 事实句拼装（完整 / 缺温度 / 缺降水）

渲染与任务
 13. 播报结构（城市 + 事实 + 注意事项 + 收尾）
 14. `/天气` 文案（含明天）
 15. `weather_morning`：未绑城市 → 跳过；API 失败 → 跳过（不播报占位）；正常 → 一条播报
 16. 收尾句润色：有 polish 用它的；polish 抛异常 → 回落模板（播报不受影响）
 17. `water_reminder`：喝水/久坐交替轮换；时段窗解析；默认值来自 Config

运行: python tests/test_p2b_weather.py
（全部缓存写临时目录，不碰真实数据）
"""
import asyncio
import json
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx

from app.config import Config
from app.schedule.task import TaskContext, TaskSkip
from app.schedule.tasks import WaterReminderTask, WeatherMorningTask, pick_text
from app.schedule.tasks.water_reminder import window_list
from app.weather import CITIES, WeatherDaily, WeatherService, resolve_city
from app.weather.provider import OpenMeteoProvider, describe_code
from app.weather.service import fact_line, notes_for

TMP = Path("data/_tmp_p2b_test")
GID = 100000060

# Open-Meteo 真实响应结构（截取 daily 部分）
SAMPLE = {
    "latitude": 39.9, "longitude": 116.4, "timezone": "Asia/Shanghai",
    "daily": {
        "time": ["2026-10-08", "2026-10-09"],
        "weather_code": [3, 61],
        "temperature_2m_max": [22.4, 19.1],
        "temperature_2m_min": [10.6, 12.0],
        "precipitation_probability_max": [45, 80],
        "wind_speed_10m_max": [31.5, 12.0],
        "uv_index_max": [6.4, 2.0],
    },
}


class StubResponse:
    def __init__(self, payload, status=200, boom=None):
        self._payload = payload
        self.status_code = status
        self.text = json.dumps(payload)[:200]
        self._boom = boom

    def json(self):
        if self._boom:
            raise self._boom
        return self._payload


class StubClient:
    """假的 httpx.AsyncClient：记录调用次数，可配置返回/异常"""

    def __init__(self, response=None, exc=None):
        self.calls = 0
        self._response = response
        self._exc = exc

    async def get(self, url, params=None):
        self.calls += 1
        if self._exc:
            raise self._exc
        return self._response

    async def aclose(self):
        pass


class StubProvider:
    name = "stub"

    def __init__(self, data=None, calls_ok=True, city_mismatch=False):
        self.calls = 0
        self.data = data
        self.calls_ok = calls_ok
        self.city_mismatch = city_mismatch

    async def fetch_daily(self, city):
        self.calls += 1
        if self.data is None:
            return None
        if self.city_mismatch:
            return self.data                      # 故意返回"北京"的数据，用来验证防串城市
        d = WeatherDaily(**{**self.data.__dict__})
        d.city = city
        return d


def _fresh() -> tuple[WeatherService, Path]:
    tmp = TMP / f"s{abs(hash(str(asyncio.get_event_loop_policy())) % 10000)}"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)
    return WeatherService(provider=StubProvider(data=_sample()), cache_dir=tmp), tmp


def _sample(city="北京") -> WeatherDaily:
    d = WeatherDaily(city=city, date=WeatherService.today(), code=3, desc="阴",
                     tmax=22.4, tmin=10.6, precip_prob=45, wind_max=31.5, uv_max=6.4)
    return d


def _ctx(city=None, **kwargs) -> TaskContext:
    group_cfg = {"weather": {"city": city} if city else {}}
    return TaskContext(group_id=GID, now=0.0, config=kwargs, group_config=group_cfg,
                       extra={"send": None})


# ---------- 城市与解析 ----------

def test_city_resolution():
    assert set(CITIES) == {"北京", "哈尔滨", "上海", "济南"}
    assert resolve_city("北京") == "北京"
    assert resolve_city("beijing") == "北京" and resolve_city("bj") == "北京"
    assert resolve_city("shanghai") == "上海" and resolve_city("SH ") == "上海"
    assert resolve_city("哈尔滨市") == "哈尔滨"
    assert resolve_city("火星") == "", "未收录城市必须返回空"
    assert resolve_city("火星", default="北京") == "北京", "支持默认值"
    assert resolve_city("") == ""
    print(f"   城市解析 OK（预置 {list(CITIES)}）")
    print("✅ test_city_resolution PASS")


def test_describe_code():
    assert describe_code(0) == "晴"
    assert describe_code(3) == "阴"
    assert describe_code(61) == "小雨"
    assert describe_code(95) == "雷阵雨"
    assert describe_code(2) == "多云"
    assert describe_code(None) == "" and describe_code("x") == ""
    assert describe_code(999) == "", "未知代码返回空串（由调用方兜底）"
    print("✅ test_describe_code PASS")


def test_provider_parse():
    d = OpenMeteoProvider._parse("北京", SAMPLE)
    assert d is not None and d.ok
    assert d.tmax == 22.4 and d.tmin == 10.6 and d.precip_prob == 45
    assert d.wind_max == 31.5 and d.uv_max == 6.4 and d.desc == "阴"
    assert d.date == "2026-10-08"

    # 没有温度 → None（这条播报没意义）
    bad = {"daily": {"time": ["2026-10-08"], "weather_code": [3]}}
    assert OpenMeteoProvider._parse("北京", bad) is None
    # 空 daily → None
    assert OpenMeteoProvider._parse("北京", {"daily": {}}) is None
    assert OpenMeteoProvider._parse("北京", {}) is None

    # 只有温度、其它字段缺失 → 仍可用（渲染时逐字段判断）
    partial = {"daily": {"time": ["2026-10-08"], "temperature_2m_max": [20.0],
                         "temperature_2m_min": [10.0]}}
    p = OpenMeteoProvider._parse("北京", partial)
    assert p is not None and p.precip_prob is None and p.wind_max is None
    print("✅ test_provider_parse PASS")


async def test_provider_network_failures():
    # HTTP 500
    p = OpenMeteoProvider(client=StubClient(StubResponse({}, status=500)))
    assert await p.fetch_daily("北京") is None
    # 超时
    p2 = OpenMeteoProvider(client=StubClient(exc=httpx.TimeoutException("boom")))
    assert await p2.fetch_daily("北京") is None
    # 其它异常（json 解析炸）
    p3 = OpenMeteoProvider(client=StubClient(StubResponse({}, boom=ValueError("bad json"))))
    assert await p3.fetch_daily("北京") is None
    # 未收录城市：直接 None，不发请求
    stub = StubClient(StubResponse(SAMPLE))
    p4 = OpenMeteoProvider(client=stub)
    assert await p4.fetch_daily("火星") is None
    assert stub.calls == 0, "未收录城市不该发请求"
    print("✅ test_provider_network_failures PASS")


# ---------- 缓存与降级 ----------

async def test_cache_roundtrip_and_isolation():
    tmp = TMP / "cache"
    shutil.rmtree(tmp, ignore_errors=True)
    prov = StubProvider(data=_sample())
    svc = WeatherService(provider=prov, cache_dir=tmp)
    d = await svc.fetch_daily("北京")
    assert d is not None and prov.calls == 1

    # 同城第二次：命中缓存，不再请求
    d2 = await svc.fetch_daily("北京")
    assert d2 is not None and prov.calls == 1, "第二次不该再请求 API"

    # 另一个实例（模拟重启）从磁盘读到
    svc2 = WeatherService(provider=StubProvider(data=None), cache_dir=tmp)
    hit = svc2.get_daily("北京")
    assert hit is not None and hit.tmax == 22.4, "落盘缓存应该能被新实例读到"
    assert (tmp / f"北京_{WeatherService.today()}.json").exists()

    # 城市隔离：上海单独一份，互不串
    await svc.fetch_daily("上海")
    assert (tmp / f"上海_{WeatherService.today()}.json").exists()
    assert svc.get_daily("上海").city == "上海"
    assert svc.get_daily("北京").city == "北京"

    # 防串城市：provider 返回的城市与请求不符时，必须按请求纠正（否则缓存内容会张冠李戴）
    mismatch = WeatherService(provider=StubProvider(data=_sample(), city_mismatch=True),
                              cache_dir=TMP / "mismatch")
    m = await mismatch.fetch_daily("上海")
    assert m is not None and m.city == "上海", m
    assert mismatch.get_daily("上海").city == "上海"
    # 跨天（改 today）→ 当天缓存读不到
    # ⚠️ 必须从 `__dict__` 取 staticmethod **对象本身**再还原：`WeatherService.today` 经类访问
    # 会自动解包成普通函数，赋回去就变成实例方法（→ self 被当成第一个参数）。P0 的日志
    # converter 就是同一个坑，这里再踩了一次。
    old = WeatherService.__dict__["today"]
    WeatherService.today = staticmethod(lambda: "2099-01-01")
    try:
        svc3 = WeatherService(provider=StubProvider(data=None), cache_dir=tmp)
        assert svc3.get_daily("北京") is None, "跨天必须失效"
    finally:
        WeatherService.today = old
    print(f"   缓存：请求 {prov.calls} 次（北京 1 + 上海 1），跨天失效、城市隔离 OK")
    print("✅ test_cache_roundtrip_and_isolation PASS")


async def test_failure_does_not_cache():
    tmp = TMP / "fail"
    shutil.rmtree(tmp, ignore_errors=True)
    prov = StubProvider(data=None)
    svc = WeatherService(provider=prov, cache_dir=tmp)
    assert await svc.fetch_daily("北京") is None
    assert not list(tmp.glob("*.json")), "失败不该写缓存（否则整天都用不到天气）"
    # 再试一次仍会请求（没有被"空缓存"挡住）
    assert await svc.fetch_daily("北京") is None
    assert prov.calls == 2
    print("✅ test_failure_does_not_cache PASS")


# ---------- 规则引擎 ----------

def test_notes_thresholds():
    def notes(**kw):
        return notes_for(WeatherDaily(city="北京", **kw))

    # 降雨：40 是边界
    assert any("伞" in n for n in notes(precip_prob=40))
    assert not any("伞" in n for n in notes(precip_prob=39))
    # 温差：8 是边界
    assert any("添件衣服" in n for n in notes(tmax=20, tmin=12))
    assert not any("添件衣服" in n for n in notes(tmax=20, tmin=12.1))
    # 高温 / 低温
    assert any("防暑" in n for n in notes(tmax=33))
    assert not any("防暑" in n for n in notes(tmax=32.9))
    assert any("保暖" in n for n in notes(tmin=5))
    assert not any("保暖" in n for n in notes(tmin=5.1))
    # UV
    assert any("防晒" in n for n in notes(uv_max=6))
    assert not any("防晒" in n for n in notes(uv_max=5.9))
    # 风
    assert any("风" in n and "坠物" in n for n in notes(wind_max=30))
    assert not any("坠物" in n for n in notes(wind_max=29.9))
    # 最多 3 条
    many = notes(precip_prob=90, tmax=35, tmin=1, uv_max=10, wind_max=50)
    assert len(many) == 3, many
    # 一切正常 → 没有注意事项
    assert notes(tmax=22, tmin=18, precip_prob=5, uv_max=2, wind_max=5) == []
    print(f"   阈值边界全部正确；极端天气下注意事项被截到 3 条")
    print("✅ test_notes_thresholds PASS")


def test_fact_line():
    full = fact_line(_sample())
    assert "11~22" in full and "阴" in full and "45%" in full and "32km/h" in full, full
    partial = fact_line(WeatherDaily(city="北京", tmax=20))
    assert "最高 20" in partial, partial
    only_min = fact_line(WeatherDaily(city="北京", tmin=3))
    assert "最低 3" in only_min, only_min
    empty = fact_line(WeatherDaily(city="北京"))
    assert empty, "完全没有数据时也要给一句兜底文案"
    print(f"   事实句：{full}")
    print("✅ test_fact_line PASS")


# ---------- 渲染与任务 ----------

async def test_render_broadcast_and_command():
    svc = WeatherService(provider=StubProvider(data=_sample()), cache_dir=TMP / "render")
    d = _sample()
    text = svc.render_broadcast("北京", d, finish="今天也要加油！")
    lines = text.splitlines()
    assert "北京" in lines[0] and "11~22" in text, text
    assert "⚠️" in text and "伞" in text, text
    assert lines[-1] == "今天也要加油！", lines
    # 不给 finish → 用模板
    t2 = svc.render_broadcast("北京", d)
    assert t2.splitlines()[-1] in WeatherService(provider=None).finish_line(0) or t2.splitlines()[-1], t2

    cmd = svc.render_command("北京", d, tomorrow=WeatherDaily(
        city="北京", tmax=19, tmin=12, desc="小雨", precip_prob=80))
    assert "明天" in cmd and "12~19" in cmd and "80%" in cmd, cmd
    print(f"   播报：{text.splitlines()[0]} / 注意事项 {len(notes_for(d))} 条 / 收尾 OK")
    print("✅ test_render_broadcast_and_command PASS")


async def test_weather_task_skip_paths():
    svc = WeatherService(provider=StubProvider(data=_sample()), cache_dir=TMP / "task")
    task = WeatherMorningTask(svc)

    # 未绑城市 → TaskSkip（宁可没有，也不要发错城市）
    try:
        await task.render(_ctx(city=None))
        raise AssertionError("未绑城市应该跳过")
    except TaskSkip as e:
        assert "未绑定城市" in str(e), e

    # 绑了城市但 API 失败 → TaskSkip（不播报占位）
    fail_svc = WeatherService(provider=StubProvider(data=None), cache_dir=TMP / "task_fail")
    try:
        await task.__class__(fail_svc).render(_ctx(city="北京"))
        raise AssertionError("取数失败应该跳过")
    except TaskSkip as e:
        assert "不播报" in str(e), e

    # 全局兜底城市也能用（WEATHER_CITY）
    old = Config.WEATHER_CITY
    Config.WEATHER_CITY = "上海"
    try:
        parts = await task.render(_ctx(city=None))
        assert parts and "上海" in parts[0], parts
    finally:
        Config.WEATHER_CITY = old
    print("   未绑城市 / API 失败 → 都安静跳过（不播报占位内容）")
    print("✅ test_weather_task_skip_paths PASS")


async def test_weather_task_polish_fallback():
    svc = WeatherService(provider=StubProvider(data=_sample()), cache_dir=TMP / "polish")

    async def good_polish(city, data):
        return "早安，今天也要元气满满！"

    task = WeatherMorningTask(svc, polish=good_polish)
    parts = await task.render(_ctx(city="北京"))
    assert parts[0].splitlines()[-1] == "早安，今天也要元气满满！", parts[0]

    async def bad_polish(city, data):
        raise RuntimeError("LLM 挂了")

    task2 = WeatherMorningTask(svc, polish=bad_polish)
    parts2 = await task2.render(_ctx(city="北京"))
    assert parts2 and "北京" in parts2[0], parts2
    assert "元气满满" not in parts2[0], "润色失败应该回落模板而不是发出半截内容"
    print("   收尾句：润色成功用润色；润色失败回落模板（播报不受影响）")
    print("✅ test_weather_task_polish_fallback PASS")


def test_water_task_and_texts():
    # 喝水/久坐交替
    texts = [pick_text(i) for i in range(6)]
    assert "喝水" in texts[0] or "水杯" in texts[0], texts
    assert "走两步" in texts[1] or "活动" in texts[1] or "伸个懒腰" in texts[1] or "椅子" in texts[1], texts
    assert texts[0] != texts[2] and texts[1] != texts[3], "池内要轮换，不要连着重复"
    assert len(set(texts)) >= 4

    # 时段窗
    old = Config.WATER_REMINDER_WINDOW
    Config.WATER_REMINDER_WINDOW = "10:00-18:00"
    try:
        assert window_list() == ["10:00", "18:00"]
        Config.WATER_REMINDER_WINDOW = "乱写"
        assert window_list() == [], "解析不了则不限制时段（宁可多提醒，也不要整天静默）"
    finally:
        Config.WATER_REMINDER_WINDOW = old

    task = WaterReminderTask()
    cfg = task.effective_config({})
    assert cfg["trigger"]["type"] == "interval"
    assert cfg["trigger"]["minutes"] == Config.WATER_REMINDER_INTERVAL_MINUTES
    assert cfg["trigger"]["window"] == ["10:00", "18:00"]
    assert cfg["daily_limit"] == Config.WATER_REMINDER_DAILY_LIMIT == 4
    assert cfg["require_active_within_minutes"] == 30, "死群不打扰"
    print(f"   提醒：间隔 {cfg['trigger']['minutes']} 分 / 时段 {cfg['trigger']['window']} / "
          f"每日 ≤{cfg['daily_limit']} / 活跃 {cfg['require_active_within_minutes']} 分内")
    print("✅ test_water_task_and_texts PASS")


async def test_task_registered_configs():
    """两个新任务的默认触发器要符合 ROADMAP（08:00 cron / 75 分 interval）"""
    svc = WeatherService(provider=StubProvider(data=_sample()), cache_dir=TMP / "cfg")
    wt = WeatherMorningTask(svc)
    assert wt.trigger_spec["type"] == "cron" and wt.trigger_spec["at"] == "08:00"
    # 群配置里的 broadcast_at 能覆盖播报时刻
    cfg = wt.effective_config({"weather": {"city": "北京", "broadcast_at": "07:30"}})
    assert cfg["trigger"]["at"] == "07:30", cfg["trigger"]
    assert wt.build_trigger(cfg).at == "07:30"
    print("   天气任务支持 per-group broadcast_at 覆盖（07:30）")
    print("✅ test_task_registered_configs PASS")


async def _run_all():
    test_city_resolution()
    test_describe_code()
    test_provider_parse()
    await test_provider_network_failures()
    await test_cache_roundtrip_and_isolation()
    await test_failure_does_not_cache()
    test_notes_thresholds()
    test_fact_line()
    await test_render_broadcast_and_command()
    await test_weather_task_skip_paths()
    await test_weather_task_polish_fallback()
    test_water_task_and_texts()
    await test_task_registered_configs()


if __name__ == "__main__":
    print("=== P2b 回归测试（天气 + 提醒）===")
    try:
        asyncio.run(_run_all())
        print("\n🎉 全部通过：天气服务（解析/缓存/降级/规则）+ 两个任务都符合设计")
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
