#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""逐条独立测量 F14 判定率与模型输出形态分布（t_15e9ed46 验收 A3 的量化依据）。

**每条消息内容都带唯一序号**，避免 TranslationCache 命中把「缓存返回」误当成
「新判定成功」——初版实测 8/8 就是这么翻车的（7/8 是缓存命中）。

密钥从环境变量读，**不落盘**：
    export AXONHUB_API_KEY=...
    python3 docs/observations/t_15e9ed46/measure_shape_rate.py [条数] [端点] [模型]
"""
import asyncio
import json
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from loguru import logger  # noqa: E402

from tg_forwarder.core import ai_content as ac  # noqa: E402

logger.remove()  # 逐条 WARNING 太吵，结果走 stdout

# 真实形态的广告消息（频道/群聊/投稿引流尾巴 + 网盘下载链接），{i} 保证每条唯一
AD = (
    "🎮：**开心消消乐**_3.8.2_免内购版\n⭐️ 非常好玩的休闲消除游戏，破解版下载\n"
    "📥 **下载地址**：https://pan.example.com/s/abc{i}\n"
    "📢 广告：加微信 xxxx123 进群领红包\n"
    "**👥** [频道](https://t.me/PJAPK) **💬** [群聊](https://t.me/PJAPKChat)"
)
NORMAL = (
    "📚 **Python 3.13 官方文档**整理\n"
    "本次更新要点：更好的错误提示、GIL 构建选项、类型参数默认值支持。\n"
    "详见 https://docs.python.org/3.13/whatsnew/{i}"
)


def raw_shape(raw):
    """只看原始串形态——不经解析器，否则修复后的解析会把信封也判成成功，
    就看不出「信封」与「散文」各自占多少了。"""
    if raw is None:
        return "network_fail"
    s = (raw or "").strip()
    if not s:
        return "empty"
    if ":::writing{" in s or s.startswith(":::"):
        return "envelope"
    if s.startswith(("{", "[")):
        return "bare_json"
    return "prose"


async def run(tmpl, n, retries, base_url, model, api_key):
    p = ac.AiContentProcessor(
        base_url=base_url, model=model, api_key=api_key,
        max_retries=retries, retry_backoff=0.3,
    )
    shapes, judged = Counter(), 0
    for i in range(n):
        seen, orig = [], p._call

        async def spy(t, _o=orig, _s=seen):
            r = await _o(t)
            _s.append(r)
            return r

        p._call = spy
        v = await p.process(tmpl.format(i=i))
        p._call = orig
        for r in seen:
            shapes[raw_shape(r)] += 1
        if v.confidence > 0:  # 出分 = 真判定（默认 Verdict 的 confidence 恒为 0）
            judged += 1
    return {"shapes": dict(shapes), "judged": judged, "total": n}


async def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 12
    base_url = sys.argv[2] if len(sys.argv) > 2 else ac.DEFAULT_BASE_URL
    model = sys.argv[3] if len(sys.argv) > 3 else "gpt-5.6-luna"
    api_key = os.environ.get("AXONHUB_API_KEY", "")
    if not api_key:
        print("需要 AXONHUB_API_KEY 环境变量（不落盘）", file=sys.stderr)
        return 2

    out = {}
    for name, tmpl in (("ad", AD), ("normal", NORMAL)):
        for retries in (0, 2):
            r = await run(tmpl, n, retries, base_url, model, api_key)
            r["judge_rate"] = f"{r['judged']}/{r['total']}"
            out[f"{name}_max_retries{retries}"] = r
            print(f"{name:7} retries={retries}: 判定 {r['judge_rate']} | 形态 {r['shapes']}",
                  flush=True)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
