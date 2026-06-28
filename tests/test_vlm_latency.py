"""
VLM 时延对比测试：固定图片 + 固定问题，多模型对比。

用法：
    python tests/test_vlm_latency.py          # 默认每模型测 5 次
    python tests/test_vlm_latency.py 10       # 每模型测 10 次
"""

import sys
import os
import time
import base64
import requests

TEST_IMAGE = os.path.join(os.path.dirname(__file__), "..", "assets", "test_game_frame.jpg")
PROMPT = "这是什么游戏？"
SYSTEM = "你是一个游戏语音教练，用口语化的方式回答，1~2句话，不超过40字。"

MODELS = [
    {
        "name": "Gemini 3.1 Flash Lite",
        "url": "https://yunwu.ai/v1/chat/completions",
        "api_key": "sk-rDl2CSNC6PhNFcfnI2jGH7UGnORAhSmgXkgBfAq7cAz2rqKS",
        "model": "gemini-3.1-flash-lite:stable",
    },
]


def run_test(cfg: dict, image_b64: str, n: int) -> list[float]:
    headers = {
        "Authorization": f"Bearer {cfg['api_key']}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": cfg["model"],
        "messages": [
            {"role": "system", "content": SYSTEM},
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
                    {"type": "text", "text": PROMPT},
                ],
            },
        ],
        "max_tokens": 120,
        "temperature": 0.7,
    }

    latencies = []
    for i in range(1, n + 1):
        try:
            t0 = time.perf_counter()
            resp = requests.post(cfg["url"], headers=headers, json=payload, timeout=30)
            ms = (time.perf_counter() - t0) * 1000
            resp.raise_for_status()
            reply = resp.json()["choices"][0]["message"]["content"].strip()
            latencies.append(ms)
            print(f"    [{i}/{n}] {ms:.0f}ms  →  {reply}")
        except Exception as e:
            print(f"    [{i}/{n}] 失败  →  {e}")
    return latencies


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 5

    with open(TEST_IMAGE, "rb") as f:
        image_b64 = base64.b64encode(f.read()).decode()

    print(f"[VLM 对比测试] 问题: \"{PROMPT}\"  每模型: {n} 次\n")

    results = {}
    for cfg in MODELS:
        print(f"  ── {cfg['name']} ({cfg['model']}) ──")
        latencies = run_test(cfg, image_b64, n)
        results[cfg["name"]] = latencies
        if latencies:
            avg = sum(latencies) / len(latencies)
            print(f"    平均 {avg:.0f}ms | 最快 {min(latencies):.0f}ms | 最慢 {max(latencies):.0f}ms\n")
        else:
            print(f"    全部失败\n")

    # 汇总对比
    print("  ═══ 对比汇总 ═══")
    for name, lats in results.items():
        if lats:
            avg = sum(lats) / len(lats)
            print(f"    {name:.<30s} 平均 {avg:.0f}ms  (最快 {min(lats):.0f} / 最慢 {max(lats):.0f})")
        else:
            print(f"    {name:.<30s} 全部失败")


if __name__ == "__main__":
    main()
