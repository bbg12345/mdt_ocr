#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
车险保单字段抽取业务逻辑（pypdf / PyMuPDF 双路块序列、保险公司名称 pass、保险费合计 pass、
商业险险种明细表双路 pass（平安 / 人保 / 人寿 / 泰康 / 太平洋 / 阳光等）及
渤海 / 中银 / 华安的明细 pass2（纵带 + 豆包；锚点 ``blocks``、上下文 ``words``；三家表头附录均走
``CommercialPass2Pass2TableConfig`` / ``CommercialPass2TableHeaderExtractSpec`` + ``extract_table``
+ ``compose_table_llm_user_prompt`` 流水线）等）。

HTTP 层见 ``ocr_service``；PDF 通用工具见 ``common``。
"""

from __future__ import annotations

import atexit
import io
import json
import logging
from dataclasses import dataclass
import os
import time
import re
import threading
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from enum import Enum
from typing import Any, Callable, Dict, Final, List, Literal, Optional, Sequence, Set, Tuple
from urllib.error import URLError
from urllib.request import Request, urlopen

from common import (
    build_extract_table_words_header_section_preamble,
    compose_table_llm_user_prompt,
    extract_pypdf_page_texts,
    extract_table,
    format_pymupdf_block_text_like_cluster_script,
    iter_pymupdf_block_rect_items,
    iter_pymupdf_word_rect_items,
    load_pdf_bytes,
    norm_paren_for_table_header_match,
    pymupdf_prompt_meta_str,
    split_pypdf_text_into_blocks,
)

logger = logging.getLogger(__name__)

ExtractionState = Literal[
    "保险公司抽取",
    "保险期间抽取",
    "被保人抽取",
    "车牌号抽取",
    "车架号抽取",
    "签单日期抽取",
    "保险费合计抽取",
    "保险费明细抽取",
]

INSURER_EXTRACTION_STATE: ExtractionState = "保险公司抽取"
PERIOD_EXTRACTION_STATE: ExtractionState = "保险期间抽取"
INSURED_EXTRACTION_STATE: ExtractionState = "被保人抽取"
LICENSE_PLATE_EXTRACTION_STATE: ExtractionState = "车牌号抽取"
VIN_EXTRACTION_STATE: ExtractionState = "车架号抽取"
SIGN_DATE_EXTRACTION_STATE: ExtractionState = "签单日期抽取"
PREMIUM_TOTAL_EXTRACTION_STATE: ExtractionState = "保险费合计抽取"
PREMIUM_DETAIL_EXTRACTION_STATE: ExtractionState = "保险费明细抽取"


def _log_pass(
    engine_label: str,
    state: ExtractionState,
    pass_no: int,
    message: str,
    *args: Any,
) -> None:
    logger.debug("[%s][state=%s][pass %s] " + message, engine_label, state, pass_no, *args)


# ---------------------------------------------------------------------------
# 响应 kv 键（与 HTTP 约定）
# ---------------------------------------------------------------------------

_CAR_INSURANCE_COMMON_KV_KEYS: tuple[str, ...] = (
    "被保险人",
    "保险期间",
    "保险公司名称",
    "签单日期",
    "保险费合计",
    "车牌号",
    "车架号",
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
    "免赔额",
)


PymupdfBlockRectItem = Tuple[int, float, float, float, float, str]
PymupdfWordRectItem = Tuple[int, int, float, float, float, float, str, int, int, int]


@dataclass(frozen=True)
class CarInsurancePdfViews:
    """车险抽取统一输入视图；当前只预抽 PDF 前两页。"""

    pypdf_page_texts: List[str]
    pypdf_blocks: List[str]
    pymupdf_blocks: List[str]
    pymupdf_block_rect_items: List[PymupdfBlockRectItem]
    pymupdf_word_rect_items: List[PymupdfWordRectItem]


def extract_car_insurance_pdf_views(
    pdf_bytes: bytes,
    *,
    max_pages: int = 2,
) -> CarInsurancePdfViews:
    from pypdf import PdfReader
    import fitz

    page_limit = max(0, int(max_pages))
    reader = PdfReader(io.BytesIO(pdf_bytes))
    pypdf_page_texts = [
        (reader.pages[i].extract_text() or "")
        for i in range(min(page_limit, len(reader.pages)))
    ]
    pypdf_blocks: List[str] = []
    for page_text in pypdf_page_texts:
        pypdf_blocks.extend(split_pypdf_text_into_blocks(page_text))

    pymupdf_block_rect_items: List[PymupdfBlockRectItem] = []
    pymupdf_word_rect_items: List[PymupdfWordRectItem] = []
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        for pi in range(min(page_limit, doc.page_count)):
            page = doc[pi]
            for b in page.get_text("blocks") or []:
                if len(b) < 5:
                    continue
                btype: Optional[int] = None
                if len(b) > 6:
                    try:
                        btype = int(b[6])
                    except (TypeError, ValueError):
                        btype = None
                if btype == 1:
                    continue
                txt = b[4] if isinstance(b[4], str) else str(b[4])
                text = txt.strip()
                if not text:
                    continue
                pymupdf_block_rect_items.append(
                    (pi, float(b[0]), float(b[1]), float(b[2]), float(b[3]), text),
                )

            for wi, w in enumerate(page.get_text("words") or [], start=1):
                if len(w) < 5:
                    continue
                raw = w[4]
                wt = raw if isinstance(raw, str) else str(raw)
                text = wt.strip()
                if not text:
                    continue
                try:
                    bn = int(float(w[5])) if len(w) > 5 else -1
                except (TypeError, ValueError):
                    bn = -1
                try:
                    ln = int(float(w[6])) if len(w) > 6 else -1
                except (TypeError, ValueError):
                    ln = -1
                try:
                    wn = int(float(w[7])) if len(w) > 7 else -1
                except (TypeError, ValueError):
                    wn = -1
                pymupdf_word_rect_items.append(
                    (
                        pi,
                        wi,
                        float(w[0]),
                        float(w[1]),
                        float(w[2]),
                        float(w[3]),
                        text,
                        bn,
                        ln,
                        wn,
                    ),
                )
    finally:
        doc.close()

    return CarInsurancePdfViews(
        pypdf_page_texts=pypdf_page_texts,
        pypdf_blocks=pypdf_blocks,
        pymupdf_blocks=[row[5] for row in pymupdf_block_rect_items],
        pymupdf_block_rect_items=pymupdf_block_rect_items,
        pymupdf_word_rect_items=pymupdf_word_rect_items,
    )


def _car_insurance_empty_kv(policy_type: str) -> Dict[str, Any]:
    """未识别字段用空字符串，HTTP JSON 中不出现 null（``保险期间`` 未识别时亦为 ``""``）。"""
    kv: Dict[str, Any] = {k: "" for k in _CAR_INSURANCE_COMMON_KV_KEYS}
    if policy_type == "commercial":
        for k in _CAR_INSURANCE_COMMERCIAL_ONLY_KV_KEYS:
            kv[k] = ""
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

# pass3：除「…公司」外，允许分支机构常见结尾（如营销服务部）
_INSURER_PASS3_ORG_TAIL_RE = re.compile(r"(公司|服务部|营业部)$")
# 已含分支机构片段则不再向下一块拼接（与「股份有限公司」总公司行区分）
_INSURER_BRANCH_INFIX_RE = re.compile(r"(分公司|支公司|营业部|营销服务部|直属营业部)")

# pass5（豆包 LLM）：与锚块 bbox 纵向重叠并允许上下各扩展 ±此值（pt）
_INSURER_DOUBAO_Y_PAD_PT = 100.0
# LLM 用户侧拼接长度上限（防超长）
_INSURER_DOUBAO_CONTEXT_MAX_CHARS = 12000
# 方舟 OpenAI 兼容 API 根路径（写死）；实际请求 ``{根}/chat/completions``
_INSURER_DOUBAO_API_BASE = "https://ark.cn-beijing.volces.com/api/v3"
# 仅存 key / 模型接入点时再请求
_INSURER_DOUBAO_ENV_API_KEY = "CAR_INSURANCE_DOUBAO_API_KEY"
_INSURER_DOUBAO_ENV_MODEL = "CAR_INSURANCE_DOUBAO_MODEL"
# 方舟 Chat Completions OpenAI 兼容字段：仅输出 JSON（提示词中须出现 “JSON” 字样）
_DOUBAO_RESPONSE_FORMAT_JSON_OBJECT = {"type": "json_object"}
_SCALAR_LLM_NOT_FOUND: Final[str] = "__NOT_FOUND__"

# 车险模块内全部方舟/豆包 HTTP 共用线程池（与 FastAPI 侧 ``ocr_service`` 的 OCR 总池分离，避免同池嵌套 submit 死锁）
_CAR_INSURANCE_LLM_ENV_POOL_SIZE = "CAR_INSURANCE_LLM_THREAD_POOL_SIZE"
_CAR_INSURANCE_LLM_RESULT_TIMEOUT_S = 150
_CAR_INSURANCE_LLM_POOL_LOCK = threading.Lock()
_CAR_INSURANCE_LLM_EXECUTOR: Optional[ThreadPoolExecutor] = None


def _parse_car_insurance_llm_pool_size() -> int:
    try:
        return max(1, int(os.environ.get(_CAR_INSURANCE_LLM_ENV_POOL_SIZE, "30")))
    except ValueError:
        return 30


def _car_insurance_llm_executor() -> ThreadPoolExecutor:
    global _CAR_INSURANCE_LLM_EXECUTOR
    with _CAR_INSURANCE_LLM_POOL_LOCK:
        if _CAR_INSURANCE_LLM_EXECUTOR is None:
            _CAR_INSURANCE_LLM_EXECUTOR = ThreadPoolExecutor(
                max_workers=_parse_car_insurance_llm_pool_size(),
                thread_name_prefix="car-ins-llm-",
            )
        return _CAR_INSURANCE_LLM_EXECUTOR


def _shutdown_car_insurance_llm_executor() -> None:
    global _CAR_INSURANCE_LLM_EXECUTOR
    with _CAR_INSURANCE_LLM_POOL_LOCK:
        if _CAR_INSURANCE_LLM_EXECUTOR is not None:
            _CAR_INSURANCE_LLM_EXECUTOR.shutdown(wait=True)
            _CAR_INSURANCE_LLM_EXECUTOR = None


atexit.register(_shutdown_car_insurance_llm_executor)


def _run_car_insurance_llm_in_pool(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """在专用 LLM 线程池中执行阻塞 HTTP，供承保 pass5 与商业险明细 pass2 共用。"""
    ex = _car_insurance_llm_executor()
    fut = ex.submit(fn, *args, **kwargs)
    return fut.result(timeout=_CAR_INSURANCE_LLM_RESULT_TIMEOUT_S)


def _insurer_text_compact(s: str) -> str:
    """去空白后做简称/拆字 tolerant 匹配。"""
    return re.sub(r"[\s\r\n\t\u00a0]+", "", s)


def _compact_suggests_ping_an_or_hua_an(text: str) -> bool:
    c = _insurer_text_compact(text)
    if not c:
        return False
    if "中国平安财产保险" in c or "华安财产保险" in c:
        return True
    # 版式打断时：中国平安 + 财产保险 常同现；华安财险
    if "中国平安" in c and "财产保险" in c:
        return True
    if "华安财产" in c and "保险" in c:
        return True
    return False


def _insurer_kw_anchor_flat_index(items: Sequence[Tuple[int, float, float, float, float, str]]) -> Optional[int]:
    """
    含「承保公司简称关键字」文本块的扁平下标。

    多命中时 **优先** 同时含「公司名称」的块（承保单中部「公司名称：××支公司」），
    避免首张命中为页眉「××股份有限公司……保险单（电子保单）」——其纵带 ±pad 内不含分支机构全称
    （见 ``new/renmin-m.pdf``：标题块 y≈94，公司名称块 y≈590）。
    """
    first_any: Optional[int] = None
    preferred: Optional[int] = None
    for fi, (_, _, _, _, _, text) in enumerate(items):
        hit = False
        for name in INSURER_COMPANY_NAMES_PASS1:
            if name in text:
                hit = True
                break
        if not hit:
            continue
        if first_any is None:
            first_any = fi
        if "公司名称" in text and preferred is None:
            preferred = fi
    return preferred if preferred is not None else first_any


def _in_vertical_band(
    cand_y0: float,
    cand_y1: float,
    ref_y0: float,
    ref_y1: float,
    pad_pt: float,
) -> bool:
    lo = ref_y0 - pad_pt
    hi = ref_y1 + pad_pt
    return not (cand_y1 < lo or cand_y0 > hi)


def _build_insurer_doubao_context_lines(
    band_items: Sequence[Tuple[float, float, str]],
    *,
    max_chars: int = _INSURER_DOUBAO_CONTEXT_MAX_CHARS,
) -> str:
    """每行：``x0_pt=…`` + 制表符 + 原文，按排版顺序传入 LLM。"""
    rows: List[str] = []
    for x0, _y0, txt in sorted(band_items, key=lambda t: (t[1], t[0])):
        rows.append(f"x0_pt={x0:.1f}\t{txt}")
    blob = "\n".join(rows)
    if len(blob) <= max_chars:
        return blob
    return blob[: max_chars - 80] + "\n…(截断)"


def _build_insurer_doubao_word_context_lines(
    page_index: int,
    band_items: Sequence[Tuple[int, float, float, float, float, str, int, int, int]],
    *,
    max_chars: int = _INSURER_DOUBAO_CONTEXT_MAX_CHARS,
) -> str:
    rows: List[str] = []
    for wi, x0, y0, x1, y1, txt, bn, ln, wn in sorted(band_items, key=lambda t: (t[2], t[1], t[0])):
        rows.append(
            f"page_index={page_index}\tword_index={wi}\tx0_pt={x0:.1f}\ty0_pt={y0:.1f}\t"
            f"x1_pt={x1:.1f}\ty1_pt={y1:.1f}\tblock_no={pymupdf_prompt_meta_str(bn)}\t"
            f"line_no={pymupdf_prompt_meta_str(ln)}\tword_no={pymupdf_prompt_meta_str(wn)}\t"
            f"{format_pymupdf_block_text_like_cluster_script(txt)}"
        )
    blob = "\n".join(rows)
    if len(blob) <= max_chars:
        return blob
    return blob[: max_chars - 80] + "\n…(截断)"


def _doubao_infer_insurer_name(context_block: str) -> Optional[str]:
    """
    调用火山方舟 OpenAI Chat Completions（豆包）。
    需 ``CAR_INSURANCE_DOUBAO_API_KEY``、``CAR_INSURANCE_DOUBAO_MODEL``（接入点 ID）；
    API 基地址固定为 ``https://ark.cn-beijing.volces.com/api/v3``，请求路径为 ``/chat/completions``。
    """
    api_key = os.environ.get(_INSURER_DOUBAO_ENV_API_KEY, "").strip()
    model = os.environ.get(_INSURER_DOUBAO_ENV_MODEL, "").strip()
    chat_url = f"{_INSURER_DOUBAO_API_BASE.rstrip('/')}/chat/completions"
    if not api_key or not model:
        return None

    system_prompt = (
        "你是车险保单 OCR 版面辅助解析器。给定同一垂直条带内若干 PyMuPDF OCR 文本项，"
        "每行可能是 word 级 bbox 或块级文本。任务：从这些文本项中辨认 **承保保险公司的完整法定名称**，"
        "通常应输出完整一条：名称形态通常以「公司」结尾；分支机构常见表述包括分公司、支公司、营业部、服务部、营销服务部等。"
        "若同一承保机构在块内另起为「××市…营业部」「…直属营业部」等与上文「…股份有限公司」等相连，通常应合并为一条全称输出。"
        "忽略投保人、被保险人、第三方名称。"
        "你必须只输出一个合法 JSON 对象（UTF-8），不要 markdown、不要解释。"
        '根对象有且仅有一个键 "result"，值为字符串：能确定时填承保公司法定全称；无法确定时填空字符串 ""，勿输出 UNKNOWN 等占位词。'
        '示例：{"result":"中国人民财产保险股份有限公司天津市南开支公司"}；不确定时：{"result":""}。'
    )
    user_content = (
        "以下为同一垂直条带内的文字项（已按从上到下、从左到右排序）：\n\n"
        + context_block
    )
    payload = {
        "model": model,
        "temperature": 0.0,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "response_format": _DOUBAO_RESPONSE_FORMAT_JSON_OBJECT,
    }

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = Request(
        chat_url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with urlopen(req, timeout=120) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except (URLError, OSError):
        return None

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    try:
        content = (
            data.get("choices", [{}])[0]
            .get("message", {})
            .get("content", "")
            .strip()
        )
    except (IndexError, TypeError, AttributeError):
        content = ""

    if not content or content.upper() == "UNKNOWN":
        return None
    obj = _commercial_parse_llm_json_object(content)
    if isinstance(obj, dict):
        name = obj.get("result")
        if isinstance(name, str):
            first = name.strip().strip("`\"“” ")
            if first and first.upper() != "UNKNOWN":
                return first
            return None
    lines = content.splitlines()
    first = (lines[0].strip() if lines else "").strip("`\"“” ")
    return first if first else None


def _build_first_page_word_context_lines(
    word_rect_items: Sequence[PymupdfWordRectItem],
    *,
    max_chars: int = _INSURER_DOUBAO_CONTEXT_MAX_CHARS,
) -> str:
    rows: List[str] = []
    for pi, wi, x0, y0, x1, y1, txt, bn, ln, wn in word_rect_items:
        if pi != 0:
            continue
        rows.append(
            f"page_index={pi}\tword_index={wi}\tx0_pt={x0:.1f}\ty0_pt={y0:.1f}\t"
            f"x1_pt={x1:.1f}\ty1_pt={y1:.1f}\tblock_no={pymupdf_prompt_meta_str(bn)}\t"
            f"line_no={pymupdf_prompt_meta_str(ln)}\tword_no={pymupdf_prompt_meta_str(wn)}\t"
            f"{format_pymupdf_block_text_like_cluster_script(txt)}"
        )
    blob = "\n".join(rows)
    if len(blob) <= max_chars:
        return blob
    return blob[: max_chars - 80] + "\n…(截断)"


def _scalar_llm_field_value(raw: Any) -> str:
    value = str(raw or "").strip()
    if value == _SCALAR_LLM_NOT_FOUND:
        return ""
    return value


def _doubao_infer_scalar_fields_from_first_page_words(
    context_block: str,
    *,
    need_sign_date: bool,
    need_premium_total: bool,
    need_period: bool,
) -> Optional[Dict[str, str]]:
    api_key = os.environ.get(_INSURER_DOUBAO_ENV_API_KEY, "").strip()
    model = os.environ.get(_INSURER_DOUBAO_ENV_MODEL, "").strip()
    chat_url = f"{_INSURER_DOUBAO_API_BASE.rstrip('/')}/chat/completions"
    if not api_key or not model:
        return None
    if not (need_sign_date or need_premium_total or need_period):
        return None

    field_lines = [
        f'签单日期：{"需要识别" if need_sign_date else "已有值，不要识别，返回 " + _SCALAR_LLM_NOT_FOUND}',
        f'保险费合计：{"需要识别" if need_premium_total else "已有值，不要识别，返回 " + _SCALAR_LLM_NOT_FOUND}',
        f'保险期间：{"需要识别" if need_period else "已有值，不要识别，start/end 均返回 " + _SCALAR_LLM_NOT_FOUND}',
    ]
    system_prompt = (
        "你是车险保单 OCR 版面辅助解析器。用户会给出第一页 PyMuPDF get_text(\"words\") 的 word 级 bbox。"
        "你只抽取被标记为“需要识别”的字段；标记为已有值的字段不要重新识别。"
        "字段规则：签单日期为保单签发/签单日期；保险费合计为全单总保费金额，不是单项保费；"
        "保险期间为保险责任起止时间，分别输出 start/end。"
        "日期时间尽量输出原文可对应的完整日期时间；金额只输出金额字符串。"
        "你必须只根据原文抽取，禁止编造。"
        "你必须只输出一个合法 JSON 对象（UTF-8），不要 markdown、不要解释。"
        '根对象有且仅有一个键 "result"，值为对象；对象必须包含 sign_date、premium_total、period_start、period_end 四个字符串键。'
        f'任何无法确定或不需要识别的字段，必须输出 "{_SCALAR_LLM_NOT_FOUND}"。'
        f'示例：{{"result":{{"sign_date":"2025年5月18日","premium_total":"1234.56",'
        f'"period_start":"2025年5月19日00:00","period_end":"2026年5月18日24:00"}}}}。'
    )
    user_content = (
        "字段识别需求：\n"
        + "\n".join(field_lines)
        + "\n\n以下为第一页 PyMuPDF word bbox：\n\n"
        + context_block
    )
    payload = {
        "model": model,
        "temperature": 0.0,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "response_format": _DOUBAO_RESPONSE_FORMAT_JSON_OBJECT,
    }

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = Request(
        chat_url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with urlopen(req, timeout=120) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except (URLError, OSError):
        return None

    try:
        data = json.loads(raw)
        content = (
            data.get("choices", [{}])[0]
            .get("message", {})
            .get("content", "")
            .strip()
        )
    except (json.JSONDecodeError, IndexError, TypeError, AttributeError):
        return None

    obj = _commercial_parse_llm_json_object(content)
    if not obj:
        return None
    inner = obj.get("result")
    row: Dict[str, Any] = inner if isinstance(inner, dict) else obj
    return {
        "sign_date": _scalar_llm_field_value(row.get("sign_date", "")),
        "premium_total": _scalar_llm_field_value(row.get("premium_total", "")),
        "period_start": _scalar_llm_field_value(row.get("period_start", "")),
        "period_end": _scalar_llm_field_value(row.get("period_end", "")),
    }


def _normalize_scalar_llm_result(row: Dict[str, str]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    sign_date = _scalar_llm_field_value(row.get("sign_date", ""))
    if sign_date:
        iso = _convert_to_iso_format(sign_date)
        if iso:
            out["签单日期"] = iso
    premium_total = _scalar_llm_field_value(row.get("premium_total", ""))
    if premium_total:
        amount = _commercial_parse_money_amount(premium_total)
        if amount is not None:
            out["保险费合计"] = amount
    start_raw = _scalar_llm_field_value(row.get("period_start", ""))
    end_raw = _scalar_llm_field_value(row.get("period_end", ""))
    if start_raw and end_raw:
        start_iso = _convert_to_iso_format(start_raw)
        end_iso = _convert_to_iso_format(end_raw)
        if start_iso and end_iso:
            out["保险期间"] = {"start": start_iso, "end": end_iso}
    return out


def run_car_insurance_scalar_llm_fallback(
    *,
    word_rect_items: Sequence[PymupdfWordRectItem],
    need_sign_date: bool,
    need_premium_total: bool,
    need_period: bool,
) -> Dict[str, Any]:
    if not (need_sign_date or need_premium_total or need_period):
        return {}
    ctx = _build_first_page_word_context_lines(word_rect_items)
    if not ctx:
        return {}
    raw = _run_car_insurance_llm_in_pool(
        _doubao_infer_scalar_fields_from_first_page_words,
        ctx,
        need_sign_date=need_sign_date,
        need_premium_total=need_premium_total,
        need_period=need_period,
    )
    if not raw:
        return {}
    return _normalize_scalar_llm_result(raw)


def _pass5_insurer_name_doubao_llm(
    pdf_bytes: Optional[bytes] = None,
    *,
    engine_label: str,
    anchor_fallback: bool = False,
    block_rect_items: Optional[Sequence[PymupdfBlockRectItem]] = None,
    word_rect_items: Optional[Sequence[PymupdfWordRectItem]] = None,
) -> Optional[str]:
    """基于「关键字块 bbox」±纵带筛 word，拼装坐标+文本调用豆包。"""
    if block_rect_items is None:
        if pdf_bytes is None:
            return None
        try:
            items = iter_pymupdf_block_rect_items(pdf_bytes)
        except ImportError:
            return None
    else:
        items = list(block_rect_items)

    if word_rect_items is None and pdf_bytes is not None:
        try:
            words = iter_pymupdf_word_rect_items(pdf_bytes)
        except ImportError:
            words = []
    else:
        words = list(word_rect_items or [])

    ai = _insurer_kw_anchor_flat_index(items)
    if ai is None and anchor_fallback:
        ai = _fallback_insurer_llm_anchor_flat_index(items)
    if ai is None:
        return None

    ap, _ax0, ay0, _ax1, ay1, _anchor_text = items[ai]
    if words:
        band_word_rows: List[Tuple[int, float, float, float, float, str, int, int, int]] = []
        for pi, wi, x0, y0, x1, y1, text, bn, ln, wn in words:
            if pi != ap:
                continue
            if _in_vertical_band(y0, y1, ay0, ay1, _INSURER_DOUBAO_Y_PAD_PT):
                band_word_rows.append((wi, x0, y0, x1, y1, text, bn, ln, wn))
        if band_word_rows:
            ctx = _build_insurer_doubao_word_context_lines(ap, band_word_rows)
            return _run_car_insurance_llm_in_pool(
                _doubao_infer_insurer_name,
                ctx,
            )

    band_rows: List[Tuple[float, float, str]] = []
    for pi, x0, y0, _x1, y1, text in items:
        if pi != ap:
            continue
        if _in_vertical_band(y0, y1, ay0, ay1, _INSURER_DOUBAO_Y_PAD_PT):
            band_rows.append((x0, y0, text))
    if not band_rows:
        return None

    ctx = _build_insurer_doubao_context_lines(band_rows)
    return _run_car_insurance_llm_in_pool(
        _doubao_infer_insurer_name,
        ctx,
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


def _joined_pdf_mu_blocks_blob(
    blocks_pdf: Sequence[str],
    blocks_mu: Sequence[str],
) -> str:
    return "\n".join(blocks_pdf) + "\n" + "\n".join(blocks_mu)


def _merge_insurer_display_prefer_longer(
    pymupdf_name: Optional[str],
    pypdf_name: Optional[str],
) -> Optional[str]:
    """
    双路承保公司展示名合并。

    PyMuPDF 常把「…股份有限公司」与「××分公司/支公司」拆到不同 text 块，pass1 扩展只在单块内拼接，
    易得到较短总公司名；pypdf 块合并后往往带齐分支机构。若一路为另一路的前缀，则取 **较长** 串。
    """
    a = (pymupdf_name or "").strip()
    b = (pypdf_name or "").strip()
    if not a:
        return b or None
    if not b:
        return a or None
    if a == b:
        return a
    if b.startswith(a):
        return b
    if a.startswith(b):
        return a
    return a


def _commercial_blocks_hint_ping_an_or_hua_an(
    blocks_pdf: Sequence[str],
    blocks_mu: Sequence[str],
) -> bool:
    """双路块文本中是否含平安/华安承保简称（与其它保司版面区分）。"""
    blob = _joined_pdf_mu_blocks_blob(blocks_pdf, blocks_mu)
    return _compact_suggests_ping_an_or_hua_an(blob)


def _commercial_insurer_must_run_doubao_overlay(
    *,
    insurer_name: Optional[str],
    known_company: Optional[KnownInsuranceCompany],
    blocks_pdf: Sequence[str],
    blocks_mu: Sequence[str],
) -> bool:
    """
    商业险且承保方为平安或华安时：须在合并后强制执行一次承保公司豆包，并以有效结果为准。
    识别方式：枚举、已合并全称、或未命中规则但块文本中出现对应简称。
    """
    if known_company in (KnownInsuranceCompany.PING_AN, KnownInsuranceCompany.HUA_AN):
        return True
    name = insurer_name or ""
    if _compact_suggests_ping_an_or_hua_an(name):
        return True
    blob = _joined_pdf_mu_blocks_blob(blocks_pdf, blocks_mu)
    return _compact_suggests_ping_an_or_hua_an(blob)


def _fallback_insurer_llm_anchor_flat_index(
    items: Sequence[Tuple[int, float, float, float, float, str]],
) -> Optional[int]:
    """无简称整块命中时：第 1 页最上、最左文本块作纵带锚点（仅作 LLM 上下文）。"""
    pool = [(i, pi, x0, y0, x1, y1, t) for i, (pi, x0, y0, x1, y1, t) in enumerate(items)]
    if not pool:
        return None
    p0 = [row for row in pool if row[1] == 0]
    use = sorted(p0 or pool, key=lambda r: (r[3], r[2]))
    return use[0][0]


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
    while right < len(block):
        ch = block[right]
        if _is_chinese_char(ch):
            right += 1
            continue
        # 「股份有限公司」与「天津市直属营业部」等分支机构常被空白/换行拆开，仍视为同一承保名称
        if ch in " \t\r\n\f\v\u3000":
            peek = right
            while peek < len(block) and block[peek] in " \t\r\n\f\v\u3000":
                peek += 1
            if peek < len(block) and _is_chinese_char(block[peek]):
                right = peek
                continue
        break
    s = block[left:right]
    # 跨空白续写后，块内「…营业部」之后常紧跟其它中文（如条款标题），截至末处「营业部」
    if "营业部" in s:
        return s[: s.rfind("营业部") + len("营业部")]
    return s


def _insurer_display_passes_pass3(value: str) -> bool:
    v = value.strip()
    if not v:
        return False
    return v.endswith("公司") or bool(_INSURER_PASS3_ORG_TAIL_RE.search(v))


def _append_insurer_name_from_following_blocks(
    blocks: Sequence[str],
    hit_bi: int,
    base: str,
) -> str:
    """
    承保公司名在 OCR 块边界处被拆开时：上一块止于「…股份有限公司」等，下一块常为「××市…支公司」。
    仅在尚未出现分支机构关键词时，尝试吸收后续少量块中的中文机构名片段。
    """
    s = (base or "").strip()
    if not s or hit_bi < 0 or hit_bi + 1 >= len(blocks):
        return s
    if _INSURER_BRANCH_INFIX_RE.search(s):
        return s
    if not s.endswith("公司"):
        return s

    stop_markers = (
        "被保险人",
        "投保人",
        "车主",
        "车牌号码",
        "车架号",
        "发动机号",
        "保险单号",
        "号牌号码",
        "厂牌型号",
        "争议处理",
        "司法管辖",
        "组织机构代码",
        "证件号码",
    )
    max_appends = 3
    appends = 0
    for j in range(hit_bi + 1, min(len(blocks), hit_bi + 1 + 8)):
        if appends >= max_appends:
            break
        if _INSURER_BRANCH_INFIX_RE.search(s):
            break
        raw = blocks[j]
        part = raw.strip() if raw else ""
        if not part:
            continue
        if any(m in part for m in stop_markers):
            break
        cand = re.sub(r"[\s\r\n\t\f\v　]+", "", part)
        if not cand or len(cand) > 48:
            break
        if sum(1 for c in cand if _is_chinese_char(c) or c in "（）()") < len(cand) * 0.82:
            continue
        merged = s + cand
        if not _insurer_display_passes_pass3(merged):
            break
        s = merged
        appends += 1
    return s


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
    m = re.search(r"公司名称\s*[：:]", block)
    tail = block[m.end() :] if m else None
    if not tail or not tail.strip():
        return None
    # 紧随其后的「公司网址」「客户服务」等不参与名称；不在此按单行 ``\n`` 截断，以免漏掉「…营业部」等换行续写
    tail = re.split(
        r"(公司网址|全国统一|客户服务|网址\s*[:：]|https?://)",
        tail,
        maxsplit=1,
    )[0]
    second = re.sub(r"[\s\r\n\t]+", "", tail)
    return second if second else None


def _pass4_enum_for_hit(hit: str) -> Optional[KnownInsuranceCompany]:
    for name in INSURER_COMPANY_NAMES_PASS1:
        if name in hit:
            return _NAME_TO_ENUM.get(name)
    return None


def _insurer_display_suggests_pic_for_pass5_overlay(
    display: Optional[str],
    known_company: Optional[KnownInsuranceCompany],
) -> bool:
    """人保（PICC）：工程侧 pass1/pass2 已命中仍允许再走 pass5 豆包，非空则覆盖。"""
    if known_company == KnownInsuranceCompany.PICC_P:
        return True
    if display and "中国人民财产保险" in display:
        return True
    return False


def _pass3_filter_insurer_name(
    value: Optional[str],
    *,
    from_pass: int,
    engine_label: str,
) -> Optional[str]:
    if value is None:
        return None
    if value.endswith("公司") or bool(_INSURER_PASS3_ORG_TAIL_RE.search(value)):
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
    pdf_bytes: Optional[bytes] = None,
    block_rect_items: Optional[Sequence[PymupdfBlockRectItem]] = None,
    word_rect_items: Optional[Sequence[PymupdfWordRectItem]] = None,
    defer_bbox_llm: bool = False,
) -> Dict[str, Any]:
    state = INSURER_EXTRACTION_STATE
    pass1_hit: Optional[str] = None
    pass1_block_idx: Optional[int] = None
    all_pass1 = _collect_all_pass1_hits(blocks)
    if all_pass1:
        _log_pass(engine_label, state, 1, "全部命中 %s 条：%s", len(all_pass1), all_pass1)
        chosen = _choose_pass1_hit(all_pass1, blocks)
        if chosen is not None:
            bi, m0, m1, matched_name = chosen
            block = blocks[bi]
            pass1_hit = _extend_pass1_hit_within_block(block, m0, m1)
            pass1_hit = _append_insurer_name_from_following_blocks(blocks, bi, pass1_hit)
            pass1_block_idx = bi
            _log_pass(
                engine_label,
                state,
                1,
                "选用：value=%r matched_substring=%r block=%r block_index=%s",
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
                pass2_hit = _append_insurer_name_from_following_blocks(blocks, bi, v)
                pass2_block_idx = bi
                _log_pass(engine_label, state, 2, "命中：保险公司名称=%r block_index=%s", v, bi)
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
            _log_pass(engine_label, state, 4, "命中：known_company=%s", known_company.name)

    pass5_hit: Optional[str] = None
    can_run_bbox_llm = not defer_bbox_llm and (pdf_bytes is not None or block_rect_items is not None)
    if can_run_bbox_llm:
        pic_pass5_overlay = (
            insurer_display is not None
            and _insurer_display_suggests_pic_for_pass5_overlay(insurer_display, known_company)
        )
        run_pass5 = insurer_display is None or pic_pass5_overlay
        if run_pass5:
            cand5 = _pass5_insurer_name_doubao_llm(
                pdf_bytes,
                engine_label=engine_label,
                anchor_fallback=(insurer_display is None),
                block_rect_items=block_rect_items,
                word_rect_items=word_rect_items,
            )
            cand5 = _pass3_filter_insurer_name(cand5, from_pass=5, engine_label=engine_label)
            if cand5 is not None and cand5.strip() != "":
                pass5_hit = cand5
                insurer_display = cand5
                known_company = _pass4_enum_for_hit(insurer_display)
                if pic_pass5_overlay:
                    _log_pass(
                        engine_label,
                        state,
                        5,
                        "pass5 人保豆包覆盖：保险公司名称=%r",
                        pass5_hit,
                    )
                else:
                    _log_pass(
                        engine_label,
                        state,
                        5,
                        "pass5 bbox LLM 命中：保险公司名称=%r",
                        pass5_hit,
                    )
                if known_company is not None:
                    _log_pass(engine_label, state, 4, "命中：known_company=%s", known_company.name)

    return {
        "pass1_hit": pass1_hit,
        "pass1_block_index": pass1_block_idx,
        "pass2_hit": pass2_hit,
        "pass2_block_index": pass2_block_idx,
        "pass5_hit": pass5_hit,
        "保险公司名称": insurer_display,
        "known_company": known_company,
    }


INSURED_NAME_PASS1_EXACT: str = "嗨车购（天津）融资租赁有限公司"
INSURED_NAME_PASS1_ALT: str = "天津明德通汽车租赁有限公司"
INSURED_NAME_PASS_TARGETS: Tuple[str, ...] = (
    INSURED_NAME_PASS1_EXACT,
    INSURED_NAME_PASS1_ALT,
)


def _normalize_company_text(text: str) -> str:
    """公司名归一化：统一括号形态并移除空白。"""
    return (
        text.replace("（", "(")
        .replace("）", ")")
        .replace(" ", "")
        .replace("\t", "")
        .replace("\r", "")
        .replace("\n", "")
    )


def _match_known_insured_name(text: str) -> Optional[str]:
    normalized_text = _normalize_company_text(text)
    for target in INSURED_NAME_PASS_TARGETS:
        if _normalize_company_text(target) in normalized_text:
            return target
    return None


def _pass1_insured_name_from_blocks(blocks: Sequence[str]) -> Optional[Tuple[int, str]]:
    for bi, block in enumerate(blocks):
        matched = _match_known_insured_name(block)
        if matched is not None:
            return bi, matched
    return None


def _pass2_insured_name_from_block(block: str) -> Optional[str]:
    m = re.search(r"投保人[：:]", block)
    if not m:
        return None

    tail = block[m.end():]
    if not tail.strip():
        return None

    matched = _match_known_insured_name(tail)
    if matched is not None:
        return matched

    tokens = re.split(r"\s+", tail.strip())
    for token in tokens:
        cleaned = token.strip("，。；;、:：()（）[]【】")
        matched = _match_known_insured_name(cleaned)
        if matched is not None:
            return matched
        if "有限公司" in cleaned:
            return cleaned
    return None


def run_car_insurance_insured_passes(
    blocks: Sequence[str],
    *,
    engine_label: str,
) -> Dict[str, Any]:
    state = INSURED_EXTRACTION_STATE

    pass1_hit: Optional[str] = None
    pass1_block_idx: Optional[int] = None
    pass1_result = _pass1_insured_name_from_blocks(blocks)
    if pass1_result is not None:
        pass1_block_idx, pass1_hit = pass1_result
        _log_pass(engine_label, state, 1, "命中：被保人=%r block_index=%s", pass1_hit, pass1_block_idx)

    pass2_hit: Optional[str] = None
    pass2_block_idx: Optional[int] = None
    if pass1_hit is None:
        for bi, block in enumerate(blocks):
            v = _pass2_insured_name_from_block(block)
            if v is not None:
                pass2_hit = v
                pass2_block_idx = bi
                _log_pass(engine_label, state, 2, "命中：被保人=%r block_index=%s", v, bi)
                break

    insured_display: Optional[str] = pass1_hit or pass2_hit
    return {
        "pass1_hit": pass1_hit,
        "pass1_block_index": pass1_block_idx,
        "pass2_hit": pass2_hit,
        "pass2_block_index": pass2_block_idx,
        "被保险人": insured_display,
    }


_KEYED_TOKEN_LEADING_PUNCT_RE = re.compile(r"^[：:：,，;；、)）\]】]+")
_KEYED_TOKEN_SPLIT_RE = re.compile(r"[ \t\r\n]+")
_LICENSE_PLATE_RE = re.compile(r"^[\u4e00-\u9fffA-Za-z0-9-]+$")
_VIN_RE = re.compile(r"^[A-Za-z0-9]+$")
_KEYED_TOKEN_BBOX_Y_TOLERANCE_PT = 10.0


def _find_keyword_end_tolerant(text: str, keyword: str, *, start: int = 0) -> Optional[Tuple[int, int]]:
    i = start
    while i < len(text):
        if text[i].isspace():
            i += 1
            continue
        if text[i] != keyword[0]:
            i += 1
            continue

        j = i + 1
        ki = 1
        while ki < len(keyword):
            while j < len(text) and text[j].isspace():
                j += 1
            if j >= len(text) or text[j] != keyword[ki]:
                break
            j += 1
            ki += 1
        if ki == len(keyword):
            return i, j
        i += 1
    return None


def _next_space_or_tab_delimited_token_after_keyword(block: str, keyword: str) -> Optional[str]:
    pos = 0
    while True:
        hit = _find_keyword_end_tolerant(block, keyword, start=pos)
        if hit is None:
            return None
        _idx, tail_start = hit
        if tail_start < len(block) and block[tail_start] == "码":
            tail_start += 1
        tail = block[tail_start:].lstrip()
        tail = _KEYED_TOKEN_LEADING_PUNCT_RE.sub("", tail).lstrip()
        if tail:
            token = _KEYED_TOKEN_SPLIT_RE.split(tail, maxsplit=1)[0].strip()
            token = token.strip("，。；;、:：()（）[]【】")
            if token:
                return token
        pos = tail_start


def _license_plate_token_passes(token: str) -> bool:
    has_chinese = any(_is_chinese_char(ch) for ch in token)
    return (
        bool(token)
        and len(token) <= 9
        and bool(_LICENSE_PLATE_RE.fullmatch(token))
        and any(ch.isascii() and ch.isalpha() for ch in token)
        and any(ch.isascii() and ch.isdigit() for ch in token)
        and (
            not has_chinese
            or (_is_chinese_char(token[0]) and not any(_is_chinese_char(ch) for ch in token[1:]))
        )
    )


def _vin_token_passes(token: str) -> bool:
    return (
        bool(token)
        and bool(_VIN_RE.fullmatch(token))
        and any(ch.isalpha() for ch in token)
        and any(ch.isdigit() for ch in token)
    )


def _pass1_keyed_token_from_block(
    block: str,
    *,
    keyword: str,
    validator: Callable[[str], bool],
) -> Optional[str]:
    token = _next_space_or_tab_delimited_token_after_keyword(block, keyword)
    if token is None:
        return None
    return token if validator(token) else None


def _pass1_keyed_token_from_block_any_keyword(
    block: str,
    *,
    keywords: Sequence[str],
    validator: Callable[[str], bool],
) -> Optional[str]:
    for keyword in keywords:
        v = _pass1_keyed_token_from_block(
            block,
            keyword=keyword,
            validator=validator,
        )
        if v is not None:
            return v
    return None


def _valid_whole_pattern_text(
    text: str,
    *,
    token_re: re.Pattern[str],
    validator: Callable[[str], bool],
) -> Optional[str]:
    token = re.sub(r"\s+", "", text).strip("，。；;、:：()（）[]【】")
    if not token_re.fullmatch(token):
        return None
    return token if validator(token) else None


def _pass2_keyed_token_with_bbox(
    pdf_bytes: Optional[bytes] = None,
    *,
    keywords: Sequence[str],
    token_re: re.Pattern[str],
    validator: Callable[[str], bool],
    engine_label: str,
    output_key: str,
    word_rect_items: Optional[Sequence[PymupdfWordRectItem]] = None,
) -> Optional[str]:
    if word_rect_items is None:
        if pdf_bytes is None:
            return None
        try:
            words = iter_pymupdf_word_rect_items(pdf_bytes)
        except Exception:
            return None
    else:
        words = list(word_rect_items)

    ref = next(
        (
            w
            for w in words
            if any(_find_keyword_end_tolerant(w[6], keyword) is not None for keyword in keywords)
        ),
        None,
    )
    if ref is None:
        logger.debug("[%s] %s bbox pass2：未找到关键字word keywords=%r", engine_label, output_key, keywords)
        return None

    ref_page, ref_wi, ref_x0, ref_y0, ref_x1, ref_y1, ref_text, _ref_bn, _ref_ln, _ref_wn = ref
    logger.debug(
        "[%s] %s bbox pass2：参考word page=%s wi=%s x0=%.2f x1=%.2f y0=%.2f y1=%.2f text=%r",
        engine_label,
        output_key,
        ref_page,
        ref_wi,
        ref_x0,
        ref_x1,
        ref_y0,
        ref_y1,
        ref_text,
    )

    candidates: List[Tuple[float, float, int, str]] = []
    for page_index, wi, x0, y0, x1, y1, text, _bn, _ln, _wn in words:
        if page_index != ref_page:
            continue
        if _rects_close(x0, y0, x1, y1, ref_x0, ref_y0, ref_x1, ref_y1):
            continue
        if y0 < ref_y0 - _KEYED_TOKEN_BBOX_Y_TOLERANCE_PT:
            continue
        if y1 > ref_y1 + _KEYED_TOKEN_BBOX_Y_TOLERANCE_PT:
            continue
        if x0 < ref_x0:
            continue
        candidates.append((x0, y0, wi, text))

    for _x0, _y0, _wi, text in sorted(candidates, key=lambda row: (row[0], row[1], row[2])):
        v = _valid_whole_pattern_text(
            text,
            token_re=token_re,
            validator=validator,
        )
        if v is not None:
            logger.debug("[%s] %s bbox pass2 word纵带从key起始x向右命中：%r", engine_label, output_key, v)
            return v

    logger.debug("[%s] %s bbox pass2：word纵向条带key右侧候选未命中", engine_label, output_key)
    return None


def run_car_insurance_keyed_token_passes(
    blocks: Sequence[str],
    *,
    engine_label: str,
    output_key: str,
    keyword: str | Sequence[str],
    token_re: re.Pattern[str],
    validator: Callable[[str], bool],
    state: ExtractionState,
    pdf_bytes: Optional[bytes] = None,
    word_rect_items: Optional[Sequence[PymupdfWordRectItem]] = None,
) -> Dict[str, Any]:
    keywords = (keyword,) if isinstance(keyword, str) else tuple(keyword)
    pass1_hit: Optional[str] = None
    pass1_block_idx: Optional[int] = None
    for bi, block in enumerate(blocks):
        v = _pass1_keyed_token_from_block_any_keyword(
            block,
            keywords=keywords,
            validator=validator,
        )
        if v is not None:
            pass1_hit = v
            pass1_block_idx = bi
            _log_pass(engine_label, state, 1, "命中：%s=%r block_index=%s", output_key, v, bi)
            break

    pass2_hit: Optional[str] = None
    if pass1_hit is None and (pdf_bytes is not None or word_rect_items is not None):
        pass2_hit = _pass2_keyed_token_with_bbox(
            pdf_bytes,
            keywords=keywords,
            token_re=token_re,
            validator=validator,
            engine_label=engine_label,
            output_key=output_key,
            word_rect_items=word_rect_items,
        )
        if pass2_hit is not None:
            _log_pass(engine_label, state, 2, "bbox条带命中：%s=%r", output_key, pass2_hit)

    display = pass1_hit or pass2_hit
    return {
        "pass1_hit": pass1_hit,
        "pass1_block_index": pass1_block_idx,
        "pass2_hit": pass2_hit,
        output_key: display,
    }


def run_car_insurance_license_plate_passes(
    blocks: Sequence[str],
    *,
    engine_label: str,
    pdf_bytes: Optional[bytes] = None,
    word_rect_items: Optional[Sequence[PymupdfWordRectItem]] = None,
) -> Dict[str, Any]:
    return run_car_insurance_keyed_token_passes(
        blocks,
        engine_label=engine_label,
        output_key="车牌号",
        keyword=("号牌号码", "车牌号", "号码号牌"),
        token_re=re.compile(r"[\u4e00-\u9fffA-Za-z0-9-]+"),
        validator=_license_plate_token_passes,
        state=LICENSE_PLATE_EXTRACTION_STATE,
        pdf_bytes=pdf_bytes,
        word_rect_items=word_rect_items,
    )


def run_car_insurance_vin_passes(
    blocks: Sequence[str],
    *,
    engine_label: str,
    pdf_bytes: Optional[bytes] = None,
    word_rect_items: Optional[Sequence[PymupdfWordRectItem]] = None,
) -> Dict[str, Any]:
    return run_car_insurance_keyed_token_passes(
        blocks,
        engine_label=engine_label,
        output_key="车架号",
        keyword="车架号",
        token_re=re.compile(r"[A-Za-z0-9]+"),
        validator=_vin_token_passes,
        state=VIN_EXTRACTION_STATE,
        pdf_bytes=pdf_bytes,
        word_rect_items=word_rect_items,
    )


_SIGN_DATE_KEY_RE = re.compile(r"签单日期\s*[：:]")
_SIGN_DATE_VALUE_RE = re.compile(r"(?<![0-9])\d{4}-\d{2}-\d{2}(?![0-9])")
_SIGN_DATE_SLASH_RE = re.compile(r"(\d{4})/(\d{1,2})/(\d{1,2})")
_SIGN_DATE_CN_RE = re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日")


def _extract_sign_date_from_text(text: str) -> Optional[str]:
    tail_no_space = re.sub(r"\s+", "", text)
    m2 = _SIGN_DATE_VALUE_RE.search(tail_no_space)
    if m2:
        return m2.group(0)

    m_slash = _SIGN_DATE_SLASH_RE.search(tail_no_space)
    if m_slash:
        year, month, day = m_slash.groups()
        return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"

    m3 = _SIGN_DATE_CN_RE.search(tail_no_space)
    if not m3:
        return None
    year, month, day = m3.groups()
    return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"


def _pass1_sign_date_from_block(block: str) -> Optional[str]:
    m = _SIGN_DATE_KEY_RE.search(block)
    if not m:
        return None
    tail = block[m.end():]
    return _extract_sign_date_from_text(tail)


def _pass3_sign_date_with_bbox_fallback(
    pdf_bytes: Optional[bytes] = None,
    *,
    word_rect_items: Optional[Sequence[PymupdfWordRectItem]] = None,
) -> Optional[str]:
    """
    基于 word 级 bbox 回退提取签单日期（与保险期间 bbox 回退一致的纵向条带）：
    1) 找到包含「签单日期」的参考 word；
    2) 同页中筛 word：纵向下缘不高于参考 word 下缘（容差内）、上缘不低于参考 word 上缘（略低即可），
       即 y0 >= ref_y0 - tol 且 y1 <= ref_y1 + tol，避免跨行误取；
    3) 条带内各 word（含参考 word）按 (y0, x0) 顺序，在拼接文本上匹配日期 pattern。
    """
    if word_rect_items is None:
        if pdf_bytes is None:
            return None
        try:
            all_words = iter_pymupdf_word_rect_items(pdf_bytes)
        except ImportError:
            logger.debug("PyMuPDF未安装，无法使用签单日期bbox回退逻辑")
            return None
    else:
        all_words = list(word_rect_items)
    sign_words = [item for item in all_words if "签单日期" in item[6]]

    if not sign_words:
        logger.debug("签单日期bbox回退逻辑：未找到包含'签单日期'的word")
        return None

    ref_page, _ref_wi, ref_x0, ref_y0, ref_x1, ref_y1, ref_text, _ref_bn, _ref_ln, _ref_wn = sign_words[0]
    logger.debug(
        "签单日期bbox回退逻辑：参考word page=%s y0=%.2f y1=%.2f text=%r",
        ref_page,
        ref_y0,
        ref_y1,
        ref_text,
    )

    y_tolerance = 20.0
    candidates: List[Tuple[float, float, int, str]] = []
    for page_index, wi, x0, y0, _x1, y1, text, _bn, _ln, _wn in all_words:
        if page_index != ref_page:
            continue
        if y0 >= ref_y0 - y_tolerance and y1 <= ref_y1 + y_tolerance:
            candidates.append((y0, x0, wi, text))

    candidates.sort(key=lambda x: (x[0], x[1], x[2]))
    combined_text = " ".join(text for _, _, _, text in candidates)
    v = _extract_sign_date_from_text(combined_text)
    if v is not None:
        logger.debug("签单日期bbox回退逻辑命中：%s", v)
        return v

    for _, _, _, text in candidates:
        v = _extract_sign_date_from_text(text)
        if v is not None:
            logger.debug("签单日期bbox回退逻辑命中：%s", v)
            return v

    logger.debug("签单日期bbox回退逻辑：候选块未匹配到日期")
    return None


def run_car_insurance_sign_date_passes(
    blocks: Sequence[str],
    *,
    engine_label: str,
    pdf_bytes: Optional[bytes] = None,
    word_rect_items: Optional[Sequence[PymupdfWordRectItem]] = None,
) -> Dict[str, Any]:
    state = SIGN_DATE_EXTRACTION_STATE

    pass1_hit: Optional[str] = None
    pass1_block_idx: Optional[int] = None
    for bi, block in enumerate(blocks):
        v = _pass1_sign_date_from_block(block)
        if v is not None:
            pass1_hit = v
            pass1_block_idx = bi
            _log_pass(engine_label, state, 1, "命中：签单日期=%r block_index=%s", v, bi)
            break

    pass2_hit: Optional[str] = None
    pass2_block_idx: Optional[int] = None
    if pass1_hit is None:
        for bi, block in enumerate(blocks):
            if "签单日期" not in block:
                continue
            v = _extract_sign_date_from_text(block)
            if v is not None:
                pass2_hit = v
                pass2_block_idx = bi
                _log_pass(
                    engine_label,
                    state,
                    2,
                    "命中：签单日期=%r block_index=%s（含签单日期块全文）",
                    v,
                    bi,
                )
                break

    pass3_hit: Optional[str] = None
    if pass1_hit is None and pass2_hit is None and (pdf_bytes is not None or word_rect_items is not None):
        pass3_hit = _pass3_sign_date_with_bbox_fallback(
            pdf_bytes,
            word_rect_items=word_rect_items,
        )
        if pass3_hit is not None:
            _log_pass(engine_label, state, 3, "bbox条带回退命中：签单日期=%r", pass3_hit)

    sign_date_display = pass1_hit or pass2_hit or pass3_hit
    return {
        "pass1_hit": pass1_hit,
        "pass1_block_index": pass1_block_idx,
        "pass2_hit": pass2_hit,
        "pass2_block_index": pass2_block_idx,
        "pass3_hit": pass3_hit,
        "签单日期": sign_date_display,
    }


# 金额「整数部」：标准千分位 ``1,234,567`` 或无明千分位时一段连续数字（商业险明细 / 乘客保额 / 保险费合计等共用）
_COMMERCIAL_MONEY_INT_RE = r"(?:\d{1,3}(?:,\d{3})+|\d+)"
# 在串内 ``re.search`` 时起算：勿紧接在数字或逗号后（避免 ``100,000.00`` 被断成 ``000.00``）
_COMMERCIAL_MONEY_INT_SEARCH_PREFIX = r"(?<![\d,])"
_COMMERCIAL_MONEY_OPT_FRAC_CAPTURE = r"(?:\.(\d{1,2}))?"

# 保险费合计金额：整数（千分位规则同上）+ 「.」+ 恰好两位小数；不要求「元」
_PREMIUM_DECIMAL_RE = re.compile(
    rf"(?<![0-9])({_COMMERCIAL_MONEY_INT_RE})\.(\d{{2}})(?![0-9])",
)

_PREMIUM_KEY_LITERAL = "保险费合计"
# 合计金额整数部分至少位数（剔除千分位逗号后计数），合计一般 ≥1000；不足则跳过再找下一处匹配
_PREMIUM_MIN_INTEGER_DIGITS = 4
# bbox 纵向条带与签单日期 pass3、保险期间 bbox 一致（pt）
_PREMIUM_BBOX_Y_TOLERANCE_PTS = 20.0
# 邻近块须在参照块 bbox 左边线右侧（或小重叠），排除完全处于左侧的无关节
_PREMIUM_BBOX_REL_X0_EPS_PT = 2.0


def _rects_close(
    ax0: float,
    ay0: float,
    ax1: float,
    ay1: float,
    bx0: float,
    by0: float,
    bx1: float,
    by1: float,
    eps: float = 0.51,
) -> bool:
    """判断两个矩形是否视作同一块 PyMuPDF 框。"""
    return (
        abs(ax0 - bx0) <= eps
        and abs(ay0 - by0) <= eps
        and abs(ax1 - bx1) <= eps
        and abs(ay1 - by1) <= eps
    )


def _iter_decimal_amount_parts_in_tail(tail: str):
    """生成 tail 内从左到右、通过日期过滤规则的 ``(整数部,两位小数部)``。用于首匹配或取最大。"""
    for m in _PREMIUM_DECIMAL_RE.finditer(tail):
        int_raw, frac = m.group(1), m.group(2)
        digits = int_raw.replace(",", "")
        if not digits.isdigit():
            continue
        end = m.end()
        if (
            end < len(tail)
            and tail[end] == "."
            and len(digits) == 4
            and 1900 <= int(digits) <= 2100
            and 1 <= int(frac) <= 12
        ):
            continue
        yield int_raw, frac


def _premium_integer_part_meets_min_digits(
    int_raw: str,
    *,
    min_digits: int = _PREMIUM_MIN_INTEGER_DIGITS,
) -> bool:
    """整数部去掉千分位逗号后，须为纯数字且位数 >= min_digits。"""
    d = int_raw.replace(",", "")
    return d.isdigit() and len(d) >= min_digits


def _premium_first_passing_decimal_display_in_tail(tail: str) -> Optional[str]:
    """
    从左到右依次尝试两位小数金额；**整数部（去逗号）须至少** ``_PREMIUM_MIN_INTEGER_DIGITS`` **位**，
    否则跳过继续；皆不满足则 None（用于排除 27.00、88.00 等小项）。
    """
    for int_raw, frac in _iter_decimal_amount_parts_in_tail(tail):
        if not _premium_integer_part_meets_min_digits(int_raw):
            continue
        disp = _premium_decimal_parts_display(int_raw, frac)
        if disp is not None:
            return disp
    return None


def _premium_decimal_parts_display(int_raw: str, frac: str) -> Optional[str]:
    digits = int_raw.replace(",", "")
    if not digits.isdigit():
        return None
    try:
        val = Decimal(f"{int(digits)}.{frac}")
    except (InvalidOperation, ValueError):
        return None
    if val == 0:
        return "0"
    return f"{int(digits)}.{frac}"


def _pass1_premium_total_from_block(block: str) -> Optional[str]:
    """
    在同一 text block（pypdf / pymupdf 双路共用）内：
    1. 块须含「保险费合计」（仅占位判定，不因关键字位置截断文本）。
    2. **从整块文本开头**从左到右匹配两位小数；**整数部至少**
       ``_PREMIUM_MIN_INTEGER_DIGITS`` **位数字**否则跳过，找下一处；全无则未命中（不要求「元」）。
    """
    if _PREMIUM_KEY_LITERAL not in block:
        return None
    return _premium_first_passing_decimal_display_in_tail(block)


def _pass2_premium_neighbor_amount_from_word_items(
    all_items: Sequence[PymupdfWordRectItem],
    *,
    y_tolerance: Optional[float] = None,
    rel_left_eps: Optional[float] = None,
    engine_label_for_log: str = "pymupdf",
) -> Optional[str]:
    """
    bbox pass2 纯逻辑：已由 PyMuPDF 得到 word 级 ``get_text("words")`` 列表（文档遍历顺序）。
    取第一份含「保险费合计」的 word 作参照；同页纵向条带内**其它**word，且与参照 bbox 左边线有足够
    水平关联者，按 (y,x) 逐 word 做两位小数从左到右扫描，且**整数部至少四位**，
    第一个满足的金额即命中。
    """
    key_words = [it for it in all_items if _PREMIUM_KEY_LITERAL in it[6]]
    if not key_words:
        logger.debug("[%s] 保险费合计 bbox pass2：未找到关键字word", engine_label_for_log)
        return None

    y_tol = y_tolerance if y_tolerance is not None else _PREMIUM_BBOX_Y_TOLERANCE_PTS
    lx_eps = rel_left_eps if rel_left_eps is not None else _PREMIUM_BBOX_REL_X0_EPS_PT

    ref_page, _ref_wi, ref_x0, ref_y0, ref_x1, ref_y1, ref_text, _ref_bn, _ref_ln, _ref_wn = key_words[0]
    logger.debug(
        "[%s] 保险费合计 bbox pass2：参照word page=%s x0=%.2f y0=%.2f y1=%.2f text=%r",
        engine_label_for_log,
        ref_page,
        ref_x0,
        ref_y0,
        ref_y1,
        ref_text[:160],
    )

    candidates: List[Tuple[float, float, int, str]] = []
    for page_index, wi, x0, y0, x1, y1, text, _bn, _ln, _wn in all_items:
        if page_index != ref_page:
            continue
        if _rects_close(x0, y0, x1, y1, ref_x0, ref_y0, ref_x1, ref_y1):
            continue
        if y0 < ref_y0 - y_tol or y1 > ref_y1 + y_tol:
            continue
        if x1 < ref_x0 - lx_eps:
            continue
        candidates.append((y0, x0, wi, text))

    candidates.sort(key=lambda t: (t[0], t[1], t[2]))
    for _, _, _, text in candidates:
        display = _premium_first_passing_decimal_display_in_tail(text)
        if display is not None:
            logger.debug(
                "[%s] 保险费合计 bbox pass2 命中：%s",
                engine_label_for_log,
                display,
            )
            return display

    logger.debug(
        "[%s] 保险费合计 bbox pass2：条带内其它块未匹配到金额",
        engine_label_for_log,
    )
    return None


def _pass2_premium_neighbor_amount_from_block_items(
    all_items: Sequence[PymupdfWordRectItem],
    *,
    y_tolerance: Optional[float] = None,
    rel_left_eps: Optional[float] = None,
    engine_label_for_log: str = "pymupdf",
) -> Optional[str]:
    return _pass2_premium_neighbor_amount_from_word_items(
        all_items,
        y_tolerance=y_tolerance,
        rel_left_eps=rel_left_eps,
        engine_label_for_log=engine_label_for_log,
    )


def _pass2_premium_total_with_bbox_fallback(
    pdf_bytes: Optional[bytes] = None,
    *,
    engine_label: str = "pymupdf",
    word_rect_items: Optional[Sequence[PymupdfWordRectItem]] = None,
) -> Optional[str]:
    """
    PyMuPDF 打开 PDF / ``get_text("words")``，再交由
    `_pass2_premium_neighbor_amount_from_word_items`。

    ``engine_label`` 仅用于调试日志前缀（双路同源几何逻辑）。
    """
    if word_rect_items is None:
        if pdf_bytes is None:
            return None
        try:
            all_words = iter_pymupdf_word_rect_items(pdf_bytes)
        except ImportError:
            logger.debug("PyMuPDF未安装，无法使用保险费合计 bbox pass2")
            return None
    else:
        all_words = list(word_rect_items)

    return _pass2_premium_neighbor_amount_from_word_items(
        all_words,
        engine_label_for_log=engine_label,
    )


def run_car_insurance_premium_total_passes(
    blocks: Sequence[str],
    *,
    engine_label: str,
    pdf_bytes: Optional[bytes] = None,
    word_rect_items: Optional[Sequence[PymupdfWordRectItem]] = None,
) -> Dict[str, Any]:
    state = PREMIUM_TOTAL_EXTRACTION_STATE
    pass1_hit: Optional[str] = None
    pass1_block_idx: Optional[int] = None
    for bi, block in enumerate(blocks):
        v = _pass1_premium_total_from_block(block)
        if v is not None:
            pass1_hit = v
            pass1_block_idx = bi
            _log_pass(engine_label, state, 1, "命中：保险费合计=%r block_index=%s", v, bi)
            break

    pass2_hit: Optional[str] = None
    if pass1_hit is None and (pdf_bytes is not None or word_rect_items is not None):
        pass2_hit = _pass2_premium_total_with_bbox_fallback(
            pdf_bytes,
            engine_label=engine_label,
            word_rect_items=word_rect_items,
        )
        if pass2_hit is not None:
            _log_pass(engine_label, state, 2, "bbox 邻块命中：保险费合计=%r", pass2_hit)

    display = pass1_hit or pass2_hit
    return {
        "pass1_hit": pass1_hit,
        "pass1_block_index": pass1_block_idx,
        "pass2_hit": pass2_hit,
        "保险费合计": display,
    }


# ---------------------------------------------------------------------------
# 保险费明细抽取（按保司分支；平安 / 人保 / 人寿 / 泰康 / 太平洋 / 阳光；pypdf + PyMuPDF 双路块）
# ---------------------------------------------------------------------------

CommercialDetailTableLayout = Literal[
    "PING_AN",
    "PICC_P",
    "CHINA_LIFE_P",
    "TAIKANG_ONLINE",
    "PACIFIC_P",
    "YANG_GUANG_P",
]


# 商业险明细：承保险种行锚点 →（保额输出键、保费输出键）；各保司共用。
_COMMERCIAL_DETAIL_LABEL_TO_COVERAGE_PREMIUM_KV: Dict[str, Tuple[str, str]] = {
    "新能源汽车损失保险": ("新能源汽车损失保险保额", "新能源汽车损失保险保费"),
    "新能源汽车第三者责任保险": (
        "新能源汽车第三者责任保险保额",
        "新能源汽车第三者责任保险保费",
    ),
    "车上人员责任险(司机)": (
        "新能源汽车车上人员责任保险（司机）保额",
        "新能源汽车车上人员责任保险（司机）保费",
    ),
    "车上人员责任险(驾驶员)": (
        "新能源汽车车上人员责任保险（司机）保额",
        "新能源汽车车上人员责任保险（司机）保费",
    ),
    "车上人员责任险(乘客)": (
        "新能源汽车车上人员责任保险(乘客)保额",
        "新能源汽车车上人员责任保险(乘客)保费",
    ),
    "车上人员责任保险(司机)": (
        "新能源汽车车上人员责任保险（司机）保额",
        "新能源汽车车上人员责任保险（司机）保费",
    ),
    "车上人员责任保险(乘客)": (
        "新能源汽车车上人员责任保险(乘客)保额",
        "新能源汽车车上人员责任保险(乘客)保费",
    ),
    "车上人员责任保险(驾驶员)": (
        "新能源汽车车上人员责任保险（司机）保额",
        "新能源汽车车上人员责任保险（司机）保费",
    ),
    "车上人员责任保险(驾驶人)": (
        "新能源汽车车上人员责任保险（司机）保额",
        "新能源汽车车上人员责任保险（司机）保费",
    ),
    "新能源汽车车上人员责任保险（司机）": (
        "新能源汽车车上人员责任保险（司机）保额",
        "新能源汽车车上人员责任保险（司机）保费",
    ),
    "新能源汽车车上人员责任保险（乘客）": (
        "新能源汽车车上人员责任保险(乘客)保额",
        "新能源汽车车上人员责任保险(乘客)保费",
    ),
    "新能源汽车车上人员责任保险(驾驶人)": (
        "新能源汽车车上人员责任保险（司机）保额",
        "新能源汽车车上人员责任保险（司机）保费",
    ),
    "新能源汽车车上人员责任保险(乘客)": (
        "新能源汽车车上人员责任保险(乘客)保额",
        "新能源汽车车上人员责任保险(乘客)保费",
    ),
    "新能源汽车车上人员责任保险 (司机)": (
        "新能源汽车车上人员责任保险（司机）保额",
        "新能源汽车车上人员责任保险（司机）保费",
    ),
    "新能源汽车车上人员责任保险 (乘客)": (
        "新能源汽车车上人员责任保险(乘客)保额",
        "新能源汽车车上人员责任保险(乘客)保费",
    ),
    "新能源汽车车上人员责任保险 (驾驶人)": (
        "新能源汽车车上人员责任保险（司机）保额",
        "新能源汽车车上人员责任保险（司机）保费",
    ),
    "新能源汽车车上人员责任保险 （司机）": (
        "新能源汽车车上人员责任保险（司机）保额",
        "新能源汽车车上人员责任保险（司机）保费",
    ),
    "新能源汽车车上人员责任保险 （乘客）": (
        "新能源汽车车上人员责任保险(乘客)保额",
        "新能源汽车车上人员责任保险(乘客)保费",
    ),
}

# 乘客行：各版式锚点（含「责任险/责任保险」、泰康长标题）
_COMMERCIAL_PASSENGER_ROW_LABELS: frozenset[str] = frozenset(
    {
        "车上人员责任险(乘客)",
        "车上人员责任保险(乘客)",
        "新能源汽车车上人员责任保险（乘客）",
        "新能源汽车车上人员责任保险(乘客)",
        "新能源汽车车上人员责任保险 (乘客)",
        "新能源汽车车上人员责任保险 （乘客）",
    }
)


# 商业险明细表：全保司共用一套行锚（KV 键 ∪ 乘客行锚 ∪ 免赔额行 ∪ 泰康「责任保险 ␠（司机）」全角括弧写法）。
# 按长度降序遍历，避免短锚在长产品名子串上误命中。
_COMMERCIAL_DETAIL_ROW_LABELS: Tuple[str, ...] = tuple(
    sorted(
        frozenset(_COMMERCIAL_DETAIL_LABEL_TO_COVERAGE_PREMIUM_KV)
        | frozenset(_COMMERCIAL_PASSENGER_ROW_LABELS)
        | frozenset(("车损险每次事故绝对免赔额",)),
        key=len,
        reverse=True,
    )
)


def _commercial_detail_row_labels(detail_layout: CommercialDetailTableLayout) -> Tuple[str, ...]:
    """
    返回全保司合并后的险种行锚点；``detail_layout`` 仅保留 API 兼容，不参与筛选。
    命中任一锚点即解析该行（与 ``run_car_insurance_commercial_detail_table_passes`` 内逐锚点扫描一致）。
    """
    return _COMMERCIAL_DETAIL_ROW_LABELS


def _ascii_parens(s: str) -> str:
    """全角括号 → 半角，便于与锚点字面量互找。"""
    return s.replace("\uFF08", "(").replace("\uFF09", ")")


def _commercial_ws_collapsed_with_map(s: str) -> Tuple[str, List[int]]:
    """
    将连续空白压成单个空格，并记录折叠后每个字符对应的 **原串下标**（用于把匹配位置映回 ``block`` 切片）。
    前导空白直接跳过。
    """
    out: List[str] = []
    maps: List[int] = []
    i = 0
    n = len(s)
    while i < n and s[i].isspace():
        i += 1
    while i < n:
        if s[i].isspace():
            ws0 = i
            while i < n and s[i].isspace():
                i += 1
            if not out:
                continue
            if out[-1] == " ":
                continue
            out.append(" ")
            maps.append(ws0)
            continue
        out.append(s[i])
        maps.append(i)
        i += 1
    return "".join(out), maps


def _commercial_find_substring_span(s: str, needle: str) -> Optional[Tuple[int, int]]:
    """
    在 ``s`` 中定位 ``needle``：先试全角括号统一后的字面量；再试 **空白折叠** 后匹配
    （解决「责任保险」与「(司机)」之间换行导致子串不连续的问题）。
    返回 ``(start, end_exclusive)`` 基于 **原串** ``s`` 的下标；未找到返回 None。
    """
    if not s or not needle:
        return None
    sn = _ascii_parens(s)
    nd = _ascii_parens(needle)
    p = sn.find(nd)
    if p >= 0:
        return p, p + len(nd)
    c_s, m_s = _commercial_ws_collapsed_with_map(sn)
    c_n = re.sub(r"\s+", " ", nd).strip()
    if not c_n:
        return None
    j = c_s.find(c_n)
    if j < 0:
        return None
    e = j + len(c_n)
    return m_s[j], m_s[e - 1] + 1


def _commercial_find_label_start(block: str, label: str, *, use_ws_fold: bool = False) -> int:
    """
    在 ``block`` 中定位 ``label`` 起点；未找到返回 -1。
    ``use_ws_fold=True``（太平洋等）时允许全角括号统一后 **再折叠空白** 匹配，以应对换行拆开的产品名。
    """
    if not use_ws_fold:
        return _ascii_parens(block).find(_ascii_parens(label))
    sp = _commercial_find_substring_span(block, label)
    return sp[0] if sp is not None else -1


def _commercial_parse_money_amount(text: str) -> Optional[str]:
    """
    商业险明细阶段统一金额抽取：支持千分位、可选小数、可选「元」后缀；亦可为纯数字格。
    排除孤立费率串（如 ``-32.5%``）。
    """
    if text is None:
        return None
    t = str(text).strip()
    if not t or t in {"-", "—", "－", "/", "／"}:
        return None
    if "%" in t and "元" not in t:
        t_compact = re.sub(r"\s+", "", t)
        if re.fullmatch(r"-?[\d.,]+\s*%?", t_compact):
            return None
    # 1) 含「元」的金额（串内首处）
    m = re.search(
        rf"{_COMMERCIAL_MONEY_INT_SEARCH_PREFIX}({_COMMERCIAL_MONEY_INT_RE}){_COMMERCIAL_MONEY_OPT_FRAC_CAPTURE}\s*元",
        t,
    )
    if m:
        return _commercial_money_from_int_frac(m.group(1), m.group(2))
    # 2) 整格为数字（无「元」）
    t2 = t.replace(" ", "")
    m2 = re.fullmatch(
        rf"({_COMMERCIAL_MONEY_INT_RE}){_COMMERCIAL_MONEY_OPT_FRAC_CAPTURE}",
        t2,
    )
    if m2:
        return _commercial_money_from_int_frac(m2.group(1), m2.group(2))
    # 3) 串内首个独立金额（如列内夹杂文字）
    m3 = re.search(
        rf"{_COMMERCIAL_MONEY_INT_SEARCH_PREFIX}({_COMMERCIAL_MONEY_INT_RE}){_COMMERCIAL_MONEY_OPT_FRAC_CAPTURE}(?![\d%])",
        t,
    )
    if m3:
        return _commercial_money_from_int_frac(m3.group(1), m3.group(2))
    return None


def _commercial_money_from_int_frac(int_raw: str, frac: Optional[str]) -> Optional[str]:
    digits = int_raw.replace(",", "")
    if not digits.isdigit():
        return None
    try:
        if frac:
            return _ping_an_norm_two_dec(f"{digits}.{frac}")
        return _ping_an_norm_two_dec(digits)
    except (InvalidOperation, ValueError):
        return None


def _commercial_trim_label_sequence(row_labels: Sequence[str]) -> Tuple[str, ...]:
    """去重后按长度降序，供行间截断时优先匹配较长锚点。"""
    return tuple(sorted(set(row_labels), key=len, reverse=True))


def _ping_an_norm_two_dec(num_str: str) -> str:
    d = Decimal(str(num_str)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if d == 0:
        return "0"
    return format(d, "f")


def _ping_an_parse_coverage_yuan(s: str) -> Optional[str]:
    """保额列金额（与保费、免赔额共用统一金钱 pattern）。"""
    return _commercial_parse_money_amount(s)


def _ping_an_parse_premium_cell(s: str) -> Optional[str]:
    """保费列金额（与保额、免赔额共用统一金钱 pattern）。"""
    return _commercial_parse_money_amount(s)


def _commercial_parse_absolute_deductible_amount(text: str) -> Optional[str]:
    if text is None:
        return None
    m = re.search(
        rf"绝对\s*免赔\s*额\s*[:：]?\s*({_COMMERCIAL_MONEY_INT_RE})"
        rf"{_COMMERCIAL_MONEY_OPT_FRAC_CAPTURE}\s*元",
        str(text),
    )
    if not m:
        return None
    return _commercial_money_from_int_frac(m.group(1), m.group(2))


def _ping_an_ws_tokens(s: str) -> List[str]:
    s = s.strip()
    if not s:
        return []
    return [t for t in re.split(r"\s+", s) if t]


def _ping_an_trim_tail_before_other_labels(
    tail: str,
    current_label: str,
    row_labels: Sequence[str],
    *,
    use_ws_fold_labels: bool = False,
) -> str:
    best = len(tail)
    cur = _ascii_parens(current_label)
    cur_cmp = re.sub(r"\s+", " ", cur).strip()
    for lab in row_labels:
        ln = _ascii_parens(lab)
        if use_ws_fold_labels:
            if re.sub(r"\s+", " ", ln).strip() == cur_cmp:
                continue
        elif ln == cur:
            continue
        if use_ws_fold_labels:
            sp = _commercial_find_substring_span(tail, lab)
            if sp is not None and sp[0] > 0 and sp[0] < best:
                best = sp[0]
        else:
            tn = _ascii_parens(tail)
            p = tn.find(ln)
            if p != -1 and p > 0 and p < best:
                best = p
    return tail[:best].strip()


def _ping_an_row_tokens_after_label(
    block: str,
    label: str,
    row_labels: Sequence[str],
    *,
    use_ws_fold: bool = False,
) -> Optional[List[str]]:
    if use_ws_fold:
        sp = _commercial_find_substring_span(block, label)
        if sp is None:
            return None
        tail = block[sp[1] :]
    else:
        sn = _ascii_parens(block)
        nd = _ascii_parens(label)
        i = sn.find(nd)
        if i == -1:
            return None
        tail = block[i + len(label) :]
    tail = _ping_an_trim_tail_before_other_labels(
        tail, label, row_labels, use_ws_fold_labels=use_ws_fold
    )
    toks = _ping_an_ws_tokens(tail)
    return toks


def _ping_an_parse_standard_coverage_premium(tokens: Sequence[str]) -> Tuple[Optional[str], Optional[str]]:
    """平安：label 后第 1 项保额（须含「元」）；再往后第 3 项为保费。"""
    if len(tokens) < 4:
        return None, None
    cov = _ping_an_parse_coverage_yuan(tokens[0])
    if cov is None:
        return None, None
    prem = _ping_an_parse_premium_cell(tokens[3])
    return cov, prem


# 乘客保额展示：「座」在前或「元」在前；运算符为 x / × / *；金额可为元/万元，可带「/座」；各段间允许空格。
_PASSENGER_COV_OP = r"(?:x|×|\*)"
# 乘客保额串内金额段：与 ``_COMMERCIAL_MONEY_INT_RE`` 同一千分位规则，并带可选小数
_PASSENGER_COVERAGE_AMOUNT = (
    rf"{_COMMERCIAL_MONEY_INT_SEARCH_PREFIX}{_COMMERCIAL_MONEY_INT_RE}(?:\.\d+)?"
)
_PASSENGER_COVERAGE_DISPLAY_RES: Tuple[re.Pattern, ...] = (
    # X元 x N座、X万元/座 x N座 等（元与座之间有运算符，避免与「X元 4座」混淆）
    re.compile(
        rf"{_PASSENGER_COVERAGE_AMOUNT}\s*(?:万元|元)(?:\s*/\s*座)?\s*{_PASSENGER_COV_OP}\s*\d+\s*座",
        re.I,
    ),
    # X元 4座、X万元 4座、X元/座 4座、X万元/座 4座；「/ 座」可拆开
    re.compile(
        rf"{_PASSENGER_COVERAGE_AMOUNT}\s*(?:万元|元)(?:\s*/\s*座)?\s+\d+\s*座",
        re.I,
    ),
    # 4座 x X…、4座 * X…、4座 × X…（座在前；X 可含「万元/座」；也允许「/ 座」被空白拆开）
    re.compile(
        rf"\d+\s*座\s*{_PASSENGER_COV_OP}\s*\S+(?:\s*/\s*\S+)?",
        re.I,
    ),
)


def _passenger_last_token_index_through_char(
    joined: str,
    char_end: int,
    sub: Sequence[str],
) -> int:
    """``joined`` 与 ``" ".join(sub)`` 一致；返回覆盖 ``[0, char_end)`` 的最后一个 token 下标。"""
    if char_end <= 0:
        return 0
    pos = 0
    for i, t in enumerate(sub):
        if i > 0:
            pos += 1
        pos += len(t)
        if pos >= char_end:
            return i
    return max(0, len(sub) - 1)


def _passenger_coverage_from_tokens(
    tokens: Sequence[str],
    *,
    max_tokens: int = 12,
) -> Tuple[Optional[str], int]:
    """
    在若干 token 拼接串上匹配乘客保额展示；返回 (保额原文, 保额占用到的末 token 下标)。
    含 ``X元 x N座``、``X元 N座``、``X万元 N座``、``X元/座 N座``、``X万元/座 N座``、``N座 x …`` 等。
    未匹配返回 (None, -1)。
    """
    n = min(max_tokens, len(tokens))
    if n < 2:
        return None, -1
    sub = list(tokens[:n])
    joined = " ".join(sub)
    for pat in _PASSENGER_COVERAGE_DISPLAY_RES:
        m = pat.search(joined)
        if not m:
            continue
        disp = m.group(0).strip()
        if not disp:
            continue
        last = _passenger_last_token_index_through_char(joined, m.end(), sub)
        return disp, last
    return None, -1


def _picc_parse_standard_coverage_premium(tokens: Sequence[str]) -> Tuple[Optional[str], Optional[str]]:
    """
    中国人民财产（PICC）承保险种行：金额 pattern 与其它保司一致；**列序**指
    ``_ping_an_row_tokens_after_label`` 得到的 ``tokens``（label 已从块首截掉，不在本列表内）：

    - **label**：整块中第 1 列（锚点），已消费。
    - **label 后第 1、2 项**：``tokens[0]``、``tokens[1]``，占位（说明/费率/浮动等）。
    - **label 后第 3 项**：``tokens[2]``，保额。
    - **保费**：在 **label 后第 4 或第 5 项**，即 ``tokens[3]`` 或 ``tokens[4]``；**先解析第 4 项**，
      无有效金额 **再解析第 5 项**。

    电子保单常见 **三列紧凑**（仅 3 个 token）：占位 + 保额 + 保费，即 ``tokens[0]`` 非金额、
    ``tokens[1]`` 保额、``tokens[2]`` 保费。
    若按上列序仍得不到保额+保费，再回退为「从左到右前两处可解析金额」以兼容怪版式。
    """
    if not tokens:
        return None, None
    n = len(tokens)
    # 第 3 项 = 下标 2 保额；保费 = 先试第 4 项（下标 3），没有再试第 5 项（下标 4）
    if n >= 4:
        cov = _commercial_parse_money_amount(tokens[2])
        if cov is not None:
            prem = _commercial_parse_money_amount(tokens[3])
            if prem is None and n > 4:
                prem = _commercial_parse_money_amount(tokens[4])
            if prem is not None:
                return cov, prem
    # 三列：单列占位（常为 ``/``、``-``）+ 保额 + 保费
    if n == 3:
        lead = _commercial_parse_money_amount(tokens[0])
        cov = _commercial_parse_money_amount(tokens[1])
        prem = _commercial_parse_money_amount(tokens[2])
        if lead is None and cov is not None and prem is not None:
            return cov, prem
    # 回退：串内前两笔可解析金额
    amounts: List[str] = []
    for t in tokens:
        v = _commercial_parse_money_amount(t)
        if v is not None:
            amounts.append(v)
            if len(amounts) >= 2:
                return amounts[0], amounts[1]
    return None, None


def _taikang_parse_standard_coverage_premium(tokens: Sequence[str]) -> Tuple[Optional[str], Optional[str]]:
    """
    泰康在线：label 后第 2 列（下标 1）为保额、第 4 列（下标 3）为保费。
    电子保单常见为「绝对免赔率 / 保额数字 / '-' / 保费数字」，保额列可能无「元」后缀。
    """
    if len(tokens) < 4:
        return None, None
    cov = _commercial_parse_money_amount(tokens[1])
    prem = _commercial_parse_money_amount(tokens[3])
    if cov is None or prem is None:
        return None, None
    return cov, prem


def _pacific_parse_standard_coverage_premium(tokens: Sequence[str]) -> Tuple[Optional[str], Optional[str]]:
    """太平洋财产：label 后第 1 项（下标 0）保额；第 2 项（下标 1）保费。"""
    if len(tokens) < 2:
        return None, None
    cov = _ping_an_parse_coverage_yuan(tokens[0])
    if cov is None:
        return None, None
    prem = _ping_an_parse_premium_cell(tokens[1])
    return cov, prem


def _commercial_canonical_passenger_coverage_display(disp: str) -> str:
    """
    将乘客保额展示统一为「N座*金额元」形式（整数去尾 .00）：
    太平洋常见「200000元×4座」；人寿等常见「100,000.00元/座 *4座」。
    """
    s = re.sub(r"\s+", "", disp.strip())
    op = _PASSENGER_COV_OP
    amt = r"\d[\d,]*(?:\.\d+)?"
    m_per_seat = re.match(rf"^({amt})元/座{op}(\d+)座$", s, re.I)
    if m_per_seat:
        raw_amt, seats = m_per_seat.group(1), m_per_seat.group(2)
        try:
            d = Decimal(raw_amt.replace(",", ""))
        except InvalidOperation:
            return disp.strip()
        if d == d.to_integral():
            amt_s = str(int(d))
        else:
            amt_s = format(d.quantize(Decimal("0.01")), "f").rstrip("0").rstrip(".")
        return f"{seats}座*{amt_s}元"
    m_yuan_first = re.match(rf"^({amt})(万元|元){op}(\d+)座$", s, re.I)
    if m_yuan_first:
        raw_amt, unit, seats = m_yuan_first.group(1), m_yuan_first.group(2), m_yuan_first.group(3)
        if unit == "万元":
            return disp.strip()
        try:
            d = Decimal(raw_amt.replace(",", ""))
        except InvalidOperation:
            return disp.strip()
        if d == d.to_integral():
            amt_s = str(int(d))
        else:
            amt_s = format(d.quantize(Decimal("0.01")), "f").rstrip("0").rstrip(".")
        return f"{seats}座*{amt_s}元"
    m_seats_first = re.match(rf"^(\d+)座{op}({amt})元$", s, re.I)
    if m_seats_first:
        seats, raw_amt = m_seats_first.group(1), m_seats_first.group(2)
        try:
            d = Decimal(raw_amt.replace(",", ""))
        except InvalidOperation:
            return disp.strip()
        if d == d.to_integral():
            amt_s = str(int(d))
        else:
            amt_s = format(d.quantize(Decimal("0.01")), "f").rstrip("0").rstrip(".")
        return f"{seats}座*{amt_s}元"
    return disp.strip()


def _yangguang_parse_standard_row(
    label: str,
    tokens: Sequence[str],
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    阳光财产：label 后第 1 项保额、第 3 项保费。
    「新能源汽车损失保险」行：第 2 项为免赔额（写入 ``免赔额``，与车损免赔行二选一，阳光版式下跳过车损免赔行）。
    """
    if len(tokens) < 3:
        return None, None, None
    cov = _ping_an_parse_coverage_yuan(tokens[0])
    prem = _ping_an_parse_premium_cell(tokens[2])
    ded: Optional[str] = None
    if label == "新能源汽车损失保险" and len(tokens) > 1:
        ded = _ping_an_parse_deductible([tokens[1]])
    return cov, prem, ded


