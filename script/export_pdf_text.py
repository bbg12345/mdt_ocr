#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
导出PDF全文用于对比观察 - 同时使用pypdf和PyMuPDF两种解析器
"""

import sys
from pathlib import Path
from urllib.request import Request, urlopen

def load_pdf_bytes(path_or_url: str) -> bytes:
    if path_or_url.startswith(("http://", "https://")):
        req = Request(path_or_url, headers={"User-Agent": "mdt_ocr_service"})
        with urlopen(req, timeout=120) as resp:
            return resp.read()
    p = Path(path_or_url).expanduser().resolve()
    if not p.is_file():
        raise FileNotFoundError(str(p))
    return p.read_bytes()

def export_pypdf_text(pdf_bytes: bytes) -> str:
    """使用pypdf导出全文"""
    try:
        import pypdf
    except ImportError:
        return "ERROR: pypdf未安装，请运行: pip install pypdf"

    import io
    reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
    result = []
    for i, page in enumerate(reader.pages):
        text = page.extract_text() or ""
        result.append(f"=== pypdf 第 {i+1} 页 ===")
        result.append(text)
        result.append("")
    return "\n".join(result)

def export_pymupdf_text(pdf_bytes: bytes) -> str:
    """使用PyMuPDF导出全文"""
    try:
        import fitz
    except ImportError:
        return "ERROR: PyMuPDF未安装，请运行: pip install PyMuPDF"

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    result = []
    try:
        for i in range(doc.page_count):
            page = doc[i]
            text = page.get_text("text") or ""
            result.append(f"=== PyMuPDF 第 {i+1} 页 ===")
            result.append(text)
            result.append("")
    finally:
        doc.close()
    return "\n".join(result)

def export_pymupdf_blocks(pdf_bytes: bytes) -> str:
    """使用PyMuPDF导出文本块（与car_insurance.py相同的逻辑）"""
    try:
        import fitz
    except ImportError:
        return "ERROR: PyMuPDF未安装，请运行: pip install PyMuPDF"

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    result = []
    try:
        for pi in range(doc.page_count):
            page = doc[pi]
            raw = page.get_text("blocks")
            blocks = raw if raw else []
            result.append(f"=== PyMuPDF 文本块 第 {pi+1} 页 ===")

            block_count = 0
            for b in blocks:
                if len(b) < 5:
                    continue
                txt = b[4] if isinstance(b[4], str) else str(b[4])
                btype = None
                if len(b) > 6:
                    try:
                        btype = int(b[6])
                    except (TypeError, ValueError):
                        btype = None
                if btype == 1:  # 图像块
                    continue
                t = txt.strip()
                if t:
                    block_count += 1
                    result.append(f"--- 块 #{block_count} ---")
                    result.append(t)
                    result.append("")

            if block_count == 0:
                result.append("(无文本块)")
            result.append("")
    finally:
        doc.close()
    return "\n".join(result)

def main():
    if len(sys.argv) < 2:
        print("用法: python export_pdf_text.py <PDF文件或URL> [输出文件前缀]")
        print("示例: python export_pdf_text.py https://example.com/policy.pdf")
        print("       python export_pdf_text.py local.pdf output")
        return 1

    pdf_path = sys.argv[1]
    prefix = sys.argv[2] if len(sys.argv) > 2 else "export"

    print(f"正在加载PDF: {pdf_path}")
    try:
        pdf_bytes = load_pdf_bytes(pdf_path)
        print(f"PDF大小: {len(pdf_bytes)} 字节")
    except Exception as e:
        print(f"加载PDF失败: {e}")
        return 1

    # 导出三种格式
    print("\n正在使用pypdf导出全文...")
    pypdf_text = export_pypdf_text(pdf_bytes)
    pypdf_file = f"{prefix}_pypdf.txt"
    with open(pypdf_file, "w", encoding="utf-8") as f:
        f.write(pypdf_text)
    print(f"已保存到: {pypdf_file}")

    print("\n正在使用PyMuPDF导出全文...")
    pymupdf_text = export_pymupdf_text(pdf_bytes)
    pymupdf_file = f"{prefix}_pymupdf.txt"
    with open(pymupdf_file, "w", encoding="utf-8") as f:
        f.write(pymupdf_text)
    print(f"已保存到: {pymupdf_file}")

    print("\n正在使用PyMuPDF导出文本块（car_insurance.py逻辑）...")
    pymupdf_blocks = export_pymupdf_blocks(pdf_bytes)
    blocks_file = f"{prefix}_blocks.txt"
    with open(blocks_file, "w", encoding="utf-8") as f:
        f.write(pymupdf_blocks)
    print(f"已保存到: {blocks_file}")

    print(f"\n导出完成！")
    print(f"- pypdf全文: {pypdf_file}")
    print(f"- PyMuPDF全文: {pymupdf_file}")
    print(f"- PyMuPDF文本块: {blocks_file}")

    # 简单对比
    print("\n=== 简单对比 ===")
    pypdf_lines = pypdf_text.split('\n')
    pymupdf_lines = pymupdf_text.split('\n')
    print(f"pypdf行数: {len(pypdf_lines)}")
    print(f"PyMuPDF行数: {len(pymupdf_lines)}")

    return 0

if __name__ == "__main__":
    sys.exit(main())
