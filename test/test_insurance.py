#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
车险保单相关测试数据（URL 测试集）与 **已启动的 OCR HTTP 服务** 联调。

- 常量 ``COMMERCIAL_INSURANCE_PDF_URLS`` / ``COMPULSORY_INSURANCE_PDF_URLS`` 供其它模块引用。
- 列表末项可为 **本机绝对路径** PDF（如新版渤海样张）；跑全量前请确认该文件存在，否则接口会报无法读取 PDF。
- 可执行用例：对交强险、商业险 **列表中的每个 PDF** 各向服务 ``POST`` 车险抽取路径（默认 ``/api/v1/ocr/car-insurance-extract``，JSON 字段 ``type``、``pdf_url``），每条结束后打印日志；pytest 中 **assert 集中在用例末尾**（汇总失败项）。

**须先启动服务**（默认 ``http://127.0.0.1:8080``）::

    source ../venv/bin/activate
    uvicorn ocr_service:app --host 0.0.0.0 --port 8080

可选环境变量（与 ``ocr_service`` 一致）::

    export OCR_SERVICE_BASE=http://127.0.0.1:8080
    export OCR_CAR_INSURANCE_PATH=/api/v1/ocr/car-insurance-extract

用法（在项目根目录）::

    source ../venv/bin/activate
    python test/test_insurance.py

使用 pytest（需安装 pytest，且加 ``-s`` 才能看到 print 输出）::

    pytest test/test_insurance.py -s

服务不可达时 pytest 会 **skip**；命令行 ``main()`` 会打印错误并退出非 0。

**只跑部分用例（三选一）**：

1. 编辑本文件下方 ``COMPULSORY_RUN_INDICES`` / ``COMMERCIAL_RUN_INDICES``：设为 ``None`` 跑全部；设为 ``{3, 5}`` 只跑第 3、5 条（序号与日志 ``[i/n]`` 的 ``i`` 一致）。
2. 命令行：``pytest test/test_insurance.py::test_car_insurance_compulsory_extract -s`` 只跑交强险；或用 ``-k compulsory`` 按名称筛选。
3. 临时注释掉 ``*_INSURANCE_PDF_URLS`` 里不需要的 URL（需同步改期望元组长度，一般不推荐）。
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Final, List, Literal, Optional, Set

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from car_insurance import _CAR_INSURANCE_COMMERCIAL_ONLY_KV_KEYS

OCR_SERVICE_BASE = os.environ.get("OCR_SERVICE_BASE", "http://127.0.0.1:9527").rstrip("/")
_OCR_CAR_PATH_RAW = os.environ.get(
    "OCR_CAR_INSURANCE_PATH",
    "/api/v1/ocr/car-insurance-extract",
).strip()
OCR_CAR_INSURANCE_PATH = (
    _OCR_CAR_PATH_RAW if _OCR_CAR_PATH_RAW.startswith("/") else f"/{_OCR_CAR_PATH_RAW}"
)

_BASE: Final[str] = "https://mingdetong-image-meterias.oss-cn-beijing.aliyuncs.com/insurance"
# 新版渤海商业险（本地）；与 OSS 上 ``bohai-m.PDF`` 为不同文件，期望见列表末项对应元组
COMMERCIAL_SAMPLE_BOHAI_NEW_PDF: Final[str] = (
    "/Users/junma/Downloads/car-insurance/new/bohai-m.pdf"
)
COMMERCIAL_SAMPLE_RENMIN_NEW_PDF: Final[str] = (
    "/Users/junma/Downloads/car-insurance/new/renmin-m.pdf"
)
COMMERCIAL_SAMPLE_RENSHOU_NEW_PDF: Final[str] = (
    "/Users/junma/Downloads/car-insurance/new/renshou-m.pdf"
)
COMPULSORY_SAMPLE_RENSHOU_NEW_PDF: Final[str] = (
    "/Users/junma/Downloads/car-insurance/new/renshou-f.pdf"
)

# ---------------------------------------------------------------------------
# 商业险（m/m）PDF 测试集
# ---------------------------------------------------------------------------
COMMERCIAL_INSURANCE_PDF_URLS: List[str] = [
    f"{_BASE}/m/m/bohai-m.PDF",
    f"{_BASE}/m/m/huaan-m.pdf",
    f"{_BASE}/m/m/pingan-m.pdf",
    f"{_BASE}/m/m/renmin-m.pdf",
    f"{_BASE}/m/m/renshou-m.pdf",
    f"{_BASE}/m/m/renshou-m2.pdf",
    f"{_BASE}/m/m/taikang-m.pdf",
    f"{_BASE}/m/m/taipingyang-m.pdf",
    f"{_BASE}/m/m/yanguang-m.pdf",
    f"{_BASE}/m/m/zhongyin-m.pdf",
    COMMERCIAL_SAMPLE_BOHAI_NEW_PDF,
    COMMERCIAL_SAMPLE_RENMIN_NEW_PDF,
    COMMERCIAL_SAMPLE_RENSHOU_NEW_PDF,
]

