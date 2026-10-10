"""
视觉管道（P0-3）回归测试
========================
用桩下载器 + 桩 LLM，不碰网络，验证：

  1. 正常图 → base64 内联后交给模型，描述正确返回
  2. 缓存：同一张图第二次发**不再调用模型**（这是线上最关心的省钱点）
  3. 大图 → 不内联，退回原始 URL
  4. 坏数据（非图片字节）→ 跳过；全坏 → 失败且原因明确（handler 会走"没看清"）
  5. 部分坏 → 好图照常识别（逐张隔离）
  6. 模型返回空 → ok=False，reason=empty_response
  7. 魔数识别：JPEG/PNG/GIF/WebP 认得，其它不认

运行: python tests/test_vision_pipeline.py
"""
import asyncio
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import Config
from app.vision.cache import VisionCache
from app.vision.pipeline import VisionPipeline, sniff_mime

TMP = Path("data/_tmp_vision_test")

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 200          # 魔数正确的小图
JPEG = b"\xff\xd8\xff" + b"\x00" * 200
GIF = b"GIF89a" + b"\x00" * 100
WEBP = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x00" * 50
NOT_IMAGE = b"<html>not an image</html>"


class StubLLM:
    """记录被调用次数与收到的 image_urls"""

    def __init__(self, reply="一只橘猫趴在键盘上"):
        self.reply = reply
        self.calls = 0
        self.last_urls: list[str] = []

    async def chat_with_images(self, image_urls, prompt="", **kwargs):
        self.calls += 1
        self.last_urls = list(image_urls)
        return self.reply


def make_pipeline(files: dict[str, bytes], reply="一只橘猫趴在键盘上"):
    shutil.rmtree(TMP, ignore_errors=True)
    TMP.mkdir(parents=True, exist_ok=True)

    async def fetcher(url: str):
        return files.get(url)

    cache = VisionCache(cache_dir=TMP / "cache", ttl_days=30)
    llm = StubLLM(reply)
    return VisionPipeline(cache=cache, fetcher=fetcher), llm


def test_sniff_mime():
    assert sniff_mime(PNG) == "image/png"
    assert sniff_mime(JPEG) == "image/jpeg"
    assert sniff_mime(GIF) == "image/gif"
    assert sniff_mime(WEBP) == "image/webp"
    assert sniff_mime(NOT_IMAGE) == ""
    assert sniff_mime(b"") == ""
    # 假 WebP（RIFF 但不是 WEBP）要拒掉
    assert sniff_mime(b"RIFF\x00\x00\x00\x00WAVE") == ""
    print("✅ test_sniff_mime PASS")


async def test_pipeline_records_metrics():
    """【P0-4】管道要把看图指标记进 metrics（命中/失败/内联）"""
    from app import metrics as metrics_mod

    metrics_mod._instance = metrics_mod.Metrics(path=TMP / "metrics.json", save_every=999)
    try:
        p, llm = make_pipeline({"u1": PNG})
        await p.describe(["u1"], llm=llm)          # 新请求 → ok
        await p.describe(["u1"], llm=llm)          # 命中缓存
        p2, llm2 = make_pipeline({"bad": NOT_IMAGE})
        await p2.describe(["bad"], llm=llm2)       # 全坏 → 失败
        vis = metrics_mod.get().snapshot()["vision"]
        assert vis["calls"] == 3 and vis["cache_hits"] == 1 and vis["failures"] == 1, vis
        assert vis["inlined"] == 1, vis
        print(f"   vision 指标：calls={vis['calls']} 命中={vis['cache_hits']} "
              f"失败={vis['failures']} 内联={vis['inlined']}")
        print("✅ test_pipeline_records_metrics PASS")
    finally:
        metrics_mod._instance = None


