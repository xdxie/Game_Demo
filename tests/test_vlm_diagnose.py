"""
VLM 请求时延诊断：拆解 DNS / 连接 / SSL / 首字节 / 传输 各阶段耗时。

用法：
    python tests/test_vlm_diagnose.py
"""

import os
import time
import base64
import socket
import requests
from urllib.parse import urlparse

URL = "https://yunwu.ai/v1/chat/completions"
API_KEY = "sk-rDl2CSNC6PhNFcfnI2jGH7UGnORAhSmgXkgBfAq7cAz2rqKS"
MODEL = "gemini-3.1-flash-lite:stable"
TEST_IMAGE = os.path.join(os.path.dirname(__file__), "..", "assets", "test_game_frame.jpg")


def test_dns():
    host = urlparse(URL).hostname
    t0 = time.perf_counter()
    ip = socket.gethostbyname(host)
    ms = (time.perf_counter() - t0) * 1000
    print(f"  DNS 解析：{host} → {ip}（{ms:.0f}ms）")
    return ip


def test_image_size():
    size_kb = os.path.getsize(TEST_IMAGE) / 1024
    with open(TEST_IMAGE, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    b64_kb = len(b64) / 1024
    print(f"  图片大小：原始 {size_kb:.0f}KB → base64 {b64_kb:.0f}KB")
    return b64


def test_connection():
    """测试纯 HTTPS 连接耗时（不发请求体）"""
    session = requests.Session()
    t0 = time.perf_counter()
    resp = session.get("https://yunwu.ai", timeout=10, allow_redirects=False)
    ms = (time.perf_counter() - t0) * 1000
    print(f"  HTTPS 连接：{ms:.0f}ms（状态码 {resp.status_code}）")
    session.close()


def test_text_only():
    """纯文本请求（无图片），对比基线"""
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "user", "content": "你好"},
        ],
        "max_tokens": 20,
    }
    headers = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}
    t0 = time.perf_counter()
    resp = requests.post(URL, headers=headers, json=payload, timeout=30)
    ms = (time.perf_counter() - t0) * 1000
    resp.raise_for_status()
    reply = resp.json()["choices"][0]["message"]["content"].strip()
    print(f"  纯文本请求：{ms:.0f}ms  →  {reply}")
    return ms


def test_with_image(image_b64: str):
    """带图片请求"""
    payload = {
        "model": MODEL,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
                    {"type": "text", "text": "这是什么游戏？"},
                ],
            },
        ],
        "max_tokens": 60,
    }
    headers = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}
    t0 = time.perf_counter()
    resp = requests.post(URL, headers=headers, json=payload, timeout=30)
    ms = (time.perf_counter() - t0) * 1000
    resp.raise_for_status()
    reply = resp.json()["choices"][0]["message"]["content"].strip()
    print(f"  图文请求：{ms:.0f}ms  →  {reply}")
    return ms


def test_reuse_connection(image_b64: str):
    """复用连接测 3 次，看首次 vs 后续差异"""
    headers = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": MODEL,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
                    {"type": "text", "text": "这是什么游戏？"},
                ],
            },
        ],
        "max_tokens": 60,
    }
    session = requests.Session()
    for i in range(1, 4):
        t0 = time.perf_counter()
        resp = session.post(URL, headers=headers, json=payload, timeout=30)
        ms = (time.perf_counter() - t0) * 1000
        resp.raise_for_status()
        reply = resp.json()["choices"][0]["message"]["content"].strip()
        tag = "首次（含握手）" if i == 1 else f"第{i}次（复用连接）"
        print(f"    [{tag}] {ms:.0f}ms  →  {reply}")
    session.close()


def main():
    print("[VLM 时延诊断]\n")

    print("1. DNS 解析")
    test_dns()

    print("\n2. HTTPS 连接")
    test_connection()

    print("\n3. 图片大小")
    image_b64 = test_image_size()

    print("\n4. 纯文本 vs 图文（判断图片上传开销）")
    text_ms = test_text_only()
    image_ms = test_with_image(image_b64)
    print(f"  差值：{image_ms - text_ms:.0f}ms（图片额外开销）")

    print("\n5. 连接复用测试（3 次，看首次 vs 后续）")
    test_reuse_connection(image_b64)

    print("\n诊断完成")


if __name__ == "__main__":
    main()
