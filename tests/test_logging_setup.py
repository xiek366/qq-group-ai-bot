"""
日志配置回归测试（线上事故回归）
================================
**事故经过（2026-10-07 线上）**：服务器上每打一条日志，stderr 就刷一大段

    --- Logging error ---
    TypeError: setup_logging.<locals>.<lambda>() takes 1 positional argument but 2 were given
      File ".../logging/__init__.py", line 615, in formatTime
        ct = self.converter(record.created)

**根因**：把普通函数（lambda）赋给了**类属性** `logging.Formatter.converter`，
它会被当作方法绑定，于是 `self.converter(record.created)` 传进去的是 `(self, ts)` 两个参数。

**为什么本地测试没抓到**：测试里的 logging 格式串是 `"%(levelname)s %(message)s"`，
**不含 `%(asctime)s`**，所以 `formatTime()` 从来没被调用过；而 `setup_logging()` 只在真正
跑 bot 时才执行。→ 本文件的职责就是把这个盲区堵上。

运行: python tests/test_logging_setup.py
"""
import io
import logging
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.main import setup_logging
from app.util import clock

TIME_RE = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")
FMT = "%(asctime)s [%(levelname)s] %(name)s - %(message)s"


def _emit_with_asctime(msg: str) -> tuple[str, str]:
    """真打一条带 %(asctime)s 的日志，返回 (格式化输出, logging 自身错误输出)"""
    buf, err = io.StringIO(), io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(logging.Formatter(FMT, datefmt="%Y-%m-%d %H:%M:%S"))
    root = logging.getLogger()
    root.addHandler(handler)
    old_stderr, sys.stderr = sys.stderr, err      # logging 的内部错误走 sys.stderr
    try:
        root.warning(msg)
    finally:
        sys.stderr = old_stderr
        root.removeHandler(handler)
    return buf.getvalue(), err.getvalue()


def test_converter_is_staticmethod():
    """setup_logging 之后，converter 必须是 staticmethod 而不是会被绑定成方法的普通函数。

    注意一个容易忽略的细节：logging 默认的 `converter = time.localtime` 是**内置函数**，
    内置函数挂在类上不会触发描述符绑定，所以默认值是安全的；
    而 Python 里定义的函数/lambda 挂上类就会被绑定 —— 这就是线上那个 TypeError 的由来。
    """
    setup_logging()          # 必须先安装，否则拿到的是默认的 time.localtime
    # 注意：staticmethod 通过**类**访问会自动解包成函数，所以结构断言要查 __dict__ 里的原始属性
    raw = logging.Formatter.__dict__.get("converter")
    assert isinstance(raw, staticmethod), (
        f"logging.Formatter.converter 必须用 staticmethod 包一层，否则 self.converter(ts) 会多传一个 self；"
        f"当前类型 = {type(raw).__name__}（内置函数不会绑定，Python 函数会）"
    )
    # 行为断言（更可靠）：直接调用必须只吃一个参数
    st = logging.Formatter.converter(time.time())
    assert hasattr(st, "tm_hour"), st
    print("✅ test_converter_is_staticmethod PASS")


def test_asctime_actually_formats():
    """真打一条带 asctime 的日志：格式化成功 + 时间戳是本时区当前时间 + 没有 Logging error"""
    setup_logging()
    out, err = _emit_with_asctime("日志时间戳自检")

    assert "Logging error" not in err, f"logging 自身报错了：\n{err}"
    assert "TypeError" not in err, f"converter 调用出错：\n{err}"
    assert "日志时间戳自检" in out, out

    m = TIME_RE.search(out)
    assert m, f"asctime 没被格式化出来：{out!r}"
    now = clock.now()
    assert m.group(0).startswith(now.strftime("%Y-%m-%d %H")), (
        f"日志时间戳 {m.group(0)} 与本地时区当前时间 {now.strftime('%Y-%m-%d %H:%M')} 不一致"
    )
    print(f"   格式化结果: {out.strip()[:78]}")
    print(f"   时区对齐  : {m.group(0)} ≈ {now.strftime('%Y-%m-%d %H:%M:%S')}（{clock.tz_label()}）")
    print("✅ test_asctime_actually_formats PASS")


def test_negative_control_plain_function_breaks():
    """对照组：把 converter 换回普通函数，必须复现线上那个 TypeError。

    没有这一段，上面的测试就只是"碰巧通过"——无法证明它真的能抓住这个坑。
    """
    saved = logging.Formatter.converter
    logging.Formatter.converter = lambda ts: time.localtime(ts)      # 故意犯错
    try:
        _out, err = _emit_with_asctime("对照组")
        assert "Logging error" in err or "TypeError" in err, (
            f"对照组竟然没报错，说明本测试检测不到这个问题：err={err!r}"
        )
        assert "takes 1 positional argument but 2 were given" in err or "Logging error" in err, err
        print(f"   对照组复现成功: {err.strip().splitlines()[0][:70]}")
    finally:
        logging.Formatter.converter = saved
    print("✅ test_negative_control_plain_function_breaks PASS（证明测试有效）")


def test_setup_logging_is_idempotent():
    """重复调用 setup_logging（测试/重载场景）不该抛异常，也不该叠加 handler"""
    root = logging.getLogger()
    before = len(root.handlers)
    setup_logging()
    setup_logging()
    assert len(root.handlers) == before, f"handler 数量变了：{before} → {len(root.handlers)}"
    # 重复配置后格式化仍然正常
    out, err = _emit_with_asctime("重复配置后")
    assert "Logging error" not in err and TIME_RE.search(out), (out, err)
    print("✅ test_setup_logging_is_idempotent PASS")


def test_startup_banner_reports_key_settings():
    """启动横幅必须把关键开关打出来（改完 .env 靠它自查，别再去翻服务器文件）

    2026-10-07 教训：改了面板 token，从日志里看不出来，白折腾了一轮。
    """
    from app.config import Config
    from app.main import log_startup_banner

    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(logging.Formatter("%(message)s"))
    log = logging.getLogger("banner-test")
    log.handlers = [handler]
    log.setLevel(logging.INFO)
    log.propagate = False
    try:
        log_startup_banner(log)
    finally:
        log.handlers = []

    out = buf.getvalue()
    for key, must_have in [
        ("Thinking", "Thinking"),
        ("Chat", "Chat       : 概率"),
        ("Vision", "Vision     : 思考"),
        ("Proactive", "Proactive  :"),
        ("Persona", "Persona    :"),
        ("Dashboard", "Dashboard  :"),
    ]:
        assert must_have in out, f"横幅缺少 {key} 行：\n{out}"

    # 关键配置值要真的出现在横幅里（而不是只打了标签）
    assert str(Config.CHAT_REPLY_CHANCE) in out, out
    assert Config.LLM_THINKING in out, out
    if Config.PROACTIVE_GROUPS:
        assert str(Config.PROACTIVE_GROUPS[0]) in out, out
    line = [l for l in out.splitlines() if l.strip().startswith("Thinking")][0]
    print(f"   横幅自查行: {line.strip()}")
    print("✅ test_startup_banner_reports_key_settings PASS")


if __name__ == "__main__":
    print("=== 日志配置回归测试 ===\n")
    test_converter_is_staticmethod()
    test_asctime_actually_formats()
    test_negative_control_plain_function_breaks()
    test_setup_logging_is_idempotent()
    test_startup_banner_reports_key_settings()
    print("\n🎉 全部通过：日志时间戳按 Config.TIMEZONE 正常格式化，且不会再刷 Logging error")
