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


def test_find_dates_in_text_new_formats() -> None:
    """测试新格式的日期时间匹配"""

    # 新格式测试用例
    new_format_cases = [
        ("2025年5月18日00:00时", ["2025年5月18日00:00时"]),
        ("2025年5月18日00:00", ["2025年5月18日00:00"]),
        ("2025年5月18日00时", ["2025年5月18日00时"]),
        ("2025-05-1800:00", ["2025-05-1800:00"]),
        # 带空格的变体
        ("2025年 5月18日 00:00时", ["2025年5月18日00:00时"]),
        ("2025 年5月18日00:00", ["2025年5月18日00:00"]),
        ("2025年5月 18日 00时", ["2025年5月18日00时"]),
        ("2025-05-18 00:00", ["2025-05-1800:00"]),
    ]

    # 现有格式回归测试
    existing_format_cases = [
        ("2025年5月28日17:41:37", ["2025年5月28日17:41:37"]),
        ("2025年5月28日17时41分", ["2025年5月28日17时41分"]),
        ("2025年5月28日17时41分59秒", ["2025年5月28日17时41分59秒"]),
        ("2025-05-2817:41:37", ["2025-05-2817:41:37"]),
        ("2025/05/2817:41:37", ["2025/05/2817:41:37"]),
    ]

    # 测试新格式
    for text, expected in new_format_cases:
        result = _find_dates_in_text(text)
        assert result == expected, f"新格式失败: {text}\n期望: {expected}\n实际: {result}"

    # 测试现有格式
    for text, expected in existing_format_cases:
        result = _find_dates_in_text(text)
        assert result == expected, f"现有格式回归失败: {text}\n期望: {expected}\n实际: {result}"


if __name__ == "__main__":
    try:
        test_find_dates_in_text_new_formats()
        print("✅ 所有测试通过！")
    except AssertionError as e:
        print(f"❌ 测试失败: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"⚠️ 运行时错误: {e}")
        sys.exit(1)
