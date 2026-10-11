#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""复现 t_15e9ed46 根因：同一提示词下 gpt-5.6-luna 的三种输出形态。

不联网、不需要密钥：直接拿现网抓到的三个真实响应样本，验证
`ai_content._extract_json` 修复前后的判定差异。

    python3 docs/observations/t_15e9ed46/repro_three_shapes.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from tg_forwarder.core.ai_content import _extract_json  # noqa: E402

# 形态 ①：纯散文（2026-10-12 直连现网端点逐字捕获）
PROSE = (
    "你发来的是一则手游资源推广帖，内容包括：\n\n"
    "- 游戏：**开心消消乐 3.8.2**\n"
    "- 声称版本：**“免内购版 / 破解版”**\n"
    "- 包含 Telegram 频道、群聊、投稿入口\n"
    "- 提供网盘下载地址\n"
    "- 还带有微信加群领红包广告\n\n"
    "如果你希望我帮你做某件事（例如提取信息、判断是否可信、润色文案、翻译、总结、"
    "识别风险等），告诉我具体需求即可。"
)

# 形态 ②：合法 JSON 被包进 :::writing 信封 —— 本次事故的根因
ENVELOPE = (
    ':::writing{variant="standard" id="58321" title="审核结果"}\n'
    "{\n"
    '  "is_ad": true,\n'
    '  "ad_type": "resource_share",\n'
    '  "confidence": 0.99,\n'
    '  "cleaned_text": "#游戏 @PJAPK\\n\\n下载",\n'
    '  "reason": "游戏破解版属于正常资源分享，已删除频道尾巴及微信引流广告。"\n'
    "}\n"
    ":::"
)

# 形态 ③：裸 JSON（一直都能正常解析，作为对照）
BARE = '{\n  "is_ad": true,\n  "ad_type": "promo_group",\n  "confidence": 0.99\n}'


def old_extract_json(raw):
    """修复前的实现（首个 `{` 到末个 `}` 切片），用于对照。"""
    import json

    s = (raw or "").strip()
    if not s:
        return None
    try:
        data = json.loads(s)
        return data if isinstance(data, dict) else None
    except Exception:
        pass
    start, end = s.find("{"), s.rfind("}")
    if start != -1 and end > start:
        try:
            data = json.loads(s[start:end + 1])
            return data if isinstance(data, dict) else None
        except Exception:
            return None
    return None


CASES = [
    ("① 纯散文（模型跑偏）", PROSE),
    ("② :::writing 信封包裹的合法 JSON（根因）", ENVELOPE),
    ("③ 裸 JSON（对照）", BARE),
]


def main() -> int:
    print("形态".ljust(42), "旧解析器".ljust(16), "新解析器")
    print("-" * 78)
    envelope_recovered = False
    for name, raw in CASES:
        old = old_extract_json(raw)
        new = _extract_json(raw)
        print(
            name.ljust(42),
            ("解析成功" if old else "拒绝→放行").ljust(16),
            "解析成功" if new else "拒绝→放行",
        )
        if name.startswith("②") and new is not None:
            envelope_recovered = True

    print()
    assert envelope_recovered, "信封形态仍未解析——修复失效"
    assert _extract_json(PROSE) is None, "散文形态被误判成 JSON，解析层过宽"
    print("✓ 信封形态已恢复解析（根因修复生效）")
    print("✓ 散文形态仍返回 None（真无 JSON，交由重试兜底，未硬编造）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
