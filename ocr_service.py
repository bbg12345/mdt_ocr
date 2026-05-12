#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OCR HTTP 服务：身份证（IDCardOCR）、机动车行驶证（VehicleLicenseOCR，路由名 driver_license）。

Python 依赖请安装到上级目录的共享虚拟环境 ``../venv``（勿在本仓库单独建 venv）::

    source ../venv/bin/activate
    pip install -r requirements.txt

命令行窗口前台运行（非 daemon）：在终端执行下面任一方式，日志会打印在当前窗口，Ctrl+C 结束进程；勿加 ``&``、systemd、docker detach 等后台方式即为「普通命令行运行」。

    uvicorn ocr_service:app --host 0.0.0.0 --port 8080
    # 或等价：
    python ocr_service.py

环境变量：
  - TENCENTCLOUD_SECRET_ID / TENCENTCLOUD_SECRET_KEY：腾讯云凭证（必填）
  - OCR_IDCARD_PATH：身份证识别路由，默认 /api/v1/ocr/idcard
  - OCR_DRIVER_LICENSE_PATH：行驶证识别路由（VehicleLicenseOCR），默认 /api/v1/ocr/driver-license
  - OCR_CAR_INSURANCE_PATH：车险保单字段抽取，默认 /api/v1/ocr/car-insurance-extract
  - OCR_HOST / OCR_PORT：uvicorn 监听地址与端口（仅 __main__ 使用）
  - OCR_THREAD_POOL_SIZE：阻塞 OCR（腾讯云 SDK）调用使用的线程池大小，默认 30
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from functools import partial
from typing import Any, Callable, Dict, Literal, Optional, cast

from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from tencentcloud.common.exception.tencent_cloud_sdk_exception import TencentCloudSDKException

from car_insurance import car_insurance_extract
from clients.tencent_ocr_client import call_id_card_ocr, call_vehicle_license_ocr

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

OCR_IDCARD_PATH = os.environ.get("OCR_IDCARD_PATH", "/api/v1/ocr/idcard")
OCR_DRIVER_LICENSE_PATH = os.environ.get(
    "OCR_DRIVER_LICENSE_PATH",
    "/api/v1/ocr/driver-license",
)
OCR_CAR_INSURANCE_PATH = os.environ.get(
    "OCR_CAR_INSURANCE_PATH",
    "/api/v1/ocr/car-insurance-extract",
)


def _parse_thread_pool_size() -> int:
    try:
        return max(1, int(os.environ.get("OCR_THREAD_POOL_SIZE", "30")))
    except ValueError:
        return 30


OCR_THREAD_POOL_SIZE = _parse_thread_pool_size()
_ocr_executor = ThreadPoolExecutor(
    max_workers=OCR_THREAD_POOL_SIZE,
    thread_name_prefix="ocr-sdk-",
)


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    yield
    _ocr_executor.shutdown(wait=True)


async def _run_ocr_in_pool(fn: Callable[..., JSONResponse], **kwargs: Any) -> JSONResponse:
    """在线程池中执行同步 OCR 逻辑，避免阻塞 asyncio 事件循环。"""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_ocr_executor, partial(fn, **kwargs))


app = FastAPI(
    title="MDT OCR 服务",
    description="提供 **身份证**、**机动车行驶证** 及 **车险保单 PDF 抽取** 等接口（路径不同，同一进程与端口）。",
    version="1.0.0",
    lifespan=_lifespan,
)


class IdCardJsonBody(BaseModel):
    """JSON 请求体：``image_base64`` 与 ``image_url`` 至少填一个。"""

    image_base64: Optional[str] = None
    image_url: Optional[str] = None
    card_side: Optional[str] = Field(
        None,
        description="FRONT：人像面；BACK：国徽面；不传则自动识别正反面",
    )
    config: Optional[str] = Field(None, description="可选，接口 Config JSON 字符串")


class CarInsuranceJsonBody(BaseModel):
    """车险保单抽取：``type`` 为 ``compulsory``（交强险）或 ``commercial``（商业险）；``pdf_url`` 为 PDF 地址。"""

    model_config = ConfigDict(populate_by_name=True)
    policy_type: Literal["compulsory", "commercial"] = Field(
        ...,
        alias="type",
        description="compulsory：交强险；commercial：商业险",
    )
    pdf_url: str = Field(..., min_length=1, description="PDF 的 http(s) 或本地路径")


