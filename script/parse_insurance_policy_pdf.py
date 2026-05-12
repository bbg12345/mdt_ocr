#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
从车险保单等 PDF 中提取嵌入文本并打印到标准输出（不调用 OCR）。

用法::

    python script/parse_insurance_policy_pdf.py
    python script/parse_insurance_policy_pdf.py /path/to/policy.pdf
    python script/parse_insurance_policy_pdf.py 'https://.../x.pdf' --char-boxes
    python script/parse_insurance_policy_pdf.py x.pdf --text-with-char-boxes
    python script/parse_insurance_policy_pdf.py x.pdf --block-boxes
    python script/parse_insurance_policy_pdf.py x.pdf --text-with-block-boxes

依赖：``pip install -r requirements.txt``（pypdf + PyMuPDF；字符用 ``rawdict``，块用 ``dict`` 的 ``blocks``）。
"""

from __future__ import annotations

import argparse
import io
import json
import sys
from pathlib import Path
from typing import Any, Dict, Iterator, List, Tuple
from urllib.request import Request, urlopen


def _load_pdf_bytes(path_or_url: str) -> bytes:
    if path_or_url.startswith(("http://", "https://")):
        req = Request(
            path_or_url,
            headers={"User-Agent": "mdt_ocr_service/parse_insurance_policy_pdf"},
        )
        with urlopen(req, timeout=120) as resp:
            return resp.read()
    p = Path(path_or_url).expanduser().resolve()
    if not p.is_file():
        raise FileNotFoundError(str(p))
    return p.read_bytes()


def _block_text_from_dict(block: Dict[str, Any]) -> str:
    """从 get_text(\"dict\") 的单个 text block 拼出块内字符串。"""
    parts: List[str] = []
    for line in block.get("lines", []):
        for span in line.get("spans", []):
            parts.append(span.get("text") or "")
    return "".join(parts)


def _iter_text_blocks(page: Any) -> Iterator[Tuple[Tuple[float, float, float, float], str]]:
    """PyMuPDF：每个文本块一个 bbox (x0,y0,x1,y1) 与块内全文（dict 顺序）。"""
    d: Dict[str, Any] = page.get_text("dict")
    for block in d.get("blocks", []):
        if block.get("type") != 0:
            continue
        bb = block.get("bbox")
        if bb is None:
            continue
        t = tuple(float(x) for x in bb)
        yield t, _block_text_from_dict(block)


def _iter_char_boxes(page: Any) -> Iterator[Tuple[str, Tuple[float, float, float, float]]]:
    """PyMuPDF page：按 rawdict 遍历每个字形/字符及其 bbox (x0,y0,x1,y1)。"""
    raw: Dict[str, Any] = page.get_text("rawdict")
    for block in raw.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                for ch in span.get("chars", []):
                    c = ch.get("c") or ""
                    bbox = ch.get("bbox")
                    if bbox is None:
                        continue
                    t = tuple(float(x) for x in bbox)
                    yield c, (t[0], t[1], t[2], t[3])


def _print_char_boxes(pdf_bytes: bytes, char_boxes_max: int) -> Tuple[int, int]:
    """返回 (已打印条数, 全文 glyph 总数)。"""
    import fitz

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    printed = 0
    try:
        per_page: List[List[Tuple[str, Tuple[float, float, float, float]]]] = []
        for page_index in range(doc.page_count):
            per_page.append(list(_iter_char_boxes(doc[page_index])))
        total_glyphs = sum(len(x) for x in per_page)

        for page_index, page_chars in enumerate(per_page):
            print(
                f"\n--- 第 {page_index + 1} 页 / 共 {doc.page_count} 页 "
                f"(字符级 bbox 数: {len(page_chars)}) ---\n"
            )
            for c, bb in page_chars:
                if char_boxes_max > 0 and printed >= char_boxes_max:
                    break
                x0, y0, x1, y1 = bb
                show = c if c.isprintable() else repr(c)
                print(
                    f"{printed + 1:6d}  {show!s:12s}  "
                    f"x0={x0:.2f} y0={y0:.2f} x1={x1:.2f} y1={y1:.2f}"
                )
                printed += 1
            if char_boxes_max > 0 and printed >= char_boxes_max:
                break

        if char_boxes_max > 0 and printed < total_glyphs:
            print(
                f"\n… 已按 --char-boxes-max={char_boxes_max} 截断；"
                f"本 PDF 字符级 bbox 总数: {total_glyphs}。"
            )
    finally:
        doc.close()
    return printed, total_glyphs


def _print_text_with_char_boxes(pdf_bytes: bytes, char_boxes_max: int) -> Tuple[int, int]:
    """
    每页先输出 PyMuPDF 拼好的纯文本（与几何同源），再输出 rawdict 顺序的「字符 + bbox」表。
    表格式：序号 TAB x0 TAB y0 TAB x1 TAB y1 TAB JSON 字符串（单字符，便于含空白/制表符）。
    返回 (已打印表行数, 全文 glyph 总数)。
    """
    import fitz

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    table_printed = 0
    total_glyphs = 0
    try:
        for page_index in range(doc.page_count):
            page = doc[page_index]
            page_chars = list(_iter_char_boxes(page))
            total_glyphs += len(page_chars)

            print(f"\n{'=' * 60}")
            print(f"第 {page_index + 1} 页 / 共 {doc.page_count} 页")
            print(f"{'=' * 60}")
            print("\n【纯文本】（PyMuPDF get_text(\"text\")）\n")
            print(page.get_text("text") or "")

            print("\n【字符与 bbox】（rawdict 顺序；坐标单位 pt，PDF 用户空间）")
            print("# 列: 序号\\tx0\\ty0\\tx1\\ty1\\tJSON 单字符（含空白时用 JSON 转义）")

            for c, bb in page_chars:
                if char_boxes_max > 0 and table_printed >= char_boxes_max:
                    break
                x0, y0, x1, y1 = bb
                line = (
                    f"{table_printed + 1}\t{x0:.4f}\t{y0:.4f}\t{x1:.4f}\t{y1:.4f}\t"
                    f"{json.dumps(c, ensure_ascii=False)}"
                )
                print(line)
                table_printed += 1

            if char_boxes_max > 0 and table_printed >= char_boxes_max:
                break

        if char_boxes_max > 0 and table_printed < total_glyphs:
            print(
                f"\n… 字符 bbox 表已按 --char-boxes-max={char_boxes_max} 截断；"
                f"总 glyph 数: {total_glyphs}。"
            )
    finally:
        doc.close()
    return table_printed, total_glyphs


def _print_block_boxes(pdf_bytes: bytes, block_max: int) -> Tuple[int, int]:
    """仅输出块级 bbox + 块文本。返回 (已打印块数, 全文块总数)。"""
    import fitz

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    printed = 0
    try:
        total_blocks = sum(
            len(list(_iter_text_blocks(doc[i]))) for i in range(doc.page_count)
        )
        for page_index in range(doc.page_count):
            page_blocks = list(_iter_text_blocks(doc[page_index]))
            if not page_blocks:
                continue
            print(
                f"\n--- 第 {page_index + 1} 页 / 共 {doc.page_count} 页 "
                f"(文本块数: {len(page_blocks)}) ---\n"
            )
            for bb, txt in page_blocks:
                if block_max > 0 and printed >= block_max:
                    break
                x0, y0, x1, y1 = bb
                print(f"块 #{printed + 1}  x0={x0:.2f} y0={y0:.2f} x1={x1:.2f} y1={y1:.2f}")
                print("----")
                print(txt)
                print()
                printed += 1
            if block_max > 0 and printed >= block_max:
                break

        if block_max > 0 and printed < total_blocks:
            print(
                f"… 已按 --char-boxes-max={block_max} 截断；"
                f"本 PDF 文本块总数: {total_blocks}。"
            )
    finally:
        doc.close()
    return printed, total_blocks


def _print_text_with_block_boxes(pdf_bytes: bytes, block_max: int) -> Tuple[int, int]:
    """每页先整页纯文本，再按块输出 bbox + 块内文本。"""
    import fitz

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    table_printed = 0
    try:
        total_blocks = sum(
            len(list(_iter_text_blocks(doc[i]))) for i in range(doc.page_count)
        )
        for page_index in range(doc.page_count):
            page = doc[page_index]
            page_blocks = list(_iter_text_blocks(page))

            print(f"\n{'=' * 60}")
            print(f"第 {page_index + 1} 页 / 共 {doc.page_count} 页")
            print(f"{'=' * 60}")
            print("\n【纯文本】（PyMuPDF get_text(\"text\")）\n")
            print(page.get_text("text") or "")

            print("\n【文本块与 bbox】（get_text(\"dict\") 顺序；坐标单位 pt）")
            print("# 每块：序号 + 外接矩形 + 块内拼接文本")

            for bb, txt in page_blocks:
                if block_max > 0 and table_printed >= block_max:
                    break
                x0, y0, x1, y1 = bb
                print(f"\n块 #{table_printed + 1}\tx0={x0:.4f}\ty0={y0:.4f}\tx1={x1:.4f}\ty1={y1:.4f}")
                print("----")
                print(txt)
                table_printed += 1

            if block_max > 0 and table_printed >= block_max:
                break

        if block_max > 0 and table_printed < total_blocks:
            print(
                f"\n… 文本块列表已按 --char-boxes-max={block_max} 截断；"
                f"总块数: {total_blocks}。"
            )
    finally:
        doc.close()
    return table_printed, total_blocks


def main() -> int:
    default_pdf = Path.home() / "Downloads" / "insured-book-test.pdf"
    parser = argparse.ArgumentParser(
        description="从 PDF 提取纯文本，或字符/文本块级 bbox（文本层，不 OCR）",
    )
    parser.add_argument(
        "pdf",
        nargs="?",
        default=str(default_pdf),
        help=f"PDF 本地路径或 http(s) URL，默认: {default_pdf}",
    )
    box_mode = parser.add_mutually_exclusive_group()
    box_mode.add_argument(
        "--char-boxes",
        action="store_true",
        help="仅输出字符级 bbox 列表（PyMuPDF rawdict）",
    )
    box_mode.add_argument(
        "--text-with-char-boxes",
        action="store_true",
        help="先输出每页完整纯文本，再输出每个字符的 bbox（PyMuPDF）",
    )
    box_mode.add_argument(
        "--block-boxes",
        action="store_true",
        help="仅按文本块输出：每块一个 bbox + 块内文字（PyMuPDF get_text dict）",
    )
    box_mode.add_argument(
        "--text-with-block-boxes",
        action="store_true",
        help="先输出每页完整纯文本，再按块输出 bbox + 块内文字",
    )
    parser.add_argument(
        "--char-boxes-max",
        type=int,
        default=400,
        metavar="N",
        help="字符模式：最多 N 条/行；块模式：最多 N 个块；0 不限制，默认 400",
    )
    args = parser.parse_args()
    path_or_url = args.pdf.strip()

    try:
        pdf_bytes = _load_pdf_bytes(path_or_url)
    except FileNotFoundError as e:
        print(f"错误：{e}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"错误：无法读取 PDF（{e}）", file=sys.stderr)
        return 1

    display = path_or_url
    print(f"来源: {display}")
    print(f"字节大小: {len(pdf_bytes)}")

    if args.char_boxes:
        try:
            printed, total = _print_char_boxes(
                pdf_bytes,
                char_boxes_max=max(0, args.char_boxes_max),
            )
        except ImportError:
            print(
                "错误：未安装 PyMuPDF。请执行: pip install -r requirements.txt",
                file=sys.stderr,
            )
            return 1
        print("=" * 60)
        print(f"字符级 bbox 打印条数: {printed}；总 glyph 数: {total}")
        return 0

    if args.text_with_char_boxes:
        try:
            rows, total = _print_text_with_char_boxes(
                pdf_bytes,
                char_boxes_max=max(0, args.char_boxes_max),
            )
        except ImportError:
            print(
                "错误：未安装 PyMuPDF。请执行: pip install -r requirements.txt",
                file=sys.stderr,
            )
            return 1
        print("=" * 60)
        print(f"字符 bbox 表行数: {rows}；总 glyph 数: {total}")
        return 0

    if args.block_boxes:
        try:
            n_print, n_total = _print_block_boxes(
                pdf_bytes,
                block_max=max(0, args.char_boxes_max),
            )
        except ImportError:
            print(
                "错误：未安装 PyMuPDF。请执行: pip install -r requirements.txt",
                file=sys.stderr,
            )
            return 1
        print("=" * 60)
        print(f"文本块打印数: {n_print}；总块数: {n_total}")
        return 0

    if args.text_with_block_boxes:
        try:
            n_print, n_total = _print_text_with_block_boxes(
                pdf_bytes,
                block_max=max(0, args.char_boxes_max),
            )
        except ImportError:
            print(
                "错误：未安装 PyMuPDF。请执行: pip install -r requirements.txt",
                file=sys.stderr,
            )
            return 1
        print("=" * 60)
        print(f"文本块打印数: {n_print}；总块数: {n_total}")
        return 0

    try:
        from pypdf import PdfReader
    except ImportError:
        print("错误：未安装 pypdf。请执行: pip install -r requirements.txt", file=sys.stderr)
        return 1

    reader = PdfReader(io.BytesIO(pdf_bytes))
    n = len(reader.pages)
    print(f"页数: {n}")
    print("=" * 60)

    total_chars = 0
    for i in range(n):
        text = reader.pages[i].extract_text() or ""
        total_chars += len(text)
        print(f"\n--- 第 {i + 1} 页 / 共 {n} 页 (字符数: {len(text)}) ---\n")
        print(text)

    print("=" * 60)
    print(f"全文合计字符数: {total_chars}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
