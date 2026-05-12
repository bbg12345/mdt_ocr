#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PDF：当前默认可输出下列内容（坐标单位：PDF pt，一位小数用于打印 bbox，另有原始 words 节）：

1. **pypdf 纯文本**：整份文档按页拼接的 ``extract_text()``。
2. **pypdf 分词块 bbox**：在 **各页** ``extract_text()`` 上用空白切分 block，再用 PyMuPDF ``get_text("words")`` 对齐合并 bbox。
3. **PyMuPDF 原生文字块**：每页 ``get_text("blocks")`` 的轴对齐框与块内文本（及块类型，若有）。
4. **PyMuPDF 原始 words**：每页 ``get_text("words")`` 逐条输出 bbox 与文本（**不截断**；文本列用 ``repr`` 保留换行、引号等字面形态）。

脚本内仍保留射线法单元格、线段打印与线段示意图等逻辑，默认 **不** 向 stdout 输出（见模块级开关）。

用法::

    python script/cluster_policy_blocks_by_grid_lines.py [pdf路径或URL]
    python script/cluster_policy_blocks_by_grid_lines.py --probe-delta 1.0 某.pdf
"""

from __future__ import annotations

import argparse
import io
import math
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any, List, NamedTuple, Optional, Sequence, Set, Tuple

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from common import (  # noqa: E402
    align_pypdf_blocks_to_fitz_words,
    extract_pypdf_page_texts,
    extract_pypdf_plain_text,
    format_pymupdf_block_text_like_cluster_script,
    load_pdf_bytes as _load_pdf_bytes,
    norm_txt as _norm_txt,
    split_pypdf_text_into_blocks,
)


class HSeg(NamedTuple):
    y: float
    x0: float
    x1: float


class VSeg(NamedTuple):
    x: float
    y0: float
    y1: float


DEFAULT_COMPULSORY_FIRST = (
    "https://mingdetong-image-meterias.oss-cn-beijing.aliyuncs.com/"
    "insurance/f/f/bohai-f.PDF"
)

# --- stdout 开关（线段相关代码保留，默认关闭） ---
OUTPUT_MINIMAL_CELLS = False
OUTPUT_LINE_SEGMENTS = False
OUTPUT_LINE_OVERLAY_PNG = False
OUTPUT_PYPDF_BLOCK_BOXES = True
OUTPUT_PYMUPDF_TEXT_BLOCKS = True
OUTPUT_PYMUPDF_RAW_WORDS = True


def _fmt1(v: float) -> str:
    """输出：一位小数。"""
    return f"{float(v):.1f}"


def print_pypdf_block_boxes(doc: Any, page_texts: Sequence[str], fitz_module: Any) -> None:
    """向 stdout 打印各页 block 的 bbox。"""
    n = doc.page_count
    print("\n=== 2 pypdf 分词块 bbox（空白分隔；PyMuPDF words 对齐；坐标 pt，一位小数）===")
    if n > 1:
        print("# 页码\t序号\tx0\ty0\tx1\ty1\t文本")
    else:
        print("# 序号\tx0\ty0\tx1\ty1\t文本")
    for pi in range(n):
        page = doc[pi]
        ptxt = page_texts[pi] if pi < len(page_texts) else ""
        blocks = split_pypdf_text_into_blocks(ptxt)
        words = page.get_text("words") or []
        aligned = align_pypdf_blocks_to_fitz_words(blocks, words, fitz_module)
        for bi, (text, rect) in enumerate(aligned, start=1):
            if rect is None:
                box = "\t".join(["—", "—", "—", "—"])
            else:
                box = "\t".join(
                    (
                        _fmt1(rect.x0),
                        _fmt1(rect.y0),
                        _fmt1(rect.x1),
                        _fmt1(rect.y1),
                    ),
                )
            safe = format_pymupdf_block_text_like_cluster_script(text)
            if n > 1:
                print(f"{pi + 1}\t{bi}\t{box}\t{safe}")
            else:
                print(f"{bi}\t{box}\t{safe}")


def print_pymupdf_native_text_blocks(doc: Any) -> None:
    """
    打印 PyMuPDF ``page.get_text(\"blocks\")``：每行为一块的 bbox 与文本。

    元组一般为 ``(x0, y0, x1, y1, text[, block_no, block_type])``；
    ``block_type``：0 文本，1 图像（无则标为 ``?``）。
    """
    n = doc.page_count
    print(
        '\n=== 3 PyMuPDF 文字块（get_text("blocks")；坐标 pt，一位小数）===',
    )
    if n > 1:
        print("# 页码\t块序号\tx0\ty0\tx1\ty1\t类型\t文本")
    else:
        print("# 块序号\tx0\ty0\tx1\ty1\t类型\t文本")
    for pi in range(n):
        page = doc[pi]
        raw = page.get_text("blocks")
        blocks: Sequence[Tuple[Any, ...]] = raw if raw else []
        for bi, b in enumerate(blocks, start=1):
            if len(b) < 5:
                continue
            x0, y0, x1, y1 = float(b[0]), float(b[1]), float(b[2]), float(b[3])
            txt = b[4] if isinstance(b[4], str) else str(b[4])
            btype: Optional[int] = None
            if len(b) > 6:
                try:
                    btype = int(b[6])
                except (TypeError, ValueError):
                    btype = None
            if btype == 0:
                tlab = "文本"
            elif btype == 1:
                tlab = "图像"
            elif btype is None:
                tlab = "?"
            else:
                tlab = str(btype)
            box = "\t".join(
                (_fmt1(x0), _fmt1(y0), _fmt1(x1), _fmt1(y1)),
            )
            safe = format_pymupdf_block_text_like_cluster_script(txt)
            if n > 1:
                print(f"{pi + 1}\t{bi}\t{box}\t{tlab}\t{safe}")
            else:
                print(f"{bi}\t{box}\t{tlab}\t{safe}")


def print_pymupdf_raw_words(doc: Any) -> None:
    """
    逐条输出 ``page.get_text("words")``：bbox（pt，一位小数）+ 元组其余字段 + 文本 ``repr``（不截断原文）。
    词条元组常见为 ``(x0, y0, x1, y1, word[, block_no, line_no, word_no])``。
    """
    n = doc.page_count
    print(
        '\n=== 4 PyMuPDF 原始 words（get_text("words")；坐标 pt，一位小数；文本列为 repr）===',
    )
    if n > 1:
        print(
            "# 页码\t词序号\tx0\ty0\tx1\ty1\tblock_no\tline_no\tword_no\t文本repr",
        )
    else:
        print("# 词序号\tx0\ty0\tx1\ty1\tblock_no\tline_no\tword_no\t文本repr")
    for pi in range(n):
        page = doc[pi]
        words = page.get_text("words") or []
        for wi, w in enumerate(words, start=1):
            if len(w) < 5:
                continue
            x0, y0, x1, y1 = float(w[0]), float(w[1]), float(w[2]), float(w[3])
            raw = w[4]
            wt = raw if isinstance(raw, str) else str(raw)
            try:
                bn = str(int(w[5])) if len(w) > 5 else "—"
            except (TypeError, ValueError):
                bn = "—"
            try:
                ln = str(int(w[6])) if len(w) > 6 else "—"
            except (TypeError, ValueError):
                ln = "—"
            try:
                wn = str(int(w[7])) if len(w) > 7 else "—"
            except (TypeError, ValueError):
                wn = "—"
            box = "\t".join((_fmt1(x0), _fmt1(y0), _fmt1(x1), _fmt1(y1)))
            rep = repr(wt)
            if n > 1:
                print(f"{pi + 1}\t{wi}\t{box}\t{bn}\t{ln}\t{wn}\t{rep}")
            else:
                print(f"{wi}\t{box}\t{bn}\t{ln}\t{wn}\t{rep}")


def _extract_line_segments(
    page: Any,
    *,
    min_vert_height: float = 15.0,
    max_vert_width: float = 2.0,
    min_horiz_width_axis: float = 80.0,
    min_horiz_width_any: float = 12.0,
    max_horiz_height: float = 2.0,
) -> Tuple[
    List[Tuple[float, float, float, float]],
    List[Tuple[float, float, float, float]],
    List[Tuple[float, float, float, float]],
]:
    vert: List[Tuple[float, float, float, float]] = []
    horiz_axis: List[Tuple[float, float, float, float]] = []
    horiz_any: List[Tuple[float, float, float, float]] = []
    for d in page.get_drawings():
        r = d.get("rect")
        if r is None:
            continue
        w, h = r.width, r.height
        x0, y0, x1, y1 = float(r.x0), float(r.y0), float(r.x1), float(r.y1)
        if w < max_vert_width and h > min_vert_height:
            vert.append((x0, x1, y0, y1))
        if h < max_horiz_height and w > min_horiz_width_any:
            seg = (x0, x1, y0, y1)
            horiz_any.append(seg)
            if w > min_horiz_width_axis:
                horiz_axis.append(seg)
    return vert, horiz_axis, horiz_any


def _dedupe_h_segments(hs: List[HSeg]) -> List[HSeg]:
    """去掉完全重复的横线段（``get_drawings()`` 常对同一矩形返回多条记录）。"""
    seen: Set[Tuple[float, float, float]] = set()
    out: List[HSeg] = []
    for h in hs:
        x0, x1 = sorted((h.x0, h.x1))
        key = (round(h.y, 3), round(x0, 3), round(x1, 3))
        if key in seen:
            continue
        seen.add(key)
        out.append(HSeg(h.y, x0, x1))
    return out


def _dedupe_v_segments(vs: List[VSeg]) -> List[VSeg]:
    """去掉完全重复的竖线段。"""
    seen: Set[Tuple[float, float, float]] = set()
    out: List[VSeg] = []
    for v in vs:
        y0, y1 = sorted((v.y0, v.y1))
        key = (round(v.x, 3), round(y0, 3), round(y1, 3))
        if key in seen:
            continue
        seen.add(key)
        out.append(VSeg(v.x, y0, y1))
    return out


def _normalize_hv_segments(
    vert: Sequence[Tuple[float, float, float, float]],
    horiz_any: Sequence[Tuple[float, float, float, float]],
) -> Tuple[List[HSeg], List[VSeg]]:
    hs: List[HSeg] = []
    vs: List[VSeg] = []
    for x0, x1, y0, y1 in vert:
        xa, xb = sorted((x0, x1))
        ya, yb = sorted((y0, y1))
        vs.append(VSeg((xa + xb) * 0.5, ya, yb))
    for x0, x1, y0, y1 in horiz_any:
        xa, xb = sorted((x0, x1))
        ya, yb = sorted((y0, y1))
        hs.append(HSeg((ya + yb) * 0.5, xa, xb))
    hs = _dedupe_h_segments(hs)
    vs = _dedupe_v_segments(vs)
    hs.sort(key=lambda h: (h.y, h.x0))
    vs.sort(key=lambda v: (v.x, v.y0))
    return hs, vs


def _probe_offsets(delta: float) -> Tuple[Tuple[float, float], ...]:
    d = delta
    return ((-d, 0.0), (d, 0.0), (0.0, -d), (0.0, d))


def _collect_raycast_probe_points(
    hs: Sequence[HSeg],
    vs: Sequence[VSeg],
    delta: float,
) -> List[Tuple[float, float]]:
    offs = _probe_offsets(delta)
    seen: Set[Tuple[int, int]] = set()
    out: List[Tuple[float, float]] = []

    def add(px: float, py: float) -> None:
        key = (round(px, 3), round(py, 3))
        if key in seen:
            return
        seen.add(key)
        out.append((px, py))

    for h in hs:
        x_lo, x_hi = sorted((h.x0, h.x1))
        y = h.y
        for bx, by in ((x_lo, y), (x_hi, y), ((x_lo + x_hi) * 0.5, y)):
            for ox, oy in offs:
                add(bx + ox, by + oy)
    for v in vs:
        y_lo, y_hi = sorted((v.y0, v.y1))
        x = v.x
        for bx, by in ((x, y_lo), (x, y_hi), (x, (y_lo + y_hi) * 0.5)):
            for ox, oy in offs:
                add(bx + ox, by + oy)
    return out


def _hit_left(px: float, py: float, vs: Sequence[VSeg], eps: float = 1e-2) -> Optional[float]:
    best_x: Optional[float] = None
    for v in vs:
        if v.y0 - eps <= py <= v.y1 + eps and v.x < px - eps:
            if best_x is None or v.x > best_x:
                best_x = v.x
    return best_x


def _hit_right(px: float, py: float, vs: Sequence[VSeg], eps: float = 1e-2) -> Optional[float]:
    best_x: Optional[float] = None
    for v in vs:
        if v.y0 - eps <= py <= v.y1 + eps and v.x > px + eps:
            if best_x is None or v.x < best_x:
                best_x = v.x
    return best_x


def _hit_up(px: float, py: float, hs: Sequence[HSeg], eps: float = 1e-2) -> Optional[float]:
    best_y: Optional[float] = None
    for h in hs:
        x_lo, x_hi = sorted((h.x0, h.x1))
        if x_lo - eps <= px <= x_hi + eps and h.y < py - eps:
            if best_y is None or h.y > best_y:
                best_y = h.y
    return best_y


def _hit_down(px: float, py: float, hs: Sequence[HSeg], eps: float = 1e-2) -> Optional[float]:
    best_y: Optional[float] = None
    for h in hs:
        x_lo, x_hi = sorted((h.x0, h.x1))
        if x_lo - eps <= px <= x_hi + eps and h.y > py + eps:
            if best_y is None or h.y < best_y:
                best_y = h.y
    return best_y


def _four_ray_cell_box(
    px: float,
    py: float,
    hs: Sequence[HSeg],
    vs: Sequence[VSeg],
) -> Optional[Tuple[float, float, float, float]]:
    xl = _hit_left(px, py, vs)
    xr = _hit_right(px, py, vs)
    yt = _hit_up(px, py, hs)
    yb = _hit_down(px, py, hs)
    if xl is None or xr is None or yt is None or yb is None:
        return None
    if not (xl < xr and yt < yb):
        return None
    if not (xl < px < xr and yt < py < yb):
        return None
    return (xl, yt, xr, yb)


def _point_in_open_rect(
    px: float,
    py: float,
    x0: float,
    y0: float,
    x1: float,
    y1: float,
    margin: float,
) -> bool:
    return (
        x0 + margin < px < x1 - margin
        and y0 + margin < py < y1 - margin
    )


def raycast_minimal_cell_boxes(
    hs: Sequence[HSeg],
    vs: Sequence[VSeg],
    *,
    probe_delta: float = 1.0,
    min_w: float = 2.0,
    min_h: float = 2.0,
    interior_margin: float = 0.35,
) -> List[Any]:
    import fitz

    probes = _collect_raycast_probe_points(hs, vs, probe_delta)
    accepted_rects: List[Any] = []
    seen_keys: Set[Tuple[float, float, float, float]] = set()

    for px, py in probes:
        skip = False
        for r in accepted_rects:
            if _point_in_open_rect(px, py, r.x0, r.y0, r.x1, r.y1, interior_margin):
                skip = True
                break
        if skip:
            continue
        box = _four_ray_cell_box(px, py, hs, vs)
        if box is None:
            continue
        x0, y0, x1, y1 = box
        if x1 - x0 < min_w or y1 - y0 < min_h:
            continue
        key = (round(x0, 2), round(y0, 2), round(x1, 2), round(y1, 2))
        if key in seen_keys:
            continue
        seen_keys.add(key)
        accepted_rects.append(fitz.Rect(x0, y0, x1, y1))

    accepted_rects.sort(key=lambda r: (r.y0, r.x0))
    return accepted_rects


def _segments_sorted(
    hs: Sequence[HSeg],
    vs: Sequence[VSeg],
) -> Tuple[List[HSeg], List[VSeg]]:
    """与 ``print_segments_lines`` / 示意图相同的横线 y、竖线 x 排序。"""
    return (
        sorted(hs, key=lambda h: (h.y, h.x0)),
        sorted(vs, key=lambda v: (v.x, v.y0)),
    )


def _overlay_label_font(size: int) -> Any:
    from PIL import ImageFont

    candidates = (
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        r"C:\Windows\Fonts\arial.ttf",
    )
    for path in candidates:
        try:
            if path.endswith(".ttc"):
                return ImageFont.truetype(path, size, index=0)
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def write_segments_overlay_png(
    doc: Any,
    out_path: Path,
    *,
    scale: float = 2.0,
) -> None:
    """
    白底 PNG：各页 ``page.rect`` 宽度取最大、高度相加纵向拼接（与 PDF 总版面一致）；
    线段顺序与控制台第 3 节一致，黑色绘制并在旁标注连续编号。
    """
    from PIL import Image, ImageDraw

    page_dims: List[Tuple[float, float]] = []
    for pi in range(doc.page_count):
        r = doc[pi].rect
        page_dims.append((float(r.width), float(r.height)))

    max_w_pt = max(w for w, _ in page_dims) if page_dims else 1.0
    total_h_pt = sum(h for _, h in page_dims) if page_dims else 1.0
    y_base_pt: List[float] = []
    acc = 0.0
    for _w, h in page_dims:
        y_base_pt.append(acc)
        acc += h

    img_w = max(1, int(math.ceil(max_w_pt * scale)))
    img_h = max(1, int(math.ceil(total_h_pt * scale)))
    img = Image.new("RGB", (img_w, img_h), (255, 255, 255))
    draw = ImageDraw.Draw(img)

    lw = max(1, int(round(scale)))
    fsize = max(10, int(round(11 * scale)))
    font = _overlay_label_font(fsize)

    def _label_size(text: str) -> Tuple[float, float]:
        bbox = draw.textbbox((0, 0), text, font=font)
        return (bbox[2] - bbox[0], bbox[3] - bbox[1])

    seg_idx = 1
    for pi in range(doc.page_count):
        page = doc[pi]
        vert, _ha, horiz_any = _extract_line_segments(page)
        hs, vs = _normalize_hv_segments(vert, horiz_any)
        hs_sorted, vs_sorted = _segments_sorted(hs, vs)
        y0 = y_base_pt[pi]

        for h in hs_sorted:
            x_lo, x_hi = sorted((h.x0, h.x1))
            y = h.y
            xa, ya = x_lo * scale, (y0 + y) * scale
            xb, yb = x_hi * scale, (y0 + y) * scale
            draw.line([(xa, ya), (xb, yb)], fill=(0, 0, 0), width=lw)
            mx = (x_lo + x_hi) * 0.5 * scale
            my = (y0 + y) * scale
            label = str(seg_idx)
            tw, th = _label_size(label)
            draw.text(
                (mx - tw * 0.5, my - th - 4.0 * scale),
                label,
                fill=(0, 0, 0),
                font=font,
            )
            seg_idx += 1

        for v in vs_sorted:
            y_lo, y_hi = sorted((v.y0, v.y1))
            x = v.x
            xa = x * scale
            ya = (y0 + y_lo) * scale
            xb = x * scale
            yb = (y0 + y_hi) * scale
            draw.line([(xa, ya), (xb, yb)], fill=(0, 0, 0), width=lw)
            mx = x * scale
            my = (y0 + (y_lo + y_hi) * 0.5) * scale
            label = str(seg_idx)
            tw, th = _label_size(label)
            draw.text(
                (mx + 4.0 * scale, my - th * 0.5),
                label,
                fill=(0, 0, 0),
                font=font,
            )
            seg_idx += 1

    out_path = out_path.expanduser()
    if not out_path.is_absolute():
        out_path = Path.cwd() / out_path
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(str(out_path), format="PNG")


def print_segments_lines(
    hs: Sequence[HSeg],
    vs: Sequence[VSeg],
    page_index: int,
    index_start: int = 1,
) -> int:
    """
    横线：起点 (min(x),y) → 终点 (max(x),y)；竖线：起点 (x,min(y)) → 终点 (x,max(y))。
    列：索引、页码、类型、x0、y0、x1、y1（坐标均为一位小数）。

    输出顺序：横线段按 **y 升序**（同 y 再按 x0）；纵线段按 **x 升序**（同 x 再按 y0）。
    索引从 ``index_start`` 起连续递增；返回下一条线段应使用的索引。
    """
    pi = page_index + 1
    hs_sorted, vs_sorted = _segments_sorted(hs, vs)
    idx = index_start
    for h in hs_sorted:
        x_lo, x_hi = sorted((h.x0, h.x1))
        y = h.y
        print(
            f"{idx}\t{pi}\tH\t{_fmt1(x_lo)}\t{_fmt1(y)}\t{_fmt1(x_hi)}\t{_fmt1(y)}",
        )
        idx += 1
    for v in vs_sorted:
        y_lo, y_hi = sorted((v.y0, v.y1))
        x = v.x
        print(
            f"{idx}\t{pi}\tV\t{_fmt1(x)}\t{_fmt1(y_lo)}\t{_fmt1(x)}\t{_fmt1(y_hi)}",
        )
        idx += 1
    return idx


def main() -> int:
    parser = argparse.ArgumentParser(
        description="默认：①pypdf 纯文本 ②pypdf 分词块 bbox（脚本内可打开单元格/线段输出）",
    )
    parser.add_argument(
        "pdf",
        nargs="?",
        default=DEFAULT_COMPULSORY_FIRST,
        help="PDF 路径或 URL",
    )
    parser.add_argument(
        "--probe-delta",
        type=float,
        default=1.0,
        metavar="PT",
        help="射线法探针错开距离（pt），默认 1.0",
    )
    parser.add_argument(
        "--segment-image",
        type=Path,
        default=Path("policy_segments_overlay.png"),
        metavar="PATH",
        help="线段示意图 PNG 路径（相对路径基于当前工作目录），默认 policy_segments_overlay.png",
    )
    parser.add_argument(
        "--segment-image-scale",
        type=float,
        default=2.0,
        metavar="S",
        help="示意图像素相对 PDF 点（pt）的缩放，默认 2",
    )
    parser.add_argument(
        "--no-segment-image",
        action="store_true",
        help="不生成线段示意图",
    )
    args = parser.parse_args()
    if args.segment_image_scale <= 0:
        print("错误：--segment-image-scale 须为正数", file=sys.stderr)
        return 1

    try:
        import fitz
    except ImportError:
        print("错误：需要 PyMuPDF。pip install -r requirements.txt", file=sys.stderr)
        return 1
    try:
        data = _load_pdf_bytes(args.pdf)
    except FileNotFoundError as e:
        print(f"错误：{e}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"错误：无法读取 PDF（{e}）", file=sys.stderr)
        return 1

    # --- 1. pypdf 纯文本 ---
    print("=== 1 纯文本（pypdf extract_text）===")
    try:
        print(extract_pypdf_plain_text(data))
    except ImportError:
        print("（未安装 pypdf，跳过本节。pip install pypdf）", file=sys.stderr)
    except Exception as e:
        print(f"（pypdf 提取失败: {e}）", file=sys.stderr)

    page_texts_for_blocks: List[str] = []
    try:
        page_texts_for_blocks = extract_pypdf_page_texts(data)
    except ImportError:
        page_texts_for_blocks = []
    except Exception:
        page_texts_for_blocks = []

    doc = fitz.open(stream=data, filetype="pdf")
    try:
        if OUTPUT_PYPDF_BLOCK_BOXES:
            if page_texts_for_blocks:
                print_pypdf_block_boxes(doc, page_texts_for_blocks, fitz)
            else:
                print(
                    "\n=== 2 pypdf 分词块 bbox ===\n"
                    "（无法获取分页 pypdf 文本，已跳过。请安装 pypdf。）",
                    file=sys.stderr,
                )

        if OUTPUT_PYMUPDF_TEXT_BLOCKS:
            print_pymupdf_native_text_blocks(doc)

        if OUTPUT_PYMUPDF_RAW_WORDS:
            print_pymupdf_raw_words(doc)

        if OUTPUT_MINIMAL_CELLS:
            all_rects: List[Any] = []
            for pi in range(doc.page_count):
                page = doc[pi]
                vert, _ha, horiz_any = _extract_line_segments(page)
                hs, vs = _normalize_hv_segments(vert, horiz_any)
                rects = raycast_minimal_cell_boxes(hs, vs, probe_delta=args.probe_delta)
                for r in rects:
                    all_rects.append((pi, r))

            print("\n=== 最小单元格（射线法；坐标 pt，一位小数）===")
            if len(doc) > 1:
                print("# 页码\tx0\ty0\tx1\ty1\tw\th")
                for pi, r in sorted(all_rects, key=lambda t: (t[1].y0, t[1].x0, t[0])):
                    print(
                        f"{pi + 1}\t{_fmt1(r.x0)}\t{_fmt1(r.y0)}\t{_fmt1(r.x1)}\t{_fmt1(r.y1)}\t"
                        f"{_fmt1(r.width)}\t{_fmt1(r.height)}",
                    )
            else:
                print("# x0\ty0\tx1\ty1\tw\th")
                for _pi, r in all_rects:
                    print(
                        f"{_fmt1(r.x0)}\t{_fmt1(r.y0)}\t{_fmt1(r.x1)}\t{_fmt1(r.y1)}\t"
                        f"{_fmt1(r.width)}\t{_fmt1(r.height)}",
                    )

        if OUTPUT_LINE_SEGMENTS:
            print(
                "\n=== 线段（起点→终点坐标，一位小数；列：索引、页码、H|V、x0、y0、x1、y1）===",
            )
            seg_idx = 1
            for pi in range(doc.page_count):
                page = doc[pi]
                vert, _ha, horiz_any = _extract_line_segments(page)
                hs, vs = _normalize_hv_segments(vert, horiz_any)
                seg_idx = print_segments_lines(hs, vs, pi, seg_idx)

        if OUTPUT_LINE_OVERLAY_PNG and not args.no_segment_image:
            try:
                write_segments_overlay_png(
                    doc,
                    args.segment_image,
                    scale=float(args.segment_image_scale),
                )
                out_abs = args.segment_image.expanduser()
                if not out_abs.is_absolute():
                    out_abs = Path.cwd() / out_abs
                print(f"（线段示意图已保存：{out_abs}）", file=sys.stderr)
            except ImportError:
                print(
                    "（未安装 Pillow，跳过线段示意图。pip install Pillow）",
                    file=sys.stderr,
                )
            except OSError as e:
                print(f"（写入线段示意图失败：{e}）", file=sys.stderr)
            except Exception as e:
                print(f"（生成线段示意图失败：{e}）", file=sys.stderr)
    finally:
        doc.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