async def test_inline_and_describe():
    p, llm = make_pipeline({"u1": PNG})
    r = await p.describe(["u1"], llm=llm)
    assert r.ok and r.description == "一只橘猫趴在键盘上", r
    assert r.inlined == 1 and r.images_used == 1 and not r.cached
    assert llm.calls == 1
    assert llm.last_urls[0].startswith("data:image/png;base64,"), llm.last_urls[0][:40]
    print("✅ test_inline_and_describe PASS")


async def test_cache_prevents_second_call():
    p, llm = make_pipeline({"u1": PNG})
    r1 = await p.describe(["u1"], llm=llm)
    r2 = await p.describe(["u1"], llm=llm)
    assert r1.ok and r2.ok
    assert r2.cached is True, "第二次应该命中缓存"
    assert llm.calls == 1, f"缓存没生效，模型被调用了 {llm.calls} 次"
    assert r2.description == r1.description
    print(f"   模型调用次数 = {llm.calls}（第二次命中缓存）")
    print("✅ test_cache_prevents_second_call PASS")


async def test_large_image_falls_back_to_url():
    big = PNG + b"\x00" * int(Config.VISION_INLINE_MAX_MB * 1024 * 1024 + 1024)
    p, llm = make_pipeline({"u1": big})
    r = await p.describe(["u1"], llm=llm)
    assert r.ok, r
    assert r.inlined == 0, "超大图不该内联"
    assert llm.last_urls[0] == "u1", "超大图应退回原始 URL"
    print(f"   大图 {len(big) // 1024}KB → 退回 URL（内联上限 {Config.VISION_INLINE_MAX_MB}MB）")
    print("✅ test_large_image_falls_back_to_url PASS")


async def test_bad_image_skipped_and_all_failed():
    # 全坏 → 失败，原因 all_failed，且不调用模型
    p, llm = make_pipeline({"bad": NOT_IMAGE})
    r = await p.describe(["bad"], llm=llm)
    assert not r.ok and r.reason == "all_failed", r
    assert r.skipped == ["bad_format"], r.skipped
    assert llm.calls == 0, "没有可用图片时不该调用模型"

    # 部分坏 → 好图照常识别（逐张隔离）
    p2, llm2 = make_pipeline({"bad": NOT_IMAGE, "good": JPEG})
    r2 = await p2.describe(["bad", "good"], llm=llm2)
    assert r2.ok and r2.images_used == 1 and r2.images_total == 2, r2
    assert llm2.calls == 1
    print("✅ test_bad_image_skipped_and_all_failed PASS")


async def test_empty_response_marks_failure():
    p, llm = make_pipeline({"u1": PNG}, reply="")
    r = await p.describe(["u1"], llm=llm)
    assert not r.ok and r.reason == "empty_response", r
    assert r.description == ""
    # 失败不写缓存：下次还会重试（而不是永久记住"没看清"）
    r2 = await p.describe(["u1"], llm=llm)
    assert llm.calls == 2, "空结果不该被缓存"
    print("✅ test_empty_response_marks_failure PASS")


async def test_no_llm_or_no_image():
    p, llm = make_pipeline({"u1": PNG})
    r = await p.describe(["u1"], llm=None)
    assert not r.ok and r.reason == "llm_missing"
    r2 = await p.describe([], llm=llm)
    assert not r2.ok and r2.reason == "no_image"
    r3 = await p.describe(["nope"], llm=llm)     # 下载器返回 None
    assert not r3.ok and r3.reason == "all_failed" and r3.skipped == ["download_failed"]
    print("✅ test_no_llm_or_no_image PASS")


async def _run_all():
    test_sniff_mime()
    await test_pipeline_records_metrics()
    await test_inline_and_describe()
    await test_cache_prevents_second_call()
    await test_large_image_falls_back_to_url()
    await test_bad_image_skipped_and_all_failed()
    await test_empty_response_marks_failure()
    await test_no_llm_or_no_image()


if __name__ == "__main__":
    print("=== 视觉管道回归测试（P0-3）===")
    try:
        asyncio.run(_run_all())
        print("\n🎉 全部通过：看图有校验、有内联、有缓存、有降级")
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
