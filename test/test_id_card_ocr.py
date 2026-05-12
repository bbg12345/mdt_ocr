#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
调用本地 OCR 服务的身份证接口做联调测试（多线程并行请求）。

用法（在项目根目录）::

    python test/test_id_card_ocr.py

默认请求 ``http://127.0.0.1:8080``，可通过环境变量覆盖::

    export OCR_SERVICE_BASE=http://<host>:8080
    export OCR_TEST_MAX_WORKERS=30
    python test/test_id_card_ocr.py
"""

from __future__ import annotations

import base64
import json
import os
import sys
import threading
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List

# ---------------------------------------------------------------------------
# 在此处填写图片地址：支持 http(s) URL，或本机文件路径（绝对/相对路径均可）
# ---------------------------------------------------------------------------
ID_CARD_FRONT_IMAGES: List[str] = [
    # 朱金磊 / 身份证正面（OSS 若为私有桶需带预签名；链接会过期）
    "https://mingdetong-image-meterias.oss-cn-beijing.aliyuncs.com/id-card/%E8%BA%AB%E4%BB%BD%E8%AF%81/%E6%9C%B1%E9%87%91%E7%A3%8A/%E8%BA%AB%E4%BB%BD%E8%AF%81%E6%AD%A3%E9%9D%A2.jpg",
    # 王兵兵 / 身份证正面
    "https://mingdetong-image-meterias.oss-cn-beijing.aliyuncs.com/id-card/%E8%BA%AB%E4%BB%BD%E8%AF%81/%E7%8E%8B%E5%85%B5%E5%85%B5/%E8%BA%AB%E4%BB%BD%E8%AF%81%E6%AD%A3%E9%9D%A2.jpg",
    # 王焕周 / 身份证正面
    "https://mingdetong-image-meterias.oss-cn-beijing.aliyuncs.com/id-card/%E8%BA%AB%E4%BB%BD%E8%AF%81/%E7%8E%8B%E7%84%95%E5%91%A8/%E7%8E%8B%E7%84%95%E5%91%A8%E8%BA%AB%E4%BB%BD%E8%AF%81%E6%AD%A3%E9%9D%A2.jpg",
    # 运天龙 / 身份证正面
    "https://mingdetong-image-meterias.oss-cn-beijing.aliyuncs.com/id-card/%E8%BA%AB%E4%BB%BD%E8%AF%81/%E8%BF%90%E5%A4%A9%E9%BE%99/%E8%BA%AB%E4%BB%BD%E8%AF%81%E6%AD%A3%E9%9D%A2.jpg",
    # 黄金凯 / 身份证正面
    "https://mingdetong-image-meterias.oss-cn-beijing.aliyuncs.com/id-card/%E8%BA%AB%E4%BB%BD%E8%AF%81/%E9%BB%84%E9%87%91%E5%87%AF/%E8%BA%AB%E4%BB%BD%E8%AF%81%E6%AD%A3%E9%9D%A2.jpg",
]

ID_CARD_BACK_IMAGES: List[str] = [
    # 朱金磊 / 身份证反面
    "https://mingdetong-image-meterias.oss-cn-beijing.aliyuncs.com/id-card/%E8%BA%AB%E4%BB%BD%E8%AF%81/%E6%9C%B1%E9%87%91%E7%A3%8A/%E8%BA%AB%E4%BB%BD%E8%AF%81%E5%8F%8D%E9%9D%A2.jpg",
    # 王兵兵 / 身份证反面
    "https://mingdetong-image-meterias.oss-cn-beijing.aliyuncs.com/id-card/%E8%BA%AB%E4%BB%BD%E8%AF%81/%E7%8E%8B%E5%85%B5%E5%85%B5/%E8%BA%AB%E4%BB%BD%E8%AF%81%E5%8F%8D%E9%9D%A2.jpg",
    # 王焕周 / 身份证反面
    "https://mingdetong-image-meterias.oss-cn-beijing.aliyuncs.com/id-card/%E8%BA%AB%E4%BB%BD%E8%AF%81/%E7%8E%8B%E7%84%95%E5%91%A8/%E7%8E%8B%E7%84%95%E5%91%A8%E8%BA%AB%E4%BB%BD%E8%AF%81%E5%8F%8D%E9%9D%A2.jpg",
    # 运天龙 / 身份证反面
    "https://mingdetong-image-meterias.oss-cn-beijing.aliyuncs.com/id-card/%E8%BA%AB%E4%BB%BD%E8%AF%81/%E8%BF%90%E5%A4%A9%E9%BE%99/%E8%BA%AB%E4%BB%BD%E8%AF%81%E5%8F%8D%E9%9D%A2.jpg",
    # 黄金凯 / 身份证反面
    "https://mingdetong-image-meterias.oss-cn-beijing.aliyuncs.com/id-card/%E8%BA%AB%E4%BB%BD%E8%AF%81/%E9%BB%84%E9%87%91%E5%87%AF/%E8%BA%AB%E4%BB%BD%E8%AF%81%E5%8F%8D%E9%9D%A2.jpg",
]

OCR_SERVICE_BASE = os.environ.get("OCR_SERVICE_BASE", "http://127.0.0.1:8080").rstrip("/")
IDCARD_PATH = "/api/v1/ocr/idcard"

_print_lock = threading.Lock()


def _parse_max_workers() -> int:
    try:
        return max(1, int(os.environ.get("OCR_TEST_MAX_WORKERS", "30")))
    except ValueError:
        return 30


def _build_body(image_ref: str, card_side: str) -> Dict[str, Any]:
    ref = image_ref.strip()
    if ref.startswith(("http://", "https://")):
        return {"image_url": ref, "card_side": card_side}
    p = Path(ref).expanduser()
    if not p.is_file():
        raise FileNotFoundError(f"不是有效文件路径: {image_ref}")
    raw = p.read_bytes()
    if not raw:
        raise ValueError(f"文件为空: {p}")
    b64 = base64.b64encode(raw).decode("ascii")
    return {"image_base64": b64, "card_side": card_side}


def _run_one_idcard(pair: tuple[str, str]) -> None:
    image_ref, card_side = pair
    header = f"\n--- card_side={card_side!r} image={image_ref!r} ---"
    try:
        body = _build_body(image_ref, card_side)
    except (OSError, ValueError) as e:
        with _print_lock:
            print(f"{header}\n跳过: {e}", file=sys.stderr)
        return

    url = f"{OCR_SERVICE_BASE}{IDCARD_PATH}"
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            text = resp.read().decode("utf-8", errors="replace")
            try:
                obj = json.loads(text)
                out = json.dumps(obj, ensure_ascii=False, indent=2)
            except json.JSONDecodeError:
                out = text
        with _print_lock:
            print(header, file=sys.stderr)
            print(out)
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        with _print_lock:
            print(header, file=sys.stderr)
            print(f"HTTP {e.code} {e.reason}", file=sys.stderr)
            print(err_body, file=sys.stderr)


def main() -> None:
    pairs: List[tuple[str, str]] = [
        *[(x, "FRONT") for x in ID_CARD_FRONT_IMAGES],
        *[(x, "BACK") for x in ID_CARD_BACK_IMAGES],
    ]
    if not pairs:
        print(
            "ID_CARD_FRONT_IMAGES 与 ID_CARD_BACK_IMAGES 均为空，请在 test_id_card_ocr.py 中填写地址后重试。",
            file=sys.stderr,
        )
        sys.exit(1)

    max_workers = min(_parse_max_workers(), len(pairs))
    print(f"OCR_SERVICE_BASE={OCR_SERVICE_BASE}", file=sys.stderr)
    print(f"OCR_TEST_MAX_WORKERS={max_workers} (并行任务数 {len(pairs)})", file=sys.stderr)

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(_run_one_idcard, p) for p in pairs]
        for fut in as_completed(futures):
            fut.result()


if __name__ == "__main__":
    main()
