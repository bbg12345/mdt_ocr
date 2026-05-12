#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
腾讯云 OCR 客户端封装（本服务）

- 身份证 ``IDCardOCR`` → ``call_id_card_ocr``
- 机动车行驶证 ``VehicleLicenseOCR`` → ``call_vehicle_license_ocr``

凭证从环境变量读取：

  - TENCENTCLOUD_SECRET_ID
  - TENCENTCLOUD_SECRET_KEY
"""

from __future__ import annotations

import base64
import logging
import os
from typing import Optional

from tencentcloud.common import credential
from tencentcloud.common.exception.tencent_cloud_sdk_exception import TencentCloudSDKException
from tencentcloud.common.profile.client_profile import ClientProfile
from tencentcloud.common.profile.http_profile import HttpProfile
from tencentcloud.ocr.v20181119 import models
from tencentcloud.ocr.v20181119.ocr_client import OcrClient

logger = logging.getLogger(__name__)

TENCENT_SECRET_ID_ENV = "TENCENTCLOUD_SECRET_ID"
TENCENT_SECRET_KEY_ENV = "TENCENTCLOUD_SECRET_KEY"

OCR_ENDPOINT = "ocr.tencentcloudapi.com"


def _build_credential(
    secret_id: Optional[str] = None,
    secret_key: Optional[str] = None,
) -> credential.Credential:
    sid = (secret_id or os.environ.get(TENCENT_SECRET_ID_ENV) or "").strip()
    sk = (secret_key or os.environ.get(TENCENT_SECRET_KEY_ENV) or "").strip()
    if not sid or not sk:
        raise ValueError(
            f"请设置环境变量 {TENCENT_SECRET_ID_ENV} 与 {TENCENT_SECRET_KEY_ENV}，"
            "或在调用时传入 secret_id / secret_key"
        )
    return credential.Credential(sid, sk)


def _build_ocr_client(
    cred: credential.Credential,
    region: str = "",
) -> OcrClient:
    http_profile = HttpProfile()
    http_profile.endpoint = OCR_ENDPOINT
    client_profile = ClientProfile()
    client_profile.httpProfile = http_profile
    return OcrClient(cred, region, client_profile)


def call_id_card_ocr(
    image_base64: Optional[str] = None,
    image_url: Optional[str] = None,
    image_path: Optional[str] = None,
    *,
    card_side: Optional[str] = None,
    config: Optional[str] = None,
    secret_id: Optional[str] = None,
    secret_key: Optional[str] = None,
    region: str = "",
) -> str:
    """
    调用腾讯云「身份证识别」接口 ``IDCardOCR``。

    ``ImageUrl`` 与 ``ImageBase64`` 至少需提供一个；若同时提供，服务端优先使用 ``ImageUrl``。
    """
    if image_path:
        if image_base64 or image_url:
            raise ValueError("image_path 不能与 image_base64 / image_url 同时指定")
        with open(image_path, "rb") as f:
            image_base64 = base64.b64encode(f.read()).decode("ascii")

    if not image_url and not image_base64:
        raise ValueError("image_url、image_base64、image_path 至少需要提供一个")

    cred = _build_credential(secret_id=secret_id, secret_key=secret_key)
    client = _build_ocr_client(cred, region=region)

    req = models.IDCardOCRRequest()
    if image_url:
        req.ImageUrl = image_url
    if image_base64:
        req.ImageBase64 = image_base64
    if card_side:
        req.CardSide = card_side
    if config is not None:
        req.Config = config

    try:
        resp = client.IDCardOCR(req)
        return resp.to_json_string()
    except TencentCloudSDKException as e:
        logger.error("腾讯云身份证 IDCardOCR 失败: %s", e)
        raise


def call_vehicle_license_ocr(
    image_base64: Optional[str] = None,
    image_url: Optional[str] = None,
    image_path: Optional[str] = None,
    *,
    card_side: Optional[str] = None,
    tractor_card_side: Optional[str] = None,
    secret_id: Optional[str] = None,
    secret_key: Optional[str] = None,
    region: str = "",
) -> str:
    """
    调用腾讯云「机动车行驶证识别」接口 ``VehicleLicenseOCR``（非驾驶证 ``DriverLicenseOCR``）。

    ``ImageUrl`` 与 ``ImageBase64`` 至少需提供一个；若同时提供，服务端优先使用 ``ImageUrl``。
    """
    if image_path:
        if image_base64 or image_url:
            raise ValueError("image_path 不能与 image_base64 / image_url 同时指定")
        with open(image_path, "rb") as f:
            image_base64 = base64.b64encode(f.read()).decode("ascii")

    if not image_url and not image_base64:
        raise ValueError("image_url、image_base64、image_path 至少需要提供一个")

    cred = _build_credential(secret_id=secret_id, secret_key=secret_key)
    client = _build_ocr_client(cred, region=region)

    req = models.VehicleLicenseOCRRequest()
    if image_url:
        req.ImageUrl = image_url
    if image_base64:
        req.ImageBase64 = image_base64
    if card_side:
        req.CardSide = card_side
    if tractor_card_side:
        req.TractorCardSide = tractor_card_side

    try:
        resp = client.VehicleLicenseOCR(req)
        return resp.to_json_string()
    except TencentCloudSDKException as e:
        logger.error("腾讯云行驶证 VehicleLicenseOCR 失败: %s", e)
        raise


if __name__ == "__main__":
    print(
        "Tencent OCR client：配置环境变量后调用 "
        "call_id_card_ocr / call_vehicle_license_ocr"
    )
