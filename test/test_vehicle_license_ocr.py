#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
调用本地 OCR 服务的机动车行驶证接口（腾讯云 VehicleLicenseOCR）做联调测试（多线程并行请求）。

``card_side``：FRONT（主页）、BACK（副页）、DOUBLE（正副页同框）。

用法（在项目根目录）::

    python test/test_vehicle_license_ocr.py

默认请求 ``http://127.0.0.1:8080``，路由可通过环境变量覆盖::

    export OCR_SERVICE_BASE=http://<host>:8080
    export OCR_DRIVER_LICENSE_PATH=/api/v1/ocr/driver-license
    export OCR_TEST_MAX_WORKERS=30
    python test/test_vehicle_license_ocr.py
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
# 图片地址：支持 http(s) URL，或本机文件路径
# ---------------------------------------------------------------------------
VEHICLE_LICENSE_FRONT_IMAGES: List[str] = [
    # 行驶证主页（有红色印章的一面），card_side=FRONT
]

VEHICLE_LICENSE_BACK_IMAGES: List[str] = [
    # 行驶证副页（有号牌的一面等），card_side=BACK
]

VEHICLE_LICENSE_DOUBLE_IMAGES: List[str] = [
    # 正副页同框，card_side=DOUBLE
    "https://mingdetong-image-meterias.oss-cn-beijing.aliyuncs.com/driver-license/double/%E6%96%B0%E8%A1%8C%E9%A9%B6%E8%AF%81%20001.jpg",
    "https://mingdetong-image-meterias.oss-cn-beijing.aliyuncs.com/driver-license/double/%E6%96%B0%E8%A1%8C%E9%A9%B6%E8%AF%81.jpg",
    "https://mingdetong-image-meterias.oss-cn-beijing.aliyuncs.com/driver-license/double/%E8%A1%8C%E9%A9%B6%E8%AF%81%EF%BC%88%E5%8F%98%E6%9B%B4%E5%90%8E%EF%BC%89.jpg",
    "https://mingdetong-image-meterias.oss-cn-beijing.aliyuncs.com/driver-license/double/%E6%96%B0%E8%A1%8C%E9%A9%B6%E8%AF%811.jpg",
    "https://mingdetong-image-meterias.oss-cn-beijing.aliyuncs.com/driver-license/double/%E6%96%B0%E8%A1%8C%E9%A9%B6%E8%AF%812.jpg",
    "https://mingdetong-image-meterias.oss-cn-beijing.aliyuncs.com/driver-license/double/%E6%96%B0%E8%A1%8C%E9%A9%B6%E8%AF%813.jpg",
    "https://mingdetong-image-meterias.oss-cn-beijing.aliyuncs.com/driver-license/double/%E6%96%B0%E8%A1%8C%E9%A9%B6%E8%AF%814.jpg",
    "https://mingdetong-image-meterias.oss-cn-beijing.aliyuncs.com/driver-license/double/%E6%96%B0%E8%A1%8C%E9%A9%B6%E8%AF%815.jpg",
    "https://mingdetong-image-meterias.oss-cn-beijing.aliyuncs.com/driver-license/double/%E6%96%B0%E8%A1%8C%E9%A9%B6%E8%AF%816.jpg",
    "https://mingdetong-image-meterias.oss-cn-beijing.aliyuncs.com/driver-license/double/%E6%96%B0%E8%A1%8C%E9%A9%B6%E8%AF%817.jpg",
]

OCR_SERVICE_BASE = os.environ.get("OCR_SERVICE_BASE", "http://127.0.0.1:8080").rstrip("/")
DRIVER_LICENSE_PATH = os.environ.get(
    "OCR_DRIVER_LICENSE_PATH",
    "/api/v1/ocr/driver-license",
).rstrip("/")
if not DRIVER_LICENSE_PATH.startswith("/"):
    DRIVER_LICENSE_PATH = "/" + DRIVER_LICENSE_PATH

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


def _run_one_vehicle_license(pair: tuple[str, str]) -> None:
    image_ref, card_side = pair
    header = f"\n--- card_side={card_side!r} image={image_ref!r} ---"
    try:
        body = _build_body(image_ref, card_side)
    except (OSError, ValueError) as e:
        with _print_lock:
            print(f"{header}\n跳过: {e}", file=sys.stderr)
        return

    url = f"{OCR_SERVICE_BASE}{DRIVER_LICENSE_PATH}"
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
        *[(x, "FRONT") for x in VEHICLE_LICENSE_FRONT_IMAGES],
        *[(x, "BACK") for x in VEHICLE_LICENSE_BACK_IMAGES],
        *[(x, "DOUBLE") for x in VEHICLE_LICENSE_DOUBLE_IMAGES],
    ]
    if not pairs:
        print(
            "VEHICLE_LICENSE_FRONT_IMAGES / BACK / DOUBLE 均为空，请在 test_vehicle_license_ocr.py 中填写地址。",
            file=sys.stderr,
        )
        sys.exit(1)

    max_workers = min(_parse_max_workers(), len(pairs))
    print(f"OCR_SERVICE_BASE={OCR_SERVICE_BASE}", file=sys.stderr)
    print(f"DRIVER_LICENSE_PATH={DRIVER_LICENSE_PATH}", file=sys.stderr)
    print(f"OCR_TEST_MAX_WORKERS={max_workers} (并行任务数 {len(pairs)})", file=sys.stderr)

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(_run_one_vehicle_license, p) for p in pairs]
        for fut in as_completed(futures):
            fut.result()


if __name__ == "__main__":
    main()