# 与 ``COMMERCIAL_INSURANCE_PDF_URLS`` 同序；本地 ``…/renmin-m.pdf`` 与 OSS 末段同名，pytest id 须区分
_COMMERCIAL_INSURANCE_PDF_PYTEST_IDS: tuple[str, ...] = (
    "bohai-m.PDF",
    "huaan-m.pdf",
    "pingan-m.pdf",
    "renmin-m.pdf",
    "renshou-m.pdf",
    "renshou-m2.pdf",
    "taikang-m.pdf",
    "taipingyang-m.pdf",
    "yanguang-m.pdf",
    "zhongyin-m.pdf",
    "new-bohai-m.pdf",
    "new-renmin-m.pdf",
    "new-renshou-m.pdf",
)

# 与 ``COMMERCIAL_INSURANCE_PDF_URLS`` 同序：期望 ``kv["保险公司名称"]``
COMMERCIAL_EXPECTED_INSURER_NAMES: tuple[str, ...] = (
    "渤海财产保险股份有限公司天津分公司滨海支公司",
    "华安财产保险股份有限公司天津分公司宝坻支公司",
    "中国平安财产保险股份有限公司天津市南开支公司",
    "中国人民财产保险股份有限公司天津市南开支公司",
    "中国人寿财产保险股份有限公司天津市分公司",
    "中国人寿财产保险股份有限公司天津市分公司",
    "泰康在线财产保险股份有限公司",
    "中国太平洋财产保险股份有限公司",
    "阳光财产保险股份有限公司天津市分公司第二营销服务部",
    "中银保险有限公司天津分公司",
    "渤海财产保险股份有限公司天津分公司滨海支公司",
    "中国人民财产保险股份有限公司天津市南开支公司",
    "中国人寿财产保险股份有限公司天津市直属营业部",
)

# 与 ``COMMERCIAL_INSURANCE_PDF_URLS`` 同序：期望 ``kv["保险期间"]``
COMMERCIAL_EXPECTED_PERIODS: tuple[Dict[str, str], ...] = (
    {"start": "2025-05-29T00:00:00", "end": "2026-05-29T00:00:00"},
    {"start": "2025-06-04T00:00:00", "end": "2026-06-04T00:00:00"},
    {"start": "2025-05-30T00:00:00", "end": "2026-05-29T24:00:00"},
    {"start": "2026-03-09T15:30:00", "end": "2027-03-09T24:00:00"},
    {"start": "2025-12-26T18:00:00", "end": "2026-12-26T24:00:00"},
    {"start": "2025-11-19T00:00:00", "end": "2026-11-18T24:00:00"},
    {"start": "2025-07-20T00:00:00", "end": "2026-07-19T24:00:00"},
    {"start": "2025-05-18T00:00:00", "end": "2026-05-17T24:00:00"},  # 修正空格：2025-05-18 T00:00:00 -> 2025-05-18T00:00:00
    {"start": "2025-11-30T00:00:00", "end": "2026-11-29T24:00:00"},
    {"start": "2025-08-30T00:00:00", "end": "2026-08-30T00:00:00"},
    {"start": "2026-03-07T00:00:00", "end": "2027-03-07T00:00:00"},
    {"start": "2026-03-27T00:00:00", "end": "2027-03-26T24:00:00"},
    # 与 new/renshou-f 交强险样张同期别；止日按一年期记为 2027-03-13T24:00:00
    {"start": "2026-03-14T00:00:00", "end": "2027-03-13T24:00:00"},
)
COMMERCIAL_EXPECTED_INSURED_NAMES: tuple[str, ...] = (
    "嗨车购（天津）融资租赁有限公司",  # bohai-m.PDF
    "嗨车购（天津）融资租赁有限公司",  # huaan-m.pdf
    "嗨车购（天津）融资租赁有限公司",  # pingan-m.pdf
    "嗨车购（天津）融资租赁有限公司",  # renmin-m.pdf
    "天津明德通汽车租赁有限公司",      # renshou-m.pdf
    "天津明德通汽车租赁有限公司",      # renshou-m2.pdf
    "嗨车购（天津）融资租赁有限公司",  # taikang-m.pdf
    "嗨车购（天津）融资租赁有限公司",  # taipingyang-m.pdf
    "嗨车购（天津）融资租赁有限公司",  # yanguang-m.pdf
    "嗨车购（天津）融资租赁有限公司",  # zhongyin-m.pdf
    "嗨车购（天津）融资租赁有限公司",  # new/bohai-m.pdf（本地）
    "嗨车购（天津）融资租赁有限公司",  # new/renmin-m.pdf（本地）
    "天津明德通汽车租赁有限公司",  # new/renshou-m.pdf（本地）
)

