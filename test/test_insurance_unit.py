#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
车险保单内部函数单元测试。
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from car_insurance import (
    _find_dates_in_text,
    _convert_to_iso_format,
    _extract_period_from_patterns,
)


def test_find_dates_in_text_new_formats():
    """测试新格式的日期时间匹配"""

    # 新格式测试用例
    test_cases = [
        # 新格式
        ("2025年5月18日00:00时", ["2025年5月18日00:00时"]),
        ("2025年5月18日00:00", ["2025年5月18日00:00"]),
        ("2025年5月18日00时", ["2025年5月18日00时"]),
        ("2025-05-1800:00", ["2025-05-1800:00"]),

        # 带空格的变体
        ("2025年 5月18日 00:00时", ["2025年5月18日00:00时"]),
        ("2025 年5月18日00:00", ["2025年5月18日00:00"]),
        ("2025年5月 18日 00时", ["2025年5月18日00时"]),
        ("2025-05-18 00:00", ["2025-05-1800:00"]),

        # 现有格式（确保仍然工作）
        ("2025年5月28日17:41:37", ["2025年5月28日17:41:37"]),
        ("2025年5月28日17时41分", ["2025年5月28日17时41分"]),
        ("2025年5月28日17时41分59秒", ["2025年5月28日17时41分59秒"]),
        ("2025-05-2817:41:37", ["2025-05-2817:41:37"]),
        ("2025/05/2817:41:37", ["2025/05/2817:41:37"]),
    ]

    for text, expected in test_cases:
        result = _find_dates_in_text(text)
        assert sorted(result) == sorted(expected), f"Failed for text: {text}\nExpected: {expected}\nGot: {result}"


if __name__ == "__main__":
    test_find_dates_in_text_new_formats()
    print("All tests passed!")
