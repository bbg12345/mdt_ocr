#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
车险保单内部函数单元测试。
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import car_insurance as ci
from car_insurance import (
    _aggregate_pass2_deductibles_from_llm_strings,
    _CAR_INSURANCE_COMMERCIAL_ONLY_KV_KEYS,
    _commercial_pass2_neighbor_blob_suggests_detail_llm,
    _commercial_pass2_row_neighbor_blocks_flat_text,
    _COMMERCIAL_PASS2_PREFILTER_Y_PAD_PT,
    _merge_commercial_detail_table_kv,
    _ping_an_refine_passenger_parse,
    _find_dates_in_text,
    _convert_to_iso_format,
    _extract_period_from_patterns,
    _find_period_blocks_indices,
    _pass2_insured_name_from_block,
    run_car_insurance_insured_passes,
    run_car_insurance_license_plate_passes,
    run_car_insurance_vin_passes,
    _pass1_sign_date_from_block,
    run_car_insurance_sign_date_passes,
    _pass1_premium_total_from_block,
    _pass2_premium_neighbor_amount_from_block_items,
    run_car_insurance_premium_total_passes,
    run_car_insurance_commercial_detail_table_passes,
    run_car_insurance_ping_an_premium_detail_passes,
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


def test_convert_to_iso_format_new_formats() -> None:
    """测试_convert_to_iso_format函数的新格式转换"""

    # 新格式测试
    new_format_cases = [
        ("2025年5月18日00:00时", "2025-05-18T00:00:00"),
        ("2025年5月18日00:00", "2025-05-18T00:00:00"),
        ("2025年5月18日00时", "2025-05-18T00:00:00"),
        ("2025-05-1800:00", "2025-05-18T00:00:00"),
        ("2025 年 5 月 18 日 00:00 时", "2025-05-18T00:00:00"),
        ("2025-05-18 00:00", "2025-05-18T00:00:00"),
    ]

    # 24:00:00特殊情况测试
    special_cases = [
        ("2026年5月17日24:00时", "2026-05-17T24:00:00"),
        ("2026年5月17日24:00", "2026-05-17T24:00:00"),
        ("2026年5月17日24时", "2026-05-17T24:00:00"),
    ]

    # 现有格式回归测试
    existing_format_cases = [
        ("2025年07月09日00时00分", "2025-07-09T00:00:00"),
        ("2025年07月09日00时00分00秒", "2025-07-09T00:00:00"),
        ("2025年07月09日00:00:00", "2025-07-09T00:00:00"),
        ("2025-07-09 00:00:00", "2025-07-09T00:00:00"),
        ("2025-07-0900:00:00", "2025-07-09T00:00:00"),
        ("2025/07/09 00:00:00", "2025-07-09T00:00:00"),
        ("2025年07月09日", "2025-07-09T00:00:00"),
        ("2025-07-09", "2025-07-09T00:00:00"),
    ]

    # 测试新格式
    for date_str, expected in new_format_cases:
        result = _convert_to_iso_format(date_str)
        assert result == expected, f"新格式失败: {date_str}\n期望: {expected}\n实际: {result}"

    # 测试特殊情况
    for date_str, expected in special_cases:
        result = _convert_to_iso_format(date_str)
        assert result == expected, f"特殊情况失败: {date_str}\n期望: {expected}\n实际: {result}"

    # 测试现有格式
    for date_str, expected in existing_format_cases:
        result = _convert_to_iso_format(date_str)
        assert result == expected, f"现有格式回归失败: {date_str}\n期望: {expected}\n实际: {result}"


def test_extract_period_from_patterns_new_formats() -> None:
    """测试_extract_period_from_patterns函数的新格式和现有格式"""

    # 新格式测试（"保险期间："前缀格式）
    new_format_cases = [
        ("保险期间：2025 年 5 月 18 日 00:00 时起至 2026 年 5 月 17 日 24:00 时止",
         {"start": "2025-05-18T00:00:00", "end": "2026-05-17T24:00:00"}),
        ("保险期间: 2025年5月18日00:00时起至2026年5月17日24:00时止",
         {"start": "2025-05-18T00:00:00", "end": "2026-05-17T24:00:00"}),
        ("保险期间：2025年5月18日00:00起至2026年5月17日24:00止",
         {"start": "2025-05-18T00:00:00", "end": "2026-05-17T24:00:00"}),
        ("保险期间：2025年5月18日00时起至2026年5月17日24时止",
         {"start": "2025-05-18T00:00:00", "end": "2026-05-17T24:00:00"}),
    ]

    # 现有格式回归测试（"自...起至...止"格式）
    existing_format_cases = [
        ("自2025年5月18日00:00时起至2026年5月17日24:00时止",
         {"start": "2025-05-18T00:00:00", "end": "2026-05-17T24:00:00"}),
        ("自2025年5月18日00:00起至2026年5月17日24:00止",
         {"start": "2025-05-18T00:00:00", "end": "2026-05-17T24:00:00"}),
        ("自 2025年5月18日 00:00 起 至 2026年5月17日 24:00 止",
         {"start": "2025-05-18T00:00:00", "end": "2026-05-17T24:00:00"}),
    ]

    # 测试新格式
    for text, expected in new_format_cases:
        # 创建包含测试文本的块列表
        blocks = [text]
        # 查找保险期间块索引
        period_blocks_indices = _find_period_blocks_indices(blocks)
        # 调用函数
        result = _extract_period_from_patterns(blocks, period_blocks_indices, "test_engine")
        assert result == expected, f"新格式失败: {text}\n期望: {expected}\n实际: {result}"

    # 测试现有格式
    for text, expected in existing_format_cases:
        # 创建包含测试文本的块列表
        blocks = [text]
        # 查找保险期间块索引
        period_blocks_indices = _find_period_blocks_indices(blocks)
        # 调用函数
        result = _extract_period_from_patterns(blocks, period_blocks_indices, "test_engine")
        assert result == expected, f"现有格式回归失败: {text}\n期望: {expected}\n实际: {result}"


def test_pass2_insured_name_from_block() -> None:
    block = "投保人： 嗨车购（天津）融资租赁有限公司 保单号：123"
    got = _pass2_insured_name_from_block(block)
    assert got == "嗨车购（天津）融资租赁有限公司"


def test_run_car_insurance_insured_passes_pass1_priority() -> None:
    blocks = [
        "其他信息",
        "本保单相关方：嗨车购（天津）融资租赁有限公司",
        "投保人： 某某有限公司",
    ]
    got = run_car_insurance_insured_passes(blocks, engine_label="test_engine")
    assert got["pass1_hit"] == "嗨车购（天津）融资租赁有限公司"
    assert got["被保险人"] == "嗨车购（天津）融资租赁有限公司"
    assert got["pass2_hit"] is None


def test_run_car_insurance_insured_passes_pass1_halfwidth_parentheses() -> None:
    blocks = [
        "其他信息",
        "本保单相关方：嗨车购(天津)融资租赁有限公司",
    ]
    got = run_car_insurance_insured_passes(blocks, engine_label="test_engine")
    assert got["pass1_hit"] == "嗨车购（天津）融资租赁有限公司"
    assert got["被保险人"] == "嗨车购（天津）融资租赁有限公司"


def test_run_car_insurance_insured_passes_pass1_mdt_company() -> None:
    blocks = [
        "其他信息",
        "本保单相关方：天津明德通汽车租赁有限公司",
    ]
    got = run_car_insurance_insured_passes(blocks, engine_label="test_engine")
    assert got["pass1_hit"] == "天津明德通汽车租赁有限公司"
    assert got["被保险人"] == "天津明德通汽车租赁有限公司"


def test_run_car_insurance_insured_passes_pass2_fallback() -> None:
    blocks = [
        "其他信息",
        "投保人： 天津测试有限公司 保险公司：中国平安财产保险",
    ]
    got = run_car_insurance_insured_passes(blocks, engine_label="test_engine")
    assert got["pass1_hit"] is None
    assert got["pass2_hit"] == "天津测试有限公司"
    assert got["被保险人"] == "天津测试有限公司"


def test_pass2_insured_name_from_block_mdt_company() -> None:
    block = "投保人： 天津明德通汽车租赁有限公司 保单号：123"
    got = _pass2_insured_name_from_block(block)
    assert got == "天津明德通汽车租赁有限公司"


def test_run_car_insurance_license_plate_passes_pass1() -> None:
    blocks = [
        "其他信息",
        "号牌号码 津A-AM8718 车架号 LFMAS14U2P0006679",
    ]
    got = run_car_insurance_license_plate_passes(blocks, engine_label="test_engine")
    assert got["pass1_hit"] == "津A-AM8718"
    assert got["pass1_block_index"] == 1
    assert got["车牌号"] == "津A-AM8718"


def test_run_car_insurance_license_plate_passes_pass1_newline_delimited() -> None:
    blocks = [
        "号牌号码\n津AAY1756\n机动车种类\n客车\n使用性质\n出租、租赁营业客车",
    ]
    got = run_car_insurance_license_plate_passes(blocks, engine_label="test_engine")
    assert got["pass1_hit"] == "津AAY1756"
    assert got["车牌号"] == "津AAY1756"


def test_run_car_insurance_license_plate_passes_pass1_car_plate_key() -> None:
    blocks = [
        "车牌号\n津ADJ1786\n厂牌型号\n北京BJ7000C5D3-BEV",
    ]
    got = run_car_insurance_license_plate_passes(blocks, engine_label="test_engine")
    assert got["pass1_hit"] == "津ADJ1786"
    assert got["车牌号"] == "津ADJ1786"


def test_run_car_insurance_license_plate_passes_pass2_reversed_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    word_items = [
        (0, 8, 97.0, 198.3, 136.1, 206.9, "号码号牌", 6, 0, 0),
        (0, 9, 169.4, 197.8, 214.1, 208.2, "津AAU7197", 7, 0, 0),
    ]
    monkeypatch.setattr(ci, "iter_pymupdf_word_rect_items", lambda _pdf_bytes: word_items)

    got = run_car_insurance_license_plate_passes(
        ["无关块"],
        engine_label="test_engine",
        pdf_bytes=b"pdf",
    )
    assert got["pass1_hit"] is None
    assert got["pass2_hit"] == "津AAU7197"
    assert got["车牌号"] == "津AAU7197"


def test_run_car_insurance_license_plate_passes_pass1_spaced_keyword() -> None:
    blocks = [
        "号 牌 号 码津ADR0108\n机动车种类\n6座以下客车",
    ]
    got = run_car_insurance_license_plate_passes(blocks, engine_label="test_engine")
    assert got["pass1_hit"] == "津ADR0108"
    assert got["车牌号"] == "津ADR0108"


def test_run_car_insurance_license_plate_passes_rejects_invalid_token() -> None:
    blocks = ["号牌号码 号牌号码 车架号 LFMAS14U2P0006679"]
    got = run_car_insurance_license_plate_passes(blocks, engine_label="test_engine")
    assert got["pass1_hit"] is None
    assert got["车牌号"] is None


def test_run_car_insurance_license_plate_passes_pass2_word_bbox_scans_right_from_key_x0(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    word_items = [
        (0, 1, 100.0, 100.0, 150.0, 120.0, "号牌号码", 1, 0, 0),
        (0, 2, 20.0, 101.0, 80.0, 119.0, "津B99999", 2, 0, 0),
        (0, 3, 170.0, 101.0, 240.0, 119.0, "津A12345", 3, 0, 0),
    ]
    monkeypatch.setattr(ci, "iter_pymupdf_word_rect_items", lambda _pdf_bytes: word_items)

    got = run_car_insurance_license_plate_passes(
        ["无关块"],
        engine_label="test_engine",
        pdf_bytes=b"pdf",
    )
    assert got["pass1_hit"] is None
    assert got["pass2_hit"] == "津A12345"
    assert got["车牌号"] == "津A12345"


def test_run_car_insurance_license_plate_passes_pass2_bbox_uses_overlap_after_right_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    word_items = [
        (0, 1, 90.0, 240.0, 126.0, 249.0, "号牌号码", 1, 0, 0),
        (0, 2, 503.0, 224.0, 552.0, 233.0, "156****6337", 2, 0, 0),
        (0, 3, 145.0, 240.0, 186.0, 250.0, "津D205710", 3, 0, 0),
    ]
    monkeypatch.setattr(ci, "iter_pymupdf_word_rect_items", lambda _pdf_bytes: word_items)

    got = run_car_insurance_license_plate_passes(
        ["无关块"],
        engine_label="test_engine",
        pdf_bytes=b"pdf",
    )
    assert got["pass1_hit"] is None
    assert got["pass2_hit"] == "津D205710"
    assert got["车牌号"] == "津D205710"


def test_run_car_insurance_license_plate_passes_pass2_rejects_chinese_unit_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    word_items = [
        (0, 1, 68.0, 200.3, 104.0, 210.0, "号牌号码", 1, 0, 0),
        (0, 2, 468.3, 252.4, 492.9, 264.0, "0千克", 2, 0, 0),
        (0, 3, 147.5, 211.4, 197.2, 219.9, "津AAU7197", 3, 0, 0),
    ]
    monkeypatch.setattr(ci, "iter_pymupdf_word_rect_items", lambda _pdf_bytes: word_items)

    got = run_car_insurance_license_plate_passes(
        ["无关块"],
        engine_label="test_engine",
        pdf_bytes=b"pdf",
    )
    assert got["pass1_hit"] is None
    assert got["pass2_hit"] == "津AAU7197"
    assert got["车牌号"] == "津AAU7197"


def test_run_car_insurance_license_plate_passes_rejects_too_long_plate() -> None:
    blocks = ["号牌号码 津ADR010189"]
    got = run_car_insurance_license_plate_passes(blocks, engine_label="test_engine")
    assert got["pass1_hit"] is None
    assert got["车牌号"] is None


def test_run_car_insurance_vin_passes_pass1_colon_variant() -> None:
    blocks = [
        "其他信息",
        "车架号： LNBMC5GK8SD205710 发动机号 ABC123",
    ]
    got = run_car_insurance_vin_passes(blocks, engine_label="test_engine")
    assert got["pass1_hit"] == "LNBMC5GK8SD205710"
    assert got["pass1_block_index"] == 1
    assert got["车架号"] == "LNBMC5GK8SD205710"


def test_run_car_insurance_vin_passes_pass1_after_closing_parenthesis() -> None:
    blocks = [
        "发动机号码\nB123004208\n识别代码(车架号) LFMAS14U2P0006679",
    ]
    got = run_car_insurance_vin_passes(blocks, engine_label="test_engine")
    assert got["pass1_hit"] == "LFMAS14U2P0006679"
    assert got["车架号"] == "LFMAS14U2P0006679"


def test_run_car_insurance_vin_passes_requires_letters_and_digits() -> None:
    blocks = ["车架号 1234567890 其他"]
    got = run_car_insurance_vin_passes(blocks, engine_label="test_engine")
    assert got["pass1_hit"] is None
    assert got["车架号"] is None


def test_run_car_insurance_vin_passes_pass2_ignores_left_of_key_x0(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    word_items = [
        (0, 1, 100.0, 100.0, 150.0, 120.0, "车架号", 1, 0, 0),
        (0, 2, 170.0, 101.0, 240.0, 119.0, "1234567890", 2, 0, 0),
        (0, 3, 20.0, 101.0, 80.0, 119.0, "LFMAS14U2P0006679", 3, 0, 0),
    ]
    monkeypatch.setattr(ci, "iter_pymupdf_word_rect_items", lambda _pdf_bytes: word_items)

    got = run_car_insurance_vin_passes(
        ["无关块"],
        engine_label="test_engine",
        pdf_bytes=b"pdf",
    )
    assert got["pass1_hit"] is None
    assert got["pass2_hit"] is None
    assert got["车架号"] is None


def test_run_car_insurance_vin_passes_pass2_uses_keyword_word_bbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    word_items = [
        (0, 16, 239.0, 252.1, 359.3, 262.1, "VIN/车架号", 3, 1, 0),
        (0, 17, 147.5, 233.5, 218.5, 245.1, "A2003P624CB035", 4, 0, 0),
        (0, 18, 294.2, 252.5, 388.1, 264.1, "LNAAKAA10P5764849", 5, 0, 0),
    ]
    monkeypatch.setattr(ci, "iter_pymupdf_word_rect_items", lambda _pdf_bytes: word_items)

    got = run_car_insurance_vin_passes(
        ["无关块"],
        engine_label="test_engine",
        pdf_bytes=b"pdf",
    )
    assert got["pass1_hit"] is None
    assert got["pass2_hit"] == "LNAAKAA10P5764849"
    assert got["车架号"] == "LNAAKAA10P5764849"


def test_pass1_sign_date_from_block_colon_variants() -> None:
    b1 = "签单日期： 2025-05-05 其他字段"
    b2 = "签单日期:2025-05-05"
    b3 = "签单日期：2025 -05-05"  # 去掉空格后应仍能匹配
    b4 = "签单日期：2025年4月15日"
    b5 = "签单日期: 2025 年 4 月 5 日"
    b6 = "签单日期：2025/04/16 "
    b7 = "签单日期: 2025 / 4 / 6"
    assert _pass1_sign_date_from_block(b1) == "2025-05-05"
    assert _pass1_sign_date_from_block(b2) == "2025-05-05"
    assert _pass1_sign_date_from_block(b3) == "2025-05-05"
    assert _pass1_sign_date_from_block(b4) == "2025-04-15"
    assert _pass1_sign_date_from_block(b5) == "2025-04-05"
    assert _pass1_sign_date_from_block(b6) == "2025-04-16"
    assert _pass1_sign_date_from_block(b7) == "2025-04-06"


def test_run_car_insurance_sign_date_passes() -> None:
    blocks = [
        "其他信息",
        "签单日期： 2025-05-05",
    ]
    got = run_car_insurance_sign_date_passes(blocks, engine_label="test_engine")
    assert got["pass1_hit"] == "2025-05-05"
    assert got["pass2_hit"] is None
    assert got.get("pass3_hit") is None
    assert got["签单日期"] == "2025-05-05"


def test_run_car_insurance_sign_date_passes_pass2_whole_block_no_colon() -> None:
    """pass2：块内含「签单日期」且全文可解析出日期（无需 pass1 的冒号形态）。"""
    blocks = ["其他", "签单日期 2025-07-08 备注"]
    got = run_car_insurance_sign_date_passes(blocks, engine_label="test_engine")
    assert got["pass1_hit"] is None
    assert got["pass2_hit"] == "2025-07-08"
    assert got["pass2_block_index"] == 1
    assert got.get("pass3_hit") is None
    assert got["签单日期"] == "2025-07-08"


def test_pass3_sign_date_bbox_fallback_cjk_single_block() -> None:
    """pass3：同页 PyMuPDF 块内含「签单日期」与换行日期、无冒号时仍能命中。"""
    fitz = pytest.importorskip("fitz")
    from car_insurance import _pass3_sign_date_with_bbox_fallback

    doc = fitz.open()
    page = doc.new_page(width=400, height=220)
    cjk = fitz.Font("cjk")
    page.insert_font("myf", fontbuffer=cjk.buffer)
    page.insert_text((40, 100), "签单日期\n2025-08-09", fontname="myf", fontsize=11)
    pdf_bytes = doc.tobytes()
    doc.close()

    assert _pass3_sign_date_with_bbox_fallback(pdf_bytes) == "2025-08-09"

    got = run_car_insurance_sign_date_passes(
        ["无关块"],
        engine_label="test_engine",
        pdf_bytes=pdf_bytes,
    )
    assert got["pass1_hit"] is None
    assert got["pass2_hit"] is None
    assert got["pass3_hit"] == "2025-08-09"
    assert got["签单日期"] == "2025-08-09"


def test_pass1_premium_total_from_block() -> None:
    # 含关键字的整块：从块首从左到右第一个「整数部（去逗号）至少 4 位」的两位小数
    assert (
        _pass1_premium_total_from_block(
            "保险费合计:RMB1350.00元（不含税保费:1273.58元，税额:76.42元）",
        )
        == "1350.00"
    )
    assert (
        _pass1_premium_total_from_block(
            "备注 保险费合计 27.00元 税额 76.42元 总 1350.00元",
        )
        == "1350.00"
    )
    assert _pass1_premium_total_from_block("保险费合计：1,234.56元") == "1234.56"
    assert _pass1_premium_total_from_block("保险费合计 ￥99.01元") is None
    assert _pass1_premium_total_from_block("保险费合计：100.00元") is None
    assert _pass1_premium_total_from_block("保险费合计：1000.00元") == "1000.00"
    assert _pass1_premium_total_from_block("无关键字 100.00元") is None
    assert _pass1_premium_total_from_block("保险费合计：100.5元") is None
    taikang_like = (
        "（￥: \n元） \n保险费合计(人民币大写)：壹仟壹佰柒拾元整\n1170.00\n"
        "其中救助基金( 2%)￥: 23.40 元"
    )
    assert _pass1_premium_total_from_block(taikang_like) == "1170.00"
    assert _pass1_premium_total_from_block("前缀 11.11 后缀 保险费合计 22.22") is None
    zhongyin_like = (
        "保险费合计（人民币大写）\n元) 救助基金 xxx\n27.00\n其它\n1350.00\n壹仟叁佰伍拾元整"
    )
    assert _pass1_premium_total_from_block(zhongyin_like) == "1350.00"
    # 泰康等：金额与「人民币大写」在块首，「保险费合计」在块尾
    assert (
        _pass1_premium_total_from_block(
            "￥：4283.13元\n（人民币大写）：肆仟贰佰捌拾叁元壹角叁分\n保险费合计",
        )
        == "4283.13"
    )


def test_run_car_insurance_premium_total_passes() -> None:
    blocks = ["其他", "保险费合计: 5888.00元 备注"]
    got = run_car_insurance_premium_total_passes(blocks, engine_label="test_engine")
    assert got["pass1_hit"] == "5888.00"
    assert got["pass1_block_index"] == 1
    assert got.get("pass2_hit") is None
    assert got["保险费合计"] == "5888.00"


def test_pass2_premium_neighbor_from_block_items() -> None:
    """参照 word 与其它 word 分列时：pass2 在纵向条带、参照左边线右侧的邻 word 中取金额。"""
    # page, word_index, x0,y0,x1,y1,text, block_no,line_no,word_no — y 使用 PyMuPDF 惯例；条带含两框
    ref = (0, 2, 40.0, 80.0, 120.0, 96.0, "保险费合计(人民币)", 0, 0, 1)
    amt = (0, 4, 200.0, 82.0, 290.0, 98.0, "1777.77", 1, 0, 1)
    left_noise = (0, 1, 10.0, 82.0, 35.0, 98.0, "3333.33", 0, 0, 0)  # x1 < ref_x0 - eps，排除
    further = (0, 5, 302.0, 82.0, 370.0, 98.0, "1212.34", 2, 0, 1)
    items = [left_noise, ref, amt, further]
    assert _pass2_premium_neighbor_amount_from_block_items(items, engine_label_for_log="test") == "1777.77"


def test_pass2_premium_neighbor_minimal_items() -> None:
    items = [
        (0, 1, 40.0, 80.0, 120.0, 96.0, "保险费合计", 0, 0, 0),
        (0, 2, 200.0, 82.0, 290.0, 98.0, "6666.66", 1, 0, 0),
    ]
    assert _pass2_premium_neighbor_amount_from_block_items(items) == "6666.66"


def test_pass2_premium_merged_pymupdf_block_yields_no_neighbor() -> None:
    """单块内同时含关键字与金额时无「其它」bbox 块，pass2 应不得误用非邻块。"""
    from car_insurance import _pass2_premium_total_with_bbox_fallback

    fitz = pytest.importorskip("fitz")
    doc = fitz.open()
    page = doc.new_page(width=520, height=200)
    f = fitz.Font("cjk")
    page.insert_font("myf", fontbuffer=f.buffer)
    page.insert_text((40, 100), "保险费合计", fontname="myf", fontsize=11)
    page.insert_text((340, 102), "444.44", fontname="helv", fontsize=11)
    pdf_bytes = doc.tobytes()
    doc.close()
    assert _pass2_premium_total_with_bbox_fallback(pdf_bytes, engine_label="t") is None


def test_ping_an_passenger_coverage_display_variants() -> None:
    """乘客保额：x / × / *；座前元后两种顺序；中间可空格。"""
    cov, prem = _ping_an_refine_passenger_parse(
        ["50000.00元*4座", "356.65", "—"],
        layout="PING_AN",
    )
    assert cov == "50000.00元*4座" and prem == "356.65"

    cov, prem = _ping_an_refine_passenger_parse(
        ["50000.00元", "×", "4座", "88.00"],
        layout="PICC_P",
    )
    assert cov == "50000.00元 × 4座" and prem == "88.00"

    cov, prem = _ping_an_refine_passenger_parse(
        ["4座", "*", "12000元", "200.00"],
        layout="PING_AN",
    )
    assert cov == "4座 * 12000元" and prem == "200.00"

    cov, prem = _ping_an_refine_passenger_parse(
        ["5万元", "4座", "356.65", "—", "356.65"],
        layout="PING_AN",
    )
    assert cov == "5万元 4座" and prem == "356.65"

    cov, prem = _ping_an_refine_passenger_parse(
        ["5000元/座", "4座", "356.65", "—", "356.65"],
        layout="PING_AN",
    )
    assert cov == "5000元/座 4座" and prem == "356.65"

    cov, prem = _ping_an_refine_passenger_parse(
        ["5万元/座", "x", "4座", "356.65", "—", "356.65"],
        layout="PING_AN",
    )
    assert cov == "5万元/座 x 4座" and prem == "356.65"


def test_merge_commercial_detail_table_kv_prefers_pymupdf_then_pypdf() -> None:
    """商业险明细双路合并：逐键 pymupdf 非空优先，否则用 pypdf。"""
    empty = {k: "" for k in _CAR_INSURANCE_COMMERCIAL_ONLY_KV_KEYS}
    mu = {**empty, "新能源汽车损失保险保额": "100.00", "新能源汽车损失保险保费": ""}
    pdf = {**empty, "新能源汽车损失保险保额": "999.00", "新能源汽车损失保险保费": "50.00"}
    got = _merge_commercial_detail_table_kv(mu, pdf)
    assert got["新能源汽车损失保险保额"] == "100.00"
    assert got["新能源汽车损失保险保费"] == "50.00"


def test_run_car_insurance_commercial_detail_table_passes_taikang_layout() -> None:
    """泰康在线：label 后第 2 项保额、第 4 项保费（下标 1 与 3）。"""
    blocks = [
        "新能源汽车损失保险 占位 91852.80元 中间 3070.13 尾",
    ]
    got = run_car_insurance_commercial_detail_table_passes(
        blocks,
        engine_label="test",
        detail_layout="TAIKANG_ONLINE",
    )
    assert got["新能源汽车损失保险保额"] == "91852.80"
    assert got["新能源汽车损失保险保费"] == "3070.13"


def test_run_car_insurance_commercial_detail_table_passes_taikang_electronic_column_block() -> None:
    """泰康电子保单：PyMuPDF 常见单列换行；保额可为无「元」后缀的纯小数；乘客锚点为全角括号长 label。"""
    blocks = [
        "新能源汽车损失保险\n-32.5%\n49455.90\n-\n1704.52",
        "新能源汽车车上人员责任保险（乘客）\n-32.5%\n5万元/座 * 4座\n-\n254.75",
    ]
    got = run_car_insurance_commercial_detail_table_passes(
        blocks,
        engine_label="test",
        detail_layout="TAIKANG_ONLINE",
    )
    assert got["新能源汽车损失保险保额"] == "49455.90"
    assert got["新能源汽车损失保险保费"] == "1704.52"
    assert got["新能源汽车车上人员责任保险(乘客)保额"] == "5万元/座 * 4座"
    assert got["新能源汽车车上人员责任保险(乘客)保费"] == "254.75"


def test_run_car_insurance_commercial_detail_table_passes_pacific_layout() -> None:
    """太平洋财产：label 后第 1 项保额、第 2 项保费（下标 0 与 1）。"""
    blocks = [
        "新能源汽车损失保险 91852.80元 3070.13 其它",
    ]
    got = run_car_insurance_commercial_detail_table_passes(
        blocks,
        engine_label="test",
        detail_layout="PACIFIC_P",
    )
    assert got["新能源汽车损失保险保额"] == "91852.80"
    assert got["新能源汽车损失保险保费"] == "3070.13"


def test_run_car_insurance_commercial_detail_table_passes_yangguang_layout() -> None:
    """阳光：第 1 项保额、第 3 项保费；损失险行第 2 项为免赔额；不采用车损免赔单独行。"""
    blocks = [
        "新能源汽车损失保险 91852.80元 500元/次 3070.13 尾",
        "新能源汽车第三者责任保险 1500000.00元 中 2989.07 尾",
        "车损险每次事故绝对免赔额 999元/次",
    ]
    got = run_car_insurance_commercial_detail_table_passes(
        blocks,
        engine_label="test",
        detail_layout="YANG_GUANG_P",
    )
    assert got["新能源汽车损失保险保额"] == "91852.80"
    assert got["新能源汽车损失保险保费"] == "3070.13"
    assert got["免赔额"] == "500.00"
    assert got["新能源汽车第三者责任保险保额"] == "1500000.00"
    assert got["新能源汽车第三者责任保险保费"] == "2989.07"


def test_run_car_insurance_commercial_detail_table_passes_china_life_layout() -> None:
    """人寿财险：驾驶员 label + 与人民一致的列序（下标 2 保额，3/4 保费）。"""
    blocks = [
        "车上人员责任险(驾驶员) 说明 费率 50000.00元 143.87 其它",
    ]
    got = run_car_insurance_commercial_detail_table_passes(
        blocks,
        engine_label="test",
        detail_layout="CHINA_LIFE_P",
    )
    assert got["新能源汽车车上人员责任保险（司机）保额"] == "50000.00"
    assert got["新能源汽车车上人员责任保险（司机）保费"] == "143.87"


def test_run_car_insurance_commercial_detail_table_passes_china_life_absolute_deductible() -> None:
    """人寿财险：车损行内的「绝对免赔额」只补免赔额，不影响保额/保费列序。"""
    blocks = [
        "新能源汽车损失保险  绝对免赔额0元 / 86,800.00 2,747.54",
    ]
    got = run_car_insurance_commercial_detail_table_passes(
        blocks,
        engine_label="test",
        detail_layout="CHINA_LIFE_P",
    )
    assert got["新能源汽车损失保险保额"] == "86800.00"
    assert got["新能源汽车损失保险保费"] == "2747.54"
    assert got["免赔额"] == "0"


def test_run_car_insurance_commercial_detail_table_passes_china_life_absolute_deductible_only() -> None:
    """人寿财险：PyMuPDF 只切出免赔额短行时，也能补免赔额。"""
    blocks = [
        "新能源汽车损失保险  绝对 免赔额 0 元",
    ]
    got = run_car_insurance_commercial_detail_table_passes(
        blocks,
        engine_label="test",
        detail_layout="CHINA_LIFE_P",
    )
    assert got["新能源汽车损失保险保额"] == ""
    assert got["新能源汽车损失保险保费"] == ""
    assert got["免赔额"] == "0"


def test_run_car_insurance_commercial_detail_table_passes_picc_layout() -> None:
    """人保财险版式：label 后两项为保额列前占位，下标 2 为保额、3 或 4 为保费。"""
    blocks = [
        "新能源汽车损失保险 说明 费率 91852.80元 3070.13 其它",
        "车上人员责任险(乘客) a b 4座 x5万元/座 356.65 —",
        "车损险每次事故绝对免赔额 0元/次",
    ]
    got = run_car_insurance_commercial_detail_table_passes(
        blocks,
        engine_label="test",
        detail_layout="PICC_P",
    )
    assert got["新能源汽车损失保险保额"] == "91852.80"
    assert got["新能源汽车损失保险保费"] == "3070.13"
    assert got["新能源汽车车上人员责任保险(乘客)保额"] == "4座 x5万元/座"
    assert got["新能源汽车车上人员责任保险(乘客)保费"] == "356.65"
    assert got["免赔额"] == "0"


def test_run_car_insurance_ping_an_premium_detail_passes_pymupdf_like_blocks() -> None:
    """平安商业险明细：与 export_pymupdf 块结构类似的若干块。"""
    blocks = [
        "新能源汽车损失保险\n91852.80元\n3070.13\n—\n3070.13",
        "新能源汽车第三者责任保险\n1500000.00元\n2989.07\n—\n2989.07",
        "车上人员责任险(司机)\n50000.00元\n143.87\n—\n143.87",
        "车上人员责任险(乘客)\n4座 x5万元/座\n356.65\n—\n356.65\n车损险每次事故绝对免赔额\n0元/次",
    ]
    got = run_car_insurance_ping_an_premium_detail_passes(blocks, engine_label="test")
    assert got["新能源汽车损失保险保额"] == "91852.80"
    assert got["新能源汽车损失保险保费"] == "3070.13"
    assert got["新能源汽车第三者责任保险保额"] == "1500000.00"
    assert got["新能源汽车第三者责任保险保费"] == "2989.07"
    assert got["新能源汽车车上人员责任保险（司机）保额"] == "50000.00"
    assert got["新能源汽车车上人员责任保险（司机）保费"] == "143.87"
    assert got["新能源汽车车上人员责任保险(乘客)保额"] == "4座 x5万元/座"
    assert got["新能源汽车车上人员责任保险(乘客)保费"] == "356.65"
    assert got["免赔额"] == "0"


def test_aggregate_pass2_deductibles_all_zero() -> None:
    assert _aggregate_pass2_deductibles_from_llm_strings(["0", "/", "0.00"]) == "0"
    assert _aggregate_pass2_deductibles_from_llm_strings(["", "/", "-"]) is None


def test_aggregate_pass2_deductibles_max_nonzero() -> None:
    assert _aggregate_pass2_deductibles_from_llm_strings(["0", "100", "50.5"]) == "100.00"


def test_aggregate_pass2_deductibles_empty_returns_none() -> None:
    assert _aggregate_pass2_deductibles_from_llm_strings([]) is None


def test_huaan_pass2_collects_deductible_only_from_damage_row(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_llm(**kwargs: Any) -> Dict[str, str]:
        return {"coverage": "1500000.00", "premium": "3416.08", "deductible": "0"}

    monkeypatch.setattr(ci, "_doubao_infer_commercial_detail_row", fake_llm)
    block_items = [
        (0, 37.5, 415.0, 145.5, 424.0, "新能源汽车第三者责任保险"),
        (0, 316.4, 412.5, 363.9, 424.9, "1500000.00"),
        (0, 400.3, 412.5, 432.8, 424.9, "3416.08"),
        (0, 456.9, 412.5, 461.9, 424.9, "0"),
    ]
    frag, ded_raws, _, _ = ci._commercial_pass2_doubao_single_lab(
        "新能源汽车第三者责任保险",
        [(0, "ctx")],
        ci.KnownInsuranceCompany.HUA_AN,
        engine_label="test",
        collect_ded=True,
        block_items=block_items,
    )
    assert frag["新能源汽车第三者责任保险保额"] == "1500000.00"
    assert frag["新能源汽车第三者责任保险保费"] == "3416.08"
    assert ded_raws == []

    frag2, ded_raws2, _, _ = ci._commercial_pass2_doubao_single_lab(
        "新能源汽车损失保险",
        [(0, "ctx")],
        ci.KnownInsuranceCompany.HUA_AN,
        engine_label="test",
        collect_ded=True,
        block_items=[(0, 37.5, 415.0, 145.5, 424.0, "新能源汽车损失保险"), *block_items[1:]],
    )
    assert frag2["新能源汽车损失保险保额"] == "1500000.00"
    assert frag2["新能源汽车损失保险保费"] == "3416.08"
    assert ded_raws2 == ["0"]


def test_pass1_damage_deductible_from_blocks_bohai_and_zhongyin_patterns() -> None:
    assert (
        ci._pass1_damage_deductible_from_blocks(
            ["车辆损失险的绝对免赔额： 元"],
            engine_label="test",
        )
        is None
    )
    assert (
        ci._pass1_damage_deductible_from_blocks(
            ["车 损 险 每 次 事故 绝对免赔额 ￥ （元）"],
            engine_label="test",
        )
        is None
    )
    assert (
        ci._pass1_damage_deductible_from_blocks(
            ["车损险每次事故绝对免赔额 ￥ 500 元"],
            engine_label="test",
        )
        == "500.00"
    )
    assert (
        ci._pass1_damage_deductible_from_blocks(
            ["第十二条 对于投保人与保险人在投保时协商确定绝对免赔额的，增加每次事故绝对免赔额。"],
            engine_label="test",
        )
        is None
    )


def test_pass2_damage_deductible_from_word_items_split_bohai_field() -> None:
    word_items = [
        (0, 1, 38.6, 549.4, 158.6, 559.4, "车辆损失险的绝对免赔额：", 27, 0, 0),
        (0, 2, 255.9, 548.4, 265.9, 558.4, "元", 27, 1, 0),
        (0, 3, 211.0, 550.2, 215.1, 560.6, "0", 117, 0, 0),
        (0, 4, 481.1, 564.5, 508.2, 574.9, "8922.32", 93, 0, 0),
    ]

    assert ci._pass2_damage_deductible_from_word_items(word_items, engine_label="test") == "0"


def test_commercial_pass2_neighbor_blob_suggests_detail_llm() -> None:
    assert not _commercial_pass2_neighbor_blob_suggests_detail_llm("")
    assert not _commercial_pass2_neighbor_blob_suggests_detail_llm("   \n仅条款说明无数字元后缀 ")
    assert _commercial_pass2_neighbor_blob_suggests_detail_llm("保额 1,234.56 元")
    assert _commercial_pass2_neighbor_blob_suggests_detail_llm("4座×50000元/座")
    assert _commercial_pass2_neighbor_blob_suggests_detail_llm("乘客责任")


def test_commercial_pass2_row_neighbor_blocks_flat_text() -> None:
    """同页纵带 + 水平窗：锚块与右侧金额块应被拼入。"""
    items = [
        (0, 100.0, 100.0, 200.0, 130.0, "新能源汽车第三者责任保险"),
        (0, 210.0, 105.0, 320.0, 125.0, "2000000.00元\n2989.07"),
        (0, 50.0, 400.0, 90.0, 420.0, "另一行远竖向"),
    ]
    blob = _commercial_pass2_row_neighbor_blocks_flat_text(
        items,
        ref_flat_index=0,
        y_pad_pt=_COMMERCIAL_PASS2_PREFILTER_Y_PAD_PT,
        x_left_pt=380.0,
        x_right_pt=520.0,
    )
    assert "新能源汽车第三者责任保险" in blob
    assert "2000000.00元" in blob
    assert "另一行远竖向" not in blob


if __name__ == "__main__":
    try:
        test_find_dates_in_text_new_formats()
        test_convert_to_iso_format_new_formats()
        test_extract_period_from_patterns_new_formats()
        print("✅ 所有测试通过！")
    except AssertionError as e:
        print(f"❌ 测试失败: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"⚠️ 运行时错误: {e}")
        sys.exit(1)
