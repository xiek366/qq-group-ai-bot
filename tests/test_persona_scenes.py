"""
人设拆分与场景模板（P1-5）回归测试
==================================
覆盖：
  1. 常驻核心优先加载，且**显著短于**原 557 行人设（这是拆分的全部意义）
  2. 核心文件缺失 → **自动回退**到原 persona.txt（绝不能变成"没有人设"）
  3. 核心文件只有注释 → 同样回退
  4. 场景模板：正常渲染、多个占位符、未知占位符原样保留（不 KeyError）
  5. 场景缺失 → 返回空串并告警（调用方有兜底）
  6. 缓存与 reset（热更新/测试用）
  7. 通过 Config.load_persona() 拿到的事实是核心版

运行: python tests/test_persona_scenes.py
"""
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.paths as paths
from app import persona as persona_mod

TMP = Path("data/_tmp_persona_test")


def _setup(tmpdir: Path, core_text: str = None) -> tuple[Path, Path]:
    """造一套临时 config：<tmp>/{persona.txt, persona_core.txt, scenes/}"""
    shutil.rmtree(tmpdir, ignore_errors=True)
    (tmpdir / "scenes").mkdir(parents=True, exist_ok=True)
    full = tmpdir / "persona.txt"
    full.write_text("# 原完整人设\n" + ("你是爱丽丝，一个游戏宅少女。\n" * 100), encoding="utf-8")
    core = tmpdir / "persona_core.txt"
    if core_text is not None:
        core.write_text(core_text, encoding="utf-8")
    return full, core


def _patch_paths(tmpdir: Path):
    """把 persona 模块的路径指到临时目录"""
    persona_mod.PERSONA_PATH = tmpdir / "persona.txt"
    persona_mod.PERSONA_CORE_PATH = tmpdir / "persona_core.txt"
    persona_mod.SCENES_DIR = tmpdir / "scenes"
    persona_mod.reset_cache()


def test_core_is_used_and_much_shorter():
    tmp = TMP / "a"
    _setup(tmp, "# 核心\n你是爱丽丝，天真乐观的游戏宅少女。说话短，1~3 句。")
    _patch_paths(tmp)
    core = persona_mod.load_core()
    assert "天真乐观" in core and "你是爱丽丝" in core, core
    assert "# 核心" not in core, "说明性注释行应被剔除"
    full_len = len((tmp / "persona.txt").read_text(encoding="utf-8"))
    assert len(core) * 5 < full_len, f"核心没短多少：{len(core)} vs {full_len}"
    print(f"   核心 {len(core)} 字（原人设 {full_len} 字，压缩到 {len(core) * 100 // full_len}%）")
    print("✅ test_core_is_used_and_much_shorter PASS")


def test_fallback_when_core_missing():
    tmp = TMP / "b"
    _setup(tmp, core_text=None)          # 没有核心文件
    _patch_paths(tmp)
    text = persona_mod.load_core()
    assert "游戏宅少女" in text, text
    assert len(text) > 500, "回退时应该拿到完整人设"
    print(f"   无核心文件 → 回退到完整人设（{len(text)} 字）")
    print("✅ test_fallback_when_core_missing PASS")


def test_fallback_when_core_is_only_comments():
    tmp = TMP / "c"
    _setup(tmp, "# 只有注释\n# 什么都没写\n")
    _patch_paths(tmp)
    text = persona_mod.load_core()
    assert "游戏宅少女" in text, f"只有注释时也该回退: {text[:60]}"
    print("✅ test_fallback_when_core_is_only_comments PASS")


def test_scene_render_and_unknown_placeholder():
    tmp = TMP / "d"
    _setup(tmp, "# 核心\n爱丽丝。")
    (tmp / "scenes" / "hello.txt").write_text(
        "群 {group} 的 {nick} 你好，未知名：{unknown}，正常花括号 {} 保留",
        encoding="utf-8",
    )
    _patch_paths(tmp)
    out = persona_mod.load_scene("hello", group=100000004, nick="小明")
    assert "100000004" in out and "小明" in out, out
    assert "{unknown}" in out, f"未知占位符不该被吃掉: {out}"
    assert "{}" in out, f"空花括号应原样保留: {out}"
    print(f"   渲染结果: {out[:58]}…")
    print("✅ test_scene_render_and_unknown_placeholder PASS")


def test_missing_scene_returns_empty():
    tmp = TMP / "e"
    _setup(tmp, "# 核心\n爱丽丝。")
    _patch_paths(tmp)
    assert persona_mod.load_scene("does_not_exist") == ""
    assert persona_mod.available_scenes() == []
    print("✅ test_missing_scene_returns_empty PASS")


def test_cache_and_reset():
    tmp = TMP / "f"
    _setup(tmp, "# 核心\n第一版人设")
    _patch_paths(tmp)
    assert "第一版" in persona_mod.load_core()
    # 改文件后不 reset 仍是旧的（缓存生效）
    (tmp / "persona_core.txt").write_text("# 核心\n第二版人设", encoding="utf-8")
    assert "第一版" in persona_mod.load_core(), "缓存没生效"
    persona_mod.reset_cache()
    assert "第二版" in persona_mod.load_core(), "reset 后应该读到新内容"
    print("✅ test_cache_and_reset PASS")


def test_real_project_files():
    """真实仓库：核心文件存在、场景齐全、Config.load_persona 走核心版"""
    persona_mod.reset_cache()
    persona_mod.PERSONA_PATH = paths.PERSONA_PATH
    persona_mod.PERSONA_CORE_PATH = paths.PERSONA_CORE_PATH
    persona_mod.SCENES_DIR = paths.SCENES_DIR

    from app.config import load_persona
    text = load_persona()
    assert "天童爱丽丝" in text, text[:80]
    assert len(text) < 900, f"常驻人设应该很短（当前 {len(text)} 字），否则拆分没意义"

    scenes = persona_mod.available_scenes()
    for name in ("chat", "icebreak", "vision_reply", "image_fail", "summary"):
        assert name in scenes, f"缺少场景模板 {name}（现有 {scenes}）"

    # 场景里的占位符必须能被真实数据渲染
    rendered = persona_mod.load_scene("icebreak", idle_minutes=35)
    assert "35" in rendered and "{idle_minutes}" not in rendered, rendered
    print(f"   实际项目：人设核心 {len(text)} 字；场景 {scenes}")
    print("✅ test_real_project_files PASS")


if __name__ == "__main__":
    print("=== 人设拆分与场景模板回归测试（P1-5）===")
    try:
        test_core_is_used_and_much_shorter()
        test_fallback_when_core_missing()
        test_fallback_when_core_is_only_comments()
        test_scene_render_and_unknown_placeholder()
        test_missing_scene_returns_empty()
        test_cache_and_reset()
        test_real_project_files()
        print("\n🎉 全部通过：人设拆成「常驻核心 + 场景模板」，且缺失时能安全回退")
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