def _ping_an_refine_passenger_parse(
    tokens: Sequence[str],
    *,
    layout: CommercialDetailTableLayout = "PING_AN",
) -> Tuple[Optional[str], Optional[str]]:
    """
    乘客保额：支持
    ``4座 x X…``、``X元 x N座``、``X元 N座``、``X万元 N座``、``X元/座 N座``、``X万元/座 N座`` 等
    （``x``/``×``/``*``；金额与 ``/座``、与 ``N座`` 之间可有空格）。
    平安：保额末 token +1 或 +3 取保费；人保 / 人寿：+1 或 +2。
    """
    if len(tokens) < 2:
        return None, None
    n = len(tokens)
    prem_offsets = (1, 3) if layout == "PING_AN" else (1, 2)

    cov_display, last_cov = _passenger_coverage_from_tokens(tokens)
    if cov_display is not None and last_cov >= 0:
        for off in prem_offsets:
            pi = last_cov + off
            if pi < n:
                prem = _ping_an_parse_premium_cell(tokens[pi])
                if prem:
                    return cov_display, prem
        return cov_display, None

    for end in (0, 1, 2):
        if end + 1 >= n:
            break
        chunk = f"{tokens[end]} {tokens[end + 1]}".strip()
        if re.match(r"^\d+\s*座\s*$", tokens[end]) and re.match(
            rf"^{_PASSENGER_COV_OP}\S+$",
            tokens[end + 1],
            re.I,
        ):
            cov_display = f"{tokens[end].strip()} {tokens[end + 1].strip()}"
            last_cov = end + 1
            for off in prem_offsets:
                pi = last_cov + off
                if pi < n:
                    prem = _ping_an_parse_premium_cell(tokens[pi])
                    if prem:
                        return cov_display, prem
            return cov_display, None
        if re.fullmatch(rf"\d+\s*座\s*{_PASSENGER_COV_OP}\s*\S+", chunk, re.I):
            cov_display = chunk.strip()
            last_cov = end + 1
            for off in prem_offsets:
                pi = last_cov + off
                if pi < n:
                    prem = _ping_an_parse_premium_cell(tokens[pi])
                    if prem:
                        return cov_display, prem
            return cov_display, None
    return None, None