class DriverLicenseJsonBody(BaseModel):
    """行驶证 VehicleLicenseOCR：``image_base64`` 与 ``image_url`` 至少填一个。"""

    image_base64: Optional[str] = None
    image_url: Optional[str] = None
    card_side: Optional[str] = Field(
        None,
        description="FRONT/BACK/DOUBLE，见腾讯云 VehicleLicenseOCR 文档",
    )
    tractor_card_side: Optional[str] = Field(
        None,
        description="可选，拖拉机行驶证副页：FRONT/BACK",
    )


def _tencent_result_to_json(raw: str) -> Dict[str, Any]:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"raw": raw}


# 身份证人像面 HTTP 响应仅保留以下字段（与 card_side=FRONT 时生效）
IDCARD_FRONT_KEYS: tuple[str, ...] = ("Name", "IdNum", "Sex", "Address")


def _idcard_front_keys_only(data: Dict[str, Any]) -> Dict[str, Any]:
    """从腾讯云回包中只取出正面四字字段，去掉其余键。"""
    inner = data.get("Response") if isinstance(data.get("Response"), dict) else data
    if not isinstance(inner, dict):
        return {k: None for k in IDCARD_FRONT_KEYS}
    return {k: inner.get(k) for k in IDCARD_FRONT_KEYS}


# 身份证国徽面 HTTP 响应仅保留 ValidDate（与 card_side=BACK 时生效）
IDCARD_BACK_KEYS: tuple[str, ...] = ("ValidDate",)


def _idcard_back_keys_only(data: Dict[str, Any]) -> Dict[str, Any]:
    """从腾讯云回包中只取出反面有效期限字段，去掉其余键。"""
    inner = data.get("Response") if isinstance(data.get("Response"), dict) else data
    if not isinstance(inner, dict):
        return {k: None for k in IDCARD_BACK_KEYS}
    return {k: inner.get(k) for k in IDCARD_BACK_KEYS}


def _unwrap_tencent_payload(data: Dict[str, Any]) -> Dict[str, Any]:
    inner = data.get("Response") if isinstance(data.get("Response"), dict) else data
    return inner if isinstance(inner, dict) else {}


# 行驶证主页 FrontInfo 中保留的字段（card_side=FRONT）
VEHICLE_LICENSE_FRONT_KEYS: tuple[str, ...] = (
    "PlateNo",
    "Vin",
    "EngineNo",
    "Owner",
    "Model",
    "RegisterDate",
)


def _vehicle_license_front_keys_only(data: Dict[str, Any]) -> Dict[str, Any]:
    """主页：仅保留 PlateNo, Vin, EngineNo, Owner, Model, RegisterDate（来自 FrontInfo）。"""
    inner = _unwrap_tencent_payload(data)
    fi = inner.get("FrontInfo")
    if not isinstance(fi, dict):
        return {k: None for k in VEHICLE_LICENSE_FRONT_KEYS}
    return {k: fi.get(k) for k in VEHICLE_LICENSE_FRONT_KEYS}


def _vehicle_license_back_keys_only(data: Dict[str, Any]) -> Dict[str, Any]:
    """副页：仅保留 PlateNo（来自 BackInfo）。"""
    inner = _unwrap_tencent_payload(data)
    bi = inner.get("BackInfo")
    if not isinstance(bi, dict):
        return {"PlateNo": None}
    return {"PlateNo": bi.get("PlateNo")}


def _vehicle_license_double_keys_only(data: Dict[str, Any]) -> Dict[str, Any]:
    """正副同框：FrontInfo 保留六字段，BackInfo 仅 PlateNo。"""
    inner = _unwrap_tencent_payload(data)
    fi = inner.get("FrontInfo")
    bi = inner.get("BackInfo")
    front = (
        {k: fi.get(k) for k in VEHICLE_LICENSE_FRONT_KEYS}
        if isinstance(fi, dict)
        else {k: None for k in VEHICLE_LICENSE_FRONT_KEYS}
    )
    back = {"PlateNo": bi.get("PlateNo")} if isinstance(bi, dict) else {"PlateNo": None}
    return {"FrontInfo": front, "BackInfo": back}