# 与 ``COMMERCIAL_INSURANCE_PDF_URLS`` 同序：期望 ``kv["签单日期"]``（YYYY-MM-DD）
COMMERCIAL_EXPECTED_SIGN_DATES: tuple[str, ...] = (
    "2025-05-28",  # bohai-m.PDF
    "2025-06-03",  # huaan-m.pdf
    "2025-04-15",  # pingan-m.pdf
    "2026-03-09",  # renmin-m.pdf
    "2025-12-26",  # renshou-m.pdf
    "2025-11-18",  # renshou-m2.pdf
    "2025-05-28",  # taikang-m.pdf
    "2025-04-16",  # taipingyang-m.pdf
    "2025-11-24",  # yanguang-m.pdf
    "2025-08-25",  # zhongyin-m.pdf
    "2026-03-05",  # new/bohai-m.pdf（本地）
    "2026-03-16",  # new/renmin-m.pdf（本地）
    "2026-03-06",  # new/renshou-m.pdf（本地）
)

# 与 ``COMMERCIAL_INSURANCE_PDF_URLS`` 同序：期望 ``kv["保险费合计"]``
COMMERCIAL_EXPECTED_PREMIUM_TOTALS: tuple[str, ...] = (
    "8922.32",  # bohai-m.PDF
    "3988.10",  # huaan-m.pdf
    "6559.72",  # pingan-m.pdf
    "6774.16",  # renmin-m.pdf
    "8447.68",  # renshou-m.pdf
    "7839.81",  # renshou-m2.pdf
    "4283.13",  # taikang-m.pdf
    "4945.32",  # taipingyang-m.pdf
    "4976.76",  # yanguang-m.pdf
    "5992.03",  # zhongyin-m.pdf
    "2083.55",  # new/bohai-m.pdf（本地）
    "6703.62",  # new/renmin-m.pdf（本地）
    "2850.07",  # new/renshou-m.pdf（本地）
)

# 与 ``COMMERCIAL_INSURANCE_PDF_URLS`` 同序：商业险专属键期望，每行 9 项对应
# ``_CAR_INSURANCE_COMMERCIAL_ONLY_KV_KEYS`` 顺序。
# 未命中承保险种锚点、或无法从原文解析时一律 ``""``（不再用人保/太平洋版式补 ``0``）；
# 原文或 LLM 可解析出的金额（含数值 0）仍为 ``0``（非 ``0.00``）。
COMMERCIAL_EXPECTED_COMMERCIAL_ONLY_KV: tuple[tuple[str, ...], ...] = (
    (
        "2384.84",
        "94000.00",
        "4270.10",
        "1500000.00",
        "616.56",
        "150000.00",
        "1528.52",
        "4座*150000元",
        "",
    ),
    (  # huaan-m.pdf：无车损险明细行；三者/司机/乘客免赔额列为 0，不回填车损免赔额
        "",
        "",
        "3416.08",
        "1500000.00",
        "164.42",
        "50000.00",
        "407.60",
        "4座*50000元",
        "",
    ),
    (
        "3070.13",
        "91852.80",
        "2989.07",
        "1500000.00",
        "143.87",
        "50000.00",
        "356.65",
        "4座 x5万元/座",
        "0",
    ),
    (
        "2662.21",
        "108800.00",
        "4111.95",
        "1500000.00",
        "",
        "",
        "",
        "",
        "",
    ),
    (
        "2747.54",
        "86800.00",
        "4270.10",
        "1500000.00",
        "411.03",
        "100000.00",
        "1019.01",
        "4座*100000元",
        "0",
    ),
    (
        "2139.67",
        "69800.00",
        "4270.10",
        "1500000.00",
        "411.03",
        "100000.00",
        "1019.01",
        "4座*100000元",  # 与 renshou-m.pdf 一致：抽取层将「元/座 *N座」规范为「N座*金额元」
        "0",
    ),
    (
        "1704.52",
        "49455.90",
        "2135.05",
        "1500000.00",
        "102.76",
        "50000.00",
        "254.75",
        "5万元/座 * 4座",
        "",
    ),
    (
        "1704.52",
        "61313.00",
        "1810.76",
        "1000000.00",
        "411.03",
        "200000.00",
        "1019.01",
        "4座*200000元",
        "",
    ),
    (
        "1560.68",
        "93716.60",
        "3416.08",
        "1500000.00",
        "",
        "",
        "",
        "",
        "2000.00",
    ),  # yanguang-m.pdf：承保险种块仅车损/三者；全文无车上人员行，司机/乘客以空串为真实可抽取结果；免赔额见车损行第二列
    (
        "2149.70",
        "85979.00",
        "2878.37",
        "1500000.00",
        "277.07",
        "100000.00",
        "686.89",
        "4座*100000元",
        "0",
    ),
    (  # new/bohai-m.pdf（本地）：渤海走明细 pass2（豆包）；未配置或未成抽时车损/三者等为空串；免赔额无单独格为 ""
        "",
        "",
        "1976.90",
        "1500000.00",
        "19.03",
        "10000.00",
        "47.18",
        "4座*10000元",
        "",
    ),
    (  # new/renmin-m.pdf（本地）：仅车损/三者；车上人员与免赔额为 ""
        "3034.49",
        "69977.60",
        "3669.13",
        "1500000.00",
        "",
        "",
        "",
        "",
        "",
    ),
    (  # new/renshou-m.pdf（本地）；车损无格为 ""；乘客保额经规范为「4座*100000元」
        "",
        "",
        "2135.05",
        "1500000.00",
        "205.52",
        "100000.00",
        "509.50",
        "4座*100000元",
        "",
    ),
)