def _ping_an_parse_deductible(tokens: Sequence[str]) -> Optional[str]:
    if not tokens:
        return None
    phrase = _commercial_parse_absolute_deductible_amount(" ".join(tokens))
    if phrase is not None:
        return phrase
    return _commercial_parse_money_amount(tokens[0])


_COMMERCIAL_DAMAGE_DEDUCTIBLE_FIELD_RE = re.compile(
    rf"(?:车\s*损\s*险|车辆\s*损失\s*险)\s*(?:每\s*次\s*事故|的)?\s*绝对\s*免赔\s*额"
    rf"\s*[:：]?\s*(?:￥|¥|RMB)?\s*(?:（|\()?"
    rf"\s*({_COMMERCIAL_MONEY_INT_RE})?{_COMMERCIAL_MONEY_OPT_FRAC_CAPTURE}\s*(?:（|\()?\s*元\s*(?:）|\))?",
    re.I,
)
_COMMERCIAL_DAMAGE_DEDUCTIBLE_LABEL_RE = re.compile(
    r"(?:车\s*损\s*险|车辆\s*损失\s*险)\s*(?:每\s*次\s*事故|的)?\s*绝对\s*免赔\s*额",
    re.I,
)
_COMMERCIAL_DAMAGE_DEDUCTIBLE_WORD_Y_PAD_PT: Final[float] = 4.0
_COMMERCIAL_DAMAGE_DEDUCTIBLE_WORD_X_RIGHT_PT: Final[float] = 180.0