def _handle_id_card(
    *,
    image_base64: Optional[str],
    image_url: Optional[str],
    card_side: Optional[str],
    config: Optional[str],
) -> JSONResponse:
    try:
        raw = call_id_card_ocr(
            image_base64=image_base64,
            image_url=image_url,
            card_side=card_side,
            config=config,
        )
        content = _tencent_result_to_json(raw)
        side = (card_side or "").strip().upper()
        if side == "FRONT":
            content = _idcard_front_keys_only(content)
        elif side == "BACK":
            content = _idcard_back_keys_only(content)
        return JSONResponse(content=content)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except TencentCloudSDKException as e:
        logger.exception("腾讯云 IDCardOCR 调用失败")
        raise HTTPException(
            status_code=502,
            detail={
                "code": e.code,
                "message": e.message,
                "requestId": e.requestId,
            },
        ) from e


def _handle_vehicle_license(
    *,
    image_base64: Optional[str],
    image_url: Optional[str],
    card_side: Optional[str],
    tractor_card_side: Optional[str],
) -> JSONResponse:
    try:
        raw = call_vehicle_license_ocr(
            image_base64=image_base64,
            image_url=image_url,
            card_side=card_side,
            tractor_card_side=tractor_card_side,
        )
        content = _tencent_result_to_json(raw)
        side = (card_side or "").strip().upper()
        if side == "FRONT":
            content = _vehicle_license_front_keys_only(content)
        elif side == "BACK":
            content = _vehicle_license_back_keys_only(content)
        elif side == "DOUBLE":
            content = _vehicle_license_double_keys_only(content)
        return JSONResponse(content=content)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except TencentCloudSDKException as e:
        logger.exception("腾讯云 VehicleLicenseOCR 调用失败")
        raise HTTPException(
            status_code=502,
            detail={
                "code": e.code,
                "message": e.message,
                "requestId": e.requestId,
            },
        ) from e


def _handle_car_insurance_extract(
    *,
    policy_type: str,
    pdf_url: str,
) -> JSONResponse:
    try:
        payload = car_insurance_extract(
            policy_type=cast(Literal["compulsory", "commercial"], policy_type),
            pdf_url=pdf_url,
        )
        return JSONResponse(content=payload)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except OSError as e:
        raise HTTPException(status_code=400, detail=f"无法读取 PDF：{e}") from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.exception("车险保单 PDF 处理失败")
        raise HTTPException(status_code=502, detail=str(e)) from e


@app.get("/health")
def health() -> Dict[str, Any]:
    return {
        "status": "ok",
        "id_card_ocr": OCR_IDCARD_PATH,
        "driver_license_ocr": OCR_DRIVER_LICENSE_PATH,
        "car_insurance_extract": OCR_CAR_INSURANCE_PATH,
        "ocr_thread_pool_size": OCR_THREAD_POOL_SIZE,
    }


@app.post(OCR_IDCARD_PATH, tags=["身份证 IDCardOCR"])
async def id_card_ocr(request: Request) -> JSONResponse:
    """
    支持 ``application/json``（字段见 ``IdCardJsonBody``）或
    ``multipart/form-data``（字段 ``file`` 为图片文件，可选 ``card_side``、``config``）。

    当 ``card_side`` 为 ``FRONT`` 时，响应体仅包含 ``Name``、``IdNum``、``Sex``、``Address``；
    为 ``BACK`` 时仅包含 ``ValidDate``；
    其他取值或未传时返回腾讯云完整字段。
    """
    ct = (request.headers.get("content-type") or "").lower()

    if "application/json" in ct:
        try:
            body = await request.json()
        except json.JSONDecodeError as e:
            raise HTTPException(status_code=400, detail="无效的 JSON 请求体") from e
        parsed = IdCardJsonBody.model_validate(body)
        return await _run_ocr_in_pool(
            _handle_id_card,
            image_base64=parsed.image_base64,
            image_url=parsed.image_url,
            card_side=parsed.card_side,
            config=parsed.config,
        )

    if "multipart/form-data" in ct:
        form = await request.form()
        file = form.get("file")
        if file is None:
            raise HTTPException(status_code=400, detail="multipart 请求缺少 file 字段")
        if not isinstance(file, UploadFile):
            raise HTTPException(status_code=400, detail="file 必须是上传文件")

        raw_bytes = await file.read()
        if not raw_bytes:
            raise HTTPException(status_code=400, detail="上传文件为空")
        b64 = base64.b64encode(raw_bytes).decode("ascii")

        card_side_val = form.get("card_side")
        card_side = str(card_side_val) if card_side_val else None
        config_val = form.get("config")
        config = str(config_val) if config_val else None

        return await _run_ocr_in_pool(
            _handle_id_card,
            image_base64=b64,
            image_url=None,
            card_side=card_side,
            config=config,
        )

    raise HTTPException(
        status_code=415,
        detail="仅支持 Content-Type: application/json 或 multipart/form-data",
    )