# ---------------------------------------------------------------------------
# 交强险（f/f）PDF 测试集
# ---------------------------------------------------------------------------
COMPULSORY_INSURANCE_PDF_URLS: List[str] = [
    f"{_BASE}/f/f/bohai-f.PDF",
    f"{_BASE}/f/f/pingan-f.pdf",
    f"{_BASE}/f/f/renmin-f-2.pdf",
    f"{_BASE}/f/f/renmin-f-3.pdf",
    f"{_BASE}/f/f/renmin-f-4.pdf",
    f"{_BASE}/f/f/renmin-f.pdf",
    f"{_BASE}/f/f/renshou-f.pdf",
    f"{_BASE}/f/f/taikang-f.pdf",
    f"{_BASE}/f/f/taipingyang-f-insurance.pdf",
    f"{_BASE}/f/f/zhongyin-f.pdf",
    COMPULSORY_SAMPLE_RENSHOU_NEW_PDF,
]

# 与 ``COMPULSORY_INSURANCE_PDF_URLS`` 同序；本地与 OSS 均有 ``renshou-f.pdf`` 末段，pytest id 须区分
_COMPULSORY_INSURANCE_PDF_PYTEST_IDS: tuple[str, ...] = (
    "bohai-f.PDF",
    "pingan-f.pdf",
    "renmin-f-2.pdf",
    "renmin-f-3.pdf",
    "renmin-f-4.pdf",
    "renmin-f.pdf",
    "renshou-f.pdf",
    "taikang-f.pdf",
    "taipingyang-f-insurance.pdf",
    "zhongyin-f.pdf",
    "new-renshou-f.pdf",
)

# 与 ``COMPULSORY_INSURANCE_PDF_URLS`` 同序：期望 ``kv["保险公司名称"]``
COMPULSORY_EXPECTED_INSURER_NAMES: tuple[str, ...] = (
    "渤海财产保险股份有限公司天津分公司滨海支公司",
    "中国平安财产保险股份有限公司天津市南开支公司",
    "中国人民财产保险股份有限公司天津市南开支公司",
    "中国人民财产保险股份有限公司天津市滨海支公司",
    "中国人民财产保险股份有限公司天津市滨海支公司",
    "中国人民财产保险股份有限公司天津市宁河支公司",
    "中国人寿财产保险股份有限公司天津市分公司",
    "泰康在线财产保险股份有限公司",
    "中国太平洋财产保险股份有限公司",
    "中银保险有限公司天津分公司",
    "中国人寿财产保险股份有限公司天津市直属营业部",
)