def _pass1_damage_deductible_from_blocks(
    blocks: Sequence[str],
    *,
    engine_label: str,
) -> Optional[str]:
    for bi, block in enumerate(blocks):
        m = _COMMERCIAL_DAMAGE_DEDUCTIBLE_FIELD_RE.search(block or "")
        if not m:
            continue
        raw_int = m.group(1)
        if raw_int:
            v = _commercial_money_from_int_frac(raw_int, m.group(2))
            if v is not None:
                _log_pass(
                    engine_label,
                    PREMIUM_DETAIL_EXTRACTION_STATE,
                    1,
                    "明细 车损免赔额=%r block_index=%s",
                    v,
                    bi,
                )
                return v
        return None
    return None


def _pass2_damage_deductible_from_word_items(
    word_items: Sequence[Tuple[int, int, float, float, float, float, str, int, int, int]],
    *,
    engine_label: str,
) -> Optional[str]:
    for wi, (page, _word_idx, x0, y0, x1, y1, text, *_meta) in enumerate(word_items):
        if not _COMMERCIAL_DAMAGE_DEDUCTIBLE_LABEL_RE.search(text or ""):
            continue
        ref_cy = (y0 + y1) / 2.0
        row_words: List[Tuple[float, str]] = []
        for page2, _word_idx2, wx0, wy0, wx1, wy1, wtext, *_meta2 in word_items:
            if page2 != page:
                continue
            w_cy = (wy0 + wy1) / 2.0
            if abs(w_cy - ref_cy) > _COMMERCIAL_DAMAGE_DEDUCTIBLE_WORD_Y_PAD_PT:
                continue
            if wx1 < x0 - _PREMIUM_BBOX_REL_X0_EPS_PT:
                continue
            if wx0 > x1 + _COMMERCIAL_DAMAGE_DEDUCTIBLE_WORD_X_RIGHT_PT:
                continue
            if wtext:
                row_words.append((wx0, str(wtext)))
        row_words.sort(key=lambda t: t[0])
        row_text = " ".join(t for _x, t in row_words)
        m = _COMMERCIAL_DAMAGE_DEDUCTIBLE_FIELD_RE.search(row_text)
        if not m or not m.group(1):
            continue
        v = _commercial_money_from_int_frac(m.group(1), m.group(2))
        if v is None:
            continue
        _log_pass(
            engine_label,
            PREMIUM_DETAIL_EXTRACTION_STATE,
            2,
            "明细 车损免赔额词级bbox=%r word_index=%s row_text=%r",
            v,
            wi,
            row_text,
        )
        return v
    return None