@app.post(OCR_DRIVER_LICENSE_PATH, tags=["行驶证 VehicleLicenseOCR"])
async def driver_license_ocr(request: Request) -> JSONResponse:
    """
    机动车行驶证识别（腾讯云 ``VehicleLicenseOCR``）：JSON 或 multipart，字段与身份证接口类似，
    额外支持 ``tractor_card_side``（拖拉机行驶证）。

    响应裁剪：``FRONT`` 时仅 ``PlateNo``、``Vin``、``EngineNo``、``Owner``、``Model``、``RegisterDate``
   （取自 ``FrontInfo``）；``BACK`` 时仅 ``PlateNo``（取自 ``BackInfo``）；``DOUBLE`` 时返回
    ``FrontInfo``（上述六字段）与 ``BackInfo``（仅 ``PlateNo``）；其他或未传 ``card_side`` 时返回完整字段。
    """
    ct = (request.headers.get("content-type") or "").lower()

    if "application/json" in ct:
        try:
            body = await request.json()
        except json.JSONDecodeError as e:
            raise HTTPException(status_code=400, detail="无效的 JSON 请求体") from e
        parsed = DriverLicenseJsonBody.model_validate(body)
        return await _run_ocr_in_pool(
            _handle_vehicle_license,
            image_base64=parsed.image_base64,
            image_url=parsed.image_url,
            card_side=parsed.card_side,
            tractor_card_side=parsed.tractor_card_side,
        )

    if "multipart/form-data" in ct:
        form = await request.form()
        file = form.get("file")
        if file is None:
            raise HTTPException(status_code=400, detail="multipart 请求缺少 file 字段")
        if not isinstance(file, UploadFile):
            raise HTTPException(status_code=400, detail="file 必须是上传文件")

        raw_bytes = await file.read()
        if not raw_bytes:
            raise HTTPException(status_code=400, detail="上传文件为空")
        b64 = base64.b64encode(raw_bytes).decode("ascii")

        card_side_val = form.get("card_side")
        card_side = str(card_side_val) if card_side_val else None
        tractor_val = form.get("tractor_card_side")
        tractor_card_side = str(tractor_val) if tractor_val else None

        return await _run_ocr_in_pool(
            _handle_vehicle_license,
            image_base64=b64,
            image_url=None,
            card_side=card_side,
            tractor_card_side=tractor_card_side,
        )

    raise HTTPException(
        status_code=415,
        detail="仅支持 Content-Type: application/json 或 multipart/form-data",
    )


@app.post(OCR_CAR_INSURANCE_PATH, tags=["车险保单抽取"])
async def api_car_insurance_extract(body: CarInsuranceJsonBody) -> JSONResponse:
    """
    JSON 请求体字段：``type``（``compulsory`` / ``commercial``）、``pdf_url``（PDF 的 URL 或本地路径）。

    响应含 ``kv``（键值字段，未识别为空字符串 ``""``）、``known_insurance_company``（pass4 枚举名，未命中为 ``""``）；
    商业险且承保枚举为平安、中国人民财产保险、中国人寿财产保险、泰康在线财产保险、太平洋财产保险或阳光财产保险时，由 pypdf 与 PyMuPDF 双路块跑
    「保险费明细抽取」后按字段合并填各主险保额/保费及免赔额（pymupdf 优先，见 ``car_insurance``）；
    各 pass 见日志（debug：pass1/2 选用或命中、pass3 过滤、pass4 枚举、pymupdf 独有 pass5 豆包兜底等）。
    Optional：承保公司豆包兜底需 ``CAR_INSURANCE_DOUBAO_API_KEY``、``CAR_INSURANCE_DOUBAO_MODEL``（方舟接入点）；
    请求基址固定为 ``https://ark.cn-beijing.volces.com/api/v3``。
    """
    return await _run_ocr_in_pool(
        _handle_car_insurance_extract,
        policy_type=body.policy_type,
        pdf_url=body.pdf_url,
    )


if __name__ == "__main__":
    import uvicorn

    host = os.environ.get("OCR_HOST", "0.0.0.0")
    port = int(os.environ.get("OCR_PORT", "8080"))
    uvicorn.run("ocr_service:app", host=host, port=port, reload=False)