# 与 ``COMPULSORY_INSURANCE_PDF_URLS`` 同序：期望 ``kv["保险期间"]``
COMPULSORY_EXPECTED_PERIODS: tuple[Dict[str, str], ...] = (
    {"start": "2025-05-29T00:00:00", "end": "2026-05-29T00:00:00"},
    {"start": "2025-05-18T00:00:00", "end": "2026-05-17T24:00:00"},
    {"start": "2025-12-13T12:00:00", "end": "2026-12-13T12:00:00"},
    {"start": "2025-08-23T00:00:00", "end": "2026-08-22T24:00:00"},
    {"start": "2025-07-20T00:00:00", "end": "2026-07-19T24:00:00"},
    {"start": "2025-11-07T00:00:00", "end": "2026-11-06T24:00:00"},
    {"start": "2025-11-19T00:00:00", "end": "2026-11-18T24:00:00"},
    {"start": "2025-07-09T00:00:00", "end": "2026-07-08T24:00:00"},
    {"start": "2025-04-19T00:00:00", "end": "2026-04-18T24:00:00"},
    {"start": "2025-08-28T00:00:00", "end": "2026-08-28T00:00:00"},
    # 起止文案为 2026-03-14 至次年 3-13 24 时；用户笔误写为 2026-03-13 则止日早于起日，此处按一年期修正为 2027-03-13
    {"start": "2026-03-14T00:00:00", "end": "2027-03-13T24:00:00"},
)
COMPULSORY_EXPECTED_INSURED_NAMES: tuple[str, ...] = (
    "嗨车购（天津）融资租赁有限公司",  # bohai-f.PDF
    "嗨车购（天津）融资租赁有限公司",  # pingan-f.pdf
    "嗨车购（天津）融资租赁有限公司",  # renmin-f-2.pdf
    "嗨车购（天津）融资租赁有限公司",  # renmin-f-3.pdf
    "嗨车购（天津）融资租赁有限公司",  # renmin-f-4.pdf
    "天津明德通汽车租赁有限公司",      # renmin-f.pdf
    "天津明德通汽车租赁有限公司",      # renshou-f.pdf（与 renshou 商业险一致；pass1 取先出现的已知公司名块）
    "嗨车购（天津）融资租赁有限公司",  # taikang-f.pdf
    "嗨车购（天津）融资租赁有限公司",  # taipingyang-f-insurance.pdf
    "嗨车购（天津）融资租赁有限公司",  # zhongyin-f.pdf
    "天津明德通汽车租赁有限公司",  # new/renshou-f.pdf（本地）
)

# 与 ``COMPULSORY_INSURANCE_PDF_URLS`` 同序：期望 ``kv["签单日期"]``（YYYY-MM-DD）
COMPULSORY_EXPECTED_SIGN_DATES: tuple[str, ...] = (
    "2025-05-28",  # bohai-f.PDF
    "2025-04-15",  # pingan-f.pdf
    "2025-12-02",  # renmin-f-2.pdf
    "2025-08-11",  # renmin-f-3.pdf
    "2025-07-19",  # renmin-f-4.pdf
    "2025-11-06",  # renmin-f.pdf
    "2025-11-18",  # renshou-f.pdf
    "2025-05-28",  # taikang-f.pdf
    "2025-04-16",  # taipingyang-f-insurance.pdf
    "2025-08-25",  # zhongyin-f.pdf
    "2026-03-06",  # new/renshou-f.pdf（本地）
)

# 与 ``COMPULSORY_INSURANCE_PDF_URLS`` 同序：期望 ``kv["保险费合计"]``
COMPULSORY_EXPECTED_PREMIUM_TOTALS: tuple[str, ...] = (
    "1800.00",  # bohai-f.PDF
    "1350.00",  # pingan-f.pdf
    "1350.00",  # renmin-f-2.pdf
    "1800.00",  # renmin-f-3.pdf
    "1800.00",  # renmin-f-4.pdf
    "1800.00",  # renmin-f.pdf
    "1800.00",  # renshou-f.pdf
    "1170.00",  # taikang-f.pdf
    "1170.00",  # taipingyang-f-insurance.pdf
    "1350.00",  # zhongyin-f.pdf
    "1170.00",  # new/renshou-f.pdf（本地）
)