def _pass2_damage_deductible_with_bbox_fallback(
    pdf_bytes: Optional[bytes] = None,
    *,
    engine_label: str = "pymupdf",
    word_rect_items: Optional[Sequence[PymupdfWordRectItem]] = None,
) -> Optional[str]:
    if word_rect_items is None:
        if pdf_bytes is None:
            return None
        try:
            word_items = iter_pymupdf_word_rect_items(pdf_bytes)
        except ImportError:
            return None
    else:
        word_items = list(word_rect_items)
    return _pass2_damage_deductible_from_word_items(
        word_items,
        engine_label=engine_label,
    )


def _merge_commercial_detail_table_kv(
    detail_mu: Dict[str, str],
    detail_pdf: Dict[str, str],
) -> Dict[str, str]:
    """双路合并：字段级 pymupdf 优先，空则取 pypdf（与保险费合计等一致）。"""
    return {
        k: (detail_mu.get(k) or detail_pdf.get(k) or "")
        for k in _CAR_INSURANCE_COMMERCIAL_ONLY_KV_KEYS
    }


def _commercial_detail_needs_llm_fallback(
    *,
    is_commercial: bool,
    known_company: Optional[KnownInsuranceCompany],
    has_rule_layout: bool,
    kv: Dict[str, Any],
) -> bool:
    if not is_commercial:
        return False
    if known_company is None:
        return True
    if not has_rule_layout:
        return True
    return any(not (kv.get(k) or "") for k in _CAR_INSURANCE_COMMERCIAL_ONLY_KV_KEYS)


def run_car_insurance_commercial_detail_table_passes(
    blocks: Sequence[str],
    *,
    engine_label: str,
    detail_layout: CommercialDetailTableLayout,
) -> Dict[str, str]:
    """
    商业险明细表（单路）：在 ``blocks`` 上跑一遍；``engine_label`` 为 ``pymupdf`` / ``pypdf`` 供日志区分。
    行锚点为全保司合并集合（见 ``_COMMERCIAL_DETAIL_ROW_LABELS``）；``detail_layout`` 仅影响列解析与日志标签。
    """
    state = PREMIUM_DETAIL_EXTRACTION_STATE
    out: Dict[str, str] = {k: "" for k in _CAR_INSURANCE_COMMERCIAL_ONLY_KV_KEYS}
    if detail_layout == "PING_AN":
        layout_tag = "平安"
    elif detail_layout == "PICC_P":
        layout_tag = "人保财险"
    elif detail_layout == "CHINA_LIFE_P":
        layout_tag = "人寿财险"
    elif detail_layout == "TAIKANG_ONLINE":
        layout_tag = "泰康在线"
    elif detail_layout == "PACIFIC_P":
        layout_tag = "太平洋财险"
    elif detail_layout == "YANG_GUANG_P":
        layout_tag = "阳光财险"
    else:
        layout_tag = "明细"

    row_labels = _commercial_detail_row_labels(detail_layout)
    row_labels_trim = _commercial_trim_label_sequence(row_labels)
    use_ws_fold = True

    for label in row_labels:
        for bi, block in enumerate(blocks):
            if _commercial_find_label_start(block, label, use_ws_fold=use_ws_fold) < 0:
                continue
            toks = _ping_an_row_tokens_after_label(
                block, label, row_labels_trim, use_ws_fold=use_ws_fold
            )
            if not toks:
                continue
            if label == "车损险每次事故绝对免赔额":
                if detail_layout == "YANG_GUANG_P":
                    # 免赔额由「新能源汽车损失保险」行第二列提供
                    break
                dv = _ping_an_parse_deductible(toks)
                if dv:
                    out["免赔额"] = dv
                    _log_pass(
                        engine_label,
                        state,
                        1,
                        "%s明细 免赔额=%r block_index=%s",
                        layout_tag,
                        dv,
                        bi,
                    )
            elif label in _COMMERCIAL_PASSENGER_ROW_LABELS:
                k_cov, k_prem = _COMMERCIAL_DETAIL_LABEL_TO_COVERAGE_PREMIUM_KV[label]
                cov, prem = _ping_an_refine_passenger_parse(toks, layout=detail_layout)
                if cov and detail_layout in ("PACIFIC_P", "CHINA_LIFE_P"):
                    cov = _commercial_canonical_passenger_coverage_display(cov)
                if cov:
                    out[k_cov] = cov
                if prem:
                    out[k_prem] = prem
                if cov or prem:
                    _log_pass(
                        engine_label,
                        state,
                        1,
                        "%s明细 乘客行 cov=%r prem=%r block_index=%s",
                        layout_tag,
                        cov,
                        prem,
                        bi,
                    )
            else:
                k_cov, k_prem = _COMMERCIAL_DETAIL_LABEL_TO_COVERAGE_PREMIUM_KV[label]
                yg_ded: Optional[str] = None
                if detail_layout == "PING_AN":
                    cov, prem = _ping_an_parse_standard_coverage_premium(toks)
                elif detail_layout == "TAIKANG_ONLINE":
                    cov, prem = _taikang_parse_standard_coverage_premium(toks)
                elif detail_layout == "PACIFIC_P":
                    cov, prem = _pacific_parse_standard_coverage_premium(toks)
                elif detail_layout == "YANG_GUANG_P":
                    cov, prem, yg_ded = _yangguang_parse_standard_row(label, toks)
                else:
                    cov, prem = _picc_parse_standard_coverage_premium(toks)
                if label == "新能源汽车损失保险" and not out.get("免赔额"):
                    row_ded = _commercial_parse_absolute_deductible_amount(" ".join(toks))
                    if row_ded is not None:
                        out["免赔额"] = row_ded
                if yg_ded:
                    out["免赔额"] = yg_ded
                if cov:
                    out[k_cov] = cov
                if prem:
                    out[k_prem] = prem
                if cov or prem or yg_ded:
                    _log_pass(
                        engine_label,
                        state,
                        1,
                        "%s明细 标准行 label=%r cov=%r prem=%r ded=%r block_index=%s",
                        layout_tag,
                        label,
                        cov,
                        prem,
                        yg_ded,
                        bi,
                    )
            break

    return out


# 商业险明细 pass2（豆包）：通用 fallback；PyMuPDF block 锚点 + word 纵带上下文按行抽取。
# 费用明细 pass2：锚点 block 的 y0/y1 向上下各扩展此值（pt），与 ``_in_vertical_band`` 筛同页 words。
_COMMERCIAL_DETAIL_LLM_Y_PAD_PT: Final[float] = 6.0
_COMMERCIAL_DETAIL_LLM_CONTEXT_MAX_CHARS: Final[int] = 12000
_COMMERCIAL_DETAIL_LLM_NOT_FOUND: Final[str] = "__NOT_FOUND__"
_COMMERCIAL_DETAIL_WORD_MAX_HEIGHT_PT: Final[float] = 50.0
_COMMERCIAL_DETAIL_MIN_REF_Y_OVERLAP_RATIO: Final[float] = 0.5
# pass2 仅使用 PDF 前两页（0-based 下标 0、1）的 ``blocks``/``words``，避免后续页条款/说明误命中。
_COMMERCIAL_DETAIL_PASS2_MAX_PAGE_INDEX: Final[int] = 1
# 明细 pass2 调豆包前：在锚点 block 同行邻域（纵带 + 水平窗）内用正则预筛；无金额/乘客形态则跳过 HTTP。
# 水平窗：锚块左缘向左、右缘向右各扩展若干 pt（与保险费合计 bbox pass2 思路一致，覆盖整行单元格）。
_COMMERCIAL_PASS2_PREFILTER_Y_PAD_PT: Final[float] = _COMMERCIAL_DETAIL_LLM_Y_PAD_PT
_COMMERCIAL_PASS2_PREFILTER_X_LEFT_PT: Final[float] = 380.0
_COMMERCIAL_PASS2_PREFILTER_X_RIGHT_PT: Final[float] = 520.0
# 车上人员（乘客）行常见片段：与保额展示、座位数相关（免触发纯条款块上的险种名误命中）。
_COMMERCIAL_PASS2_PASSENGER_TEXT_HINT_RE = re.compile(
    r"(?:乘客|元\s*/\s*座|/\s*座|\d+\s*座|[x×*]\s*\d|\d+\s*[x×*]|\d+\s*座\s*[x×*]|万元)",
    re.I,
)
# 正文/脚注也含「承保险种」等字样；命中下列片段的词条不作为表头带入。
_COMMERCIAL_PASS2_TABLE_HEADER_EXCLUDE_SUBSTR: Tuple[str, ...] = (
    "鉴于投保人",
    "保险合同由保险条款",
    "收到本保险单",
    "请详细阅读承保险种对应",
    "拨打报案咨询",
)


