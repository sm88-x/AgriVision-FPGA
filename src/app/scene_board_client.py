
# Locate the repository independently of launch directory and source depth.
from pathlib import Path as _LayoutPath
import sys as _layout_sys
PROJECT_ROOT = next(p for p in _LayoutPath(__file__).resolve().parents
                    if (p / "LICENSE").is_file() and (p / "src").is_dir())
for _layout_rel in ("src", "src/app", "src/adapters",
                    "src/algorithm", "hardware/board", "hardware/drivers"):
    _layout_sys.path.insert(0, str(PROJECT_ROOT / _layout_rel))

# -*- coding: utf-8 -*-
import hashlib
import json
import math
import struct
import time
import uuid
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler

import numpy as np

def process_board(bgr, frame_id, address, mode, timeout=10.0):
    height, width = bgr.shape[:2]

    b = bgr[:, :, 0].astype(np.uint32)
    g = bgr[:, :, 1].astype(np.uint32)
    r = bgr[:, :, 2].astype(np.uint32)
    payload = ((r << 16) | (g << 8) | b).astype("<u4").tobytes()

    request_id = uuid.uuid4().hex
    request = Request(
        "http://" + address + "/frame",
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/octet-stream",
            "X-Width": str(width),
            "X-Height": str(height),
            "X-Frame-Id": str(frame_id),
            "X-Request-Id": request_id,
            "X-Control-Mode": mode,
        },
    )

    started = time.perf_counter()
    try:
        with build_opener(ProxyHandler({})).open(request, timeout=timeout) as response:
            # 限制响应长度，避免错误响应无限读取。
            limit = width * height + 1024 * 1024
            body = response.read(limit + 1)
            if len(body) > limit:
                raise RuntimeError("板端响应超过允许长度")
    except HTTPError as exc:
        detail = exc.read(65536).decode("utf-8", errors="replace")
        raise RuntimeError("板端处理失败：" + detail) from exc

    rtt_ms = (time.perf_counter() - started) * 1000

    if len(body) < 4:
        raise RuntimeError("板端响应缺少元数据长度")

    length = struct.unpack("!I", body[:4])[0]
    if length > 1024 * 1024 or len(body) < 4 + length:
        raise RuntimeError("板端元数据长度错误")

    meta = json.loads(body[4:4 + length].decode("utf-8"))
    pixels = body[4 + length:]

    if meta.get("request_id") != request_id:
        raise RuntimeError("板端 request_id 不一致")
    if meta.get("frame_id") != frame_id:
        raise RuntimeError("板端 frame_id 不一致")
    if meta.get("mode") != mode:
        raise RuntimeError("板端模式不一致")
    if (meta.get("width"), meta.get("height")) != (width, height):
        raise RuntimeError("板端尺寸不一致")
    if len(pixels) != width * height:
        raise RuntimeError("板端图像长度不一致")

    if meta.get("input_sha256") != hashlib.sha256(payload).hexdigest():
        raise RuntimeError("板端处理的输入图像与本次请求不一致")
    if meta.get("output_sha256") != hashlib.sha256(pixels).hexdigest():
        raise RuntimeError("板端输出校验失败")

    if meta.get("simulated", False):
        raise RuntimeError("板端返回模拟回执，拒绝将其作为真板结果")
    if meta.get("hardware") is not True:
        raise RuntimeError("板端回执 hardware 不为 true，本次结果未经过真板")
    if meta.get("register_verified") is not True:
        raise RuntimeError("真板响应缺少寄存器验证")
    if meta.get("applied_frame") != frame_id:
        raise RuntimeError("配置生效帧号不一致")

    config = meta.get("applied_config")
    registers = meta.get("actual_registers")
    if not isinstance(config, dict) or not isinstance(registers, list):
        raise RuntimeError("真板回执缺少实际配置或寄存器读回值")
    names = {"width": width, "height": height, "mode": config.get("edge_mode"),
             "threshold": config.get("threshold"), "morph_mode": config.get("morph_mode"),
             "enable_gray": config.get("enable_gray"), "enable_gauss": config.get("enable_gauss"),
             "out_sel": config.get("out_sel")}
    actual = {r.get("name"): r.get("value") for r in registers if isinstance(r, dict)}
    if any(type(value) is not int or actual.get(name) != value for name, value in names.items()):
        raise RuntimeError("真板寄存器读回值与本帧配置不一致")
    elapsed = meta.get("fpga_dma_ms")
    if type(elapsed) not in (int, float) or not math.isfinite(elapsed) or elapsed < 0:
        raise RuntimeError("真板回执缺少有效的 DMA 耗时")

    image = np.frombuffer(
        pixels, dtype=np.uint8
    ).reshape(height, width).copy()

    meta["transport_rtt_ms"] = rtt_ms
    return image, meta
