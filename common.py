#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PDF 与纯文本抽取通用工具（pypdf / PyMuPDF），供脚本与业务模块复用。

车险保单解析见 ``car_insurance`` 模块。
"""

from __future__ import annotations

import io
import re
import unicodedata
from collections import defaultdict
from pathlib import Path
from typing import Any, DefaultDict, List, Literal, Optional, Sequence, Set, Tuple
from urllib.request import Request, urlopen


def norm_txt(s: str) -> str:
    """与 PyMuPDF 词串比较时的规范化。"""
    return unicodedata.normalize("NFC", s).strip()


def norm_paren_for_table_header_match(s: str) -> str:
    """
    表头子串 ``in`` 匹配前将全角括号转为半角，使「保险费（元）」与「保险费(元)」在 PDF 文本中互认。
    """
    return (s or "").replace("（", "(").replace("）", ")")


def format_pymupdf_block_text_like_cluster_script(s: str, max_len: int = 480) -> str:
    """
    将块/行文本压成单行可打印形态：换行/tab 压成空格、UTF-8 可替换字符、超长截断。

    ``script/cluster_policy_blocks_by_grid_lines`` 第 2、3 节 stdout 与本模块 ``extract_table`` 等均直接调用本函数，
    便于与 LLM prompt 对照。
    """
    t = s.replace("\t", " ").replace("\r", " ").replace("\n", " ")
    t = t.encode("utf-8", errors="replace").decode("utf-8")
    if len(t) > max_len:
        t = t[: max_len - 3] + "..."
    return t


def load_pdf_bytes(path_or_url: str) -> bytes:
    s = path_or_url.strip()
    if s.startswith(("http://", "https://")):
        req = Request(s, headers={"User-Agent": "mdt_ocr_service"})
        with urlopen(req, timeout=120) as resp:
            return resp.read()
    p = Path(s).expanduser().resolve()
    if not p.is_file():
        raise FileNotFoundError(str(p))
    return p.read_bytes()


def extract_pypdf_plain_text(pdf_bytes: bytes) -> str:
    """整份 PDF 纯文本（pypdf），页与页之间用分隔线。"""
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(pdf_bytes))
    parts: List[str] = []
    for i, page in enumerate(reader.pages):
        t = page.extract_text() or ""
        if len(reader.pages) > 1:
            parts.append(f"--- 第 {i + 1} / {len(reader.pages)} 页 ---\n{t}")
        else:
            parts.append(t)
    return "\n\n".join(parts)


def extract_pypdf_page_texts(pdf_bytes: bytes) -> List[str]:
    """分页纯文本（无页间装饰行）。"""
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(pdf_bytes))
    return [(p.extract_text() or "") for p in reader.pages]


def split_pypdf_text_into_blocks(page_text: str) -> List[str]:
    """
    按空白切分 block：空格、制表、换行等均视为分隔符；
    与 ``re.split(r'\\s+', ...)`` 一致。
    """
    s = page_text.strip()
    if not s:
        return []
    return [t for t in re.split(r"\s+", s) if t]


def align_pypdf_blocks_to_fitz_words(
    blocks: Sequence[str],
    words: Sequence[Tuple[Any, ...]],
    fitz_module: Any,
) -> List[Tuple[str, Optional[Any]]]:
    """
    将 pypdf 切出的 block 与 ``page.get_text('words')`` 按阅读顺序对齐。
    """
    if not blocks:
        return []
    wlist = sorted(words, key=lambda w: (w[5], w[6], w[7]))
    i = 0
    out: List[Tuple[str, Optional[Any]]] = []
    Rect = fitz_module.Rect
    for block in blocks:
        nb = norm_txt(block)
        if i >= len(wlist):
            out.append((block, None))
            continue
        matched = False
        for j in range(i, len(wlist)):
            chunk = wlist[i : j + 1]
            texts = [norm_txt(str(w[4])) for w in chunk]
            jn = "".join(texts)
            js = " ".join(texts)
            if norm_txt(jn) == nb or norm_txt(js) == nb:
                xs0 = min(float(w[0]) for w in chunk)
                ys0 = min(float(w[1]) for w in chunk)
                xs1 = max(float(w[2]) for w in chunk)
                ys1 = max(float(w[3]) for w in chunk)
                out.append((block, Rect(xs0, ys0, xs1, ys1)))
                i = j + 1
                matched = True
                break
        if not matched:
            out.append((block, None))
    return out


def iter_pymupdf_text_blocks(pdf_bytes: bytes) -> List[str]:
    """
    PyMuPDF：每页 ``get_text('blocks')`` 中文本块（非图像块）的文本，按页顺序拼接为扁平列表。
    """
    import fitz

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        out: List[str] = []
        for pi in range(doc.page_count):
            page = doc[pi]
            raw = page.get_text("blocks")
            blocks: Sequence[Tuple[Any, ...]] = raw if raw else []
            for b in blocks:
                if len(b) < 5:
                    continue
                txt = b[4] if isinstance(b[4], str) else str(b[4])
                btype: Optional[int] = None
                if len(b) > 6:
                    try:
                        btype = int(b[6])
                    except (TypeError, ValueError):
                        btype = None
                if btype == 1:
                    continue
                t = txt.strip()
                if t:
                    out.append(t)
        return out
    finally:
        doc.close()


def iter_pymupdf_block_rect_items(
    pdf_bytes: bytes,
) -> List[Tuple[int, float, float, float, float, str]]:
    """
    PyMuPDF：文本块与非 ``iter_pymupdf_text_blocks`` 同序；
    每项 ``(page_index, x0, y0, x1, y1, text)``，不含图像块，text 已 ``strip()``。
    """
    import fitz

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        out: List[Tuple[int, float, float, float, float, str]] = []
        for pi in range(doc.page_count):
            page = doc[pi]
            raw_blocks = page.get_text("blocks")
            blocks_list: Sequence[Tuple[Any, ...]] = raw_blocks if raw_blocks else []
            for b in blocks_list:
                if len(b) < 5:
                    continue
                txt = b[4] if isinstance(b[4], str) else str(b[4])
                btype: Optional[int] = None
                if len(b) > 6:
                    try:
                        btype = int(b[6])
                    except (TypeError, ValueError):
                        btype = None
                if btype == 1:
                    continue
                t = txt.strip()
                if not t:
                    continue
                x0, y0, x1, y1 = float(b[0]), float(b[1]), float(b[2]), float(b[3])
                out.append((pi, x0, y0, x1, y1, t))
        return out
    finally:
        doc.close()


def _fitz_word_int_meta(w: Sequence[Any], idx: int) -> int:
    """``get_text("words")`` 元组中 block_no / line_no / word_no；缺失或非法时为 ``-1``。"""
    if len(w) <= idx:
        return -1
    try:
        return int(float(w[idx]))
    except (TypeError, ValueError):
        return -1


def iter_pymupdf_word_rect_items(
    pdf_bytes: bytes,
) -> List[Tuple[int, int, float, float, float, float, str, int, int, int]]:
    """
    PyMuPDF：每页 ``get_text("words")`` 逐词一项，与 ``script/cluster_policy_blocks_by_grid_lines`` 第四节一致。

    每项 ``(page_index, word_index_on_page, x0, y0, x1, y1, text, block_no, line_no, word_no)``。
    ``word_index_on_page`` 为该页 ``enumerate(words, start=1)`` 的序号（与脚本「词序号」一致）；
    ``text`` 已 ``strip()``，空词跳过；``block_no`` 等缺失时为 ``-1``。
    """
    import fitz

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        out: List[Tuple[int, int, float, float, float, float, str, int, int, int]] = []
        for pi in range(doc.page_count):
            page = doc[pi]
            words = page.get_text("words") or []
            for wi, w in enumerate(words, start=1):
                if len(w) < 5:
                    continue
                x0, y0, x1, y1 = float(w[0]), float(w[1]), float(w[2]), float(w[3])
                raw = w[4]
                wt = raw if isinstance(raw, str) else str(raw)
                t = wt.strip()
                if not t:
                    continue
                bn = _fitz_word_int_meta(w, 5)
                ln = _fitz_word_int_meta(w, 6)
                wn = _fitz_word_int_meta(w, 7)
                out.append((pi, wi, x0, y0, x1, y1, t, bn, ln, wn))
        return out
    finally:
        doc.close()


def pymupdf_prompt_meta_str(n: int) -> str:
    """LLM prompt 中 block_no / line_no / word_no 等：缺失或非法时输出 ``—``。"""
    return str(n) if n >= 0 else "—"


def _extract_table_header_column_pairs(
    header_texts_in_column_order: Sequence[str],
    column_indices: Optional[Sequence[int]],
) -> List[Tuple[str, int]]:
    """
    (表头子串, 列索引) 列表；空子串跳过。
    ``column_indices`` 须与 ``header_texts_in_column_order`` 等长；缺省时列索引用表头在序列中的下标 ``i``。
    """
    n = len(header_texts_in_column_order)
    if column_indices is not None and len(column_indices) != n:
        raise ValueError(
            "column_indices 长度须与 header_texts_in_column_order 一致："
            f"{len(column_indices)} != {n}"
        )
    out: List[Tuple[str, int]] = []
    for i, h in enumerate(header_texts_in_column_order):
        ht = (h or "").strip()
        if not ht:
            continue
        col = int(column_indices[i]) if column_indices is not None else i
        out.append((ht, col))
    return out


def _extract_table_item_matched_min_column(text: str, pairs: Sequence[Tuple[str, int]]) -> int:
    """文本命中的若干表头子串里，取对应列索引的最小值（用于排序与 prompt 标注）。"""
    nt = norm_paren_for_table_header_match(text or "")
    best = 10**9
    for ht, col in pairs:
        if norm_paren_for_table_header_match(ht) in nt:
            best = min(best, col)
    return best


def extract_table(
    pdf_bytes: bytes,
    *,
    header_texts_in_column_order: Sequence[str],
    column_indices: Optional[Sequence[int]] = None,
    max_page_index: int = 1,
    exclude_substrings: Sequence[str] = (),
    source: Literal["blocks", "words"] = "blocks",
    sort_mode: Literal["document_order", "column_rank_then_geom"] = "document_order",
    words_line_format: Literal["pymupdf_meta", "bbox_column_text"] = "pymupdf_meta",
) -> str:
    """
    按列顺序给定表头子串 ``header_texts_in_column_order``，从 PyMuPDF ``blocks`` 或 ``words`` 中
    挑出表头相关项，格式化为可拼进大模型 prompt 的多行文本（坐标单位 pt）。

    - ``source="blocks"``：与 ``cluster_policy_blocks_by_grid_lines`` 第三节一致思路（``page_index``、
      ``block_index``、bbox、单行化文本）。
    - ``source="words"``：默认 ``words_line_format=pymupdf_meta`` 含 ``word_index``、``block_no`` /
      ``line_no`` / ``word_no``；``bbox_column_text`` 仅输出 ``page_index``、可选 ``column_index``、
      bbox 与单行化文本（供渤海等精简表头附录）。

    ``column_indices``（可选）：与表头子串**等长**的整数列号（通常 1-based）；提供时
    每行增加 ``column_index=``（该项命中的表头所对应列号之最小值），且 ``column_rank_then_geom``
    排序使用该整数；不传时列号退化为表头在序列中的下标，且 prompt 中不输出 ``column_index``。

    匹配：项内文本含**任一**表头子串即入选（子串与原文在比较前均经 ``norm_paren_for_table_header_match``，
    全角/半角括号等价）；``exclude_substrings`` 命中则整项丢弃。
    ``sort_mode``：``document_order`` 为 PyMuPDF 遍历顺序；``column_rank_then_geom`` 为「列索引 → 页 → y → x」排序。
    """
    pairs = _extract_table_header_column_pairs(header_texts_in_column_order, column_indices)
    if not pairs:
        return ""
    show_column_index = column_indices is not None

    def excluded(t: str) -> bool:
        return any(ex in t for ex in exclude_substrings)

    import fitz

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        n_pages = min(doc.page_count, max_page_index + 1)
        if source == "blocks":
            picked_b: List[Tuple[int, int, int, float, float, float, float, str]] = []
            seen_b: Set[Tuple[int, int]] = set()
            for pi in range(n_pages):
                raw = doc[pi].get_text("blocks") or []
                for bi, b in enumerate(raw, start=1):
                    if len(b) < 5:
                        continue
                    txt = b[4] if isinstance(b[4], str) else str(b[4])
                    btype: Optional[int] = None
                    if len(b) > 6:
                        try:
                            btype = int(b[6])
                        except (TypeError, ValueError):
                            btype = None
                    if btype == 1:
                        continue
                    t = txt.strip()
                    if not t or excluded(t):
                        continue
                    if not any(norm_paren_for_table_header_match(ht) in norm_paren_for_table_header_match(t) for ht, _ in pairs):
                        continue
                    if (pi, bi) in seen_b:
                        continue
                    seen_b.add((pi, bi))
                    x0, y0, x1, y1 = float(b[0]), float(b[1]), float(b[2]), float(b[3])
                    rank = _extract_table_item_matched_min_column(t, pairs)
                    picked_b.append((rank, pi, bi, x0, y0, x1, y1, t))
            if sort_mode == "column_rank_then_geom":
                picked_b.sort(key=lambda r: (r[0], r[1], r[4], r[3]))
            return "\n".join(
                (
                    f"page_index={pi}\tcolumn_index={rank}\tblock_index={bi}\tx0_pt={x0:.1f}\ty0_pt={y0:.1f}\t"
                    f"x1_pt={x1:.1f}\ty1_pt={y1:.1f}\t{format_pymupdf_block_text_like_cluster_script(tx)}"
                    if show_column_index
                    else f"page_index={pi}\tblock_index={bi}\tx0_pt={x0:.1f}\ty0_pt={y0:.1f}\t"
                    f"x1_pt={x1:.1f}\ty1_pt={y1:.1f}\t{format_pymupdf_block_text_like_cluster_script(tx)}"
                )
                for rank, pi, bi, x0, y0, x1, y1, tx in picked_b
            )

        picked_w: List[Tuple[int, int, int, float, float, float, float, str, int, int, int]] = []
        seen_w: Set[Tuple[int, int]] = set()
        for pi in range(n_pages):
            words = doc[pi].get_text("words") or []
            for wi, w in enumerate(words, start=1):
                if len(w) < 5:
                    continue
                x0, y0, x1, y1 = float(w[0]), float(w[1]), float(w[2]), float(w[3])
                raw = w[4]
                wt = raw if isinstance(raw, str) else str(raw)
                t = wt.strip()
                if not t or excluded(t):
                    continue
                if not any(norm_paren_for_table_header_match(ht) in norm_paren_for_table_header_match(t) for ht, _ in pairs):
                    continue
                if (pi, wi) in seen_w:
                    continue
                seen_w.add((pi, wi))
                bn = _fitz_word_int_meta(w, 5)
                ln = _fitz_word_int_meta(w, 6)
                wn = _fitz_word_int_meta(w, 7)
                rank = _extract_table_item_matched_min_column(t, pairs)
                picked_w.append((rank, pi, wi, x0, y0, x1, y1, t, bn, ln, wn))
        if sort_mode == "column_rank_then_geom":
            picked_w.sort(key=lambda r: (r[0], r[1], r[5], r[4]))
        lines_w: List[str] = []
        for rank, pi, wi, x0, y0, x1, y1, tx, bn, ln, wn in picked_w:
            tcell = format_pymupdf_block_text_like_cluster_script(tx)
            if words_line_format == "bbox_column_text":
                if show_column_index:
                    lines_w.append(
                        f"x0={x0:.1f}\tx1={x1:.1f}\t{tcell}"
                    )
                else:
                    lines_w.append(
                        f"x0={x0:.1f}\tx1={x1:.1f}\t{tcell}"
                    )
            elif show_column_index:
                lines_w.append(
                    f"page_index={pi}\tcolumn_index={rank}\tword_index={wi}\tx0_pt={x0:.1f}\ty0_pt={y0:.1f}\t"
                    f"x1_pt={x1:.1f}\ty1_pt={y1:.1f}\tblock_no={pymupdf_prompt_meta_str(bn)}\t"
                    f"line_no={pymupdf_prompt_meta_str(ln)}\tword_no={pymupdf_prompt_meta_str(wn)}\t"
                    f"{tcell}"
                )
            else:
                lines_w.append(
                    f"page_index={pi}\tword_index={wi}\tx0_pt={x0:.1f}\ty0_pt={y0:.1f}\t"
                    f"x1_pt={x1:.1f}\ty1_pt={y1:.1f}\tblock_no={pymupdf_prompt_meta_str(bn)}\t"
                    f"line_no={pymupdf_prompt_meta_str(ln)}\tword_no={pymupdf_prompt_meta_str(wn)}\t"
                    f"{tcell}"
                )
        return "\n".join(lines_w)
    finally:
        doc.close()


def build_extract_table_words_header_section_preamble(
    header_texts_in_column_order: Sequence[str],
    column_indices_to_extract: Sequence[int],
    *,
    words_line_format: Literal["pymupdf_meta", "bbox_column_text"] = "pymupdf_meta",
) -> str:
    """
    由结构化列定义生成接在 ``extract_table``（``source="words"``）输出行块前的 LLM 说明段。

    - ``header_texts_in_column_order``：按表格列顺序排列的表头匹配子串；第 *i* 项与 ``column_indices_to_extract[i]`` 配对。
    - ``column_indices_to_extract``：与上表等长的 **1-based** 列号，与 ``extract_table(..., column_indices=...)`` 及行内 ``column_index`` 语义一致。
    - ``words_line_format``：须与 ``extract_table(..., words_line_format=...)`` 一致，以便说明与实同行字段对齐。

    二者等长且一一对应；空子串列表时返回空串。
    """
    texts = tuple((s or "").strip() for s in header_texts_in_column_order)
    cols = tuple(column_indices_to_extract)
    if not texts:
        return ""
    if len(texts) != len(cols):
        raise ValueError(
            "header_texts_in_column_order 与 column_indices_to_extract 长度须一致"
        )
    joined = "」「".join(texts)
    pair_desc = "；".join(f"「{h}」→第{c}列" for h, c in zip(texts, cols))
    if words_line_format == "bbox_column_text":
        return (
            f'下列词条为从 PDF 前几页检出的、文本含「{joined}」任一字样的表头相关文字（'
            "每行仅含横坐标 ``x0``/``x1``（单位 pt）与单行化文本，x0/x1 分别为文字框左/右边界；"
            f"列对应关系：{pair_desc}；"
            "文本列已单行化（换行与制表符已压为空白，过长截断）。"
            "表头相关文字与横坐标（单位 pt）如下："
        )
    return (
        f'下列词条为从 PDF 前几页 ``get_text("words")`` 中检出的、文本含「{joined}」任一字样的行（'
        "每行含词序号、bbox、block_no、line_no、word_no（与 PyMuPDF 词条结构一致）；"
        f"并含 ``column_index``（1-based），与下列对应：{pair_desc}；"
        "文本列已单行化（换行与制表符已压为空白，过长截断）。"
        "表头相关文字与坐标（单位 pt）如下："
    )


def compose_table_llm_user_prompt(
    *,
    instruction: str,
    context_block: str,
    extracted_table_lines: str = "",
    table_header_section_preamble: str = "",
    context_section_title: str = "以下为上下文块：",
) -> str:
    """
    将「业务 instruction + 可选 ``extract_table`` 表头行块 + 锚点区域上下文」拼成一条 user 消息正文。

    - 仅当 ``extracted_table_lines`` 非空时，才接上表头块；若同时有 ``table_header_section_preamble``，
      则先前言、换行、再接行块（与车险渤海 pass2 结构一致）。
    - 最后固定接上 ``context_section_title`` 与 ``context_block``。
    """
    parts: List[str] = [instruction.strip()]
    ext = (extracted_table_lines or "").strip()
    pre = (table_header_section_preamble or "").strip()
    if ext:
        parts.append(pre + "\n" + ext if pre else ext)
    title = (context_section_title or "以下为上下文块：").strip()
    parts.append(title + "\n\n" + (context_block or "").strip())
    return "\n\n".join(parts)


def iter_pymupdf_word_line_rect_items(
    pdf_bytes: bytes,
    *,
    line_tol_pt: float = 2.5,
) -> List[Tuple[int, float, float, float, float, str]]:
    """
    PyMuPDF：按 ``get_text("words")`` 聚成**视觉行**，每行一项，形与 ``iter_pymupdf_block_rect_items`` 相同。

    同一页内按词框竖直中点分桶（``line_tol_pt``），桶内按 ``x0`` 升序拼接文本（无空格，便于中文）；
    bbox 为该行各词框外包矩形。与 ``iter_pymupdf_block_rect_items`` 一致：跳过空行，``text`` 为整行拼接后 ``strip()``。
    """
    import fitz

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        out: List[Tuple[int, float, float, float, float, str]] = []
        for pi in range(doc.page_count):
            page = doc[pi]
            words = page.get_text("words") or []
            buckets: DefaultDict[float, List[Tuple[float, float, float, float, str]]] = defaultdict(
                list
            )
            for w in words:
                if len(w) < 5:
                    continue
                x0, y0, x1, y1 = float(w[0]), float(w[1]), float(w[2]), float(w[3])
                raw = w[4]
                wt = raw if isinstance(raw, str) else str(raw)
                t = wt.strip()
                if not t:
                    continue
                ymid = (y0 + y1) * 0.5
                y_key = round(ymid / line_tol_pt) * line_tol_pt
                buckets[y_key].append((x0, y0, x1, y1, t))
            for y_key in sorted(buckets.keys()):
                parts = sorted(buckets[y_key], key=lambda p: p[0])
                minx = min(p[0] for p in parts)
                miny = min(p[1] for p in parts)
                maxx = max(p[2] for p in parts)
                maxy = max(p[3] for p in parts)
                joined = "".join(p[4] for p in parts).strip()
                if joined:
                    out.append((pi, minx, miny, maxx, maxy, joined))
        return out
    finally:
        doc.close()


def extract_pymupdf_plain_text(pdf_bytes: bytes) -> str:
    """整份 PDF：各文本块用换行拼接（便于与 pypdf 整页文本对照调试）。"""
    parts = iter_pymupdf_text_blocks(pdf_bytes)
    return "\n".join(parts)