@dataclass(frozen=True)
class CommercialPass2Pass2TableConfig:
    """
    商业险明细 pass2 表结构（渤海 / 中银 / 华安共用）：全表 ``header_texts_in_column_order`` 与 ``column_indices`` 一一对应（1-based）。

    - ``policy_issuer_for_llm``：layout 首句「本保单为…承保险种明细」中的承保方简称。
    - ``exclude_substrings``：命中则整词不入选表头附录（与 ``extract_table`` 一致）。
    - ``key_column_index``：行锚险种名所在列。
    - ``detail_column_indices``：须在 instruction「只抽取同一行的…」中列出的列；亦为 ``extract_table`` 与 key 列并集。
    - ``detail_json_field_keys`` / ``detail_role_cn``：与 ``detail_column_indices`` 等长，供内部校验与扩展；layout 文案以列号+表头子串为主。

    ``extract_table`` 仅抽取 ``{key_column_index} ∪ set(detail_column_indices)`` 在全表配对中出现的子串。
    """

    policy_issuer_for_llm: str
    exclude_substrings: Tuple[str, ...]
    header_texts_in_column_order: Tuple[str, ...]
    column_indices: Tuple[int, ...]
    key_column_index: int
    detail_column_indices: Tuple[int, ...]
    detail_json_field_keys: Tuple[str, ...]
    detail_role_cn: Tuple[str, ...]

    def __post_init__(self) -> None:
        if len(self.header_texts_in_column_order) != len(self.column_indices):
            raise ValueError("CommercialPass2Pass2TableConfig：header 与 column_indices 长度须一致")
        n = len(self.detail_column_indices)
        if not (n == len(self.detail_json_field_keys) == len(self.detail_role_cn)):
            raise ValueError("CommercialPass2Pass2TableConfig：detail_* 三个元组须等长")
        cols_full = frozenset(self.column_indices)
        if self.key_column_index not in cols_full:
            raise ValueError("CommercialPass2Pass2TableConfig：key_column_index 须出现在 column_indices 中")
        for c in self.detail_column_indices:
            if c not in cols_full:
                raise ValueError("CommercialPass2Pass2TableConfig：detail 列须出现在全表 column_indices 中")

    def header_text_for_column(self, column_1based: int) -> str:
        for t, c in zip(self.header_texts_in_column_order, self.column_indices):
            if c == column_1based:
                return t
        return ""

    def filtered_header_pairs_for_extract_table(self) -> Tuple[Tuple[str, ...], Tuple[int, ...]]:
        keep = frozenset(self.detail_column_indices) | {self.key_column_index}
        texts: List[str] = []
        cols: List[int] = []
        for t, c in zip(self.header_texts_in_column_order, self.column_indices):
            if c in keep:
                texts.append(t)
                cols.append(c)
        return tuple(texts), tuple(cols)

    def auxiliary_header_texts_not_in_extract(self) -> Tuple[str, ...]:
        """全表中未进入 ``extract_table`` 的表头子串（「仅作列对齐参考」一句）。"""
        keep = frozenset(self.detail_column_indices) | {self.key_column_index}
        return tuple(
            t for t, c in zip(self.header_texts_in_column_order, self.column_indices) if c not in keep
        )


_COMMERCIAL_PASS2_BOHAI_PASS2_TABLE_CONFIG = CommercialPass2Pass2TableConfig(
    policy_issuer_for_llm="渤海财产保险",
    exclude_substrings=_COMMERCIAL_PASS2_TABLE_HEADER_EXCLUDE_SUBSTR,
    header_texts_in_column_order=(
        "承保险种",
        "费率浮动",
        "保险金额",
        "绝对免赔率",
        "保险费(元)",
    ),
    column_indices=(1, 2, 3, 4, 5),
    key_column_index=1,
    detail_column_indices=(3, 5),
    detail_json_field_keys=("coverage", "premium"),
    detail_role_cn=("保额", "保费"),
)

_COMMERCIAL_PASS2_ZHONGYIN_PASS2_TABLE_CONFIG = CommercialPass2Pass2TableConfig(
    policy_issuer_for_llm="中银保险",
    exclude_substrings=_COMMERCIAL_PASS2_TABLE_HEADER_EXCLUDE_SUBSTR,
    header_texts_in_column_order=("承保险种", "保险金额", "绝对免赔率", "保险费（元）"),
    column_indices=(1, 2, 3, 4),
    key_column_index=1,
    detail_column_indices=(2, 4),
    detail_json_field_keys=("coverage", "premium"),
    detail_role_cn=("保额", "保费"),
)

_COMMERCIAL_PASS2_HUAAN_PASS2_TABLE_CONFIG = CommercialPass2Pass2TableConfig(
    policy_issuer_for_llm="华安财产保险",
    exclude_substrings=_COMMERCIAL_PASS2_TABLE_HEADER_EXCLUDE_SUBSTR,
    header_texts_in_column_order=(
        "承保险种",
        "保险金额",
        "保险费（元）",
        "每次事故绝对免赔额",
        "绝对免赔率",
    ),
    column_indices=(1, 2, 3, 4, 5),
    key_column_index=1,
    detail_column_indices=(2, 3, 4),
    detail_json_field_keys=("coverage", "premium", "deductible"),
    detail_role_cn=("保额", "保费", "免赔额"),
)


@dataclass(frozen=True)
class CommercialPass2TableHeaderExtractSpec:
    """
    仅描述 ``extract_table`` 所需参数（表头子串、列索引、排除串、页范围、粒度等），不含任何保司文案。
    接在表头行块前的说明可由 ``common.build_extract_table_words_header_section_preamble`` 从本 dataclass 的
    ``header_texts_in_column_order`` / ``column_indices`` 生成，或由调用方单独传入
    ``commercial_pass2_build_user_prompt_with_extract_table(..., table_header_section_preamble=...)``。
    """

    header_texts_in_column_order: Tuple[str, ...]
    column_indices: Tuple[int, ...]
    exclude_substrings: Tuple[str, ...]
    max_page_index: int
    extract_source: Literal["blocks", "words"] = "words"
    sort_mode: Literal["document_order", "column_rank_then_geom"] = "document_order"
    #: ``words`` 源表头附录行格式；明细 pass2 保司统一用 ``bbox_column_text`` 仅保留横坐标与文本。
    words_line_format: Literal["pymupdf_meta", "bbox_column_text"] = "pymupdf_meta"

    def extract_table_lines(self, pdf_bytes: bytes) -> str:
        return extract_table(
            pdf_bytes,
            header_texts_in_column_order=self.header_texts_in_column_order,
            column_indices=self.column_indices,
            max_page_index=self.max_page_index,
            exclude_substrings=self.exclude_substrings,
            source=self.extract_source,
            sort_mode=self.sort_mode,
            words_line_format=self.words_line_format,
        ).strip()


def _extract_table_header_column_pairs_local(
    header_texts_in_column_order: Sequence[str],
    column_indices: Sequence[int],
) -> List[Tuple[str, int]]:
    return [
        (text, int(column_indices[i]))
        for i, text in enumerate(header_texts_in_column_order)
        if text
    ]


def _table_item_matched_min_column_local(text: str, pairs: Sequence[Tuple[str, int]]) -> int:
    normalized = norm_paren_for_table_header_match(text)
    matched = [
        col
        for header, col in pairs
        if norm_paren_for_table_header_match(header) in normalized
    ]
    return min(matched) if matched else 0


def _extract_table_lines_from_precomputed_items(
    table_header_spec: CommercialPass2TableHeaderExtractSpec,
    *,
    block_rect_items: Sequence[PymupdfBlockRectItem],
    word_rect_items: Sequence[PymupdfWordRectItem],
    max_y0_by_page: Optional[Dict[int, float]] = None,
) -> str:
    pairs = _extract_table_header_column_pairs_local(
        table_header_spec.header_texts_in_column_order,
        table_header_spec.column_indices,
    )
    if not pairs:
        return ""

    def excluded(text: str) -> bool:
        return any(ex in text for ex in table_header_spec.exclude_substrings)

    def matched(text: str) -> bool:
        normalized = norm_paren_for_table_header_match(text)
        return any(norm_paren_for_table_header_match(header) in normalized for header, _col in pairs)

    if table_header_spec.extract_source == "blocks":
        picked_b: List[Tuple[int, int, int, float, float, float, float, str]] = []
        page_counts: Dict[int, int] = {}
        for pi, x0, y0, x1, y1, text in block_rect_items:
            if pi > table_header_spec.max_page_index:
                continue
            if max_y0_by_page is not None and y0 > max_y0_by_page.get(pi, -float("inf")):
                continue
            page_counts[pi] = page_counts.get(pi, 0) + 1
            if excluded(text) or not matched(text):
                continue
            rank = _table_item_matched_min_column_local(text, pairs)
            picked_b.append((rank, pi, page_counts[pi], x0, y0, x1, y1, text))
        if table_header_spec.sort_mode == "column_rank_then_geom":
            picked_b.sort(key=lambda row: (row[0], row[1], row[4], row[3]))
        return "\n".join(
            f"page_index={pi}\tcolumn_index={rank}\tblock_index={bi}\tx0_pt={x0:.1f}\ty0_pt={y0:.1f}\t"
            f"x1_pt={x1:.1f}\ty1_pt={y1:.1f}\t{format_pymupdf_block_text_like_cluster_script(text)}"
            for rank, pi, bi, x0, y0, x1, y1, text in picked_b
        ).strip()

    picked_w: List[Tuple[int, int, int, float, float, float, float, str, int, int, int]] = []
    for pi, wi, x0, y0, x1, y1, text, bn, ln, wn in word_rect_items:
        if pi > table_header_spec.max_page_index:
            continue
        if max_y0_by_page is not None and y0 > max_y0_by_page.get(pi, -float("inf")):
            continue
        if excluded(text) or not matched(text):
            continue
        rank = _table_item_matched_min_column_local(text, pairs)
        picked_w.append((rank, pi, wi, x0, y0, x1, y1, text, bn, ln, wn))
    if table_header_spec.sort_mode == "column_rank_then_geom":
        picked_w.sort(key=lambda row: (row[0], row[1], row[5], row[4]))

    lines: List[str] = []
    for rank, pi, wi, x0, y0, x1, y1, text, bn, ln, wn in picked_w:
        tcell = format_pymupdf_block_text_like_cluster_script(text)
        if table_header_spec.words_line_format == "bbox_column_text":
            lines.append(
                f"x0={x0:.1f}\tx1={x1:.1f}\t{tcell}"
            )
        else:
            lines.append(
                f"page_index={pi}\tcolumn_index={rank}\tword_index={wi}\tx0_pt={x0:.1f}\ty0_pt={y0:.1f}\t"
                f"x1_pt={x1:.1f}\ty1_pt={y1:.1f}\tblock_no={pymupdf_prompt_meta_str(bn)}\t"
                f"line_no={pymupdf_prompt_meta_str(ln)}\tword_no={pymupdf_prompt_meta_str(wn)}\t{tcell}"
            )
    return "\n".join(lines).strip()


def _commercial_pass2_table_header_spec_from_cfg(
    cfg: CommercialPass2Pass2TableConfig,
) -> CommercialPass2TableHeaderExtractSpec:
    et, ec = cfg.filtered_header_pairs_for_extract_table()
    return CommercialPass2TableHeaderExtractSpec(
        header_texts_in_column_order=et,
        column_indices=ec,
        exclude_substrings=cfg.exclude_substrings,
        max_page_index=_COMMERCIAL_DETAIL_PASS2_MAX_PAGE_INDEX,
        extract_source="words",
        sort_mode="document_order",
        words_line_format="bbox_column_text",
    )


_COMMERCIAL_PASS2_PASS2_TABLE_CFG_BY_COMPANY: Final[Dict[KnownInsuranceCompany, CommercialPass2Pass2TableConfig]] = {
    KnownInsuranceCompany.BO_HAI: _COMMERCIAL_PASS2_BOHAI_PASS2_TABLE_CONFIG,
    KnownInsuranceCompany.ZHONG_YIN: _COMMERCIAL_PASS2_ZHONGYIN_PASS2_TABLE_CONFIG,
    KnownInsuranceCompany.HUA_AN: _COMMERCIAL_PASS2_HUAAN_PASS2_TABLE_CONFIG,
}
_COMMERCIAL_PASS2_TABLE_HEADER_SPEC_BY_COMPANY: Final[
    Dict[KnownInsuranceCompany, CommercialPass2TableHeaderExtractSpec]
] = {
    co: _commercial_pass2_table_header_spec_from_cfg(cfg)
    for co, cfg in _COMMERCIAL_PASS2_PASS2_TABLE_CFG_BY_COMPANY.items()
}
_COMMERCIAL_DETAIL_GENERIC_HEADER_UP_PAD_PT: Final[float] = 20.0


def _commercial_detail_word_height_ok(
    item: PymupdfWordRectItem,
    *,
    max_height_pt: float = _COMMERCIAL_DETAIL_WORD_MAX_HEIGHT_PT,
) -> bool:
    return (item[5] - item[3]) <= max_height_pt


def commercial_pass2_build_user_prompt_with_extract_table(
    *,
    instruction_without_table_header: str,
    context_block: str,
    table_header_spec: Optional[CommercialPass2TableHeaderExtractSpec] = None,
    pdf_bytes: Optional[bytes] = None,
    precomputed_extracted_lines: Optional[str] = None,
    table_header_section_preamble: str = "",
    context_section_title: str = "以下为上下文块：",
) -> str:
    """
    抽象链路：``extract_table``（由 ``table_header_spec`` 描述；可用 ``precomputed_extracted_lines`` 避免重复解析）
    → ``compose_table_llm_user_prompt``。``table_header_section_preamble`` 为接在表头行块前的说明，由调用方传入。
    不含 HTTP；与 ``_doubao_infer_commercial_detail_row`` 的 user 侧一致。
    """
    lines = (precomputed_extracted_lines or "").strip()
    if table_header_spec is not None and not lines:
        if pdf_bytes is None:
            raise ValueError(
                "commercial_pass2_build_user_prompt_with_extract_table："
                "未提供 precomputed_extracted_lines 时必须传入 pdf_bytes"
            )
        lines = table_header_spec.extract_table_lines(pdf_bytes)
    preamble = (table_header_section_preamble or "").strip()
    return compose_table_llm_user_prompt(
        instruction=instruction_without_table_header,
        context_block=context_block,
        extracted_table_lines=lines,
        table_header_section_preamble=preamble if lines else "",
        context_section_title=context_section_title,
    )


def _commercial_pass2_anchor_labels() -> Tuple[str, ...]:
    """pass2 锚点：险种行 + 乘客行（不含「车损险每次事故绝对免赔额」独立行，以免与列说明冲突）。"""
    return tuple(
        sorted(
            frozenset(_COMMERCIAL_DETAIL_LABEL_TO_COVERAGE_PREMIUM_KV)
            | frozenset(_COMMERCIAL_PASSENGER_ROW_LABELS),
            key=len,
            reverse=True,
        )
    )


def _commercial_longest_pass2_anchor_label(
    block_text: str,
    anchors: Sequence[str],
) -> Optional[str]:
    best: Optional[str] = None
    for lab in anchors:
        if _commercial_find_label_start(block_text, lab, use_ws_fold=True) < 0:
            continue
        if best is None or len(lab) > len(best):
            best = lab
    return best


def _commercial_build_detail_llm_band_words(
    words: Sequence[Tuple[int, int, float, float, float, float, str, int, int, int]],
    *,
    page_index: int,
    ref_y0: float,
    ref_y1: float,
    pad_pt: float,
) -> List[Tuple[int, float, float, float, float, str, int, int, int]]:
    """
    与锚块同一页、纵带相交的 ``get_text("words")`` 词条（阅读顺序：y0 再 x0）。
    每项 (word_index_on_page, x0, y0, x1, y1, text, block_no, line_no, word_no)。
    """
    rows: List[Tuple[int, float, float, float, float, str, int, int, int]] = []
    for pi, wi, x0, y0, x1, y1, text, bn, ln, wn in words:
        if pi != page_index:
            continue
        if not _in_vertical_band(y0, y1, ref_y0, ref_y1, pad_pt):
            continue
        if not _commercial_detail_bbox_overlaps_ref_y_enough(y0, y1, ref_y0, ref_y1):
            continue
        rows.append((wi, x0, y0, x1, y1, text, bn, ln, wn))
    rows.sort(key=lambda t: (t[1], t[2]))
    return rows


def _commercial_detail_bbox_overlaps_ref_y_enough(
    cand_y0: float,
    cand_y1: float,
    ref_y0: float,
    ref_y1: float,
    *,
    min_ratio: float = _COMMERCIAL_DETAIL_MIN_REF_Y_OVERLAP_RATIO,
) -> bool:
    cand_h = cand_y1 - cand_y0
    if cand_h <= 0:
        return False
    overlap = min(cand_y1, ref_y1) - max(cand_y0, ref_y0)
    if overlap <= 0:
        return False
    return (overlap / cand_h) > min_ratio


def _commercial_detail_llm_context_from_word_rows(
    page_index: int,
    rows: Sequence[Tuple[int, float, float, float, float, str, int, int, int]],
    *,
    max_chars: int = _COMMERCIAL_DETAIL_LLM_CONTEXT_MAX_CHARS,
) -> str:
    """每行格式对齐 ``cluster_policy_blocks_by_grid_lines`` 第四节（页码/词序号/bbox/meta/文本）。"""
    lines = [
        f"x0={x0:.1f}\ty0={y0:.1f}\tx1={x1:.1f}\ty1={y1:.1f}\t"
        f"{format_pymupdf_block_text_like_cluster_script(txt)}"
        for wi, x0, y0, x1, y1, txt, bn, ln, wn in rows
    ]
    blob = "\n".join(lines)
    if len(blob) <= max_chars:
        return blob
    return blob[: max_chars - 80] + "\n…(截断)"


def _commercial_detail_generic_header_context_from_top_anchors(
    word_items: Sequence[PymupdfWordRectItem],
    anchor_items: Sequence[PymupdfBlockRectItem],
    *,
    up_pad_pt: float = _COMMERCIAL_DETAIL_GENERIC_HEADER_UP_PAD_PT,
) -> str:
    if not anchor_items:
        return ""
    top_page = min(row[0] for row in anchor_items)
    top_page_anchors = [row for row in anchor_items if row[0] == top_page]
    if not top_page_anchors:
        return ""
    top_y0 = min(row[2] for row in top_page_anchors)
    lo = top_y0 - up_pad_pt
    hi = top_y0
    rows: List[Tuple[int, float, float, float, float, str, int, int, int]] = []
    for pi, wi, x0, y0, x1, y1, text, bn, ln, wn in word_items:
        if pi != top_page:
            continue
        if y1 < lo or y0 > hi:
            continue
        rows.append((wi, x0, y0, x1, y1, text, bn, ln, wn))
    rows.sort(key=lambda t: (t[2], t[1]))
    return _commercial_detail_llm_context_from_word_rows(top_page, rows)


def _commercial_parse_llm_json_object(content: str) -> Optional[Dict[str, Any]]:
    s = (content or "").strip()
    if not s:
        return None
    s = s.strip("`\"“” ")
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s, flags=re.I)
        s = re.sub(r"\s*```\s*$", "", s)
    i = s.find("{")
    j = s.rfind("}")
    if i < 0 or j < i:
        return None
    chunk = s[i : j + 1]
    try:
        obj = json.loads(chunk)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def _commercial_detail_llm_field_value(raw: Any) -> str:
    value = str(raw or "").strip()
    if value == _COMMERCIAL_DETAIL_LLM_NOT_FOUND:
        return ""
    return value


