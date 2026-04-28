#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
车险保单字段抽取业务逻辑（pypdf / PyMuPDF 双路块序列、保险公司名称 pass 等）。

HTTP 层见 ``ocr_service``；PDF 通用工具见 ``common``。
"""

from __future__ import annotations

import logging
import re
from enum import Enum
from typing import Any, Dict, List, Literal, Optional, Sequence, Tuple

from common import (
    extract_pypdf_page_texts,
    iter_pymupdf_text_blocks,
    load_pdf_bytes,
    split_pypdf_text_into_blocks,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 响应 kv 键（与 HTTP 约定）
# ---------------------------------------------------------------------------

_CAR_INSURANCE_COMMON_KV_KEYS: tuple[str, ...] = (
    "被保险人",
    "保险期间",
    "保险公司名称",
    "签单日期",
    "保险费合计",
)
_CAR_INSURANCE_COMMERCIAL_ONLY_KV_KEYS: tuple[str, ...] = (
    "新能源汽车损失保险保费",
    "新能源汽车损失保险保额",
    "新能源汽车第三者责任保险保费",
    "新能源汽车第三者责任保险保额",
    "新能源汽车车上人员责任保险（司机）保费",
    "新能源汽车车上人员责任保险（司机）保额",
    "新能源汽车车上人员责任保险(乘客)保费",
    "新能源汽车车上人员责任保险(乘客)保额",
)


def _car_insurance_empty_kv(policy_type: str) -> Dict[str, Any]:
    kv: Dict[str, Any] = {k: None for k in _CAR_INSURANCE_COMMON_KV_KEYS}
    if policy_type == "commercial":
        for k in _CAR_INSURANCE_COMMERCIAL_ONLY_KV_KEYS:
            kv[k] = None
    return kv


def flatten_pypdf_blocks(pdf_bytes: bytes) -> List[str]:
    """pypdf：各页按空白分块后顺序拼接。"""
    out: List[str] = []
    for pt in extract_pypdf_page_texts(pdf_bytes):
        out.extend(split_pypdf_text_into_blocks(pt))
    return out


# ---------------------------------------------------------------------------
# pass1 保险公司名列表与枚举
# ---------------------------------------------------------------------------

INSURER_COMPANY_NAMES_PASS1: Tuple[str, ...] = (
    "中国平安财产保险",
    "华安财产保险",
    "渤海财产保险",
    "中国人民财产保险",
    "中国人寿财产保险",
    "泰康在线财产保险",
    "太平洋财产保险",
    "中银保险",
    "阳光财产保险",
)


class KnownInsuranceCompany(str, Enum):
    """pass4 在命中项中匹配下列表后回填的枚举（内存判断用）。"""

    PING_AN = "中国平安财产保险"
    HUA_AN = "华安财产保险"
    BO_HAI = "渤海财产保险"
    PICC_P = "中国人民财产保险"
    CHINA_LIFE_P = "中国人寿财产保险"
    TAIKANG_ONLINE = "泰康在线财产保险"
    CPIC_P = "太平洋财产保险"
    ZHONG_YIN = "中银保险"
    YANG_GUANG = "阳光财产保险"


_NAME_TO_ENUM: Dict[str, KnownInsuranceCompany] = {
    "中国平安财产保险": KnownInsuranceCompany.PING_AN,
    "华安财产保险": KnownInsuranceCompany.HUA_AN,
    "渤海财产保险": KnownInsuranceCompany.BO_HAI,
    "中国人民财产保险": KnownInsuranceCompany.PICC_P,
    "中国人寿财产保险": KnownInsuranceCompany.CHINA_LIFE_P,
    "泰康在线财产保险": KnownInsuranceCompany.TAIKANG_ONLINE,
    "太平洋财产保险": KnownInsuranceCompany.CPIC_P,
    "中银保险": KnownInsuranceCompany.ZHONG_YIN,
    "阳光财产保险": KnownInsuranceCompany.YANG_GUANG,
}


def _is_chinese_char(ch: str) -> bool:
    if len(ch) != 1:
        return False
    o = ord(ch)
    if o == 0x3007:
        return True
    if 0x3400 <= o <= 0x4DBF:
        return True
    if 0x4E00 <= o <= 0x9FFF:
        return True
    if 0xF900 <= o <= 0xFAFF:
        return True
    if 0x20000 <= o <= 0x323AF:
        return True
    return False


def _extend_pass1_hit_within_block(block: str, match_start: int, match_end: int) -> str:
    left = match_start
    while left > 0 and _is_chinese_char(block[left - 1]):
        left -= 1
    right = match_end
    while right < len(block) and _is_chinese_char(block[right]):
        right += 1
    return block[left:right]


def _collect_all_pass1_hits(
    blocks: Sequence[str],
) -> List[Tuple[int, int, int, str]]:
    out: List[Tuple[int, int, int, str]] = []
    for bi, block in enumerate(blocks):
        for name in INSURER_COMPANY_NAMES_PASS1:
            pos = 0
            while pos <= len(block):
                idx = block.find(name, pos)
                if idx == -1:
                    break
                out.append((bi, idx, idx + len(name), name))
                pos = idx + len(name)
    out.sort(key=lambda h: (h[0], h[1]))
    return out


def _prefix_all_chinese_before(block: str, match_start: int) -> bool:
    for i in range(match_start):
        if not _is_chinese_char(block[i]):
            return False
    return True


def _choose_pass1_hit(
    hits: Sequence[Tuple[int, int, int, str]],
    blocks: Sequence[str],
) -> Optional[Tuple[int, int, int, str]]:
    if not hits:
        return None
    if len(hits) == 1:
        return hits[0]

    kw_hits = [h for h in hits if "公司名称" in blocks[h[0]]]
    if kw_hits:
        candidates = kw_hits
    else:
        candidates = list(hits)

    if len(candidates) == 1:
        return candidates[0]

    prefix_ok = [h for h in candidates if _prefix_all_chinese_before(blocks[h[0]], h[1])]
    if len(prefix_ok) == 1:
        return prefix_ok[0]

    return sorted(candidates, key=lambda h: (h[0], h[1]))[-1]


def _pass2_company_name_from_block(block: str) -> Optional[str]:
    if "公司名称" not in block:
        return None
    parts = re.split(r"[:：]", block, maxsplit=1)
    if len(parts) < 2:
        return None
    second = re.sub(r"[\s\r\n\t]+", "", parts[1])
    return second if second else None


def _pass4_enum_for_hit(hit: str) -> Optional[KnownInsuranceCompany]:
    for name in INSURER_COMPANY_NAMES_PASS1:
        if name in hit:
            return _NAME_TO_ENUM.get(name)
    return None


def _pass3_filter_insurer_name(
    value: Optional[str],
    *,
    from_pass: int,
    engine_label: str,
) -> Optional[str]:
    if value is None:
        return None
    if value.endswith("公司"):
        return value
    logger.debug(
        '%s pass %s: 过滤命中保险公司名称 "%s"',
        engine_label,
        from_pass,
        value,
    )
    return None


def run_car_insurance_insurer_passes(
    blocks: Sequence[str],
    *,
    engine_label: str,
) -> Dict[str, Any]:
    pass1_hit: Optional[str] = None
    pass1_block_idx: Optional[int] = None
    all_pass1 = _collect_all_pass1_hits(blocks)
    if all_pass1:
        logger.debug("[%s] pass 1 全部命中 %s 条：%s", engine_label, len(all_pass1), all_pass1)
        chosen = _choose_pass1_hit(all_pass1, blocks)
        if chosen is not None:
            bi, m0, m1, matched_name = chosen
            block = blocks[bi]
            pass1_hit = _extend_pass1_hit_within_block(block, m0, m1)
            pass1_block_idx = bi
            logger.debug(
                "[%s] pass 1 选用：value=%r matched_substring=%r block=%r block_index=%s",
                engine_label,
                pass1_hit,
                matched_name,
                block,
                bi,
            )

    pass1_hit = _pass3_filter_insurer_name(pass1_hit, from_pass=1, engine_label=engine_label)
    if pass1_hit is None:
        pass1_block_idx = None

    pass2_hit: Optional[str] = None
    pass2_block_idx: Optional[int] = None
    if pass1_hit is None:
        for bi, block in enumerate(blocks):
            v = _pass2_company_name_from_block(block)
            if v is not None:
                pass2_hit = v
                pass2_block_idx = bi
                logger.debug(
                    "[%s] pass 2 命中：保险公司名称=%r block_index=%s",
                    engine_label,
                    v,
                    bi,
                )
                break

    pass2_hit = _pass3_filter_insurer_name(pass2_hit, from_pass=2, engine_label=engine_label)
    if pass2_hit is None:
        pass2_block_idx = None

    insurer_display: Optional[str] = None
    if pass1_hit is not None:
        insurer_display = pass1_hit
    elif pass2_hit is not None:
        insurer_display = pass2_hit

    known_company: Optional[KnownInsuranceCompany] = None
    if insurer_display is not None:
        known_company = _pass4_enum_for_hit(insurer_display)
        if known_company is not None:
            logger.debug(
                "[%s] pass 4 命中：known_company=%s",
                engine_label,
                known_company.name,
            )

    return {
        "pass1_hit": pass1_hit,
        "pass1_block_index": pass1_block_idx,
        "pass2_hit": pass2_hit,
        "pass2_block_index": pass2_block_idx,
        "保险公司名称": insurer_display,
        "known_company": known_company,
    }


def _find_dates_in_text(text: str) -> List[str]:
    """
    在文本中查找日期时间字符串，返回所有匹配的列表。
    只匹配同时包含日期和时间的格式（移除空格后匹配）：
    - 2025年5月28日17:41:37
    - 2025年5月28日17时41分
    - 2025年5月28日17时41分59秒
    - 2025-05-2817:41:37
    - 2025/05/2817:41:37
    """
    # 移除所有空白字符（空格、制表符、换行等）
    text_no_spaces = re.sub(r'\s+', '', text)

    patterns = [
        # 完整日期时间：年-月-日时:分:秒
        r'\d{4}[-/]\d{1,2}[-/]\d{1,2}\d{1,2}:\d{1,2}:\d{1,2}',
        # 完整日期时间：年月日时分秒（中文）
        r'\d{4}年\d{1,2}月\d{1,2}日\d{1,2}时\d{1,2}分\d{1,2}秒',
        r'\d{4}年\d{1,2}月\d{1,2}日\d{1,2}时\d{1,2}分',
        r'\d{4}年\d{1,2}月\d{1,2}日\d{1,2}:\d{1,2}:\d{1,2}',
        # 新增格式 - 按从具体到通用排序
        r'\d{4}年\d{1,2}月\d{1,2}日\d{1,2}:\d{1,2}时',  # 00:00时（最具体）
        r'\d{4}年\d{1,2}月\d{1,2}日\d{1,2}:\d{1,2}',     # 00:00
        r'\d{4}年\d{1,2}月\d{1,2}日\d{1,2}时',           # 00时
        r'\d{4}[-/]\d{1,2}[-/]\d{1,2}\d{1,2}:\d{1,2}',   # 2025-05-1800:00
    ]

    dates = []
    for pattern in patterns:
        matches = re.findall(pattern, text_no_spaces)
        for match in matches:
            # 去重并防止子串匹配
            if match not in dates:
                # 检查是否已经包含该匹配的更长版本
                has_longer_match = any(match in existing and match != existing for existing in dates)
                if not has_longer_match:
                    # 删除任何已存在的该匹配的子串
                    dates = [d for d in dates if d not in match]
                    dates.append(match)
    return dates


def _convert_to_iso_format(date_str: str) -> Optional[str]:
    """
    将中文日期时间字符串转换为ISO 8601格式（YYYY-MM-DDTHH:mm:ss）。
    支持格式：
    - 2025年07月09日00时00分
    - 2025年07月09日00时00分00秒
    - 2025年07月09日00:00:00
    - 2025-07-09 00:00:00
    - 2025-07-0900:00:00
    - 2025/07/09 00:00:00
    - 2025年07月09日（自动补全时间为00:00:00）
    - 2025-07-09（自动补全时间为00:00:00）
    """
    import re

    # 移除所有空白字符
    text = re.sub(r'\s+', '', date_str)

    # 定义匹配模式
    patterns = [
        # 完整日期时间：年月日时分秒（中文）
        (r'(\d{4})年(\d{1,2})月(\d{1,2})日(\d{1,2})时(\d{1,2})分(\d{1,2})秒',
         lambda m: f"{m[1]}-{m[2]:0>2}-{m[3]:0>2}T{m[4]:0>2}:{m[5]:0>2}:{m[6]:0>2}"),
        (r'(\d{4})年(\d{1,2})月(\d{1,2})日(\d{1,2})时(\d{1,2})分',
         lambda m: f"{m[1]}-{m[2]:0>2}-{m[3]:0>2}T{m[4]:0>2}:{m[5]:0>2}:00"),
        (r'(\d{4})年(\d{1,2})月(\d{1,2})日(\d{1,2}):(\d{1,2}):(\d{1,2})',
         lambda m: f"{m[1]}-{m[2]:0>2}-{m[3]:0>2}T{m[4]:0>2}:{m[5]:0>2}:{m[6]:0>2}"),
        (r'(\d{4})年(\d{1,2})月(\d{1,2})日(\d{1,2}):(\d{1,2})时',
         lambda m: f"{m[1]}-{m[2]:0>2}-{m[3]:0>2}T{m[4]:0>2}:{m[5]:0>2}:00"),
        (r'(\d{4})年(\d{1,2})月(\d{1,2})日(\d{1,2}):(\d{1,2})',
         lambda m: f"{m[1]}-{m[2]:0>2}-{m[3]:0>2}T{m[4]:0>2}:{m[5]:0>2}:00"),
        (r'(\d{4})年(\d{1,2})月(\d{1,2})日(\d{1,2})时',
         lambda m: f"{m[1]}-{m[2]:0>2}-{m[3]:0>2}T{m[4]:0>2}:00:00"),

        # 完整日期时间：年-月-日时:分:秒
        (r'(\d{4})[-/](\d{1,2})[-/](\d{1,2})(\d{1,2}):(\d{1,2}):(\d{1,2})',
         lambda m: f"{m[1]}-{m[2]:0>2}-{m[3]:0>2}T{m[4]:0>2}:{m[5]:0>2}:{m[6]:0>2}"),
        (r'(\d{4})[-/](\d{1,2})[-/](\d{1,2})\s*(\d{1,2}):(\d{1,2}):(\d{1,2})',
         lambda m: f"{m[1]}-{m[2]:0>2}-{m[3]:0>2}T{m[4]:0>2}:{m[5]:0>2}:{m[6]:0>2}"),
        (r'(\d{4})[-/](\d{1,2})[-/](\d{1,2})(\d{1,2}):(\d{1,2})',
         lambda m: f"{m[1]}-{m[2]:0>2}-{m[3]:0>2}T{m[4]:0>2}:{m[5]:0>2}:00"),
        (r'(\d{4})[-/](\d{1,2})[-/](\d{1,2})\s*(\d{1,2}):(\d{1,2})',
         lambda m: f"{m[1]}-{m[2]:0>2}-{m[3]:0>2}T{m[4]:0>2}:{m[5]:0>2}:00"),

        # 只有日期部分
        (r'(\d{4})年(\d{1,2})月(\d{1,2})日',
         lambda m: f"{m[1]}-{m[2]:0>2}-{m[3]:0>2}T00:00:00"),
        (r'(\d{4})[-/](\d{1,2})[-/](\d{1,2})',
         lambda m: f"{m[1]}-{m[2]:0>2}-{m[3]:0>2}T00:00:00"),
    ]

    for pattern, converter in patterns:
        match = re.match(pattern, text)
        if match:
            try:
                return converter(match)
            except (ValueError, IndexError):
                continue

    # 无法转换
    logger.debug("无法转换日期时间格式: %r", date_str)
    return None


def _extract_period_from_dates(dates: List[str]) -> Optional[Dict[str, str]]:
    """
    从日期列表中提取起始和结束时间。
    假设前两个日期分别是起始和结束时间。
    返回ISO 8601格式的日期时间。
    """
    if len(dates) < 2:
        return None

    # 过滤掉空或纯空格的日期
    valid_dates = [d.strip() for d in dates if d.strip()]
    if len(valid_dates) < 2:
        return None

    # 尝试找到一对合理的日期
    # 简单策略：取前两个
    start_raw = valid_dates[0]
    end_raw = valid_dates[1]

    # 转换为ISO格式
    start_iso = _convert_to_iso_format(start_raw)
    end_iso = _convert_to_iso_format(end_raw)

    if not start_iso or not end_iso:
        logger.debug("无法转换日期格式: start=%r, end=%r", start_raw, end_raw)
        return None

    return {"start": start_iso, "end": end_iso}


def _find_period_blocks_indices(blocks: Sequence[str]) -> List[int]:
    """查找包含'保险期间'的块索引"""
    return [i for i, block in enumerate(blocks) if "保险期间" in block]


def _extract_period_from_dates_in_blocks(
    blocks: Sequence[str],
    period_blocks_indices: List[int],
    engine_label: str,
) -> Optional[Dict[str, str]]:
    """从包含'保险期间'的块中提取日期信息"""
    if not period_blocks_indices:
        return None

    # 从第一个包含"保险期间"的块开始，向后查找日期
    start_idx = period_blocks_indices[0]
    # 扩大搜索范围，最多查看后续5个块
    end_idx = min(start_idx + 6, len(blocks))  # 当前块 + 后续5个块
    search_blocks = blocks[start_idx:end_idx]
    search_text = " ".join(search_blocks)

    dates = _find_dates_in_text(search_text)
    logger.debug("[%s] 在保险期间相关块中找到日期: %s", engine_label, dates)

    if len(dates) >= 2:
        period = _extract_period_from_dates(dates)
        if period:
            logger.debug("[%s] 提取到保险期间: %s", engine_label, period)
            return period

    return None


def _extract_period_from_patterns(
    blocks: Sequence[str],
    period_blocks_indices: List[int],
    engine_label: str,
) -> Optional[Dict[str, str]]:
    """通过模式匹配从块中提取保险期间，返回ISO 8601格式"""
    patterns = [
        r'自\s*([^起]+?)\s*起\s*至\s*([^止]+?)\s*止',
        r'自\s*([^至]+?)\s*至\s*([^止]+?)\s*止',
        r'自\s*(.+?)\s*起\s*至\s*(.+?)\s*止',
        r'自\s*(.+?)\s*至\s*(.+?)\s*止',
    ]

    for i in period_blocks_indices:
        block = blocks[i]
        for pattern in patterns:
            match = re.search(pattern, block)
            if match:
                start_raw = match.group(1).strip()
                end_raw = match.group(2).strip()
                # 检查是否非空
                if start_raw and end_raw:
                    logger.debug("[%s] 通过模式匹配提取保险期间: %s -> %s",
                               engine_label, start_raw, end_raw)

                    # 转换为ISO格式
                    start_iso = _convert_to_iso_format(start_raw)
                    end_iso = _convert_to_iso_format(end_raw)

                    if not start_iso or not end_iso:
                        logger.debug("[%s] 无法转换日期格式: start=%r, end=%r",
                                   engine_label, start_raw, end_raw)
                        return None

                    return {"start": start_iso, "end": end_iso}
                else:
                    logger.debug("[%s] 匹配到空内容，忽略: start=%r, end=%r",
                               engine_label, start_raw, end_raw)

    return None


def extract_insurance_period_from_blocks(
    blocks: Sequence[str],
    *,
    engine_label: str,
) -> Optional[Dict[str, str]]:
    """
    从文本块中提取保险期间（起止时间）。
    逻辑：
    1. 查找包含"保险期间"的块
    2. 在该块及后续块中查找日期时间pattern
    3. 找到至少两个日期时，返回第一个作为起始，第二个作为结束
    """
    # 查找包含"保险期间"的块索引
    period_blocks_indices = _find_period_blocks_indices(blocks)

    if not period_blocks_indices:
        logger.debug("[%s] 未找到包含'保险期间'的块", engine_label)
        return None

    logger.debug("[%s] 找到 %d 个包含'保险期间'的块: %s",
                 engine_label, len(period_blocks_indices), period_blocks_indices)

    # 尝试从日期中提取
    period = _extract_period_from_dates_in_blocks(blocks, period_blocks_indices, engine_label)
    if period:
        return period

    # 尝试从模式匹配中提取
    period = _extract_period_from_patterns(blocks, period_blocks_indices, engine_label)
    if period:
        return period

    logger.debug("[%s] 无法提取保险期间", engine_label)
    return None


def _extract_period_with_bbox_fallback(pdf_bytes: bytes) -> Optional[Dict[str, str]]:
    """
    使用PyMuPDF的块bbox坐标信息提取保险期间（回退逻辑）。
    1. 获取所有文本块及其bbox
    2. 找到包含"保险期间"的块，记录纵坐标范围
    3. 查找纵坐标相近的候选块（时间应该不高于保险期间块，不低于或略低于）
    4. 在候选块中匹配日期时间pattern
    """
    try:
        import fitz
    except ImportError:
        logger.debug("PyMuPDF未安装，无法使用bbox回退逻辑")
        return None

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        all_blocks = []  # 列表元素: (x0, y0, x1, y1, text)
        period_blocks = []  # 包含"保险期间"的块

        for page_index in range(doc.page_count):
            page = doc[page_index]
            raw_blocks = page.get_text("blocks")
            if not raw_blocks:
                continue

            for b in raw_blocks:
                if len(b) < 5:
                    continue
                # b[0:4] 是 x0, y0, x1, y1
                # b[4] 是文本
                # b[6] 是块类型（0=文本，1=图像）
                btype = None
                if len(b) > 6:
                    try:
                        btype = int(b[6])
                    except (TypeError, ValueError):
                        btype = None
                if btype == 1:  # 跳过图像块
                    continue

                text = b[4] if isinstance(b[4], str) else str(b[4])
                text = text.strip()
                if not text:
                    continue

                x0, y0, x1, y1 = float(b[0]), float(b[1]), float(b[2]), float(b[3])
                all_blocks.append((x0, y0, x1, y1, text))

                if "保险期间" in text:
                    period_blocks.append((x0, y0, x1, y1, text))

        if not period_blocks:
            logger.debug("bbox回退逻辑：未找到包含'保险期间'的块")
            return None

        # 使用第一个找到的"保险期间"块作为参考
        ref_x0, ref_y0, ref_x1, ref_y1, ref_text = period_blocks[0]
        logger.debug("bbox回退逻辑：参考块 y0=%.2f, y1=%.2f, 文本=%r", ref_y0, ref_y1, ref_text)

        # 定义纵坐标容差：时间块应该不高于参考块，不低于或略低于参考块
        # 不高于：候选块的y0应该 >= ref_y0 - tolerance
        # 不低于或略低于：候选块的y1应该 <= ref_y1 + tolerance
        y_tolerance = 10.0  # 点，可根据实际情况调整

        candidate_texts = []
        for x0, y0, x1, y1, text in all_blocks:
            # 跳过参考块本身
            if (x0, y0, x1, y1, text) == (ref_x0, ref_y0, ref_x1, ref_y1, ref_text):
                continue

            # 检查纵坐标条件：时间块应该不高于保险期间块，不低于或略低于
            # 条件1: y0 >= ref_y0 - y_tolerance (不高于)
            # 条件2: y1 <= ref_y1 + y_tolerance (不低于或略低于)
            if y0 >= ref_y0 - y_tolerance and y1 <= ref_y1 + y_tolerance:
                candidate_texts.append(text)

        if not candidate_texts:
            logger.debug("bbox回退逻辑：未找到符合条件的候选块")
            return None

        # 合并候选文本，查找日期
        combined_text = " ".join(candidate_texts)
        dates = _find_dates_in_text(combined_text)
        logger.debug("bbox回退逻辑：候选文本中找到日期: %s", dates)

        if len(dates) >= 2:
            period = _extract_period_from_dates(dates)
            if period:
                logger.debug("bbox回退逻辑：提取到保险期间: %s", period)
                return period

        # 如果没找到两个日期，尝试在单个候选块中查找
        for text in candidate_texts:
            dates = _find_dates_in_text(text)
            if len(dates) >= 2:
                period = _extract_period_from_dates(dates)
                if period:
                    logger.debug("bbox回退逻辑：在单个候选块中提取到保险期间: %s", period)
                    return period

        logger.debug("bbox回退逻辑：在候选块中未找到足够的日期")
        return None

    finally:
        doc.close()


def car_insurance_extract(
    policy_type: Literal["compulsory", "commercial"],
    pdf_url: str,
) -> Dict[str, Any]:
    """
    下载/读取 PDF，双路块序列跑保险公司名称相关 pass，合并为 ``kv`` 与 ``known_insurance_company``。
    """
    pdf_bytes = load_pdf_bytes(pdf_url)
    blocks_pdf = flatten_pypdf_blocks(pdf_bytes)
    blocks_mu = iter_pymupdf_text_blocks(pdf_bytes)

    r_pdf = run_car_insurance_insurer_passes(blocks_pdf, engine_label="pypdf")
    r_mu = run_car_insurance_insurer_passes(blocks_mu, engine_label="pymupdf")

    merged_name = r_mu.get("保险公司名称") or r_pdf.get("保险公司名称")
    merged_enum: Optional[KnownInsuranceCompany] = (
        r_mu.get("known_company") or r_pdf.get("known_company")
    )

    kv = _car_insurance_empty_kv(policy_type)
    kv["保险公司名称"] = merged_name

    # 提取保险期间
    period_pdf = extract_insurance_period_from_blocks(blocks_pdf, engine_label="pypdf")
    period_mu = extract_insurance_period_from_blocks(blocks_mu, engine_label="pymupdf")

    merged_period = period_mu or period_pdf
    if merged_period:
        kv["保险期间"] = merged_period
        logger.debug("提取到保险期间: %s", merged_period)
    else:
        logger.debug("未提取到保险期间，尝试bbox回退逻辑")
        period_fallback = _extract_period_with_bbox_fallback(pdf_bytes)
        if period_fallback:
            kv["保险期间"] = period_fallback
            logger.debug("bbox回退逻辑提取到保险期间: %s", period_fallback)
        else:
            logger.debug("bbox回退逻辑也未提取到保险期间")

    return {
        "kv": kv,
        "known_insurance_company": merged_enum.name if merged_enum else None,
    }
