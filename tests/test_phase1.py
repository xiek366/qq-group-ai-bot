"""
Phase 1 冒烟测试 —— 不依赖 NapCat/LLM，只测消息解析和基本导入。
运行: python tests/test_phase1.py
"""
import sys
import os

# 让 import 能找到 app/
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_imports():
    """测试所有模块能正常 import"""
    from app.config import Config, load_persona
    from app.bot.message import parse_message
    from app.bot.handler import MessageHandler
    from app.bot.onebot import OneBotClient
    persona = load_persona()
    assert len(persona) > 10, "人设内容太短"
    print("✅ test_imports PASS")


def test_parse_ping():
    """测试解析 /ping 命令"""
    event = {
        "post_type": "message",
        "message_type": "group",
        "group_id": 123456,
        "message_id": 10086,
        "message": [{"type": "text", "data": {"text": "/ping"}}],
        "sender": {
            "user_id": 888888,
            "nickname": "小明",
        },
    }
    from app.bot.message import parse_message
    parsed = parse_message(event)
    assert parsed is not None
    assert parsed["group_id"] == 123456
    assert parsed["command"] == "/ping"
    assert parsed["content"] == "/ping"
    assert parsed["is_at_bot"] is False
    print("✅ test_parse_ping PASS")


def test_parse_at_bot():
    """测试解析 @机器人"""
    event = {
        "post_type": "message",
        "message_type": "group",
        "group_id": 123456,
        "message_id": 10087,
        "message": [
            {"type": "at", "data": {"qq": "999999", "display": "@AI助手"}},
            {"type": "text", "data": {"text": " 你好"}},
        ],
        "sender": {
            "user_id": 888888,
            "nickname": "小明",
        },
    }
    # 设置 BOT_QQ 让 is_at_bot 能识别
    os.environ["BOT_QQ"] = "999999"
    # 重新加载 Config
    import importlib, app.config
    importlib.reload(app.config)

    from app.bot.message import parse_message
    parsed = parse_message(event)
    assert parsed is not None
    assert parsed["is_at_bot"] is True
    assert "你好" in parsed["content"]
    assert parsed["command"] is None
    print("✅ test_parse_at_bot PASS")


def test_parse_command():
    """测试解析 /搜 xxx /问 xxx"""
    from app.bot.message import parse_message

    event = {
        "post_type": "message",
        "message_type": "group",
        "group_id": 123456,
        "message_id": 10088,
        "message": [{"type": "text", "data": {"text": "/搜 Python最新版本"}}],
        "sender": {"user_id": 888888, "nickname": "小明"},
    }
    parsed = parse_message(event)
    assert parsed["command"] == "/搜"
    assert parsed["args"] == "Python最新版本"
    print("✅ test_parse_command PASS")


def test_ignore_non_group():
    """测试忽略非群消息"""
    from app.bot.message import parse_message

    event = {
        "post_type": "notice",  # 不是 message
        "group_id": 123456,
    }
    assert parse_message(event) is None
    print("✅ test_ignore_non_group PASS")


if __name__ == "__main__":
    print("=== Phase 1 冒烟测试 ===\n")
    test_imports()
    test_parse_ping()
    test_parse_at_bot()
    test_parse_command()
    test_ignore_non_group()
    print("\n🎉 所有 Phase 1 测试通过！")
    print("下一步: 安装依赖 → 连接 NapCat → 运行 python -m app.main")
