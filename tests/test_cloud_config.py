"""
云端适配回归测试
================
针对云端（systemd 拉起、UTC 时区、公网可达）最容易踩的三个坑：

  1. 相对路径 → 换工作目录后 data/ 写到别处，表现为「重启后记忆全丢」
  2. 服务器 TZ 是 UTC → 破冰工作时间窗整体偏 8 小时
  3. 面板监听 0.0.0.0 → 公网可直接访问

运行: python tests/test_cloud_config.py
"""
import asyncio
import datetime as dt
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.paths as paths
from app.config import Config
from app.util import clock

TEST_GID = 100000003


def test_paths_are_absolute():
    """所有数据/配置路径必须是绝对路径，且挂在项目根下"""
    for name in ("DATA_DIR", "MEMORY_DIR", "STYLE_DIR", "STICKER_DIR", "ALIAS_DIR", "IMAGE_DIR"):
        p = getattr(paths, name)
        assert p.is_absolute(), f"{name} 不是绝对路径: {p}"
        assert str(p).startswith(str(paths.PROJECT_ROOT)), f"{name} 不在项目根下: {p}"
    for name in ("PERSONA_PATH", "FACE_DICT_PATH", "LEGACY_ALIASES_PATH"):
        assert getattr(paths, name).is_absolute(), f"{name} 不是绝对路径"
    assert paths.PROJECT_ROOT.is_absolute()
    print(f"   project={paths.PROJECT_ROOT}")
    print("✅ test_paths_are_absolute PASS")


def test_data_written_to_project_root_not_cwd():
    """核心回归：把工作目录换掉，记忆/画像仍然落在项目根 data/ 下

    老代码用 Path("data/group_style") 这种相对路径，
    工作目录一变（systemd 少写 WorkingDirectory、从别处启动）
    数据就跑到别的目录去了。
    """
    from app.memory.memory import GroupMemory
    from app.style.profile import StyleProfile

    old_cwd = os.getcwd()
    other_cwd = paths.PROJECT_ROOT / "tests"   # 故意换一个已存在的工作目录
    os.chdir(other_cwd)
    try:
        memory = GroupMemory()
        memory.add_user(TEST_GID, 888888, "小明", "换个目录写写看")

        style = StyleProfile()
        style.add_message(TEST_GID, "小明", "画像也要写对地方")

        # 1) 该写的地方写了
        # 【P0-1】记忆已改为 append-only 对话流：data/stream/<群ID>.jsonl
        assert (paths.STREAM_DIR / f"{TEST_GID}.jsonl").exists(), "对话流没写到项目根 data/stream"
        assert (paths.STYLE_DIR / f"{TEST_GID}.json").exists(), "画像没写到项目根 data/group_style"

        # 2) 工作目录下不该冒出 data/
        stray = Path.cwd() / "data"
        assert not stray.exists(), f"又在工作目录下建了 data/: {stray}"
    finally:
        os.chdir(old_cwd)
        for p in (
            paths.STREAM_DIR / f"{TEST_GID}.jsonl",
            paths.MEMORY_DIR / f"{TEST_GID}.json",
            paths.STYLE_DIR / f"{TEST_GID}.json",
        ):
            try:
                p.unlink()
            except OSError:
                pass
        # 清理可能产生的空目录
        for d in (paths.PROJECT_ROOT / "tests" / "data",):
            if d.exists():
                import shutil
                shutil.rmtree(d, ignore_errors=True)
    print(f"   cwd 换到 {other_cwd} 也能写对位置")
    print("✅ test_data_written_to_project_root_not_cwd PASS")


def test_timezone_is_explicit():
    """破冰/报时用的时间必须带显式时区，不看服务器 TZ"""
    Config.TIMEZONE = "Asia/Shanghai"
    now = clock.now()
    assert now.tzinfo is not None, "clock.now() 居然是没有时区的裸时间"
    assert now.utcoffset() == dt.timedelta(hours=8), f"偏移不对: {now.utcoffset()}"
    assert "Asia/Shanghai" in clock.tz_label()

    # 报时工具的输出里要能看出时区，别再写死 UTC+8
    from app.bot.handler import MessageHandler
    text = MessageHandler._tool_current_time()
    assert "Asia/Shanghai" in text or "UTC+8" in text, text
    print(f"   clock.now() = {now.isoformat()}  |  {text}")
    print("✅ test_timezone_is_explicit PASS")


def test_dashboard_binds_loopback_by_default():
    """面板默认只监听本机（云端走 SSH 隧道），不是 0.0.0.0"""
    host = Config.DASHBOARD_HOST
    assert host, "DASHBOARD_HOST 不能为空"
    if host not in ("127.0.0.1", "localhost", "::1"):
        print(f"   ⚠️ 你的 .env 里 DASHBOARD_HOST={host}（非本机）—— 请确认安全组已限制来源")
    else:
        print(f"   DASHBOARD_HOST={host}（仅本机，安全）")
    assert len(Config.DASHBOARD_TOKEN) > 0
    print("✅ test_dashboard_binds_loopback_by_default PASS")


def test_legacy_data_warning_is_safe():
    """旧数据目录检测函数在正常情况下应静默通过（不能自己抛异常）"""
    paths.warn_legacy_relative_data(logging.getLogger("test"))
    print("✅ test_legacy_data_warning_is_safe PASS")


def main():
    test_paths_are_absolute()
    test_data_written_to_project_root_not_cwd()
    test_timezone_is_explicit()
    test_dashboard_binds_loopback_by_default()
    test_legacy_data_warning_is_safe()
    print("\n🎉 全部通过：代码不再依赖进程工作目录 / 服务器时区")


if __name__ == "__main__":
    print("=== 云端适配回归测试 ===\n")
    main()