assert len(COMPULSORY_INSURANCE_PDF_URLS) == len(COMPULSORY_EXPECTED_INSURER_NAMES)
assert len(COMPULSORY_INSURANCE_PDF_URLS) == len(COMPULSORY_EXPECTED_PERIODS)
assert len(COMPULSORY_INSURANCE_PDF_URLS) == len(COMPULSORY_EXPECTED_INSURED_NAMES)
assert len(COMPULSORY_INSURANCE_PDF_URLS) == len(COMPULSORY_EXPECTED_SIGN_DATES)
assert len(COMPULSORY_INSURANCE_PDF_URLS) == len(COMPULSORY_EXPECTED_PREMIUM_TOTALS)
assert len(COMPULSORY_INSURANCE_PDF_URLS) == len(_COMPULSORY_INSURANCE_PDF_PYTEST_IDS)
assert len(COMMERCIAL_INSURANCE_PDF_URLS) == len(COMMERCIAL_EXPECTED_INSURER_NAMES)
assert len(COMMERCIAL_INSURANCE_PDF_URLS) == len(COMMERCIAL_EXPECTED_PERIODS)
assert len(COMMERCIAL_INSURANCE_PDF_URLS) == len(COMMERCIAL_EXPECTED_INSURED_NAMES)
assert len(COMMERCIAL_INSURANCE_PDF_URLS) == len(COMMERCIAL_EXPECTED_SIGN_DATES)
assert len(COMMERCIAL_INSURANCE_PDF_URLS) == len(COMMERCIAL_EXPECTED_PREMIUM_TOTALS)
assert len(COMMERCIAL_INSURANCE_PDF_URLS) == len(COMMERCIAL_EXPECTED_COMMERCIAL_ONLY_KV)
assert len(COMMERCIAL_INSURANCE_PDF_URLS) == len(_COMMERCIAL_INSURANCE_PDF_PYTEST_IDS)
assert len(_CAR_INSURANCE_COMMERCIAL_ONLY_KV_KEYS) == 9
for _row in COMMERCIAL_EXPECTED_COMMERCIAL_ONLY_KV:
    assert len(_row) == 9

# ---------------------------------------------------------------------------
# 本地调试：只跑部分序号（与日志 ``[i/n]`` 中 i 一致，从 1 起）。None = 跑全部。
# 例：只跑交强险第 3 条 → COMPULSORY_RUN_INDICES = {3}
# ---------------------------------------------------------------------------
COMPULSORY_RUN_INDICES: Optional[Set[int]] = None
COMMERCIAL_RUN_INDICES: Optional[Set[int]] = None


def _should_run_index(indices: Optional[Set[int]], i: int) -> bool:
    if indices is None:
        return True
    return i in indices


def _validate_run_indices(indices: Optional[Set[int]], n_max: int, name: str) -> None:
    if indices is None:
        return
    valid = set(range(1, n_max + 1))
    bad = indices - valid
    if bad:
        raise ValueError(f"{name} 含无效序号 {bad}，有效范围为 1..{n_max}")


def _http_car_insurance_extract(
    policy_type: Literal["compulsory", "commercial"],
    pdf_url: str,
) -> Dict[str, Any]:
    """POST 已启动 ``ocr_service`` 的车险抽取接口，返回 JSON 解析后的 dict。"""
    url = f"{OCR_SERVICE_BASE}{OCR_CAR_INSURANCE_PATH}"
    body = json.dumps(
        {"type": policy_type, "pdf_url": pdf_url},
        ensure_ascii=False,
    ).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    text = ""
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            text = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode("utf-8", errors="replace")
        except Exception as read_err:
            err_body = f"(无法读取 HTTP 错误响应体: {read_err})"
        raise RuntimeError(
            "车险 OCR 接口返回 HTTP 错误（查看服务端日志；常见 502=抽取异常、400=参数或 PDF 不可读）。\n"
            f"HTTP {e.code} {e.reason!s} POST {url}\n{err_body[:8000]}"
        ) from e
    # 连接/DNS/超时等保持为 URLError，供上层 pytest.skip

    if not text.strip():
        raise RuntimeError(f"车险 OCR 返回空响应 POST {url}")

    try:
        obj = json.loads(text)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"响应非 JSON POST {url}: {text[:800]}") from e
    if not isinstance(obj, dict):
        raise RuntimeError(f"响应根非对象 POST {url}: {type(obj)}")
    return obj


def _car_insurance_extract(
    policy_type: Literal["compulsory", "commercial"],
    pdf_url: str,
) -> Dict[str, Any]:
    """pytest：调用已启动的 OCR 服务；仅连接失败时 skip，HTTP/解析错误照常失败。"""
    try:
        return _http_car_insurance_extract(policy_type, pdf_url)
    except RuntimeError:
        raise
    except urllib.error.URLError as e:
        pytest.skip(
            f"车险 HTTP 服务不可用（请先启动 ocr_service，{OCR_SERVICE_BASE}{OCR_CAR_INSURANCE_PATH}）：{e}"
        )


def _print_result(title: str, data: Dict[str, Any]) -> None:
    print(title, flush=True)
    print(json.dumps(data, ensure_ascii=False, indent=2), flush=True)


def _print_case_done(tag: str, i: int, n: int) -> None:
    """单个用例跑完后的日志（与最终 assert 分离）。"""
    print(f"[pytest] {tag} [{i}/{n}] 用例结束。", flush=True)