def _aggregate_pass2_deductibles_from_llm_strings(raws: Sequence[str]) -> Optional[str]:
    """
    多条 LLM 返回的免赔额字符串：均为明确数值 0 则 ``\"0\"``；
    若有任一非零则取最大值并保留两位小数；无任何有效片段则 ``None``。
    """
    decs: List[Decimal] = []
    for r in raws:
        t = str(r).strip()
        if t in {"", "/", "／", "-", "—", "－"}:
            continue
        v = _commercial_parse_money_amount(t)
        if v is None:
            continue
        try:
            decs.append(Decimal(str(v)))
        except InvalidOperation:
            continue
    if not decs:
        return None
    if all(d == 0 for d in decs):
        return "0"
    mx = max(decs)
    return _ping_an_norm_two_dec(format(mx, "f"))


def _commercial_detail_pass2_layout_instruction(
    company: Optional[KnownInsuranceCompany],
    *,
    row_label: str,
    company_name: str = "",
) -> str:
    """各保司列序说明；表头 ``extract_table`` 行块由 ``compose_table_llm_user_prompt`` 另接（与配置一致）。"""
    cfg = _COMMERCIAL_PASS2_PASS2_TABLE_CFG_BY_COMPANY.get(company) if company is not None else None
    if cfg is None:
        company_display = company_name.strip() or (company.value if company is not None else "某保险公司")
        return "\n".join(
            [
                f"本保单为{company_display}承保险种明细。",
                (
                    f"当前险种「{row_label}」。只抽取和当前险种「{row_label}」同一行的保额/保险金额/责任限额"
                    "以及保费/保险费。"
                ),
                (
                    "通常承保险种在左侧列，保额/保险金额/责任限额在中间金额列，保费/保险费在右侧金额列。"
                    "不要把费率、免赔率、税额、合计金额、其它险种行的金额当作当前险种结果。"
                ),
                "同一行指纵坐标范围相同或很接近（纵带内各词条 y0、y1 相近即视为同一表格行）。",
            ]
        )
    key_h = cfg.header_text_for_column(cfg.key_column_index)
    col_order_desc = " ".join(
        f"第{c}列「{t}」"
        for c, t in sorted(
            zip(cfg.column_indices, cfg.header_texts_in_column_order),
            key=lambda p: p[0],
        )
    )
    detail_cols_desc = "、".join(
        f"第{col}列「{cfg.header_text_for_column(col)}」" for col in cfg.detail_column_indices
    )
    lines = [
        f"本保单为{cfg.policy_issuer_for_llm}承保险种明细。",
        (
            f"当前表格列顺序为{col_order_desc}，当前险种「{row_label}」，可与表头「{key_h}」列对齐。"
            f"只抽取和当前险种「{row_label}」同一行的{detail_cols_desc}。"
        ),
        "不要合并其它险种行。补充：同一行指纵坐标范围相同或很接近（纵带内各词条 y0、y1 相近即视为同一表格行）。",
    ]
    return "\n".join(lines)