@pytest.mark.parametrize(
    "url,expected_name,expected_period,expected_insured_name,"
    "expected_sign_date,expected_premium_total",
    list(
        zip(
            COMPULSORY_INSURANCE_PDF_URLS,
            COMPULSORY_EXPECTED_INSURER_NAMES,
            COMPULSORY_EXPECTED_PERIODS,
            COMPULSORY_EXPECTED_INSURED_NAMES,
            COMPULSORY_EXPECTED_SIGN_DATES,
            COMPULSORY_EXPECTED_PREMIUM_TOTALS,
        )
    ),
    ids=list(_COMPULSORY_INSURANCE_PDF_PYTEST_IDS),
)
def test_car_insurance_compulsory_extract(
    url: str,
    expected_name: str,
    expected_period: Dict[str, str],
    expected_insured_name: str,
    expected_sign_date: str,
    expected_premium_total: str,
) -> None:
    """交强险：单个PDF文件测试用例。"""
    # 处理COMPULSORY_RUN_INDICES过滤（向后兼容）
    if COMPULSORY_RUN_INDICES is not None:
        # 获取当前URL在列表中的索引（从1开始）
        try:
            idx = COMPULSORY_INSURANCE_PDF_URLS.index(url) + 1
        except ValueError:
            idx = -1

        if idx == -1 or idx not in COMPULSORY_RUN_INDICES:
            pytest.skip(f"跳过交强险测试 {url} (COMPULSORY_RUN_INDICES={COMPULSORY_RUN_INDICES})")

    result = _car_insurance_extract("compulsory", url)
    _print_result(f"[pytest] compulsory pdf_url={url}", result)

    if "kv" not in result:
        pytest.fail(f"缺少 kv: {url}")
    if "known_insurance_company" not in result:
        pytest.fail(f"缺少 known_insurance_company: {url}")

    got = result["kv"].get("保险公司名称")
    if got != expected_name:
        pytest.fail(f"保险公司名称不匹配: 期望={expected_name!r}, 实际={got!r}")

    got_insured_name = result["kv"].get("被保险人")
    if got_insured_name != expected_insured_name:
        pytest.fail(f"被保险人不匹配: 期望={expected_insured_name!r}, 实际={got_insured_name!r}")

    # 检查保险期间（命中时为 dict，未识别为 ""）
    got_period = result["kv"].get("保险期间")
    if not isinstance(got_period, dict):
        pytest.fail(f"缺少保险期间: {url}, 实际={got_period!r}")
    if got_period != expected_period:
        pytest.fail(f"保险期间不匹配: 期望={expected_period!r}, 实际={got_period!r}")

    got_sign_date = result["kv"].get("签单日期")
    if got_sign_date != expected_sign_date:
        pytest.fail(
            f"签单日期不匹配: 期望={expected_sign_date!r}, 实际={got_sign_date!r}, url={url}"
        )

    got_premium = result["kv"].get("保险费合计")
    if got_premium != expected_premium_total:
        pytest.fail(
            f"保险费合计不匹配: 期望={expected_premium_total!r}, 实际={got_premium!r}, url={url}"
        )