def _doubao_infer_commercial_detail_row(
    *,
    company: Optional[KnownInsuranceCompany],
    company_name: str = "",
    row_label: str,
    k_cov: str,
    k_prem: str,
    context_block: str,
    collect_deductible: bool,
    precomputed_table_header_lines: Optional[str] = None,
    table_header_spec: Optional[CommercialPass2TableHeaderExtractSpec] = None,
    table_header_section_preamble: str = "",
) -> Optional[Dict[str, str]]:
    """单行承保险种上下文 → JSON：coverage / premium / deductible（金额字符串，可空）。"""
    api_key = os.environ.get(_INSURER_DOUBAO_ENV_API_KEY, "").strip()
    model = os.environ.get(_INSURER_DOUBAO_ENV_MODEL, "").strip()
    chat_url = f"{_INSURER_DOUBAO_API_BASE.rstrip('/')}/chat/completions"
    if not api_key or not model:
        return None

    layout = _commercial_detail_pass2_layout_instruction(
        company,
        row_label=row_label,
        company_name=company_name,
    )
    ded_line = ""
    if collect_deductible:
        ded_line = (
            f'字段 "deductible"：该行免赔额；原文无金额、为 /、或无法确定时一律 "{_COMMERCIAL_DETAIL_LLM_NOT_FOUND}"。'
        )
    else:
        ded_line = f'字段 "deductible"：无单独免赔列时固定输出 "{_COMMERCIAL_DETAIL_LLM_NOT_FOUND}"。'

    passenger_cov_hint = ""
    if (
        k_cov == "新能源汽车车上人员责任保险(乘客)保额"
        or row_label in _COMMERCIAL_PASSENGER_ROW_LABELS
    ):
        passenger_cov_hint = (
            "若本行为「新能源汽车车上人员责任保险(乘客)保额」对应行：coverage 常为座位数与每座保额的组合展示，"
            "形式类似「4座*50000元/座」或「50000元/座*4座」（座与金额之间可用 *、×、x；可有万元/座、元/座等）；"
            "请将表格单元格内与原文一致的整段保额写入 coverage，勿只输出孤立数字。"
        )

    if company in _COMMERCIAL_PASS2_PASS2_TABLE_CFG_BY_COMPANY:
        user_ctx_line_desc = (
            "用户消息含两部分：其一为从 PDF 前几页筛出的表头相关词条若干行（每行仅横坐标 x0/x1 与文本）；"
            "其二为当前险种行附近的文字项，每行含轴对齐 bbox 坐标 x0/y0/x1/y1 与文本。"
            "坐标单位为 pt，x0/x1 分别为文字框左/右边界，y0/y1 分别为文字框上/下边界。"
        )
    else:
        user_ctx_line_desc = (
            "用户给出当前险种行附近的若干文字项"
            "（每行含轴对齐 bbox 坐标 x0/y0/x1/y1 与文本）。"
            "坐标单位为 pt，x0/x1 分别为文字框左/右边界，y0/y1 分别为文字框上/下边界。"
        )
    system_prompt = (
        "你是车险商业险承保险种明细表抽取助手。"
        + user_ctx_line_desc
        + "你必须只根据原文抽取，禁止编造。"
        "你必须只输出一个合法 JSON 对象（UTF-8），不要 markdown、不要解释。"
        '根对象有且仅有一个键 "result"，值为对象；该对象含键 coverage、premium、deductible，值均为字符串：'
        "coverage 为保额原文中的金额（可含千分位、小数、元/万元等，与单元格一致即可）；"
        "premium 为保费金额字符串；"
        + ded_line
        + f'coverage 或 premium 无法从原文确定时，对应键必须输出 "{_COMMERCIAL_DETAIL_LLM_NOT_FOUND}"；'
        + f'工程侧会把 "{_COMMERCIAL_DETAIL_LLM_NOT_FOUND}" 识别为未命中并返回空字符串。'
        + (
            f'示例：{{"result":{{"coverage":"500000.00","premium":"1200.00",'
            f'"deductible":"{_COMMERCIAL_DETAIL_LLM_NOT_FOUND}"}}}}。'
        )
        + passenger_cov_hint
    )
    user_content = commercial_pass2_build_user_prompt_with_extract_table(
        instruction_without_table_header=layout,
        context_block=context_block,
        table_header_spec=table_header_spec,
        precomputed_extracted_lines=precomputed_table_header_lines,
        table_header_section_preamble=table_header_section_preamble,
    )
    payload = {
        "model": model,
        "temperature": 0.0,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "response_format": _DOUBAO_RESPONSE_FORMAT_JSON_OBJECT,
    }

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    req = Request(
        chat_url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with urlopen(req, timeout=120) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except (URLError, OSError):
        return None

    try:
        data = json.loads(raw)
        content = (
            data.get("choices", [{}])[0]
            .get("message", {})
            .get("content", "")
            .strip()
        )
    except (json.JSONDecodeError, IndexError, TypeError, AttributeError):
        return None

    obj = _commercial_parse_llm_json_object(content)
    if not obj:
        return None
    inner = obj.get("result")
    data_row: Dict[str, Any] = inner if isinstance(inner, dict) else obj
    out = {
        "coverage": _commercial_detail_llm_field_value(data_row.get("coverage", "")),
        "premium": _commercial_detail_llm_field_value(data_row.get("premium", "")),
        "deductible": _commercial_detail_llm_field_value(data_row.get("deductible", "")),
    }
    return out


def _commercial_pass2_stop_detail_llm_group(
    out: Dict[str, str],
    lab: str,
    k_cov: str,
    k_prem: str,
) -> bool:
    """
    保额或保费任一非空即停。
    乘客行例外：若已有保费但保额仅为纯数字串（无常「座」「万元」等展示形态），
    继续尝试后续块，避免先命中单列金额时被 ``_commercial_parse_money_amount`` 归一化后早停。
    """
    cov = (out.get(k_cov) or "").strip()
    prem = (out.get(k_prem) or "").strip()
    if not cov and not prem:
        return False
    if lab in _COMMERCIAL_PASSENGER_ROW_LABELS and prem and cov:
        if "座" not in cov and "万元" not in cov:
            return False
    return bool(cov) or bool(prem)


def _commercial_pass2_detail_llm_prefilter_enabled() -> bool:
    v = os.environ.get("CAR_INSURANCE_COMMERCIAL_PASS2_DETAIL_PREFILTER", "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def _commercial_pass2_prefilter_x_window_pt() -> Tuple[float, float]:
    """
    环境变量 ``CAR_INSURANCE_COMMERCIAL_PASS2_PREFILTER_X_PT``：``左扩展,右扩展``（pt），
    如 ``400,600``；非法或未设则用模块默认 ``_COMMERCIAL_PASS2_PREFILTER_X_LEFT_PT`` /
    ``_COMMERCIAL_PASS2_PREFILTER_X_RIGHT_PT``。
    """
    raw = os.environ.get("CAR_INSURANCE_COMMERCIAL_PASS2_PREFILTER_X_PT", "").strip()
    if not raw:
        return (_COMMERCIAL_PASS2_PREFILTER_X_LEFT_PT, _COMMERCIAL_PASS2_PREFILTER_X_RIGHT_PT)
    parts = [p for p in re.split(r"[\s,;]+", raw) if p]
    if len(parts) >= 2:
        try:
            return float(parts[0]), float(parts[1])
        except ValueError:
            pass
    return (_COMMERCIAL_PASS2_PREFILTER_X_LEFT_PT, _COMMERCIAL_PASS2_PREFILTER_X_RIGHT_PT)


def _commercial_pass2_row_neighbor_blocks_flat_text(
    block_items: Sequence[Tuple[int, float, float, float, float, str]],
    *,
    ref_flat_index: int,
    y_pad_pt: float,
    x_left_pt: float,
    x_right_pt: float,
) -> str:
    """
    关键字锚定的 ``block_items[ref_flat_index]`` 同页、与锚块纵带相交（±pad）、
    且与锚块水平外包络在「左扩 x_left_pt / 右扩 x_right_pt」并集内的所有块 ``text``，
    按阅读顺序 ``(y0, x0)`` 拼接。用于 pass2 调大模型前判断同行是否像「有金额或乘客保额形态」。
    """
    if ref_flat_index < 0 or ref_flat_index >= len(block_items):
        return ""
    pi, ref_x0, ref_y0, ref_x1, ref_y1, _ = block_items[ref_flat_index]
    chunks: List[Tuple[float, float, str]] = []
    for j, (pj, x0, y0, x1, y1, text) in enumerate(block_items):
        if pj != pi:
            continue
        if not _in_vertical_band(y0, y1, ref_y0, ref_y1, y_pad_pt):
            continue
        if x1 < ref_x0 - x_left_pt or x0 > ref_x1 + x_right_pt:
            continue
        if text:
            chunks.append((y0, x0, text))
    chunks.sort(key=lambda t: (t[0], t[1]))
    return "\n".join(t[2] for t in chunks)


def _commercial_pass2_neighbor_blob_suggests_detail_llm(blob: str) -> bool:
    """邻域块拼接串内是否出现金额（与 ``_commercial_parse_money_amount`` 一致）或乘客保额常见形态。"""
    if not (blob or "").strip():
        return False
    if _COMMERCIAL_PASS2_PASSENGER_TEXT_HINT_RE.search(blob):
        return True
    if _commercial_parse_money_amount(blob):
        return True
    for seg in re.split(r"[\n\r]+", blob):
        s = seg.strip()
        if s and _commercial_parse_money_amount(s):
            return True
    return False


def _commercial_pass2_doubao_single_lab(
    lab: str,
    jobs: List[Tuple[int, str]],
    company: Optional[KnownInsuranceCompany],
    *,
    engine_label: str,
    collect_ded: bool,
    block_items: Sequence[Tuple[int, float, float, float, float, str]],
    company_name: str = "",
    precomputed_table_header_lines: Optional[str] = None,
    table_header_spec: Optional[CommercialPass2TableHeaderExtractSpec] = None,
    table_header_section_preamble: str = "",
) -> Tuple[Dict[str, str], List[str], int, int]:
    """
    单个锚点险种：同 lab 多块 **串行** 调豆包；返回该 lab 写入的 kv 片段（仅非空键）、华安免赔额片段列表、
    本 lab 锚点块数、pattern 预筛跳过的组数（仅预筛开启时递增）。
    每块在 HTTP 前对「锚块同行邻域 blocks」拼接串做金额/乘客形态预筛，无命中则跳过该次调用
    （见 ``_commercial_pass2_row_neighbor_blocks_flat_text``；可用环境变量关闭）。
    本 lab 全部尝试结束后在同一线程内打 ``明细pass2 统计`` DEBUG（避免主线程等齐所有并行任务后才打、与 LLM 行脱节）。
    """
    k_cov, k_prem = _COMMERCIAL_DETAIL_LABEL_TO_COVERAGE_PREMIUM_KV[lab]
    frag: Dict[str, str] = {}
    ded_raws: List[str] = []
    state = PREMIUM_DETAIL_EXTRACTION_STATE
    x_left, x_right = _commercial_pass2_prefilter_x_window_pt()
    prefilter_on = _commercial_pass2_detail_llm_prefilter_enabled()
    n_anchor_blocks = len(jobs)
    n_prefilter_skipped = 0

    for fi, ctx in jobs:
        if _commercial_pass2_stop_detail_llm_group(frag, lab, k_cov, k_prem):
            break
        if prefilter_on:
            row_blob = _commercial_pass2_row_neighbor_blocks_flat_text(
                block_items,
                ref_flat_index=fi,
                y_pad_pt=_COMMERCIAL_PASS2_PREFILTER_Y_PAD_PT,
                x_left_pt=x_left,
                x_right_pt=x_right,
            )
            if not _commercial_pass2_neighbor_blob_suggests_detail_llm(row_blob):
                n_prefilter_skipped += 1
                _log_pass(
                    engine_label,
                    state,
                    2,
                    "明细pass2 预筛跳过 LLM（邻域无金额/乘客形态）label=%r flat_index=%s",
                    lab,
                    fi,
                )
                continue
        try:
            llm_row = _doubao_infer_commercial_detail_row(
                company=company,
                company_name=company_name,
                row_label=lab,
                k_cov=k_cov,
                k_prem=k_prem,
                context_block=ctx,
                collect_deductible=collect_ded,
                precomputed_table_header_lines=precomputed_table_header_lines,
                table_header_spec=table_header_spec,
                table_header_section_preamble=table_header_section_preamble,
            )
        except Exception:
            continue
        if not llm_row:
            _log_pass(
                engine_label,
                state,
                2,
                "明细pass2 无LLM结果 label=%r flat_index=%s",
                lab,
                fi,
            )
            continue
        cov_s = llm_row.get("coverage") or ""
        prem_s = llm_row.get("premium") or ""
        ded_s = llm_row.get("deductible") or ""
        cov_n = _commercial_parse_money_amount(cov_s) if cov_s else None
        prem_n = _commercial_parse_money_amount(prem_s) if prem_s else None
        if k_cov == "新能源汽车车上人员责任保险(乘客)保额" and cov_s.strip():
            cs = cov_s.strip()
            if "座" in cs or "万元" in cs or re.search(r"元\s*/\s*座|/座", cs, re.I):
                frag[k_cov] = _commercial_canonical_passenger_coverage_display(cs)
            elif cov_n:
                frag[k_cov] = cov_n
        elif cov_n:
            frag[k_cov] = cov_n
        elif cov_s and ("座" in cov_s or "万元" in cov_s):
            frag[k_cov] = cov_s.strip()
        if prem_n:
            frag[k_prem] = prem_n
        if collect_ded and lab == "新能源汽车损失保险" and ded_s != "":
            ded_raws.append(ded_s)
        _log_pass(
            engine_label,
            state,
            2,
            "明细pass2 LLM label=%r cov=%r prem=%r ded=%r flat_index=%s",
            lab,
            cov_n,
            prem_n,
            ded_s if collect_ded else "",
            fi,
        )

    _log_pass(
        engine_label,
        state,
        2,
        "明细pass2 统计 label=%r kv_cov=%r kv_prem=%r 锚点块数=%s pattern筛掉组数=%s",
        lab,
        k_cov,
        k_prem,
        n_anchor_blocks,
        n_prefilter_skipped,
    )
    return frag, ded_raws, n_anchor_blocks, n_prefilter_skipped


def run_car_insurance_commercial_detail_pass2_doubao(
    company: Optional[KnownInsuranceCompany],
    *,
    engine_label: str,
    company_name: str = "",
    block_rect_items: Optional[Sequence[PymupdfBlockRectItem]] = None,
    word_rect_items: Optional[Sequence[PymupdfWordRectItem]] = None,
) -> Dict[str, str]:
    """
    PyMuPDF 扁平项（**仅前两页**）：先按行锚在 ``text`` 内定位险种名，再取锚项纵坐标 ±pad 的项拼上下文，
    按锚点险种分组；**不同 lab（不同险种行）之间并行**调豆包，同 lab 多块仍 **串行** 且早停。
    （乘客行在「有保费但保额仅为纯数字」时继续尝试，以免保额展示串被金额解析覆盖）。
    华安收集已调用行返回的 deductible 后做免赔额聚合。

    每锚点块在调豆包前：将同页、纵带相交且水平方向落在锚块左/右扩展窗内的 blocks 文本拼接，
    若其中既无 ``_commercial_parse_money_amount`` 可解析金额、也无乘客保额常见片段（见
    ``_COMMERCIAL_PASS2_PASSENGER_TEXT_HINT_RE``），则跳过该次 HTTP（省调用量）。
    关闭：环境变量 ``CAR_INSURANCE_COMMERCIAL_PASS2_DETAIL_PREFILTER=0``；左右窗宽
    ``CAR_INSURANCE_COMMERCIAL_PASS2_PREFILTER_X_PT``（``左pt,右pt``）。
    ``明细pass2 统计`` 在各险种并行任务内、该 lab 处理结束时打出（与对应 ``明细pass2 LLM`` 相邻），
    而非等全部险种跑完再在主线程打一批。

    锚点险种名用 ``iter_pymupdf_block_rect_items``（``blocks``）；LLM 纵带上下文与渤海表头附录用
    ``iter_pymupdf_word_rect_items``（``words``），与 ``cluster_policy_blocks_by_grid_lines.py`` 第四节一致。
    """
    out: Dict[str, str] = {k: "" for k in _CAR_INSURANCE_COMMERCIAL_ONLY_KV_KEYS}
    collect_ded = company == KnownInsuranceCompany.HUA_AN
    ded_raws: List[str] = []

    if block_rect_items is None or word_rect_items is None:
        return out
    blocks_all = list(block_rect_items)
    words_all = list(word_rect_items)

    block_items = [
        row
        for row in blocks_all
        if row[0] <= _COMMERCIAL_DETAIL_PASS2_MAX_PAGE_INDEX
    ]
    word_items = [
        row
        for row in words_all
        if row[0] <= _COMMERCIAL_DETAIL_PASS2_MAX_PAGE_INDEX
        and _commercial_detail_word_height_ok(row)
    ]

    anchors = _commercial_pass2_anchor_labels()
    seen_flat: Set[int] = set()
    matched_anchor_rows: List[Tuple[int, PymupdfBlockRectItem, str]] = []
    for fi, row in enumerate(block_items):
        if fi in seen_flat:
            continue
        pi, x0, y0, x1, y1, text = row
        lab = _commercial_longest_pass2_anchor_label(text, anchors)
        if lab is None:
            continue
        seen_flat.add(fi)
        if lab not in _COMMERCIAL_DETAIL_LABEL_TO_COVERAGE_PREMIUM_KV:
            continue
        matched_anchor_rows.append((fi, row, lab))

    precomputed_table_header_lines: Optional[str] = None
    table_header_spec = _COMMERCIAL_PASS2_TABLE_HEADER_SPEC_BY_COMPANY.get(company)
    table_header_section_preamble: str = ""
    if table_header_spec is not None:
        header_max_y0_by_page: Dict[int, float] = {}
        for _fi, row, _lab in matched_anchor_rows:
            pi = row[0]
            y0 = row[2]
            old = header_max_y0_by_page.get(pi)
            if old is None or y0 < old:
                header_max_y0_by_page[pi] = y0
        precomputed_table_header_lines = _extract_table_lines_from_precomputed_items(
            table_header_spec,
            block_rect_items=block_items,
            word_rect_items=word_items,
            max_y0_by_page=header_max_y0_by_page,
        )
        table_header_section_preamble = build_extract_table_words_header_section_preamble(
            table_header_spec.header_texts_in_column_order,
            table_header_spec.column_indices,
            words_line_format=table_header_spec.words_line_format,
        )
    else:
        precomputed_table_header_lines = _commercial_detail_generic_header_context_from_top_anchors(
            word_items,
            [row for _fi, row, _lab in matched_anchor_rows],
        )
        table_header_section_preamble = (
            "下列词条为从所有已识别商业险明细 key 中最上方 key 的上方 20pt 区域收集的 "
            "文字 bbox，用作通用表头/列对齐参考："
        )

    lab_order: List[str] = []
    lab_jobs: Dict[str, List[Tuple[int, str]]] = {}

    for fi, (pi, x0, y0, x1, y1, text), lab in matched_anchor_rows:
        k_cov, k_prem = _COMMERCIAL_DETAIL_LABEL_TO_COVERAGE_PREMIUM_KV[lab]
        band = _commercial_build_detail_llm_band_words(
            word_items,
            page_index=pi,
            ref_y0=y0,
            ref_y1=y1,
            pad_pt=_COMMERCIAL_DETAIL_LLM_Y_PAD_PT,
        )
        ctx = _commercial_detail_llm_context_from_word_rows(pi, band)
        if lab not in lab_jobs:
            lab_order.append(lab)
            lab_jobs[lab] = []
        lab_jobs[lab].append((fi, ctx))

    ex = _car_insurance_llm_executor()
    pending: Dict[Future, str] = {}
    for lab in lab_order:
        fut = ex.submit(
            _commercial_pass2_doubao_single_lab,
            lab,
            lab_jobs[lab],
            company,
            engine_label=engine_label,
            collect_ded=collect_ded,
            block_items=block_items,
            company_name=company_name,
            precomputed_table_header_lines=precomputed_table_header_lines,
            table_header_spec=table_header_spec,
            table_header_section_preamble=table_header_section_preamble,
        )
        pending[fut] = lab

    by_lab: Dict[str, Tuple[Dict[str, str], List[str], int, int]] = {}
    if pending:
        _log_pass(
            engine_label,
            PREMIUM_DETAIL_EXTRACTION_STATE,
            2,
            "明细pass2 并行任务已提交 lab数=%s labels=%r（各 lab 内多块仍串行 HTTP）",
            len(pending),
            lab_order,
        )
        t_parallel0 = time.monotonic()
        for fut in as_completed(pending):
            lab_done = pending[fut]
            try:
                by_lab[lab_done] = fut.result()
            except Exception:
                by_lab[lab_done] = ({}, [], 0, 0)
        wall_parallel_s = time.monotonic() - t_parallel0
        _log_pass(
            engine_label,
            PREMIUM_DETAIL_EXTRACTION_STATE,
            2,
            "明细pass2 全部并行任务已返回 wall_s=%.2f（墙钟≈最慢 lab；非各次 HTTP 简单相加）",
            wall_parallel_s,
        )

    for lab in lab_order:
        frag, dr, _n_anchor, _n_pf_skip = by_lab.get(lab, ({}, [], 0, 0))
        for k, v in frag.items():
            if v:
                out[k] = v
        ded_raws.extend(dr)

    if collect_ded:
        agg = _aggregate_pass2_deductibles_from_llm_strings(ded_raws)
        if agg is not None:
            out["免赔额"] = agg

    return out


def run_car_insurance_ping_an_premium_detail_passes(
    blocks: Sequence[str],
    *,
    engine_label: str,
) -> Dict[str, str]:
    """兼容入口：等价于 ``detail_layout='PING_AN'``。"""
    return run_car_insurance_commercial_detail_table_passes(
        blocks,
        engine_label=engine_label,
        detail_layout="PING_AN",
    )


def _find_dates_in_text(text: str) -> List[str]:
    """
    在文本中查找日期时间字符串，返回所有匹配的列表。
    只匹配同时包含日期和时间的格式（移除空格后匹配）：
    - 2025年5月28日17:41:37
    - 2025年5月28日17时41分
    - 2025年5月28日17时41分59秒
    - 2025-05-2817:41:37
    - 2025/05/2817:41:37
    - 2025年5月18日00:00时
    - 2025年5月18日00:00
    - 2025年5月18日00时
    - 2025-05-1800:00
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
    - 2025年5月18日00:00时
    - 2025年5月18日00:00
    - 2025年5月18日00时
    - 2025-05-1800:00
    - 24:00:00特殊情况（如2026年5月17日24:00时）
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
    """
    通过模式匹配从块中提取保险期间，返回ISO 8601格式。
    支持格式：
    - 保险期间：...起至...止（新格式）
    - 自...起至...止（原有格式）
    """
    patterns = [
        # 新格式：保险期间：...起至...止
        r'保险期间[：:]\s*(.+?)\s*起至\s*(.+?)\s*止',
        r'保险期间[：:]\s*(.+?)起至\s*(.+?)止',
        # 原有格式：自...起至...止
        r'自\s*([^起]+?)\s*起\s*至\s*([^止]+?)\s*止',
        r'自\s*([^至]+?)\s*至\s*([^止]+?)\s*止',
        r'自\s*(.+?)\s*起\s*至\s*(.+?)\s*止',
        r'自\s*(.+?)\s*至\s*(.+?)\s*止',
    ]

    # 如果没有传入特定的块索引，则搜索所有块（用于单元测试）
    indices_to_search = period_blocks_indices if period_blocks_indices else range(len(blocks))

    for i in indices_to_search:
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
    state = PERIOD_EXTRACTION_STATE

    # pass 1: 查找包含"保险期间"的块索引
    period_blocks_indices = _find_period_blocks_indices(blocks)

    if not period_blocks_indices:
        _log_pass(engine_label, state, 1, "未找到包含'保险期间'的块")
        return None

    _log_pass(engine_label, state, 1, "找到 %d 个包含'保险期间'的块: %s", len(period_blocks_indices), period_blocks_indices)

    # pass 2: 尝试从日期中提取
    period = _extract_period_from_dates_in_blocks(blocks, period_blocks_indices, engine_label)
    if period:
        _log_pass(engine_label, state, 2, "命中：%s", period)
        return period

    # pass 3: 尝试从模式匹配中提取
    period = _extract_period_from_patterns(blocks, period_blocks_indices, engine_label)
    if period:
        _log_pass(engine_label, state, 3, "命中：%s", period)
        return period

    _log_pass(engine_label, state, 3, "未命中，无法提取保险期间")
    return None


def _extract_period_with_bbox_fallback(
    pdf_bytes: Optional[bytes] = None,
    *,
    word_rect_items: Optional[Sequence[PymupdfWordRectItem]] = None,
) -> Optional[Dict[str, str]]:
    """
    使用 PyMuPDF 的 word 级 bbox 坐标信息提取保险期间（回退逻辑）。
    1. 获取所有 word 及其 bbox
    2. 找到包含"保险期间"的 word，记录纵坐标范围
    3. 查找纵坐标相近的候选 word（时间应该不高于保险期间 word，不低于或略低于）
    4. 在候选 word 拼接文本中匹配日期时间 pattern
    """
    if word_rect_items is None:
        if pdf_bytes is None:
            return None
        try:
            source_words = iter_pymupdf_word_rect_items(pdf_bytes)
        except ImportError:
            logger.debug("PyMuPDF未安装，无法使用bbox回退逻辑")
            return None
    else:
        source_words = list(word_rect_items)

    all_words = [(pi, wi, x0, y0, x1, y1, text) for pi, wi, x0, y0, x1, y1, text, _bn, _ln, _wn in source_words]
    period_words = [
        (pi, wi, x0, y0, x1, y1, text)
        for pi, wi, x0, y0, x1, y1, text in all_words
        if "保险期间" in text
    ]

    if not period_words:
        logger.debug("bbox回退逻辑：未找到包含'保险期间'的word")
        return None

    ref_page, ref_wi, ref_x0, ref_y0, ref_x1, ref_y1, ref_text = period_words[0]
    logger.debug("bbox回退逻辑：参考word y0=%.2f, y1=%.2f, 文本=%r", ref_y0, ref_y1, ref_text)

    y_tolerance = 10.0
    candidates: List[Tuple[float, float, int, str]] = []
    for page_index, wi, x0, y0, x1, y1, text in all_words:
        if page_index != ref_page:
            continue
        if _rects_close(x0, y0, x1, y1, ref_x0, ref_y0, ref_x1, ref_y1):
            continue
        if y0 >= ref_y0 - y_tolerance and y1 <= ref_y1 + y_tolerance:
            candidates.append((y0, x0, wi, text))

    if not candidates:
        logger.debug("bbox回退逻辑：未找到符合条件的候选word")
        return None

    candidates.sort(key=lambda row: (row[0], row[1], row[2]))
    candidate_texts = [text for _y0, _x0, _wi, text in candidates]
    combined_text = " ".join(candidate_texts)
    dates = _find_dates_in_text(combined_text)
    logger.debug("bbox回退逻辑：候选文本中找到日期: %s", dates)

    if len(dates) >= 2:
        period = _extract_period_from_dates(dates)
        if period:
            logger.debug("bbox回退逻辑：提取到保险期间: %s", period)
            return period

    for text in candidate_texts:
        dates = _find_dates_in_text(text)
        if len(dates) >= 2:
            period = _extract_period_from_dates(dates)
            if period:
                logger.debug("bbox回退逻辑：在单个候选块中提取到保险期间: %s", period)
                return period

    logger.debug("bbox回退逻辑：在候选块中未找到足够的日期")
    return None


def _policy_type_is_commercial(policy_type: str) -> bool:
    """兼容直接调用时大小写/空白。"""
    return str(policy_type).strip().lower() == "commercial"


def car_insurance_extract(
    policy_type: Literal["compulsory", "commercial"],
    pdf_url: str,
) -> Dict[str, Any]:
    """
    下载/读取 PDF，双路块序列跑保险公司名称相关 pass，合并为 ``kv`` 与 ``known_insurance_company``。
    未识别的标量字段为 ``""``（非 null）；``保险期间`` 未识别时为 ``""``，识别到则为起止字典。
    ``known_insurance_company`` 未命中枚举时为 ``""``。

    **商业险**：若承保方为平安或华安（规则枚举/全称或块内简称），合并后再强制调用一次承保公司豆包，
    若模型返回通过 pass3 筛选的有效全称，则以豆包为准覆盖规则结果。
    """
    pdf_bytes = load_pdf_bytes(pdf_url)
    pdf_views = extract_car_insurance_pdf_views(pdf_bytes)
    blocks_pdf = pdf_views.pypdf_blocks
    page_texts_pdf = pdf_views.pypdf_page_texts
    blocks_mu = pdf_views.pymupdf_blocks
    block_rect_items = pdf_views.pymupdf_block_rect_items
    word_rect_items = pdf_views.pymupdf_word_rect_items

    is_commercial = _policy_type_is_commercial(policy_type)
    defer_mu_bbox_llm = is_commercial and _commercial_blocks_hint_ping_an_or_hua_an(
        blocks_pdf,
        blocks_mu,
    )
    r_pdf = run_car_insurance_insurer_passes(page_texts_pdf, engine_label="pypdf", pdf_bytes=None)
    r_mu = run_car_insurance_insurer_passes(
        blocks_mu,
        engine_label="pymupdf",
        block_rect_items=block_rect_items,
        word_rect_items=word_rect_items,
        defer_bbox_llm=defer_mu_bbox_llm,
    )
    merged_name = _merge_insurer_display_prefer_longer(
        r_mu.get("保险公司名称"),
        r_pdf.get("保险公司名称"),
    )
    merged_enum: Optional[KnownInsuranceCompany] = (
        r_mu.get("known_company") or r_pdf.get("known_company")
    )
    if merged_enum is None and merged_name:
        merged_enum = _pass4_enum_for_hit(merged_name)

    if is_commercial and _commercial_insurer_must_run_doubao_overlay(
        insurer_name=merged_name,
        known_company=merged_enum,
        blocks_pdf=blocks_pdf,
        blocks_mu=blocks_mu,
    ):
        ol = _pass5_insurer_name_doubao_llm(
            engine_label="commercial_ping_an_hua_an",
            anchor_fallback=True,
            block_rect_items=block_rect_items,
            word_rect_items=word_rect_items,
        )
        ol = _pass3_filter_insurer_name(
            ol,
            from_pass=5,
            engine_label="commercial_ping_an_hua_an",
        )
        if ol is not None:
            merged_name = ol
            merged_enum = _pass4_enum_for_hit(ol)

    insured_pdf = run_car_insurance_insured_passes(page_texts_pdf, engine_label="pypdf")
    insured_mu = run_car_insurance_insured_passes(blocks_mu, engine_label="pymupdf")
    license_plate_pdf = run_car_insurance_license_plate_passes(page_texts_pdf, engine_label="pypdf")
    license_plate_mu = run_car_insurance_license_plate_passes(
        blocks_mu,
        engine_label="pymupdf",
        word_rect_items=word_rect_items,
    )
    vin_pdf = run_car_insurance_vin_passes(page_texts_pdf, engine_label="pypdf")
    vin_mu = run_car_insurance_vin_passes(
        blocks_mu,
        engine_label="pymupdf",
        word_rect_items=word_rect_items,
    )
    sign_date_pdf = run_car_insurance_sign_date_passes(
        page_texts_pdf,
        engine_label="pypdf",
        word_rect_items=word_rect_items,
    )
    sign_date_mu = run_car_insurance_sign_date_passes(
        blocks_mu,
        engine_label="pymupdf",
        word_rect_items=word_rect_items,
    )
    premium_pdf = run_car_insurance_premium_total_passes(
        blocks_pdf,
        engine_label="pypdf",
        word_rect_items=word_rect_items,
    )
    premium_mu = run_car_insurance_premium_total_passes(
        blocks_mu,
        engine_label="pymupdf",
        word_rect_items=word_rect_items,
    )

    kv = _car_insurance_empty_kv(policy_type)
    kv["保险公司名称"] = merged_name or ""
    kv["被保险人"] = (insured_mu.get("被保险人") or insured_pdf.get("被保险人")) or ""
    kv["签单日期"] = (sign_date_mu.get("签单日期") or sign_date_pdf.get("签单日期")) or ""
    kv["保险费合计"] = (premium_mu.get("保险费合计") or premium_pdf.get("保险费合计")) or ""
    kv["车牌号"] = (license_plate_mu.get("车牌号") or license_plate_pdf.get("车牌号")) or ""
    kv["车架号"] = (vin_mu.get("车架号") or vin_pdf.get("车架号")) or ""

    # 提取保险期间
    period_pdf = extract_insurance_period_from_blocks(blocks_pdf, engine_label="pypdf")
    period_mu = extract_insurance_period_from_blocks(blocks_mu, engine_label="pymupdf")

    merged_period = period_mu or period_pdf
    if merged_period:
        kv["保险期间"] = merged_period
        logger.debug("提取到保险期间: %s", merged_period)
    else:
        logger.debug("未提取到保险期间，尝试bbox回退逻辑")
        period_fallback = _extract_period_with_bbox_fallback(
            word_rect_items=word_rect_items,
        )
        if period_fallback:
            kv["保险期间"] = period_fallback
            logger.debug("bbox回退逻辑提取到保险期间: %s", period_fallback)
        else:
            kv["保险期间"] = ""
            logger.debug("bbox回退逻辑也未提取到保险期间")

    need_scalar_sign_date = not (kv.get("签单日期") or "")
    need_scalar_premium_total = not (kv.get("保险费合计") or "")
    need_scalar_period = not (kv.get("保险期间") or "")
    if need_scalar_sign_date or need_scalar_premium_total or need_scalar_period:
        scalar_llm = run_car_insurance_scalar_llm_fallback(
            word_rect_items=word_rect_items,
            need_sign_date=need_scalar_sign_date,
            need_premium_total=need_scalar_premium_total,
            need_period=need_scalar_period,
        )
        if need_scalar_sign_date and scalar_llm.get("签单日期"):
            kv["签单日期"] = scalar_llm["签单日期"]
        if need_scalar_premium_total and scalar_llm.get("保险费合计"):
            kv["保险费合计"] = scalar_llm["保险费合计"]
        if need_scalar_period and scalar_llm.get("保险期间"):
            kv["保险期间"] = scalar_llm["保险期间"]

    _commercial_detail_layout: Dict[KnownInsuranceCompany, CommercialDetailTableLayout] = {
        KnownInsuranceCompany.PING_AN: "PING_AN",
        KnownInsuranceCompany.PICC_P: "PICC_P",
        KnownInsuranceCompany.CHINA_LIFE_P: "CHINA_LIFE_P",
        KnownInsuranceCompany.TAIKANG_ONLINE: "TAIKANG_ONLINE",
        KnownInsuranceCompany.CPIC_P: "PACIFIC_P",
        KnownInsuranceCompany.YANG_GUANG: "YANG_GUANG_P",
    }
    has_commercial_detail_rule_layout = is_commercial and merged_enum in _commercial_detail_layout
    if has_commercial_detail_rule_layout:
        _dl = _commercial_detail_layout[merged_enum]
        detail_mu = run_car_insurance_commercial_detail_table_passes(
            blocks_mu,
            engine_label="pymupdf",
            detail_layout=_dl,
        )
        detail_pdf = run_car_insurance_commercial_detail_table_passes(
            blocks_pdf,
            engine_label="pypdf",
            detail_layout=_dl,
        )
        for dk, dv in _merge_commercial_detail_table_kv(detail_mu, detail_pdf).items():
            if dv:
                kv[dk] = dv

    if is_commercial and merged_enum in (KnownInsuranceCompany.BO_HAI, KnownInsuranceCompany.ZHONG_YIN):
        ded_mu = _pass1_damage_deductible_from_blocks(
            blocks_mu,
            engine_label="pymupdf",
        )
        ded_pdf = _pass1_damage_deductible_from_blocks(
            blocks_pdf,
            engine_label="pypdf",
        )
        ded = ded_mu or ded_pdf
        if not ded:
            ded = _pass2_damage_deductible_with_bbox_fallback(
                engine_label="pymupdf",
                word_rect_items=word_rect_items,
            )
        if ded:
            kv["免赔额"] = ded

    if _commercial_detail_needs_llm_fallback(
        is_commercial=is_commercial,
        known_company=merged_enum,
        has_rule_layout=has_commercial_detail_rule_layout,
        kv=kv,
    ):
        detail_p2 = run_car_insurance_commercial_detail_pass2_doubao(
            merged_enum,
            engine_label="pymupdf",
            company_name=merged_name or "",
            block_rect_items=block_rect_items,
            word_rect_items=word_rect_items,
        )
        for dk in _CAR_INSURANCE_COMMERCIAL_ONLY_KV_KEYS:
            v2 = detail_p2.get(dk) or ""
            if dk == "免赔额":
                if merged_enum == KnownInsuranceCompany.HUA_AN and v2 != "":
                    kv[dk] = v2
            elif not (kv.get(dk) or ""):
                if v2:
                    kv[dk] = v2

    return {
        "kv": kv,
        "known_insurance_company": merged_enum.name if merged_enum else "",
    }