@pytest.mark.parametrize(
    "url,expected_name,expected_period,expected_insured_name,"
    "expected_sign_date,expected_premium_total,expected_commercial_only",
    list(
        zip(
            COMMERCIAL_INSURANCE_PDF_URLS,
            COMMERCIAL_EXPECTED_INSURER_NAMES,
            COMMERCIAL_EXPECTED_PERIODS,
            COMMERCIAL_EXPECTED_INSURED_NAMES,
            COMMERCIAL_EXPECTED_SIGN_DATES,
            COMMERCIAL_EXPECTED_PREMIUM_TOTALS,
            COMMERCIAL_EXPECTED_COMMERCIAL_ONLY_KV,
        )
    ),
    ids=list(_COMMERCIAL_INSURANCE_PDF_PYTEST_IDS),
)
def test_car_insurance_commercial_extract(
    url: str,
    expected_name: str,
    expected_period: Dict[str, str],
    expected_insured_name: str,
    expected_sign_date: str,
    expected_premium_total: str,
    expected_commercial_only: tuple[str, ...],
) -> None:
    """商业险：单个PDF文件测试用例。"""
    # 处理COMMERCIAL_RUN_INDICES过滤（向后兼容）
    if COMMERCIAL_RUN_INDICES is not None:
        # 获取当前URL在列表中的索引（从1开始）
        try:
            idx = COMMERCIAL_INSURANCE_PDF_URLS.index(url) + 1
        except ValueError:
            idx = -1

        if idx == -1 or idx not in COMMERCIAL_RUN_INDICES:
            pytest.skip(f"跳过商业险测试 {url} (COMMERCIAL_RUN_INDICES={COMMERCIAL_RUN_INDICES})")

    result = _car_insurance_extract("commercial", url)
    _print_result(f"[pytest] commercial pdf_url={url}", result)

    errs: List[str] = []

    def _fail_all() -> None:
        if errs:
            pytest.fail(f"{url}\n" + "\n".join(errs))

    if "kv" not in result:
        pytest.fail(f"缺少 kv: {url}")
    if "known_insurance_company" not in result:
        pytest.fail(f"缺少 known_insurance_company: {url}")
    if "新能源汽车损失保险保费" not in result["kv"]:
        pytest.fail(f"kv 缺少键 新能源汽车损失保险保费: {url}")
    if "免赔额" not in result["kv"]:
        pytest.fail(f"kv 缺少键 免赔额: {url}")

    kv = result["kv"]
    got = kv.get("保险公司名称")
    if got != expected_name:
        errs.append(f"保险公司名称不匹配: 期望={expected_name!r}, 实际={got!r}")

    got_insured_name = kv.get("被保险人")
    if got_insured_name != expected_insured_name:
        errs.append(f"被保险人不匹配: 期望={expected_insured_name!r}, 实际={got_insured_name!r}")

    got_period = kv.get("保险期间")
    if not isinstance(got_period, dict):
        errs.append(f"缺少保险期间: {url}, 实际={got_period!r}")
    elif got_period != expected_period:
        errs.append(f"保险期间不匹配: 期望={expected_period!r}, 实际={got_period!r}")

    got_sign_date = kv.get("签单日期")
    if got_sign_date != expected_sign_date:
        errs.append(
            f"签单日期不匹配: 期望={expected_sign_date!r}, 实际={got_sign_date!r}, url={url}"
        )

    got_premium = kv.get("保险费合计")
    if got_premium != expected_premium_total:
        errs.append(
            f"保险费合计不匹配: 期望={expected_premium_total!r}, 实际={got_premium!r}, url={url}"
        )

    expected_only_map = dict(zip(_CAR_INSURANCE_COMMERCIAL_ONLY_KV_KEYS, expected_commercial_only))
    for k, ev in expected_only_map.items():
        gv = kv.get(k)
        if gv != ev:
            errs.append(f"商业险专属字段 key={k!r}: 期望={ev!r}, 实际={gv!r}")

    _fail_all()


def main() -> None:
    """命令行：经 HTTP 调服务；交强险/商业险列表各跑一次并打印 JSON（遵守 ``*_RUN_INDICES``）。"""
    print(
        f"OCR_SERVICE_BASE={OCR_SERVICE_BASE} OCR_CAR_INSURANCE_PATH={OCR_CAR_INSURANCE_PATH}",
        file=sys.stderr,
        flush=True,
    )
    n_f = len(COMPULSORY_INSURANCE_PDF_URLS)
    _validate_run_indices(COMPULSORY_RUN_INDICES, n_f, "COMPULSORY_RUN_INDICES")
    tag_f = "全部" if COMPULSORY_RUN_INDICES is None else f"序号{sorted(COMPULSORY_RUN_INDICES)}"
    print(f"=== 交强险 compulsory（{tag_f}）===", flush=True)
    for i, u_f in enumerate(COMPULSORY_INSURANCE_PDF_URLS, start=1):
        if not _should_run_index(COMPULSORY_RUN_INDICES, i):
            continue
        try:
            r_f = _http_car_insurance_extract("compulsory", u_f)
        except (RuntimeError, urllib.error.URLError, OSError) as e:
            print(f"请求失败 [{i}/{n_f}] {u_f}: {e}", file=sys.stderr, flush=True)
            sys.exit(1)
        _print_result(f"[{i}/{n_f}] pdf_url={u_f}", r_f)

    n_m = len(COMMERCIAL_INSURANCE_PDF_URLS)
    _validate_run_indices(COMMERCIAL_RUN_INDICES, n_m, "COMMERCIAL_RUN_INDICES")
    tag_m = "全部" if COMMERCIAL_RUN_INDICES is None else f"序号{sorted(COMMERCIAL_RUN_INDICES)}"
    print(f"=== 商业险 commercial（{tag_m}）===", flush=True)
    for i, u_m in enumerate(COMMERCIAL_INSURANCE_PDF_URLS, start=1):
        if not _should_run_index(COMMERCIAL_RUN_INDICES, i):
            continue
        try:
            r_m = _http_car_insurance_extract("commercial", u_m)
        except (RuntimeError, urllib.error.URLError, OSError) as e:
            print(f"请求失败 [{i}/{n_m}] {u_m}: {e}", file=sys.stderr, flush=True)
            sys.exit(1)
        _print_result(f"[{i}/{n_m}] pdf_url={u_m}", r_m)


if __name__ == "__main__":
    main()
